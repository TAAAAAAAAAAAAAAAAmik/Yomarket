from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from storage import get_settings, save_settings

router = Router()


class NotifState(StatesGroup):
    waiting_blacklist_name = State()
    waiting_reminder_hours = State()
    waiting_balance_threshold = State()
    waiting_report_hour = State()


def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _notif_text(s: dict) -> str:
    rem = s.get("reminders", {})
    rem_on = rem.get("enabled", False)
    rem_h = rem.get("hours", 24)
    bn = s.get("balance_notify", {})
    bn_on = bn.get("enabled", False)
    bn_thr = bn.get("threshold", 1000)
    dr = s.get("daily_report", {})
    dr_on = dr.get("enabled", False)
    dr_h = dr.get("hour", 20)
    bl: list = s.get("blacklist", [])

    lines = ["🔔 <b>Уведомления</b>\n"]
    lines.append(f"🛒 <b>Новый заказ</b> — "
                 f"{_st(s.get('notify_orders', {}).get('enabled', True))}")
    lines.append(f"💬 <b>Сообщения из чатов</b> — "
                 f"{_st(s.get('notify_messages', {}).get('enabled', True))}")
    lines.append("   <i>Заказы, поддержка и модерация</i>")
    lines.append("")
    lines.append(f"⏰ <b>Напоминания о заказах</b> — {_st(rem_on)}")
    if rem_on:
        lines.append(f"   Через <b>{rem_h} ч</b>")
    lines.append(f"\n🔔 <b>Уведомление о балансе</b> — {_st(bn_on)}")
    if bn_on:
        lines.append(f"   Порог: <b>{bn_thr} ₽</b>")
    lines.append(f"\n📊 <b>Ежедневный отчёт</b> — {_st(dr_on)}")
    if dr_on:
        lines.append(f"   В <b>{dr_h}:00</b>")
    lines.append(f"\n⛔ <b>Чёрный список</b>: {len(bl)} покупателей")
    rm = s.get("reviews_monitor", {})
    rm_on = rm.get("enabled", False)
    lines.append(f"\n⭐ <b>Мониторинг отзывов</b> — {_st(rm_on)}")
    return "\n".join(lines)


def _notif_kb(s: dict):
    rem = s.get("reminders", {})
    rem_on = rem.get("enabled", False)
    bn = s.get("balance_notify", {})
    bn_on = bn.get("enabled", False)
    dr = s.get("daily_report", {})
    dr_on = dr.get("enabled", False)

    orders_on = s.get("notify_orders", {}).get("enabled", True)
    msgs_on = s.get("notify_messages", {}).get("enabled", True)

    b = InlineKeyboardBuilder()
    b.button(text=f"🛒 Новый заказ: {'🟢 вкл' if orders_on else '🔴 выкл'}",
             callback_data="notif:toggle:orders")
    b.button(text=f"💬 Сообщения из чатов: {'🟢 вкл' if msgs_on else '🔴 выкл'}",
             callback_data="notif:toggle:messages")
    b.button(
        text=f"{'🔴 Выкл' if rem_on else '🟢 Вкл'} напоминания",
        callback_data="notif:toggle:reminders",
    )
    if rem_on:
        b.button(text="⏱ Время напоминания", callback_data="notif:set:rem_hours")
    b.button(
        text=f"{'🔴 Выкл' if bn_on else '🟢 Вкл'} уведомление о балансе",
        callback_data="notif:toggle:balance",
    )
    if bn_on:
        b.button(text="💰 Порог баланса", callback_data="notif:set:balance_thr")
    b.button(
        text=f"{'🔴 Выкл' if dr_on else '🟢 Вкл'} ежедневный отчёт",
        callback_data="notif:toggle:daily",
    )
    if dr_on:
        b.button(text="🕐 Время отчёта", callback_data="notif:set:report_hour")
    b.button(text="⛔ Чёрный список", callback_data="notif:blacklist")
    rm = s.get("reviews_monitor", {})
    rm_on = rm.get("enabled", False)
    b.button(
        text=f"{'🔴 Выкл' if rm_on else '🟢 Вкл'} мониторинг отзывов",
        callback_data="notif:toggle:reviews",
    )
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    ui.lay(b)
    return b.as_markup()


def _cancel_kb(back: str = "notif:menu"):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _refresh(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_notif_text(s), reply_markup=_notif_kb(s))
    await callback.answer()


@router.callback_query(F.data == "notif:menu")
async def notif_menu(callback: CallbackQuery) -> None:
    await _refresh(callback)


