from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)


class YooMarketAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = "https://api.yoo.market/integration/v1"
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False)
        # По умолчанию aiohttp ждёт пять минут: подвисший запрос оставлял на
        # экране «⏳ Загружаю…» все эти пять минут, и от сломавшегося бота
        # это неотличимо. Лучше отказать быстро и сказать, что случилось.
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"},
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=25, connect=10, sock_read=20),
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    # Икота шлюза на стороне маркетплейса — не та ошибка, которую стоит
    # показывать: 502/503/504 означают, что их прокси не достучался до
    # приложения, и вторая попытка через мгновение обычно проходит. На всё
    # остальное отвечаем сразу.
    _RETRY_STATUSES = (502, 503, 504)
    _RETRIES = 2

    @staticmethod
    def _clean_error(status: int, text: str) -> str:
        """Короткая причина из тела ответа, которое может быть HTML-страницей.

        На 502 приходит целая страница с DOCTYPE; вываленная в чат, она не
        говорила продавцу ничего и хоронила под собой единственный полезный
        факт — код ответа.
        """
        body = (text or "").strip()
        if body[:1] == "<" or "<html" in body[:200].lower():
            body = ""                       # an HTML error page says nothing
        else:
            body = re.sub(r"\s+", " ", body)[:150]
        if status in (502, 503, 504):
            return f"HTTP {status}: сервер Юмаркета недоступен"
        return f"HTTP {status}" + (f": {body}" if body else "")

    @staticmethod
    def _error_message(data) -> str:
        """Читаемый текст из тела ошибки, с сохранением машинного кода.

        Маркетплейс кладёт отказ внутрь: {"error": {"code": …, "message": …}}, и
        именно `str()` от этого словаря раньше уходил продавцу дословно. Код в
        строке оставлен намеренно: возврат в продажу решает, когда перестать
        повторять, по наличию `incorrect_status` в причине.
        """
        node = data
        for _ in range(3):
            if not isinstance(node, dict):
                break
            inner = node.get("error") or node.get("errors")
            if isinstance(inner, dict) and (inner.get("message") or inner.get("code")):
                node = inner
                continue
            break
        if not isinstance(node, dict):
            return str(node or "")[:150]
        # иногда `error` — это сам текст, а не вложенный объект
        msg = str(node.get("message") or "").strip()
        if not msg and isinstance(node.get("error"), str):
            msg = node["error"].strip()
        code = str(node.get("code") or "").strip()
        if msg and code and code not in msg:
            return f"{msg} ({code})"[:200]
        return (msg or code)[:200]

    async def _request(self, method: str, path: str, *,
                       params: dict | None = None,
                       json: dict | None = None) -> dict:
        assert self.session is not None, "Call start() first"
        last = ""
        for attempt in range(self._RETRIES + 1):
            try:
                async with self.session.request(
                    method, f"{self.base_url}{path}",
                    params=params, json=json,
                ) as resp:
                    text = await resp.text()
                    logger.debug("%s %s → %s: %s", method, path, resp.status,
                                 text[:500])
                    if resp.status in self._RETRY_STATUSES and attempt < self._RETRIES:
                        last = self._clean_error(resp.status, text)
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        raise RuntimeError(self._clean_error(resp.status, text))
                    if not resp.ok:
                        raise RuntimeError(
                            self._error_message(data)
                            or self._clean_error(resp.status, text))
                    return data
            except asyncio.TimeoutError:
                # Таймаут стоит повторить по той же причине, что и 502
                if attempt < self._RETRIES:
                    last = "timeout"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError("timeout: Юмаркет не ответил вовремя")
        raise RuntimeError(last or "запрос не удался")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict | None = None) -> dict:
        return await self._request("POST", path, json=json or {})

    async def _patch(self, path: str, json: dict | None = None) -> dict:
        return await self._request("PATCH", path, json=json or {})

    async def check(self) -> dict:
        return await self._get("/check")

    async def get_ads(self, cursor: str | None = None) -> dict:
        params: dict = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/ads", params=params)

    async def get_ad(self, ad_id: int | str) -> dict:
        return await self._get(f"/ads/{ad_id}")

    async def restore_ad(self, ad_id: int | str) -> dict:
        """Снова опубликовать объявление — POST /ads/{ad_id}/publish, как в спецификации.

        Это автовозврат истёкших, а не ручная кнопка: ручную сняли по решению
        продавца, и `test_dead_buttons` следит, чтобы она не вернулась.
        """
        return await self._post(f"/ads/{ad_id}/publish")

    # Статусы, из которых этот маркетплейс возвращает товар в продажу. Он
    # поддерживает истечение срока: у объявления кончилось время — его можно
    # опубликовать снова. Намеренно не включены moderate/draft (они и так на
    # пути наверх) и blocked/fraud (публикацию отвергнут, а снимать блокировку
    # не нам).
    _DOWN = ("expired", "inactive", "sold", "archived", "disabled", "closed",
             "hidden", "not_active", "paused", "stopped")

    # Снято с публикации руками. Публиковать такие маркетплейс не даёт: каждая
    # попытка отвечает `incorrect_status` — проверено на живом API, на трёх
    # объявлениях за несколько дней. Сам собой здесь возвращается только
    # истёкший срок, поэтому эти статусы узнаются и объясняются, а не
    # перебираются вечно. В `_NEVER` они не попали: тот список означает «сказать
    # нечего», а этим есть что сказать — и на сайте их всё ещё можно вернуть
    # руками.
    _MANUAL_ONLY = ("unpublish", "unpublished", "unpublic")
    # «publish» — это слово, которым ЭТОТ маркетплейс называет живое объявление.
    # Без него двенадцать живых товаров читались как неизвестный статус.
    # Отменённые и отклонённые — конечные состояния, а не просто снятые:
    # публикация такого отвечает `incorrect_status`, поэтому их место здесь, а
    # не в `_DOWN`.
    _NEVER = ("blocked", "banned", "fraud", "moderate", "moderation", "draft",
              "deleted", "removed", "active", "publish", "published",
              "cancelled", "canceled", "rejected", "declined",
              "finished", "ended", "completed")

    @staticmethod
    def _ad_state(ad: dict) -> str:
        # Первый ключ, в котором действительно что-то есть: `get(k, запасное)`
        # отдаёт None для существующего, но пустого ключа, и до запасного очередь
        # не доходит — объявление, описанное одним `is_active`, читалось как
        # «ничего не известно».
        raw = None
        for key in ("status", "state", "is_active"):
            if ad.get(key) is not None:
                raw = ad[key]
                break
        if isinstance(raw, bool):
            return "inactive" if not raw else "active"
        if isinstance(raw, (int, float)):
            return "inactive" if raw == 0 else "active"
        return str(raw or "").lower()

    async def ad_stock(self, ad_id: int | str,
                       ad: dict | None = None) -> tuple[bool, str]:
        """Сколько у объявления осталось на продажу → (есть ли остаток, пояснение).

        Опубликовать распроданное — единственное, чего возврат в продажу делать
        не должен: маркетплейс откажет, а по расписанию это превращается в один
        и тот же отказ каждый час и навсегда. При любой ошибке отвечает «есть»:
        проверка, которая не смогла выполниться, не имеет права запрещать
        действие, которое она лишь уточняет.
        """
        try:
            inner = (ad.get("data") or ad) if ad else None
            # В строке из списка /ads обычно нет ни `type`, ни `stock`. Решение по
            # ней сваливалось в «остаток None — значит он есть», и это пропускало
            # любое объявление, включая распроданную АВТОВЫДАЧУ, публикацию которой
            # маркетплейс потом отвергал с `incorrect_status`. Если строка ответить не
            # может — дочитываем карточку, которая может, а не догадываемся.
            if not inner or (not inner.get("type") and inner.get("stock") is None):
                full = await self.get_ad(ad_id)
                inner = full.get("data") or full
            kind = str(inner.get("type") or "")

            if kind == "auto-delivery":
                data = await self.get_ad_items(ad_id)
                rows = data.get("data") or data.get("items") or []
                free = [r for r in rows
                        if str((r or {}).get("status", "available")) == "available"]
                return bool(free), f"позиций в наличии: {len(free)}"

            if kind == "auto-value":
                val = await self.get_ad_value(ad_id)
                stock = (val.get("data") or val).get("stock")
                return bool(stock), f"остаток: {stock}"

            stock = inner.get("stock")
            if stock is None:
                return True, ""
            return bool(stock), f"остаток: {stock}"
        except Exception as e:
            logger.info("stock check skipped for %s: %s", ad_id, e)
            return True, ""

    async def restore_ads(self, *, require_stock: bool = True,
                          skip_ids=(), limit: int = 0,
                          dry_run: bool = False,
                          skip_statuses=()) -> dict:
        """Снова опубликовать снятые объявления.

        Отдаёт отчёт, а не число со строкой: вызывающий должен уметь сказать,
        какие поднялись, у каких нечего продавать и какие маркетплейс отверг, —
        три разных исхода, которые прежде схлопывались в одно число и последнее
        сообщение об ошибке.
        """
        data = await self.get_ads()
        ads = [a for a in (data.get("data") or data.get("items") or [])
               if isinstance(a, dict)]
        skip = {str(i) for i in (skip_ids or ())}
        # Статусы, публиковать которые маркетплейс уже отказывался. Список собран
        # из его собственных ответов `incorrect_status`, а не придуман мной:
        # состояние, из которого не опубликовалось однажды, не станет публикуемым
        # оттого, что мы переберём в нём все объявления.
        barred = {str(x).lower() for x in (skip_statuses or ())}

        report = {"restored": [], "no_stock": [], "failed": [], "skipped": 0,
                  "unknown": [], "manual": [],
                  "statuses": sorted({self._ad_state(a) for a in ads}),
                  "total": len(ads), "dry_run": dry_run}

        candidates = []
        for ad in ads:
            aid = ad.get("id")
            if not aid:
                continue
            if str(aid) in skip:
                report["skipped"] += 1     # deleted in the panel, still listed
                continue
            state = self._ad_state(ad)
            if state in self._NEVER or state in barred:
                continue
            if state in self._MANUAL_ONLY:
                # Снято руками. Публиковать такие маркетплейс отказывается, то есть
                # каждая отправка — это гарантированный отчёт об ошибке на каждом
                # проходе; вместо этого они помечаются как требующие продавца.
                report["manual"].append(
                    {"id": str(aid),
                     "title": str(ad.get("title") or ad.get("name") or f"#{aid}"),
                     "status": state})
                continue
            if state not in self._DOWN:
                # Статус, не попавший ни в один список, молча не отбрасывается. Именно
                # так и остался незамеченным «unpublish»: проход просто сообщал, что
                # делать нечего, и причины не называл.
                report["unknown"].append(
                    {"id": str(aid),
                     "title": str(ad.get("title") or ad.get("name") or f"#{aid}"),
                     "status": state})
                continue
            candidates.append(ad)

        report["candidates"] = len(candidates)
        if limit:
            candidates = candidates[:limit]

        for ad in candidates:
            aid = ad.get("id")
            title = str(ad.get("title") or ad.get("name") or f"#{aid}")
            row = {"id": str(aid), "title": title,
                   "status": self._ad_state(ad)}

            if require_stock:
                has, note = await self.ad_stock(aid, ad)
                if not has:
                    report["no_stock"].append({**row, "note": note})
                    continue

            if dry_run:
                report["restored"].append(row)
                continue

            try:
                await self.restore_ad(aid)
                report["restored"].append(row)
            except Exception as e:
                reason = str(e)[:160]
                # Отказ, сославшийся на состояние, — единственный случай, когда строке
                # из списка верить нельзя: она-то и сказала, что объявление публикуемо.
                # Дочитываем карточку, которая решает, и прикладываем её ответ — тогда
                # отказ несёт с собой собственный диагноз, а не требует второй команды,
                # чтобы себя объяснить.
                if "incorrect_status" in reason:
                    try:
                        full = await self.get_ad(aid)
                        inner = full.get("data") or full
                        real = self._ad_state(inner)
                        kind = str(inner.get("type") or "?")
                        _has, note = await self.ad_stock(aid, inner)
                        row["status"] = real or row["status"]
                        reason += f" · карточка: {real or '?'}/{kind}"
                        if note:
                            reason += f", {note}"
                        for key in ("moderation_status", "moderation", "reason",
                                    "reject_reason", "comment"):
                            extra = inner.get(key)
                            if extra:
                                reason += f", {key}={str(extra)[:60]}"
                                break
                    except Exception as probe:
                        reason += f" · карточка не прочиталась: {str(probe)[:60]}"
                report["failed"].append({**row, "reason": reason})
        return report

    async def restore_all_ads(self) -> tuple[int, str]:
        """Обёртка над `restore_ads()`, оставленная для совместимости."""
        rep = await self.restore_ads()
        n = len(rep["restored"])
        if n:
            return n, f"✅ Восстановлено: {n}"
        if rep["no_stock"]:
            return 0, f"ℹ️ Нечего восстанавливать: {len(rep['no_stock'])} без остатков"
        if rep["failed"]:
            return 0, f"⚠️ Не удалось: {rep['failed'][0]['reason']}"
        return 0, (f"ℹ️ Нет товаров для восстановления "
                   f"(статусы: {', '.join(rep['statuses'][:8])})")

    async def get_balance(self) -> tuple[float, str]:
        """Текущий баланс → (число, готовая строка)."""
        # Основной путь: /check — тот же адрес, которым пользуется экран баланса
        try:
            data = await self._get("/check")
            # Общее с экраном баланса: это API заворачивает деньги в
            # {"amount": …, "currency": …} и кладёт магазин глубже одного уровня —
            # и то и другое раньше читалось как ноль.
            from handlers.balance import _MONEY_KEYS, _deep_find
            amount = _deep_find(data, _MONEY_KEYS)
            if amount is not None:
                return amount, f"{amount:.0f} ₽"
        except RuntimeError:
            pass
        except Exception as e:
            logger.warning("Balance parse failed: %s", e)

        # Отдельного адреса под баланс не существует: по спецификации данные
        # магазина отдаёт только /check. Показываем форму того, что пришло, вместо
        # перебора путей, которые гарантированно ответят 404.
        logger.warning("Balance not found in /check response")
        return 0.0, "—"

    async def get_withdrawals(self) -> list[dict]:
        """Список выводов средств — перебором обычных адресов.

        Пустой список означает «этот API такого не отдаёт», а не «выводов не было».
        """
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
        """Создать объявление — POST /ads (есть начиная с версии API 1.4.0).

        Объявления создаются только в конечных разделах (`is_leaf: true`);
        картинки загружаются отдельно через POST /media и цепляются по media_id.
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
        """Создать объявление, наполнить остаток и выставить на продажу.

        Остаток должен появиться до публикации, а как его передать — зависит от
        вида объявления:
          автовыдача      → `items`  (тексты, которые получит покупатель)
          авто-значение   → `value`  (остаток/мин/макс/шаг/label_id)
          обычное         → поле `stock` у самого объявления

        Фотографии сперва уходят в буфер медиа: публикация объявления без
        картинок отвергается с `empty_images`.

        Отдаёт (номер объявления, сообщение для человека).
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
                # Объявление оставляем: оно создано и опубликуется, когда починят.
                return ad_id, (f"⚠️ Товар создан (#{ad_id}), но не опубликован: "
                               f"{str(e)[:200]}")
        return ad_id, "✅ " + ", ".join(steps)

    # ------------------------------------------------------------------
    # Остаток — заполняется ДО публикации, по-разному для разных типов
    # ------------------------------------------------------------------

    async def get_ad_items(self, ad_id: int | str, cursor: str | None = None) -> dict:
        """Позиции автовыдачи у объявления: доступные и проданные."""
        params = {"cursor": cursor} if cursor else None
        return await self._get(f"/ads/{ad_id}/items", params=params)

    async def add_ad_items(self, ad_id: int | str, items: list[str]) -> dict:
        """Добавить позиции автовыдачи — POST /ads/{ad_id}/items.

        API принимает не больше 50 за раз, поэтому длинные списки уходят
        частями. `items` — это тексты, которые получит покупатель: ключи, коды.
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
        """Убрать одну непроданную позицию."""
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
        """Настройки авто-значения: остаток, мин/макс, шаг, единица измерения."""
        return await self._get(f"/ads/{ad_id}/value")

    async def update_ad_value(self, ad_id: int | str, **fields) -> dict:
        """Задать настройки авто-значения: остаток, мин, макс, шаг, label_id."""
        return await self._patch(f"/ads/{ad_id}/value", json=fields)

    async def refill_ad_value(self, ad_id: int | str, amount: float) -> dict:
        """Добавить к остатку авто-значения — или, при отрицательном числе, убавить."""
        return await self._post(f"/ads/{ad_id}/value/refill",
                                json={"amount": amount})

    async def get_value_labels(self) -> list[dict]:
        """Единицы измерения, доступные объявлениям с авто-значением."""
        data = await self._get("/values/labels")
        return data.get("data") or data.get("items") or []

    async def upload_media(self, content: bytes, filename: str = "photo.jpg",
                           content_type: str = "image/jpeg") -> str:
        """Загрузить одну картинку в буфер медиа — POST /media → media_id.

        Каждый media_id одноразовый и сгорает через сутки, если его никуда не
        прицепили.
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
        """Выставить объявление на продажу.

        Без фотографий отвечает отказом `empty_images`.
        """
        return await self._post(f"/ads/{ad_id}/publish")

    async def get_category_filters(self, category_id: int | str) -> list[dict]:
        """Product parameters a category expects (with `required` flags).

        Ответ приходит **списком**, а не конвертом: проверено живьём 19.08 на
        категории 14, где он вернул пустой список. Разбор ждал словаря и
        падал с `AttributeError` — то есть вызов не работал ни разу.
        """
        data = await self._get(f"/categories/{category_id}/filters")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("items") or []
        return []

    async def categories_raw(self) -> dict:
        """Первая страница /categories как есть — чтобы разобрать её форму."""
        return await self._get("/categories")

    async def find_categories(self, wanted: set[int],
                              max_requests: int = 120) -> dict[int, str]:
        """Названия для конкретных номеров разделов, с обходом дерева ровно
        настолько, насколько нужно.

        /categories отдаёт верхушку дерева, а товары живут в его листьях, — то
        есть номера листа в первом ответе не будет никогда. Обход идёт по
        уровням и останавливается, как только найдены все нужные номера, вместо
        того чтобы расписывать весь каталог.
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
                # Спускаться есть смысл только по ветвям: листья лежат в них
                if not row.get("is_leaf") and cid not in visited:
                    frontier.append(cid)

        logger.info("categories: %d/%d found in %d requests",
                    len(found), len(wanted), requests_made)
        return found

    async def resolve_category(self, category_id: int | str) -> str:
        """Название одного раздела без обхода всего дерева.

        Плоский справочник отдаёт только верхний уровень, поэтому номера листа
        вроде 5221 в нём нет. Адрес фильтров привязан к одному разделу и обычно
        называет его в ответе.
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



