PRIORITY_BARCODES = frozenset({'4610231972241', '2047555651545', '2046819266549'})
DEMAND_ONLY_BARCODES = frozenset({'2055014478763', '2047922424581'})


def priority_rules() -> dict:
    return {
        'hard_priority': sorted(PRIORITY_BARCODES),
        'strict_demand': sorted(DEMAND_ONLY_BARCODES),
        'protected_target': 'three_warehouses_per_cabinet_coverage_before_orders',
        'strict_target': 'orders_with_one_included_unit_in_every_available_cabinet',
        'no_orders_fallback': 'three_sku_group_one_cabinet_300_600_every_cabinet',
        'period': 'configured_lookback_days',
    }


def selected_floors(candidates: list, limit: int, selling_only: bool = False) -> dict:
    by_cabinet = {}
    for warehouse, orders in candidates:
        by_cabinet.setdefault(warehouse.cabinet_key, []).append((warehouse, orders))
    if not any(orders for _warehouse, orders in candidates) and by_cabinet:
        cabinet = min(by_cabinet)
        by_cabinet = {cabinet: by_cabinet[cabinet]}
        selling_only = False
    floors = {}
    for rows in by_cabinet.values():
        rows.sort(key=lambda item: (-item[1], item[0].warehouse_id))
        eligible = [item for item in rows if item[1] > 0 or not selling_only]
        for warehouse, _orders in eligible[:limit]:
            floors[warehouse.cabinet_key, warehouse.warehouse_id] = 1
    return floors


def spread_floors(plans: dict) -> dict:
    eligible = {}
    result = {sku: {} for sku in plans}
    covered = {}
    for sku, (candidates, limit, selling_only) in sorted(plans.items()):
        cabinets = {warehouse.cabinet_key for warehouse, _orders in candidates}
        if not selling_only and not any(orders for _warehouse, orders in candidates) and cabinets:
            cabinets = {min(cabinets)}
        eligible[sku] = [(warehouse, orders) for warehouse, orders in candidates if warehouse.cabinet_key in cabinets]
    for level in range(max((plan[1] for plan in plans.values()), default=0)):
        for sku in sorted(plans, key=lambda key: (plans[key][1], key)):
            if level >= plans[sku][1]:
                continue
            for cabinet in sorted({warehouse.cabinet_key for warehouse, _orders in eligible[sku]}):
                choices = [(warehouse, orders) for warehouse, orders in eligible[sku]
                           if warehouse.cabinet_key == cabinet
                           and (cabinet, warehouse.warehouse_id) not in result[sku]]
                if choices:
                    warehouse, _orders = min(choices, key=lambda item: (
                        covered.get((cabinet, item[0].warehouse_id), 0), -item[1], item[0].warehouse_id))
                    key = cabinet, warehouse.warehouse_id
                    result[sku][key] = 1
                    covered[key] = covered.get(key, 0) + 1
    return result


def dynamic_minimum(quantity: int, warehouse_count: int) -> int:
    if warehouse_count <= 0:
        return 1
    return min(5, max(1, (quantity // warehouse_count + 1) // 2))


def balanced_floors(candidates: list, quantity: int, minimum: int = 1) -> dict:
    if quantity == 1 and candidates:
        warehouse, _orders = min(candidates, key=lambda item: (
            -item[1], item[0].cabinet_key, item[0].warehouse_id))
        return {(warehouse.cabinet_key, warehouse.warehouse_id): 1}
    groups = {}
    for warehouse, orders in candidates:
        groups.setdefault(warehouse.cabinet_key, []).append((warehouse, orders))
    keys = sorted(groups)
    if len(keys) < 2:
        quotas = {key: min(quantity, minimum * len(groups[key])) for key in keys}
    else:
        first, second = keys
        choices = [(left, right) for left in range(min(quantity, minimum * len(groups[first])) + 1)
                   for right in range(min(quantity, minimum * len(groups[second])) + 1)
                   if left + right <= quantity and 100 * abs(left - right) <= 15 * min(left, right)]
        left, right = max(choices, key=lambda pair: (sum(pair), -abs(pair[0] - pair[1]), pair[0]))
        quotas = {first: left, second: right}
    floors = {}
    for key, rows in groups.items():
        base, remainder = divmod(quotas[key], len(rows))
        for index, (warehouse, _orders) in enumerate(sorted(rows, key=lambda item: (-item[1], item[0].warehouse_id))):
            amount = base + (index < remainder)
            if amount:
                floors[key, warehouse.warehouse_id] = amount
    return floors


def policy_roots(alias_root: dict[str, str], catalogs: dict) -> tuple[set[str], set[str]]:
    priority = {alias_root.get(sku, sku) for sku in PRIORITY_BARCODES}
    strict = {alias_root.get(sku, sku) for sku in DEMAND_ONLY_BARCODES}
    if priority & strict:
        raise ValueError('Приоритетный ШК и ШК со строгой потребностью объединены в одну вариацию')
    for cabinet, catalog in catalogs.items():
        for variant in catalog.values():
            roots = {alias_root.get(sku, sku) for sku in variant.skus}
            if roots & priority and roots & strict:
                raise ValueError(f'Приоритетный ШК и ШК со строгой потребностью имеют общий chrtID '
                                 f'{variant.chrt_id} в кабинете {cabinet}')
    return priority, strict
