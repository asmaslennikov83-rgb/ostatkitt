from __future__ import annotations

import math
from collections import defaultdict

from .models import DistributionLine, ProductVariant, Warehouse


def _priority_key(item: tuple[Warehouse, int]) -> tuple[int, str, int]:
    wh, sales = item
    return (-sales, wh.cabinet_name.lower(), wh.warehouse_id)


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

    # Если товара меньше, чем складов: по 1 шт. складам с наибольшим спросом.
    if quantity < len(candidates):
        for wh, _sales in candidates[:quantity]:
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
        # Нет истории: доводим каждый склад до 2 шт., пока хватает.
        target = max(1, no_sales_target)
        if target > 1:
            for wh, _sales in candidates:
                if remaining <= 0:
                    break
                add = min(target - allocations[(wh.cabinet_key, wh.warehouse_id)], remaining)
                allocations[(wh.cabinet_key, wh.warehouse_id)] += add
                remaining -= add
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
