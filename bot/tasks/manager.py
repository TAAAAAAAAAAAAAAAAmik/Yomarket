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

# Scheduled promotions are evaluated in the fast 60s loop, so a slot normally
# fires within a minute of its time. This catch-up window lets a slot still run
# if the bot was briefly down at that minute, without replaying long-stale slots
# (e.g. after the bot was off all morning) on the next start.
_BUMP_CATCHUP_SECONDS = 2 * 3600

# How many listings one restore pass will act on. Each candidate costs a stock
# check plus a publish, run one after another while the per-user lock is held —
# and the 60s orders loop needs that same lock. A shop with a few hundred ads
# down would otherwise stall order notifications for minutes. A backlog is
# drained over consecutive passes instead.
_RESTORE_MAX_PER_PASS = 40

# A status the marketplace refused with incorrect_status is skipped, but not
# forever: the ban is inferred from one ad's answer and applied to every ad in
# that state, so it is re-tested after a week in case it was transient or the
# marketplace changed its mind.
_RESTORE_BARRED_TTL = 7 * 86400


async def shop_balance(user_id: int, api) -> tuple[float, str]:
    """The shop's balance → (amount, formatted) — «—» when it can't be read.

    The Integration API has none: /check answers identity only, so every
    caller reading it saw zero and reported it as fact. The panel holds the
    figure, and it is the one withdrawal acts on.
    """
    try:
        amount, text = await api.get_balance()
        if text not in ("—", None):
            return amount, text
    except Exception:
        pass
    from handlers.balance import _panel_balance
    try:
        shown, _err = await _panel_balance(user_id)
    except Exception:
        shown = None
    if shown is None:
        return 0.0, "—"
    try:
        return float(shown), f"{float(shown):.0f} ₽"
    except (TypeError, ValueError):
        return 0.0, str(shown)


def _barred_map(ar: dict) -> dict:
    """{status: expiry} of statuses restore should skip.

    Accepts the older plain-list form, which carried no expiry, and dates it
    from now so an existing install starts re-testing instead of skipping those
    statuses forever.
    """
    raw = ar.get("barred_until")
    if isinstance(raw, dict):
        return {str(k).lower(): float(v or 0) for k, v in raw.items()}
    legacy = ar.get("barred_statuses") or []
    return {str(st).lower(): time.time() + _RESTORE_BARRED_TTL for st in legacy}


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


# Windowed order figures used to live here. They now come from stats_source,
# which reads the panel's ledger and falls back to this same local history, so
# the report and the «Статистика» screen cannot disagree about a day.


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


def _watch_name(w: dict) -> str:
    """What to call a watched listing in a notification."""
    title = str(w.get("title") or "").strip()
    if title:
        return title[:40]
    url = str(w.get("url") or "").split("://")[-1]
    return (url[:34] + "…") if len(url) > 35 else (url or "товар")


def _promo_price(settings: dict) -> int:
    from handlers.selenium_settings import promo_price
    return promo_price(settings)


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


def _shop_label(user_id: int) -> str:
    """The shop the bot's token belongs to, with the active account name.

    Naming both sides is what turns "разные магазины" from a verdict into
    something the seller can act on: adding an account is not the same as
    switching to it, and that is the usual reason the two drift apart.
    """
    from storage import get_active_account, get_shop_name
    shop = get_shop_name(user_id) or "магазин без имени"
    account = get_active_account(user_id)
    return f"{shop}" + (f" · аккаунт «{account}»" if account else "")


