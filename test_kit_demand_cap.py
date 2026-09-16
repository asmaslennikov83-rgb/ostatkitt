"""Regression: kit building is capped by demand, with a 4-unit safety floor per warehouse."""
from app.service import _kit_target_sets

physical_qty = 409
safety_stock_per_warehouse = 4
warehouse_count = 10
single_minimum_reserved = warehouse_count * safety_stock_per_warehouse
component_multiplicity = 2
sales_14d = 68
possible = (physical_qty - single_minimum_reserved) // component_multiplicity
built = _kit_target_sets(possible, sales_14d, warehouse_count, safety_stock_per_warehouse)
consumed = built * component_multiplicity

assert possible == 184
assert built == 68
assert consumed == 136
assert consumed < 388
# Low/no demand still keeps 4 kits per warehouse when components allow it.
assert _kit_target_sets(100, 3, 10, 4) == 40
assert _kit_target_sets(100, 0, 10, 4) == 40
assert _kit_target_sets(4, 68, 10, 4) == 4
print("OK kit demand cap")
