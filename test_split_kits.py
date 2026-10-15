import asyncio

import pytest

import app.service as service_module
import app.stock_ops as stock_module
from app.config import CabinetConfig, Settings
from app.models import KitDefinition, ProductVariant, Warehouse
from app.service import DistributionService
from app.split_kits import SPLIT_KIT_SOURCES, apply_split_rules
from app.stock_ops import StockManager
from test_kit_stock_controls import Session, allocation, process, warehouse_rows, workbook


FIRST_SOURCE = '4610231972418'
FIRST_HALF = '2046819245780'
SECOND_SOURCE = '4610231972241'
SECOND_HALF = '2046819266549'


class FakeWB:
    def __init__(self, cabinet, session):
        self.cabinet = cabinet

    async def get_warehouses(self):
        return [Warehouse(self.cabinet.key, self.cabinet.name, ident, f'Warehouse {ident}')
                for ident in self.warehouse_ids]

    async def get_catalog_variants(self):
        offset = 0 if self.cabinet.key == 'cabinet_1' else 100
        aliases = [(FIRST_SOURCE, 'source_alias'), (FIRST_HALF, 'half_alias'),
                   ('pack200',), ('pack300',), ('pack600',), (SECOND_SOURCE,), (SECOND_HALF,),
                   ('mixed_kit',), ('other_component',)]
        variants = {offset + index: ProductVariant(self.cabinet.key, offset + index, index, skus)
                    for index, skus in enumerate(aliases, start=1)}
        if self.cabinet.key == 'cabinet_2':
            variants[offset + 2] = ProductVariant(self.cabinet.key, offset + 2, 2, ('half_alias',))
        return {sku: variant for variant in variants.values() for sku in variant.skus}, variants

    async def get_orders(self, days):
        assert days == 14
        catalog, _variants = await self.get_catalog_variants()
        return [{'chrtId': catalog['half_alias' if sku == FIRST_HALF and sku not in catalog else sku].chrt_id,
                 'warehouseId': warehouse}
                for sku, amount in self.demand.items() for warehouse in self.warehouse_ids
                for order in range(amount)]

    async def get_fbs_stocks(self, warehouse, ids):
        return {int(ident): self.stock.get((self.cabinet.key, warehouse, int(ident)), 0) for ident in ids}

    async def put_fbs_stocks(self, warehouse, amounts):
        self.puts.append((self.cabinet.key, warehouse, dict(amounts)))
        for ident, amount in amounts.items():
            self.stock[self.cabinet.key, warehouse, ident] = amount


