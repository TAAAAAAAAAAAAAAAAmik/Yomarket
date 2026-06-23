from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class ToolsState(StatesGroup):
    waiting_crm_buyer = State()
    waiting_crm_note = State()
    waiting_qr_text = State()


def _tools_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📝 Заметки о покупателях", callback_data="tools:crm")
    b.button(text="💬 Быстрые ответы", callback_data="tools:quick_replies")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
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
    await message.answer("✅ Шаблон добавлен")
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
