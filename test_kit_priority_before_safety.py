"""Regression: 1-unit single presence -> kits -> single safety stock."""
from app.service import _kit_target_sets

physical_qty = 16
warehouse_count = 10
component_multiplicity = 2
sales_14d = 5

# Only 1 single unit per warehouse is protected before kits.
basic_single_reserved = min(physical_qty, warehouse_count)
remaining_for_kits = physical_qty - basic_single_reserved
possible_sets = remaining_for_kits // component_multiplicity
built = _kit_target_sets(possible_sets, sales_14d, warehouse_count, 4)
consumed = built * component_multiplicity
left_after_kits = remaining_for_kits - consumed

assert basic_single_reserved == 10
assert possible_sets == 3
assert built == 3
assert consumed == 6
assert left_after_kits == 0
print("OK kit priority before safety stock")
