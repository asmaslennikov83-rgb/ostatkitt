from __future__ import annotations

import shutil
from pathlib import Path

from openpyxl import Workbook, load_workbook


def _norm(value) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def _barcode(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _filled(value) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text not in {"", "0", "0.0", "-"}


def _iter_rows(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            yield list(row)
        return
    if suffix == ".xls":
        import xlrd
        wb = xlrd.open_workbook(path)
        ws = wb.sheet_by_index(0)
        for r in range(ws.nrows):
            yield [ws.cell_value(r, c) for c in range(ws.ncols)]
        return
    raise ValueError("Шаблон исключений должен быть .xlsx или .xls")



def create_exclusions_template(
    target_path: Path,
    cabinet_ids: tuple[str | None, ...] | None = None,
) -> Path:
    """Create a fresh exclusions XLSX template at *target_path*.

    The template is generated at runtime so downloading it does not depend on
    a pre-bundled file being present in ``templates/``.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Исключения"
    ws.append(["ШК", "ID кабинета 1", "ID кабинета 2"])
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 22

    # Keep one empty editable row. Do not pre-fill seller IDs because an empty
    # cabinet cell means "allowed"; filling it would accidentally create an
    # exclusion when the user adds a barcode.
    ws.append([None, None, None])
    wb.save(target_path)
    return target_path

def read_exclusions(
    path: Path,
    cabinet_keys: tuple[str, ...],
    cabinet_ids: tuple[str | None, ...] | None = None,
) -> dict[str, set[str]]:
    """Read per-cabinet barcode exclusions.

    Column 1 is barcode. Columns 2/3 correspond to cabinet 1/2. A filled seller-ID
    cell marks the barcode as excluded in that cabinet. The value itself is kept
    user-facing/informational; the column position determines the cabinet.
    """
    result = {key: set() for key in cabinet_keys}
    cabinet_ids = cabinet_ids or tuple(None for _ in cabinet_keys)
    if not path.exists():
        return result

    rows = iter(_iter_rows(path))
    try:
        headers = next(rows)
    except StopIteration:
        return result

    normalized = [_norm(x) for x in headers]
    barcode_col = next((i for i, h in enumerate(normalized) if h in {"шк", "баркод", "штрихкод", "код"}), None)
    cab1_col = next((i for i, h in enumerate(normalized) if h == "id кабинета 1"), None)
    cab2_col = next((i for i, h in enumerate(normalized) if h == "id кабинета 2"), None)
    if barcode_col is None or cab1_col is None or cab2_col is None:
        raise ValueError("В шаблоне исключений нужны колонки: «ШК», «ID кабинета 1», «ID кабинета 2»")

    for values in rows:
        raw_barcode = values[barcode_col] if barcode_col < len(values) else None
        barcode = _barcode(raw_barcode)
        if not barcode:
            continue
        if len(cabinet_keys) >= 1:
            value = values[cab1_col] if cab1_col < len(values) else None
            if _filled(value):
                entered = _barcode(value)
                expected = cabinet_ids[0] if len(cabinet_ids) > 0 else None
                if expected and entered != str(expected):
                    raise ValueError(
                        f"ШК {barcode}: в «ID кабинета 1» указан {entered}, ожидался ID продавца {expected}"
                    )
                result[cabinet_keys[0]].add(barcode)
        if len(cabinet_keys) >= 2:
            value = values[cab2_col] if cab2_col < len(values) else None
            if _filled(value):
                entered = _barcode(value)
                expected = cabinet_ids[1] if len(cabinet_ids) > 1 else None
                if expected and entered != str(expected):
                    raise ValueError(
                        f"ШК {barcode}: в «ID кабинета 2» указан {entered}, ожидался ID продавца {expected}"
                    )
                result[cabinet_keys[1]].add(barcode)
    return result


def exclusions_count(
    path: Path, cabinet_keys: tuple[str, ...], cabinet_ids: tuple[str | None, ...] | None = None
) -> tuple[int, int]:
    data = read_exclusions(path, cabinet_keys, cabinet_ids)
    return sum(len(v) for v in data.values()), len(set().union(*data.values()) if data else set())


def save_exclusions_template(
    input_path: Path,
    target_path: Path,
    cabinet_keys: tuple[str, ...],
    cabinet_ids: tuple[str | None, ...] | None = None,
) -> tuple[int, int]:
    # Validate before replacing the persistent file.
    data = read_exclusions(input_path, cabinet_keys, cabinet_ids)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_suffix(".tmp.xlsx")
    if input_path.suffix.lower() == ".xlsx":
        shutil.copy2(input_path, tmp)
    else:
        import xlrd
        src = xlrd.open_workbook(input_path).sheet_by_index(0)
        wb = Workbook()
        ws = wb.active
        ws.title = "Исключения"
        for r in range(src.nrows):
            ws.append([src.cell_value(r, c) for c in range(src.ncols)])
        wb.save(tmp)
    tmp.replace(target_path)
    return sum(len(v) for v in data.values()), len(set().union(*data.values()) if data else set())
