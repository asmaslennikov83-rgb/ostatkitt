from __future__ import annotations

from collections import Counter
from dataclasses import replace

from .models import KitDefinition, ProductVariant


SPLIT_KIT_SOURCES = {
    '2046819245780': '4610231972418',
    '2046819266549': '4610231972241',
}


def apply_split_rules(
    kits: list[KitDefinition],
    stock: dict[str, int],
    alias_root: dict[str, str],
    catalogs: dict[str, dict[str, ProductVariant]],
) -> list[KitDefinition]:
    result = list(kits)
    stock_roots = {alias_root.get(sku, sku) for sku in stock}
    applied_targets = set()
    for target, source in SPLIT_KIT_SOURCES.items():
        target_root = alias_root.get(target, target)
        source_root = alias_root.get(source, source)
        matches = [index for index, kit in enumerate(result)
                   if alias_root.get(kit.barcode, kit.barcode) == target_root]
        if not matches and not {target_root, source_root} & stock_roots:
            continue
        if target_root in applied_targets:
            raise ValueError(f'Два разных правила деления имеют общий выходной chrtID: {target}')
        applied_targets.add(target_root)
        if len(matches) > 1:
            raise ValueError(f'Несколько строк шаблона относятся к набору по 50 шт. {target}')
        if target_root == source_root:
            raise ValueError(f'Набор {target} и исходный ШК {source} имеют общий chrtID')
        for cabinet_key, catalog in catalogs.items():
            for variant in catalog.values():
                roots = {alias_root.get(sku, sku) for sku in variant.skus}
                if {target_root, source_root}.issubset(roots):
                    raise ValueError(f'Набор {target} и исходный ШК {source} имеют общий chrtID '
                                     f'в кабинете {cabinet_key}')
        if matches:
            index = matches[0]
            result[index] = replace(result[index], components=(source,), output_per_batch=2)
        else:
            row_number = max((kit.row_number for kit in result), default=1) + 1
            result.append(KitDefinition(f'Набор 50 шт. из {source}', target, (source,), row_number,
                                        output_per_batch=2))
    return result


def resolve_split_components(
    kits: list[KitDefinition],
    stock: dict[str, int],
    alias_root: dict[str, str],
    blocked_roots: set[str],
) -> list[KitDefinition]:
    stocked_roots = {alias_root.get(sku, sku) for sku, quantity in stock.items() if quantity > 0}
    conversions = {alias_root.get(half, half): source for half, source in SPLIT_KIT_SOURCES.items()
                   if alias_root.get(half, half) not in stocked_roots | blocked_roots
                   and alias_root.get(source, source) not in blocked_roots}
    result = []
    for kit in kits:
        if alias_root.get(kit.barcode, kit.barcode) in stocked_roots | blocked_roots:
            result.append(kit)
            continue
        split_counts = Counter(alias_root.get(component, component) for component in kit.components
                               if alias_root.get(component, component) in conversions)
        if not split_counts:
            result.append(kit)
            continue
        multiplier = 2 if any(count % 2 for count in split_counts.values()) else 1
        components = tuple(component for component in kit.components
                           if alias_root.get(component, component) not in conversions) * multiplier
        for root, count in sorted(split_counts.items()):
            components += (conversions[root],) * (count * multiplier // 2)
        result.append(replace(kit, components=components, output_per_batch=kit.output_per_batch * multiplier))
    return result
