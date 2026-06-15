from __future__ import annotations

import aiohttp


class YooMarketAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = "https://api.yoo.market/integration/v1"
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        async with self.session.get(
            f"{self.base_url}{path}", params=params
        ) as resp:
            data = await resp.json()
            if not resp.ok:
                raise RuntimeError(
                    data.get("message") or data.get("error") or f"HTTP {resp.status}"
                )
            return data

    async def _post(self, path: str, json: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        async with self.session.post(
            f"{self.base_url}{path}", json=json or {}
        ) as resp:
            data = await resp.json()
            if not resp.ok:
                raise RuntimeError(
                    data.get("message") or data.get("error") or f"HTTP {resp.status}"
                )
            return data

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def check(self) -> dict:
        """GET /check — shop info (name, balance)."""
        return await self._get("/check")

    async def get_ads(self, cursor: str | None = None) -> dict:
        """GET /ads — list ads with optional cursor pagination."""
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/ads", params=params)

    async def get_ad(self, ad_id: int | str) -> dict:
        """GET /ads/{ad_id} — single ad details."""
        return await self._get(f"/ads/{ad_id}")

    async def get_orders(self, cursor: str | None = None) -> dict:
        """GET /orders — list orders with optional cursor pagination."""
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/orders", params=params)

    async def get_order(self, order_id: int | str) -> dict:
        """GET /orders/{order_id} — single order details."""
        return await self._get(f"/orders/{order_id}")

    async def work_order(self, order_id: int | str) -> dict:
        """POST /orders/{order_id}/work — set order in work."""
        return await self._post(f"/orders/{order_id}/work")

    async def confirm_order(self, order_id: int | str) -> dict:
        """POST /orders/{order_id}/confirm — confirm order."""
        return await self._post(f"/orders/{order_id}/confirm")

    async def refund_order(self, order_id: int | str) -> dict:
        """POST /orders/{order_id}/refund — refund order."""
        return await self._post(f"/orders/{order_id}/refund")

    async def get_messages(
        self, chat_id: int | str, cursor: str | None = None
    ) -> dict:
        """GET /chats/{chat_id}/messages — paginated message list."""
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"/chats/{chat_id}/messages", params=params)

    async def send_message(self, chat_id: int | str, text: str) -> dict:
        """POST /chats/{chat_id}/sendMessage — send a text message."""
        return await self._post(f"/chats/{chat_id}/sendMessage", json={"text": text})
