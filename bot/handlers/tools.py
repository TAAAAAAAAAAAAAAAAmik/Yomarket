from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "confirmed": "✅ Выполнен",
    "completed": "✅ Выполнен",
    "done": "✅ Выполнен",
    "refunded": "↩️ Возврат",
    "cancelled": "↩️ Отменён",
    "returned": "↩️ Возврат",
    "active": "⏳ Активен",
    "new": "🆕 Новый",
    "work": "🔧 В работе",
}


class ToolsState(StatesGroup):
    waiting_blacklist_name = State()
    waiting_reminder_hours = State()
    waiting_crm_buyer = State()
    waiting_crm_note = State()
    waiting_qr_text = State()


# ---------------------------------------------------------------------------
# Tools sub-menu
# ---------------------------------------------------------------------------

def _tools_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📤 Экспорт заказов", callback_data="tools:export")
    b.button(text="⛔ Чёрный список", callback_data="tools:blacklist")
    b.button(text="⭐ Отзывы", callback_data="tools:reviews")
    b.button(text="⏰ Напоминания", callback_data="tools:reminders")
    b.button(text="📝 Заметки о покупателях", callback_data="tools:crm")
    b.button(text="💬 Быстрые ответы", callback_data="tools:quick_replies")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "tools:menu")
async def tools_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🛠 <b>Инструменты</b>\n\nВыберите функцию:",
        reply_markup=_tools_kb(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Export orders
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "tools:export")
async def export_orders(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("⏳ Генерирую файл...", show_alert=False)

    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    all_orders: list[dict] = []
    if api:
        try:
            data = await api.get_orders()
            all_orders = data.get("data") or data.get("items") or []
        except Exception as e:
            logger.warning("Export: API error: %s", e)

    lines = [
        "YooMarket — Экспорт заказов",
        f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"Всего заказов: {len(known_orders)}",
        "=" * 40,
        "",
    ]

    if all_orders:
        for order in all_orders:
            oid = str(order.get("id", ""))
            title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
            buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
            price = order.get("price") or order.get("total") or "—"
            status_raw = str(order.get("status", ""))
            status = _STATUS_MAP.get(status_raw, status_raw)
            created = order.get("created_at") or order.get("date") or ""
            lines += [
                f"ID: {oid}",
                f"Товар: {title}",
                f"Покупатель: {buyer}",
                f"Сумма: {price} ₽",
                f"Статус: {status}",
                f"Создан: {str(created)[:19] if created else '—'}",
                "-" * 30,
                "",
            ]
    else:
        for oid, status_raw in known_orders.items():
            det = order_details.get(oid, {})
            status = _STATUS_MAP.get(status_raw, status_raw)
            lines += [
                f"ID: {oid}",
                f"Товар: {det.get('title', '—')}",
                f"Покупатель: {det.get('buyer', '—')}",
                f"Сумма: {det.get('price', '—')} ₽",
                f"Статус: {status}",
                "-" * 30,
                "",
            ]

    content = "\n".join(lines).encode("utf-8")
    file = BufferedInputFile(content, filename="orders_export.txt")

    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="tools:menu")

    await callback.message.answer_document(
        file,
        caption=f"📤 <b>Экспорт заказов</b>\nВсего: <b>{len(known_orders)}</b>",
        reply_markup=b.as_markup(),
    )


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
    lines.append(
        "\n<i>Покупатели из этого списка не вызывают уведомлений о новых заказах.</i>"
    )
    return "\n".join(lines)


def _bl_kb(s: dict):
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить покупателя", callback_data="bl:add")
    if s.get("blacklist"):
        b.button(text="🗑 Удалить последнего", callback_data="bl:del_last")
        b.button(text="🧹 Очистить всё", callback_data="bl:clear")
    b.button(text="⬅️ Назад", callback_data="tools:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "tools:blacklist")
async def blacklist_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_bl_text(s), reply_markup=_bl_kb(s))
    await callback.answer()


