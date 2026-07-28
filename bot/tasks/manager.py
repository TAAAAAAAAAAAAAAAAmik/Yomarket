from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

_ACTIVE_STATUSES = {"active", "new", "work", "processing", "pending"}

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_token, get_settings, save_settings

logger = logging.getLogger(__name__)

# Selenium loop interval in seconds (check every 30 minutes)
_AUTO_LOOP_INTERVAL = 30 * 60


def _fmt_time(raw) -> str:
    if not raw:
        return ""
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.fromtimestamp(raw)
        else:
            s = str(raw)[:19]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return str(raw)[:16]
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return str(raw)[:16]


def _is_newer(msg_id: str, last_id: str) -> bool:
    try:
        return int(msg_id) > int(last_id)
    except (ValueError, TypeError):
        return msg_id > last_id


_USERNAME_RE = re.compile(r"@?([a-zA-Z][a-zA-Z0-9_]{3,31})")


def _extract_username(text: str) -> str:
    """Pull a Telegram @username out of free-form buyer text."""
    if not text:
        return ""
    # Prefer an explicit @mention
    m = re.search(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})", text)
    if m:
        return m.group(1)
    stripped = text.strip()
    m = _USERNAME_RE.fullmatch(stripped)
    if m:
        return m.group(1)
    return ""


_COMPLAINT_KEYWORDS = (
    "жалоб", "жалуюсь", "проблем", "обман", "не работает", "не пришл",
    "не пришло", "верните", "верни деньги", "возврат", "скам", "scam",
    "развод", "кидал", "кинул", "арбитраж", "спор", "диспут", "dispute",
    "администрац", "поддержк", "модератор", "обратился в", "напишу в поддержку",
)
_COMPLAINT_STATUSES = (
    "dispute", "disputed", "complaint", "arbitration", "arbitrage",
    "problem", "conflict", "appeal",
)


def _is_complaint_text(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _COMPLAINT_KEYWORDS)


def _order_field(order: dict, *keys, default=None):
    """First non-empty value among keys (supports nested buyer.*)."""
    for k in keys:
        if "." in k:
            a, b = k.split(".", 1)
            v = (order.get(a) or {})
            v = v.get(b) if isinstance(v, dict) else None
        else:
            v = order.get(k)
        if v not in (None, "", "—"):
            return v
    return default


def _order_username(order: dict) -> str:
    """Buyer @username / contact if the API exposes it."""
    u = _order_field(order, "buyer_username", "username", "buyer.username",
                     "buyer.login", "contact", "buyer.contact")
    if not u:
        return ""
    u = str(u)
    return u if u.startswith("@") else f"@{u}"


def _today_stats(order_details: dict, known_orders: dict) -> tuple[int, int]:
    """(orders today, revenue today ₽) from locally tracked order details."""
    now = time.time()
    day_start = now - (now % 86400)
    cnt = 0
    rev = 0
    for oid, det in order_details.items():
        seen = det.get("seen_at", 0)
        if seen and seen >= day_start:
            cnt += 1
            try:
                rev += int(float(str(det.get("price", 0))))
            except (ValueError, TypeError):
                pass
    return cnt, rev


def _parse_star_qty(title: str, default: int) -> int:
    """Extract a star count from an order title like '100 звёзд Telegram'."""
    if title:
        nums = re.findall(r"\d{2,6}", title.replace(" ", ""))
        if nums:
            try:
                val = int(nums[0])
                if 50 <= val <= 1_000_000:
                    return val
            except ValueError:
                pass
    return default


