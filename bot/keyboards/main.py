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


def main_menu_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    """Главное меню. Ширина ряда считается по надписям, а не берётся как «2».

    Пункты меню продавец переименовывает сам, и жёсткая пара ломалась об
    длинное название: «🛒 Заказы» и «🤖 Автопилот сделок» в одном ряду —
    второе переносится на две строки, ряд становится вдвое выше соседнего.

    Кнопка админа добавляется **до** раскладки. Раньше она шла после
    `adjust(2)`, а `add()` дописывает кнопку в последний ряд, если там есть
    место, — и при нечётном числе пунктов «👑 Админ-панель» вставала рядом
    со случайным из них. Вёрстка зависела от того, сколько пунктов включено.
    """
    import storage
    from storage import get_menu_labels
    import ui
    labels = get_menu_labels()
    builder = InlineKeyboardBuilder()
    texts = []
    for key, _default, cb in storage.MENU_BUTTONS:
        text = labels.get(key, _default)
        texts.append(text)
        builder.button(text=text, callback_data=cb)
    spec = ui.sizes(texts)
    if is_admin_user:
        builder.button(text="👑 Админ-панель", callback_data="admin:menu")
        spec = spec + [1]
    builder.adjust(*spec)
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
        from orderfields import ad_price, money
        value = ad_price(ad)
        price = money(value) if value is not None else ""
        label = f"{title[:28]} — {price} ₽" if price else title[:35]
        builder.button(text=label, callback_data=AdCallback(ad_id=ad_id).pack())
    # Товары — по одному в ряд, даже короткие: два названия рядом читаются
    # как одно длинное. Подвал раскладывается по ширине надписей.
    import ui
    page = 0
    if next_cursor:
        # Листалка отдельной строкой: рядом с действиями она читается как
        # ещё одно действие.
        builder.button(text="Ещё товары ▶️", callback_data=PaginationCallback(
            entity="ads", cursor=next_cursor).pack())
        page = 1
    tail = [("➕ Добавить товар", "create_ad:start"),
             ("🔄 Обновить", "ads_load"),
             ("⬅️ Меню", "menu:main")]
    for text, cb in tail:
        builder.button(text=text, callback_data=cb)
    builder.adjust(*([1] * (len(ads) + page)
                     + ui.sizes([t for t, _ in tail])))
    return builder.as_markup()


def order_actions_keyboard(order_id: str, chat_id: str = "",
                           status: str = "",
                           refused: dict[str, list[str]] | None = None
                           ) -> InlineKeyboardMarkup:
    """Кнопки заказа. `refused` — статусы, в которых маркетплейс уже отказал.

    Кнопка, которая в этом статусе заведомо ответит «Incorrect Order
    Status», — то же обещание невозможного, что и совет ответить в закрытый
    чат. Список отказов накапливается наблюдениями, а не догадками: пока
    отказа не было, кнопка на месте.
    """
    builder = InlineKeyboardBuilder()
    now = str(status or "").strip().lower()
    refused = refused or {}

    def allowed(action: str) -> bool:
        return not (now and now in [str(x).lower()
                                    for x in (refused.get(action) or [])])

    # «В работу» убрана: что POST /orders/{id}/work делает на этом
    # маркетплейсе, не подтверждено — на тестовом заказе покупатель сразу
    # увидел «магазин сообщил, что выполнил заказ». Автопринятие в Автопилоте
    # остаётся, там это осознанный выбор с показом настоящего статуса.
    if allowed("confirm"):
        builder.button(text="✅ Подтвердить", callback_data=OrderCallback(order_id=order_id, action="confirm").pack())
    if allowed("refund"):
        builder.button(text="↩️ Возврат", callback_data=OrderCallback(order_id=order_id, action="refund").pack())
    builder.button(text="💬 Чат по заказу", callback_data=ChatCallback(chat_id=chat_id or order_id).pack())
    builder.button(text="⬅️ Назад", callback_data="menu:orders")
    # Столбиком, и это не «недоделанная раскладка». «✅ Подтвердить» и
    # «↩️ Возврат» коротки обе и по ширине встали бы в один ряд — а промах
    # пальцем на телефоне означает либо закрытую сделку вместо возврата,
    # либо отданные покупателю деньги вместо закрытой сделки.
    builder.adjust(1)
    return builder.as_markup()


def reply_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=ChatCallback(chat_id=chat_id).pack())
    builder.button(text="⬅️ Назад", callback_data="menu:chats")
    builder.adjust(1)
    return builder.as_markup()
