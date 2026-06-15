from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api import YooMarketAPI
from keyboards import ChatCallback, OrderCallback, PaginationCallback, back_keyboard

router = Router()


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------


class ReplyState(StatesGroup):
    waiting_for_text = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return "Сообщений пока нет."

    lines: list[str] = []
    for msg in messages:
        sender = (
            msg.get("sender_name")
            or msg.get("sender", {}).get("name")
            or msg.get("from")
            or "Неизвестно"
        )
        text = msg.get("text") or msg.get("message") or "—"
        lines.append(f"<b>{sender}:</b> {text}")

    return "\n\n".join(lines)


def _build_chat_orders_keyboard(
    orders: list[dict], next_cursor: str | None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for order in orders:
        oid = str(order.get("id", ""))
        title = (
            order.get("title")
            or order.get("ad_title")
            or order.get("product_name")
            or f"Заказ {oid}"
        )
        builder.button(
            text=f"💬 #{oid} {title}",
            callback_data=ChatCallback(chat_id=oid).pack(),
        )
    builder.adjust(1)

    if next_cursor:
        builder.button(
            text="Следующая страница →",
            callback_data=PaginationCallback(
                entity="chat_orders", cursor=next_cursor
            ).pack(),
        )

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.message(F.text == "💬 Чаты")
async def show_chats(message: Message, api: YooMarketAPI) -> None:
    """Show orders list — chats are per order."""
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = (
            data.get("meta", {}).get("next_cursor")
            or data.get("next_cursor")
        )

        if not orders:
            await message.answer("💬 Чатов не найдено.", reply_markup=back_keyboard())
            return

        text = "💬 Выберите заказ для просмотра чата:"
        keyboard = _build_chat_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await message.answer(text, reply_markup=keyboard)


@router.callback_query(PaginationCallback.filter(F.entity == "chat_orders"))
async def paginate_chat_orders(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    try:
        data = await api.get_orders(cursor=callback_data.cursor)
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = (
            data.get("meta", {}).get("next_cursor")
            or data.get("next_cursor")
        )
        text = "💬 Выберите заказ для просмотра чата:"
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
    """Show last messages for an order chat."""
    chat_id = callback_data.chat_id
    try:
        data = await api.get_messages(chat_id)
        messages: list[dict] = data.get("data") or data.get("items") or []

        # Show up to last 10 messages
        messages = messages[-10:] if len(messages) > 10 else messages

        text = f"💬 <b>Чат по заказу #{chat_id}</b>\n\n" + _format_messages(messages)

        builder = InlineKeyboardBuilder()
        builder.button(
            text="✉️ Ответить",
            callback_data=f"reply_init:{chat_id}",
        )
        builder.button(text="⬅️ Назад", callback_data="back_to_menu")
        builder.adjust(1)
        keyboard = builder.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("reply_init:"))
async def init_reply(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Store chat_id and ask user for reply text."""
    chat_id = callback.data.split(":", 1)[1]
    await state.set_state(ReplyState.waiting_for_text)
    await state.update_data(chat_id=chat_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"reply_cancel:{chat_id}")

    await callback.message.edit_text(
        f"✉️ Введите текст сообщения для чата #{chat_id}:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reply_cancel:"))
async def cancel_reply(
    callback: CallbackQuery,
    state: FSMContext,
    api: YooMarketAPI,
) -> None:
    """Cancel reply and show chat again."""
    await state.clear()
    chat_id = callback.data.split(":", 1)[1]

    try:
        data = await api.get_messages(chat_id)
        messages: list[dict] = data.get("data") or data.get("items") or []
        messages = messages[-10:] if len(messages) > 10 else messages
        text = f"💬 <b>Чат по заказу #{chat_id}</b>\n\n" + _format_messages(messages)

        builder = InlineKeyboardBuilder()
        builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
        builder.button(text="⬅️ Назад", callback_data="back_to_menu")
        builder.adjust(1)
        keyboard = builder.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.message(ReplyState.waiting_for_text)
async def send_reply(
    message: Message,
    state: FSMContext,
    api: YooMarketAPI,
) -> None:
    """Send the typed message to YooMarket chat."""
    data = await state.get_data()
    chat_id = data.get("chat_id")
    await state.clear()

    if not chat_id:
        await message.answer("❌ Ошибка: не найден ID чата.", reply_markup=back_keyboard())
        return

    text_to_send = message.text or ""
    if not text_to_send.strip():
        await message.answer(
            "❌ Сообщение не может быть пустым.",
            reply_markup=back_keyboard(),
        )
        return

    try:
        await api.send_message(chat_id, text_to_send)
        response_text = f"✅ Сообщение отправлено в чат #{chat_id}"
    except Exception as e:
        response_text = f"❌ Ошибка отправки: {e}"

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💬 Вернуться в чат",
        callback_data=ChatCallback(chat_id=chat_id).pack(),
    )
    builder.button(text="⬅️ Главное меню", callback_data="back_to_menu")
    builder.adjust(1)

    await message.answer(response_text, reply_markup=builder.as_markup())