# ---------------------------------------------------------------------------
# Orders / chat messages
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "notif:toggle:orders")
async def toggle_order_notify(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    n = s.setdefault("notify_orders", {"enabled": True})
    n["enabled"] = not n.get("enabled", True)
    save_settings(callback.from_user.id, s)
    await callback.answer("Новые заказы: "
                          + ("вкл" if n["enabled"] else "выкл"))
    await _refresh(callback)


@router.callback_query(F.data == "notif:toggle:messages")
async def toggle_message_notify(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    n = s.setdefault("notify_messages", {"enabled": True})
    n["enabled"] = not n.get("enabled", True)
    save_settings(callback.from_user.id, s)
    await callback.answer(
        "Сообщения из чатов: " + ("вкл" if n["enabled"] else "выкл")
        + ("" if n["enabled"] else " · жалобы всё равно придут"),
        show_alert=not n["enabled"])
    await _refresh(callback)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "notif:toggle:reminders")
async def toggle_reminders(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    rem = s.setdefault("reminders", {"enabled": False, "hours": 24})
    rem["enabled"] = not rem.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "notif:set:rem_hours")
async def set_rem_hours(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NotifState.waiting_reminder_hours)
    cur = get_settings(callback.from_user.id).get("reminders", {}).get("hours", 24)
    await callback.message.edit_text(
        f"⏰ Через сколько часов напоминать?\nТекущее: <b>{cur} ч</b>\n\nВведите число (1–72):",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(NotifState.waiting_reminder_hours)
async def save_rem_hours(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if not 1 <= hours <= 72:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 1 до 72")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("reminders", {})["hours"] = hours
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Напоминание через <b>{hours} ч</b>")
    await message.answer(_notif_text(s), reply_markup=_notif_kb(s))


# ---------------------------------------------------------------------------
# Balance notify
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "notif:toggle:balance")
async def toggle_balance_notify(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    bn = s.setdefault("balance_notify", {"enabled": False, "threshold": 1000, "last_notified_balance": 0.0})
    bn["enabled"] = not bn.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "notif:set:balance_thr")
async def set_balance_threshold(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NotifState.waiting_balance_threshold)
    cur = get_settings(callback.from_user.id).get("balance_notify", {}).get("threshold", 1000)
    await callback.message.edit_text(
        f"💰 Порог уведомления о балансе (сейчас: <b>{cur} ₽</b>)\n\nВведите сумму:",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(NotifState.waiting_balance_threshold)
async def save_balance_threshold(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("balance_notify", {})["threshold"] = amount
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Порог: <b>{amount} ₽</b>")
    await message.answer(_notif_text(s), reply_markup=_notif_kb(s))


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "notif:toggle:daily")
async def toggle_daily_report(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    dr = s.setdefault("daily_report", {"enabled": False, "hour": 20, "last_report_day": ""})
    dr["enabled"] = not dr.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "notif:set:report_hour")
async def set_report_hour(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NotifState.waiting_report_hour)
    cur = get_settings(callback.from_user.id).get("daily_report", {}).get("hour", 20)
    await callback.message.edit_text(
        f"🕐 В какой час отправлять отчёт? (сейчас: <b>{cur}:00</b>)\n\nВведите час (0–23):",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(NotifState.waiting_report_hour)
async def save_report_hour(message: Message, state: FSMContext) -> None:
    try:
        hour = int((message.text or "").strip())
        if not 0 <= hour <= 23:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 0 до 23")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("daily_report", {})["hour"] = hour
    s["daily_report"]["last_report_day"] = ""
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Отчёт в <b>{hour}:00</b>")
    await message.answer(_notif_text(s), reply_markup=_notif_kb(s))


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def _bl_text(s: dict) -> str:
    bl: list = s.get("blacklist", [])
    lines = ["⛔ <b>Чёрный список покупателей</b>\n"]
    if bl:
        lines.append(f"В списке ({len(bl)}):")
        for name in bl:
            lines.append(f"  • {name}")
    else:
        lines.append("Список пуст.")
    lines.append("\n<i>Покупатели из этого списка не вызывают уведомлений о новых заказах.</i>")
    return "\n".join(lines)


def _bl_kb(s: dict):
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить покупателя", callback_data="bl:add")
    if s.get("blacklist"):
        b.button(text="🗑 Удалить последнего", callback_data="bl:del_last")
        b.button(text="🧹 Очистить всё", callback_data="bl:clear")
    b.button(text="⬅️ Уведомления", callback_data="notif:menu")
    # «Очистить всё» стирает список без вопроса, поэтому в паре с соседней
    # кнопкой не ставится: промахнуться в «Назад» и стереть чужой список —
    # не то, что чинится нажатием ещё раз.
    ui.lay(b, solo={"bl:clear"})
    return b.as_markup()


@router.callback_query(F.data == "notif:toggle:reviews")
async def toggle_reviews_monitor(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    rm = s.setdefault("reviews_monitor", {"enabled": False, "known_review_ids": []})
    rm["enabled"] = not rm.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "notif:blacklist")
async def blacklist_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_bl_text(s), reply_markup=_bl_kb(s))
    await callback.answer()


@router.callback_query(F.data == "bl:add")
async def bl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NotifState.waiting_blacklist_name)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="notif:blacklist")
    await callback.message.edit_text(
        "⛔ Введите имя покупателя:\n\n"
        "<i>Укажите точно так, как отображается в уведомлениях о заказах</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(NotifState.waiting_blacklist_name)
async def bl_add_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Введите имя")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    bl: list = s.setdefault("blacklist", [])
    if name not in bl:
        bl.append(name)
        save_settings(message.from_user.id, s)
        await message.answer(f"✅ <b>{name}</b> добавлен в чёрный список")
    else:
        await message.answer(f"ℹ️ <b>{name}</b> уже в списке")
    await message.answer(_bl_text(s), reply_markup=_bl_kb(s))


@router.callback_query(F.data == "bl:del_last")
async def bl_del_last(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    bl: list = s.get("blacklist", [])
    if bl:
        removed = bl.pop()
        s["blacklist"] = bl
        save_settings(callback.from_user.id, s)
        await callback.answer(f"✅ Удалено: {removed}", show_alert=True)
    else:
        await callback.answer("Список пуст", show_alert=True)
    await callback.message.edit_text(_bl_text(s), reply_markup=_bl_kb(s))


@router.callback_query(F.data == "bl:clear")
async def bl_clear(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["blacklist"] = []
    save_settings(callback.from_user.id, s)
    await callback.answer("✅ Список очищен", show_alert=True)
    await callback.message.edit_text(_bl_text(s), reply_markup=_bl_kb(s))
