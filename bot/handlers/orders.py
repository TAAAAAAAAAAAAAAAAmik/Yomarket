from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import OrderCallback, PaginationCallback, back_keyboard, order_actions_keyboard
from storage import get_settings

router = Router()


class OrderSearchState(StatesGroup):
    waiting_query = State()

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
    builder.button(text="🔍 Поиск", callback_data="orders:search")
    builder.button(text="✅ Выполненные", callback_data="orders:filter:done")
    builder.button(text="⏳ Активные", callback_data="orders:filter:active")
    builder.button(text="↩️ Возвраты", callback_data="orders:filter:refunds")
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
        chat_id = str(order.get("chat_id") or oid)
        text = (
            f"🛒 <b>Заказ #{oid}</b>\n\n"
            f"📦 Товар: {title}\n"
            f"👤 Покупатель: {buyer}\n"
            f"💰 Сумма: {price} ₽\n"
            f"📊 Статус: {status}\n"
            f"📍 Адрес: {address}\n"
            f"💬 Комментарий: {comment}"
        )
        keyboard = order_actions_keyboard(str(oid), chat_id)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(OrderCallback.filter(F.action == "refundask"))
async def ask_refund(
    callback: CallbackQuery,
    callback_data: OrderCallback,
) -> None:
    oid = callback_data.order_id
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, оформить возврат",
             callback_data=OrderCallback(order_id=oid, action="refund").pack())
    b.button(text="❌ Отмена",
             callback_data=OrderCallback(order_id=oid, action="view").pack())
    b.adjust(1)
    await callback.message.edit_text(
        f"↩️ <b>Оформить возврат по заказу #{oid}?</b>\n\n"
        "⚠️ Действие вернёт деньги покупателю и его нельзя отменить.",
        reply_markup=b.as_markup(),
    )
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


