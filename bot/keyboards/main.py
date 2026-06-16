from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# ---------------------------------------------------------------------------
# Callback data factories
# ---------------------------------------------------------------------------


class AdCallback(CallbackData, prefix="ad"):
    ad_id: str


class OrderCallback(CallbackData, prefix="order"):
    order_id: str
    action: str = "view"


class ChatCallback(CallbackData, prefix="chat"):
    chat_id: str
    cursor: str = ""


class PaginationCallback(CallbackData, prefix="page"):
    entity: str
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


def ads_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Загрузить товары", callback_data="ads_load")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_menu")
    builder.adjust(1)
    return builder.as_markup()


def back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    return builder.as_markup()


def ads_list_keyboard(
    ads: list[dict],
    next_cursor: str | None,
    prev_cursor: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ad in ads:
        ad_id = str(ad.get("id", ""))
        title = ad.get("title") or ad.get("name") or f"Товар {ad_id}"
        price = ad.get("price", "")
        label = f"{title[:28]} — {price} ₽" if price else title[:35]
        builder.button(text=label, callback_data=AdCallback(ad_id=ad_id).pack())
    builder.adjust(1)
    if prev_cursor:
        builder.button(
            text="◀️ Назад",
            callback_data=PaginationCallback(entity="ads", cursor=prev_cursor).pack(),
        )
    if next_cursor:
        builder.button(
            text="Ещё товары ▶️",
            callback_data=PaginationCallback(entity="ads", cursor=next_cursor).pack(),
        )
    builder.button(text="🔄 Обновить", callback_data="ads_load")
    builder.button(text="⬅️ Меню", callback_data="back_to_menu")
    builder.adjust(1)
    return builder.as_markup()


def order_actions_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ В работу", callback_data=OrderCallback(order_id=order_id, action="work").pack())
    builder.button(text="✅ Подтвердить", callback_data=OrderCallback(order_id=order_id, action="confirm").pack())
    builder.button(text="↩️ Возврат", callback_data=OrderCallback(order_id=order_id, action="refund").pack())
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(3, 1)
    return builder.as_markup()


def reply_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=ChatCallback(chat_id=chat_id).pack())
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)
    return builder.as_markup()
