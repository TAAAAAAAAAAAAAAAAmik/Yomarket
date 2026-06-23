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

    async def _patch(self, path: str, json: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        async with self.session.patch(f"{self.base_url}{path}", json=json or {}) as resp:
            text = await resp.text()
            logger.debug("PATCH %s → %s: %s", path, resp.status, text[:500])
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
        """Bump a single ad. Tries common endpoint patterns."""
        for path in (f"/ads/{ad_id}/up", f"/ads/{ad_id}/bump", f"/ads/{ad_id}/raise"):
            try:
                return await self._post(path)
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        raise RuntimeError("not supported")

    async def bump_all_ads(self) -> tuple[int, str]:
        """Bump all active ads. Returns (count, message)."""
        data = await self.get_ads()
        ads = data.get("data") or data.get("items") or []
        if not ads:
            return 0, "ℹ️ Нет объявлений"
        count = 0
        last_err = ""
        for ad in ads:
            ad_id = ad.get("id")
            if not ad_id:
                continue
            try:
                await self.bump_ad(ad_id)
                count += 1
            except RuntimeError as e:
                last_err = str(e)
        if count:
            return count, f"✅ Поднято: {count}"
        return 0, f"⚠️ API не поддерживает поднятие ({last_err})"

    async def restore_ad(self, ad_id: int | str) -> dict:
        """Restore / reactivate a single ad."""
        for path in (
            f"/ads/{ad_id}/activate",
            f"/ads/{ad_id}/restore",
            f"/ads/{ad_id}/republish",
            f"/ads/{ad_id}/publish",
        ):
            try:
                return await self._post(path)
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        try:
            return await self._patch(f"/ads/{ad_id}", {"status": "active"})
        except RuntimeError:
            pass
        raise RuntimeError("not supported")

    async def restore_all_ads(self) -> tuple[int, str]:
        """Restore all inactive/sold ads. Returns (count, message)."""
        data = await self.get_ads()
        ads = data.get("data") or data.get("items") or []
        inactive = [
            ad for ad in ads
            if ad.get("status") in ("inactive", "sold", "expired", "archived", "disabled", "closed")
        ]
        if not inactive:
            return 0, "ℹ️ Нет товаров для восстановления"
        count = 0
        last_err = ""
        for ad in inactive:
            try:
                await self.restore_ad(ad["id"])
                count += 1
            except RuntimeError as e:
                last_err = str(e)
        if count:
            return count, f"✅ Восстановлено: {count}"
        return 0, f"⚠️ API не поддерживает восстановление ({last_err})"

    async def get_balance(self) -> tuple[float, str]:
        """Get current balance. Returns (amount_float, formatted_string)."""
        for path in ("/balance", "/wallet", "/finance", "/account/balance", "/account"):
            try:
                data = await self._get(path)
                inner = data.get("data") or data
                if isinstance(inner, dict):
                    for key in ("balance", "amount", "available", "total"):
                        if key in inner:
                            amount = float(inner[key])
                            return amount, f"{amount:.0f} ₽"
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        return 0.0, "—"

    async def withdraw_balance(self, min_amount: float = 0) -> tuple[bool, str]:
        """Request balance withdrawal. Returns (success, message)."""
        balance, balance_str = await self.get_balance()
        if balance_str == "—":
            return False, "❌ Не удалось получить баланс через API"
        if balance < min_amount:
            return False, f"ℹ️ Баланс {balance:.0f} ₽ ниже порога {min_amount:.0f} ₽"
        for path in ("/withdraw", "/wallet/withdraw", "/balance/withdraw", "/finance/withdraw"):
            try:
                await self._post(path, json={"amount": int(balance)})
                return True, f"✅ Вывод {balance:.0f} ₽ выполнен"
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                return False, f"❌ Ошибка: {e}"
        return False, "⚠️ API не поддерживает вывод средств"

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

    async def create_ad(self, title: str, price: int, description: str,
                        quantity: int = 1, category: str = "") -> dict:
        """Create a new product listing."""
        payload: dict = {"title": title, "price": price, "description": description, "quantity": quantity}
        if category:
            payload["category"] = category
        for path in ("/ads", "/products", "/listings"):
            try:
                return await self._post(path, json=payload)
            except RuntimeError as e:
                err = str(e)
                if "404" in err or "not found" in err.lower() or "405" in err or "method" in err.lower():
                    continue
                raise
        raise RuntimeError(
            "YooMarket Integration API не поддерживает создание товаров.\n"
            "Создай товар вручную в панели <b>panel.yoomarket.net</b>"
        )

    async def get_categories(self) -> list[dict]:
        """Fetch available categories."""
        for path in ("/categories", "/ads/categories", "/catalog/categories"):
            try:
                data = await self._get(path)
                return data.get("data") or data.get("items") or []
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        return []
        for path in ("/reviews", "/feedback", "/ratings"):
            try:
                return await self._get(path)
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower() or "405" in str(e):
                    continue
                raise
        return {"data": []}