def _order_notify_kb(order_id: str, chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Quick actions straight from the notification (no need to open the order)
    builder.button(text="▶️ В работу", callback_data=f"order:{order_id}:work")
    builder.button(text="✅ Подтвердить", callback_data=f"order:{order_id}:confirm")
    builder.button(text="↩️ Возврат", callback_data=f"order:{order_id}:refundask")
    builder.button(text="💬 Чат", callback_data=f"chat:{chat_id}:")
    builder.button(text="🔍 Детали", callback_data=f"order:{order_id}:view")
    builder.adjust(3, 2)
    return builder.as_markup()


def _message_notify_kb(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
    builder.button(text="📋 К сделке", callback_data=f"chat:{chat_id}:")
    builder.adjust(2)
    return builder.as_markup()


class TaskManager:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}
        self._auto_tasks: dict[int, asyncio.Task] = {}
        # Per-user lock: the orders loop (60s) and the auto-tasks loop (30min)
        # both load→mutate→save the whole settings blob. Without serializing
        # them, whichever saves last silently clobbers the other's changes
        # (e.g. AutoStars pending / known_orders vs bump_schedule.last_runs).
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def start_for_user(self, user_id: int) -> None:
        if user_id in self._tasks and not self._tasks[user_id].done():
            self._tasks[user_id].cancel()
        self._tasks[user_id] = asyncio.create_task(self._user_loop(user_id))
        # Also start the auto-features loop
        if user_id in self._auto_tasks and not self._auto_tasks[user_id].done():
            self._auto_tasks[user_id].cancel()
        self._auto_tasks[user_id] = asyncio.create_task(self._auto_loop(user_id))

    def stop_for_user(self, user_id: int) -> None:
        if user_id in self._tasks:
            self._tasks[user_id].cancel()
            del self._tasks[user_id]
        if user_id in self._auto_tasks:
            self._auto_tasks[user_id].cancel()
            del self._auto_tasks[user_id]

    async def start_all(self) -> None:
        from storage import get_all_users
        for uid in get_all_users():
            self.start_for_user(uid)

    async def _user_loop(self, user_id: int) -> None:
        while True:
            try:
                await self._tick(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Task error for user %s: %s", user_id, e)
            await asyncio.sleep(60)

    async def _tick(self, user_id: int) -> None:
        token = get_token(user_id)
        if not token:
            return
        async with self._lock(user_id):
            settings = get_settings(user_id)
            await self._process_orders(user_id, token, settings)
            await self._check_reminders(user_id, settings)

    async def _process_orders(self, user_id: int, token: str, settings: dict) -> None:
        api = YooMarketAPI(token)
        await api.start()
        try:
            data = await api.get_orders()
            orders = data.get("data") or data.get("items") or []

            known: dict = settings.get("known_orders", {})
            ar = settings.get("auto_reply", {})
            ae = settings.get("auto_events", {})
            rules = settings.get("auto_rules", [])
            responders_map = settings.get("responders", {})
            order_details: dict = settings.setdefault("known_order_details", {})

            blacklist: list = settings.get("blacklist", [])
            reminded: list = settings.setdefault("reminded_orders", [])

            # First-run baseline: on the very first pass (fresh DB / new account)
            # record existing orders SILENTLY — otherwise every old order would
            # be treated as "new" and spam a notification. Only orders that
            # appear AFTER initialization trigger alerts/auto-actions.
            initialized = settings.get("orders_initialized", False)

            for order in orders:
                oid = str(order.get("id", ""))
                if not oid:
                    continue

                status = str(order.get("status", ""))
                prev_status = known.get(oid)
                title = _order_field(order, "title", "ad_title", "product_name", default="—")
                buyer = _order_field(order, "buyer_name", "buyer.name", default="—")
                price = _order_field(order, "price", "total", "amount", default="—")
                time_raw = order.get("created_at") or order.get("date") or order.get("created")
                chat_id = str(order.get("chat_id") or oid)
                username = _order_username(order)
                quantity = _order_field(order, "quantity", "count", "qty", "amount_items")
                category = _order_field(order, "category", "category_name", "ad_category")

                prev_det = order_details.get(oid, {})
                work_at = prev_det.get("work_at")
                if status in ("work", "working", "processing") and prev_status not in ("work", "working", "processing"):
                    work_at = time.time()
                order_details[oid] = {
                    "title": title,
                    "buyer": buyer,
                    "price": price,
                    "chat_id": chat_id,
                    "username": username,
                    "quantity": quantity,
                    "category": category,
                    "seen_at": prev_det.get("seen_at") or time.time(),
                    "work_at": work_at,
                }

                # If order moved to a terminal/changed status, clear its reminder record
                if prev_status is not None and prev_status != status:
                    if oid in reminded:
                        reminded.remove(oid)

                # Complaint/dispute status → high-priority alert
                cn = settings.get("complaint_notify", {"enabled": True})
                if (initialized and cn.get("enabled", True) and prev_status != status
                        and any(cs in str(status).lower() for cs in _COMPLAINT_STATUSES)):
                    seen = cn.setdefault("seen", [])
                    mark = f"status:{oid}:{status}"
                    if mark not in seen:
                        seen.append(mark)
                        settings["complaint_notify"] = cn
                        who = f"{buyer}" + (f" ({username})" if username else "")
                        await self._notify(
                            user_id,
                            f"⚠️ <b>СПОР / ЖАЛОБА по заказу!</b>\n\n"
                            f"🧾 Заказ <code>#{oid}</code>\n"
                            f"📦 {title}\n"
                            f"👤 {who}  •  💰 {price} ₽\n"
                            f"📊 Статус: <b>{status}</b>\n\n"
                            f"🔺 Требуется ваше вмешательство.",
                            reply_markup=_order_notify_kb(oid, chat_id),
                        )

                is_blacklisted = buyer in blacklist

                if prev_status is None and not initialized:
                    # baseline pass: record silently, no notify / no auto-actions
                    known[oid] = status
                    continue

                if prev_status is None:
                    # Auto-accept: press "начать заказ" immediately so orders
                    # don't sit unaccepted (buyer can't force a refund).
                    accepted = False
                    if settings.get("auto_accept", {}).get("enabled") and status in (
                            "new", "pending", "created", "paid", "active"):
                        try:
                            await api.work_order(oid)
                            known[oid] = status = "work"
                            order_details[oid]["work_at"] = time.time()
                            accepted = True
                        except Exception as e:
                            logger.warning("Auto-accept order %s: %s", oid, e)
                    if not is_blacklisted:
                        time_str = _fmt_time(time_raw)
                        cnt_today, rev_today = _today_stats(order_details, known)
                        lines = [
                            "🛒 <b>Новая покупка!</b>",
                            f"🧾 Заказ <code>#{oid}</code>",
                            "",
                            f"📦 Товар: <b>{title}</b>",
                        ]
                        if category:
                            lines.append(f"🏷 Категория: {category}")
                        if quantity:
                            lines.append(f"🔢 Количество: <b>{quantity}</b>")
                        lines.append(f"💰 Сумма: <b>{price} ₽</b>")
                        buyer_line = f"👤 Покупатель: <b>{buyer}</b>"
                        if username:
                            buyer_line += f"  ({username})"
                        lines.append(buyer_line)
                        if time_str:
                            lines.append(f"🕐 Время: {time_str}")
                        if accepted:
                            lines.append("▶️ <i>Автоматически взят в работу</i>")
                        lines.append("")
                        lines.append(f"📊 Сегодня: <b>{cnt_today}</b> заказ(ов) на "
                                     f"<b>{rev_today} ₽</b>")
                        await self._notify(
                            user_id, "\n".join(lines),
                            reply_markup=_order_notify_kb(oid, chat_id),
                        )
                    if ar.get("enabled"):
                        msg = self._pick_message(title, ar.get("message", "Спасибо за заказ!"), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    # AutoStars: ask the buyer for their @username in chat
                    await self._maybe_ask_stars_username(
                        api, settings, oid, title, chat_id)

                elif prev_status != status and status in ("confirmed", "completed", "done"):
                    ev = ae.get("on_confirmed", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Заказ подтверждён!"), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    cnt_today, rev_today = _today_stats(order_details, known)
                    buyer_line = f"👤 {buyer}" + (f" ({username})" if username else "")
                    await self._notify(
                        user_id,
                        f"✅ <b>Заказ выполнен!</b>\n"
                        f"🧾 <code>#{oid}</code>\n\n"
                        f"📦 {title}\n"
                        f"💰 <b>{price} ₽</b>\n"
                        f"{buyer_line}\n\n"
                        f"📊 Сегодня выполнено на <b>{rev_today} ₽</b>",
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                elif prev_status != status and status in ("refunded", "cancelled", "returned"):
                    ev = ae.get("on_refunded", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Возврат оформлен."), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    buyer_line = f"👤 {buyer}" + (f" ({username})" if username else "")
                    await self._notify(
                        user_id,
                        f"↩️ <b>Возврат по заказу</b>\n"
                        f"🧾 <code>#{oid}</code>\n\n"
                        f"📦 {title}\n"
                        f"💰 <b>{price} ₽</b>\n"
                        f"{buyer_line}\n"
                        f"📊 Статус: {status}",
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                known[oid] = status

            settings["reminded_orders"] = reminded
            settings["orders_initialized"] = True  # baseline established

            settings["known_orders"] = known
            settings["known_order_ids"] = list(known.keys())
            settings["known_order_details"] = order_details

            await self._check_messages(user_id, api, settings)
            await self._auto_confirm(user_id, api, settings)

            save_settings(user_id, settings)
        finally:
            await api.close()

    async def _check_messages(self, user_id: int, api: YooMarketAPI, settings: dict) -> None:
        known_orders: dict = settings.get("known_orders", {})
        known_messages: dict = settings.setdefault("known_messages", {})
        order_details: dict = settings.get("known_order_details", {})

        active = [
            oid for oid, st in known_orders.items()
            if st not in ("refunded", "cancelled", "returned")
        ][:15]

        for order_id in active:
            try:
                data = await api.get_messages(order_id)
                messages: list[dict] = data.get("data") or data.get("items") or []
                if not messages:
                    continue

                newest_id = str(messages[-1].get("id", ""))
                last_known_id = known_messages.get(order_id)

                if last_known_id is None:
                    known_messages[order_id] = newest_id
                    continue

                if not _is_newer(newest_id, last_known_id):
                    continue

                details = order_details.get(order_id, {})
                title = details.get("title", f"Заказ #{order_id}")
                buyer_name = details.get("buyer", "Покупатель")
                chat_id = details.get("chat_id", order_id)
                d_username = details.get("username", "")
                d_price = details.get("price", "")
                who = f"{buyer_name}" + (f" ({d_username})" if d_username else "")
                order_line = f"📦 {title}" + (f"  •  💰 {d_price} ₽" if d_price and d_price != "—" else "")

                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known_id):
                        continue
                    sender = msg.get("sender_type") or msg.get("sender") or ""
                    if sender not in ("buyer", "client", "customer", "user"):
                        continue

                    time_str = _fmt_time(msg.get("created_at") or msg.get("date"))
                    time_part = f"  •  🕐 {time_str}" if time_str else ""
                    raw_text = msg.get("text") or msg.get("message") or ""
                    msg_text = raw_text[:200] or "—"

                    # AutoStars: buyer replied with their @username → deliver
                    handled = await self._maybe_deliver_stars_reply(
                        user_id, api, settings, order_id, raw_text, chat_id)
                    if handled:
                        continue

                    # Complaint detection → distinct high-priority alert
                    cn = settings.get("complaint_notify", {"enabled": True})
                    if cn.get("enabled", True) and _is_complaint_text(raw_text):
                        seen = cn.setdefault("seen", [])
                        mark = f"{order_id}:{msg_id}"
                        if mark not in seen:
                            seen.append(mark)
                            if len(seen) > 500:
                                del seen[:250]
                            settings["complaint_notify"] = cn
                            await self._notify(
                                user_id,
                                f"⚠️ <b>ЖАЛОБА / ПРОБЛЕМА клиента!</b>\n\n"
                                f"👤 <b>{who}</b>{time_part}\n"
                                f"🧾 Заказ <code>#{order_id}</code>\n"
                                f"{order_line}\n\n"
                                f"<i>«{msg_text}»</i>\n\n"
                                f"🔺 Ответьте как можно быстрее.",
                                reply_markup=_message_notify_kb(chat_id),
                            )
                            continue

                    await self._notify(
                        user_id,
                        f"💬 <b>Новое сообщение</b>\n\n"
                        f"👤 <b>{who}</b>{time_part}\n"
                        f"🧾 Заказ <code>#{order_id}</code>\n"
                        f"{order_line}\n\n"
                        f"<i>«{msg_text}»</i>",
                        reply_markup=_message_notify_kb(chat_id),
                    )

                known_messages[order_id] = newest_id

            except Exception as e:
                logger.warning("Message check for order %s: %s", order_id, e)

        settings["known_messages"] = known_messages

    async def _check_reminders(self, user_id: int, settings: dict) -> None:
        rem = settings.get("reminders", {})
        if not rem.get("enabled"):
            return

        threshold_secs = rem.get("hours", 24) * 3600
        now = time.time()
        known_orders: dict = settings.get("known_orders", {})
        order_details: dict = settings.get("known_order_details", {})
        reminded: list = settings.setdefault("reminded_orders", [])
        reminded_set = set(reminded)

        changed = False
        for oid, status in known_orders.items():
            if status in ("confirmed", "completed", "done", "refunded", "cancelled", "returned"):
                continue
            if oid in reminded_set:
                continue
            det = order_details.get(oid, {})
            seen_at = det.get("seen_at", now)
            if (now - seen_at) < threshold_secs:
                continue

            hours_waiting = int((now - seen_at) / 3600)
            title = det.get("title", f"Заказ #{oid}")
            buyer = det.get("buyer", "—")
            price = det.get("price", "—")
            chat_id = det.get("chat_id", oid)
            uname = det.get("username", "")
            who = f"{buyer}" + (f" ({uname})" if uname else "")

            await self._notify(
                user_id,
                f"⏰ <b>Напоминание о заказе</b>\n\n"
                f"🧾 Заказ <code>#{oid}</code>\n"
                f"📦 {title}\n"
                f"👤 {who}  •  💰 {price} ₽\n"
                f"📊 Статус: {status}\n\n"
                f"⏳ Ждёт подтверждения уже <b>{hours_waiting} ч</b>",
                reply_markup=_order_notify_kb(oid, chat_id),
            )
            reminded_set.add(oid)
            changed = True

        if changed:
            settings["reminded_orders"] = list(reminded_set)
            from storage import save_settings
            save_settings(user_id, settings)

    async def _auto_confirm(self, user_id: int, api: YooMarketAPI, settings: dict) -> None:
        ac = settings.get("auto_confirm", {})
        if not ac.get("enabled"):
            return
        threshold_secs = ac.get("hours", 24) * 3600
        now = time.time()
        known_orders: dict = settings.get("known_orders", {})
        order_details: dict = settings.get("known_order_details", {})
        for oid, status in list(known_orders.items()):
            if status not in ("work", "working", "processing"):
                continue
            det = order_details.get(oid, {})
            work_at = det.get("work_at")
            if not work_at or (now - work_at) < threshold_secs:
                continue
            try:
                await api.confirm_order(oid)
                known_orders[oid] = "confirmed"
                title = det.get("title", f"Заказ #{oid}")
                await self._notify(user_id, f"✅ <b>Авто-подтверждение</b>\n\n📦 {title} #{oid}")
            except Exception as e:
                logger.warning("Auto-confirm order %s: %s", oid, e)
        settings["known_orders"] = known_orders

    async def _price_schedule_tick(self, api: YooMarketAPI, settings: dict) -> str:
        """Apply/restore scheduled prices. Returns a status message or ''.

        Inside the [from_hour, to_hour) window every ad price is changed by
        `percent`; base prices are remembered and restored when the window ends.
        The window may cross midnight (e.g. 22 → 8).
        """
        ps = settings["price_schedule"]
        from_h = int(ps.get("from_hour", 22)) % 24
        to_h = int(ps.get("to_hour", 8)) % 24
        percent = float(ps.get("percent", -10))
        hour = datetime.now().hour

        if from_h == to_h:
            in_window = False  # нулевое окно — ничего не делаем
        elif from_h < to_h:
            in_window = from_h <= hour < to_h
        else:  # окно через полночь, например 22 → 8
            in_window = hour >= from_h or hour < to_h

        night_active = bool(ps.get("night_active"))

        if in_window and not night_active:
            data = await api.get_ads()
            ads = data.get("data") or data.get("items") or []
            base: dict = {}
            changed = 0
            for ad in ads:
                ad_id = ad.get("id")
                try:
                    price = int(float(str(ad.get("price", 0))))
                except (TypeError, ValueError):
                    continue
                if not ad_id or price <= 0:
                    continue
                new_price = max(1, round(price * (1 + percent / 100)))
                try:
                    await api.update_price(ad_id, new_price)
                    base[str(ad_id)] = price
                    changed += 1
                except Exception as e:
                    logger.warning("Price schedule apply %s: %s", ad_id, e)
            ps["base_prices"] = base
            ps["night_active"] = True
            settings["price_schedule"] = ps
            sign = "+" if percent >= 0 else ""
            return (f"🕐 Расписание цен: окно {from_h:02d}:00–{to_h:02d}:00, "
                    f"применено {sign}{percent:.0f}% к {changed} товарам")

        if not in_window and night_active:
            base = ps.get("base_prices", {})
            restored = 0
            for ad_id, price in base.items():
                try:
                    await api.update_price(ad_id, int(price))
                    restored += 1
                except Exception as e:
                    logger.warning("Price schedule restore %s: %s", ad_id, e)
            ps["base_prices"] = {}
            ps["night_active"] = False
            settings["price_schedule"] = ps
            return f"🕐 Расписание цен: окно закончилось, восстановлено {restored} цен"

        return ""

    # ------------------------------------------------------------------
    # AutoStars — Telegram Stars auto-delivery via Fragment
    # ------------------------------------------------------------------

    async def _maybe_ask_stars_username(
        self, api: YooMarketAPI, settings: dict,
        order_id: str, title: str, chat_id: str,
    ) -> None:
        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled") or not p.get("ask_username", True):
            return
        keyword = (p.get("keyword") or "звёзд").lower()
        if keyword not in (title or "").lower():
            return
        pending: dict = p.setdefault("pending", {})
        delivered: list = p.setdefault("delivered", [])
        if order_id in pending or order_id in delivered:
            return
        qty = _parse_star_qty(title, p.get("amount", 50))
        pending[order_id] = {"quantity": qty, "asked_at": time.time()}
        await self._send_chat(
            api, chat_id,
            "⭐ Для выдачи звёзд отправьте, пожалуйста, ваш Telegram "
            "@username (например @durov). Звёзды придут автоматически.",
        )
        logger.info("AutoStars: asked username for order %s (qty=%s)", order_id, qty)

    async def _maybe_deliver_stars_reply(
        self, user_id: int, api: YooMarketAPI, settings: dict,
        order_id: str, buyer_text: str, chat_id: str,
    ) -> bool:
        """If this order is awaiting a username, try to deliver. Returns True
        if the message was consumed by the stars flow."""
        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled"):
            return False
        pending: dict = p.get("pending", {})
        entry = pending.get(order_id)
        if not entry:
            return False

        username = _extract_username(buyer_text)
        if not username:
            await self._send_chat(
                api, chat_id,
                "Не разобрал username. Пришлите его в формате @username "
                "(латиница, минимум 5 символов).",
            )
            return True

        qty = int(entry.get("quantity", p.get("amount", 50)))
        # Deliver via Fragment in a thread
        from automation.fragment import buy_stars_sync
        from storage import get_fragment_creds
        creds = get_fragment_creds(user_id)
        if not creds or not creds.get("cookies") or not creds.get("mnemonic"):
            await self._notify(
                user_id,
                f"⚠️ <b>AutoStars</b>: заказ #{order_id} — покупатель прислал "
                f"@{username}, но данные Fragment не настроены.\n"
                "Плагины → AutoStars → ⚙️ Настройки → 🔑 Данные Fragment",
            )
            return True

        await self._send_chat(
            api, chat_id, f"⏳ Отправляю {qty}⭐ на @{username}…")

        loop = asyncio.get_event_loop()
        try:
            ok, result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, buy_stars_sync,
                    creds["cookies"], creds["mnemonic"], username, qty,
                    creds.get("wallet_version", "v4r2"),
                    creds.get("api_hash", "af142ec36cafbbfa89"),
                ),
                timeout=180,
            )
        except Exception as e:
            ok, result = False, f"ошибка: {str(e)[:100]}"

        # Update plugin state
        pending.pop(order_id, None)
        if ok:
            p.setdefault("delivered", []).append(order_id)
            await self._send_chat(
                api, chat_id,
                f"✅ Готово! {qty}⭐ отправлены на @{username}. Спасибо за заказ!",
            )
            # try to confirm the order automatically
            try:
                await api.confirm_order(order_id)
            except Exception as e:
                logger.warning("AutoStars confirm order %s: %s", order_id, e)
            await self._notify(
                user_id,
                f"⭐ <b>AutoStars</b>: выдано {qty}⭐ на @{username} "
                f"(заказ #{order_id})\n{result}",
            )
        else:
            # keep pending so the seller can retry / buyer can resend
            pending[order_id] = {"quantity": qty, "asked_at": time.time()}
            await self._send_chat(
                api, chat_id,
                "⚠️ Не удалось отправить звёзды автоматически. "
                "Продавец скоро выдаст их вручную.",
            )
            await self._notify(
                user_id,
                f"❌ <b>AutoStars</b>: заказ #{order_id}, @{username}, {qty}⭐ — "
                f"не удалось.\n{result}\n\n"
                "Выдайте вручную: Плагины → AutoStars → 🚀 Ручная выдача",
            )
        return True

    def _pick_message(self, title: str, default: str, rules: list[dict], responders: dict | None = None) -> str:
        title_lower = title.lower()
        if responders:
            for game_name, message in responders.items():
                if game_name.lower() in title_lower:
                    return message
        for rule in rules:
            kw = rule.get("keyword", "").lower()
            if kw and kw in title_lower:
                return rule.get("message", default)
        return default

    async def _send_chat(self, api: YooMarketAPI, chat_id: str, text: str) -> None:
        try:
            await api.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Auto chat send failed (chat %s): %s", chat_id, e)

    async def _panel_bump(self, user_id: int) -> tuple[int, str]:
        """Raise all listings through the panel (needs a live panel session)."""
        from storage import get_panel_creds
        from automation.panel import panel_bump_all_sync

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return 0, "нужен вход в панель — откройте «Панель продавца»"

        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_bump_all_sync, creds["cookies"], user_id),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return 0, "панель не ответила вовремя"

    async def _check_panel_session(self, user_id: int, settings: dict, now: float) -> None:
        """Warn once when the stored panel session stops working.

        Checked at most every 6 hours, and the warning is sent only on the
        transition from working to dead, so a logged-out user is not nagged.
        """
        from storage import get_panel_creds

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return

        state = settings.setdefault("panel_session", {})
        if now - state.get("last_check", 0) < 6 * 3600:
            return
        state["last_check"] = now

        from automation.panel import panel_check_session_sync
        loop = asyncio.get_event_loop()
        ok, _detail = await asyncio.wait_for(
            loop.run_in_executor(None, panel_check_session_sync, creds["cookies"]),
            timeout=30,
        )
        was_ok = state.get("ok", True)
        state["ok"] = ok

        if not ok and was_ok:
            b = InlineKeyboardBuilder()
            b.button(text="📧 Войти по коду", callback_data="panel:sms_start")
            b.adjust(1)
            await self._notify(
                user_id,
                "⚠️ <b>Сессия панели истекла</b>\n\n"
                "Создание товаров и управление объявлениями сейчас недоступны. "
                "Войдите заново — код придёт на почту.",
                reply_markup=b.as_markup(),
            )

    async def _notify(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Notify failed (user %s): %s", user_id, e)

    # ------------------------------------------------------------------
    # Auto-features loop (separate from the orders loop)
    # ------------------------------------------------------------------

    async def _auto_loop(self, user_id: int) -> None:
        """Background loop for the auto features (bump / restore / withdraw)."""
        # Initial delay so it doesn't run immediately on startup
        await asyncio.sleep(60)
        while True:
            try:
                await self._tick_auto(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Auto-task error for user %s: %s", user_id, e)
            await asyncio.sleep(_AUTO_LOOP_INTERVAL)

    async def _tick_auto(self, user_id: int) -> None:
        """Run auto-bump / auto-restore / auto-withdraw via the Integration API."""
        token = get_token(user_id)
        if not token:
            return
        async with self._lock(user_id):
            await self._tick_auto_locked(user_id, token)

    async def _tick_auto_locked(self, user_id: int, token: str) -> None:
        settings = get_settings(user_id)
        now = time.time()
        messages = []

        api = YooMarketAPI(token)
        await api.start()
        try:
            # --- Auto-bump ---
            ab = settings.get("auto_bump", {})
            if ab.get("enabled"):
                interval_hours = ab.get("interval_hours", 24)
                last_run = ab.get("last_bump_run", 0)
                if (now - last_run) / 3600 >= interval_hours:
                    # Bumping runs against the panel, not the Integration API:
                    # the API has no such method, the panel exposes it as a
                    # Nova action.
                    logger.info("Auto-bump for user %s via panel", user_id)
                    count, msg = await self._panel_bump(user_id)
                    settings["auto_bump"]["last_bump_run"] = now
                    messages.append(f"⬆️ Авто-поднятие: {msg}")

            # --- Auto-restore ---
            ar = settings.get("auto_restore", {})
            if ar.get("enabled"):
                logger.info("Auto-restore for user %s via API", user_id)
                count, msg = await api.restore_all_ads()
                settings["auto_restore"]["last_restore_run"] = now
                messages.append(f"🔄 Авто-восстановление: {msg}")

            # --- Auto-withdraw ---
            aw = settings.get("auto_withdraw", {})
            if aw.get("enabled"):
                min_amount = aw.get("min_amount", 500)
                logger.info("Auto-withdraw for user %s via API", user_id)
                success, msg = await api.withdraw_balance(min_amount)
                # only log/notify when something actually happened (not "below threshold")
                if success or "ниже порога" not in msg:
                    hist = settings.setdefault("withdrawal_history", [])
                    hist.insert(0, {"amount": 0.0, "ts": now, "type": "auto",
                                    "status": "requested" if success else "failed"})
                    del hist[100:]
                    messages.append(f"💸 Авто-вывод: {msg}")

            # --- Panel session health ---
            # Panel operations (product creation, item management) run on
            # cookies that expire silently; without this the user only finds
            # out when an action fails mid-use.
            try:
                await self._check_panel_session(user_id, settings, now)
            except Exception as e:
                logger.warning("Panel session check failed for %s: %s", user_id, e)

            # --- Balance notify ---
            bn = settings.get("balance_notify", {})
            if bn.get("enabled"):
                threshold = bn.get("threshold", 1000)
                last_bal = bn.get("last_notified_balance", 0.0)
                try:
                    balance, balance_str = await api.get_balance()
                    settings["balance_notify"]["last_notified_balance"] = balance
                    if balance >= threshold > last_bal:
                        await self._notify(
                            user_id,
                            f"🔔 <b>Уведомление о балансе</b>\n\n"
                            f"Баланс достиг порога: <b>{balance_str}</b> ≥ {threshold:.0f} ₽",
                        )
                except Exception as e:
                    logger.warning("Balance notify error for user %s: %s", user_id, e)

            # --- Daily report ---
            dr = settings.get("daily_report", {})
            if dr.get("enabled"):
                report_hour = dr.get("hour", 20)
                today_str = datetime.now().strftime("%Y-%m-%d")
                last_day = dr.get("last_report_day", "")
                if last_day != today_str and datetime.now().hour >= report_hour:
                    settings["daily_report"]["last_report_day"] = today_str
                    try:
                        known_orders = get_settings(user_id).get("known_orders", {})
                        completed_today = sum(
                            1 for s in known_orders.values()
                            if s in ("confirmed", "completed", "done")
                        )
                        total = len(known_orders)
                        balance, balance_str = await api.get_balance()
                        await self._notify(
                            user_id,
                            f"📊 <b>Ежедневный отчёт</b>\n\n"
                            f"📦 Всего заказов: <b>{total}</b>\n"
                            f"✅ Выполнено: <b>{completed_today}</b>\n"
                            f"💰 Баланс: <b>{balance_str}</b>",
                        )
                    except Exception as e:
                        logger.warning("Daily report error for user %s: %s", user_id, e)

            # --- Reviews monitor ---
            rm = settings.get("reviews_monitor", {})
            if rm.get("enabled"):
                try:
                    data = await api.get_reviews()
                    reviews = data.get("data") or data.get("items") or []
                    known_ids: list = rm.get("known_review_ids", [])
                    known_set = set(str(r) for r in known_ids)
                    new_reviews = []
                    for rev in reviews:
                        rid = str(rev.get("id", ""))
                        if rid and rid not in known_set:
                            new_reviews.append(rev)
                            known_set.add(rid)
                    if new_reviews:
                        settings["reviews_monitor"]["known_review_ids"] = list(known_set)
                        for rev in new_reviews:
                            author = rev.get("author") or rev.get("buyer_name") or "Покупатель"
                            rating = rev.get("rating") or rev.get("stars") or "?"
                            text = (rev.get("text") or rev.get("comment") or "—")[:300]
                            stars = "⭐" * int(rating) if str(rating).isdigit() else f"★{rating}"
                            await self._notify(
                                user_id,
                                f"⭐ <b>Новый отзыв</b>\n\n"
                                f"👤 {author}  {stars}\n\n"
                                f"<i>«{text}»</i>",
                            )
                    elif not known_ids:
                        settings["reviews_monitor"]["known_review_ids"] = [
                            str(r.get("id", "")) for r in reviews if r.get("id")
                        ]
                except Exception as e:
                    logger.warning("Reviews monitor error for user %s: %s", user_id, e)

            # --- Price schedule (day/night pricing) ---
            ps = settings.get("price_schedule", {})
            if ps.get("enabled"):
                try:
                    msg = await self._price_schedule_tick(api, settings)
                    if msg:
                        messages.append(msg)
                except Exception as e:
                    logger.warning("Price schedule error for user %s: %s", user_id, e)

            # --- Bump scheduler (with optional paid-bump cost ceiling) ---
            bs = settings.get("bump_schedule", {})
            if bs.get("enabled") and bs.get("times"):
                now_dt = datetime.now()
                current_mins = now_dt.hour * 60 + now_dt.minute
                last_runs: dict = bs.get("last_runs", {})
                today_str = now_dt.strftime("%Y-%m-%d")
                price_per_bump = float(bs.get("price_per_bump", 0) or 0)
                ceiling = float(bs.get("daily_ceiling", 0) or 0)
                # reset the daily spend counter when the day changes
                if bs.get("spent_day") != today_str:
                    bs["spent_day"] = today_str
                    bs["spent_today"] = 0.0
                spent_today = float(bs.get("spent_today", 0) or 0)
                for slot in bs["times"]:
                    try:
                        sh, sm = map(int, slot.split(":"))
                    except (ValueError, AttributeError):
                        continue
                    slot_mins = sh * 60 + sm
                    # Within 35-minute window of the slot
                    if not (0 <= current_mins - slot_mins < 35):
                        continue
                    last_run_key = f"{today_str}_{slot}"
                    if last_runs.get(last_run_key):
                        continue
                    # Cost ceiling: skip if we've already hit the daily cap
                    if ceiling > 0 and price_per_bump > 0 and spent_today >= ceiling:
                        last_runs[last_run_key] = now_dt.isoformat()
                        messages.append(
                            f"⛔ Поднятие ({slot}) пропущено: достигнут потолок "
                            f"{ceiling:.0f} ₽/день (потрачено {spent_today:.0f} ₽)")
                        continue
                    try:
                        count, msg = await self._panel_bump(user_id)
                        last_runs[last_run_key] = now_dt.isoformat()
                        bs["bumps_total"] = int(bs.get("bumps_total", 0)) + (count or 0)
                        if price_per_bump > 0 and count:
                            spent_today += count * price_per_bump
                            bs["spent_today"] = spent_today
                            bs["spent_total"] = float(bs.get("spent_total", 0)) + count * price_per_bump
                            cap = f" (потрачено {spent_today:.0f}"
                            cap += f"/{ceiling:.0f} ₽)" if ceiling > 0 else " ₽)"
                            messages.append(f"⬆️ Поднятие ({slot}): {msg}{cap}")
                        else:
                            messages.append(f"⬆️ Поднятие ({slot}): {msg}")
                        settings["bump_schedule"] = bs
                    except Exception as e:
                        logger.warning("Bump scheduler error for user %s slot %s: %s", user_id, slot, e)

        except Exception as e:
            logger.error("Auto-tasks error for user %s: %s", user_id, e)
            messages.append(f"❌ Ошибка авто-задач: {e}")
        finally:
            await api.close()

        # Always persist: balance_notify / daily_report / reviews_monitor update
        # their dedupe state (last_notified_balance, last_report_day,
        # known_review_ids) WITHOUT appending to `messages`. Saving only when
        # `messages` was non-empty lost that state every cycle → repeated spam.
        save_settings(user_id, settings)

        if messages:
            await self._notify(
                user_id,
                "🤖 <b>Авто-задачи</b>\n\n" + "\n".join(messages),
            )
