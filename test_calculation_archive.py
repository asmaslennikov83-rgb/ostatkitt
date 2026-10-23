import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import app.service as service_module
from app.distributor import distribute_barcode
from app.models import ProductVariant, Warehouse
from app.service import DistributionService
from test_split_kits import FIRST_SOURCE, SECOND_SOURCE, FakeWB, process, project


def snapshot_from(result):
    with ZipFile(result['zip']) as archive:
        return json.loads(archive.read('diagnostics/calculation_snapshot.json'))


def test_archive_has_version_orders_inputs_templates_and_verified_hashes(project):
    result = process(project, [(FIRST_SOURCE, 200), (SECOND_SOURCE, 50)])
    snapshot = snapshot_from(result)
    assert result['report']['calculation']['bot_version'] == '17.7'
    assert snapshot['input_stock'][FIRST_SOURCE] == 200
    assert snapshot['calculation']['settings']['lookback_days'] == 14
    assert snapshot['template_kits'] and snapshot['effective_kits']
    assert snapshot['catalogs'] and snapshot['order_counts']
    with ZipFile(result['zip']) as archive:
        assert {'diagnostics/input.xlsx', 'diagnostics/kits_template.xlsx', 'diagnostics/stocks_template.xlsx',
                'diagnostics/calculation_snapshot.json', 'diagnostics/manifest.json'} <= set(archive.namelist())
        manifest = json.loads(archive.read('diagnostics/manifest.json'))
        assert set(manifest['files']) == set(archive.namelist()) - {'diagnostics/manifest.json'}
        for name, record in manifest['files'].items():
            content = archive.read(name)
            assert record['sha256'] == sha256(content).hexdigest()
            assert record['bytes'] == len(content)
        assert archive.testzip() is None


def test_every_distribution_can_be_repeated_using_only_snapshot(project):
    result = process(project, [(FIRST_SOURCE, 150), (SECOND_SOURCE, 40)])
    snapshot = snapshot_from(result)
    warehouses = [Warehouse(**row) for row in snapshot['warehouses']]
    variants = {key: {sku: ProductVariant(**{**row, 'skus': tuple(row['skus'])})
                      for sku, row in catalog.items()}
                for key, catalog in snapshot['effective_variants_by_barcode'].items()}
    counts = {key: {(row['chrt_id'], row['warehouse_id']): row['count'] for row in records}
              for key, records in snapshot['order_counts'].items()}
    for line in result['report']['allocations']:
        trace = line['distribution_details']
        repeated = distribute_barcode(line['barcode'], trace['quantity_to_distribute'], warehouses, variants,
                                      counts, trace['threshold'], trace['no_sales_target'],
                                      trace['safety_stock_per_warehouse'], strict_demand=trace.get('strict_demand', False),
                                      warehouse_floors={tuple((key.split(':')[0], int(key.split(':')[1]))): value
                                                        for key, value in trace['warehouse_floors'].items()}
                                      if 'warehouse_floors' in trace else None)
        assert line['allocations'] == {f'{key}:{warehouse}': amount
                                      for (key, warehouse), amount in repeated.allocations.items() if amount}
        assert line['reserve_qty'] == repeated.reserve_qty


def test_credentials_telegram_ids_and_unneeded_order_details_are_not_archived(project, monkeypatch):
    secrets = ('wb-secret-first-998877', 'wb-secret-second-112233', 'tg-secret-778899',
               'customer-private-address', 'customer-private-phone')
    project.settings = replace(project.settings, telegram_token=secrets[2],
        cabinets=tuple(replace(cabinet, token=secret) for cabinet, secret in zip(project.settings.cabinets, secrets)))
    original = FakeWB.get_orders
    async def orders_with_private_fields(client, days):
        return [{**row, 'address': secrets[3], 'phone': secrets[4], 'token': secrets[0]}
                for row in await original(client, days)]
    monkeypatch.setattr(FakeWB, 'get_orders', orders_with_private_fields)
    result = process(project, [(FIRST_SOURCE, 100)])
    with ZipFile(result['zip']) as archive:
        for name in archive.namelist():
            assert Path(name).name != '.env'
            if name.endswith('.json'):
                text = archive.read(name).decode('utf-8')
                assert all(secret not in text for secret in secrets)
                assert 'allowed_ids' not in text and 'admin_ids' not in text


def test_sku_diagnostics_show_per_cabinet_orders_and_warehouse_counts(project):
    result = process(project, [(FIRST_SOURCE, 9)])
    details = result['report']['sku_diagnostics'][FIRST_SOURCE]
    assert details['effective_group_stock'] == 9
    assert details['cabinets']['cabinet_1']['orders_total'] == 20
    assert details['cabinets']['cabinet_2']['orders_total'] == 20
    assert len(details['cabinets']['cabinet_1']['warehouses']) == 2
    recorded = details['cabinets']['cabinet_1']['allocated_total'] + details['cabinets']['cabinet_2']['allocated_total']
    assert recorded == sum(sum(line['allocations'].values()) for line in result['report']['allocations']
                           if line['barcode'] == FIRST_SOURCE)


def test_full_calculation_can_be_replayed_offline_from_one_archive(project, monkeypatch, tmp_path):
    result = process(project, [(FIRST_SOURCE, 250), (SECOND_SOURCE, 40)])
    snapshot = snapshot_from(result)
    replay_base = tmp_path / 'replay'
    with ZipFile(result['zip']) as archive:
        for name, target in [('input.xlsx', 'input.xlsx'), ('kits_template.xlsx', 'data/kits_template.xlsx'),
                             ('stocks_template.xlsx', 'templates/stocks_template.xlsx')]:
            path = replay_base / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read('diagnostics/' + name))

    class ReplayWB:
        def __init__(self, cabinet, session):
            self.key = cabinet.key

        async def get_warehouses(self):
            return [Warehouse(**row) for row in snapshot['warehouses'] if row['cabinet_key'] == self.key]

        async def get_catalog_variants(self):
            variants = [ProductVariant(**{**row, 'skus': tuple(row['skus'])})
                        for row in snapshot['catalogs'][self.key]]
            return {sku: variant for variant in variants for sku in variant.skus}, {
                variant.chrt_id: variant for variant in variants}

        async def get_orders(self, days):
            assert days == snapshot['calculation']['settings']['lookback_days']
            return snapshot['orders'][self.key]

    monkeypatch.setattr(service_module, 'WBClient', ReplayWB)
    replay = DistributionService(replace(project.settings, base_dir=replay_base))
    repeated = asyncio.run(replay.process(replay_base / 'input.xlsx', 123))
    assert repeated['report']['allocations'] == result['report']['allocations']
    assert repeated['report']['component_consumed_by_kits'] == result['report']['component_consumed_by_kits']


def test_actual_order_query_period_is_saved_without_request_headers(project, monkeypatch):
    from app.wb_api import WBClient
    from unittest.mock import AsyncMock
    client = WBClient(project.settings.cabinets[0], None)
    monkeypatch.setattr(client, '_request', AsyncMock(return_value={'orders': [], 'next': 0}))
    assert asyncio.run(client.get_orders(14)) == []
    assert client.last_orders_query['dateTo'] - client.last_orders_query['dateFrom'] == 14 * 86400
    assert client.last_orders_query['exact_api_window_available'] is True
    assert set(client.last_orders_query) == {'exact_api_window_available', 'lookback_days', 'dateFrom', 'dateTo'}
