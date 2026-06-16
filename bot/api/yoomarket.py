from __future__ import annotations

import logging
import aiohttp

logger = logging.getLogger(__name__)


class YooMarketAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = "https://api.yoo.market/integration/v1"
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False)
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"},
            connector=connector,
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        async with self.session.get(f"{self.base_url}{path}", params=params) as resp:
            text = await resp.text()
            logger.debug("GET %s → %s: %s", path, resp.status, text[:500])
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            if not resp.ok:
                raise RuntimeError(data.get("message") or data.get("error") or f"HTTP {resp.status}")
            return data

    async def _post(self, path: str, json: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        async with self.session.post(f"{self.base_url}{path}", json=json or {}) as resp:
            text = await resp.text()
            logger.debug("POST %s → %s: %s", path, resp.status, text[:500])
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            if not resp.ok:
                raise RuntimeError(data.get("message") or data.get("error") or f"HTTP {resp.status}")
            return data

    async def check(self) -> dict:
        return await self._get("/check")

    async def get_ads(self, cursor: str | None = None) -> dict:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/ads", params=params)

    async def get_ad(self, ad_id: int | str) -> dict:
        return await self._get(f"/ads/{ad_id}")

    async def bump_ad(self, ad_id: int | str) -> dict:
        """Try to bump/raise ad. Tries common endpoint patterns."""
        for path in (f"/ads/{ad_id}/up", f"/ads/{ad_id}/bump", f"/ads/{ad_id}/raise"):
            try:
                return await self._post(path)
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        raise RuntimeError("Поднятие товаров не поддерживается текущей версией API YooMarket")

    async def get_orders(self, cursor: str | None = None) -> dict:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/orders", params=params)

    async def get_order(self, order_id: int | str) -> dict:
        return await self._get(f"/orders/{order_id}")

    async def work_order(self, order_id: int | str) -> dict:
        return await self._post(f"/orders/{order_id}/work")

    async def confirm_order(self, order_id: int | str) -> dict:
        return await self._post(f"/orders/{order_id}/confirm")

    async def refund_order(self, order_id: int | str) -> dict:
        return await self._post(f"/orders/{order_id}/refund")

    async def get_messages(self, chat_id: int | str, cursor: str | None = None) -> dict:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"/chats/{chat_id}/messages", params=params)

    async def send_message(self, chat_id: int | str, text: str) -> dict:
        return await self._post(f"/chats/{chat_id}/sendMessage", json={"text": text})
