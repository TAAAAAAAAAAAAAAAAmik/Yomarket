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
        """Not available: the Integration API v1.4 has no bump endpoint.

        The spec lists only publish / unpublish / price / items / value for an
        ad — nothing that raises it in the listing. Previous code guessed
        /up, /bump and /raise, which simply 404.
        """
        raise RuntimeError(
            "Интеграционное API не поддерживает поднятие объявлений — "
            "такого метода нет в спецификации v1.4"
        )

    async def bump_all_ads(self) -> tuple[int, str]:
        """Not available — see bump_ad(). Fails fast instead of walking every
        ad only to 404 on each one."""
        return 0, ("⚠️ Поднятие недоступно: в Интеграционном API нет такого "
                   "метода. Поднимайте объявления в панели.")

    async def restore_ad(self, ad_id: int | str) -> dict:
        """Put an ad back on sale — POST /ads/{ad_id}/publish (per the spec)."""
        return await self._post(f"/ads/{ad_id}/publish")

    async def unpublish_ad(self, ad_id: int | str) -> dict:
        """Take an ad off sale — POST /ads/{ad_id}/unpublish."""
        return await self._post(f"/ads/{ad_id}/unpublish")

    async def restore_all_ads(self) -> tuple[int, str]:
        """Restore all inactive/sold ads. Returns (count, message)."""
        data = await self.get_ads()
        ads = data.get("data") or data.get("items") or []
        _DEAD = ("inactive", "sold", "expired", "archived", "disabled",
                 "closed", "hidden", "not_active", "paused", "stopped")

        def _is_dead(ad: dict) -> bool:
            raw = ad.get("status", ad.get("state", ad.get("is_active")))
            if isinstance(raw, bool):
                return not raw          # is_active=False means it needs restoring
            if isinstance(raw, (int, float)):
                return raw == 0
            return str(raw).lower() in _DEAD

        inactive = [ad for ad in ads if _is_dead(ad)]
        if not inactive:
            # Report the statuses actually seen: the list above is a guess, and
            # a silent "nothing to do" would hide an unrecognised value.
            seen = sorted({
                str(ad.get("status", ad.get("state", ad.get("is_active", "?"))))
                for ad in ads
            })
            logger.info("restore_all_ads: no dead ads; statuses seen: %s", seen)
            return 0, (f"ℹ️ Нет товаров для восстановления "
                       f"(статусы объявлений: {', '.join(seen[:8])})")
        count = 0
        last_err = ""
        for ad in inactive:
            ad_id = ad.get("id")
            if not ad_id:
                continue
            try:
                await self.restore_ad(ad_id)
                count += 1
            except RuntimeError as e:
                last_err = str(e)
        if count:
            return count, f"✅ Восстановлено: {count}"
        return 0, f"⚠️ API не поддерживает восстановление ({last_err})"

    async def get_balance(self) -> tuple[float, str]:
        """Get current balance. Returns (amount_float, formatted_string)."""
        # Primary: /check (same endpoint used by balance handler)
        try:
            data = await self._get("/check")
            shop = data.get("data") or data.get("shop") or data.get("seller") or data
            raw = None
            for src in (shop, data):
                if not isinstance(src, dict):
                    continue
                for key in ("balance", "wallet", "money", "balance_rub", "amount"):
                    # `in` rather than a truthiness chain: a balance of 0 is a
                    # real value, not a missing one.
                    if key in src and src[key] not in (None, ""):
                        raw = src[key]
                        break
                if raw is not None:
                    break
            if raw is not None:
                try:
                    amount = float(str(raw).replace(" ", "").replace(",", "."))
                    return amount, f"{amount:.0f} ₽"
                except (ValueError, TypeError):
                    pass
        except RuntimeError:
            pass

        # No dedicated balance endpoint exists: the spec lists /check as the
        # only place shop data is returned. Report the shape we got instead of
        # probing paths that are guaranteed to 404.
        logger.warning("Balance not found in /check response")
        return 0.0, "—"

    async def withdraw_balance(self, min_amount: float = 0, amount: float | None = None) -> tuple[bool, str]:
        """Request a withdrawal. If `amount` is None, withdraws the full balance.
        Returns (success, message)."""
        balance, balance_str = await self.get_balance()
        if balance_str == "—":
            return False, "❌ Не удалось получить баланс через API"
        want = balance if amount is None else float(amount)
        if want <= 0:
            return False, "ℹ️ Нечего выводить (баланс 0)"
        if want > balance:
            return False, f"ℹ️ Запрошено {want:.0f} ₽, а на балансе {balance:.0f} ₽"
        if balance < min_amount:
            return False, f"ℹ️ Баланс {balance:.0f} ₽ ниже порога {min_amount:.0f} ₽"
        for path in ("/withdraw", "/wallet/withdraw", "/balance/withdraw", "/finance/withdraw"):
            try:
                await self._post(path, json={"amount": int(want)})
                return True, f"✅ Вывод {want:.0f} ₽ выполнен"
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                return False, f"❌ Ошибка: {e}"
        return False, "⚠️ API не поддерживает вывод средств"

    async def get_withdrawals(self) -> list[dict]:
        """Fetch withdrawal list from the API (tries common endpoints).
        Returns [] if unsupported."""
        for path in ("/withdrawals", "/withdraw/history", "/wallet/withdrawals",
                     "/finance/withdrawals", "/balance/withdrawals"):
            try:
                data = await self._get(path)
                return data.get("data") or data.get("items") or []
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower() or "405" in str(e):
                    continue
                raise
        return []

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
                        quantity: int = 1, category: str = "",
                        **extra) -> dict:
        """Create a listing — POST /ads (available since API v1.4.0).

        Ads can only be created in leaf categories (is_leaf: true); images are
        uploaded separately via POST /media and attached by media_id.
        """
        payload: dict = {
            "title": title,
            "price": price,
            "description": description,
            "stock": quantity,
        }
        if category:
            payload["category_id"] = category
        payload.update(extra)
        return await self._post("/ads", json=payload)

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

    async def update_price(self, ad_id: int | str, price: int,
                           discount: int | None = None) -> dict:
        """Update price (and optional discount) — PATCH /ads/{ad_id}/price.

        There is no PATCH /ads/{ad_id} in the spec; the old update_ad() posted
        to that non-existent path, so every price change failed.
        """
        payload: dict = {"price": price}
        if discount is not None:
            payload["discount"] = discount
        return await self._patch(f"/ads/{ad_id}/price", json=payload)

    async def bulk_change_prices(self, percent: float) -> tuple[int, str]:
        """Change all ad prices by percent (+/-). Returns (count, message)."""
        data = await self.get_ads()
        ads = data.get("data") or data.get("items") or []
        if not ads:
            return 0, "ℹ️ Нет объявлений"
        count = 0
        last_err = ""
        for ad in ads:
            ad_id = ad.get("id")
            try:
                current_price = int(float(str(ad.get("price", 0))))
            except (TypeError, ValueError):
                continue
            if not ad_id or current_price <= 0:
                continue
            new_price = max(1, round(current_price * (1 + percent / 100)))
            try:
                await self.update_price(ad_id, new_price)
                count += 1
            except RuntimeError as e:
                last_err = str(e)
        sign = "+" if percent >= 0 else ""
        if count:
            return count, f"✅ Обновлено {count} товаров ({sign}{percent:.0f}%)"
        return 0, f"⚠️ Не удалось обновить цены ({last_err})"

    async def get_reviews(self) -> dict:
        """Fetch reviews/feedback. Tries common endpoint names."""
        for path in ("/reviews", "/feedback", "/ratings"):
            try:
                return await self._get(path)
            except RuntimeError as e:
                if "404" in str(e) or "not found" in str(e).lower() or "405" in str(e):
                    continue
                raise
        return {"data": []}
