from app.distributor import distribute_barcode
from app.models import ProductVariant, Warehouse

barcode = "SKU"
warehouses = [
    Warehouse("a", "A", 1, "A1"),
    Warehouse("a", "A", 2, "A2"),
    Warehouse("b", "B", 3, "B1"),
    Warehouse("b", "B", 4, "B2"),
]
variants = {
    "a": {barcode: ProductVariant("a", 10, 100, (barcode,))},
    "b": {barcode: ProductVariant("b", 20, 200, (barcode,))},
}
orders = {"a": {(10, 1): 100}, "b": {}}

# Enough stock: every warehouse gets at least 4, despite all sales on A1.
line = distribute_barcode(barcode, 30, warehouses, variants, orders, 20, 2, 4)
assert all(q >= 4 for q in line.allocations.values()), line.allocations
assert sum(line.allocations.values()) + line.reserve_qty == 30

line = distribute_barcode(barcode, 10, warehouses, variants, orders, 20, 2, 4)
assert sum(qty for (cabinet, warehouse), qty in line.allocations.items() if cabinet == 'a') == 9
assert sum(qty for (cabinet, warehouse), qty in line.allocations.items() if cabinet == 'b') == 1
assert sum(line.allocations.values()) == 10
print("OK safety stock", line.allocations)
