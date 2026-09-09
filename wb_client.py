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
CARD_API = "https://card.wb.ru"


class WBApiError(RuntimeError):
    pass


class WBStorefrontForbidden(WBApiError):
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
    def __init__(
        self,
        token: str,
        *,
        dest: str,
        currency: str = "rub",
        app_type: int = 1,
        spp: int = 30,
        timeout_seconds: int = 45,
        storefront_proxy: str = "",
    ) -> None:
        self.token = token.strip()
        self.dest = str(dest)
        self.currency = currency
        self.app_type = int(app_type)
        self.spp = int(spp)
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.storefront_proxy = storefront_proxy.strip() or None

    @property
    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WB-Real-Stock-Bot/2.0",
        }

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        auth: bool = True,
        storefront: bool = False,
        **kwargs: Any,
    ) -> Any:
        headers = kwargs.pop("headers", {})

        if auth:
            headers = {**self.auth_headers, **headers}
        else:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": "https://www.wildberries.ru/",
                "Origin": "https://www.wildberries.ru",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0.0.0 Safari/537.36"
                ),
                **headers,
            }

        if storefront and self.storefront_proxy:
            kwargs["proxy"] = self.storefront_proxy

        async with session.request(method, url, headers=headers, **kwargs) as resp:
            text = await resp.text()

            if resp.status == 403 and storefront:
                raise WBStorefrontForbidden(
                    "Покупательская витрина Wildberries вернула 403 Forbidden. "
                    "Seller API продолжает работать, но IP сервера заблокирован "
                    "для запросов к card.wb.ru. Укажите WB_STOREFRONT_PROXY "
                    "в .env или перенесите бота на сервер/IP, с которого витрина WB доступна."
                )

            if resp.status >= 400:
                raise WBApiError(f"WB HTTP {resp.status}: {text[:800]}")

            if not text:
                return {}

            try:
                return await resp.json(content_type=None)
            except Exception as exc:
                raise WBApiError(
                    f"WB вернул не JSON для {url}: {text[:500]}"
                ) from exc

    async def get_all_products(self, session: aiohttp.ClientSession) -> list[Product]:
        result: list[Product] = []
        cursor: dict[str, Any] | None = None

        while True:
            settings: dict[str, Any] = {
                "cursor": {"limit": 100},
                "filter": {"withPhoto": -1},
            }
            if cursor:
                settings["cursor"].update(cursor)

            payload = await self._request_json(
                session,
                "POST",
                f"{CONTENT_API}/content/v2/get/cards/list",
                json={"settings": settings},
            )

            data = payload.get("cards") or payload.get("data", {}).get("cards") or []

            for card in data:
                nm_id = _int_field(card, "nmID", "nmId", "id")
                if not nm_id:
                    continue

                chrt_ids: list[int] = []
                barcodes: list[str] = []

                for size in card.get("sizes", []) or []:
                    chrt = _int_field(size, "chrtID", "chrtId", "optionId")
                    if chrt:
                        chrt_ids.append(chrt)

                    for sku in size.get("skus", []) or []:
                        if sku is not None:
                            barcodes.append(str(sku))

                result.append(
                    Product(
                        nm_id=nm_id,
                        vendor_code=str(
                            card.get("vendorCode")
                            or card.get("supplierArticle")
                            or ""
                        ),
                        title=str(card.get("title") or ""),
                        chrt_ids=sorted(set(chrt_ids)),
                        barcodes=sorted(set(barcodes)),
                    )
                )

            cursor_data = payload.get("cursor") or payload.get("data", {}).get("cursor") or {}
            total = int(cursor_data.get("total", len(data)) or 0)

            if len(data) < 100 or total < 100:
                break

            updated_at = cursor_data.get("updatedAt")
            nm_id_cursor = cursor_data.get("nmID") or cursor_data.get("nmId")

            if not updated_at or not nm_id_cursor:
                break

            cursor = {
                "updatedAt": updated_at,
                "nmID": nm_id_cursor,
            }

        unique: dict[int, Product] = {}
        for product in result:
            unique[product.nm_id] = product

        return list(unique.values())

    async def get_seller_warehouses(
        self,
        session: aiohttp.ClientSession,
    ) -> list[dict]:
        payload = await self._request_json(
            session,
            "GET",
            f"{MARKETPLACE_API}/api/v3/warehouses",
        )

        if isinstance(payload, list):
            return payload

        return _find_list(payload)

    async def get_fbs_by_chrt(
        self,
        session: aiohttp.ClientSession,
        products: list[Product],
    ) -> dict[int, int]:
        all_chrt = sorted(
            {chrt for product in products for chrt in product.chrt_ids}
        )

        if not all_chrt:
            return {}

        warehouses = await self.get_seller_warehouses(session)
        result: dict[int, int] = defaultdict(int)

        for wh in warehouses:
            warehouse_id = _int_field(wh, "id", "warehouseId")
            if not warehouse_id:
                continue

            for batch in chunks(all_chrt, 1000):
                payload = await self._request_json(
                    session,
                    "POST",
                    f"{MARKETPLACE_API}/api/v3/stocks/{warehouse_id}",
                    json={"chrtIds": batch},
                )

                rows = payload.get("stocks", []) if isinstance(payload, dict) else []

                for row in rows:
                    chrt = _int_field(row, "chrtId", "chrtID")
                    amount = _int_field(row, "amount", "quantity", "qty")
                    if chrt:
                        result[chrt] += max(amount, 0)

                await asyncio.sleep(0.05)

        return dict(result)

    async def _get_physical_fbo_all(
        self,
        session: aiohttp.ClientSession,
    ) -> list[dict]:
        limit = 250_000
        offset = 0
        all_rows: list[dict] = []

        while True:
            body = {
                "nmIds": [],
                "chrtIds": [],
                "limit": limit,
                "offset": offset,
            }

            payload = await self._request_json(
                session,
                "POST",
                f"{ANALYTICS_API}/api/analytics/v1/stocks-report/wb-warehouses",
                json=body,
            )

            rows = _find_list(payload)
            all_rows.extend(rows)

            if len(rows) < limit:
                break

            offset += len(rows)
            await asyncio.sleep(20.2)

        return all_rows

    async def _get_physical_fbo_by_products(
        self,
        session: aiohttp.ClientSession,
        products: list[Product],
    ) -> list[dict]:
        all_rows: list[dict] = []
        nm_ids = [p.nm_id for p in products]

        for index, batch in enumerate(chunks(nm_ids, 1000)):
            body = {
                "nmIds": batch,
                "chrtIds": [],
                "limit": 250_000,
                "offset": 0,
            }

            payload = await self._request_json(
                session,
                "POST",
                f"{ANALYTICS_API}/api/analytics/v1/stocks-report/wb-warehouses",
                json=body,
            )

            all_rows.extend(_find_list(payload))

            if index < (len(nm_ids) - 1) // 1000:
                await asyncio.sleep(20.2)

        return all_rows

    async def get_physical_fbo(
        self,
        session: aiohttp.ClientSession,
        products: list[Product],
    ) -> tuple[dict[int, int], dict[int, int]]:
        try:
            rows = await self._get_physical_fbo_all(session)
        except WBApiError as exc:
            if "400" not in str(exc):
                raise
            rows = await self._get_physical_fbo_by_products(session, products)

        by_nm: dict[int, int] = defaultdict(int)
        by_chrt: dict[int, int] = defaultdict(int)

        for row in rows:
            warehouse_name = str(
                row.get("warehouseName")
                or row.get("warehouse")
                or row.get("officeName")
                or ""
            ).lower()

            if "в пути" in warehouse_name or "to client" in warehouse_name:
                continue
            if "от клиент" in warehouse_name or "from client" in warehouse_name:
                continue

            nm_id = _int_field(row, "nmID", "nmId", "nm_id")
            chrt_id = _int_field(row, "chrtID", "chrtId", "chrt_id")
            qty = _int_field(
                row,
                "quantity",
                "qty",
                "stock",
                "amount",
                "quantityWarehouses",
                default=0,
            )
            qty = max(qty, 0)

            if nm_id:
                by_nm[nm_id] += qty
            if chrt_id:
                by_chrt[chrt_id] += qty

        return dict(by_nm), dict(by_chrt)

    async def get_storefront_real_fbo(
        self,
        session: aiohttp.ClientSession,
        nm_ids: list[int],
    ) -> dict[int, int]:
        result: dict[int, int] = defaultdict(int)

        for batch in chunks(nm_ids, 50):
            params = {
                "appType": self.app_type,
                "curr": self.currency,
                "dest": self.dest,
                "spp": self.spp,
                "nm": ";".join(str(x) for x in batch),
            }

            payload = await self._request_json(
                session,
                "GET",
                f"{CARD_API}/cards/v4/detail",
                auth=False,
                storefront=True,
                params=params,
            )

            products = (
                payload.get("products")
                or payload.get("data", {}).get("products")
                or []
            )

            for product in products:
                nm_id = _int_field(product, "id", "nmId", "nmID")
                if not nm_id:
                    continue

                fbo_qty = 0

                for size in product.get("sizes", []) or []:
                    for stock in size.get("stocks", []) or []:
                        dtype = _int_field(stock, "dtype")
                        qty = _int_field(stock, "qty", "quantity", "amount")

                        if dtype == 4:
                            fbo_qty += max(qty, 0)

                result[nm_id] = fbo_qty

            await asyncio.sleep(0.08)

        return {
            nm_id: int(result.get(nm_id, 0))
            for nm_id in nm_ids
        }

    async def build_stock_rows(self) -> list[StockRow]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            products = await self.get_all_products(session)

            if not products:
                return []

            fbs_task = asyncio.create_task(
                self.get_fbs_by_chrt(session, products)
            )

            storefront_task = asyncio.create_task(
                self.get_storefront_real_fbo(
                    session,
                    [p.nm_id for p in products],
                )
            )

            physical_by_nm, _ = await self.get_physical_fbo(
                session,
                products,
            )

            fbs_by_chrt = await fbs_task
            storefront_fbo = await storefront_task

            rows: list[StockRow] = []

            for p in products:
                fbs = sum(
                    fbs_by_chrt.get(chrt, 0)
                    for chrt in p.chrt_ids
                )

                physical_fbo = int(
                    physical_by_nm.get(p.nm_id, 0)
                )

                real_fbo = int(
                    storefront_fbo.get(p.nm_id, 0)
                )

                hidden = max(
                    physical_fbo - real_fbo,
                    0,
                )

                total = physical_fbo + fbs

                rows.append(
                    StockRow(
                        nm_id=p.nm_id,
                        vendor_code=p.vendor_code,
                        title=p.title,
                        barcodes=", ".join(p.barcodes),
                        physical_fbo=physical_fbo,
                        real_fbo=real_fbo,
                        hidden_fbo=hidden,
                        fbs=fbs,
                        total_physical=total,
                    )
                )

            rows.sort(key=lambda r: r.nm_id)
            return rows

    async def check_nm_ids(
        self,
        nm_ids: list[int],
    ) -> dict[int, int]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            return await self.get_storefront_real_fbo(
                session,
                nm_ids,
            )