async def panel_republish(user_id: int, rows: list[dict]
                          ) -> tuple[list[dict], list[dict]]:
    """Retry refused publishes through the panel. Returns (done, still_failed).

    The Integration API answers incorrect_status for listings sitting in
    «unpublish» — the state it is supposed to undo — so publishing over it is a
    dead end for them. The panel runs the same Nova action a human clicks,
    which is already how promotion and withdrawal work here, so the same route
    is the fallback rather than an invented endpoint.

    Only listings the API refused with incorrect_status come here: a network
    failure or a real validation error should not silently drive a second
    attempt by another road.

    Module level, not a method: the manual «Запустить сейчас» button needs it
    too, and it uses nothing from the task manager.
    """
    from storage import get_panel_creds
    creds = get_panel_creds(user_id)
    if not creds or not creds.get("cookies"):
        return [], [{**r, "panel": "unreached",
                     "reason": f"{r.get('reason', '')} · нет входа в панель"}
                    for r in rows]

    from automation.panel import (panel_find_listing_sync,
                                  panel_publish_item_sync)
    loop = asyncio.get_event_loop()

    # The two systems number the same listing differently, and not every
    # listing lives in the panel's `items` resource — an id from the API
    # answered 404 there. Find each one by title across the panel's own lists,
    # then act on the resource that actually holds it.
    found, trace = await loop.run_in_executor(
        None, panel_find_listing_sync, creds["cookies"],
        [r.get("title") for r in rows])

    # Nothing matched at all, while the panel clearly holds listings: that is
    # not a naming quirk but the panel being logged into a different shop than
    # the API token belongs to. Said plainly, because no amount of matching can
    # fix an account mismatch — and it is the same cause behind the panel
    # answering "нет прав" to promotion.
    # Deliberately not concluding "different shops" from an empty result: this
    # panel keeps listings in more than one resource, so failing to find them
    # means the search missed, not that the shop is wrong. That inference cost
    # several rounds chasing a configuration that was correct all along.
    mismatch = False
    done, failed = [], []
    for row in rows:
        hit = found.get(_title_key(row.get("title")))
        if not hit:
            note = (f" · ⚠️ панель и токен — разные магазины ({_shop_label(user_id)})"
                    if mismatch else
                    f" · в панели не нашёл этот товар (искал: {trace})")
            # Not found is a verdict on where the listing is, not on its
            # status — marked so, so it cannot bar anything.
            failed.append({**row, "panel": "not_found",
                           "reason": f"{row.get('reason', '')}{note}"})
            continue
        res_name, panel_id = hit
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_publish_item_sync,
                                     creds["cookies"], panel_id,
                                     user_id, True, res_name),
                timeout=60)
        except Exception as e:
            ok, msg = False, str(e)[:120]
        if ok:
            done.append(row)
        else:
            # The panel's own markup is stripped: the reason is escaped
            # downstream, so its tags would reach the seller as literal <code>.
            plain = re.sub(r"<[^>]+>", "", str(msg)).replace("\n", " ")
            failed.append({**row, "panel": "refused",
                           "reason": f"{row.get('reason', '')} · панель "
                                     f"{res_name}#{panel_id}: {plain[:300]}"})
    return done, failed


def _title_key(title) -> str:
    """A title reduced to what both systems agree on: letters and digits.

    Titles carry emoji, pipes and spacing that differ between the API and the
    panel, so comparing them raw finds nothing.
    """
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", str(title or "")).lower()


