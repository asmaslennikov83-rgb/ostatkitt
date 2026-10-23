import asyncio
import json
from zipfile import ZipFile

import pytest

import app.service as service_module
import app.stock_ops as stock_module
from app.config import CabinetConfig, Settings
from app.distributor import distribute_barcode
from app.inventory_priority import policy_roots, selected_floors
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


def test_reported_216_source_stock_protects_selected_warehouses_and_meets_demand(project):
    result = process(project, [(SOURCE, 216)])
    protected = result['report']['assembly_policy']['protected_before_other_kits']
    assert protected == {SOURCE: 6, HIGH_200: 6, HIGH_50: 6, LOW_300: 2, LOW_600: 1}
    assert allocation(result, SOURCE) >= 40
    assert reserve(result, SOURCE) == 0
    assert allocation(result, HIGH_200) == result['report']['assembly_policy']['kit_requested_targets'][HIGH_200]
    assert allocation(result, HIGH_50) >= 18
    assert allocation(result, LOW_300) == 8
    assert allocation(result, LOW_600) == 2
    assert_balanced(result, 216)


@pytest.mark.parametrize('quantity', [0, 1, 2, 9, 24, 48, 100, 167, 168])
def test_shortage_preserves_physical_stock_and_reports_missing_floors(project, quantity):
    result = process(project, [(SOURCE, quantity)])
    shortfall = result['report']['assembly_policy']['priority_shortfall']
    assert all(value >= 0 for value in shortfall.values())
    assert (sum(shortfall.values()) == 0) == (quantity >= 33)
    assert_balanced(result, quantity)
    assert all(isinstance(value, int) and value >= 0 for rows in warehouse_rows(result) for value in rows.values())


def test_huge_secondary_demand_cannot_reduce_protected_floor(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_300] = (300, 0, 0, 0, 0, 0)
        cabinet[LOW_600] = (300, 0, 0, 0, 0, 0)
    result = process(project, [(SOURCE, 216)])
    for barcode in (SOURCE, HIGH_200, HIGH_50):
        assert allocation(result, barcode) >= 6
    low_cost = allocation(result, LOW_300) * 3 + allocation(result, LOW_600) * 6
    assert low_cost <= 195
    assert_balanced(result, 216)


def test_priority_demand_competes_after_selected_floors(project):
    FakeWB.demand['cabinet_1'][SOURCE] = (100, 0, 0, 0, 0, 0)
    FakeWB.demand['cabinet_2'][SOURCE] = (0, 0, 0, 0, 0, 0)
    result = process(project, [(SOURCE, 250)])
    assert result['report']['assembly_policy']['protected_before_other_kits'][SOURCE] == 6
    assert allocation(result, HIGH_200) == result['report']['assembly_policy']['kit_requested_targets'][HIGH_200]
    assert allocation(result, HIGH_50) >= 18
    assert allocation(result, SOURCE) >= 100
    assert_balanced(result, 250)


def test_priority_target_is_not_lost_to_rounding_across_many_warehouses(project):
    FakeWB.demand['cabinet_1'][SOURCE] = (9, 8, 9, 8, 9, 7)
    FakeWB.demand['cabinet_2'][SOURCE] = (9, 8, 9, 8, 9, 7)
    result = process(project, [(SOURCE, 220)])
    assert allocation(result, SOURCE) >= 100
    assert reserve(result, SOURCE) == 0
    assert allocation(result, LOW_300, LOW_600) == 10
    assert_balanced(result, 220)


def test_no_orders_secondary_kits_have_one_fallback_in_one_cabinet(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_300] = (0,) * 6
        cabinet[LOW_600] = (0,) * 6
    result = process(project, [(SOURCE, 500)])
    assert allocation(result, LOW_300) == allocation(result, LOW_600) == 1
    for barcode in (LOW_300, LOW_600):
        line = next(line for line in result['report']['allocations'] if line['barcode'] == barcode)
        assert sum(line['allocations'].values()) == 1
        assert all(key.startswith('cabinet_1:') for key in line['allocations'])
    assert_balanced(result, 500)


def test_one_order_secondary_kit_is_only_on_the_ordered_warehouse(project):
    result = process(project, [(SOURCE, 216)])
    line = next(line for line in result['report']['allocations'] if line['barcode'] == LOW_600)
    assert line['allocations']['cabinet_1:2'] == 1
    assert sum(line['allocations'].values()) == 2
    assert line['distribution_details']['rule'] == 'selected_safety_then_demand'
    assert line['distribution_details']['required_for_full_safety'] == 1