# ---------------------------------------------------------------------------
# Подключение магазина: почему токен не принят
# ---------------------------------------------------------------------------

# Отказ при подключении — единственная ошибка, которую видит человек,
# ещё ничего в боте не купивший и не настроивший. Раньше на этом месте
# печаталось `HTTP 401: {"message":"Unauthenticated."}`: продавец читал
# английскую строку и не узнавал из неё ни что случилось, ни что делать.
#
# Порядок важен: сначала «маркетплейс лежит» — при нём про токен вообще
# ничего не известно, и советовать «проверьте токен» значит отправить
# человека искать несуществующую поломку у себя.
_AUTH_TROUBLE: tuple[tuple[tuple[str, ...], str, str, bool], ...] = (
    (("timeout", "недоступен", "http 502", "http 503", "http 504"),
     "Юмаркет не ответил",
     "Токен здесь ни при чём: не отвечает сам маркетплейс. "
     "Искать поломку у себя не надо, её нет.", False),
    (("http 429", "too_many", "rate limit"),
     "Слишком много попыток подряд",
     "Юмаркет попросил сбавить темп — это про частоту запросов, "
     "а не про сам токен.", False),
    (("http 401", "unauthenticated", "unauthorized", "invalid token"),
     "Юмаркет не признал этот токен",
     "Чаще всего это одно из трёх: скопировалась только часть строки, "
     "токен уже отозван в панели, или он от другого магазина. "
     "Надёжнее всего создать новый — старый при этом перестанет работать.",
     True),
    (("http 403", "forbidden", "access_denied", "permission"),
     "Токен принят, но доступа к магазину не даёт",
     "У токена сняты права на заказы и чаты. Создай новый в разделе "
     "«Интеграции» — права выдаются при создании.", True),
    (("http 404", "not_found", "resource_not_found"),
     "Юмаркет ответил «не найдено»",
     "Так отвечают на токен от другого сервиса. Нужен именно токен "
     "Юмаркета из панели продавца.", True),
)


