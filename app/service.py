from __future__ import annotations

import asyncio
import shutil
import zipfile
from collections import Counter
from pathlib import Path

import aiohttp

from .config import Settings
from .distributor import distribute_barcode
from .excel_io import build_summary, read_input_xlsx, write_warehouse_files
from .history import cleanup_history, make_run_dir, write_json
from .kits import read_kits_xlsx
from .models import DistributionLine, KitDefinition, ProductVariant, Warehouse
from .wb_api import WBClient, count_orders_by_variant_and_warehouse


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
        return len(read_kits_xlsx(self.kits_path))

    def update_kits_template(self, input_path: Path) -> int:
        kits = read_kits_xlsx(input_path)  # сначала валидируем
        tmp = self.kits_path.with_suffix(".tmp.xlsx")
        shutil.copy2(input_path, tmp)
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
        input_copy = run_dir / "input.xlsx"
        shutil.copy2(input_path, input_copy)
        output_dir = run_dir / "output"

        stock = read_input_xlsx(input_copy)
        kits: list[KitDefinition] = read_kits_xlsx(self.kits_path) if self.kits_path.exists() else []

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
            minimum = min(qty, len(candidates)) if candidates else 0
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
                    "built": 0,
                    "components": dict(component_counts),
                })
                continue

            line = distribute_barcode(
                barcode=kit.barcode,
                quantity=max_sets,
                warehouses=warehouses,
                barcode_variants_by_cabinet=variants_by_cabinet,
                order_counts_by_cabinet=counts_by_cabinet,
                threshold=self.settings.distribute_all_threshold,
                no_sales_target=self.settings.no_sales_target,
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
            )
            # Для физического отчёта source_qty — исходный остаток до сборки комплектов.
            line.source_qty = original_qty
            single_lines.append(line)

        lines = single_lines + kit_lines

        all_barcodes_by_cabinet = {
            cabinet_key: set(barcode_map.keys())
            for cabinet_key, barcode_map in variants_by_cabinet.items()
        }
        files = write_warehouse_files(
            self.template_path,
            output_dir,
            warehouses,
            lines,
            all_barcodes_by_cabinet,
        )
        summary = build_summary(
            lines=lines,
            output_files=len(files),
            no_sales=no_sales,
            not_found=not_found,
            no_sales_kits=no_sales_kits,
            not_found_kits=not_found_kits,
            physical_input_units=sum(stock.values()),
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
