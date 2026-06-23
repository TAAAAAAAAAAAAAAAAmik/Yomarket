from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import ChatCallback, PaginationCallback, back_keyboard
from storage import get_settings

router = Router()


class ReplyState(StatesGroup):
    waiting_for_text = State()


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return "Сообщений пока нет."
    lines: list[str] = []
    for msg in messages:
        sender_type = msg.get("sender_type") or msg.get("sender") or "unknown"
        text = msg.get("text") or msg.get("message") or "—"
        if sender_type in ("shop", "seller"):
            prefix = "🏪 <b>Вы</b>"
        elif sender_type == "system":
            prefix = "⚙️ <i>Система</i>"
        else:
            prefix = "👤 <b>Покупатель</b>"
        lines.append(f"{prefix}: {text}")
    return "\n\n".join(lines)


def _build_chat_orders_keyboard(orders: list[dict], next_cursor: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        oid = str(order.get("id", ""))
        title = order.get("title") or order.get("ad_title") or order.get("product_name") or f"Заказ {oid}"
        buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or ""
        label = f"💬 #{oid} {title[:25]}" + (f" ({buyer})" if buyer else "")
        builder.button(text=label, callback_data=ChatCallback(chat_id=oid).pack())
    builder.adjust(1)
    if next_cursor:
        builder.button(text="Следующая →", callback_data=PaginationCallback(entity="chat_orders", cursor=next_cursor).pack())
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    return builder.as_markup()


@router.callback_query(F.data == "menu:chats")
async def show_chats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю чаты...")
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
        if not orders:
            await callback.message.edit_text("💬 Чатов не найдено.", reply_markup=back_keyboard())
            await callback.answer()
            return
        text = "💬 <b>Чаты</b>\nВыберите заказ:"
        keyboard = _build_chat_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(PaginationCallback.filter(F.entity == "chat_orders"))
async def paginate_chat_orders(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        data = await api.get_orders(cursor=callback_data.cursor)
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
        text = "💬 <b>Чаты</b>\nВыберите заказ:"
        keyboard = _build_chat_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(ChatCallback.filter(F.cursor == ""))
async def show_chat_messages(
    callback: CallbackQuery,
    callback_data: ChatCallback,
    api: YooMarketAPI,
) -> None:
    chat_id = callback_data.chat_id
    await callback.message.edit_text(f"⏳ Загружаю чат #{chat_id}...")
    try:
        data = await api.get_messages(chat_id)
        messages: list[dict] = data.get("data") or data.get("items") or []
        messages = messages[-10:] if len(messages) > 10 else messages
        text = f"💬 <b>Чат по заказу #{chat_id}</b>\n\n" + _format_messages(messages)
        settings = get_settings(callback.from_user.id)
        quick_replies: list = settings.get("quick_replies", [])
        builder = InlineKeyboardBuilder()
        builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
        for i, qr in enumerate(quick_replies[:3]):
            builder.button(text=f"💬 {qr[:28]}", callback_data=f"qr:{chat_id}:{i}")
        builder.button(text="🔄 Обновить", callback_data=ChatCallback(chat_id=chat_id).pack())
        builder.button(text="⬅️ Чаты", callback_data="menu:chats")
        builder.adjust(1)
        keyboard = builder.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("reply_init:"))
async def init_reply(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.data.split(":", 1)[1]
    await state.set_state(ReplyState.waiting_for_text)
    await state.update_data(chat_id=chat_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"reply_cancel:{chat_id}")
    await callback.message.edit_text(
        f"✉️ Напишите сообщение для чата #{chat_id}:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reply_cancel:"))
async def cancel_reply(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    await state.clear()
    chat_id = callback.data.split(":", 1)[1]
    try:
        data = await api.get_messages(chat_id)
        messages: list[dict] = data.get("data") or data.get("items") or []
        messages = messages[-10:] if len(messages) > 10 else messages
        text = f"💬 <b>Чат по заказу #{chat_id}</b>\n\n" + _format_messages(messages)
        builder = InlineKeyboardBuilder()
        builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
        builder.button(text="⬅️ Чаты", callback_data="menu:chats")
        builder.adjust(1)
        keyboard = builder.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(ReplyState.waiting_for_text)
async def send_reply(message: Message, state: FSMContext, api: YooMarketAPI) -> None:
    data = await state.get_data()
    chat_id = data.get("chat_id")
    await state.clear()

    text_to_send = (message.text or "").strip()
    if not chat_id or not text_to_send:
        await message.answer("❌ Ошибка.", reply_markup=back_keyboard())
        return

    try:
        await api.send_message(chat_id, text_to_send)
        text = f"✅ Сообщение отправлено в чат #{chat_id}"
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Вернуться в чат", callback_data=ChatCallback(chat_id=chat_id).pack())
    builder.button(text="⬅️ Чаты", callback_data="menu:chats")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("qr:"))
async def send_quick_reply(callback: CallbackQuery, api: YooMarketAPI) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    _, chat_id, idx_str = parts
    try:
        idx = int(idx_str)
    except ValueError:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    settings = get_settings(callback.from_user.id)
    quick_replies: list = settings.get("quick_replies", [])
    if idx >= len(quick_replies):
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    text = quick_replies[idx]
    try:
        await api.send_message(chat_id, text)
        await callback.answer("✅ Отправлено!", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)
