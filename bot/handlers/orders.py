from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api import YooMarketAPI
from keyboards import OrderCallback, PaginationCallback, back_keyboard, order_actions_keyboard

router = Router()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_EMOJI = {
    "new": "🔄 Новый",
    "working": "✅ В работе",
    "confirmed": "✔️ Подтверждён",
    "refunded": "↩️ Возврат",
    "cancelled": "❌ Отменён",
}


def _order_status(raw: str) -> str:
    return STATUS_EMOJI.get(raw, raw)


def _format_orders_text(orders: list[dict]) -> str:
    if not orders:
        return "🛒 Заказов не найдено."

    lines = ["🛒 Заказы:\n"]
    for order in orders:
        oid = order.get("id", "—")
        title = (
            order.get("title")
            or order.get("ad_title")
            or order.get("product_name")
            or "—"
        )
        buyer = (
            order.get("buyer_name")
            or order.get("buyer", {}).get("name")
            or "—"
        )
        price = order.get("price") or order.get("total") or "—"
        status = _order_status(order.get("status", ""))
        lines.append(
            f"#{oid} — {title}\n"
            f"👤 Покупатель: {buyer}\n"
            f"💰 {price} ₽ | {status}\n"
        )
    return "\n".join(lines)


def _build_orders_keyboard(
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
            text=f"🔍 #{oid} {title}",
            callback_data=OrderCallback(order_id=oid, action="view").pack(),
        )
    builder.adjust(1)

    if next_cursor:
        builder.button(
            text="Следующая страница →",
            callback_data=PaginationCallback(entity="orders", cursor=next_cursor).pack(),
        )

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.message(F.text == "🛒 Заказы")
async def show_orders(message: Message, api: YooMarketAPI) -> None:
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = (
            data.get("meta", {}).get("next_cursor")
            or data.get("next_cursor")
        )
        text = _format_orders_text(orders)
        keyboard = _build_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await message.answer(text, reply_markup=keyboard)


@router.callback_query(PaginationCallback.filter(F.entity == "orders"))
async def paginate_orders(
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
        text = _format_orders_text(orders)
        keyboard = _build_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(OrderCallback.filter(F.action == "view"))
async def show_order_detail(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    api: YooMarketAPI,
) -> None:
    try:
        order = await api.get_order(callback_data.order_id)
        oid = order.get("id", callback_data.order_id)
        title = (
            order.get("title")
            or order.get("ad_title")
            or order.get("product_name")
            or "—"
        )
        buyer = (
            order.get("buyer_name")
            or order.get("buyer", {}).get("name")
            or "—"
        )
        price = order.get("price") or order.get("total") or "—"
        status = _order_status(order.get("status", ""))
        address = order.get("address") or order.get("delivery_address") or "—"
        comment = order.get("comment") or "—"

        text = (
            f"🛒 <b>Заказ #{oid}</b>\n\n"
            f"📦 Товар: {title}\n"
            f"👤 Покупатель: {buyer}\n"
            f"💰 Сумма: {price} ₽\n"
            f"📊 Статус: {status}\n"
            f"📍 Адрес: {address}\n"
            f"💬 Комментарий: {comment}"
        )
        keyboard = order_actions_keyboard(str(oid))
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(OrderCallback.filter(F.action.in_({"work", "confirm", "refund"})))
async def handle_order_action(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    api: YooMarketAPI,
) -> None:
    action_map = {
        "work": ("api.work_order", "▶️ Заказ взят в работу"),
        "confirm": ("api.confirm_order", "✅ Заказ подтверждён"),
        "refund": ("api.refund_order", "↩️ Возврат оформлен"),
    }

    action_label = action_map[callback_data.action][1]

    try:
        if callback_data.action == "work":
            await api.work_order(callback_data.order_id)
        elif callback_data.action == "confirm":
            await api.confirm_order(callback_data.order_id)
        else:
            await api.refund_order(callback_data.order_id)

        text = f"{action_label} для заказа #{callback_data.order_id}"
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔍 Заказ",
        callback_data=OrderCallback(
            order_id=callback_data.order_id, action="view"
        ).pack(),
    )
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()