@pytest.fixture
def project(tmp_path, monkeypatch):
    settings = Settings('fake', {123}, (
        CabinetConfig('cabinet_1', 'TT', 'fake1'),
        CabinetConfig('cabinet_2', 'Group', 'fake2'),
    ), 14, 4, 2, 20, 4, base_dir=tmp_path, admin_ids={123})
    workbook(tmp_path / 'templates' / 'stocks_template.xlsx', ['Баркод', 'Количество'], [])
    workbook(tmp_path / 'data' / 'kits_template.xlsx',
             ['Название', 'баркод комплекта'] + [f'баркод{index}' for index in range(1, 7)],
             [('200', 'pack200') + (FIRST_SOURCE,) * 2,
              ('300', 'pack300') + (FIRST_SOURCE,) * 3,
              ('600', 'pack600') + (FIRST_SOURCE,) * 6])
    monkeypatch.setattr(service_module.aiohttp, 'TCPConnector', lambda **kwargs: None)
    monkeypatch.setattr(service_module.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(service_module, 'WBClient', FakeWB)
    monkeypatch.setattr(stock_module, 'WBClient', FakeWB)
    FakeWB.stock = {}
    FakeWB.puts = []
    FakeWB.warehouse_ids = (11, 12)
    FakeWB.demand = {FIRST_SOURCE: 10, FIRST_HALF: 10, 'pack200': 10, 'pack300': 10,
                     'pack600': 10, SECOND_SOURCE: 10, SECOND_HALF: 10, 'mixed_kit': 10}
    return DistributionService(settings)


def line_reserve(result, *barcodes):
    return sum(line['reserve_qty'] for line in result['report']['allocations']
               if line['barcode'] in barcodes)


def assert_first_pool_balanced(result, source_qty):
    halves = sum(item['built'] for item in result['report']['kits']
                 if item['barcode'] in (FIRST_HALF, 'half_alias'))
    assert halves % 2 == 0
    used = (allocation(result, FIRST_SOURCE, 'source_alias') + halves // 2
            + allocation(result, 'pack200') * 2 + allocation(result, 'pack300') * 3
            + allocation(result, 'pack600') * 6)
    assert used + line_reserve(result, FIRST_SOURCE, 'source_alias') == source_qty
    assert all(isinstance(amount, int) and amount >= 0 for rows in warehouse_rows(result)
               for amount in rows.values())


def test_both_fixed_pairs_share_stock_with_template_kits(project):
    result = process(project, [(FIRST_SOURCE, 400), (SECOND_SOURCE, 40)])
    assert_first_pool_balanced(result, 400)
    for barcode in (FIRST_HALF, 'pack200', 'pack300', 'pack600', SECOND_HALF):
        assert allocation(result, barcode) >= 16
    assert (allocation(result, SECOND_SOURCE) + allocation(result, SECOND_HALF) // 2
            + line_reserve(result, SECOND_SOURCE) == 40)
    for item in result['report']['kits']:
        assert item['built'] == item['batches'] * item['output_per_batch']
    assert not FakeWB.puts


@pytest.mark.parametrize('quantity', [0, 1, 2, 3, 5, 9, 17, 33, 75, 201])
def test_scarce_stock_does_not_double_count_or_create_halves(project, quantity):
    result = process(project, [(FIRST_SOURCE, quantity)])
    assert_first_pool_balanced(result, quantity)


def test_halves_really_use_one_source_for_two_outputs(project):
    FakeWB.demand = {FIRST_HALF: 20}
    result = process(project, [(FIRST_SOURCE, 60), ('pack200', '!'), ('pack300', '!'), ('pack600', '!')])
    built = allocation(result, FIRST_HALF)
    assert built > 0 and built % 2 == 0
    assert result['report']['component_consumed_by_kits'][FIRST_SOURCE] == built // 2
    assert_first_pool_balanced(result, 60)


def test_split_rules_work_without_a_template(project):
    project.kits_path.unlink()
    result = process(project, [(FIRST_SOURCE, 50), (SECOND_SOURCE, 30)])
    assert allocation(result, FIRST_HALF) > 0
    assert allocation(result, SECOND_HALF) > 0
    assert (allocation(result, FIRST_SOURCE) + allocation(result, FIRST_HALF) // 2
            + line_reserve(result, FIRST_SOURCE) == 50)


def test_manual_and_percent_controls_remain_active(project):
    result = process(project, [(FIRST_SOURCE, 200), (FIRST_HALF, '%'), (SECOND_SOURCE, 40), (SECOND_HALF, '!')])
    assert allocation(result, FIRST_HALF, SECOND_HALF) == 0
    assert allocation(result, 'pack200') > 0
    assert_first_pool_balanced(result, 200)
    for rows in warehouse_rows(result):
        assert rows.get(FIRST_HALF, rows.get('half_alias')) == 0
        assert SECOND_HALF not in rows


def test_manual_source_is_not_opened(project):
    result = process(project, [(FIRST_SOURCE, '!'), (SECOND_SOURCE, 20)])
    assert allocation(result, FIRST_HALF, 'pack200', 'pack300', 'pack600') == 0
    assert allocation(result, SECOND_HALF) > 0
    assert FIRST_SOURCE not in result['report']['component_consumed_by_kits']


def test_ready_halves_are_distributed_without_more_splitting(project):
    result = process(project, [(FIRST_SOURCE, 200), (FIRST_HALF, 7)])
    assert allocation(result, FIRST_HALF) == 7
    assert not any(item['barcode'] == FIRST_HALF for item in result['report']['kits'])
    used = (allocation(result, FIRST_SOURCE) + allocation(result, 'pack200') * 2
            + allocation(result, 'pack300') * 3 + allocation(result, 'pack600') * 6)
    assert used + line_reserve(result, FIRST_SOURCE) == 200


def test_source_and_target_aliases_share_rules(project):
    result = process(project, [('source_alias', 80), ('half_alias', 0)])
    assert allocation(result, FIRST_HALF, 'half_alias') > 0
    assert_first_pool_balanced(result, 80)


def test_source_is_still_available_when_it_is_a_ready_template_kit(project):
    workbook(project.kits_path, ['Название', 'баркод комплекта', 'баркод1'],
             [('Source', FIRST_SOURCE, 'other_component')])
    result = process(project, [(FIRST_SOURCE, 40), ('other_component', 0)])
    assert allocation(result, FIRST_HALF) > 0
    assert not any(item['barcode'] == FIRST_SOURCE for item in result['report']['kits'])
    assert_first_pool_balanced(result, 40)


def test_template_controls_other_kit_components(project):
    workbook(project.kits_path, ['Название', 'баркод комплекта', 'баркод1', 'баркод2'],
             [('Mixed', 'mixed_kit', FIRST_SOURCE, 'other_component')])
    result = process(project, [(FIRST_SOURCE, 80), ('other_component', 25)])
    mixed = allocation(result, 'mixed_kit')
    halves = allocation(result, FIRST_HALF)
    assert mixed > 0 and halves > 0
    assert (allocation(result, FIRST_SOURCE) + halves // 2 + mixed
            + line_reserve(result, FIRST_SOURCE) == 80)
    assert allocation(result, 'other_component') + mixed + line_reserve(result, 'other_component') == 25


def test_fixed_half_recipe_overrides_same_template_barcode(project):
    workbook(project.kits_path, ['Название', 'баркод комплекта', 'баркод1', 'баркод2'],
             [('Half', FIRST_HALF, FIRST_SOURCE, FIRST_SOURCE)])
    result = process(project, [(FIRST_SOURCE, 80)])
    half = next(item for item in result['report']['kits'] if item['barcode'] == FIRST_HALF)
    assert half['output_per_batch'] == 2
    assert half['components'] == {FIRST_SOURCE: 1}
    assert_first_pool_balanced(result, 80)


def test_cabinet_exclusion_still_removes_target(project):
    workbook(project.exclusions_path, ['ШК', 'ID кабинета 1', 'ID кабинета 2'],
             [('half_alias', None, 'seller2')])
    result = process(project, [(FIRST_SOURCE, 80)])
    for path, rows in zip(result['files'], warehouse_rows(result)):
        if path.name.startswith('Group'):
            assert 'half_alias' not in rows
        else:
            assert rows[FIRST_HALF] > 0
    assert_first_pool_balanced(result, 80)


def test_no_orders_does_not_consume_unreported_pair_remainder(project):
    FakeWB.demand = {}
    FakeWB.warehouse_ids = (11,)
    result = process(project, [(FIRST_SOURCE, 100)])
    assert allocation(result, FIRST_HALF) == 8
    assert_first_pool_balanced(result, 100)


def test_odd_target_demand_rounds_to_whole_source_pack(project, monkeypatch):
    FakeWB.warehouse_ids = (11,)
    FakeWB.demand = {FIRST_HALF: 9}
    original_orders = FakeWB.get_orders
    async def first_cabinet_orders(client, days):
        return await original_orders(client, days) if client.cabinet.key == 'cabinet_1' else []
    monkeypatch.setattr(FakeWB, 'get_orders', first_cabinet_orders)
    result = process(project, [(FIRST_SOURCE, 40), ('pack200', '!'), ('pack300', '!'), ('pack600', '!')])
    assert allocation(result, FIRST_HALF) == 10
    assert_first_pool_balanced(result, 40)


def test_half_counts_survive_upload_verification_and_rollback(project):
    result = process(project, [(FIRST_SOURCE, 100), (SECOND_SOURCE, 40)])
    manager = StockManager(project.settings)
    async def scenario():
        preview = await manager.preview('distribution', 123, {'cabinet_1', 'cabinet_2'}, files=result['files'])
        assert not FakeWB.puts
        record = await manager.execute(preview['id'], 123)
        assert record['status'] == 'verified'
        for cabinet in ('cabinet_1', 'cabinet_2'):
            offset = 0 if cabinet == 'cabinet_1' else 100
            assert sum(FakeWB.stock.get((cabinet, warehouse, offset + 2), 0)
                       for warehouse in FakeWB.warehouse_ids) > 0
        rollback = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert (await manager.execute(rollback['id'], 123))['status'] == 'verified'
        assert all(amount == 0 for amount in FakeWB.stock.values())
    asyncio.run(scenario())


def test_incompatible_source_target_catalog_is_rejected():
    with pytest.raises(ValueError, match='общий chrtID'):
        apply_split_rules([], {FIRST_SOURCE: 20}, {FIRST_SOURCE: FIRST_SOURCE, FIRST_HALF: FIRST_SOURCE}, {})


def test_duplicate_target_aliases_in_template_are_rejected():
    kits = [KitDefinition('Half', barcode, (FIRST_SOURCE,), row)
            for row, barcode in enumerate((FIRST_HALF, 'half_alias'), start=2)]
    with pytest.raises(ValueError, match='Несколько строк'):
        apply_split_rules(kits, {FIRST_SOURCE: 20}, {'half_alias': FIRST_HALF}, {})


def test_unrelated_recipes_and_quantities_are_unchanged():
    recipe = KitDefinition('Other', 'unrelated', ('source', 'source'), 2)
    assert apply_split_rules([recipe], {'source': 20}, {}, {}) == [recipe]
    assert SPLIT_KIT_SOURCES == {FIRST_HALF: FIRST_SOURCE, SECOND_HALF: SECOND_SOURCE}


def test_different_split_targets_cannot_be_merged():
    with pytest.raises(ValueError, match='общий выходной chrtID'):
        apply_split_rules([], {FIRST_SOURCE: 20, SECOND_SOURCE: 30}, {SECOND_HALF: FIRST_HALF}, {})


def test_percent_on_an_alternative_half_barcode_prevents_splitting(project):
    result = process(project, [(FIRST_SOURCE, 200), ('half_alias', '%')])
    assert allocation(result, FIRST_HALF, 'half_alias') == 0
    assert_first_pool_balanced(result, 200)


def test_absent_source_does_not_create_stock_for_half_kits(project):
    result = process(project, [(FIRST_HALF, 0), (SECOND_SOURCE, 40)])
    assert allocation(result, FIRST_HALF, FIRST_SOURCE) == 0
    assert allocation(result, SECOND_HALF) > 0


def test_high_demand_for_halves_reduces_other_assemblies(project):
    FakeWB.demand = {FIRST_SOURCE: 10, FIRST_HALF: 30, 'pack200': 10, 'pack300': 10, 'pack600': 10}
    higher = process(project, [(FIRST_SOURCE, 500)])
    FakeWB.demand[FIRST_HALF] = 1
    lower = process(project, [(FIRST_SOURCE, 500)])
    assert allocation(higher, FIRST_HALF) > allocation(lower, FIRST_HALF)
    assert_first_pool_balanced(higher, 500)
    assert_first_pool_balanced(lower, 500)
