"""Autopilot: one screen to set up every automation.

The automations live in several different menus (авто-функции, уведомления,
объявления → премиум продвижение / авто-восстановление, расписание цен), and
several of them silently do nothing until a second screen is visited and a
required field filled in — an empty promotion schedule, or a «Премиум» schedule
with no tariff picked, just sits there looking enabled. That's a lot of places
for a client who only wants the bot working.

This puts every automation in one list with one-tap toggles, fills in sensible
defaults on enable, warns about the ones that still need a decision, and offers
presets that configure a whole working set at once. The detailed menus stay as
they are — this is the fast path, not a replacement.
"""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()


# ---------------------------------------------------------------------------
# Registry: every automation the bot has, declared once.
#   path     — where the on/off flag lives in the settings blob
#   defaults — sibling values filled in when switching it ON, so the automation
#              actually does something without a second visit
#   costs    — spends the seller's money (promotion, withdrawals, price cuts)
#   warn     — returns a message when it's on but still can't run
#   tune     — callback of the detailed screen
# ---------------------------------------------------------------------------

def _no_tariff(s: dict) -> str:
    """«Премиум» promotion can't run until a tariff is chosen."""
    promo = (s.get("auto_bump", {}) or {}).get("promo") or {}
    if not (promo.get("values") or {}):
        return ("нужен тариф: «Объявления» → «Премиум продвижение» → «Тариф»")
    return ""


def _no_showcase_url(s: dict) -> str:
    if not (s.get("promo_position", {}).get("url") or "").strip():
        return "нужна ссылка на витрину: «Проверка позиции» → «Страница витрины»"
    return ""


AUTOMATIONS: list[dict] = [
    # --- общение с покупателем ---
    {
        "key": "reply", "group": "💬 Общение с покупателем",
        "title": "Ответ на новый заказ",
        "path": ("auto_reply", "enabled"),
        "tune": "auto:set:reply_msg",
    },
    {
        "key": "refunded", "group": "💬 Общение с покупателем",
        "title": "Ответ при возврате",
        "path": ("auto_events", "on_refunded", "enabled"),
        "tune": "auto:set:refunded_msg",
    },
    # --- заказы ---
    {
        "key": "accept", "group": "📦 Заказы",
        "title": "Брать заказы в работу",
        "path": ("auto_accept", "enabled"),
    },
    {
        "key": "confirm", "group": "📦 Заказы",
        "title": "Подтверждать заказы",
        "path": ("auto_confirm", "enabled"),
        "defaults": {("auto_confirm", "hours"): 24},
        "note": lambda s: f"через {s.get('auto_confirm', {}).get('hours', 24)} ч",
        "tune": "auto:set:confirm_hours",
    },
    {
        "key": "reminders", "group": "📦 Заказы",
        "title": "Напоминать о зависших",
        "path": ("reminders", "enabled"),
        "defaults": {("reminders", "hours"): 24},
        "note": lambda s: f"через {s.get('reminders', {}).get('hours', 24)} ч",
        "tune": "notif:set:rem_hours",
    },
    # --- товары ---
    {
        "key": "restore", "group": "🛒 Товары",
        "title": "Восстанавливать снятые",
        "path": ("auto_restore", "enabled"),
        "defaults": {("auto_restore", "interval_hours"): 1},
        "note": lambda s: f"раз в {s.get('auto_restore', {}).get('interval_hours', 1)} ч",
        "tune": "selenium:restore:menu",
    },
    {
        "key": "promo_sched", "group": "🛒 Товары",
        "title": "Премиум по расписанию",
        "path": ("bump_schedule", "enabled"),
        "defaults": {("bump_schedule", "times"): ["09:00", "15:00", "21:00"]},
        "note": lambda s: ", ".join(s.get("bump_schedule", {}).get("times", [])) or "не задано",
        "costs": True,
        "warn": _no_tariff,
        "tune": "auto:set:bump_times",
    },
    {
        "key": "promo_position", "group": "🛒 Товары",
        "title": "Следить за позицией",
        "path": ("promo_position", "enabled"),
        "defaults": {("promo_position", "max_position"): 3,
                     ("promo_position", "interval_hours"): 1},
        "note": lambda s: (
            f"ниже {s.get('promo_position', {}).get('max_position', 3)} места"
            + (" — продвигать" if s.get("promo_position", {}).get("auto_promote")
               else " — предупреждать")
        ),
        "warn": _no_showcase_url,
        "tune": "pos:menu",
    },
    {
        "key": "promo_interval", "group": "🛒 Товары",
        "title": "Премиум каждые N часов",
        "path": ("auto_bump", "enabled"),
        "defaults": {("auto_bump", "interval_hours"): 24},
        "note": lambda s: f"каждые {s.get('auto_bump', {}).get('interval_hours', 24)} ч",
        "costs": True,
        "warn": _no_tariff,
        "tune": "selenium:bump:set_interval",
    },
    {
        "key": "price_sched", "group": "🛒 Товары",
        "title": "Ночные скидки",
        "path": ("price_schedule", "enabled"),
        "defaults": {("price_schedule", "from_hour"): 22,
                     ("price_schedule", "to_hour"): 8,
                     ("price_schedule", "percent"): -10.0},
        "note": lambda s: (
            f"{s.get('price_schedule', {}).get('from_hour', 22)}:00–"
            f"{s.get('price_schedule', {}).get('to_hour', 8)}:00, "
            f"{s.get('price_schedule', {}).get('percent', -10):+.0f}%"
        ),
        "costs": True,
        "tune": "pricesched:menu",
    },
    # --- уведомления ---
    {
        "key": "notify_orders", "group": "🔔 Уведомления",
        "title": "Новые заказы",
        "path": ("notify_orders", "enabled"),
    },
    {
        "key": "notify_messages", "group": "🔔 Уведомления",
        "title": "Сообщения покупателей",
        "path": ("notify_messages", "enabled"),
    },
    {
        "key": "complaints", "group": "🔔 Уведомления",
        "title": "Жалобы и споры",
        "path": ("complaint_notify", "enabled"),
    },
    {
        "key": "reviews", "group": "🔔 Уведомления",
        "title": "Новые отзывы",
        "path": ("reviews_monitor", "enabled"),
    },
    {
        "key": "balance", "group": "🔔 Уведомления",
        "title": "Баланс достиг порога",
        "path": ("balance_notify", "enabled"),
        "defaults": {("balance_notify", "threshold"): 1000},
        "note": lambda s: f"от {s.get('balance_notify', {}).get('threshold', 1000):.0f} ₽",
        "tune": "notif:set:balance_thr",
    },
    {
        "key": "daily", "group": "🔔 Уведомления",
        "title": "Итоги дня",
        "path": ("daily_report", "enabled"),
        "defaults": {("daily_report", "hour"): 20},
        "note": lambda s: f"в {s.get('daily_report', {}).get('hour', 20)}:00",
        "tune": "notif:set:report_hour",
    },
    # --- деньги ---
    {
        "key": "withdraw", "group": "💰 Деньги",
        "title": "Выводить баланс",
        "path": ("auto_withdraw", "enabled"),
        "defaults": {("auto_withdraw", "min_amount"): 500,
                     ("auto_withdraw", "interval_hours"): 24},
        "note": lambda s: f"от {s.get('auto_withdraw', {}).get('min_amount', 500):.0f} ₽",
        "costs": True,
        "tune": "selenium:withdraw:menu",
    },
]

