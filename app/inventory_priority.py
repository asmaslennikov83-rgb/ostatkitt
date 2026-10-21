PRIORITY_BARCODES = frozenset({'4610231972241', '2047555651545', '2046819266549'})
DEMAND_ONLY_BARCODES = frozenset({'2055014478763', '2047922424581'})


def priority_rules() -> dict:
    return {
        'hard_priority': sorted(PRIORITY_BARCODES),
        'strict_demand': sorted(DEMAND_ONLY_BARCODES),
        'protected_target': 'max(orders_for_period, safety_stock_per_warehouse * eligible_warehouses)',
        'strict_target': 'orders_for_period_without_safety_stock',
        'period': 'configured_lookback_days',
    }


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