def test_ready_secondary_stock_is_capped_and_extra_stays_in_reserve(project):
    for cabinet in FakeWB.demand.values():
        cabinet[LOW_600] = (0,) * 6
    result = process(project, [(SOURCE, 216), (LOW_300, 40), (LOW_600, 5)])
    assert allocation(result, LOW_300) == 8
    assert reserve(result, LOW_300) == 32
    assert allocation(result, LOW_600) == 1
    assert reserve(result, LOW_600) == 4
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
    assert result['report']['assembly_policy']['priority_targets'][HIGH_200] == 6
    assert result['report']['assembly_policy']['protected_before_other_kits'][HIGH_200] == 6
    assert_balanced(result, 216)


def test_priority_and_strict_policy_follow_aliases(project):
    workbook(project.kits_path, ['Название', 'баркод комплекта'] + [f'баркод{number}' for number in range(1, 7)],
             [('200', 'high200_alias') + (SOURCE,) * 2,
              ('300', 'low300_alias') + (SOURCE,) * 3,
              ('600', 'low600_alias') + (SOURCE,) * 6])
    result = process(project, [('source_alias', 216)])
    assert allocation(result, 'high200_alias') == result['report']['assembly_policy']['kit_requested_targets']['high200_alias']
    assert allocation(result, 'low300_alias') == 8
    assert allocation(result, 'low600_alias') >= 1
    assert_balanced(result, 216, ('source_alias',))


def test_cabinet_exclusion_remains_in_force_for_priority_sku(project):
    workbook(project.exclusions_path, ['ШК', 'ID кабинета 1', 'ID кабинета 2'],
             [(HIGH_200, None, 'seller2'), ('high200_alias', None, 'seller2')])
    result = process(project, [(SOURCE, 216)])
    assert result['report']['assembly_policy']['priority_targets'][HIGH_200] == 3
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
        assert sum(amount for (cabinet, warehouse, ident), amount in FakeWB.stock.items() if ident == 4) == allocation(result, LOW_600)
        assert sum(amount for (cabinet, warehouse, ident), amount in FakeWB.stock.items() if ident == 2) == allocation(result, HIGH_200)
        rollback = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert (await manager.execute(rollback['id'], 123))['status'] == 'verified'
        assert not any(FakeWB.stock.values())
    asyncio.run(scenario())


def test_archive_contains_priority_targets_and_replayable_strict_distributions(project):
    result = process(project, [(SOURCE, 216)])
    with ZipFile(result['zip']) as archive:
        snapshot = json.loads(archive.read('diagnostics/calculation_snapshot.json'))
    assert snapshot['assembly_policy'] == result['report']['assembly_policy']
    assert snapshot['assembly_policy']['kit_requested_targets'][LOW_600] == 2
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
                                    trace['safety_stock_per_warehouse'], strict_demand=trace['strict_demand'],
                                    warehouse_floors={tuple((key.split(':')[0], int(key.split(':')[1]))): value
                                                      for key, value in trace['warehouse_floors'].items()})
        assert line['allocations'] == {f'{cabinet}:{warehouse}': amount
                                      for (cabinet, warehouse), amount in replay.allocations.items() if amount}
        assert line['reserve_qty'] == replay.reserve_qty


@pytest.mark.parametrize('limit,selling_only', [(1, True), (3, False)])
def test_top_warehouses_ranked_independently_per_cabinet(limit, selling_only):
    candidates = [(Warehouse(cabinet, cabinet, warehouse, str(warehouse)), orders)
                  for cabinet in ('a', 'b')
                  for warehouse, orders in ((1, 0), (2, 3), (3, 10), (4, 3), (5, 1))]
    floors = selected_floors(candidates, limit, selling_only)
    expected_ids = (3,) if limit == 1 else (2, 3, 4)
    assert floors == {(cabinet, warehouse): 1 for cabinet in ('a', 'b') for warehouse in expected_ids}
    assert selected_floors(list(reversed(candidates)), limit, selling_only) == floors


@pytest.mark.parametrize('limit,selling_only', [(1, True), (3, False)])
def test_no_orders_fallback_uses_one_available_cabinet(limit, selling_only):
    candidates = [(Warehouse(cabinet, cabinet, warehouse, str(warehouse)), 0)
                  for cabinet in ('b', 'a') for warehouse in (5, 2, 1, 3)]
    assert selected_floors(candidates, limit, selling_only) == {('a', warehouse): 1 for warehouse in (1, 2, 3)[:limit]}
    assert selected_floors([], limit, selling_only) == {}


