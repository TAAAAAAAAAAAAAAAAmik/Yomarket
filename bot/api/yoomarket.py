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
        # Deliberately excludes "moderate" and "draft": those are ads on their
        # way to being published, not ones taken down.
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

    async def create_and_publish(
        self,
        title: str,
        price: int,
        description: str,
        category_id: int | str,
        ad_type: str = "simple",
        stock: int = 1,
        items: list[str] | None = None,
        value: dict | None = None,
        photos: list[tuple[bytes, str]] | None = None,
        publish: bool = True,
        **extra,
    ) -> tuple[str, str]:
        """Create a listing, fill its stock, then put it on sale.

        Stock has to exist before publishing, and how it is supplied depends on
        the ad type:
          auto-delivery -> `items`  (the texts buyers receive)
          auto-value    -> `value`  (stock/min/max/step/label_id)
          simple/unlimited -> the `stock` field on the ad itself

        Photos are uploaded to the media buffer first: publishing an ad without
        images is rejected with `empty_images`.
        Returns (ad_id, human_message).
        """
        media_ids: list[str] = []
        for content, filename in (photos or []):
            media_ids.append(await self.upload_media(content, filename))

        payload: dict = {
            "title": title,
            "price": price,
            "description": description,
            "category_id": category_id,
            "type": ad_type,
        }
        if ad_type in ("simple", "auto-value"):
            payload["stock"] = stock
        if media_ids:
            payload["media_ids"] = media_ids
        payload.update(extra)

        created = await self._post("/ads", json=payload)
        inner = created.get("data") or created
        ad_id = inner.get("id") or inner.get("ad_id")
        if not ad_id:
            raise RuntimeError(f"Товар создан, но в ответе нет id: {str(created)[:200]}")
        ad_id = str(ad_id)

        steps = ["создан"]
        if ad_type == "auto-delivery" and items:
            await self.add_ad_items(ad_id, items)
            steps.append(f"позиций: {len(items)}")
        elif ad_type == "auto-value" and value:
            await self.update_ad_value(ad_id, **value)
            steps.append("параметры авто-выбора заданы")

        if publish:
            try:
                await self.publish_ad(ad_id)
                steps.append("опубликован")
            except RuntimeError as e:
                # Keep the ad — it exists and can be published once fixed.
                return ad_id, (f"⚠️ Товар создан (#{ad_id}), но не опубликован: "
                               f"{str(e)[:200]}")
        return ad_id, "✅ " + ", ".join(steps)

    # ------------------------------------------------------------------
    # Stock — must be filled BEFORE publishing, per ad type
    # ------------------------------------------------------------------

    async def get_ad_items(self, ad_id: int | str, cursor: str | None = None) -> dict:
        """Auto-delivery positions of an ad (available / sold)."""
        params = {"cursor": cursor} if cursor else None
        return await self._get(f"/ads/{ad_id}/items", params=params)

    async def add_ad_items(self, ad_id: int | str, items: list[str]) -> dict:
        """Add auto-delivery positions — POST /ads/{ad_id}/items.

        The API accepts up to 50 per request, so longer lists are sent in
        batches. `items` are the texts handed to the buyer (keys, codes).
        """
        if not items:
            raise RuntimeError("Список позиций пуст")
        last: dict = {}
        for i in range(0, len(items), 50):
            batch = [str(x) for x in items[i:i + 50] if str(x).strip()]
            if batch:
                last = await self._post(f"/ads/{ad_id}/items",
                                        json={"items": batch})
        return last

    async def delete_ad_item(self, ad_id: int | str, item_id: int | str) -> dict:
        """Remove one unsold position."""
        assert self.session is not None, "Call start() first"
        url = f"{self.base_url}/ads/{ad_id}/items/{item_id}"
        async with self.session.delete(url) as resp:
            text = await resp.text()
            logger.debug("DELETE %s → %s: %s", url, resp.status, text[:300])
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {}

    async def get_ad_value(self, ad_id: int | str) -> dict:
        """Auto-value parameters: stock, min/max, step, unit."""
        return await self._get(f"/ads/{ad_id}/value")

    async def update_ad_value(self, ad_id: int | str, **fields) -> dict:
        """Set auto-value parameters (stock, min, max, step, label_id)."""
        return await self._patch(f"/ads/{ad_id}/value", json=fields)

    async def refill_ad_value(self, ad_id: int | str, amount: float) -> dict:
        """Add to (or, with a negative amount, take from) the auto-value stock."""
        return await self._post(f"/ads/{ad_id}/value/refill",
                                json={"amount": amount})

    async def get_value_labels(self) -> list[dict]:
        """Units of measure available for auto-value ads."""
        data = await self._get("/values/labels")
        return data.get("data") or data.get("items") or []

    async def upload_media(self, content: bytes, filename: str = "photo.jpg",
                           content_type: str = "image/jpeg") -> str:
        """Upload one image to the media buffer — POST /media → media_id.

        Each media_id is single-use and expires in 24h if never attached.
        """
        assert self.session is not None, "Call start() first"
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename,
                       content_type=content_type)
        url = f"{self.base_url}/media"
        async with self.session.post(url, data=form) as resp:
            text = await resp.text()
            logger.debug("POST /media → %s: %s", resp.status, text[:300])
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json(content_type=None)
        inner = data.get("data") or data
        media_id = inner.get("media_id") or inner.get("id")
        if not media_id:
            raise RuntimeError(f"В ответе нет media_id: {str(data)[:150]}")
        return str(media_id)

    async def publish_ad(self, ad_id: int | str) -> dict:
        """Put an ad on sale. Fails with `empty_images` if it has no photos."""
        return await self._post(f"/ads/{ad_id}/publish")

    async def get_category_filters(self, category_id: int | str) -> list[dict]:
        """Product parameters a category expects (with `required` flags)."""
        data = await self._get(f"/categories/{category_id}/filters")
        return data.get("data") or data.get("items") or []

    async def categories_raw(self) -> dict:
        """First page of /categories untouched — for diagnosing its shape."""
        return await self._get("/categories")

    async def find_categories(self, wanted: set[int],
                              max_requests: int = 120) -> dict[int, str]:
        """Names for specific category ids, walking the tree only as far as
        needed.

        /categories returns the top of a tree; products sit in its leaves, so a
        leaf id is never in that first response. This walks level by level and
        stops the moment every wanted id is found, rather than mapping the
        whole catalogue.
        """
        found: dict[int, str] = {}
        if not wanted:
            return found

        frontier: list[int | None] = [None]      # None = the root level
        visited: set[int | None] = set()
        requests_made = 0

        while frontier and requests_made < max_requests:
            parent = frontier.pop(0)
            if parent in visited:
                continue
            visited.add(parent)
            try:
                rows = await self.get_categories(max_pages=5, parent_id=parent)
            except RuntimeError as e:
                logger.info("categories(parent=%s): %s", parent, e)
                continue
            requests_made += 1

            for row in rows:
                cid = row.get("id")
                if cid is None:
                    continue
                try:
                    cid = int(cid)
                except (TypeError, ValueError):
                    continue
                label = row.get("name") or row.get("title")
                if cid in wanted and label:
                    found[cid] = str(label)
                    if len(found) == len(wanted):
                        logger.info("categories found in %d requests",
                                    requests_made)
                        return found
                # Only branches can contain the leaves we are after
                if not row.get("is_leaf") and cid not in visited:
                    frontier.append(cid)

        logger.info("categories: %d/%d found in %d requests",
                    len(found), len(wanted), requests_made)
        return found

    async def resolve_category(self, category_id: int | str) -> str:
        """Name of one category without walking the whole tree.

        The flat reference returns only the top level, so a leaf id like 5221
        is absent from it. The filters endpoint is scoped to a single category
        and commonly echoes it back.
        """
        try:
            data = await self._get(f"/categories/{category_id}/filters")
        except RuntimeError as e:
            logger.info("resolve_category(%s): %s", category_id, e)
            return ""
        for block in (data.get("category"), data.get("meta"), data.get("data"), data):
            if isinstance(block, dict):
                label = block.get("name") or block.get("title")
                if label:
                    return str(label)
        return ""

    async def get_categories(self, max_pages: int = 40,
                             parent_id: int | str | None = None) -> list[dict]:
        """Full category reference — GET /categories, following the cursor.

        Lists use cursor pagination, and a marketplace this size has thousands
        of categories: reading only the first page left deep ids (like 5221)
        unresolved, so they showed as «Категория #5221».
        """
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            if parent_id is not None:
                params["parent_id"] = str(parent_id)
            data = await self._get("/categories", params=params or None)
            rows = data.get("data") or data.get("items") or []
            out.extend(r for r in rows if isinstance(r, dict))
            meta = data.get("meta") or {}
            cursor = (meta.get("next_cursor") or meta.get("next")
                      or (data.get("links") or {}).get("next"))
            if not cursor or not rows:
                break
        logger.info("categories loaded: %d (parent=%s)", len(out), parent_id)
        return out

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
            # GET /ads returns price nested:
            # {"amount": 149, "base_amount": 149, "currency": "RUB"}
            raw = ad.get("price")
            if isinstance(raw, dict):
                raw = raw.get("amount", raw.get("base_amount", 0))
            try:
                current_price = int(float(str(raw or 0)))
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
