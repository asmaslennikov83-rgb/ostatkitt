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


def test_less_than_warehouses():
    whs, variants, counts = build()
    line = distribute_barcode("111", 2, whs, variants, counts, 20, 2)
    assert sum(line.allocations.values()) == 2
    assert line.allocations[("a", 1)] == 1
    assert line.allocations[("a", 2)] == 1


if __name__ == "__main__":
    test_small_all_distributed()
    test_large_rounds_down_and_keeps_tail()
    test_less_than_warehouses()
    print("OK")
