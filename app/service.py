from __future__ import annotations

import asyncio
import shutil
import zipfile
from collections import Counter
from pathlib import Path

import aiohttp

from .config import Settings
from .distributor import distribute_barcode
from .excel_io import build_summary, read_input_excel, write_warehouse_files
from .history import cleanup_history, make_run_dir, write_json
from .kits import read_kits_excel
from .models import DistributionLine, KitDefinition, ProductVariant, Warehouse
from .wb_api import WBClient, count_orders_by_variant_and_warehouse


def _kit_target_sets(possible_sets: int, sales_14d: int, warehouse_count: int, safety_stock_per_warehouse: int = 4) -> int:
    """Return how many kits should actually be built.

    Demand is capped by 14-day orders, while preserving minimum presence of
    one kit per available FBS warehouse whenever components are sufficient.
    Physical availability always has the final say.
    """
    if possible_sets <= 0 or warehouse_count <= 0:
        return 0
    minimum_presence = int(warehouse_count) * max(1, int(safety_stock_per_warehouse))
    demand_target = max(int(sales_14d), minimum_presence)
    return min(int(possible_sets), demand_target)


def _build_sku_alias_groups(
    variants_by_cabinet: dict[str, dict[str, ProductVariant]],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Build global SKU alias groups from WB variants.

    Every set of SKUs belonging to one chrtID is one product variation. If a SKU
    is shared between cabinets, the groups are merged across cabinets.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Stable root makes reports/tests deterministic.
        if ra <= rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    seen_variants: set[tuple[str, int]] = set()
    for cabinet_key, barcode_map in variants_by_cabinet.items():
        for variant in barcode_map.values():
            marker = (cabinet_key, variant.chrt_id)
            if marker in seen_variants:
                continue
            seen_variants.add(marker)
            skus = [str(x).strip() for x in variant.skus if str(x).strip()]
            if not skus:
                continue
            for sku in skus:
                find(sku)
            first = skus[0]
            for sku in skus[1:]:
                union(first, sku)

    groups: dict[str, set[str]] = {}
    for sku in list(parent):
        root = find(sku)
        groups.setdefault(root, set()).add(sku)
    alias_root = {sku: root for root, members in groups.items() for sku in members}
    return alias_root, groups


def _expand_variants_for_aliases(
    actual_by_cabinet: dict[str, dict[str, ProductVariant]],
    groups: dict[str, set[str]],
) -> dict[str, dict[str, ProductVariant]]:
    """Allow any alias SKU to resolve to the cabinet's matching chrtID variant."""
    expanded: dict[str, dict[str, ProductVariant]] = {}
    for cabinet_key, actual in actual_by_cabinet.items():
        mapping = dict(actual)
        for members in groups.values():
            variant = next((actual[sku] for sku in members if sku in actual), None)
            if variant is None:
                continue
            for sku in members:
                mapping[sku] = variant
        expanded[cabinet_key] = mapping
    return expanded


def _normalize_stock_by_alias_group(
    stock: dict[str, int],
    excluded_barcodes: set[str],
    alias_root: dict[str, str],
) -> tuple[dict[str, int], set[str], dict[str, str], dict[str, list[str]]]:
    """Aggregate physical stock when several input SKUs represent one chrtID.

    If any alias in a group is marked `!`, the whole variation becomes manual.
    Returns normalized stock, excluded group roots, preferred input SKU per group,
    and a diagnostic mapping of merged input SKUs.
    """
    excluded_roots = {alias_root.get(sku, sku) for sku in excluded_barcodes}
    preferred: dict[str, str] = {}
    grouped_qty: dict[str, int] = {}
    grouped_inputs: dict[str, list[str]] = {}

    for sku, qty in stock.items():
        root = alias_root.get(sku, sku)
        if root in excluded_roots:
            continue
        preferred.setdefault(root, sku)
        grouped_qty[root] = grouped_qty.get(root, 0) + int(qty)
        grouped_inputs.setdefault(root, []).append(sku)

    normalized = {preferred[root]: qty for root, qty in grouped_qty.items()}
    return normalized, excluded_roots, preferred, grouped_inputs


def _build_output_sku_maps(
    actual_by_cabinet: dict[str, dict[str, ProductVariant]],
    groups: dict[str, set[str]],
    alias_root: dict[str, str],
    excluded_roots: set[str],
    preferred_input_by_root: dict[str, str],
    kit_barcodes: set[str],
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Choose exactly one output SKU per product variation and cabinet."""
    all_output: dict[str, set[str]] = {}
    canonical_map: dict[str, dict[str, str]] = {}

    for cabinet_key, actual in actual_by_cabinet.items():
        output_set: set[str] = set()
        alias_to_output: dict[str, str] = {}
        processed_chrt: set[int] = set()

        # Iterate unique real variants in the cabinet, not every SKU.
        for variant in actual.values():
            if variant.chrt_id in processed_chrt:
                continue
            processed_chrt.add(variant.chrt_id)
            real_skus = [sku for sku in variant.skus if sku]
            if not real_skus:
                continue
            root = alias_root.get(real_skus[0], real_skus[0])
            if root in excluded_roots:
                continue
            members = groups.get(root, set(real_skus))

            preferred = preferred_input_by_root.get(root)
            if preferred not in real_skus:
                preferred = next((sku for sku in real_skus if sku in kit_barcodes), None)
            if preferred not in real_skus:
                preferred = sorted(real_skus)[0]

            output_set.add(preferred)
            for alias in members | set(real_skus):
                alias_to_output[alias] = preferred

        all_output[cabinet_key] = output_set
        canonical_map[cabinet_key] = alias_to_output

    return all_output, canonical_map


class DistributionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.template_path = settings.base_dir / "templates" / "stocks_template.xlsx"
        self.default_kits_path = settings.base_dir / "templates" / "kits_template.xlsx"
        self.data_dir = settings.base_dir / "data"
        self.kits_path = self.data_dir / "kits_template.xlsx"
        self.history_root = self.data_dir / "history"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.kits_path.exists() and self.default_kits_path.exists():
            shutil.copy2(self.default_kits_path, self.kits_path)

    def kits_count(self) -> int:
        if not self.kits_path.exists():
            return 0
        return len(read_kits_excel(self.kits_path))

    def update_kits_template(self, input_path: Path) -> int:
        kits = read_kits_excel(input_path)  # сначала валидируем
        tmp = self.kits_path.with_suffix(".tmp.xlsx")
        if input_path.suffix.lower() == ".xlsx":
            shutil.copy2(input_path, tmp)
        else:
            # Нормализуем старый XLS в XLSX для постоянного хранения.
            from openpyxl import Workbook
            import xlrd
            src = xlrd.open_workbook(input_path).sheet_by_index(0)
            wb = Workbook()
            ws = wb.active
            for r in range(src.nrows):
                ws.append([src.cell_value(r, c) for c in range(src.ncols)])
            wb.save(tmp)
        tmp.replace(self.kits_path)
        return len(kits)

    @staticmethod
    def _candidate_warehouses(
        barcode: str,
        warehouses: list[Warehouse],
        variants_by_cabinet: dict[str, dict[str, ProductVariant]],
    ) -> list[Warehouse]:
        return [
            wh for wh in warehouses
            if barcode in variants_by_cabinet.get(wh.cabinet_key, {})
        ]

    @staticmethod
    def _total_sales(
        barcode: str,
        warehouses: list[Warehouse],
        variants_by_cabinet: dict[str, dict[str, ProductVariant]],
        counts_by_cabinet: dict[str, dict[tuple[int, int], int]],
    ) -> int:
        total = 0
        for wh in warehouses:
            variant = variants_by_cabinet.get(wh.cabinet_key, {}).get(barcode)
            if variant:
                total += counts_by_cabinet.get(wh.cabinet_key, {}).get(
                    (variant.chrt_id, wh.warehouse_id), 0
                )
        return total

    @staticmethod
    def _resolve_component_stock_barcode(
        component_barcode: str,
        stock: dict[str, int],
        variants_by_cabinet: dict[str, dict[str, ProductVariant]],
    ) -> str:
        """Resolve an alternate WB SKU to the physical barcode present in input stock.

        Exact barcode always wins. If the kit template contains another SKU of the
        same WB size (chrtID), use the single physical SKU present in the input file.
        """
        if component_barcode in stock:
            return component_barcode

        for source_barcode in stock:
            for cabinet_key, barcode_map in variants_by_cabinet.items():
                component_variant = barcode_map.get(component_barcode)
                source_variant = barcode_map.get(source_barcode)
                if (
                    component_variant
                    and source_variant
                    and component_variant.chrt_id == source_variant.chrt_id
                ):
                    return source_barcode
        return component_barcode

    async def process(self, input_path: Path, user_id: int) -> dict:
        cleanup_history(self.history_root, self.settings.retention_days)
        run_dir = make_run_dir(self.history_root, user_id)
        input_copy = run_dir / f"input{input_path.suffix.lower()}"
        shutil.copy2(input_path, input_copy)
        output_dir = run_dir / "output"

        stock, excluded_barcodes = read_input_excel(input_copy)
        kits: list[KitDefinition] = read_kits_excel(self.kits_path) if self.kits_path.exists() else []

        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            clients = [WBClient(c, session) for c in self.settings.cabinets]
            results = await asyncio.gather(*[
                asyncio.gather(
                    client.get_warehouses(),
                    client.get_orders(self.settings.lookback_days),
                    client.get_catalog_variants(),
                )
                for client in clients
            ])

        warehouses: list[Warehouse] = []
        variants_by_cabinet: dict[str, dict[str, ProductVariant]] = {}
        counts_by_cabinet: dict[str, dict[tuple[int, int], int]] = {}
        diagnostic = {"cabinets": {}}

        for cabinet, (whs, orders, catalog_tuple) in zip(self.settings.cabinets, results):
            by_barcode, by_chrt = catalog_tuple
            warehouses.extend(whs)
            variants_by_cabinet[cabinet.key] = by_barcode
            valid_ids = {w.warehouse_id for w in whs}
            counts = count_orders_by_variant_and_warehouse(orders, valid_ids)
            counts_by_cabinet[cabinet.key] = counts
            diagnostic["cabinets"][cabinet.key] = {
                "name": cabinet.name,
                "warehouses": [{"id": w.warehouse_id, "name": w.name} for w in whs],
                "orders_14d": len(orders),
                "catalog_barcodes": len(by_barcode),
                "catalog_variants": len(by_chrt),
            }

        if not warehouses:
            raise RuntimeError("WB API не вернул ни одного FBS-склада ни в одном кабинете")

        # WB может иметь несколько ШК у одной товарной вариации (один chrtID).
        # Объединяем такие ШК в одну логическую сущность до любых расчётов.
        actual_variants_by_cabinet = variants_by_cabinet
        alias_root, alias_groups = _build_sku_alias_groups(actual_variants_by_cabinet)
        stock, excluded_roots, preferred_input_by_root, grouped_inputs = _normalize_stock_by_alias_group(
            stock, excluded_barcodes, alias_root
        )
        variants_by_cabinet = _expand_variants_for_aliases(actual_variants_by_cabinet, alias_groups)

        merged_input_aliases = {
            preferred_input_by_root[root]: skus
            for root, skus in grouped_inputs.items()
            if len(set(skus)) > 1
        }
        diagnostic["merged_input_aliases"] = merged_input_aliases

        # --- Фаза 1. Резервируем обязательный минимум одиночного товара. ---
        # Это и есть приоритет одиночного товара: комплект никогда не забирает
        # единицы, необходимые для 1 шт. одиночного SKU на каждый доступный склад.
        reserved_for_single: dict[str, int] = {}
        kit_available: dict[str, int] = {}
        for barcode, qty in stock.items():
            if qty <= 0:
                reserved_for_single[barcode] = 0
                kit_available[barcode] = 0
                continue
            candidates = self._candidate_warehouses(barcode, warehouses, variants_by_cabinet)
            minimum = min(qty, len(candidates) * self.settings.safety_stock_per_warehouse) if candidates else 0
            reserved_for_single[barcode] = minimum
            kit_available[barcode] = qty - minimum

        not_found: list[str] = []
        no_sales: list[str] = []
        for barcode, qty in stock.items():
            if qty <= 0:
                continue
            candidates = self._candidate_warehouses(barcode, warehouses, variants_by_cabinet)
            if not candidates:
                not_found.append(barcode)
            elif self._total_sales(barcode, warehouses, variants_by_cabinet, counts_by_cabinet) == 0:
                no_sales.append(barcode)

        # --- Фаза 2. Формируем комплекты из остатка после обязательного минимума. ---
        valid_kits: list[tuple[KitDefinition, Counter[str], int]] = []
        not_found_kits: list[str] = []
        no_sales_kits: list[str] = []

        for kit in kits:
            # Если баркод комплекта помечен ! во входном файле, он полностью
            # ведётся вручную и бот не должен формировать/обнулять его.
            if alias_root.get(kit.barcode, kit.barcode) in excluded_roots:
                continue
            # Если любой компонент набора (включая альтернативный ШК того же
            # chrtID) помечен !, этот набор не формируем.
            if any(alias_root.get(component, component) in excluded_roots for component in kit.components):
                continue
            kit_candidates = self._candidate_warehouses(kit.barcode, warehouses, variants_by_cabinet)
            if not kit_candidates:
                not_found_kits.append(kit.barcode)
                continue

            sales = self._total_sales(kit.barcode, warehouses, variants_by_cabinet, counts_by_cabinet)
            if sales == 0:
                no_sales_kits.append(kit.barcode)

            resolved = [
                self._resolve_component_stock_barcode(component, stock, variants_by_cabinet)
                for component in kit.components
            ]
            component_counts = Counter(resolved)
            valid_kits.append((kit, component_counts, sales))

        # При дефиците компонентов: сначала более продаваемые комплекты,
        # при равенстве — порядок строк в шаблоне.
        valid_kits.sort(key=lambda x: (-x[2], x[0].row_number))

        kit_lines: list[DistributionLine] = []
        kit_report: list[dict] = []
        component_consumed: Counter[str] = Counter()

        for kit, component_counts, sales in valid_kits:
            max_sets: int | None = None
            for component_barcode, multiplicity in component_counts.items():
                available = kit_available.get(component_barcode, 0)
                possible = available // multiplicity
                max_sets = possible if max_sets is None else min(max_sets, possible)
            max_sets = int(max_sets or 0)
            if max_sets <= 0:
                kit_report.append({
                    "barcode": kit.barcode,
                    "name": kit.name,
                    "sales_14d": sales,
                    "possible_before_distribution": 0,
                    "target_sets": 0,
                    "built": 0,
                    "components": dict(component_counts),
                })
                continue

            # Комплекты нельзя собирать из всего доступного остатка компонентов.
            # Их целевое количество ограничиваем реальной потребностью за 14 дней,
            # но одновременно сохраняем обязательное присутствие минимум по 1 шт.
            # на каждом FBS-складе, если компонентов хватает.
            kit_candidates = self._candidate_warehouses(kit.barcode, warehouses, variants_by_cabinet)
            minimum_presence = len(kit_candidates)
            target_sets = _kit_target_sets(max_sets, sales, minimum_presence, self.settings.safety_stock_per_warehouse)

            line = distribute_barcode(
                barcode=kit.barcode,
                quantity=target_sets,
                warehouses=warehouses,
                barcode_variants_by_cabinet=variants_by_cabinet,
                order_counts_by_cabinet=counts_by_cabinet,
                # Для комплектов целевое количество уже рассчитано выше.
                # Поэтому округлённый хвост не оставляем виртуальным резервом:
                # распределяем весь target_sets по складам.
                threshold=max(self.settings.distribute_all_threshold, target_sets),
                no_sales_target=self.settings.no_sales_target,
                safety_stock_per_warehouse=self.settings.safety_stock_per_warehouse,
            )
            built = sum(line.allocations.values())
            # Виртуальный резерв комплектов не строим физически: компоненты
            # списываются только на реально выставленное количество комплектов.
            line.source_qty = 0
            line.reserve_qty = 0
            line.is_kit = True

            if built > 0:
                for component_barcode, multiplicity in component_counts.items():
                    used = built * multiplicity
                    kit_available[component_barcode] = max(0, kit_available.get(component_barcode, 0) - used)
                    component_consumed[component_barcode] += used
                kit_lines.append(line)

            kit_report.append({
                "barcode": kit.barcode,
                "name": kit.name,
                "sales_14d": sales,
                "possible_before_distribution": max_sets,
                "target_sets": target_sets,
                "built": built,
                "components": dict(component_counts),
                "allocations": {f"{k[0]}:{k[1]}": v for k, v in line.allocations.items() if v},
            })

        # --- Фаза 3. Всё, что осталось после комплектов, снова отдаём одиночным SKU. ---
        single_lines: list[DistributionLine] = []
        for barcode, original_qty in stock.items():
            if original_qty <= 0:
                single_lines.append(DistributionLine(barcode=barcode, source_qty=original_qty))
                continue

            available_for_single = reserved_for_single.get(barcode, 0) + kit_available.get(barcode, 0)
            line = distribute_barcode(
                barcode=barcode,
                quantity=available_for_single,
                warehouses=warehouses,
                barcode_variants_by_cabinet=variants_by_cabinet,
                order_counts_by_cabinet=counts_by_cabinet,
                threshold=self.settings.distribute_all_threshold,
                no_sales_target=self.settings.no_sales_target,
                safety_stock_per_warehouse=self.settings.safety_stock_per_warehouse,
            )
            # Для физического отчёта source_qty — исходный остаток до сборки комплектов.
            line.source_qty = original_qty
            single_lines.append(line)

        lines = single_lines + kit_lines

        all_barcodes_by_cabinet, output_barcode_by_cabinet = _build_output_sku_maps(
            actual_by_cabinet=actual_variants_by_cabinet,
            groups=alias_groups,
            alias_root=alias_root,
            excluded_roots=excluded_roots,
            preferred_input_by_root=preferred_input_by_root,
            kit_barcodes={kit.barcode for kit in kits},
        )
        files = write_warehouse_files(
            self.template_path,
            output_dir,
            warehouses,
            lines,
            all_barcodes_by_cabinet,
            output_barcode_by_cabinet=output_barcode_by_cabinet,
        )
        summary = build_summary(
            lines=lines,
            output_files=len(files),
            no_sales=no_sales,
            not_found=not_found,
            no_sales_kits=no_sales_kits,
            not_found_kits=not_found_kits,
            physical_input_units=sum(stock.values()),
            excluded_barcodes=excluded_barcodes,
        )

        report = {
            "input_lines": summary.input_lines,
            "input_units": summary.input_units,
            "allocated_physical_units": summary.allocated_units,
            "reserve_units": summary.reserve_units,
            "output_sku_units": summary.output_units,
            "kit_units": summary.kit_units,
            "kit_skus": summary.kit_skus,
            "output_files": summary.output_files,
            "no_sales_barcodes": no_sales,
            "not_found_barcodes": not_found,
            "no_sales_kit_barcodes": no_sales_kits,
            "not_found_kit_barcodes": not_found_kits,
            "excluded_barcodes": sorted(excluded_barcodes),
            "excluded_variant_groups": sorted(excluded_roots),
            "merged_input_aliases": merged_input_aliases,
            "component_consumed_by_kits": dict(component_consumed),
            "kits": kit_report,
            "allocations": [
                {
                    "barcode": line.barcode,
                    "is_kit": line.is_kit,
                    "source_qty": line.source_qty,
                    "reserve_qty": line.reserve_qty,
                    "allocations": {f"{k[0]}:{k[1]}": v for k, v in line.allocations.items() if v},
                }
                for line in lines
            ],
            "diagnostic": diagnostic,
        }
        write_json(run_dir / "report.json", report)

        zip_path = run_dir / "Распределение_FBS.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
            zf.write(run_dir / "report.json", arcname="report.json")

        return {
            "run_dir": run_dir,
            "files": files,
            "zip": zip_path,
            "summary": summary,
            "report": report,
        }
