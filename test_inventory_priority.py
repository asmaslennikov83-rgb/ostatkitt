import asyncio
import json
from zipfile import ZipFile

import pytest

import app.service as service_module
import app.stock_ops as stock_module
from app.config import CabinetConfig, Settings
from app.distributor import distribute_barcode
from app.inventory_priority import policy_roots
from app.models import ProductVariant, Warehouse
from app.service import DistributionService
from app.stock_ops import StockManager
from test_kit_stock_controls import Session, allocation, process, warehouse_rows, workbook


SOURCE = '4610231972241'
HIGH_200 = '2047555651545'
HIGH_50 = '2046819266549'
LOW_600 = '2055014478763'
LOW_300 = '2047922424581'


class FakeWB:
    def __init__(self, cabinet, session):
        self.cabinet = cabinet

    async def get_warehouses(self):
        return [Warehouse(self.cabinet.key, self.cabinet.name, warehouse, str(warehouse))
                for warehouse in range(1, 7)]

    async def get_catalog_variants(self):
        variants = {index: ProductVariant(self.cabinet.key, index, index, skus)
                    for index, skus in enumerate(((SOURCE, 'source_alias'), (HIGH_200, 'high200_alias'),
                                                  (HIGH_50, 'half_alias'), (LOW_600, 'low600_alias'),
                                                  (LOW_300, 'low300_alias')), start=1)}
        return {sku: variant for variant in variants.values() for sku in variant.skus}, variants

    async def get_orders(self, days):
        catalog, _variants = await self.get_catalog_variants()
        return [{'chrtId': catalog[sku].chrt_id, 'warehouseId': warehouse}
                for sku, distribution in self.demand[self.cabinet.key].items()
                for warehouse, count in enumerate(distribution, start=1) for order in range(count)]

    async def get_fbs_stocks(self, warehouse, ids):
        return {int(ident): self.stock.get((self.cabinet.key, warehouse, int(ident)), 0) for ident in ids}

    async def put_fbs_stocks(self, warehouse, amounts):
        self.puts.append((self.cabinet.key, warehouse, dict(amounts)))
        for ident, amount in amounts.items():
            self.stock[self.cabinet.key, warehouse, ident] = amount