_BY_KEY = {a["key"]: a for a in AUTOMATIONS}

# "Рекомендуемое" deliberately leaves out everything that spends money — a
# client shouldn't discover paid «Премиум» promotion by tapping a preset.
_SAFE = ["reply", "refunded", "accept", "confirm", "reminders", "restore",
         "notify_orders", "notify_messages", "complaints", "reviews",
         "balance", "daily"]
# "Максимум" adds the promotion schedule and position watch. Position watch
# only warns by default (auto_promote stays off), so this still spends nothing
# without a further, explicit decision.
_MAX = _SAFE + ["promo_sched", "promo_position"]
_NOTIFY = ["notify_orders", "notify_messages", "complaints", "reviews",
           "balance", "daily"]

PRESETS = {
    "safe": ("✅ Рекомендуемое", _SAFE),
    "max": ("🚀 Максимум продаж", _MAX),
    "notify": ("🔔 Только уведомления", _NOTIFY),
    "off": ("🔴 Выключить всё", []),
}


# ---------------------------------------------------------------------------
# Settings helpers (paths are nested, e.g. auto_events.on_refunded.enabled)
# ---------------------------------------------------------------------------

def _get_path(s: dict, path: tuple):
    node = s
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set_path(s: dict, path: tuple, value) -> None:
    node = s
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[path[-1]] = value


def is_on(s: dict, auto: dict) -> bool:
    return bool(_get_path(s, auto["path"]))


