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


def _msg_rows(data) -> list[dict]:
    """The messages out of whatever envelope the API used.

    A chat outside an order does not have to answer in the same shape as one
    attached to an order, and a nested list arriving where a list was assumed
    raised inside the poll loop, where the error was only logged — so nothing
    ever arrived and nothing ever complained.
    """
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "items", "messages", "results"):
        v = data.get(key)
        if isinstance(v, list):
            return [m for m in v if isinstance(m, dict)]
        if isinstance(v, dict):
            inner = _msg_rows(v)
            if inner:
                return inner
    return []


def _newest_id(rows: list[dict]) -> str:
    """The largest message id present.

    Taking rows[-1] assumed the API sorts oldest-first. If it sorts the other
    way round, that is the OLDEST message, "is there anything newer" is false
    forever, and no notification is ever sent.
    """
    best = ""
    for m in rows:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        if not best or _is_newer(mid, best):
            best = mid
    return best


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


_DONE_STATUSES = ("confirmed", "completed", "done")
_BACK_STATUSES = ("refunded", "cancelled", "returned")


def _window_stats(order_details: dict, known_orders: dict,
                  since_ts: float) -> dict:
    """Order figures for everything first seen at/after since_ts.

    Revenue counts completed orders only — an order that came in and was
    refunded is not money earned — so «выручка» in a report means what it says.
    Orders are dated by when the bot first saw them (seen_at): the only per-order
    timestamp kept locally, and good enough for "today" once the bot is running.
    """
    orders = completed = refunded = revenue = 0
    for oid, det in order_details.items():
        seen = det.get("seen_at", 0)
        if not seen or seen < since_ts:
            continue
        orders += 1
        st = str(known_orders.get(oid) or known_orders.get(str(oid)) or "")
        try:
            price = int(float(str(det.get("price", 0))))
        except (ValueError, TypeError):
            price = 0
        if st in _DONE_STATUSES:
            completed += 1
            revenue += price
        elif st in _BACK_STATUSES:
            refunded += 1
    return {"orders": orders, "completed": completed,
            "refunded": refunded, "revenue": revenue}


def _today_stats(order_details: dict, known_orders: dict) -> tuple[int, int]:
    """(orders today, revenue today ₽) from locally tracked order details.

    Kept for the new-purchase notification's running tally, which counts every
    order taken today, not only completed ones.
    """
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


# Notifications share one layout: a title, a rule, the facts, a rule, context.
# Scanning a phone screen is easier when every alert puts the same thing in the
# same place.
_RULE = "━━━━━━━━━━━━━━"


