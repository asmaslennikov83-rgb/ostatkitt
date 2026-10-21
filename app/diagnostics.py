from __future__ import annotations

from dataclasses import asdict
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import platform
import shutil

from .history import write_json
from .inventory_priority import priority_rules
from .version import APP_VERSION, REPORT_SCHEMA_VERSION


def freeze_file(source: Path, directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if source.is_file():
        shutil.copy2(source, target)
    return target


def calculation_metadata(settings, started_at: str) -> dict:
    source_dir = Path(__file__).resolve().parent
    modules = ('version.py', 'distributor.py', 'service.py', 'models.py', 'kits.py',
               'split_kits.py', 'excel_io.py', 'exclusions.py', 'wb_api.py', 'diagnostics.py', 'inventory_priority.py')
    return {
        'bot_version': APP_VERSION, 'report_schema_version': REPORT_SCHEMA_VERSION,
        'started_at_utc': started_at, 'finished_at_utc': datetime.now(timezone.utc).isoformat(),
        'python_version': platform.python_version(),
        'settings': {key: getattr(settings, key) for key in (
            'lookback_days', 'no_sales_target', 'distribute_all_threshold', 'safety_stock_per_warehouse')},
        'order_count_definition': 'Количество FBS-сборочных заданий за период по chrtID и доступному складу; не выкупы.',
        'deficit_policy': 'cabinet_demand_then_warehouse_v1',
        'inventory_priority': priority_rules(),
        'module_sha256': {name: sha256((source_dir / name).read_bytes()).hexdigest() for name in modules},
    }


def serialize_catalog(catalogs: dict) -> dict:
    return {key: [asdict(variant) for variant in {variant.chrt_id: variant for variant in mapping.values()}.values()]
            for key, mapping in catalogs.items()}


def sku_diagnostics(barcodes, stock, alias_root, preferred, catalogs, counts, warehouses, output_maps,
                    lines, excluded_roots, zero_roots, ready_roots, raw_catalogs) -> dict:
    result = {}
    allocated = defaultdict(Counter)
    for line in lines:
        for (cabinet_key, warehouse_id), amount in line.allocations.items():
            output_sku = output_maps.get(cabinet_key, {}).get(line.barcode, line.barcode)
            allocated[cabinet_key][output_sku] += amount
    for barcode in sorted(barcodes):
        root = alias_root.get(barcode, barcode)
        preferred_sku = preferred.get(root, barcode)
        cabinets = {}
        for cabinet_key, catalog in catalogs.items():
            variant = catalog.get(barcode)
            counted_variant = variant or raw_catalogs.get(cabinet_key, {}).get(barcode)
            candidates = [warehouse for warehouse in warehouses if warehouse.cabinet_key == cabinet_key]
            by_warehouse = {str(warehouse.warehouse_id): {
                'name': warehouse.name,
                'orders': counts.get(cabinet_key, {}).get((counted_variant.chrt_id, warehouse.warehouse_id), 0)
                          if counted_variant else 0,
            } for warehouse in candidates}
            cabinets[cabinet_key] = {
                'available_for_calculation': variant is not None,
                'chrt_id': counted_variant.chrt_id if counted_variant else None,
                'output_barcode': output_maps.get(cabinet_key, {}).get(barcode),
                'allocated_total': allocated[cabinet_key].get(output_maps.get(cabinet_key, {}).get(barcode), 0),
                'orders_total': sum(row['orders'] for row in by_warehouse.values()),
                'warehouses': by_warehouse,
            }
        result[barcode] = {
            'alias_root': root, 'stock_barcode': preferred_sku,
            'effective_group_stock': stock.get(preferred_sku, 0),
            'manual_excluded': root in excluded_roots, 'zero_kit': root in zero_roots,
            'ready_kit': root in ready_roots,
            'allocation_barcodes': [line.barcode for line in lines if alias_root.get(line.barcode, line.barcode) == root],
            'cabinets': cabinets,
        }
    return result


def write_archive_manifest(directory: Path, warehouse_files: list[Path], report_path: Path) -> None:
    files = {path.name: path for path in warehouse_files}
    files['report.json'] = report_path
    files.update({f'diagnostics/{path.name}': path for path in directory.iterdir() if path.is_file()})
    entries = {name: {'bytes': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()}
               for name, path in files.items() if name != 'diagnostics/manifest.json'}
    write_json(directory / 'manifest.json', {
        'bot_version': APP_VERSION, 'report_schema_version': REPORT_SCHEMA_VERSION, 'files': entries,
    })