@router.callback_query(F.data == "bl:add")
async def bl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ToolsState.waiting_blacklist_name)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="tools:blacklist")
    await callback.message.edit_text(
        "⛔ Введите имя покупателя:\n\n"
        "<i>Укажите точно так, как отображается в уведомлениях о заказах</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(ToolsState.waiting_blacklist_name)
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


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "tools:reviews")
async def reviews_menu(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю отзывы...")

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="tools:reviews")
    b.button(text="⬅️ Назад", callback_data="tools:menu")
    b.adjust(1)

    if not api:
        await callback.message.edit_text(
            "⭐ <b>Отзывы</b>\n\n❌ API токен не настроен",
            reply_markup=b.as_markup(),
        )
        await callback.answer()
        return

    try:
        data = await api.get_reviews()
        reviews: list[dict] = data.get("data") or data.get("items") or []
        if not reviews:
            text = "⭐ <b>Отзывы</b>\n\nОтзывов пока нет."
        else:
            lines = [f"⭐ <b>Отзывы</b> (показаны последние {min(len(reviews), 10)})\n"]
            for r in reviews[:10]:
                rating = r.get("rating") or r.get("score") or 0
                author = r.get("author") or r.get("buyer_name") or "Покупатель"
                comment = (r.get("text") or r.get("comment") or "—")[:120]
                try:
                    stars = "⭐" * int(rating)
                except (TypeError, ValueError):
                    stars = str(rating)
                lines.append(f"{stars or '—'} <b>{author}</b>")
                lines.append(f"   <i>{comment}</i>\n")
            text = "\n".join(lines)
    except Exception as e:
        text = f"⭐ <b>Отзывы</b>\n\n❌ Ошибка: {e}"

    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def _rem_text(s: dict) -> str:
    r = s.get("reminders", {})
    on = r.get("enabled", False)
    hours = r.get("hours", 24)
    return (
        f"⏰ <b>Напоминания о заказах</b> — {'🟢 ВКЛ' if on else '🔴 ВЫКЛ'}\n\n"
        f"Если заказ ждёт подтверждения более <b>{hours} ч</b> — бот пришлёт напоминание.\n\n"
        "<i>Каждый заказ напоминает только один раз.</i>"
    )


def _rem_kb(s: dict):
    r = s.get("reminders", {})
    on = r.get("enabled", False)
    b = InlineKeyboardBuilder()
    b.button(text="🔴 Выключить" if on else "🟢 Включить", callback_data="rem:toggle")
    b.button(text="⏱ Изменить время", callback_data="rem:set_hours")
    b.button(text="⬅️ Назад", callback_data="tools:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "tools:reminders")
async def reminders_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_rem_text(s), reply_markup=_rem_kb(s))
    await callback.answer()


@router.callback_query(F.data == "rem:toggle")
async def rem_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    rem = s.setdefault("reminders", {"enabled": False, "hours": 24})
    rem["enabled"] = not rem.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_rem_text(s), reply_markup=_rem_kb(s))
    await callback.answer()


@router.callback_query(F.data == "rem:set_hours")
async def rem_set_hours_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ToolsState.waiting_reminder_hours)
    s = get_settings(callback.from_user.id)
    cur = s.get("reminders", {}).get("hours", 24)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="tools:reminders")
    await callback.message.edit_text(
        f"⏰ Через сколько часов напоминать?\n\nТекущее: <b>{cur} ч</b>\n\nВведите число от 1 до 72:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(ToolsState.waiting_reminder_hours)
async def rem_set_hours_save(message: Message, state: FSMContext) -> None:
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
    await message.answer(_rem_text(s), reply_markup=_rem_kb(s))


# ---------------------------------------------------------------------------
# CRM — buyer notes
# ---------------------------------------------------------------------------

def _crm_text(s: dict) -> str:
    notes: dict = s.get("buyer_notes", {})
    lines = ["📝 <b>Заметки о покупателях</b>\n"]
    if notes:
        for buyer, note in list(notes.items())[:20]:
            lines.append(f"👤 <b>{buyer}</b>\n   {note[:80]}")
    else:
        lines.append("Заметок пока нет.")
    return "\n\n".join(lines)


def _crm_kb(s: dict):
    notes: dict = s.get("buyer_notes", {})
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить заметку", callback_data="crm:add")
    if notes:
        b.button(text="🗑 Удалить последнюю", callback_data="crm:del_last")
        b.button(text="🧹 Очистить всё", callback_data="crm:clear")
    b.button(text="⬅️ Назад", callback_data="tools:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "tools:crm")
async def crm_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_crm_text(s), reply_markup=_crm_kb(s))
    await callback.answer()


@router.callback_query(F.data == "crm:add")
async def crm_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ToolsState.waiting_crm_buyer)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="tools:crm")
    await callback.message.edit_text(
        "📝 Введите имя покупателя:\n\n"
        "<i>Укажите так же, как отображается в заказах</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(ToolsState.waiting_crm_buyer)
async def crm_buyer_received(message: Message, state: FSMContext) -> None:
    buyer = (message.text or "").strip()
    if not buyer:
        await message.answer("❌ Введите имя покупателя")
        return
    await state.update_data(crm_buyer=buyer)
    await state.set_state(ToolsState.waiting_crm_note)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="tools:crm")
    await message.answer(
        f"📝 Покупатель: <b>{buyer}</b>\n\nВведите заметку:",
        reply_markup=b.as_markup(),
    )