def auth_trouble(err: str, sent: str = "") -> tuple[str, str, bool]:
    """Отказ при подключении → (что случилось, что делать, токен ли виноват).

    Второе значение — не украшение: «попробуй ещё раз» при лежащем
    маркетплейсе и «создай токен заново» при отозванном токене — это
    противоположные действия, и перепутать их означает потратить время
    продавца на поиск поломки не там.

    Третье отвечает на вопрос «повторять ли прямо сейчас». Экран, который
    объясняет «маркетплейс не отвечает, токен ни при чём» и следующей же
    строкой просит прислать токен ещё раз, противоречит сам себе — а
    продавец в этот момент как раз решает, чинить ему что-то или ждать.

    `sent` — то, что человек прислал. По нему добавляется замечание, но
    **только вдобавок к настоящему отказу**, а не вместо него: какой формы
    бывают токены Юмаркета, мы не знаем, и отвергать строку по виду —
    прямой путь отказать в настоящем токене. Спрашиваем маркетплейс всегда,
    замечание лишь помогает прочесть его ответ.
    """
    low = (err or "").lower()
    why, what, ours = "", "", True
    for needles, text, advice, blame in _AUTH_TROUBLE:
        if any(n in low for n in needles):
            why, what, ours = text, advice, blame
            break
    if not why:
        # Непонятый ответ показывается как есть: «что-то пошло не так» —
        # это и есть та причина, по которой пишут в поддержку.
        why = "Юмаркет отказал при подключении"
        what = ("Ответ маркетплейса: " + (err or "без объяснения")[:150]
                + "\nЕсли он ничего не проясняет — создай токен заново.")

    note = _sent_note(sent)
    return why, (note + "\n\n" + what if note else what), ours


def _sent_note(sent: str) -> str:
    """Замечание о присланной строке — рядом с отказом, не вместо него."""
    text = (sent or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low.startswith(("http://", "https://", "panel.", "www.")):
        return "❗️ Похоже, это ссылка, а не токен."
    if len(text.split()) > 1:
        return ("❗️ В сообщении несколько слов — токен приходит одной "
                "строкой, без пробелов и подписей.")
    return ""


def is_rate_limit(err) -> bool:
    """Ответил ли маркетплейс «сбавь темп».

    Сколько запросов в минуту Юмаркет разрешает, нигде не написано и нам
    неизвестно. Единственный честный способ выяснить — идти с той скоростью,
    которая нужна, и слушать, когда он попросит помедленнее. Поэтому темп
    опроса подбирается по этому ответу, а не по угаданному числу.
    """
    low = str(err or "").lower()
    return any(n in low for n in
               ("429", "too_many", "too many", "rate limit", "rate_limit"))