@router.callback_query(F.data == "orders:search")
async def orders_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OrderSearchState.waiting_query)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="menu:orders")
    await callback.message.edit_text(
        "🔍 <b>Поиск по заказам</b>\n\nВведите имя покупателя, название товара или сумму:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(OrderSearchState.waiting_query)
async def orders_search_exec(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip().lower()
    await state.clear()

    s = get_settings(message.from_user.id)
    order_details: dict = s.get("known_order_details", {})
    known_orders: dict = s.get("known_orders", {})

    results = []
    for oid, det in order_details.items():
        buyer = (det.get("buyer") or "").lower()
        price = str(det.get("price") or "").lower()
        title = (det.get("title") or "").lower()
        if query in buyer or query in price or query in title:
            results.append((oid, det))

    b = InlineKeyboardBuilder()
    if not results:
        b.button(text="⬅️ Заказы", callback_data="menu:orders")
        await message.answer(
            f"🔍 По запросу «{message.text}» ничего не найдено.",
            reply_markup=b.as_markup(),
        )
        return

    lines = [f"🔍 <b>Найдено: {len(results)}</b>\n"]
    for oid, det in results[:15]:
        buyer = det.get("buyer", "—")
        price = det.get("price", "—")
        title = (det.get("title") or "")[:28]
        status_raw = known_orders.get(str(oid), known_orders.get(oid, ""))
        STATUS = {"confirmed": "✅", "completed": "✅", "done": "✅",
                  "refunded": "↩️", "cancelled": "↩️", "work": "🔧", "new": "🆕"}
        emoji = STATUS.get(status_raw, "⚪")
        lines.append(f"{emoji} #{oid} — <b>{title}</b>\n   👤 {buyer}  💰 {price} ₽")

    for oid, det in results[:10]:
        title = (det.get("title") or f"Заказ {oid}")[:30]
        b.button(
            text=f"#{oid} {title}",
            callback_data=OrderCallback(order_id=str(oid), action="view").pack(),
        )
    b.adjust(1)
    b.button(text="⬅️ Заказы", callback_data="menu:orders")
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "orders:filter:done")
async def filter_orders_done(callback: CallbackQuery) -> None:
    await callback.answer()
    done_statuses = ("confirmed", "completed", "done")
    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    matched = [
        (oid, order_details.get(str(oid), order_details.get(oid, {})))
        for oid, status in known_orders.items()
        if status in done_statuses
    ]

    b = InlineKeyboardBuilder()
    if not matched:
        b.button(text="⬅️ Заказы", callback_data="menu:orders")
        await callback.message.edit_text(
            "✅ <b>Выполненные заказы</b>\n\nНет выполненных заказов.",
            reply_markup=b.as_markup(),
        )
        return

    lines = [f"✅ <b>Выполненные заказы</b> ({len(matched)})\n"]
    STATUS_LABEL = {"confirmed": "✔️ Подтверждён", "completed": "✅ Выполнен", "done": "✅ Готов"}
    for oid, det in matched[:15]:
        title = (det.get("title") or "—")[:28]
        buyer = det.get("buyer", "—")
        price = det.get("price", "—")
        status_raw = known_orders.get(str(oid), known_orders.get(oid, ""))
        status_label = STATUS_LABEL.get(status_raw, status_raw)
        lines.append(f"#{oid} — <b>{title}</b>\n   👤 {buyer}  💰 {price} ₽  {status_label}")

    for oid, det in matched[:15]:
        title = (det.get("title") or f"Заказ {oid}")[:30]
        b.button(
            text=f"#{oid} {title}",
            callback_data=OrderCallback(order_id=str(oid), action="view").pack(),
        )
    b.adjust(1)
    b.button(text="⬅️ Заказы", callback_data="menu:orders")
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "orders:filter:active")
async def filter_orders_active(callback: CallbackQuery) -> None:
    await callback.answer()
    active_statuses = ("new", "work", "working", "processing", "active", "pending")
    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    matched = [
        (oid, order_details.get(str(oid), order_details.get(oid, {})))
        for oid, status in known_orders.items()
        if status in active_statuses
    ]

    b = InlineKeyboardBuilder()
    if not matched:
        b.button(text="⬅️ Заказы", callback_data="menu:orders")
        await callback.message.edit_text(
            "⏳ <b>Активные заказы</b>\n\nНет активных заказов.",
            reply_markup=b.as_markup(),
        )
        return

    lines = [f"⏳ <b>Активные заказы</b> ({len(matched)})\n"]
    STATUS_LABEL = {
        "new": "🆕 Новый", "work": "🔧 В работе", "working": "🔧 В работе",
        "processing": "⚙️ Обработка", "active": "⏳ Активен", "pending": "🕐 Ожидает",
    }
    for oid, det in matched[:15]:
        title = (det.get("title") or "—")[:28]
        buyer = det.get("buyer", "—")
        price = det.get("price", "—")
        status_raw = known_orders.get(str(oid), known_orders.get(oid, ""))
        status_label = STATUS_LABEL.get(status_raw, status_raw)
        lines.append(f"#{oid} — <b>{title}</b>\n   👤 {buyer}  💰 {price} ₽  {status_label}")

    for oid, det in matched[:15]:
        title = (det.get("title") or f"Заказ {oid}")[:30]
        b.button(
            text=f"#{oid} {title}",
            callback_data=OrderCallback(order_id=str(oid), action="view").pack(),
        )
    b.adjust(1)
    b.button(text="⬅️ Заказы", callback_data="menu:orders")
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "orders:filter:refunds")
async def filter_orders_refunds(callback: CallbackQuery) -> None:
    await callback.answer()
    refund_statuses = ("refunded", "cancelled", "returned")
    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    matched = [
        (oid, order_details.get(str(oid), order_details.get(oid, {})))
        for oid, status in known_orders.items()
        if status in refund_statuses
    ]

    b = InlineKeyboardBuilder()
    if not matched:
        b.button(text="⬅️ Заказы", callback_data="menu:orders")
        await callback.message.edit_text(
            "↩️ <b>Возвраты и отмены</b>\n\nНет возвратов или отменённых заказов.",
            reply_markup=b.as_markup(),
        )
        return

    lines = [f"↩️ <b>Возвраты и отмены</b> ({len(matched)})\n"]
    STATUS_LABEL = {"refunded": "↩️ Возврат", "cancelled": "❌ Отменён", "returned": "↩️ Возврат"}
    for oid, det in matched[:15]:
        title = (det.get("title") or "—")[:28]
        buyer = det.get("buyer", "—")
        price = det.get("price", "—")
        status_raw = known_orders.get(str(oid), known_orders.get(oid, ""))
        status_label = STATUS_LABEL.get(status_raw, status_raw)
        lines.append(f"#{oid} — <b>{title}</b>\n   👤 {buyer}  💰 {price} ₽  {status_label}")

    for oid, det in matched[:15]:
        title = (det.get("title") or f"Заказ {oid}")[:30]
        b.button(
            text=f"#{oid} {title}",
            callback_data=OrderCallback(order_id=str(oid), action="view").pack(),
        )
    b.adjust(1)
    b.button(text="⬅️ Заказы", callback_data="menu:orders")
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
