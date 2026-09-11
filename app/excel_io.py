from __future__ import annotations

import re
import shutil
from pathlib import Path

from openpyxl import load_workbook

from .models import DistributionLine, RunSummary, Warehouse


BARCODE_HEADERS = {"баркод", "штрихкод", "barcode", "шк"}
QTY_HEADERS = {"количество", "кол-во", "qty", "quantity", "остаток"}


def _norm(value) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def read_input_xlsx(path: Path) -> dict[str, int]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    headers = {_norm(ws.cell(1, col).value): col for col in range(1, ws.max_column + 1)}
    barcode_col = next((headers[h] for h in BARCODE_HEADERS if h in headers), None)
    qty_col = next((headers[h] for h in QTY_HEADERS if h in headers), None)
    if not barcode_col or not qty_col:
        raise ValueError("В файле должны быть две колонки: Баркод и Количество")

    result: dict[str, int] = {}
    for row in range(2, ws.max_row + 1):
        barcode_raw = ws.cell(row, barcode_col).value
        qty_raw = ws.cell(row, qty_col).value
        if barcode_raw is None and qty_raw is None:
            continue
        barcode = str(barcode_raw or "").strip()
        if barcode.endswith(".0") and barcode[:-2].isdigit():
            barcode = barcode[:-2]
        if not barcode:
            continue
        try:
            qty = int(float(qty_raw or 0))
        except (TypeError, ValueError):
            raise ValueError(f"Некорректное количество для ШК {barcode}: {qty_raw}")
        if qty < 0:
            raise ValueError(f"Количество не может быть отрицательным: ШК {barcode}")
        result[barcode] = result.get(barcode, 0) + qty
    if not result:
        raise ValueError("В файле нет строк с остатками")
    return result


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", value).strip().strip(".")
    return value[:140] or "warehouse"


def write_warehouse_files(
    template_path: Path,
    output_dir: Path,
    warehouses: list[Warehouse],
    lines: list[DistributionLine],
    all_barcodes_by_cabinet: dict[str, set[str]],
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    # Положительные распределения по конкретному складу.
    by_wh: dict[tuple[str, int], dict[str, int]] = {}
    for line in lines:
        for key, qty in line.allocations.items():
            if qty > 0:
                by_wh.setdefault(key, {})[line.barcode] = qty

    for wh in warehouses:
        allocated = by_wh.get((wh.cabinet_key, wh.warehouse_id), {})

        # ВАЖНО: WB не обнуляет позиции, отсутствующие в загружаемом файле.
        # Поэтому каждый файл склада содержит ВСЕ доступные баркоды кабинета:
        # распределённым ставим рассчитанный остаток, всем остальным — 0.
        catalog_barcodes = set(all_barcodes_by_cabinet.get(wh.cabinet_key, set()))
        all_rows = catalog_barcodes | set(allocated)

        filename = f"{safe_filename(wh.cabinet_name)} — {safe_filename(wh.name)}.xlsx"
        target = output_dir / filename
        shutil.copy2(template_path, target)
        wb = load_workbook(target)
        ws = wb.active
        ws.title = "Остатки"
        # Очищаем данные ниже заголовка, сохраняя шаблон.
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row - 1)
        for barcode in sorted(all_rows):
            ws.append([barcode, int(allocated.get(barcode, 0))])
        wb.save(target)
        files.append(target)
    return files


def build_summary(lines: list[DistributionLine], output_files: int, no_sales: list[str], not_found: list[str]) -> RunSummary:
    summary = RunSummary()
    summary.input_lines = len(lines)
    summary.input_units = sum(x.source_qty for x in lines)
    summary.allocated_units = sum(sum(x.allocations.values()) for x in lines)
    summary.reserve_units = sum(x.reserve_qty for x in lines)
    summary.output_files = output_files
    summary.no_sales_barcodes = no_sales
    summary.not_found_barcodes = not_found
    return summary
