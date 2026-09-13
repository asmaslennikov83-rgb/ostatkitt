
from app.distributor import distribute_barcode
from app.models import ProductVariant, Warehouse

warehouses = []
variants = {"cabinet_1": {}, "cabinet_2": {}}
orders = {"cabinet_1": {}, "cabinet_2": {}}

barcode = "2047555679853"
chrt = 999
for i in range(6):
    wh = Warehouse("cabinet_1", "Кабинет 1", 100+i, f"A{i+1}")
    warehouses.append(wh)
for i in range(4):
    wh = Warehouse("cabinet_2", "Кабинет 2", 200+i, f"B{i+1}")
    warehouses.append(wh)

variants["cabinet_1"][barcode] = ProductVariant(chrt_id=chrt, nm_id=1, skus=(barcode,))
variants["cabinet_2"][barcode] = ProductVariant(chrt_id=chrt, nm_id=1, skus=(barcode,))

# All demand on a single warehouse: minimum 1 must still remain everywhere.
orders["cabinet_1"][(chrt, 100)] = 100

line = distribute_barcode(
    barcode=barcode,
    quantity=50,
    warehouses=warehouses,
    barcode_variants_by_cabinet=variants,
    order_counts_by_cabinet=orders,
    threshold=20,
    no_sales_target=2,
)

assert len(line.allocations) == 10
assert all(q >= 1 for q in line.allocations.values()), line.allocations
assert sum(line.allocations.values()) + line.reserve_qty == 50
print("OK:", line.allocations, "reserve", line.reserve_qty)