def _match_panel_id(title, by_title: dict) -> str:
    """The panel id for a listing title: exact first, then a containment match
    for the truncation and decoration the two lists apply differently."""
    key = _title_key(title)
    if not key:
        return ""
    if key in by_title:
        return by_title[key]
    for other, pid in by_title.items():
        if key.startswith(other[:24]) or other.startswith(key[:24]):
            return pid
    return ""


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
            await self._maybe_bump_schedule(user_id, settings)

    async def _maybe_bump_schedule(self, user_id: int, settings: dict) -> None:
        """Fire scheduled promotions at their configured times.

        Runs in the fast (60s) loop. In the auto loop this was evaluated every
        30 minutes against a 35-minute window — two periods so close that any
        drift in that loop (a slow panel call, a restart) pushed the tick past
        the window and the slot was silently dropped for the whole day. Here a
        slot fires within a minute of its time, and a catch-up window covers
        brief downtime instead of losing the run.
        """
        bs = settings.get("bump_schedule", {})
        if not (bs.get("enabled") and bs.get("times")):
            return

        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        last_runs: dict = bs.setdefault("last_runs", {})
        dirty = False

        if bs.get("spent_day") != today_str:
            bs["spent_day"] = today_str
            bs["spent_today"] = 0.0
            dirty = True

        # Run-markers are keyed "{date}_{slot}"; drop previous days' so the
        # settings blob doesn't grow without bound.
        stale = [k for k in last_runs if not k.startswith(f"{today_str}_")]
        if stale:
            for k in stale:
                last_runs.pop(k, None)
            dirty = True

        due: list[str] = []
        for slot in bs["times"]:
            try:
                sh, sm = map(int, str(slot).split(":"))
                slot_dt = now_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
            except (ValueError, AttributeError):
                continue
            if last_runs.get(f"{today_str}_{slot}"):
                continue
            due_secs = (now_dt - slot_dt).total_seconds()
            if 0 <= due_secs <= _BUMP_CATCHUP_SECONDS:
                due.append(slot)

        if not due:
            if dirty:
                save_settings(user_id, settings)
            return

        price_per_bump = float(bs.get("price_per_bump", 0) or 0)
        ceiling = float(bs.get("daily_ceiling", 0) or 0)
        spent_today = float(bs.get("spent_today", 0) or 0)

        # Ceiling already reached: mark the due slots handled so this stops
        # being re-checked every minute, and say so once.
        if ceiling > 0 and price_per_bump > 0 and spent_today >= ceiling:
            for slot in due:
                last_runs[f"{today_str}_{slot}"] = now_dt.isoformat()
            save_settings(user_id, settings)
            await self._notify(
                user_id,
                f"⛔ Продвижение ({', '.join(due)}) пропущено: достигнут потолок "
                f"{ceiling:.0f} ₽/день (потрачено {spent_today:.0f} ₽)",
            )
            return

        # One run promotes every listing, so slots that came due together
        # (after downtime) collapse into a single pass — never paying twice.
        try:
            count, msg = await self._panel_bump(user_id)
        except Exception as e:
            logger.warning("Scheduled promotion error for user %s: %s", user_id, e)
            return  # slots stay unmarked, so the next tick retries

        for slot in due:
            last_runs[f"{today_str}_{slot}"] = now_dt.isoformat()
        bs["bumps_total"] = int(bs.get("bumps_total", 0) or 0) + (count or 0)
        bs["last_bump_at"] = now_dt.isoformat()
        if price_per_bump > 0 and count:
            spent_today += count * price_per_bump
            bs["spent_today"] = spent_today
            bs["spent_total"] = float(bs.get("spent_total", 0) or 0) + count * price_per_bump
            cap = f" (потрачено {spent_today:.0f}"
            cap += f"/{ceiling:.0f} ₽)" if ceiling > 0 else " ₽)"
            msg += cap
        settings["bump_schedule"] = bs
        save_settings(user_id, settings)
        await self._notify(user_id, f"⬆️ Продвижение ({', '.join(due)}): {msg}")

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

            # Listings that just sold, for the instant put-back-on-sale below.
            sold_now = False
            sold_ads: set[str] = set()

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
                    # A sale is the moment the listing may have gone off sale —
                    # note it so it can be put back up right now instead of
                    # waiting out the restore interval.
                    sold_now = True
                    sold_ad = _order_field(order, "ad_id", "product_id",
                                           "item_id", "listing_id",
                                           "ad.id", "product.id", "item.id")
                    if sold_ad:
                        sold_ads.add(str(sold_ad))

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
            if sold_ads or sold_now:
                await self._restore_after_sale(user_id, api, settings,
                                               sold_ads, sold_now)

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

    async def _promote_one(self, user_id: int, item_id: str,
                           settings: dict) -> tuple[bool, str]:
        """Pay for one «Премиум» promotion of one listing.

        The position trigger fires for a single listing, so it must pay for
        that one. Routing it through the shop-wide bump would charge for every
        selected listing because one of them slipped — the difference between
        one tariff and thirty.
        """
        from automation.panel import panel_bump_item_sync
        from handlers.selenium_settings import promo_params
        from storage import get_panel_creds

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return False, "нужен вход в панель"
        params = promo_params(settings)
        if not params:
            return False, "не выбран тариф «Премиум»"
        if not item_id:
            return False, "товар не привязан к наблюдению"
        loop = asyncio.get_event_loop()
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_bump_item_sync,
                                     creds["cookies"], str(item_id), user_id,
                                     True, params),
                timeout=120,
            )
        except asyncio.TimeoutError:
            return False, "панель не ответила вовремя"
        except Exception as e:
            return False, str(e)[:120]
        return bool(ok), str(msg)

    async def _check_position(self, user_id: int, settings: dict, now: float,
                              api: YooMarketAPI | None = None) -> str:
        """Walk the watched listings; act on the ones that slipped.

        Returns a notification, or '' to stay quiet. One watch per listing:
        a shop does not have "a position", each listing has its own, on its own
        page. Promotion costs money, so a slip only *pays* when the seller
        switched that on — and even then the cooldown and the daily cap in
        automation/position.py stand between a bad day and an empty card.
        """
        from automation.market import fetch_offers_sync
        from automation.position import (evaluate, is_due, note_promotion,
                                         watches)
        from storage import get_shop_name

        pp = settings.setdefault("promo_position", {})
        ws = watches(pp)
        if not ws:
            return ""
        shop = get_shop_name(user_id) or ""
        loop = asyncio.get_event_loop()
        blocks: list[str] = []
        checked = 0

        for w in ws:
            if not is_due(w, pp, now):
                continue
            url = (w.get("url") or "").strip()
            if not url:
                continue
            checked += 1
            try:
                ok, res = await asyncio.wait_for(
                    loop.run_in_executor(None, fetch_offers_sync, url),
                    timeout=60)
            except Exception as e:
                logger.warning("position check for %s: %s", user_id, e)
                ok, res = False, str(e)[:120]
            if not ok:
                # A page that will not load says nothing about the position, so
                # nothing is claimed and nothing is paid. Repeated failures are
                # reported once — silence would let a dead watch look healthy.
                w["last_check"] = now
                w["fails"] = int(w.get("fails") or 0) + 1
                if w["fails"] == 3:
                    blocks.append(
                        f"⚠️ <b>{_esc(_watch_name(w))}</b>\n"
                        f"Страница не читается 3 проверки подряд: "
                        f"{_esc(str(res))[:120]}")
                continue
            w["fails"] = 0

            verdict = evaluate(w, res["offers"], shop=shop, pp=pp, now=now)
            lines = list(verdict.lines)
            if verdict.promote:
                ok_paid, msg = await self._promote_one(
                    user_id, str(w.get("item_id") or ""), settings)
                if ok_paid:
                    note_promotion(w, now)
                    w.pop("last_fail", None)
                    price = _promo_price(settings)
                    lines.append("⭐ Поднял" + (f" · {price} ₽" if price else "")
                                 + f": {_esc(str(msg))[:160]}")
                else:
                    # A listing that stays down retries every hour, and a
                    # misconfiguration — no tariff, nothing bound — fails the
                    # same way every time. Say it once, and again only when the
                    # answer changes.
                    reason = str(msg)[:160]
                    if w.get("last_fail") != reason:
                        w["last_fail"] = reason
                        lines.append(f"⚠️ Не поднял: {_esc(reason)}")
            if lines:
                head = f"<b>{_esc(_watch_name(w))}</b>"
                if verdict.found:
                    head += f" — {verdict.pos} место"
                blocks.append(head + "\n" + "\n".join(lines))

        if not blocks:
            return ""
        # An over-long send fails whole — including the part that says money was
        # spent — so the message is kept well inside Telegram's 4096 as the
        # watch list grows. Whole blocks are dropped, never a cut mid-tag.
        kept, used = [], 0
        for block in blocks:
            if used + len(block) > 2800 and kept:
                kept.append(f"…и ещё {len(blocks) - len(kept)} — /pos_debug")
                break
            kept.append(block)
            used += len(block)
        return _card("📍 <b>ПОЗИЦИЯ НА ВИТРИНЕ</b>", kept,
                     f"🏪 {_esc(shop)}   ·   проверено: {checked}")

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
            balance, balance_str = await shop_balance(user_id, api)
        except Exception as e:
            logger.warning("Auto-withdraw balance for %s: %s", user_id, e)
            return ""
        if balance_str == "—":
            return ""                                   # unknown balance, skip
        if balance < min_amount:
            return ""                                   # below threshold, quiet

        sent_amount = float(int(balance))
        more_left = False
        # The panel is the only route: the Integration API has no withdrawal
        # endpoint, and the screen says so. Defaulting to "api" meant the
        # automatic run reported «не удалось получить баланс через API» — an
        # error about a road that does not exist.
        method = aw.get("method") or "panel"
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
            # The wizard stores the method and requisites without an amount.
            # The whole balance used to be submitted, which a shop holding more
            # than the per-payout ceiling can never pass — the panel states
            # «Максимальная сумма: 75 000 ₽» and rejects anything above it. The
            # limits are read from the form rather than hardcoded, so a change
            # on their side follows along.
            from automation.panel import panel_withdraw_limits_sync
            loop = asyncio.get_event_loop()
            lim = {}
            # With the stored payment system, so the form reshapes and states
            # its limits — without it «Сумма» is not even visible yet.
            chosen = {k: v for k, v in values.items() if k == "system"}
            try:
                lim_ok, got = await asyncio.wait_for(
                    loop.run_in_executor(None, panel_withdraw_limits_sync,
                                         creds["cookies"], balance_id, chosen),
                    timeout=45)
                if lim_ok and isinstance(got, dict):
                    lim = got
            except Exception as e:
                logger.info("Withdraw limits for %s: %s", user_id, e)

            cap = float(lim.get("max") or 0)
            floor = float(lim.get("min") or 0)
            amount = int(min(balance, cap) if cap else balance)
            if floor and amount < floor:
                return (f"💸 Авто-вывод: на счёте {balance:.0f} ₽, а минимум "
                        f"для выплаты — {floor:.0f} ₽. Жду накопления.")
            left = balance - amount

            values = {**values, "amount": amount}
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_withdraw_sync,
                                     creds["cookies"], balance_id, action_key,
                                     values, user_id, True),
                timeout=60)
            if ok:
                fee = max(amount * float(lim.get("fee_pct") or 0) / 100,
                          float(lim.get("fee_min") or 0))
                # The panel's own words are kept, not replaced. Accepting a
                # payout is not the same as paying it: the answer can be
                # «ссылка для подтверждения реквизитов отправлена на почту»,
                # and reporting «выведено» over that would claim money moved
                # when it is waiting on the seller's inbox.
                msg = (f"заявка на {amount:.0f} ₽"
                       + (f" (придёт ≈{amount - fee:.0f} ₽ после комиссии)"
                          if fee else "")
                       + f" — {msg}" if msg else f"заявка на {amount:.0f} ₽")
                if left >= 1:
                    msg += (f". Остаток {left:.0f} ₽ — за раз не больше "
                            f"{cap:.0f} ₽, продолжу в следующий заход")
                sent_amount = float(amount)
                # More than one payout's worth is left: come back next tick
                # instead of waiting out the interval.
                more_left = left >= max(floor, 1)
        else:
            # An explicitly stored "api" method from before this was known.
            aw["enabled"] = False
            aw["last_run"] = now
            aw["last_result"] = "вывода через API нет"
            return ("💸 Авто-вывод выключен: у Юмаркета нет вывода через API. "
                    "Настройте его через панель: «Баланс» → «Автовывод» → "
                    "«Настроить вывод через панель».")

        # Zero, not `now`, when a payout ceiling left money behind: the next
        # tick should continue rather than wait out the whole interval.
        aw["last_run"] = 0 if more_left else now
        aw["last_result"] = msg
        hist = settings.setdefault("withdrawal_history", [])
        # What was actually sent, not what the balance happened to be — the
        # ceiling means those differ, and the history is what the seller
        # reconciles against.
        hist.insert(0, {"amount": sent_amount, "ts": now,
                        "type": "auto",
                        "status": "requested" if ok else "failed"})
        del hist[100:]
        return _card("💸 <b>АВТО-ВЫВОД</b>",
                     [f"💰 Баланс был: <b>{balance_str}</b>", "",
                      ("✅ " if ok else "⚠️ ") + _esc(msg)])

    async def _daily_report_text(self, user_id: int, api: YooMarketAPI,
                                 settings: dict) -> str:
        """The end-of-day summary, as today's numbers rather than lifetime ones.

        The figures come from the panel's own ledger, with the bot's order
        history as the fallback: built from local tracking alone, the report
        counted only what the poller happened to witness, so a restart or a
        sale made before the bot came up simply vanished from the day.

        The balance line used to read an undefined name — the exception was
        caught and every report went out saying «—». It is passed in now.
        """
        from stats_source import LOCAL, day_start, events_for, summarize

        events, source, panel_err = await events_for(user_id, settings, force=True)
        d0 = day_start()
        st = summarize(events, d0)

        spent_today = st["spend"]
        if not spent_today and source == LOCAL:
            bs = settings.get("bump_schedule", {})
            if bs.get("spent_day") == datetime.now().strftime("%Y-%m-%d"):
                spent_today = float(bs.get("spent_today", 0) or 0)

        try:
            _bal, balance_str = await shop_balance(user_id, api)
        except Exception as e:
            logger.warning("Daily report balance for %s: %s", user_id, e)
            balance_str = "—"

        net = st["revenue"] - spent_today
        body = [
            f"🛒 Заказов за день: <b>{st['orders']}</b>",
            f"✅ Продаж: <b>{st['sales']}</b>"
            + (f"   ↩️ Возвраты: {st['refunded']}" if st['refunded'] else ""),
            "",
            f"💵 Выручка: <b>{_money(st['revenue'])} ₽</b>",
        ]
        if spent_today:
            body.append(f"⬆️ Продвижение: −{_money(spent_today)} ₽")
            body.append(f"🟰 Чистыми: <b>{_money(net)} ₽</b>")
        if st["payout_count"]:
            body.append(f"💸 Выведено: <b>{_money(st['payouts'])} ₽</b>")
        body.append("")
        body.append(f"💰 Баланс сейчас: <b>{balance_str}</b>")
        if not st["orders"]:
            body.append("")
            body.append("<i>Сегодня продаж не было.</i>")
        if source == LOCAL:
            body.append("")
            body.append("<i>Считано по истории бота — панель не ответила"
                        + (f" ({_esc(panel_err)})" if panel_err else "") + ".</i>")

        return _card("📊 <b>ИТОГИ ДНЯ</b>", body,
                     f"🗓 {datetime.now().strftime('%d.%m.%Y')}")

    async def _panel_republish(self, user_id: int, rows: list[dict]
                               ) -> tuple[list[dict], list[dict]]:
        return await panel_republish(user_id, rows)

    async def _restore_after_sale(self, user_id: int, api: YooMarketAPI,
                                  settings: dict, sold_ads: set, sold_now: bool
                                  ) -> None:
        """Put a listing back on sale the moment it sells.

        The scheduled pass runs on an interval — an hour by default — so a
        listing that sold out sat off-sale for up to that long. A sale is the
        one event that reliably means "this may have gone down", so it drives
        the restore directly, in the 60s orders loop.

        Two paths. When the order says which listing it was, that listing alone
        is republished: one detail fetch and one publish, no full sweep, and no
        throttle needed. When the order carries no listing id, a normal restore
        pass is used instead — that costs a full listing fetch, so it is capped
        to once every few minutes rather than running every time an order lands
        during a rush.
        """
        ar = settings.setdefault("auto_restore", {})
        if not (ar.get("enabled") and ar.get("instant", True)):
            return

        now = time.time()
        from handlers.panel_items import _deleted_ids
        skip = _deleted_ids(user_id) | {
            aid for aid, f in (ar.get("failures") or {}).items()
            if float(f.get("until", 0) or 0) > now}

        back: list[str] = []
        for aid in sorted(sold_ads - set(skip)):
            try:
                ad = await api.get_ad(aid)
                inner = ad.get("data") or ad
                state = api._ad_state(inner)
                if state not in api._DOWN:
                    continue                       # still live, nothing to do
                if ar.get("require_stock", True):
                    has, _note = await api.ad_stock(aid, inner)
                    if not has:
                        continue                   # sold out — nothing to sell
                title = str(inner.get("title") or inner.get("name") or f"#{aid}")
                try:
                    await api.restore_ad(aid)
                except Exception as e:
                    # Same dead end as the scheduled pass: the API won't publish
                    # out of «unpublish». Take the panel route instead.
                    if "incorrect_status" not in str(e):
                        raise
                    done, _still = await self._panel_republish(
                        user_id, [{"id": str(aid), "title": title, "reason": str(e)}])
                    if not done:
                        continue
                back.append(title)
            except Exception as e:
                logger.info("Instant restore of %s: %s", aid, e)

        if back:
            ar["restored_total"] = int(ar.get("restored_total", 0) or 0) + len(back)
            await self._notify(
                user_id,
                _card("♻️ <b>СНОВА В ПРОДАЖЕ</b>",
                      [f"• {_esc(t)[:44]}" for t in back[:8]]
                      + ["", "<i>Вернул сразу после продажи.</i>"]))
            return

        # No listing id in the order — fall back to a normal pass, rate-limited.
        if sold_now and now - float(ar.get("last_instant_run", 0) or 0) >= 300:
            ar["last_instant_run"] = now
            note = await self._auto_restore(user_id, api, settings, now)
            if note:
                await self._notify(user_id, note)

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

        # Statuses barred by a past incorrect_status, minus the ones whose ban
        # has aged out and is due for a re-test.
        barred_until: dict = _barred_map(ar)
        active_barred = [st for st, until in barred_until.items() if until > now]

        try:
            rep = await api.restore_ads(
                require_stock=bool(ar.get("require_stock", True)),
                skip_ids=skip,
                limit=_RESTORE_MAX_PER_PASS,
                skip_statuses=active_barred)
        except Exception as e:
            logger.warning("Auto-restore for %s: %s", user_id, e)
            return f"🔄 Авто-восстановление не отработало: {_esc(str(e)[:120])}"

        ar["last_restore_run"] = now

        # Anything the API refused because of the listing's state gets a second
        # try through the panel, which can make that transition when the API
        # cannot. Recovered listings move into `restored` and are reported as
        # normal successes — the route taken is an implementation detail.
        via_panel: list[dict] = []
        retryable = [r for r in rep["failed"]
                     if "incorrect_status" in str(r.get("reason", ""))]
        if retryable:
            via_panel, still = await self._panel_republish(user_id, retryable)
            rep["restored"] += via_panel
            rep["failed"] = [r for r in rep["failed"] if r not in retryable] + still

        for row in rep["restored"]:
            failures.pop(str(row["id"]), None)      # it worked; forget the past
        ar["restored_total"] = int(ar.get("restored_total", 0)) + len(rep["restored"])

        # Learn which statuses the marketplace itself refuses to publish, so
        # the next pass skips them instead of re-asking every hour. This is the
        # answer coming from the marketplace, not a guess about its states.
        # Each ban carries an expiry: it is inferred from a single ad and
        # applied to every ad in that state, so it must be able to heal.
        #
        # A status the panel just published from is not barred, and any
        # standing ban on it is lifted. The API refusing `unpublish` is the
        # normal case the panel fallback exists to handle — barring it would
        # switch off restore for the one state it is built around, and every
        # later pass would skip in silence.
        # A pass that never got a verdict out of the panel — no login, or the
        # listing not located there — says nothing about any status. Bans
        # standing from such a pass were never evidence, so they are dropped
        # rather than holding restore back for a week after the cause is fixed.
        if any(r.get("panel") in ("unreached", "not_found")
               for r in rep["failed"]):
            barred_until.clear()
        recovered = {str(r.get("status") or "").lower() for r in via_panel}
        for st in recovered:
            barred_until.pop(st, None)
        for row in rep["failed"]:
            reason = str(row.get("reason", ""))
            if "incorrect_status" not in reason:
                continue
            # Only the panel actually refusing the action is evidence about
            # the status. Matching on the reason text was tried and let «в
            # панели не нашёл этот товар» through, which barred `unpublish` —
            # the one state restore exists for — over a lookup problem.
            if row.get("panel") != "refused":
                continue
            st = str(row.get("status") or "").lower()
            if st and st not in recovered:
                barred_until[st] = now + _RESTORE_BARRED_TTL
        ar["barred_until"] = barred_until
        ar.pop("barred_statuses", None)          # superseded by the dated form

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

        # A status in neither list is the signal that the marketplace has a
        # state this code doesn't know about — exactly how «unpublish» hid for
        # so long. Reporting it only when something *else* happened defeats the
        # purpose, so a status not seen before counts as news in its own right.
        # Each distinct status is announced once, not every hour.
        # Taken down by hand: the marketplace will not publish these back, so
        # they are named once and then left alone. Reporting them every pass
        # would be an hourly error about something only the seller can undo.
        told_manual = set(str(x) for x in (ar.get("told_manual") or []))
        manual_new = [r for r in (rep.get("manual") or [])
                      if str(r["id"]) not in told_manual]
        if manual_new:
            ar["told_manual"] = sorted(told_manual
                                       | {str(r["id"]) for r in manual_new})

        seen_unknown = set(ar.get("seen_unknown") or [])
        new_unknown = sorted({str(r["status"]) for r in rep.get("unknown") or []}
                             - seen_unknown)
        if new_unknown:
            ar["seen_unknown"] = sorted(seen_unknown | set(new_unknown))

        # Listings left over because the pass is capped; picked up next round
        # rather than after another full interval.
        backlog = max(0, int(rep.get("candidates", 0)) - _RESTORE_MAX_PER_PASS)
        if backlog:
            ar["last_restore_run"] = 0

        summary = (f"поднято {len(rep['restored'])}, "
                   f"без остатков {len(rep['no_stock'])}, "
                   f"отказов {len(rep['failed'])}")
        ar["last_result"] = summary

        if (not rep["restored"] and not fresh_failures and not new_unknown
                and not manual_new):
            return ""                                # nothing happened, say nothing

        body: list[str] = []
        if rep["restored"]:
            body.append(f"✅ Снова в продаже: <b>{len(rep['restored'])}</b>")
            for row in rep["restored"][:8]:
                body.append(f"   • {_esc(row['title'])[:40]}")
            if via_panel:
                body.append(f"   <i>из них через панель: {len(via_panel)}</i>")
            body.append("")
            body.append("<i>Опубликованное уходит на модерацию — "
                        "статус сменится после проверки.</i>")
        if rep["no_stock"]:
            body.append("")
            body.append(f"📦 Без остатков: <b>{len(rep['no_stock'])}</b> "
                        f"— их публиковать нечем")
            for row in rep["no_stock"][:5]:
                body.append(f"   • {_esc(row['title'])[:34]} — {_esc(row.get('note'))}")
        if manual_new:
            body.append("")
            body.append(f"✋ <b>Сняты вручную: {len(manual_new)}</b> — "
                        f"Юмаркет возвращает в продажу только истёкшие, "
                        f"эти нужно вернуть самому на сайте:")
            for row in manual_new[:6]:
                body.append(f"   • {_esc(row['title'])[:40]}")

        if manual_new:
            body.append("")
            body.append(f"✋ <b>Сняты вручную: {len(manual_new)}</b>")
            for row in manual_new[:6]:
                body.append(f"   • {_esc(row['title'])[:40]}")
            body.append("<i>Юмаркет возвращает в продажу только истёкшие "
                        "объявления. Снятые вручную нужно вернуть самому — "
                        "бот их больше не трогает.</i>")

        if new_unknown:
            affected = [r for r in rep["unknown"] if str(r["status"]) in new_unknown]
            body.append("")
            body.append("❔ <b>Незнакомый статус</b> — не трогал: "
                        + ", ".join(_esc(x) for x in new_unknown[:6]))
            for row in affected[:4]:
                body.append(f"   • {_esc(row['title'])[:38]}")
            body.append("<i>Напишите нам этот статус, если такие товары "
                        "должны возвращаться в продажу.</i>")
        if fresh_failures:
            body.append("")
            body.append(f"⛔ Маркетплейс отказал: <b>{len(fresh_failures)}</b>")
            for row in fresh_failures[:5]:
                # The status is the whole point of an incorrect_status refusal
                # — without it the seller can't tell what to change.
                st = _esc(str(row.get("status") or "?"))
                body.append(f"   • {_esc(row['title'])[:26]}  <code>{st}</code>\n"
                            f"     {_esc(row['reason'])[:400]}")
            body.append("<i>Повтор будет позже — с нарастающей паузой.</i>")

        if backlog:
            body.append("")
            body.append(f"⏳ Ещё <b>{backlog}</b> в очереди — продолжу "
                        f"в следующий заход.")

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
                    balance, balance_str = await shop_balance(user_id, api)
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
                    try:
                        await self._notify(
                            user_id,
                            await self._daily_report_text(user_id, api, settings),
                            reply_markup=_balance_notify_kb())
                        # Marked only once it actually went out. Marking first
                        # meant a panel timeout or a failed send burned the day:
                        # the report was recorded as delivered and never
                        # retried, which is «статистика не приходит» exactly.
                        settings["daily_report"]["last_report_day"] = today_str
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

            # --- Bump scheduler ---
            # Moved to the fast 60s loop (_maybe_bump_schedule) so slots fire at
            # their exact time instead of drifting with this 30-min loop.

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
