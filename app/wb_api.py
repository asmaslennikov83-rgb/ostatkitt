from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

import aiohttp

from .config import CabinetConfig
from .models import ProductVariant, Warehouse

MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
CONTENT_BASE = "https://content-api.wildberries.ru"


class WBApiError(RuntimeError):
    pass


class WBClient:
    def __init__(self, cabinet: CabinetConfig, session: aiohttp.ClientSession):
        self.cabinet = cabinet
        self.session = session
        self.headers = {"Authorization": cabinet.token, "Content-Type": "application/json"}

    async def _request(self, method: str, url: str, **kwargs):
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with self.session.request(method, url, headers=self.headers, timeout=60, **kwargs) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        body = await resp.text()
                        if attempt == 3:
                            raise WBApiError(f"{self.cabinet.name}: WB API {resp.status}: {body[:500]}")
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise WBApiError(f"{self.cabinet.name}: WB API {resp.status}: {body[:700]}")
                    if resp.status == 204:
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt == 3:
                    raise WBApiError(f"{self.cabinet.name}: ошибка соединения с WB API: {exc}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise WBApiError(str(last_error))

    async def get_warehouses(self) -> list[Warehouse]:
        data = await self._request("GET", f"{MARKETPLACE_BASE}/api/v3/warehouses")
        result: list[Warehouse] = []
        for wh in data or []:
            # deliveryType=1 — FBS в текущей документации. Если поле отсутствует, склад не отбрасываем.
            delivery_type = wh.get("deliveryType")
            if delivery_type not in (None, 1, "1", "fbs", "FBS"):
                continue
            if wh.get("isDeleting") is True:
                continue
            result.append(Warehouse(
                cabinet_key=self.cabinet.key,
                cabinet_name=self.cabinet.name,
                warehouse_id=int(wh["id"]),
                name=str(wh.get("name") or f"Склад {wh['id']}"),
            ))
        return result

    async def get_orders(self, days: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        date_from = int((now - timedelta(days=days)).timestamp())
        date_to = int(now.timestamp())
        cursor = 0
        result: list[dict] = []
        seen_cursors: set[int] = set()

        while True:
            params = {
                "limit": 1000,
                "next": cursor,
                "dateFrom": date_from,
                "dateTo": date_to,
            }
            data = await self._request("GET", f"{MARKETPLACE_BASE}/api/v3/orders", params=params)
            batch = (data or {}).get("orders", [])
            result.extend(batch)
            nxt = int((data or {}).get("next") or 0)
            if not batch or nxt == 0 or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
        return result

    async def get_catalog_variants(self) -> tuple[dict[str, ProductVariant], dict[int, ProductVariant]]:
        """Returns barcode->variant and chrtID->variant for all active cards."""
        by_barcode: dict[str, ProductVariant] = {}
        by_chrt: dict[int, ProductVariant] = {}
        cursor: dict = {"limit": 100}

        while True:
            payload = {
                "settings": {
                    "sort": {"ascending": True},
                    "filter": {"withPhoto": -1},
                    "cursor": cursor,
                }
            }
            data = await self._request("POST", f"{CONTENT_BASE}/content/v2/get/cards/list", json=payload)
            cards = (data or {}).get("cards", [])
            for card in cards:
                nm_id = int(card.get("nmID") or 0)
                for size in card.get("sizes", []) or []:
                    chrt_id = int(size.get("chrtID") or 0)
                    skus = tuple(str(x).strip() for x in (size.get("skus") or []) if str(x).strip())
                    if not chrt_id or not skus:
                        continue
                    variant = ProductVariant(self.cabinet.key, chrt_id, nm_id, skus)
                    by_chrt[chrt_id] = variant
                    for sku in skus:
                        by_barcode[sku] = variant

            cur = (data or {}).get("cursor") or {}
            total = int(cur.get("total") or 0)
            if not cards or total < 100:
                break
            updated_at = cur.get("updatedAt")
            nm_id = cur.get("nmID")
            if not updated_at or not nm_id:
                break
            cursor = {"limit": 100, "updatedAt": updated_at, "nmID": nm_id}

        return by_barcode, by_chrt


def count_orders_by_variant_and_warehouse(
    orders: Iterable[dict],
    valid_warehouse_ids: set[int],
) -> dict[tuple[int, int], int]:
    """(chrtID, warehouseID) -> number of assembly orders."""
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for order in orders:
        try:
            warehouse_id = int(order.get("warehouseId") or 0)
            chrt_id = int(order.get("chrtId") or 0)
        except (TypeError, ValueError):
            continue
        if warehouse_id in valid_warehouse_ids and chrt_id:
            counts[(chrt_id, warehouse_id)] += 1
    return dict(counts)
