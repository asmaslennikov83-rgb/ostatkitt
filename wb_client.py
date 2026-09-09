\
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

import aiohttp

CONTENT_API = "https://content-api.wildberries.ru"
MARKETPLACE_API = "https://marketplace-api.wildberries.ru"
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"


class WBApiError(RuntimeError):
    pass


@dataclass
class Product:
    nm_id: int
    vendor_code: str
    title: str
    chrt_ids: list[int]
    barcodes: list[str]


@dataclass
class StockRow:
    nm_id: int
    vendor_code: str
    title: str
    barcodes: str
    physical_fbo: int
    real_fbo: int
    hidden_fbo: int
    fbs: int
    total_physical: int


def chunks(seq: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _find_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "stocks", "items", "rows", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _find_list(value)
            if nested:
                return nested
    for value in payload.values():
        if isinstance(value, (dict, list)):
            nested = _find_list(value)
            if nested:
                return nested
    return []


def _int_field(row: dict, *names: str, default: int = 0) -> int:
    for name in names:
        if name in row and row[name] is not None:
            try:
                return int(row[name])
            except (TypeError, ValueError):
                pass
    return default


class WBClient:
    def __init__(self, token: str, timeout_seconds: int = 60):
        self.token = token.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WB-Real-Stock-Server/3.0",
        }

    async def _json(self, session, method, url, **kwargs):
        async with session.request(
            method, url, headers=self.headers, **kwargs
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise WBApiError(f"WB HTTP {resp.status}: {text[:800]}")
            if not text:
                return {}
            return await resp.json(content_type=None)

    async def get_all_products(self, session) -> list[Product]:
        result = []
        cursor = None

        while True:
            settings = {
                "cursor": {"limit": 100},
                "filter": {"withPhoto": -1},
            }
            if cursor:
                settings["cursor"].update(cursor)

            payload = await self._json(
                session,
                "POST",
                f"{CONTENT_API}/content/v2/get/cards/list",
                json={"settings": settings},
            )
            cards = payload.get("cards") or payload.get("data", {}).get("cards") or []

            for card in cards:
                nm_id = _int_field(card, "nmID", "nmId", "id")
                if not nm_id:
                    continue

                chrts, barcodes = [], []
                for size in card.get("sizes", []) or []:
                    chrt = _int_field(size, "chrtID", "chrtId", "optionId")
                    if chrt:
                        chrts.append(chrt)
                    for sku in size.get("skus", []) or []:
                        if sku is not None:
                            barcodes.append(str(sku))

                result.append(Product(
                    nm_id=nm_id,
                    vendor_code=str(card.get("vendorCode") or card.get("supplierArticle") or ""),
                    title=str(card.get("title") or ""),
                    chrt_ids=sorted(set(chrts)),
                    barcodes=sorted(set(barcodes)),
                ))

            cur = payload.get("cursor") or payload.get("data", {}).get("cursor") or {}
            total = int(cur.get("total", len(cards)) or 0)
            if len(cards) < 100 or total < 100:
                break

            updated = cur.get("updatedAt")
            nm_cursor = cur.get("nmID") or cur.get("nmId")
            if not updated or not nm_cursor:
                break

            cursor = {"updatedAt": updated, "nmID": nm_cursor}

        return list({p.nm_id: p for p in result}.values())

    async def get_fbs_by_chrt(self, session, products: list[Product]) -> dict[int, int]:
        all_chrt = sorted({c for p in products for c in p.chrt_ids})
        if not all_chrt:
            return {}

        wh_payload = await self._json(
            session, "GET", f"{MARKETPLACE_API}/api/v3/warehouses"
        )
        warehouses = wh_payload if isinstance(wh_payload, list) else _find_list(wh_payload)

        result = defaultdict(int)
        for wh in warehouses:
            wid = _int_field(wh, "id", "warehouseId")
            if not wid:
                continue
            for batch in chunks(all_chrt, 1000):
                payload = await self._json(
                    session,
                    "POST",
                    f"{MARKETPLACE_API}/api/v3/stocks/{wid}",
                    json={"chrtIds": batch},
                )
                for row in payload.get("stocks", []) if isinstance(payload, dict) else []:
                    chrt = _int_field(row, "chrtId", "chrtID")
                    qty = _int_field(row, "amount", "quantity", "qty")
                    if chrt:
                        result[chrt] += max(qty, 0)
                await asyncio.sleep(0.05)

        return dict(result)

    async def get_physical_fbo(self, session, products: list[Product]) -> dict[int, int]:
        body = {
            "nmIds": [],
            "chrtIds": [],
            "limit": 250000,
            "offset": 0,
        }
        try:
            payload = await self._json(
                session,
                "POST",
                f"{ANALYTICS_API}/api/analytics/v1/stocks-report/wb-warehouses",
                json=body,
            )
            rows = _find_list(payload)
        except WBApiError as exc:
            if "400" not in str(exc):
                raise
            rows = []
            nm_ids = [p.nm_id for p in products]
            for i, batch in enumerate(chunks(nm_ids, 1000)):
                payload = await self._json(
                    session,
                    "POST",
                    f"{ANALYTICS_API}/api/analytics/v1/stocks-report/wb-warehouses",
                    json={
                        "nmIds": batch,
                        "chrtIds": [],
                        "limit": 250000,
                        "offset": 0,
                    },
                )
                rows.extend(_find_list(payload))
                if i < (len(nm_ids) - 1) // 1000:
                    await asyncio.sleep(20.2)

        by_nm = defaultdict(int)
        for row in rows:
            wh = str(
                row.get("warehouseName")
                or row.get("warehouse")
                or row.get("officeName")
                or ""
            ).lower()
            if "в пути" in wh or "to client" in wh or "от клиент" in wh or "from client" in wh:
                continue

            nm = _int_field(row, "nmID", "nmId", "nm_id")
            qty = _int_field(
                row, "quantity", "qty", "stock", "amount", "quantityWarehouses"
            )
            if nm:
                by_nm[nm] += max(qty, 0)

        return dict(by_nm)

    async def base_data(self) -> tuple[list[Product], dict[int, int], dict[int, int]]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            products = await self.get_all_products(session)
            if not products:
                return [], {}, {}

            fbs_task = asyncio.create_task(self.get_fbs_by_chrt(session, products))
            physical = await self.get_physical_fbo(session, products)
            fbs = await fbs_task
            return products, physical, fbs
