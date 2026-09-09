\
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import xlsxwriter

from wb_client import StockRow


HEADERS = [
    "Баркод",
    "Артикул продавца",
    "Артикул WB",
    "Название",
    "Остаток WB (физический FBO)",
    "FBS (склады продавца)",
    "Реально доступно FBO",
    "Скрыто / недоступно FBO",
    "Общий физический остаток",
    "Статус",
]


def _sheet_name(name: str) -> str:
    cleaned = "".join(
        "_" if c in "[]:*?/\\" else c
        for c in name
    )
    return (cleaned or "Кабинет")[:31]


def write_sheet(
    workbook: xlsxwriter.Workbook,
    name: str,
    rows: list[StockRow],
) -> None:
    ws = workbook.add_worksheet(_sheet_name(name))

    header = workbook.add_format({
        "bold": True,
        "font_color": "white",
        "bg_color": "#1F4E78",
        "border": 1,
        "align": "center",
        "valign": "vcenter",
        "text_wrap": True,
    })

    cell = workbook.add_format({
        "border": 1,
        "valign": "top",
    })

    center = workbook.add_format({
        "border": 1,
        "align": "center",
        "valign": "top",
    })

    warn = workbook.add_format({
        "border": 1,
        "bg_color": "#FFF2CC",
        "font_color": "#9C6500",
        "align": "center",
    })

    bad = workbook.add_format({
        "border": 1,
        "bg_color": "#FCE4D6",
        "font_color": "#C00000",
        "align": "center",
    })

    good = workbook.add_format({
        "border": 1,
        "bg_color": "#E2F0D9",
        "font_color": "#375623",
        "align": "center",
    })

    title_fmt = workbook.add_format({
        "bold": True,
        "font_size": 14,
        "font_color": "#1F4E78",
    })

    ws.write(
        0,
        0,
        f"Реальные остатки Wildberries — {name}",
        title_fmt,
    )

    ws.write(
        1,
        0,
        f"Сформировано: {datetime.now():%d.%m.%Y %H:%M}",
    )

    ws.write(
        2,
        0,
        "Реальный FBO — покупательская витрина WB (dtype=4). "
        "FBS — API склада продавца.",
    )

    header_row = 4

    for col, h in enumerate(HEADERS):
        ws.write(header_row, col, h, header)

    for i, r in enumerate(
        rows,
        start=header_row + 1,
    ):
        status = (
            f"{r.hidden_fbo} шт. недоступно"
            if r.hidden_fbo > 0
            else "Без расхождения"
        )

        values = [
            r.barcodes,
            r.vendor_code,
            r.nm_id,
            r.title,
            r.physical_fbo,
            r.fbs,
            r.real_fbo,
            r.hidden_fbo,
            r.total_physical,
            status,
        ]

        for col, value in enumerate(values):
            fmt = (
                cell
                if col in (0, 1, 3)
                else center
            )

            if col == 9:
                if (
                    r.real_fbo == 0
                    and r.physical_fbo > 0
                ):
                    fmt = bad
                elif r.hidden_fbo > 0:
                    fmt = warn
                else:
                    fmt = good

            ws.write(i, col, value, fmt)

    last_row = max(
        header_row + 1,
        header_row + len(rows),
    )

    ws.autofilter(
        header_row,
        0,
        last_row,
        len(HEADERS) - 1,
    )

    ws.freeze_panes(
        header_row + 1,
        0,
    )

    ws.set_column(0, 0, 24)
    ws.set_column(1, 1, 22)
    ws.set_column(2, 2, 14)
    ws.set_column(3, 3, 42)
    ws.set_column(4, 8, 20)
    ws.set_column(9, 9, 24)
    ws.set_row(header_row, 42)


def export_workbook(
    path: Path,
    sheets: Iterable[tuple[str, list[StockRow]]],
) -> Path:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    workbook = xlsxwriter.Workbook(str(path))

    try:
        any_sheet = False

        for name, rows in sheets:
            any_sheet = True
            write_sheet(
                workbook,
                name,
                rows,
            )

        if not any_sheet:
            workbook.add_worksheet("Нет данных")

    finally:
        workbook.close()

    return path
