from app.inventory_priority import balanced_floors, spread_floors
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
                assert 100 * abs(left - right) <= 15 * min(left, right)
                assert left + right <= quantity
                assert all(value == 1 for value in floors.values())
                feasible = [(small, large) for small in range(first + 1) for large in range(second + 1)
                            if small + large <= quantity and 100 * abs(small - large) <= 15 * min(small, large)]
                assert len(floors) == max(sum(pair) for pair in feasible)


def test_eight_and_seven_allowed_but_nine_and_seven_is_capped():
    assert len(balanced_floors(candidates(8, 7), 100)) == 15
    assert len(balanced_floors(candidates(9, 7), 100)) == 15
    assert balanced_floors(candidates(3, 4), 1) == {}
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