@router.message(ToolsState.waiting_crm_note)
async def crm_note_save(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    if not note:
        await message.answer("❌ Введите текст заметки")
        return
    data = await state.get_data()
    buyer = data.get("crm_buyer", "")
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("buyer_notes", {})[buyer] = note
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Заметка сохранена для <b>{buyer}</b>")
    await message.answer(_crm_text(s), reply_markup=_crm_kb(s))


@router.callback_query(F.data == "crm:del_last")
async def crm_del_last(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    notes: dict = s.get("buyer_notes", {})
    if notes:
        last_key = list(notes.keys())[-1]
        del notes[last_key]
        save_settings(callback.from_user.id, s)
        await callback.answer(f"✅ Удалено: {last_key}", show_alert=True)
    else:
        await callback.answer("Заметок нет", show_alert=True)
    await callback.message.edit_text(_crm_text(s), reply_markup=_crm_kb(s))


@router.callback_query(F.data == "crm:clear")
async def crm_clear(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["buyer_notes"] = {}
    save_settings(callback.from_user.id, s)
    await callback.answer("✅ Заметки очищены", show_alert=True)
    await callback.message.edit_text(_crm_text(s), reply_markup=_crm_kb(s))


# ---------------------------------------------------------------------------
# Quick replies management
# ---------------------------------------------------------------------------

def _qr_text(s: dict) -> str:
    qrs: list = s.get("quick_replies", [])
    lines = ["💬 <b>Быстрые ответы</b>\n"]
    if qrs:
        for i, qr in enumerate(qrs, 1):
            lines.append(f"{i}. {qr}")
    else:
        lines.append("Шаблонов нет.")
    lines.append("\n<i>Кнопки быстрых ответов появятся в чатах (до 3 шт.).</i>")
    return "\n".join(lines)


def _qr_kb(s: dict):
    qrs: list = s.get("quick_replies", [])
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить шаблон", callback_data="qrm:add")
    if qrs:
        b.button(text="🗑 Удалить последний", callback_data="qrm:del_last")
        b.button(text="🧹 Очистить всё", callback_data="qrm:clear")
    b.button(text="⬅️ Назад", callback_data="tools:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "tools:quick_replies")
async def qr_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_qr_text(s), reply_markup=_qr_kb(s))
    await callback.answer()


@router.callback_query(F.data == "qrm:add")
async def qr_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ToolsState.waiting_qr_text)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="tools:quick_replies")
    await callback.message.edit_text(
        "💬 Введите текст шаблона быстрого ответа:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(ToolsState.waiting_qr_text)
async def qr_add_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Введите текст")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    qrs: list = s.setdefault("quick_replies", [])
    qrs.append(text)
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Шаблон добавлен")
    await message.answer(_qr_text(s), reply_markup=_qr_kb(s))


@router.callback_query(F.data == "qrm:del_last")
async def qr_del_last(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    qrs: list = s.get("quick_replies", [])
    if qrs:
        removed = qrs.pop()
        save_settings(callback.from_user.id, s)
        await callback.answer(f"✅ Удалено: {removed[:30]}", show_alert=True)
    else:
        await callback.answer("Шаблонов нет", show_alert=True)
    await callback.message.edit_text(_qr_text(s), reply_markup=_qr_kb(s))


@router.callback_query(F.data == "qrm:clear")
async def qr_clear(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["quick_replies"] = []
    save_settings(callback.from_user.id, s)
    await callback.answer("✅ Шаблоны очищены", show_alert=True)
    await callback.message.edit_text(_qr_text(s), reply_markup=_qr_kb(s))
