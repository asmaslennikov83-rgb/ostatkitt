PRIORITY_BARCODES = frozenset({'4610231972241', '2047555651545', '2046819266549'})
DEMAND_ONLY_BARCODES = frozenset({'2055014478763', '2047922424581'})


def priority_rules() -> dict:
    return {
        'hard_priority': sorted(PRIORITY_BARCODES),
        'strict_demand': sorted(DEMAND_ONLY_BARCODES),
        'protected_target': 'one_included_unit_on_top_three_warehouses_per_cabinet',
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
