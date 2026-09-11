from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from .models import KitDefinition


def _norm(value) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def _barcode(value) -> str:
    value = str(value or "").strip()
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    return value


def read_kits_xlsx(path: Path) -> list[KitDefinition]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    headers = {_norm(ws.cell(1, col).value): col for col in range(1, ws.max_column + 1)}
    kit_col = headers.get("баркод комплекта")
    if not kit_col:
        raise ValueError("В шаблоне комплектов нет колонки «баркод комплекта»")

    name_col = headers.get("название")
    component_cols: list[tuple[int, int]] = []
    for header, col in headers.items():
        match = re.fullmatch(r"баркод\s*(\d+)", header)
        if match:
            component_cols.append((int(match.group(1)), col))
    component_cols.sort()
    if not component_cols:
        raise ValueError("В шаблоне комплектов должны быть колонки баркод1, баркод2, ...")

    kits: list[KitDefinition] = []
    seen: dict[str, int] = {}
    for row in range(2, ws.max_row + 1):
        kit_barcode = _barcode(ws.cell(row, kit_col).value)
        components = tuple(
            bc for _idx, col in component_cols
            if (bc := _barcode(ws.cell(row, col).value))
        )
        name = str(ws.cell(row, name_col).value or "").strip() if name_col else ""

        if not kit_barcode and not components and not name:
            continue
        if not kit_barcode:
            raise ValueError(f"Строка {row}: не указан баркод комплекта")
        if not components:
            raise ValueError(f"Строка {row}: для комплекта {kit_barcode} не указаны компоненты")
        if kit_barcode in seen:
            raise ValueError(
                f"Баркод комплекта {kit_barcode} повторяется в строках {seen[kit_barcode]} и {row}"
            )
        seen[kit_barcode] = row
        kits.append(KitDefinition(name=name, barcode=kit_barcode, components=components, row_number=row))

    if not kits:
        raise ValueError("В шаблоне комплектов нет комплектов")
    return kits