def test_selling_only_does_not_seed_a_cabinet_without_orders():
    candidates = [(Warehouse('a', 'a', 1, '1'), 0), (Warehouse('b', 'b', 2, '2'), 3)]
    assert selected_floors(candidates, 1, True) == {('b', 2): 1}


def test_selected_floor_is_included_in_demand_not_added():
    warehouses = [Warehouse('a', 'a', warehouse, str(warehouse)) for warehouse in (1, 2, 3)]
    variants = {'a': {'sku': ProductVariant('a', 1, 1, ('sku',))}}
    counts = {'a': {(1, 1): 5, (1, 2): 2}}
    for quantity in range(15):
        line = distribute_barcode('sku', quantity, warehouses, variants, counts, 20, 2,
                                  strict_demand=True, warehouse_floors={('a', 1): 1})
        assert sum(line.allocations.values()) == min(quantity, 7)
        assert line.allocations.get(('a', 1), 0) <= 5
        assert line.allocations.get(('a', 2), 0) <= 2
        assert line.allocations.get(('a', 3), 0) == 0
        assert line.reserve_qty == max(0, quantity - 7)


def test_all_no_orders_have_only_selected_floors_and_preserve_split_remainder(project):
    for cabinet in FakeWB.demand.values():
        for barcode in cabinet:
            cabinet[barcode] = (0,) * 6
    result = process(project, [(SOURCE, 100)])
    for barcode in (SOURCE, HIGH_200, HIGH_50):
        assert allocation(result, barcode) == 3
        line = next(line for line in result['report']['allocations'] if line['barcode'] == barcode)
        assert len(line['allocations']) == 3
        assert all(key.startswith('cabinet_1:') and value == 1 for key, value in line['allocations'].items())
    assert allocation(result, LOW_300) == allocation(result, LOW_600) == 1
    half = next(kit for kit in result['report']['kits'] if kit['barcode'] == HIGH_50)
    assert half['built'] == 4
    assert half['batches'] == 2
    assert reserve(result, HIGH_50) == 1
    assert_balanced(result, 100)


def test_priority_floors_do_not_seed_all_twelve_warehouses(project):
    result = process(project, [(SOURCE, 167)])
    floors = result['report']['assembly_policy']['selected_warehouse_floors']
    for barcode in (SOURCE, HIGH_200, HIGH_50):
        assert len(floors[barcode]) == 6
        assert all(sum(key.startswith(cabinet + ':') for key in floors[barcode]) == 3
                   for cabinet in ('cabinet_1', 'cabinet_2'))
    assert allocation(result, LOW_300) == 8
    assert allocation(result, LOW_600) == 2
    assert_balanced(result, 167)


def test_october_22_orders_with_fourteen_warehouses_build_300_and_600(project, monkeypatch):
    async def warehouses(client):
        count = 8 if client.cabinet.key == 'cabinet_1' else 6
        return [Warehouse(client.cabinet.key, client.cabinet.name, ident, str(ident))
                for ident in range(1, count + 1)]
    monkeypatch.setattr(FakeWB, 'get_warehouses', warehouses)
    FakeWB.demand = {
        'cabinet_1': {SOURCE: (0, 4, 6, 3, 5, 0, 0, 4), HIGH_200: (0, 0, 1, 0, 0, 0, 0, 0),
                      HIGH_50: (1, 7, 2, 2, 4, 0, 0, 3), LOW_300: (1, 2, 2, 1, 1, 0, 0, 0),
                      LOW_600: (0, 1, 2, 0, 0, 0, 0, 0)},
        'cabinet_2': {SOURCE: (3, 0, 6, 9, 8, 8), HIGH_200: (0, 0, 0, 1, 2, 1),
                      HIGH_50: (7, 0, 0, 0, 2, 3), LOW_300: (0, 0, 0, 0, 3, 3),
                      LOW_600: (0,) * 6},
    }
    result = process(project, [(SOURCE, 167)])
    assert {barcode: allocation(result, barcode) for barcode in (SOURCE, HIGH_200, HIGH_50, LOW_300, LOW_600)} == {
        SOURCE: 78, HIGH_200: 8, HIGH_50: 32, LOW_300: 13, LOW_600: 3}
    assert_balanced(result, 167)
    for barcode in (LOW_300, LOW_600):
        line = next(line for line in result['report']['allocations'] if line['barcode'] == barcode)
        for key, quantity in line['allocations'].items():
            cabinet, warehouse = key.split(':')
            assert quantity == FakeWB.demand[cabinet][barcode][int(warehouse) - 1]
