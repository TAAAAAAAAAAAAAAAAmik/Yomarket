from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import OrderCallback, PaginationCallback, back_keyboard, order_actions_keyboard
from orderfields import (BACK, DONE, describe, money, status_icon,
                         status_ru)
from storage import get_settings

router = Router()
logger = logging.getLogger(__name__)


class OrderSearchState(StatesGroup):
    waiting_query = State()


# Оставлено для совместимости: статусы переводит orderfields.status_ru.
STATUS_EMOJI = {
    "new": "🔄 Новый",
    "working": "✅ В работе",
    "confirmed": "✔️ Подтверждён",
    "refunded": "↩️ Возврат",
    "cancelled": "❌ Отменён",
}


def _order_status(raw: str) -> str:
    return status_ru(raw)


def _esc(value) -> str:
    import html
    return html.escape(str(value), quote=False)


def _merge(order: dict, details: dict | None) -> dict:
    """Заказ из списка, дополненный тем, что бот уже дочитал.

    Список отдаёт номер и статус; товар и покупателя фоновый опрос дочитывает
    из карточки заказа и хранит у себя. Без этого экран показывал «Заказ
    #1136046» даже там, где название давно известно.
    """
    d = describe(order)
    det = (details or {}).get(str(d["id"])) or {}
    for key in ("title", "buyer", "username"):
        if not d.get(key):
            value = str(det.get(key) or "").strip()
            d[key] = "" if value == "—" else value
    if d.get("price") is None:
        raw = det.get("price")
        d["price"] = raw if isinstance(raw, (int, float)) else None
    return d


def _format_orders_text(orders: list[dict], details: dict | None = None) -> str:
    """Список заказов человеческим текстом.

    Раньше каждая строка печаталась по шаблону «товар — покупатель — сумма»,
    и когда чего-то из этого в ответе не было, на экран уходили прочерки:
    «#1184186 — — 👤 — 💰 — ₽ created». Теперь печатается только то, что
    известно, а статусы переведены.
    """
    if not orders:
        return "🛒 Заказов не найдено."

    by_status: dict[str, int] = {}
    rows: list[str] = []
    for order in orders:
        d = _merge(order, details)
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1

        head = f"{status_icon(d['status']) or '•'} "
        head += f"<b>{_esc(d['title'])}</b>" if d["title"] else f"Заказ #{_esc(d['id'])}"
        if d["quantity"] and str(d["quantity"]) != "1":
            head += f" ×{_esc(d['quantity'])}"

        tail: list[str] = []
        if d["price"] is not None:
            tail.append(f"<b>{money(d['price'])} ₽</b>")
        if d["buyer"]:
            tail.append(f"👤 {_esc(d['buyer'])}")
        tail.append(status_ru(d["status"]))
        if d["title"]:
            tail.append(f"<code>#{_esc(d['id'])}</code>")

        rows.append(head + "\n   " + "  ·  ".join(tail))

    summary = "  ·  ".join(f"{status_ru(k)}: <b>{v}</b>"
                           for k, v in sorted(by_status.items(),
                                              key=lambda kv: -kv[1])[:4])
    return (f"🛒 <b>Заказы</b> · {len(orders)}\n{summary}\n\n"
            + "\n\n".join(rows))