def _baseline_past_slots(bs: dict) -> None:
    """Mark today's already-past promotion slots as done.

    The scheduler catches up on slots missed while the bot was down. Without a
    baseline, enabling the schedule at 10:00 with a 09:00 slot would promote
    immediately — surprising, and «Премиум» costs money. Only slots that come
    due after the change should fire.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    last_runs = bs.setdefault("last_runs", {})
    for slot in bs.get("times", []):
        try:
            h, m = map(int, str(slot).strip().split(":"))
        except (ValueError, AttributeError):
            continue
        if now.replace(hour=h, minute=m, second=0, microsecond=0) <= now:
            last_runs.setdefault(f"{today}_{slot}", now.isoformat())


def _apply_defaults(s: dict, auto: dict) -> None:
    """Fill in what an automation needs to actually run.

    Only writes what's missing, so a client's own tuning survives toggling the
    switch off and on again.
    """
    for path, value in (auto.get("defaults") or {}).items():
        if _get_path(s, path) in (None, "", [], {}, 0):
            _set_path(s, path, value)
    if auto["key"] == "promo_sched":
        _baseline_past_slots(s.setdefault("bump_schedule", {}))


def set_automation(s: dict, auto: dict, on: bool) -> None:
    if on:
        _apply_defaults(s, auto)
    _set_path(s, auto["path"], on)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _autopilot_text(s: dict) -> str:
    on_count = sum(1 for a in AUTOMATIONS if is_on(s, a))
    lines = [
        "⚡ <b>Автопилот</b>",
        f"Работает: <b>{on_count}</b> из {len(AUTOMATIONS)}",
        "",
        "Нажмите на строку, чтобы включить или выключить.",
        "Всё нужное настраивается само — донастройка не обязательна.",
    ]

    active = [a for a in AUTOMATIONS if is_on(s, a)]
    if active:
        lines.append("\n<b>Сейчас включено:</b>")
        for a in active:
            note = a.get("note")
            lines.append(f"• {a['title']}" + (f" — <i>{note(s)}</i>" if note else ""))

    # Anything switched on that still can't run — the exact trap this screen
    # exists to close.
    blocked = []
    for a in active:
        warn = a.get("warn")
        msg = warn(s) if warn else ""
        if msg:
            blocked.append(f"• <b>{a['title']}</b>: {msg}")
    if blocked:
        lines.append("\n⚠️ <b>Включено, но не заработает:</b>")
        lines.extend(blocked)

    if is_on(s, _BY_KEY["promo_sched"]) and is_on(s, _BY_KEY["promo_interval"]):
        lines.append(
            "\n⚠️ Включены два режима продвижения сразу — «Премиум» купится "
            "чаще и дороже, чем нужно. Оставьте один."
        )

    if any(is_on(s, a) for a in AUTOMATIONS if a.get("costs")):
        lines.append("\n💵 — эти функции тратят деньги.")

    return "\n".join(lines)


def _autopilot_kb(s: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    rows: list[int] = []

    b.button(text="✅ Рекомендуемое", callback_data="ap:p:safe")
    b.button(text="🔴 Выключить всё", callback_data="ap:p:off")
    rows.append(2)
    b.button(text="🚀 Максимум продаж", callback_data="ap:p:max")
    b.button(text="🔔 Только уведомления", callback_data="ap:p:notify")
    rows.append(2)

    group = None
    for a in AUTOMATIONS:
        if a["group"] != group:
            group = a["group"]
            b.button(text=f"───  {group}  ───", callback_data="ap:noop")
            rows.append(1)
        mark = "🟢" if is_on(s, a) else "🔴"
        warn = a.get("warn")
        if is_on(s, a) and warn and warn(s):
            mark = "⚠️"
        money = " 💵" if a.get("costs") else ""
        b.button(text=f"{mark} {a['title']}{money}", callback_data=f"ap:t:{a['key']}")
        rows.append(1)

    b.button(text="🛠 Тонкая настройка", callback_data="auto:menu")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    rows.append(2)

    b.adjust(*rows)
    return b.as_markup()


async def _refresh(callback: CallbackQuery, alert: str | None = None) -> None:
    s = get_settings(callback.from_user.id)
    try:
        await callback.message.edit_text(_autopilot_text(s), reply_markup=_autopilot_kb(s))
    except Exception:
        pass  # "message is not modified" when nothing visibly changed
    await callback.answer(alert or "")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "ap:menu")
async def autopilot_menu(callback: CallbackQuery) -> None:
    await _refresh(callback)


@router.callback_query(F.data == "ap:noop")
async def autopilot_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("ap:t:"))
async def autopilot_toggle(callback: CallbackQuery) -> None:
    auto = _BY_KEY.get(callback.data.split(":")[-1])
    if not auto:
        await callback.answer("Не найдено", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    now_on = not is_on(s, auto)
    set_automation(s, auto, now_on)
    save_settings(callback.from_user.id, s)

    if not now_on:
        alert = f"🔴 {auto['title']} — выключено"
    else:
        warn = auto.get("warn")
        blocked = warn(s) if warn else ""
        if blocked:
            alert = f"⚠️ {auto['title']}: {blocked}"
        else:
            note = auto.get("note")
            alert = f"🟢 {auto['title']}" + (f" ({note(s)})" if note else "")
    await _refresh(callback, alert)


@router.callback_query(F.data.startswith("ap:p:"))
async def autopilot_preset(callback: CallbackQuery) -> None:
    preset = PRESETS.get(callback.data.split(":")[-1])
    if not preset:
        await callback.answer("Не найдено", show_alert=True)
        return
    label, keys = preset
    s = get_settings(callback.from_user.id)
    for auto in AUTOMATIONS:
        set_automation(s, auto, auto["key"] in keys)
    save_settings(callback.from_user.id, s)
    await _refresh(callback, f"{label}: включено {len(keys)}")
