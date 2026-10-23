PRIORITY_BARCODES = frozenset({'4610231972241', '2047555651545', '2046819266549'})
DEMAND_ONLY_BARCODES = frozenset({'2055014478763', '2047922424581'})


def priority_rules() -> dict:
    return {
        'hard_priority': sorted(PRIORITY_BARCODES),
        'strict_demand': sorted(DEMAND_ONLY_BARCODES),
        'protected_target': 'three_warehouses_per_cabinet_coverage_before_orders',
        'strict_target': 'orders_with_one_included_unit_on_top_selling_warehouse_per_cabinet',
        'no_orders_fallback': 'selected_warehouses_in_one_available_cabinet',
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
        if not any(orders for _warehouse, orders in candidates) and cabinets:
            cabinets = {min(cabinets)}
        elif selling_only:
            cabinets = {warehouse.cabinet_key for warehouse, orders in candidates if orders}
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


def balanced_floors(candidates: list, quantity: int) -> dict:
    groups = {}
    for warehouse, orders in candidates:
        groups.setdefault(warehouse.cabinet_key, []).append((warehouse, orders))
    keys = sorted(groups)
    if len(keys) < 2:
        quotas = {key: min(quantity, len(groups[key])) for key in keys}
    else:
        first, second = keys
        choices = [(left, right) for left in range(len(groups[first]) + 1)
                   for right in range(len(groups[second]) + 1)
                   if left + right <= quantity and 100 * abs(left - right) <= 15 * min(left, right)]
        left, right = max(choices, key=lambda pair: (sum(pair), -abs(pair[0] - pair[1]), pair[0]))
        quotas = {first: left, second: right}
    return {(warehouse.cabinet_key, warehouse.warehouse_id): 1
            for key, rows in groups.items()
            for warehouse, _orders in sorted(rows, key=lambda item: (-item[1], item[0].warehouse_id))[:quotas[key]]}


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