@pytest.fixture
def project(tmp_path, monkeypatch):
    settings = Settings('fake', {123}, (
        CabinetConfig('cabinet_1', 'TT', 'fake1'), CabinetConfig('cabinet_2', 'Group', 'fake2'),
    ), 14, 4, 2, 20, 4, base_dir=tmp_path, admin_ids={123})
    workbook(tmp_path / 'templates/stocks_template.xlsx', ['Баркод', 'Количество'], [])
    workbook(tmp_path / 'data/kits_template.xlsx',
             ['Название', 'баркод комплекта'] + [f'баркод{index}' for index in range(1, 7)],
             [('200', HIGH_200) + (SOURCE,) * 2, ('300', LOW_300) + (SOURCE,) * 3,
              ('600', LOW_600) + (SOURCE,) * 6])
    monkeypatch.setattr(service_module.aiohttp, 'TCPConnector', lambda **kwargs: None)
    monkeypatch.setattr(service_module.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(service_module, 'WBClient', FakeWB)
    monkeypatch.setattr(stock_module, 'WBClient', FakeWB)
    FakeWB.stock = {}
    FakeWB.puts = []
    FakeWB.demand = {
        'cabinet_1': {SOURCE: (0, 4, 6, 1, 5, 3), HIGH_200: (0, 0, 0, 0, 0, 0),
                      HIGH_50: (0, 4, 3, 3, 2, 2), LOW_300: (0, 1, 0, 1, 0, 0),
                      LOW_600: (0, 1, 0, 0, 0, 0)},
        'cabinet_2': {SOURCE: (3, 0, 2, 4, 8, 4), HIGH_200: (0, 0, 0, 0, 4, 0),
                      HIGH_50: (2, 0, 0, 0, 1, 1), LOW_300: (1, 0, 1, 1, 2, 1),
                      LOW_600: (0, 0, 0, 0, 0, 0)},
    }
    return DistributionService(settings)


def reserve(result, barcode):
    return sum(line['reserve_qty'] for line in result['report']['allocations'] if line['barcode'] == barcode)


def assert_balanced(result, quantity, source_barcodes=(SOURCE,)):
    consumed = sum(result['report']['component_consumed_by_kits'].get(sku, 0) for sku in source_barcodes)
    assert sum(allocation(result, sku) + reserve(result, sku) for sku in source_barcodes) + consumed == quantity
    for kit in result['report']['kits']:
        assert kit['built'] == kit['batches'] * kit['output_per_batch']


def test_reported_216_source_stock_protects_three_priority_skus(project):
    result = process(project, [(SOURCE, 216)])
    protected = result['report']['assembly_policy']['protected_before_other_kits']
    assert protected == {SOURCE: 48, HIGH_200: 48, HIGH_50: 48}
    assert allocation(result, SOURCE) == 66
    assert reserve(result, SOURCE) == 0
    assert allocation(result, HIGH_200) == 48
    assert allocation(result, HIGH_50) == 48
    assert allocation(result, LOW_300) == 8
    assert allocation(result, LOW_600) == 1
    assert_balanced(result, 216)


@pytest.mark.parametrize('quantity', [0, 1, 2, 9, 24, 48, 100, 167, 168])
def test_secondary_assembly_does_not_take_stock_needed_by_priority_skus(project, quantity):
    result = process(project, [(SOURCE, quantity)])
    assert allocation(result, LOW_300, LOW_600) == 0
    shortfall = result['report']['assembly_policy']['priority_shortfall']
    assert all(value >= 0 for value in shortfall.values())
    assert (sum(shortfall.values()) == 0) == (quantity == 168)
    assert_balanced(result, quantity)
    assert all(isinstance(value, int) and value >= 0 for rows in warehouse_rows(result) for value in rows.values())


def test_huge_secondary_demand_cannot_reduce_protected_floor(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_300] = (300, 0, 0, 0, 0, 0)
        cabinet[LOW_600] = (300, 0, 0, 0, 0, 0)
    result = process(project, [(SOURCE, 216)])
    for barcode in (SOURCE, HIGH_200, HIGH_50):
        assert allocation(result, barcode) >= 48
    low_cost = allocation(result, LOW_300) * 3 + allocation(result, LOW_600) * 6
    assert low_cost <= 48
    assert_balanced(result, 216)


def test_priority_demand_above_floor_is_protected_before_secondary(project):
    FakeWB.demand['cabinet_1'][SOURCE] = (100, 0, 0, 0, 0, 0)
    FakeWB.demand['cabinet_2'][SOURCE] = (0, 0, 0, 0, 0, 0)
    result = process(project, [(SOURCE, 250)])
    assert result['report']['assembly_policy']['protected_before_other_kits'][SOURCE] == 100
    assert allocation(result, HIGH_200) == 48
    assert allocation(result, HIGH_50) == 48
    assert allocation(result, SOURCE) >= 100
    assert_balanced(result, 250)


def test_priority_target_is_not_lost_to_rounding_across_many_warehouses(project):
    FakeWB.demand['cabinet_1'][SOURCE] = (9, 8, 9, 8, 9, 7)
    FakeWB.demand['cabinet_2'][SOURCE] = (9, 8, 9, 8, 9, 7)
    result = process(project, [(SOURCE, 220)])
    assert allocation(result, SOURCE) == 100
    assert reserve(result, SOURCE) == 0
    assert allocation(result, LOW_300, LOW_600) == 0
    assert_balanced(result, 220)


def test_no_orders_secondary_kits_are_not_built_even_with_large_stock(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_300] = (0,) * 6
        cabinet[LOW_600] = (0,) * 6
    result = process(project, [(SOURCE, 500)])
    assert allocation(result, LOW_300, LOW_600) == 0
    for rows in warehouse_rows(result):
        assert rows[LOW_300] == rows[LOW_600] == 0
    assert_balanced(result, 500)


def test_one_order_secondary_kit_is_only_on_the_ordered_warehouse(project):
    result = process(project, [(SOURCE, 216)])
    line = next(line for line in result['report']['allocations'] if line['barcode'] == LOW_600)
    assert line['allocations'] == {'cabinet_1:2': 1}
    assert line['distribution_details']['rule'] == 'strict_demand_only'
    assert line['distribution_details']['required_for_full_safety'] == 0


def test_ready_secondary_stock_is_capped_and_extra_stays_in_reserve(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_600] = (0,) * 6
    result = process(project, [(SOURCE, 216), (LOW_300, 40), (LOW_600, 5)])
    assert allocation(result, LOW_300) == 8
    assert reserve(result, LOW_300) == 32
    assert allocation(result, LOW_600) == 0
    assert reserve(result, LOW_600) == 5
    assert not any(kit['barcode'] in (LOW_300, LOW_600) for kit in result['report']['kits'])
    assert_balanced(result, 216)


def test_manual_and_percent_rules_override_priority(project):
    result = process(project, [(SOURCE, 216), (HIGH_200, '%'), (HIGH_50, '!')])
    assert allocation(result, HIGH_200, HIGH_50) == 0
    for rows in warehouse_rows(result):
        assert rows[HIGH_200] == 0
        assert HIGH_50 not in rows and 'half_alias' not in rows
    assert_balanced(result, 216)


def test_source_manual_exclusion_stops_all_assembly(project):
    result = process(project, [(SOURCE, '!')])
    assert allocation(result, SOURCE, HIGH_200, HIGH_50, LOW_300, LOW_600) == 0
    assert not result['report']['component_consumed_by_kits']


def test_ready_priority_kit_retains_existing_no_extra_assembly_rule(project):
    result = process(project, [(SOURCE, 216), (HIGH_200, 10)])
    assert allocation(result, HIGH_200) == 10
    assert not any(kit['barcode'] == HIGH_200 for kit in result['report']['kits'])
    assert result['report']['assembly_policy']['priority_targets'][HIGH_200] == 48
    assert result['report']['assembly_policy']['protected_before_other_kits'][HIGH_200] == 10
    assert_balanced(result, 216)


def test_priority_and_strict_policy_follow_aliases(project):
    workbook(project.kits_path, ['Название', 'баркод комплекта'] + [f'баркод{number}' for number in range(1, 7)],
             [('200', 'high200_alias') + (SOURCE,) * 2,
              ('300', 'low300_alias') + (SOURCE,) * 3,
              ('600', 'low600_alias') + (SOURCE,) * 6])
    result = process(project, [('source_alias', 216)])
    assert allocation(result, 'high200_alias') == 48
    assert allocation(result, 'low300_alias') == 8
    assert allocation(result, 'low600_alias') == 1
    assert_balanced(result, 216, ('source_alias',))


def test_cabinet_exclusion_remains_in_force_for_priority_sku(project):
    workbook(project.exclusions_path, ['ШК', 'ID кабинета 1', 'ID кабинета 2'],
             [(HIGH_200, None, 'seller2'), ('high200_alias', None, 'seller2')])
    result = process(project, [(SOURCE, 216)])
    assert result['report']['assembly_policy']['priority_targets'][HIGH_200] == 24
    for path, rows in zip(result['files'], warehouse_rows(result)):
        if path.name.startswith('Group'):
            assert HIGH_200 not in rows and 'high200_alias' not in rows
    assert_balanced(result, 216)


def test_policy_conflicting_chrtid_is_rejected():
    with pytest.raises(ValueError, match='одну вариацию'):
        policy_roots({LOW_600: SOURCE}, {})
    variant = ProductVariant('cabinet_1', 1, 1, (SOURCE, LOW_600))
    with pytest.raises(ValueError, match='общий chrtID'):
        policy_roots({}, {'cabinet_1': {SOURCE: variant, LOW_600: variant}})


def test_strict_demand_integer_distribution_never_exceeds_per_warehouse_orders():
    warehouses = [Warehouse(key, key, warehouse, str(warehouse))
                  for key in ('a', 'b') for warehouse in (1, 2, 3)]
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b')}
    counts = {'a': {(1, 1): 2, (1, 2): 1}, 'b': {(1, 1): 5, (1, 3): 1}}
    for quantity in range(20):
        line = distribute_barcode('sku', quantity, warehouses, variants, counts, 20, 2, 4, strict_demand=True)
        assert sum(line.allocations.values()) == min(quantity, 9)
        assert line.reserve_qty == max(0, quantity - 9)
        for (cabinet, warehouse), amount in line.allocations.items():
            assert amount <= counts[cabinet].get((1, warehouse), 0)


def test_upload_and_rollback_preserve_strict_targets_and_priority_outputs(project):
    result = process(project, [(SOURCE, 216)])
    manager = StockManager(project.settings)
    async def scenario():
        preview = await manager.preview('distribution', 123, {'cabinet_1', 'cabinet_2'}, files=result['files'])
        assert not FakeWB.puts
        assert (await manager.execute(preview['id'], 123))['status'] == 'verified'
        assert sum(amount for (cabinet, warehouse, ident), amount in FakeWB.stock.items() if ident == 4) == 1
        assert sum(amount for (cabinet, warehouse, ident), amount in FakeWB.stock.items() if ident == 2) == 48
        rollback = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert (await manager.execute(rollback['id'], 123))['status'] == 'verified'
        assert not any(FakeWB.stock.values())
    asyncio.run(scenario())


def test_archive_contains_priority_targets_and_replayable_strict_distributions(project):
    result = process(project, [(SOURCE, 216)])
    with ZipFile(result['zip']) as archive:
        snapshot = json.loads(archive.read('diagnostics/calculation_snapshot.json'))
    assert snapshot['assembly_policy'] == result['report']['assembly_policy']
    assert snapshot['assembly_policy']['kit_requested_targets'][LOW_600] == 1
    warehouses = [Warehouse(**row) for row in snapshot['warehouses']]
    variants = {key: {sku: ProductVariant(**{**row, 'skus': tuple(row['skus'])})
                      for sku, row in catalog.items()}
                for key, catalog in snapshot['effective_variants_by_barcode'].items()}
    counts = {key: {(row['chrt_id'], row['warehouse_id']): row['count'] for row in rows}
              for key, rows in snapshot['order_counts'].items()}
    for line in result['report']['allocations']:
        trace = line['distribution_details']
        replay = distribute_barcode(line['barcode'], trace['quantity_to_distribute'], warehouses,
                                    variants, counts, trace['threshold'], trace['no_sales_target'],
                                    trace['safety_stock_per_warehouse'], strict_demand=trace['strict_demand'])
        assert line['allocations'] == {f'{cabinet}:{warehouse}': amount
                                      for (cabinet, warehouse), amount in replay.allocations.items() if amount}
        assert line['reserve_qty'] == replay.reserve_qty