def _build_orders_keyboard(orders: list[dict], next_cursor: str | None,
                           details: dict | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        d = _merge(order, details)
        # Номер заказа сам по себе ничего не говорит — на кнопке товар и
        # сумма, номер остаётся внутри карточки.
        label = f"{status_icon(d['status']) or '•'} "
        label += d["title"][:26] if d["title"] else f"Заказ #{d['id']}"
        if d["price"] is not None:
            label += f" · {money(d['price'])} ₽"
        builder.button(
            text=label,
            callback_data=OrderCallback(order_id=d["id"], action="view").pack(),
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
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "menu:orders")
async def show_orders(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю заказы...")
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
        details = get_settings(callback.from_user.id).get("known_order_details") or {}
        text = _format_orders_text(orders, details)
        keyboard = _build_orders_keyboard(orders, next_cursor, details)
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
        details = get_settings(callback.from_user.id).get("known_order_details") or {}
        text = _format_orders_text(orders, details)
        keyboard = _build_orders_keyboard(orders, next_cursor, details)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def _why_not_in_work(s: dict, status: str, oid: str) -> str:
    """Почему заказ не взят в работу автоматически. Пусто — вопрос не стоит.

    Продавец прислал снимок панели: заказ «Оплачен», кнопка «В работу» не
    нажата. Бот заказ видел — автоответ покупателю ушёл, — а в работу не
    взял, и понять причину со стороны было нельзя ничем: ни экраном, ни
    логом. Причин же ровно четыре, и делать по ним надо разное.
    """
    from orderfields import WORK, needs_work
    key = str(status or "").strip().lower()
    if not needs_work(status):
        # Взят в работу, выполнен, возвращён — вопрос не стоит. Неоплаченный
        # заказ бот в работу не берёт намеренно: панель на такой прямо
        # предупреждает «не выдавайте товар».
        if not key or key in WORK or key in DONE or key in BACK:
            return ""
        return ("этот статус бот оплаченным не считает, а неоплаченный заказ "
                "в работу не берёт")
    aa = s.get("auto_accept", {}) or {}
    if not aa.get("enabled"):
        return "автопринятие выключено — Автопилот → Заказы"
    det = ((s.get("known_order_details") or {}).get(str(oid)) or {})
    if det.get("work_error"):
        return f"маркетплейс отказал: {_esc(str(det['work_error']))}"
    if det.get("work_skip"):
        return _esc(str(det["work_skip"]))
    return "должен взять в работу на ближайшем проходе — иначе /version"


@router.callback_query(OrderCallback.filter(F.action == "view"))
async def show_order_detail(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    api: YooMarketAPI,
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        order = await api.get_order(callback_data.order_id)
        d = _merge(order, get_settings(callback.from_user.id).get(
            "known_order_details") or {})
        oid = d["id"] or callback_data.order_id
        chat_id = str(order.get("chat_id") or oid)

        # Заказ ссылается на объявление номером, а название лежит в самом
        # объявлении — дочитываем, вместо того чтобы писать «название не
        # пришло» там, где его просто не спросили.
        if not d["title"]:
            from orderfields import ad_title, order_ad_id
            ad_id = order_ad_id(order)
            if ad_id:
                try:
                    d["title"] = ad_title(await api.get_ad(ad_id))
                except Exception as e:
                    logger.info("ad %s for order %s: %s", ad_id, oid, e)

        # Печатаем только то, что пришло: строка «📍 Адрес: —» ничего не
        # сообщает, а места занимает столько же, сколько настоящая.
        rows = [f"📦 <b>{_esc(d['title'])}</b>" if d["title"]
                else "📦 <i>товар без названия</i>"]
        if d["price"] is not None:
            rows.append(f"💰 <b>{money(d['price'])} ₽</b>"
                        + (f"   ×{_esc(d['quantity'])}" if d["quantity"]
                           and str(d["quantity"]) != "1" else ""))
        # Сырой статус рядом с переведённым. Стоило круга: «Оплачен» на
        # экране и `paid` в ответе API — разные вещи, и когда автоматика на
        # такой заказ не срабатывает, по одному переводу это не различить.
        rows.append(f"📊 {status_ru(d['status'])}"
                    + (f"  <code>{_esc(d['status'])}</code>" if d["status"]
                       else ""))
        why = _why_not_in_work(get_settings(callback.from_user.id), d["status"],
                               str(oid))
        if why:
            rows.append(f"⚙️ {why}")
        if d["buyer"] or d["username"]:
            rows.append(f"👤 {_esc(d['buyer'] or '')}"
                        + (f"  {_esc(d['username'])}" if d["username"] else ""))
        for label, value in (("📍", order.get("address")
                              or order.get("delivery_address")),
                             ("💬", order.get("comment"))):
            if value and str(value).strip() not in ("", "—"):
                rows.append(f"{label} {_esc(str(value)[:200])}")
        text = f"🛒 <b>Заказ #{_esc(oid)}</b>\n\n" + "\n".join(rows)
        keyboard = order_actions_keyboard(
            str(oid), chat_id, d["status"],
            get_settings(callback.from_user.id).get("action_refusals") or {})
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


_ACTION_RU = {"work": "взять в работу", "confirm": "подтвердить",
              "refund": "оформить возврат"}


def refused_statuses(uid: int, action: str) -> list[str]:
    """Статусы, в которых маркетплейс уже отказывал в этом действии.

    Список наблюдений, а не догадок: пополняется только настоящим отказом.
    Нужен, чтобы не предлагать кнопку, которая заведомо не сработает, —
    «не советуйте невозможного» относится и к кнопкам, не только к тексту.
    """
    from storage import get_settings
    seen = (get_settings(uid).get("action_refusals") or {}).get(action)
    return list(seen or [])


def _note_refusal(uid: int, action: str, status: str) -> None:
    from storage import get_settings, save_settings
    key = str(status or "").strip().lower()
    if not key:
        return
    s = get_settings(uid)
    seen = s.setdefault("action_refusals", {}).setdefault(action, [])
    if key not in seen:
        seen.append(key)
        del seen[8:]
        save_settings(uid, s)


def refusal_reason(action: str, status: str) -> str:
    """Почему отказали — по ситуации, а не одной фразой на все случаи.

    «Заказ в другом статусе» одинаково описывает уже подтверждённый заказ,
    закрытый и неоплаченный, а делать с ними надо разное. Незнакомый статус
    не подгоняется под знакомый: про него честнее сказать общее.
    """
    from orderfields import BACK, DONE, UNPAID
    key = str(status or "").strip().lower()
    what = _ACTION_RU.get(action, action)
    if key in ("confirmed", "closed") or (key in DONE and action == "confirm"):
        return "заказ уже подтверждён — второй раз не нужно."
    if key in ("refunded", "returned", "refund"):
        return "по заказу оформлен возврат, он закрыт."
    if key in ("cancelled", "canceled", "rejected", "failed", "expired"):
        return "заказ закрыт маркетплейсом."
    if key in UNPAID:
        return ("заказ ещё не оплачен. Деньги за него маркетплейс не "
                "получил, поэтому и закрывать нечего.")
    if key in BACK:
        return "заказ закрыт."
    return f"в этом статусе маркетплейс не даёт {what} — ни боту, ни вручную."


async def _why_refused(uid: int, api: YooMarketAPI, oid: str,
                       action: str, err: str) -> str:
    """Дочитать настоящий статус заказа после отказа и объяснить его.

    Ответ «заказ сейчас в другом статусе» без названия статуса — половина
    ответа: продавцу всё равно идти смотреть на сайт.
    """
    if "incorrect_status" not in (err or "").lower():
        return ""
    from orderfields import order_status, status_ru
    try:
        data = await api.get_order(oid)
    except Exception:
        return ""
    order = data.get("data") if isinstance(data, dict) else None
    status = order_status(order if isinstance(order, dict) else data or {})
    if not status:
        return ""
    _note_refusal(uid, action, status)
    return (f"\n\nСейчас заказ: <b>{status_ru(status)}</b> — "
            f"{refusal_reason(action, status)}")


def known_status(uid: int, oid: str) -> str:
    """Статус заказа по памяти опроса. Пусто — заказа он ещё не видел."""
    s = get_settings(uid)
    known = s.get("known_orders") or {}
    got = known.get(str(oid)) or known.get(oid)
    if got:
        return str(got)
    det = (s.get("known_order_details") or {}).get(str(oid)) or {}
    return str(det.get("status") or "")


@router.callback_query(OrderCallback.filter(F.action == "confirm_anyway"))
async def confirm_anyway(callback: CallbackQuery,
                         callback_data: OrderCallback,
                         api: YooMarketAPI) -> None:
    """Подтвердить неоплаченный заказ, когда продавец настоял.

    Предупреждение — не запрет: бывает, что деньги дошли, а опрос ещё не
    успел это увидеть. Решает продавец, но вслепую он этого не делает.
    """
    await _do_order_action(callback, callback_data.order_id, "confirm", api)


@router.callback_query(OrderCallback.filter(F.action.in_({"work", "confirm", "refund"})))
async def handle_order_action(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    api: YooMarketAPI,
) -> None:
    oid, action = callback_data.order_id, callback_data.action
    # Подтвердить неоплаченный заказ — заявить покупателю и площадке, что
    # сделка закрыта, пока денег нет. Маркетплейс это, скорее всего,
    # отклонит, но узнавать об этом из английского кода после нажатия —
    # плохой способ. Предупреждаем до, а решение оставляем продавцу:
    # деньги могли дойти между проходами опроса.
    if action == "confirm":
        from orderfields import is_paid, status_ru
        status = known_status(callback.from_user.id, oid)
        if status and not is_paid(status):
            b = InlineKeyboardBuilder()
            b.button(text="✅ Всё равно подтвердить",
                     callback_data=OrderCallback(
                         order_id=oid, action="confirm_anyway").pack())
            b.button(text="🔍 Детали заказа", callback_data=OrderCallback(
                order_id=oid, action="view").pack())
            b.button(text="⬅️ Заказы", callback_data="menu:orders")
            b.adjust(1)
            await callback.message.edit_text(
                f"⚠️ <b>Заказ #{_esc(oid)} не оплачен</b>\n\n"
                f"Сейчас: <b>{status_ru(status)}</b>\n\n"
                "Подтверждение означает, что сделка закрыта, — а денег за "
                "неё маркетплейс ещё не получил. Скорее всего он и не даст "
                "подтвердить.\n\n"
                "<i>Если оплата только что прошла, бот мог ещё не увидеть "
                "её: проверьте «Детали заказа».</i>",
                reply_markup=b.as_markup())
            await callback.answer()
            return
    await _do_order_action(callback, oid, action, api)


async def _do_order_action(callback: CallbackQuery, oid: str, action: str,
                           api: YooMarketAPI) -> None:
    labels = {"work": "▶️ Заказ взят в работу", "confirm": "✅ Заказ подтверждён", "refund": "↩️ Возврат оформлен"}
    try:
        if action == "work":
            await api.work_order(oid)
        elif action == "confirm":
            await api.confirm_order(oid)
        else:
            await api.refund_order(oid)
        text = f"{labels[action]} — заказ #{oid}"
    except Exception as e:
        # Текст ошибки идёт в HTML-сообщение, а приходит он с маркетплейса:
        # одиночный «<» роняет всю отправку, и продавец видит вечно
        # крутящуюся кнопку вместо причины. Плюс английский код ошибки на
        # этом экране — отписка, разбирать её продавцу не по чему.
        import html as _html
        from autoreply import explain_error
        why, _fixable = explain_error(str(e))
        text = f"❌ Не получилось: {_html.escape(why)}"
        # «Заказ в другом статусе» без названия статуса — половина ответа.
        # Дочитываем и показываем, в каком он на самом деле, и запоминаем
        # отказ: кнопку, которая в этом статусе не работает, предлагать
        # второй раз незачем.
        text += await _why_refused(callback.from_user.id, api, oid, action,
                                   str(e))

    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Детали заказа", callback_data=OrderCallback(order_id=oid, action="view").pack())
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
    b.adjust(1)
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "orders:filter:done")
async def filter_orders_done(callback: CallbackQuery) -> None:
    await callback.answer()
    # Общий список, а не свой: «success» — статус выполненного заказа у
    # этого маркетплейса, и без него фильтр показывал пусто при полном
    # магазине выполненных заказов.
    done_statuses = DONE
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
    b.adjust(1)
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
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "orders:filter:refunds")
async def filter_orders_refunds(callback: CallbackQuery) -> None:
    await callback.answer()
    refund_statuses = BACK
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
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


def _money_keys(node: dict) -> list[str]:
    """Поля верхнего уровня, в которых может лежать сумма, и что в них есть."""
    from orderfields import _to_number
    out: list[str] = []
    for key in ("price", "total", "sum", "amount", "cost", "total_price",
                "price_total", "paid", "paid_amount", "summ"):
        if key not in node:
            continue
        value = node.get(key)
        got = value.get("amount", value.get("value", value.get("sum"))) \
            if isinstance(value, dict) else value
        out.append(f"{key} = {str(value)[:60]}"
                   + ("" if _to_number(got) is not None else "  ← не число"))
    return out


def _price_line(where: str, node: dict) -> str:
    """«None» на экране продавца — такая же отписка, как английский код."""
    from orderfields import order_price
    got = order_price(node)
    return (f"{where}: <b>{money(got)} ₽</b>" if got is not None
            else f"{where}: <b>цены в ответе нет</b>")


@router.message(Command("order_debug"))
async def order_debug(message: Message, api: YooMarketAPI) -> None:
    """Один заказ: откуда взялась цена и почему он не взят в работу.

    Два вопроса, которые до сих пор выяснялись перепиской. Уведомление о
    покупке собирается из **списка** заказов, а карточка — из ответа по
    одному заказу, и это разные ответы маркетплейса. Пока их не сравнить,
    «💰 — ₽» в уведомлении при живой цене в карточке объяснить нечем.

    Только чтение: ничего не нажимает и ничего не меняет.
    """
    if not api:
        await message.answer("⚠️ Нет токена — отправьте /start")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Укажите номер: <code>/order_debug 1218314</code>")
        return
    oid = parts[1].lstrip("#")
    status = await message.answer("⏳ Читаю заказ…")

    from orderfields import (is_paid, needs_work, order_price, shape,
                             status_ru)
    from handlers.start import BOT_VERSION

    listed: dict = {}
    list_err = ""
    try:
        data = await api.get_orders()
        rows = data.get("data") or data.get("items") or []
        listed = next((r for r in rows if str(r.get("id")) == oid), {})
    except Exception as e:
        list_err = str(e)[:150]

    card: dict = {}
    card_err = ""
    try:
        got = await api.get_order(oid)
        card = got.get("data") if isinstance(got.get("data"), dict) else got
    except Exception as e:
        card_err = str(e)[:150]

    s = get_settings(message.from_user.id)
    det = ((s.get("known_order_details") or {}).get(oid) or {})
    aa = s.get("auto_accept", {}) or {}

    rows_out = [f"🔍 <b>Заказ #{_esc(oid)}</b>  <code>{_esc(BOT_VERSION)}</code>",
                "", "<b>💰 ЦЕНА</b>"]
    if list_err:
        rows_out.append(f"список заказов не прочитался: <code>{_esc(list_err)}</code>")
    elif not listed:
        rows_out.append("в списке заказов его нет — уведомление о покупке "
                        "собиралось не из этого ответа")
    else:
        rows_out.append(_price_line("в списке", listed))
        rows_out += [f"  <code>{_esc(k)}</code>" for k in _money_keys(listed)] \
            or ["  денежных полей в ответе нет"]
    if card_err:
        rows_out.append(f"карточка не прочиталась: <code>{_esc(card_err)}</code>")
    else:
        rows_out.append(_price_line("в карточке", card))
        rows_out += [f"  <code>{_esc(k)}</code>" for k in _money_keys(card)] \
            or ["  денежных полей в ответе нет"]

    raw_list = str(listed.get("status") or "—")
    raw_card = str(card.get("status") or "—")
    rows_out += ["", "<b>📊 СТАТУС</b>",
                 f"в списке: <code>{_esc(raw_list)}</code>   "
                 f"в карточке: <code>{_esc(raw_card)}</code>",
                 f"{status_ru(raw_card)} · оплачен: "
                 f"{'да' if is_paid(raw_card) else 'НЕТ'} · ждёт «в работу»: "
                 f"{'да' if needs_work(raw_card) else 'нет'}"]

    from tasks.manager import (_CATCHUP_HOURS, _CATCHUP_TRIES, _order_age,
                               _stars_awaiting_delivery)
    age = _order_age(det) if det else None
    hours = float(aa.get("catchup_hours") or _CATCHUP_HOURS)
    rows_out += ["", "<b>⚙️ ВЗЯТИЕ В РАБОТУ</b>",
                 f"тумблер: {'включён' if aa.get('enabled') else 'ВЫКЛЮЧЕН'}",
                 f"«в работу» = отчёт о выдаче: "
                 f"{'да' if aa.get('means_fulfilled') else 'нет'}",
                 f"ждёт ник для звёзд: "
                 f"{'да' if _stars_awaiting_delivery(s, oid) else 'нет'}",
                 f"бот знает этот заказ: {'да' if det else 'НЕТ'}",
                 f"возраст: {int(age // 3600) if age else 0} ч "
                 f"(берёт до {int(hours)} ч)" if age is not None
                 else "возраст: неизвестен",
                 f"жали «в работу»: {'да' if det.get('work_at') else 'нет'} · "
                 f"попыток: {det.get('work_tries', 0)} из {_CATCHUP_TRIES}"]
    if det.get("work_result"):
        rows_out.append(f"стало после нажатия: "
                        f"<code>{_esc(str(det['work_result']))}</code>")
    for label, key in (("отказ", "work_error"), ("причина пропуска", "work_skip")):
        if det.get(key):
            rows_out.append(f"{label}: <code>{_esc(str(det[key])[:150])}</code>")

    await status.edit_text("\n".join(rows_out))
    body = shape(listed)[:1500] if listed else "заказа в списке нет"
    await message.answer(
        f"<b>Ответ маркетплейса — список</b>\n<code>{_esc(body)}</code>")
    if card:
        await message.answer(f"<b>Ответ маркетплейса — карточка</b>\n"
                             f"<code>{_esc(shape(card)[:1500])}</code>")


@router.message(Command("orders_debug"))
async def orders_debug(message: Message, api: YooMarketAPI) -> None:
    """Что на самом деле лежит в заказе — только чтение, ничего не меняет.

    Имена полей у этого маркетплейса приходилось угадывать, и именно поэтому
    список заказов однажды состоял из прочерков. Здесь видно настоящую
    структуру ответа и то, как её прочитал бот, — если разбор снова промахнётся,
    это будет видно сразу, а не растворится в «—».
    """
    if not api:
        await message.answer("⚠️ Нет токена — отправьте /start")
        return
    status = await message.answer("⏳ Читаю заказы…")
    try:
        data = await api.get_orders()
    except Exception as e:
        await status.edit_text(f"❌ Заказы не загрузились: <code>{_esc(str(e)[:200])}</code>")
        return

    from orderfields import shape
    rows = data.get("data") or data.get("items") or (data if isinstance(data, list) else [])
    if not rows:
        await status.edit_text("🛒 Заказов в ответе нет.\n\n"
                               f"<code>{_esc(shape(data)[:2000])}</code>")
        return

    first = rows[0]
    d = describe(first)
    read = "\n".join(f"{k}: {v!r}" for k, v in d.items())
    await status.edit_text(
        f"🛒 <b>Заказов в ответе: {len(rows)}</b>\n\n"
        f"<b>Как прочитал бот:</b>\n<code>{_esc(read)}</code>\n\n"
        f"<b>Что прислал маркетплейс:</b>\n<code>{_esc(shape(first)[:2200])}</code>")
