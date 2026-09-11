from __future__ import annotations

import math
from collections import defaultdict

from .models import DistributionLine, ProductVariant, Warehouse


def _priority_key(item: tuple[Warehouse, int]) -> tuple[int, str, int]:
    wh, sales = item
    return (-sales, wh.cabinet_name.lower(), wh.warehouse_id)


def _balanced_no_sales_order(candidates: list[tuple[Warehouse, int]]) -> list[tuple[Warehouse, int]]:
    """Interleave warehouses by cabinet instead of exhausting one cabinet first.

    This is used when there is no sales history. Example: with two cabinets
    A=[A1,A2,A3] and B=[B1,B2], the order becomes A1,B1,A2,B2,A3.
    """
    by_cabinet: dict[str, list[tuple[Warehouse, int]]] = defaultdict(list)
    cabinet_names: dict[str, str] = {}
    for item in candidates:
        wh, _sales = item
        by_cabinet[wh.cabinet_key].append(item)
        cabinet_names[wh.cabinet_key] = wh.cabinet_name.lower()

    cabinet_keys = sorted(by_cabinet, key=lambda key: (cabinet_names[key], key))
    for key in cabinet_keys:
        by_cabinet[key].sort(key=lambda item: (item[0].warehouse_id, item[0].name.lower()))

    result: list[tuple[Warehouse, int]] = []
    level = 0
    while True:
        added = False
        for key in cabinet_keys:
            rows = by_cabinet[key]
            if level < len(rows):
                result.append(rows[level])
                added = True
        if not added:
            break
        level += 1
    return result


def _scarce_stock_order(candidates: list[tuple[Warehouse, int]]) -> list[tuple[Warehouse, int]]:
    """Order candidates when stock is insufficient for every warehouse.

    1. If there are no sales at all, alternate cabinets.
    2. If sales exist, first keep cabinet coverage (best warehouse of each
       cabinet), then use sales priority for the rest. This prevents a SKU from
       disappearing completely from another cabinet merely because its recent
       sales are zero.
    """
    total_sales = sum(sales for _wh, sales in candidates)
    if total_sales == 0:
        return _balanced_no_sales_order(candidates)

    by_cabinet: dict[str, list[tuple[Warehouse, int]]] = defaultdict(list)
    for item in candidates:
        by_cabinet[item[0].cabinet_key].append(item)

    cabinet_groups: list[tuple[int, str, str, list[tuple[Warehouse, int]]]] = []
    for cabinet_key, items in by_cabinet.items():
        items.sort(key=_priority_key)
        cabinet_sales = sum(sales for _wh, sales in items)
        cabinet_name = items[0][0].cabinet_name.lower()
        cabinet_groups.append((-cabinet_sales, cabinet_name, cabinet_key, items))
    cabinet_groups.sort()

    result: list[tuple[Warehouse, int]] = []
    used: set[tuple[str, int]] = set()

    # One strongest warehouse from every cabinet first.
    for _neg_sales, _name, _key, items in cabinet_groups:
        first = items[0]
        result.append(first)
        used.add((first[0].cabinet_key, first[0].warehouse_id))

    # Then remaining warehouses strictly by demand.
    rest = [
        item for item in candidates
        if (item[0].cabinet_key, item[0].warehouse_id) not in used
    ]
    rest.sort(key=_priority_key)
    result.extend(rest)
    return result


def distribute_barcode(
    barcode: str,
    quantity: int,
    warehouses: list[Warehouse],
    barcode_variants_by_cabinet: dict[str, dict[str, ProductVariant]],
    order_counts_by_cabinet: dict[str, dict[tuple[int, int], int]],
    threshold: int,
    no_sales_target: int,
) -> DistributionLine:
    line = DistributionLine(barcode=barcode, source_qty=quantity)
    if quantity <= 0:
        return line

    candidates: list[tuple[Warehouse, int]] = []
    for wh in warehouses:
        variant = barcode_variants_by_cabinet.get(wh.cabinet_key, {}).get(barcode)
        if not variant:
            continue
        line.found_in_cabinets.add(wh.cabinet_key)
        sales = order_counts_by_cabinet.get(wh.cabinet_key, {}).get((variant.chrt_id, wh.warehouse_id), 0)
        candidates.append((wh, int(sales)))

    if not candidates:
        line.reserve_qty = quantity
        return line

    candidates.sort(key=_priority_key)
    allocations: dict[tuple[str, int], int] = {(wh.cabinet_key, wh.warehouse_id): 0 for wh, _ in candidates}

    # Если товара меньше, чем складов, нельзя дать по 1 шт. всем.
    # Важно не "съедать" весь дефицит первым кабинетом:
    # без истории чередуем кабинеты; при наличии истории сначала сохраняем
    # присутствие хотя бы в каждом кабинете, затем идём по спросу.
    if quantity < len(candidates):
        scarce_order = _scarce_stock_order(candidates)
        for wh, _sales in scarce_order[:quantity]:
            allocations[(wh.cabinet_key, wh.warehouse_id)] = 1
        line.allocations = allocations
        return line

    # Обязательный минимум: 1 шт. на каждый склад.
    remaining = quantity
    for wh, _sales in candidates:
        allocations[(wh.cabinet_key, wh.warehouse_id)] = 1
        remaining -= 1

    total_sales = sum(sales for _wh, sales in candidates)

    if total_sales == 0:
        # Нет истории: доводим каждый склад до целевого минимума, но также
        # чередуем кабинеты, чтобы дополнительные единицы не ушли только
        # в первый кабинет.
        target = max(1, no_sales_target)
        if target > 1:
            balanced = _balanced_no_sales_order(candidates)
            for _level in range(1, target):
                for wh, _sales in balanced:
                    if remaining <= 0:
                        break
                    key = (wh.cabinet_key, wh.warehouse_id)
                    if allocations[key] < target:
                        allocations[key] += 1
                        remaining -= 1
                if remaining <= 0:
                    break
        line.reserve_qty = remaining
        line.allocations = allocations
        return line

    # Есть история продаж. Распределяем остаток сверх обязательного минимума пропорционально заказам.
    if remaining > 0:
        raw_extra: list[tuple[Warehouse, int, float, int]] = []
        floor_sum = 0
        for wh, sales in candidates:
            exact = remaining * sales / total_sales
            floored = math.floor(exact)
            raw_extra.append((wh, sales, exact, floored))
            allocations[(wh.cabinet_key, wh.warehouse_id)] += floored
            floor_sum += floored

        leftover = remaining - floor_sum

        if quantity <= threshold:
            # До 20 шт. распределяем весь остаток. Сначала по максимальной дробной части,
            # затем по продажам, затем стабильный порядок.
            raw_extra.sort(key=lambda x: (-(x[2] - x[3]), -x[1], x[0].cabinet_name.lower(), x[0].warehouse_id))
            i = 0
            while leftover > 0 and raw_extra:
                wh = raw_extra[i % len(raw_extra)][0]
                allocations[(wh.cabinet_key, wh.warehouse_id)] += 1
                leftover -= 1
                i += 1
            line.reserve_qty = 0
        else:
            # Более 20 шт.: дробный хвост оставляем в физическом резерве.
            line.reserve_qty = leftover

    line.allocations = allocations
    return line
