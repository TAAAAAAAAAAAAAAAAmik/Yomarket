from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import OrderCallback, PaginationCallback, back_keyboard, order_actions_keyboard

router = Router()

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
    lines = ["🛒 <b>Заказы</b>\n"]
    for order in orders:
        oid = order.get("id", "—")
        title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
        buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
        price = order.get("price") or order.get("total") or "—"
        status = _order_status(order.get("status", ""))
        lines.append(f"#{oid} — <b>{title}</b>\n👤 {buyer}  💰 {price} ₽  {status}\n")
    return "\n".join(lines)


def _build_orders_keyboard(orders: list[dict], next_cursor: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        oid = str(order.get("id", ""))
        title = order.get("title") or order.get("ad_title") or order.get("product_name") or f"Заказ {oid}"
        builder.button(
            text=f"#{oid} {title[:30]}",
            callback_data=OrderCallback(order_id=oid, action="view").pack(),
        )
    builder.adjust(1)
    if next_cursor:
        builder.button(
            text="Следующая →",
            callback_data=PaginationCallback(entity="orders", cursor=next_cursor).pack(),
        )
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    return builder.as_markup()


@router.callback_query(F.data == "menu:orders")
async def show_orders(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю заказы...")
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
        text = _format_orders_text(orders)
        keyboard = _build_orders_keyboard(orders, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(PaginationCallback.filter(F.entity == "orders"))
async def paginate_orders(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        data = await api.get_orders(cursor=callback_data.cursor)
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
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
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        order = await api.get_order(callback_data.order_id)
        oid = order.get("id", callback_data.order_id)
        title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
        buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
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
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(OrderCallback.filter(F.action.in_({"work", "confirm", "refund"})))
async def handle_order_action(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    api: YooMarketAPI,
) -> None:
    labels = {"work": "▶️ Заказ взят в работу", "confirm": "✅ Заказ подтверждён", "refund": "↩️ Возврат оформлен"}
    try:
        if callback_data.action == "work":
            await api.work_order(callback_data.order_id)
        elif callback_data.action == "confirm":
            await api.confirm_order(callback_data.order_id)
        else:
            await api.refund_order(callback_data.order_id)
        text = f"{labels[callback_data.action]} — заказ #{callback_data.order_id}"
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Детали заказа", callback_data=OrderCallback(order_id=callback_data.order_id, action="view").pack())
    builder.button(text="⬅️ Заказы", callback_data="menu:orders")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()
