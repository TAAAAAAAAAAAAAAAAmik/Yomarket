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

def _no_tariff(s: dict) -> tuple[str, str]:
    """«Премиум» promotion can't run until a tariff is chosen.

    Returns (reason, callback that fixes it) — the fix screen is the tariff
    picker, which is not the same place as the automation's own tune screen.
    """
    promo = (s.get("auto_bump", {}) or {}).get("promo") or {}
    if not (promo.get("values") or {}):
        return "нужен тариф «Премиум»", "promo:setup"
    return "", ""


def _no_showcase_url(s: dict) -> tuple[str, str]:
    if not (s.get("promo_position", {}).get("url") or "").strip():
        return "нужна ссылка на витрину", "pos:url"
    return "", ""


AUTOMATIONS: list[dict] = [
    # --- общение с покупателем ---
    {
        "key": "reply", "group": "💬 Общение с покупателем",
        "title": "Ответ на новый заказ",
        "short": "Ответ на заказ",
        "path": ("auto_reply", "enabled"),
        "tune": "auto:set:reply_msg",
    },
    {
        "key": "refunded", "group": "💬 Общение с покупателем",
        "title": "Ответ при возврате",
        "short": "Ответ на возврат",
        "path": ("auto_events", "on_refunded", "enabled"),
        "tune": "auto:set:refunded_msg",
    },
    # --- заказы ---
    {
        "key": "accept", "group": "📦 Заказы",
        "title": "Брать заказы в работу",
        "short": "Брать в работу",
        "path": ("auto_accept", "enabled"),
    },
    {
        "key": "confirm", "group": "📦 Заказы",
        "title": "Подтверждать заказы",
        "short": "Подтверждать",
        "path": ("auto_confirm", "enabled"),
        "defaults": {("auto_confirm", "hours"): 24},
        "note": lambda s: f"через {s.get('auto_confirm', {}).get('hours', 24)} ч",
        "tune": "auto:set:confirm_hours",
    },
    {
        "key": "reminders", "group": "📦 Заказы",
        "title": "Напоминать о зависших",
        "short": "Напоминания",
        "path": ("reminders", "enabled"),
        "defaults": {("reminders", "hours"): 24},
        "note": lambda s: f"через {s.get('reminders', {}).get('hours', 24)} ч",
        "tune": "notif:set:rem_hours",
    },
    # --- товары ---
    {
        "key": "restore", "group": "🛒 Товары",
        "title": "Восстанавливать снятые",
        "short": "Восстановление",
        "path": ("auto_restore", "enabled"),
        "defaults": {("auto_restore", "interval_hours"): 1},
        "note": lambda s: f"раз в {s.get('auto_restore', {}).get('interval_hours', 1)} ч",
        "tune": "selenium:restore:menu",
    },
    {
        "key": "promo_sched", "group": "🛒 Товары",
        "title": "Премиум по расписанию",
        "short": "Премиум: время",
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
        "short": "Позиция",
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
        "short": "Премиум: интервал",
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
        "short": "Ночные скидки",
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
        "short": "Заказы",
        "path": ("notify_orders", "enabled"),
    },
    {
        "key": "notify_messages", "group": "🔔 Уведомления",
        "title": "Сообщения покупателей",
        "short": "Сообщения",
        "path": ("notify_messages", "enabled"),
    },
    {
        "key": "complaints", "group": "🔔 Уведомления",
        "title": "Жалобы и споры",
        "short": "Жалобы",
        "path": ("complaint_notify", "enabled"),
    },
    {
        "key": "reviews", "group": "🔔 Уведомления",
        "title": "Новые отзывы",
        "short": "Отзывы",
        "path": ("reviews_monitor", "enabled"),
    },
    {
        "key": "balance", "group": "🔔 Уведомления",
        "title": "Баланс достиг порога",
        "short": "Баланс",
        "path": ("balance_notify", "enabled"),
        "defaults": {("balance_notify", "threshold"): 1000},
        "note": lambda s: f"от {s.get('balance_notify', {}).get('threshold', 1000):.0f} ₽",
        "tune": "notif:set:balance_thr",
    },
    {
        "key": "daily", "group": "🔔 Уведомления",
        "title": "Итоги дня",
        "short": "Итоги дня",
        "path": ("daily_report", "enabled"),
        "defaults": {("daily_report", "hour"): 20},
        "note": lambda s: f"в {s.get('daily_report', {}).get('hour', 20)}:00",
        "tune": "notif:set:report_hour",
    },
    # --- деньги ---
    {
        "key": "withdraw", "group": "💰 Деньги",
        "title": "Выводить баланс",
        "short": "Вывод денег",
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

def _blocked(s: dict) -> list[tuple[dict, str, str]]:
    """Automations that are ON but still can't run: (automation, reason, fix)."""
    out = []
    for a in AUTOMATIONS:
        if not is_on(s, a):
            continue
        warn = a.get("warn")
        msg, fix = warn(s) if warn else ("", "")
        if msg:
            out.append((a, msg, fix))
    return out


def _autopilot_text(s: dict) -> str:
    on_count = sum(1 for a in AUTOMATIONS if is_on(s, a))
    lines = [f"⚡ <b>Автопилот</b> — работает {on_count} из {len(AUTOMATIONS)}"]

    if not on_count:
        lines += [
            "",
            "Пока ничего не включено.",
            "Нажмите <b>✅ Рекомендуемое</b> — бот настроится сам "
            "и не потратит ни рубля.",
        ]
    else:
        # One line per group, items inline: seventeen separate bullet lines is
        # a lot of scrolling on a phone, and the grouping is what carries the
        # meaning anyway. The note in brackets is the detail worth checking.
        lines.append("")
        group = None
        parts: list[str] = []

        def flush() -> None:
            if group and parts:
                lines.append(f"{group.split()[0]} " + " · ".join(parts))

        for a in AUTOMATIONS:
            if not is_on(s, a):
                continue
            if a["group"] != group:
                flush()
                group, parts = a["group"], []
            note = a.get("note")
            parts.append(a["short"] + (f" ({note(s)})" if note else ""))
        flush()

    blocked = _blocked(s)
    if blocked:
        lines.append("")
        lines.append("⚠️ <b>Включено, но не заработает:</b>")
        for a, msg, _fix in blocked:
            lines.append(f"· <b>{a['title']}</b> — {msg}")
        lines.append("<i>Кнопки ниже ведут прямо туда, где это чинится.</i>")

    if is_on(s, _BY_KEY["promo_sched"]) and is_on(s, _BY_KEY["promo_interval"]):
        lines.append("")
        lines.append(
            "⚠️ Два режима продвижения сразу — «Премиум» купится чаще и "
            "дороже, чем нужно. Оставьте один."
        )

    if any(is_on(s, a) for a in AUTOMATIONS if a.get("costs")):
        lines.append("")
        lines.append("💵 — тратит деньги.")

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

    # Straight to the screen that fixes it, instead of making the seller hunt
    # for the menu named in the warning.
    for a, msg, fix in _blocked(s)[:3]:
        target = fix or a.get("tune")
        if target:
            b.button(text=f"⚠️ {a['short']} — {msg}", callback_data=target)
            rows.append(1)

    # Two columns, grouped order — the text above carries the group headings,
    # so no separator rows are needed (they were dead taps anyway).
    toggles = 0
    for a in AUTOMATIONS:
        mark = "🟢" if is_on(s, a) else "🔴"
        warn = a.get("warn")
        if is_on(s, a) and warn and warn(s)[0]:
            mark = "⚠️"
        money = "💵" if a.get("costs") else ""
        b.button(text=f"{mark} {a['short']}{money}", callback_data=f"ap:t:{a['key']}")
        toggles += 1
    rows.extend([2] * (toggles // 2) + ([1] if toggles % 2 else []))

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
    # The group separators are gone, but keyboards already sitting in clients'
    # chats still carry them — answer so the tap doesn't hang on a spinner.
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
        blocked = (warn(s) if warn else ("", ""))[0]
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
