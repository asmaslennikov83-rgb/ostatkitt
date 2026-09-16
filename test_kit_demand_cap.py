"""Regression: kit building must be capped by 14-day demand, not raw components."""
from app.service import _kit_target_sets

physical_qty = 409
single_minimum_reserved = 10
component_multiplicity = 2
sales_14d = 68
warehouse_count = 10
possible = (physical_qty - single_minimum_reserved) // component_multiplicity
built = _kit_target_sets(possible, sales_14d, warehouse_count)
consumed = built * component_multiplicity

assert possible == 199
assert built == 68
assert consumed == 136
assert consumed < 388
assert _kit_target_sets(100, 3, 10) == 10
assert _kit_target_sets(100, 0, 10) == 10
assert _kit_target_sets(4, 68, 10) == 4
print("OK kit demand cap")
