from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


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


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Объявления", callback_data="menu:ads")
    builder.button(text="🛒 Заказы", callback_data="menu:orders")
    builder.button(text="💬 Чаты", callback_data="menu:chats")
    builder.button(text="💰 Баланс", callback_data="menu:balance")
    builder.button(text="⚙️ Авто-функции", callback_data="auto:menu")
    builder.button(text="🧩 Плагины", callback_data="plugins:menu")
    builder.adjust(2)
    return builder.as_markup()


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    return builder.as_markup()


def back_keyboard() -> InlineKeyboardMarkup:
    return back_to_menu_keyboard()


def ads_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Загрузить товары", callback_data="ads_load")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def ads_list_keyboard(
    ads: list[dict],
    next_cursor: str | None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ad in ads:
        ad_id = str(ad.get("id", ""))
        title = ad.get("title") or ad.get("name") or f"Товар {ad_id}"
        price = ad.get("price", "")
        label = f"{title[:28]} — {price} ₽" if price else title[:35]
        builder.button(text=label, callback_data=AdCallback(ad_id=ad_id).pack())
    builder.adjust(1)
    if next_cursor:
        builder.button(
            text="Ещё товары ▶️",
            callback_data=PaginationCallback(entity="ads", cursor=next_cursor).pack(),
        )
    builder.button(text="🔄 Обновить", callback_data="ads_load")
    builder.button(text="⬅️ Меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def order_actions_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ В работу", callback_data=OrderCallback(order_id=order_id, action="work").pack())
    builder.button(text="✅ Подтвердить", callback_data=OrderCallback(order_id=order_id, action="confirm").pack())
    builder.button(text="↩️ Возврат", callback_data=OrderCallback(order_id=order_id, action="refund").pack())
    builder.button(text="⬅️ Назад", callback_data="menu:orders")
    builder.adjust(3, 1)
    return builder.as_markup()


def reply_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=ChatCallback(chat_id=chat_id).pack())
    builder.button(text="⬅️ Назад", callback_data="menu:chats")
    builder.adjust(1)
    return builder.as_markup()
