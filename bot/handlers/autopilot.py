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

import localtime as _lt
from storage import get_settings, save_settings

router = Router()


# ---------------------------------------------------------------------------
# Перечень: вся автоматика бота, объявленная в одном месте.
#   path     — где в настройках лежит признак «включено»
#   defaults — что дописать рядом при включении, чтобы функция сразу работала,
#              а не требовала второго захода в настройки
#   costs    — тратит деньги продавца (продвижение, вывод, снижение цены)
#   warn     — сообщение о том, почему включённое всё ещё не работает
#   tune     — кнопка подробного экрана
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
    """Позиция считается по каждому товару отдельно — значит пустым не должен
        быть список слежений, а не адрес витрины."""
    from automation.position import watches
    if not watches(dict(s.get("promo_position", {}))):
        return "добавьте товар для наблюдения", "pos:add"
    return "", ""


def _no_autoreply_rules(s: dict) -> tuple[str, str]:
    """Включённые автоответы без единого правила молчат — это надо сказать."""
    conf = s.get("autoreplies", {}) or {}
    rules = [r for r in (conf.get("rules") or []) if r.get("on", True)]
    if not rules and not (conf.get("fallback") or {}).get("on"):
        return "нет ни одного правила", "ar:tpl"
    return "", ""


AUTOMATIONS: list[dict] = [
    # --- общение с покупателем ---
    {
        "key": "answer", "group": "💬 Общение с покупателем",
        "title": "Отвечать на сообщения покупателя",
        "short": "Ответы в чате",
        "path": ("autoreplies", "enabled"),
        "note": lambda s: (
            f"правил: {len([r for r in (s.get('autoreplies', {}).get('rules') or []) if r.get('on', True)])}"
            + (", вне рабочего времени" if s.get("autoreplies", {}).get("quiet_only") else "")
        ),
        "warn": _no_autoreply_rules,
        "tune": "ar:menu",
    },
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
        "key": "restore_instant", "group": "🛒 Товары",
        "title": "Возвращать сразу после продажи",
        "short": "Сразу в продажу",
        "path": ("auto_restore", "instant"),
        "warn": lambda s: (("", "") if s.get("auto_restore", {}).get("enabled")
                           else ("нужно включить восстановление",
                                 "selenium:restore:menu")),
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
        "defaults": {("promo_position", "interval_hours"): 1},
        "note": lambda s: (
            f"{len((s.get('promo_position') or {}).get('watches') or [])} товар(ов)"
            + (" — поднимать" if s.get("promo_position", {}).get("auto_promote")
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
]

_BY_KEY = {a["key"]: a for a in AUTOMATIONS}

# Разделы главного экрана. Семнадцать тумблеров одним списком — это стена
# на телефоне; теперь на главном экране разделы со счётом «включено из
# всего», а сами тумблеры на одно нажатие глубже. Ключи — постоянные
# строки, а не номера: иначе клавиатура, открытая у клиента со вчера,
# отправила бы нажатие не в тот раздел.
GROUP_KEYS = {
    "💬 Общение с покупателем": "chat",
    "📦 Заказы": "orders",
    "🛒 Товары": "goods",
    "🔔 Уведомления": "notify",
    "💰 Баланс": "money",
}
# Надписи кнопок: полные названия хорошо читаются в тексте, но в кнопке
# половинной ширины обрезаются.
GROUP_SHORT = {
    "chat": "💬 Общение", "orders": "📦 Заказы", "goods": "🛒 Товары",
    "notify": "🔔 Уведомления", "money": "💰 Баланс",
}
GROUPS: list[tuple[str, str]] = []          # [(key, title)] in registry order
for _a in AUTOMATIONS:
    _pair = (GROUP_KEYS[_a["group"]], _a["group"])
    if _pair not in GROUPS:
        GROUPS.append(_pair)


def group_items(gkey: str) -> list[dict]:
    return [a for a in AUTOMATIONS if GROUP_KEYS[a["group"]] == gkey]


def group_title(gkey: str) -> str:
    return next((t for k, t in GROUPS if k == gkey), "")

# "Рекомендуемое" deliberately leaves out everything that spends money — a
# client shouldn't discover paid «Премиум» promotion by tapping a preset.
_SAFE = ["reply", "refunded", "accept", "confirm", "reminders", "restore",
         "restore_instant",
         "notify_orders", "notify_messages", "complaints", "reviews",
         "balance", "daily"]
# "Максимум" adds the promotion schedule and position watch. Position watch
# по умолчанию только предупреждает (auto_promote остаётся выключенным),
# так что без отдельного осознанного решения денег это не тратит.
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
# Работа с настройками: пути вложенные, вида auto_events.on_refunded.enabled
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


def _baseline_past_slots(bs: dict, s: dict | None = None) -> None:
    """Mark today's already-past promotion slots as done.

    The scheduler catches up on slots missed while the bot was down. Without a
    baseline, enabling the schedule at 10:00 with a 09:00 slot would promote
    immediately — surprising, and «Премиум» costs money. Only slots that come
    due after the change should fire.
    """
    now = _lt.now(s)
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
    """Дописать то, без чего автоматика включится, но работать не будет.

    Записывается только недостающее: собственная настройка продавца должна
    пережить выключение и повторное включение тумблера.
    """
    for path, value in (auto.get("defaults") or {}).items():
        if _get_path(s, path) in (None, "", [], {}, 0):
            _set_path(s, path, value)
    if auto["key"] == "promo_sched":
        _baseline_past_slots(s.setdefault("bump_schedule", {}), s)


def set_automation(s: dict, auto: dict, on: bool) -> None:
    if on:
        _apply_defaults(s, auto)
    _set_path(s, auto["path"], on)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _blocked(s: dict) -> list[tuple[dict, str, str]]:
    """Что включено, но всё равно не работает: (автоматика, причина, куда идти).

        Включённый тумблер над неработающей функцией — это то самое «бодрое
        сообщение об успехе там, где ничего не происходит».
        """
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
        # По строке на раздел: что включено и как настроено. Сами тумблеры на
        # нажатие глубже, поэтому здесь только сводка.
        lines.append("")
        for gkey, title in GROUPS:
            items = group_items(gkey)
            on = [a for a in items if is_on(s, a)]
            if not on:
                lines.append(f"{title} — <i>выключено</i>")
                continue
            names = ", ".join(a["short"] for a in on)
            lines.append(f"{title} — <b>{len(on)}/{len(items)}</b>: {names}")

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

    # Сразу на экран, где это чинится, вместо того чтобы заставлять продавца
    # искать меню, названное в предупреждении.
    for a, msg, fix in _blocked(s)[:3]:
        target = fix or a.get("tune")
        if target:
            b.button(text=f"⚠️ {a['short']} — {msg}", callback_data=target)
            rows.append(1)

    # Разделы по два в ряд, у каждого видно, сколько в нём включено.
    n = 0
    for gkey, title in GROUPS:
        items = group_items(gkey)
        on = sum(1 for a in items if is_on(s, a))
        b.button(text=f"{GROUP_SHORT.get(gkey, title)}  {on}/{len(items)}",
                 callback_data=f"ap:g:{gkey}")
        n += 1
    rows.extend([2] * (n // 2) + ([1] if n % 2 else []))

    b.button(text="🛠 Тонкая настройка", callback_data="auto:menu")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    rows.append(2)

    b.adjust(*rows)
    return b.as_markup()


# ---------------------------------------------------------------------------
# One section
# ---------------------------------------------------------------------------

def _group_text(s: dict, gkey: str) -> str:
    items = group_items(gkey)
    on = [a for a in items if is_on(s, a)]
    lines = [f"{group_title(gkey)} — включено <b>{len(on)}</b> из {len(items)}"]

    if on:
        lines.append("")
        for a in on:
            note = a.get("note")
            lines.append(f"🟢 {a['title']}" + (f" — <i>{note(s)}</i>" if note else ""))

    off = [a for a in items if not is_on(s, a)]
    if off:
        lines.append("")
        lines.append("<i>Выключено: " + ", ".join(a["short"] for a in off) + "</i>")

    blocked = [(a, m, f) for a, m, f in _blocked(s) if a in items]
    if blocked:
        lines.append("")
        lines.append("⚠️ <b>Не заработает, пока не настроить:</b>")
        for a, msg, _fix in blocked:
            lines.append(f"· <b>{a['title']}</b> — {msg}")

    if any(a.get("costs") for a in items):
        lines.append("")
        lines.append("💵 — тратит деньги.")
    return "\n".join(lines)


def _group_kb(s: dict, gkey: str) -> InlineKeyboardMarkup:
    items = group_items(gkey)
    b = InlineKeyboardBuilder()
    rows: list[int] = []

    for a, msg, fix in [(a, m, f) for a, m, f in _blocked(s) if a in items][:3]:
        target = fix or a.get("tune")
        if target:
            b.button(text=f"⚠️ {a['short']} — {msg}", callback_data=target)
            rows.append(1)

    n = 0
    for a in items:
        mark = "🟢" if is_on(s, a) else "🔴"
        warn = a.get("warn")
        if is_on(s, a) and warn and warn(s)[0]:
            mark = "⚠️"
        money = "💵" if a.get("costs") else ""
        b.button(text=f"{mark} {a['short']}{money}",
                 callback_data=f"ap:x:{gkey}:{a['key']}")
        n += 1
    rows.extend([2] * (n // 2) + ([1] if n % 2 else []))

    # «Включить всё» предлагается только там, где в разделе нет ничего
    # денежного: включить платное продвижение одним нажатием случайно нельзя.
    if not any(a.get("costs") for a in items):
        b.button(text="✅ Включить всё", callback_data=f"ap:ga:{gkey}")
        rows.append(1)
    if any(is_on(s, a) for a in items):
        b.button(text="🔴 Выключить раздел", callback_data=f"ap:gn:{gkey}")
        rows.append(1)

    b.button(text="⬅️ Автопилот", callback_data="ap:menu")
    rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


def _toggle_alert(s: dict, auto: dict, now_on: bool) -> str:
    """Что показать после нажатия тумблера: подтверждение — или чего не хватает."""
    if not now_on:
        return f"🔴 {auto['title']} — выключено"
    warn = auto.get("warn")
    blocked = (warn(s) if warn else ("", ""))[0]
    if blocked:
        return f"⚠️ {auto['title']}: {blocked}"
    note = auto.get("note")
    return f"🟢 {auto['title']}" + (f" ({note(s)})" if note else "")


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
    # Разделителей групп больше нет, но в чатах у клиентов остались старые
    # клавиатуры с ними — отвечаем, чтобы нажатие не висело крутящейся кнопкой.
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

    await _refresh(callback, _toggle_alert(s, auto, now_on))


async def _refresh_group(callback: CallbackQuery, gkey: str,
                         alert: str | None = None) -> None:
    s = get_settings(callback.from_user.id)
    try:
        await callback.message.edit_text(_group_text(s, gkey),
                                         reply_markup=_group_kb(s, gkey))
    except Exception:
        pass
    await callback.answer(alert or "")


@router.callback_query(F.data.startswith("ap:g:"))
async def autopilot_group(callback: CallbackQuery) -> None:
    gkey = callback.data.split(":")[-1]
    if not group_items(gkey):
        await callback.answer("Раздел не найден", show_alert=True)
        return
    await _refresh_group(callback, gkey)


@router.callback_query(F.data.startswith("ap:x:"))
async def autopilot_group_toggle(callback: CallbackQuery) -> None:
    _, _, gkey, key = callback.data.split(":", 3)
    auto = _BY_KEY.get(key)
    if not auto:
        await callback.answer("Не найдено", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    now_on = not is_on(s, auto)
    set_automation(s, auto, now_on)
    save_settings(callback.from_user.id, s)
    await _refresh_group(callback, gkey, _toggle_alert(s, auto, now_on))


@router.callback_query(F.data.startswith("ap:ga:"))
async def autopilot_group_all(callback: CallbackQuery) -> None:
    gkey = callback.data.split(":")[-1]
    items = group_items(gkey)
    if not items:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    for a in items:
        set_automation(s, a, True)
    save_settings(callback.from_user.id, s)
    await _refresh_group(callback, gkey, f"🟢 Включено: {len(items)}")


@router.callback_query(F.data.startswith("ap:gn:"))
async def autopilot_group_none(callback: CallbackQuery) -> None:
    gkey = callback.data.split(":")[-1]
    items = group_items(gkey)
    if not items:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    for a in items:
        set_automation(s, a, False)
    save_settings(callback.from_user.id, s)
    await _refresh_group(callback, gkey, "🔴 Раздел выключен")


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
