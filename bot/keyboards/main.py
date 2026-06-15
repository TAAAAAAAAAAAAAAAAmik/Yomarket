from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup


# ---------------------------------------------------------------------------
# Callback data factories
# ---------------------------------------------------------------------------


class AdCallback(CallbackData, prefix="ad"):
    ad_id: str
    cursor: str = ""


class OrderCallback(CallbackData, prefix="order"):
    order_id: str
    action: str = "view"  # view / work / confirm / refund


class ChatCallback(CallbackData, prefix="chat"):
    chat_id: str
    cursor: str = ""


class PaginationCallback(CallbackData, prefix="page"):
    entity: str  # ads / orders / chat_orders
    cursor: str


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="💰 Баланс")
    builder.button(text="📦 Товары")
    builder.button(text="🛒 Заказы")
    builder.button(text="💬 Чаты")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)


def back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    return builder.as_markup()


def order_actions_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="▶️ В работу",
        callback_data=OrderCallback(order_id=order_id, action="work").pack(),
    )
    builder.button(
        text="✅ Подтвердить",
        callback_data=OrderCallback(order_id=order_id, action="confirm").pack(),
    )
    builder.button(
        text="↩️ Возврат",
        callback_data=OrderCallback(order_id=order_id, action="refund").pack(),
    )
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(3, 1)
    return builder.as_markup()


def reply_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✉️ Ответить",
        callback_data=ChatCallback(chat_id=chat_id).pack(),
    )
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)
    return builder.as_markup()
