from collections import Counter

import pytest

from app.distributor import distribute_barcode
from app.models import ProductVariant, Warehouse


def calculate(quantity, first_orders=1, second_orders=2, names=('СПб-ТТ', 'СПб-ТТ ГРУПП'),
              warehouse_counts=(6, 6), reverse=False):
    barcode = '4744131011486'
    warehouses = [Warehouse(cabinet, names[index], index * 100 + warehouse, str(warehouse))
                  for index, cabinet in enumerate(('cabinet_1', 'cabinet_2'))
                  for warehouse in range(1, warehouse_counts[index] + 1)]
    if reverse:
        warehouses.reverse()
    variants = {key: {barcode: ProductVariant(key, 1, 1, (barcode,))}
                for key in ('cabinet_1', 'cabinet_2')}
    counts = {'cabinet_1': {(1, 1): first_orders}, 'cabinet_2': {(1, 101): second_orders}}
    return distribute_barcode(barcode, quantity, warehouses, variants, counts, 20, 2, 4)


def totals(line):
    result = Counter()
    for (cabinet, warehouse), amount in line.allocations.items():
        result[cabinet] += amount
    return dict(result)


def test_reported_barcode_gets_three_and_six():
    result = calculate(9)
    assert totals(result) == {'cabinet_1': 3, 'cabinet_2': 6}
    assert result.reserve_qty == 0
    assert result.distribution_details['rule'] == 'cabinet_demand_deficit'
    assert result.distribution_details['cabinet_quotas'] == {'cabinet_1': 3, 'cabinet_2': 6}


@pytest.mark.parametrize('quantity', [1, 2, 3, 5, 9, 12, 17, 24, 35, 47])
def test_deficit_balances_and_uses_all_stock(quantity):
    result = calculate(quantity)
    assert sum(result.allocations.values()) == quantity
    assert result.reserve_qty == 0
    assert all(isinstance(value, int) and value >= 0 for value in result.allocations.values())
    shares = totals(result)
    if quantity >= 2:
        assert shares['cabinet_1'] >= 1 and shares['cabinet_2'] >= 1
    if quantity % 3 == 0:
        assert shares == {'cabinet_1': quantity // 3, 'cabinet_2': 2 * quantity // 3}


def test_one_unit_goes_to_higher_demand():
    assert totals(calculate(1)) == {'cabinet_1': 0, 'cabinet_2': 1}


def test_renaming_cabinets_and_reversing_warehouses_does_not_change_shares():
    expected = totals(calculate(9))
    assert totals(calculate(9, names=('Я', 'А'))) == expected
    assert totals(calculate(9, reverse=True)) == expected


@pytest.mark.parametrize('quantity', [2, 9, 24, 47])
def test_zero_demand_cabinet_keeps_minimum_presence(quantity):
    assert totals(calculate(quantity, 100, 0)) == {'cabinet_1': quantity - 1, 'cabinet_2': 1}


def test_no_orders_split_equally_not_by_warehouse_count():
    assert totals(calculate(9, 0, 0, warehouse_counts=(6, 3))) == {'cabinet_1': 5, 'cabinet_2': 4}


def test_no_orders_does_not_overfill_small_cabinet_and_leave_others_empty():
    result = calculate(20, 0, 0, warehouse_counts=(1, 6))
    assert totals(result) == {'cabinet_1': 4, 'cabinet_2': 16}
    assert sum(result.allocations.values()) == 20


def test_full_safety_still_protects_every_warehouse_when_stock_is_sufficient():
    result = calculate(60, 100, 0)
    assert all(value >= 4 for value in result.allocations.values())
    assert sum(result.allocations.values()) + result.reserve_qty == 60


def test_single_cabinet_is_not_limited_by_absent_cabinet():
    result = calculate(9, 1, 0, warehouse_counts=(6, 0))
    assert totals(result) == {'cabinet_1': 9}


def test_three_cabinets_round_proportionally_and_keep_stock():
    warehouses = [Warehouse(key, key, 1, 'Main') for key in ('a', 'b', 'c')]
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b', 'c')}
    counts = {key: {(1, 1): value} for key, value in zip(('a', 'b', 'c'), (1, 2, 3))}
    result = distribute_barcode('sku', 6, warehouses, variants, counts, 20, 2, 4)
    assert totals(result) == {'a': 1, 'b': 2, 'c': 3}


def test_deficit_conservation_with_different_warehouse_counts_and_demands():
    for warehouse_counts in ((1, 1), (1, 6), (6, 1), (5, 7)):
        for demand in ((0, 0), (1, 2), (100, 0), (3, 19)):
            for quantity in range(1, sum(warehouse_counts) * 4):
                result = calculate(quantity, *demand, warehouse_counts=warehouse_counts)
                assert sum(result.allocations.values()) == quantity
                assert result.reserve_qty == 0
                if quantity >= 2:
                    assert all(amount >= 1 for amount in totals(result).values())
