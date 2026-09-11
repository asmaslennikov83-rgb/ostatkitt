from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path

import aiohttp

from .config import Settings
from .distributor import distribute_barcode
from .excel_io import build_summary, read_input_xlsx, write_warehouse_files
from .history import cleanup_history, make_run_dir, write_json
from .wb_api import WBClient, count_orders_by_variant_and_warehouse


class DistributionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.template_path = settings.base_dir / "templates" / "stocks_template.xlsx"
        self.history_root = settings.base_dir / "data" / "history"

    async def process(self, input_path: Path, user_id: int) -> dict:
        cleanup_history(self.history_root, self.settings.retention_days)
        run_dir = make_run_dir(self.history_root, user_id)
        input_copy = run_dir / "input.xlsx"
        shutil.copy2(input_path, input_copy)
        output_dir = run_dir / "output"

        stock = read_input_xlsx(input_copy)

        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            clients = [WBClient(c, session) for c in self.settings.cabinets]
            # Для каждого кабинета параллельно получаем склады, заказы и каталог.
            results = await asyncio.gather(*[
                asyncio.gather(
                    client.get_warehouses(),
                    client.get_orders(self.settings.lookback_days),
                    client.get_catalog_variants(),
                )
                for client in clients
            ])

        warehouses = []
        variants_by_cabinet = {}
        counts_by_cabinet = {}
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

        lines = []
        not_found: list[str] = []
        no_sales: list[str] = []

        for barcode, qty in stock.items():
            line = distribute_barcode(
                barcode=barcode,
                quantity=qty,
                warehouses=warehouses,
                barcode_variants_by_cabinet=variants_by_cabinet,
                order_counts_by_cabinet=counts_by_cabinet,
                threshold=self.settings.distribute_all_threshold,
                no_sales_target=self.settings.no_sales_target,
            )
            lines.append(line)
            if not line.found_in_cabinets:
                not_found.append(barcode)
                continue
            total_sales = 0
            for wh in warehouses:
                variant = variants_by_cabinet.get(wh.cabinet_key, {}).get(barcode)
                if variant:
                    total_sales += counts_by_cabinet.get(wh.cabinet_key, {}).get((variant.chrt_id, wh.warehouse_id), 0)
            if total_sales == 0:
                no_sales.append(barcode)

        files = write_warehouse_files(self.template_path, output_dir, warehouses, lines)
        summary = build_summary(lines, len(files), no_sales, not_found)

        report = {
            "input_lines": summary.input_lines,
            "input_units": summary.input_units,
            "allocated_units": summary.allocated_units,
            "reserve_units": summary.reserve_units,
            "output_files": summary.output_files,
            "no_sales_barcodes": no_sales,
            "not_found_barcodes": not_found,
            "allocations": [
                {
                    "barcode": line.barcode,
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