def _esc(text) -> str:
    """Notifications are sent with HTML parsing, so anything typed by a buyer or
    by support — a '<' in a message, an angle bracket in a title — would make
    Telegram reject the send and the notification would never arrive."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _money(value) -> str:
    """1234567 -> '1 234 567' — a thin space every three digits."""
    try:
        return f"{int(float(str(value).replace(' ', '').replace(',', '.'))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _card(title: str, body: list[str], footer: str = "") -> str:
    # Empty strings are deliberate spacing, so only None is dropped — filtering
    # on truthiness collapsed the blank lines that separate the blocks.
    parts = [title, _RULE, *[b for b in body if b is not None]]
    if footer:
        parts += [_RULE, footer]
    return "\n".join(parts)


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


def _balance_notify_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Вывести средства", callback_data="balance:withdraw")
    builder.button(text="💰 Баланс", callback_data="menu:balance")
    builder.button(text="📊 Статистика", callback_data="menu:stats")
    builder.adjust(1, 2)
    return builder.as_markup()


def _watched_notify_kb(chat_id: str) -> InlineKeyboardMarkup:
    """For a support/moderation message: the marketplace API refuses a reply
    (no active order), so answering goes through the panel chat token in-bot,
    with a link to the panel as a fallback."""
    digits = "".join(ch for ch in str(chat_id) if ch.isdigit()) or str(chat_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"sreply:{chat_id}")
    builder.button(text="📜 История", callback_data=f"wchat_hist:{chat_id}")
    builder.button(text="🌐 В панели",
                   url=f"https://panel.yoomarket.net/chats/{digits}")
    builder.adjust(2, 1)
    return builder.as_markup()


def _message_notify_kb(chat_id: str, order_id: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
    builder.button(text="💬 Открыть чат", callback_data=f"chat:{chat_id}:")
    if order_id:
        # Most replies are followed by acting on the order, so keep those here
        builder.button(text="▶️ В работу", callback_data=f"order:{order_id}:work")
        builder.button(text="✅ Подтвердить",
                       callback_data=f"order:{order_id}:confirm")
        builder.adjust(2, 2)
    else:
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
                        who = _esc(f"{buyer}" + (f" ({username})" if username else ""))
                        await self._notify(
                            user_id,
                            _card("🚨 <b>СПОР ПО ЗАКАЗУ</b>",
                                  [f"📦 <b>{_esc(title)}</b>",
                                   f"💰 <b>{_money(price)} ₽</b>   📊 {_esc(status)}",
                                   "",
                                   f"👤 {who}",
                                   f"🧾 <code>#{oid}</code>",
                                   "",
                                   "🔺 <b>Требуется вмешательство</b>"]),
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
                    if not is_blacklisted and settings.get(
                            "notify_orders", {}).get("enabled", True):
                        time_str = _fmt_time(time_raw)
                        cnt_today, rev_today = _today_stats(order_details, known)
                        qty_part = f"  ×{quantity}" if quantity else ""
                        who_line = f"👤 <b>{_esc(buyer)}</b>" + (
                            f"  {_esc(username)}" if username else "")
                        body = [
                            f"📦 <b>{_esc(title)}</b>{qty_part}",
                            f"💰 <b>{_money(price)} ₽</b>"
                            + (f"   🏷 {_esc(category)}" if category else ""),
                            "",
                            who_line,
                            f"🕐 {time_str}   🧾 <code>#{oid}</code>"
                            if time_str else f"🧾 <code>#{oid}</code>",
                        ]
                        if accepted:
                            body.append("▶️ <i>взят в работу автоматически</i>")
                        await self._notify(
                            user_id,
                            _card("🛒 <b>НОВАЯ ПОКУПКА</b>", body,
                                  f"📊 Сегодня: <b>{cnt_today}</b> · "
                                  f"<b>{_money(rev_today)} ₽</b>"),
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
                    buyer_line = _esc(f"👤 {buyer}" + (f" ({username})" if username else ""))
                    await self._notify(
                        user_id,
                        _card("✅ <b>ЗАКАЗ ВЫПОЛНЕН</b>",
                              [f"📦 <b>{_esc(title)}</b>",
                               f"💰 <b>{_money(price)} ₽</b>",
                               "",
                               buyer_line,
                               f"🧾 <code>#{oid}</code>"],
                              f"📊 Сегодня выполнено на "
                              f"<b>{_money(rev_today)} ₽</b>"),
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                elif prev_status != status and status in ("refunded", "cancelled", "returned"):
                    ev = ae.get("on_refunded", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Возврат оформлен."), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    buyer_line = _esc(f"👤 {buyer}" + (f" ({username})" if username else ""))
                    await self._notify(
                        user_id,
                        _card("↩️ <b>ВОЗВРАТ</b>",
                              [f"📦 <b>{_esc(title)}</b>",
                               f"💰 <b>{_money(price)} ₽</b>",
                               "",
                               buyer_line,
                               f"🧾 <code>#{oid}</code>   📊 {_esc(status)}"]),
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                known[oid] = status

            settings["reminded_orders"] = reminded
            settings["orders_initialized"] = True  # baseline established

            settings["known_orders"] = known
            settings["known_order_ids"] = list(known.keys())
            settings["known_order_details"] = order_details

            await self._check_messages(user_id, api, settings)
            await self._check_watched_chats(user_id, api, settings)
            await self._auto_confirm(user_id, api, settings)

            save_settings(user_id, settings)
        finally:
            await api.close()

    async def _check_watched_chats(self, user_id: int, api: YooMarketAPI,
                                   settings: dict) -> None:
        """Poll chats that belong to no order — support, moderation.

        The API cannot list chats, only read one by id, so these are the ids the
        seller added by hand. Without this, support messages never reach the bot
        at all: order polling has nothing to find them by.
        """
        watched: dict = settings.get("watched_chats") or {}
        if not watched:
            return
        # Support and moderation are chat traffic too — the same switch covers
        # them, but their position is still tracked so nothing is re-sent when
        # notifications are turned back on.
        announce = settings.get("notify_messages", {}).get("enabled", True)

        for chat_id, info in list(watched.items()):
            try:
                data = await api.get_messages(chat_id)
                messages = _msg_rows(data)
                if not messages:
                    continue

                newest_id = _newest_id(messages)
                last_known = info.get("last_msg")
                if last_known is None:
                    info["last_msg"] = newest_id      # baseline, stay quiet
                    continue
                if not _is_newer(newest_id, last_known):
                    continue

                label = info.get("label") or f"Чат #{chat_id}"
                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known):
                        continue
                    sender = msg.get("sender_type") or msg.get("sender") or ""
                    if isinstance(sender, dict):
                        sender = sender.get("type") or sender.get("role") or ""
                    sender = str(sender).lower()
                    if msg.get("is_mine") or msg.get("is_own") or sender in (
                            "me", "self", "own", "shop", "seller"):
                        continue

                    if not announce:
                        continue
                    text = (msg.get("text") or msg.get("message") or "")[:400]
                    time_str = _fmt_time(msg.get("created_at") or msg.get("date"))
                    await self._notify(
                        user_id,
                        _card(f"🛟 <b>{_esc(label).upper()}</b>",
                              [f"🕐 {time_str}" if time_str else "",
                               "",
                               f"<blockquote>{_esc(text)}</blockquote>"],
                              f"💬 <code>#{chat_id}</code>"),
                        reply_markup=_watched_notify_kb(str(chat_id)),
                    )
                info["last_msg"] = newest_id
            except Exception as e:
                logger.warning("watched chat %s: %s", chat_id, e)

        settings["watched_chats"] = watched

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
                # Messages live under the ORDER'S CHAT, not the order:
                # GET /chats/{chat_id}/messages. Querying by order id returned
                # nothing whenever the two differ, so buyer messages never
                # arrived. The id was already stored when the order was seen.
                details = order_details.get(order_id, {})
                chat_id = str(details.get("chat_id") or order_id)

                data = await api.get_messages(chat_id)
                messages = _msg_rows(data)
                if not messages:
                    continue

                newest_id = _newest_id(messages)
                last_known_id = known_messages.get(order_id)

                if last_known_id is None:
                    known_messages[order_id] = newest_id
                    continue

                if not _is_newer(newest_id, last_known_id):
                    continue

                title = details.get("title", f"Заказ #{order_id}")
                buyer_name = details.get("buyer", "Покупатель")
                d_username = details.get("username", "")
                d_price = details.get("price", "")
                who = _esc(f"{buyer_name}" + (f" ({d_username})" if d_username else ""))
                order_line = _esc(f"📦 {title}" + (f"  •  💰 {d_price} ₽" if d_price and d_price != "—" else ""))

                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known_id):
                        continue
                    # Skip what the shop itself sent; anything else counts as
                    # the buyer. Requiring a known buyer value meant an
                    # unfamiliar wording silently dropped every message.
                    sender = msg.get("sender_type") or msg.get("sender") or ""
                    if isinstance(sender, dict):
                        sender = (sender.get("type") or sender.get("role")
                                  or sender.get("name") or "")
                    sender = str(sender).lower()
                    if msg.get("is_mine") or msg.get("is_own"):
                        continue
                    # Substring match only for distinctive words: short ones
                    # like "me" also occur inside "customer".
                    _OWN_PARTS = ("shop", "seller", "store", "merchant",
                                  "продав", "магаз", "support", "админ")
                    _OWN_EXACT = {"me", "self", "own", "admin", "bot", "system"}
                    if sender in _OWN_EXACT or any(k in sender for k in _OWN_PARTS):
                        continue
                    if sender and sender not in (
                            "buyer", "client", "customer", "user"):
                        logger.info("chat %s: unknown sender %r", chat_id, sender)

                    time_str = _fmt_time(msg.get("created_at") or msg.get("date"))
                    time_part = f"  •  🕐 {time_str}" if time_str else ""
                    raw_text = msg.get("text") or msg.get("message") or ""
                    # raw_text stays intact for the rules below; only the copy
                    # that goes into an HTML message is escaped
                    msg_text = _esc(raw_text[:200]) or "—"

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
                                _card("🚨 <b>ЖАЛОБА КЛИЕНТА</b>",
                                      [f"👤 <b>{who}</b>{time_part}",
                                       "",
                                       f"<blockquote>{msg_text}</blockquote>",
                                       "",
                                       "🔺 <b>Ответьте как можно быстрее</b>"],
                                      f"{order_line}\n🧾 <code>#{order_id}</code>"),
                                reply_markup=_message_notify_kb(chat_id, order_id),
                            )
                            continue

                    # A complaint is an alert, not chat traffic, and is sent
                    # above regardless — this switch only silences ordinary
                    # buyer messages.
                    if settings.get("notify_messages", {}).get("enabled", True):
                        await self._notify(
                            user_id,
                            _card("💬 <b>СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ</b>",
                                  [f"👤 <b>{who}</b>{time_part}",
                                   "",
                                   f"<blockquote>{msg_text}</blockquote>"],
                                  f"{order_line}\n🧾 <code>#{order_id}</code>"),
                            reply_markup=_message_notify_kb(chat_id, order_id),
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
            who = _esc(f"{buyer}" + (f" ({uname})" if uname else ""))

            await self._notify(
                user_id,
                f"⏰ <b>Напоминание о заказе</b>\n\n"
                f"🧾 Заказ <code>#{oid}</code>\n"
                f"📦 {_esc(title)}\n"
                f"👤 {who}  •  💰 {_esc(price)} ₽\n"
                f"📊 Статус: {_esc(status)}\n\n"
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

    async def _panel_bump(self, user_id: int, api: YooMarketAPI | None = None,
                          ) -> tuple[int, str]:
        """Promote all listings through the panel.

        Runs only from schedules the owner switched on themselves, so
        confirm=True is passed here; the tariff they picked decides what is
        bought, and the daily spend ceiling caps how many listings a run pays
        for.
        """
        from storage import get_panel_creds
        from automation.panel import panel_bump_all_sync
        from handlers.selenium_settings import (promo_limit, promo_only_ids,
                                                promo_params, promo_price)

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return 0, "нужен вход в панель — откройте «Панель продавца»"

        settings = get_settings(user_id)
        params = promo_params(settings)
        if not params:
            return 0, ("не выбран тариф «Премиум» — откройте "
                       "«Объявления» → «Премиум продвижение» → «Тариф»")

        # Not gated on the shop balance: «Премиум» is paid by СБП/card/crypto,
        # not from it, so the balance says nothing about whether this can run.
        caps = [c for c in (promo_limit(settings),) if c]
        loop = asyncio.get_event_loop()
        try:
            count, msg = await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_bump_all_sync, creds["cookies"], user_id, True,
                    params, min(caps) if caps else 0,
                    promo_only_ids(settings)),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return 0, "панель не ответила вовремя"
        spent = count * promo_price(settings)
        if spent:
            msg += f" · к оплате {spent} ₽"
        return count, msg

    async def _check_position(self, user_id: int, settings: dict, now: float,
                              api: YooMarketAPI | None = None) -> str:
        """Watch where the shop sits in the offers list; act when it slips.

        Returns a notification, or '' to stay quiet. Promotion costs money, so
        falling below the threshold only *promotes* when the seller switched
        that on explicitly — otherwise it just says so, which is the safe
        default for a trigger that spends.
        """
        pp = settings.setdefault("promo_position", {})
        url = (pp.get("url") or "").strip()
        if not url:
            return ""
        interval = float(pp.get("interval_hours", 1) or 1)
        if (now - float(pp.get("last_check", 0) or 0)) / 3600 < interval:
            return ""

        from automation.market import cheapest, fetch_offers_sync, find_position
        from storage import get_shop_name

        loop = asyncio.get_event_loop()
        try:
            ok, res = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_offers_sync, url), timeout=60)
        except Exception as e:
            logger.warning("position check for %s: %s", user_id, e)
            return ""
        pp["last_check"] = now
        if not ok:
            return ""                       # page unreadable; stay quiet

        offers = res["offers"]
        shop = get_shop_name(user_id) or ""
        mine = find_position(offers, seller=shop) if shop else None
        if not mine:
            return ""                       # cannot locate ourselves — no alarm

        pos = int(mine["pos"])
        prev = int(pp.get("last_pos", 0) or 0)
        pp["last_pos"] = pos
        threshold = int(pp.get("max_position", 3) or 3)

        body: list[str] = []
        slipped = pos > threshold
        # Alert once per slip, and again only if it gets worse — a listing
        # sitting at 7th place should not shout every hour.
        alerted_at = int(pp.get("last_alert_pos", 0) or 0)
        if slipped and (pos > alerted_at or alerted_at == 0):
            pp["last_alert_pos"] = pos
            body.append(f"📉 Опустились на <b>{pos}-е место</b>"
                        + (f" (было {prev})" if prev and prev != pos else ""))
            body.append(f"🎯 Порог: не ниже {threshold}-го")
        elif not slipped and alerted_at:
            pp["last_alert_pos"] = 0
            body.append(f"📈 Вернулись на <b>{pos}-е место</b> — порог соблюдён")

        # Undercutting: the cheapest offer on the page against ours
        low = cheapest(offers)
        if pp.get("undercut_notify", True) and low is not None and mine["price"]:
            if low < float(mine["price"]):
                body.append("")
                body.append(f"💰 Дешевле всех: <b>{low:.0f} ₽</b>, "
                            f"у вас {float(mine['price']):.0f} ₽")
        floor = float(pp.get("min_price", 0) or 0)
        if floor and low is not None and low < floor:
            body.append(f"⚠️ Цена на витрине упала ниже {floor:.0f} ₽")

        if not body:
            return ""

        promoted = ""
        if slipped and pp.get("auto_promote"):
            from handlers.selenium_settings import promo_params
            if promo_params(settings):
                count, msg = await self._panel_bump(user_id, api)
                promoted = (f"\n⭐ Поднятие: {_esc(str(msg))[:120]}"
                            if count or msg else "")
            else:
                promoted = "\n⚠️ Не поднял: не выбран тариф «Премиум»"

        return _card("📍 <b>ПОЗИЦИЯ НА ВИТРИНЕ</b>", body,
                     f"🏪 {_esc(shop)}   ·   предложений: {len(offers)}"
                     + promoted)

    async def _auto_withdraw(self, user_id: int, api: YooMarketAPI,
                             settings: dict, now: float) -> str:
        """One scheduled withdrawal attempt. Returns a notification, or ''.

        Two routes: the Integration API (which has no withdrawal endpoint, so
        this only works if one ever appears), and the panel (where withdrawal
        actually lives). The panel route runs only with a method and values the
        seller configured — this moves money out, so nothing is guessed.
        """
        aw = settings.setdefault("auto_withdraw", {})
        min_amount = float(aw.get("min_amount", 500) or 0)

        try:
            balance, balance_str = await api.get_balance()
        except Exception as e:
            logger.warning("Auto-withdraw balance for %s: %s", user_id, e)
            return ""
        if balance_str == "—":
            return ""                                   # unknown balance, skip
        if balance < min_amount:
            return ""                                   # below threshold, quiet

        method = aw.get("method", "api")
        if method == "panel":
            balance_id = aw.get("panel_balance_id") or ""
            action_key = aw.get("panel_action_key") or ""
            values = dict(aw.get("panel_values") or {})
            if not balance_id or not action_key or not values:
                aw["enabled"] = False                   # misconfigured; stop
                return ("💸 Авто-вывод выключен: не настроен способ вывода "
                        "через панель. Откройте «Баланс» → «Автовывод».")
            from storage import get_panel_creds
            from automation.panel import panel_withdraw_sync
            creds = get_panel_creds(user_id)
            if not creds or not creds.get("cookies"):
                return "💸 Авто-вывод: нужен вход в панель продавца."
            # The wizard stores the method and requisites without an amount —
            # auto-withdraw takes the whole available balance.
            values = {**values, "amount": int(balance)}
            loop = asyncio.get_event_loop()
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_withdraw_sync,
                                     creds["cookies"], balance_id, action_key,
                                     values, user_id, True),
                timeout=60)
        else:
            ok, msg = await api.withdraw_balance(min_amount)
            if not ok and "не поддерживает" in msg:
                # Do not repeat a doomed API attempt on every interval
                aw["enabled"] = False
                aw["last_run"] = now
                aw["last_result"] = msg
                return ("💸 Авто-вывод выключен: вывод через API недоступен. "
                        "Настройте вывод через панель в «Баланс» → «Автовывод».")

        aw["last_run"] = now
        aw["last_result"] = msg
        hist = settings.setdefault("withdrawal_history", [])
        hist.insert(0, {"amount": float(int(balance)), "ts": now,
                        "type": "auto",
                        "status": "requested" if ok else "failed"})
        del hist[100:]
        return _card("💸 <b>АВТО-ВЫВОД</b>",
                     [f"💰 Баланс был: <b>{balance_str}</b>", "",
                      ("✅ " if ok else "⚠️ ") + _esc(msg)])

    async def _daily_report_text(self, api: YooMarketAPI, settings: dict) -> str:
        """The end-of-day summary, as today's numbers rather than lifetime ones.

        The old report showed the all-time completed count under a «за сегодня»
        heading; here every figure is the day's own, with revenue net of what
        promotion cost, so the line the seller reads is the money that actually
        moved.
        """
        details = settings.get("known_order_details", {})
        known = settings.get("known_orders", {})
        day_start = time.time() - (time.time() % 86400)
        st = _window_stats(details, known, day_start)

        bs = settings.get("bump_schedule", {})
        spent_today = 0.0
        if bs.get("spent_day") == datetime.now().strftime("%Y-%m-%d"):
            spent_today = float(bs.get("spent_today", 0) or 0)

        try:
            _bal, balance_str = await api.get_balance()
        except Exception:
            balance_str = "—"

        net = st["revenue"] - spent_today
        body = [
            f"🛒 Заказов за день: <b>{st['orders']}</b>",
            f"✅ Выполнено: <b>{st['completed']}</b>"
            + (f"   ↩️ Возвраты: {st['refunded']}" if st['refunded'] else ""),
            "",
            f"💵 Выручка: <b>{_money(st['revenue'])} ₽</b>",
        ]
        if spent_today:
            body.append(f"⬆️ Продвижение: −{_money(spent_today)} ₽")
            body.append(f"🟰 Чистыми: <b>{_money(net)} ₽</b>")
        body.append("")
        body.append(f"💰 Баланс сейчас: <b>{balance_str}</b>")
        if not st["orders"]:
            body.append("")
            body.append("<i>Сегодня заказов не было.</i>")

        return _card("📊 <b>ИТОГИ ДНЯ</b>", body,
                     f"🗓 {datetime.now().strftime('%d.%m.%Y')}")

    async def _auto_restore(self, user_id: int, api: YooMarketAPI,
                            settings: dict, now: float) -> str:
        """One scheduled restore pass. Returns a notification, or '' to stay quiet.

        Quiet matters here: this runs on a timer, and "нечего восстанавливать"
        every hour is noise that trains the seller to ignore the channel. Only
        real events speak — ads that went back up, and refusals not already
        reported.
        """
        ar = settings.setdefault("auto_restore", {})
        failures: dict = ar.setdefault("failures", {})

        # An ad the marketplace refused is not retried immediately: the reason
        # rarely changes within the hour, and a schedule would otherwise repeat
        # the same rejected call forever. The wait doubles, up to a day.
        held = {aid for aid, f in failures.items()
                if float(f.get("until", 0) or 0) > now}

        from handlers.panel_items import _deleted_ids
        skip = set(held) | _deleted_ids(user_id)

        try:
            rep = await api.restore_ads(
                require_stock=bool(ar.get("require_stock", True)),
                skip_ids=skip)
        except Exception as e:
            logger.warning("Auto-restore for %s: %s", user_id, e)
            return f"🔄 Авто-восстановление не отработало: {_esc(str(e)[:120])}"

        ar["last_restore_run"] = now

        for row in rep["restored"]:
            failures.pop(str(row["id"]), None)      # it worked; forget the past
        ar["restored_total"] = int(ar.get("restored_total", 0)) + len(rep["restored"])

        fresh_failures = []
        for row in rep["failed"]:
            aid = str(row["id"])
            prev = failures.get(aid) or {}
            tries = int(prev.get("tries", 0)) + 1
            # 1h, 2h, 4h ... capped at a day
            wait = min(3600 * (2 ** (tries - 1)), 86400)
            failures[aid] = {"tries": tries, "until": now + wait,
                             "reason": row["reason"], "title": row["title"]}
            if prev.get("reason") != row["reason"]:
                fresh_failures.append(row)          # only new news is reported

        # Keep the memory from growing without bound
        for aid in [a for a, f in failures.items()
                    if float(f.get("until", 0) or 0) < now - 7 * 86400]:
            failures.pop(aid, None)
        ar["failures"] = failures

        summary = (f"поднято {len(rep['restored'])}, "
                   f"без остатков {len(rep['no_stock'])}, "
                   f"отказов {len(rep['failed'])}")
        ar["last_result"] = summary

        if not rep["restored"] and not fresh_failures:
            return ""                                # nothing happened, say nothing

        body: list[str] = []
        if rep["restored"]:
            body.append(f"✅ Снова в продаже: <b>{len(rep['restored'])}</b>")
            for row in rep["restored"][:8]:
                body.append(f"   • {_esc(row['title'])[:40]}")
            body.append("")
            body.append("<i>Опубликованное уходит на модерацию — "
                        "статус сменится после проверки.</i>")
        if rep["no_stock"]:
            body.append("")
            body.append(f"📦 Без остатков: <b>{len(rep['no_stock'])}</b> "
                        f"— их публиковать нечем")
            for row in rep["no_stock"][:5]:
                body.append(f"   • {_esc(row['title'])[:34]} — {_esc(row.get('note'))}")
        if rep.get("unknown"):
            body.append("")
            body.append(f"❔ Незнакомый статус у <b>{len(rep['unknown'])}</b> "
                        f"— не трогал: "
                        + ", ".join(sorted({_esc(r["status"])
                                            for r in rep["unknown"]})[:6]))
        if fresh_failures:
            body.append("")
            body.append(f"⛔ Маркетплейс отказал: <b>{len(fresh_failures)}</b>")
            for row in fresh_failures[:5]:
                body.append(f"   • {_esc(row['title'])[:30]}: "
                            f"{_esc(row['reason'])[:70]}")
            body.append("<i>Повтор будет позже — с нарастающей паузой.</i>")

        return _card("🔄 <b>АВТО-ВОССТАНОВЛЕНИЕ</b>", body,
                     f"📦 Всего объявлений: {rep['total']}")

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
            return
        except Exception as e:
            logger.warning("Notify failed (user %s): %s", user_id, e)
        # A notification is worth more unformatted than not at all: if the HTML
        # was rejected, strip the tags and send it as plain text.
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await self.bot.send_message(user_id, plain, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Notify plain fallback failed (user %s): %s", user_id, e)

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
                    count, msg = await self._panel_bump(user_id, api)
                    settings["auto_bump"]["last_bump_run"] = now
                    messages.append(f"⬆️ Авто-поднятие: {msg}")

            # --- Position watch ---
            if settings.get("promo_position", {}).get("enabled"):
                note = await self._check_position(user_id, settings, now, api)
                if note:
                    messages.append(note)

            # --- Auto-restore ---
            ar = settings.get("auto_restore", {})
            if ar.get("enabled"):
                interval = float(ar.get("interval_hours", 1) or 1)
                if (now - float(ar.get("last_restore_run", 0) or 0)) / 3600 >= interval:
                    logger.info("Auto-restore for user %s via API", user_id)
                    note = await self._auto_restore(user_id, api, settings, now)
                    if note:
                        messages.append(note)

            # --- Auto-withdraw ---
            aw = settings.get("auto_withdraw", {})
            if aw.get("enabled"):
                interval = float(aw.get("interval_hours", 24) or 24)
                # A withdrawal must not fire on every 30-minute tick: without
                # this guard a balance above the threshold was re-submitted
                # every half hour.
                if (now - float(aw.get("last_run", 0) or 0)) / 3600 >= interval:
                    note = await self._auto_withdraw(user_id, api, settings, now)
                    if note:
                        messages.append(note)

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
                threshold = float(bn.get("threshold", 1000) or 0)
                last_bal = float(bn.get("last_notified_balance", 0.0) or 0)
                try:
                    balance, balance_str = await api.get_balance()
                    # An unreadable balance must not be recorded as 0: that would
                    # re-arm the alert and fire a false "crossed the threshold"
                    # the moment a real number came back.
                    if balance_str != "—":
                        settings["balance_notify"]["last_notified_balance"] = balance
                        # Edge-triggered: fire once as the balance crosses up to
                        # the threshold, re-arm only after it drops back below.
                        if balance >= threshold > last_bal:
                            await self._notify(
                                user_id,
                                _card("🔔 <b>БАЛАНС ДОСТИГ ПОРОГА</b>",
                                      [f"💰 На счету: <b>{balance_str}</b>",
                                       f"🎯 Порог: {_money(threshold)} ₽",
                                       "",
                                       "Можно выводить средства."]),
                                reply_markup=_balance_notify_kb(),
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
                        await self._notify(
                            user_id,
                            await self._daily_report_text(api, settings),
                            reply_markup=_balance_notify_kb())
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
                        count, msg = await self._panel_bump(user_id, api)
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
