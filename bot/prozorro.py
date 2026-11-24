"""Клиент публичного API Prozorro: лента изменений и детали тендера."""
import asyncio
import logging

import httpx

API = "https://public.api.openprocurement.org/api/2.5/tenders"
FEED_FIELDS = "tenderID,status,procurementMethodType,dateModified"
UA = "prozorro-tg-bot/1.0 (tender alerts; public API reader)"
log = logging.getLogger("prozorro")


class Api:
    def __init__(self, on_error=None):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), headers={"User-Agent": UA},
                                      limits=httpx.Limits(max_connections=20))
        self.on_error = on_error or (lambda text: None)

    async def get(self, url, params=None):
        for attempt in range(6):
            try:
                r = await self.http.get(url, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status and 400 <= status < 500 and status != 429:
                    raise  # 404 и т.п. — повтор не поможет
                text = f"{type(e).__name__}: {e} ({url})"
                log.warning("%s, попытка %d", text, attempt + 1)  # одной строкой, без трейсбека
                self.on_error(text)
                await asyncio.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"API недоступно: {url}")

    async def feed(self, offset=None, descending=False, limit=1000):
        params = {"feed": "changes", "limit": limit, "opt_fields": FEED_FIELDS}
        if offset:
            params["offset"] = offset
        if descending:
            params["descending"] = 1
        return await self.get(API, params)

    async def head_offset(self):
        """Офсет текущего конца ленты: prev_page первой страницы descending разворачивает направление вперёд."""
        page = await self.feed(descending=True, limit=1)
        return page["prev_page"]["offset"]

    async def tender(self, tid):
        return (await self.get(f"{API}/{tid}"))["data"]

    async def aclose(self):
        await self.http.aclose()
