from app.distributor import distribute_barcode
from app.models import ProductVariant, Warehouse


def build():
    whs = [
        Warehouse("a", "A", 1, "W1"),
        Warehouse("a", "A", 2, "W2"),
        Warehouse("b", "B", 3, "W3"),
    ]
    variants = {
        "a": {"111": ProductVariant("a", 10, 100, ("111", "222"))},
        "b": {"111": ProductVariant("b", 20, 200, ("111",))},
    }
    counts = {
        "a": {(10, 1): 10, (10, 2): 5},
        "b": {(20, 3): 1},
    }
    return whs, variants, counts


def test_small_all_distributed():
    whs, variants, counts = build()
    line = distribute_barcode("111", 7, whs, variants, counts, 20, 2)
    assert sum(line.allocations.values()) == 7
    assert line.reserve_qty == 0
    assert all(v >= 1 for v in line.allocations.values())


def test_large_rounds_down_and_keeps_tail():
    whs, variants, counts = build()
    line = distribute_barcode("111", 23, whs, variants, counts, 20, 2)
    assert sum(line.allocations.values()) + line.reserve_qty == 23
    assert line.reserve_qty >= 0


def test_less_than_warehouses_keeps_cabinet_presence():
    whs, variants, counts = build()
    line = distribute_barcode("111", 2, whs, variants, counts, 20, 2)
    assert sum(line.allocations.values()) == 2
    assert sum(q for (cab, _), q in line.allocations.items() if cab == "a") == 1
    assert sum(q for (cab, _), q in line.allocations.items() if cab == "b") == 1


def test_no_sales_scarce_balances_cabinets():
    whs = [
        Warehouse("a", "A", 1, "A1"),
        Warehouse("a", "A", 2, "A2"),
        Warehouse("a", "A", 3, "A3"),
        Warehouse("a", "A", 4, "A4"),
        Warehouse("a", "A", 5, "A5"),
        Warehouse("a", "A", 6, "A6"),
        Warehouse("b", "B", 7, "B1"),
        Warehouse("b", "B", 8, "B2"),
        Warehouse("b", "B", 9, "B3"),
        Warehouse("b", "B", 10, "B4"),
    ]
    variants = {
        "a": {"111": ProductVariant("a", 10, 100, ("111",))},
        "b": {"111": ProductVariant("b", 20, 200, ("111",))},
    }
    counts = {"a": {}, "b": {}}
    line = distribute_barcode("111", 6, whs, variants, counts, 20, 2)
    a_qty = sum(qty for (cab, _wid), qty in line.allocations.items() if cab == "a")
    b_qty = sum(qty for (cab, _wid), qty in line.allocations.items() if cab == "b")
    assert a_qty == 3
    assert b_qty == 3


def test_scarce_with_sales_keeps_both_cabinets_present():
    whs = [
        Warehouse("a", "A", 1, "A1"),
        Warehouse("a", "A", 2, "A2"),
        Warehouse("b", "B", 3, "B1"),
        Warehouse("b", "B", 4, "B2"),
    ]
    variants = {
        "a": {"111": ProductVariant("a", 10, 100, ("111",))},
        "b": {"111": ProductVariant("b", 20, 200, ("111",))},
    }
    counts = {"a": {(10, 1): 10, (10, 2): 5}, "b": {}}
    line = distribute_barcode("111", 2, whs, variants, counts, 20, 2)
    assert sum(q for (cab, _), q in line.allocations.items() if cab == "a") == 1
    assert sum(q for (cab, _), q in line.allocations.items() if cab == "b") == 1


if __name__ == "__main__":
    test_small_all_distributed()
    test_large_rounds_down_and_keeps_tail()
    test_less_than_warehouses_keeps_cabinet_presence()
    test_no_sales_scarce_balances_cabinets()
    test_scarce_with_sales_keeps_both_cabinets_present()
    print("OK")
