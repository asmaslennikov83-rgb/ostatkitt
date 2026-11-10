from app.inventory_priority import balanced_floors, spread_floors, dynamic_minimum
from app.distributor import distribute_barcode
from app.models import Warehouse, ProductVariant


def candidates(first, second):
    return [(Warehouse(key, key, ident, str(ident)), 100 if key == 'a' else 1)
            for key, count in [('a', first), ('b', second)] for ident in range(count)]


def test_balanced_floors_all_sizes_and_stock_levels():
    for first in range(1, 13):
        for second in range(1, 13):
            for quantity in range(first + second + 2):
                floors = balanced_floors(candidates(first, second), quantity)
                left = sum(key[0] == 'a' for key in floors)
                right = sum(key[0] == 'b' for key in floors)
                assert quantity == 1 or 100 * abs(left - right) <= 15 * min(left, right)
                assert left + right <= quantity
                assert all(value == 1 for value in floors.values())
                feasible = [(small, large) for small in range(first + 1) for large in range(second + 1)
                            if small + large <= quantity and 100 * abs(small - large) <= 15 * min(small, large)]
                assert len(floors) == (1 if quantity == 1 else max(sum(pair) for pair in feasible))


def test_eight_and_seven_allowed_but_nine_and_seven_is_capped():
    assert len(balanced_floors(candidates(8, 7), 100)) == 15
    assert len(balanced_floors(candidates(9, 7), 100)) == 15
    assert balanced_floors(candidates(3, 4), 1) == {('a', 0): 1}
    assert len(balanced_floors(candidates(3, 4), 5)) == 4


def test_one_available_cabinet_and_empty_candidates():
    assert len(balanced_floors(candidates(3, 0), 10)) == 3
    assert balanced_floors([], 100) == {}


def test_five_skus_cover_every_warehouse_before_repetition():
    plans = {str(index): (candidates(8, 7), 3 if index < 3 else 1, index >= 3) for index in range(5)}
    floors = spread_floors(plans)
    for cabinet, count in [('a', 8), ('b', 7)]:
        covered = {warehouse for rows in floors.values() for key, warehouse in rows if key == cabinet}
        assert covered == set(range(count))
        assert all(sum(key == cabinet for key, _warehouse in floors[str(index)]) == (3 if index < 3 else 1)
                   for index in range(5))
    assert spread_floors(dict(reversed(list(plans.items())))) == floors


def test_reported_41_units_no_longer_leave_second_cabinet_with_one():
    rows = candidates(8, 7)
    warehouses = [warehouse for warehouse, _orders in rows]
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b')}
    orders = {'a': {(1, 0): 29}, 'b': {(1, 0): 1}}
    for quantity in range(60):
        line = distribute_barcode('sku', quantity, warehouses, variants, orders, 20, 2,
                                  balanced_safety=True)
        assert sum(line.allocations.values()) + line.reserve_qty == quantity
    line = distribute_barcode('sku', 41, warehouses, variants, orders, 20, 2, balanced_safety=True)
    assert sum(value for (key, _warehouse), value in line.allocations.items() if key == 'b') >= 7


def test_dynamic_thresholds_and_different_warehouse_counts():
    for count in (1, 2, 7, 16, 25):
        for quantity in range(400):
            average = quantity / count
            expected = 1 if average < 3 else 2 if average < 5 else 3 if average < 7 else 4 if average < 9 else 5
            assert dynamic_minimum(quantity, count) == expected
    assert dynamic_minimum(100, 0) == 1


def test_dynamic_minima_conserve_stock_and_balance_cabinets():
    for first, second in ((8, 8), (8, 7), (3, 8), (1, 0)):
        rows = candidates(first, second)
        for quantity in (0, 1, 47, 48, 79, 80, 111, 112, 143, 144, 160, 1000):
            minimum = dynamic_minimum(quantity, len(rows))
            floors = balanced_floors(rows, quantity, minimum)
            assert sum(floors.values()) <= quantity
            assert all(1 <= amount <= minimum for amount in floors.values())
            if second:
                left = sum(amount for (key, _warehouse), amount in floors.items() if key == 'a')
                right = sum(floors.values()) - left
                assert quantity == 1 or 100 * abs(left - right) <= 15 * min(left, right)


def test_160_units_give_five_each_without_orders_and_keep_remainder():
    rows = candidates(8, 8)
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b')}
    line = distribute_barcode('sku', 160, [warehouse for warehouse, _orders in rows],
                              variants, {'a': {}, 'b': {}}, 20, 2, balanced_safety=True)
    assert set(line.allocations.values()) == {5}
    assert line.reserve_qty == 80
    assert line.distribution_details['safety_stock_per_warehouse'] == 5


def test_special_explicit_floors_do_not_grow_with_stock():
    rows = candidates(8, 8)
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b')}
    line = distribute_barcode('sku', 160, [warehouse for warehouse, _orders in rows],
                              variants, {'a': {}, 'b': {}}, 20, 2, warehouse_floors={('a', 0): 1})
    assert sum(line.allocations.values()) == 1
    assert line.reserve_qty == 159


def test_singleton_with_no_orders_is_listed_not_reserved():
    rows = [(warehouse, 0) for warehouse, _orders in candidates(8, 8)]
    variants = {key: {'sku': ProductVariant(key, 1, 1, ('sku',))} for key in ('a', 'b')}
    line = distribute_barcode('sku', 1, [warehouse for warehouse, _orders in rows], variants,
                              {'a': {}, 'b': {}}, 20, 2, balanced_safety=True)
    assert sum(line.allocations.values()) == 1
    assert line.reserve_qty == 0
    assert line.distribution_details['singleton_balance_exception']


def test_singleton_prefers_highest_demand_and_is_stable():
    rows = candidates(2, 2)
    rows[-1] = (rows[-1][0], 200)
    assert balanced_floors(rows, 1) == {('b', 1): 1}
    assert balanced_floors(list(reversed(rows)), 1) == {('b', 1): 1}


def test_300_600_seed_both_cabinets_with_no_or_one_sided_orders():
    for orders in (False, True):
        rows = [(warehouse, int(orders and warehouse.cabinet_key == 'a'))
                for warehouse, _orders in candidates(3, 4)]
        floors = spread_floors({'300': (rows, 1, True), '600': (rows, 1, True)})
        for selected in floors.values():
            assert sum(key[0] == 'a' for key in selected) == 1
            assert sum(key[0] == 'b' for key in selected) == 1


def test_300_600_do_not_create_missing_cabinet():
    floors = spread_floors({'600': (candidates(3, 0), 1, True)})
    assert len(floors['600']) == 1
    assert {key[0] for key in floors['600']} == {'a'}
