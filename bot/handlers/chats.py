from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import ChatCallback, PaginationCallback, back_keyboard
from aiogram.filters import Command
from storage import get_settings, save_settings

router = Router()


def _newest_id(rows: list[dict]) -> str:
    """The largest message id present — not rows[-1], which is only the newest
    if the API sorts oldest-first."""
    best = ""
    for m in rows:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        try:
            newer = int(mid) > int(best) if best else True
        except (TypeError, ValueError):
            newer = mid > best
        if newer:
            best = mid
    return best


def _esc(text) -> str:
    """Text that came from outside — chat messages, labels, error bodies — goes
    into HTML-parsed messages, where a stray '<' makes Telegram reject the whole
    send and the reply silently never arrives."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _send_error(e: Exception) -> str:
    """A readable reason a chat message could not be sent.

    The Integration API only accepts a message while the chat has an ACTIVE
    order — support and moderation chats have none, and a finished order's chat
    stops accepting too. That comes back as no_active_orders_in_chat; showing
    the raw JSON left the seller with no idea what it meant."""
    s = str(e)
    if "no_active" in s or "no active order" in s.lower():
        return ("💬 В этом чате нет активного заказа, поэтому ответить через "
                "бота нельзя — так устроено API Юмаркета.\n\n"
                "Это чат поддержки/модерации или заказ уже закрыт. "
                "Ответьте прямо в панели: panel.yoomarket.net")
    return f"❌ Не отправилось: {_esc(s[:200])}"


class ReplyState(StatesGroup):
    waiting_for_text = State()
    waiting_chat_id = State()
    waiting_support_reply = State()   # reply to a support/moderation chat via the panel


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
        lines.append(f"{prefix}: {_esc(text)}")
    return "\n\n".join(lines)


# Состояние чата, в порядке важности. Номер заказа человеку ничего не говорит —
# он узнаёт переписку по покупателю и по товару, поэтому номер ушёл внутрь
# карточки чата, а на кнопке остались имя и название.
MARK_PROBLEM = "🔴"      # жалоба или спор — сюда нужно вмешаться
MARK_WAITING = "🟠"      # покупатель написал, ответа от человека не было
MARK_PLAIN = "💬"

# Через сколько «покупатель ждёт» превращается в проблему: полдня молчания по
# заказу — это уже повод для спора, а не просто непрочитанное.
_WAIT_ALARM = 12 * 3600


def _chat_state(det: dict, now: float = 0.0) -> tuple[str, int]:
    """(значок, вес для сортировки) по тому, что бот знает о чате."""
    import time as _t
    now = now or _t.time()
    waiting = float((det or {}).get("waiting", 0) or 0)
    if (det or {}).get("problem") or (waiting and now - waiting > _WAIT_ALARM):
        return MARK_PROBLEM, 2
    if waiting:
        return MARK_WAITING, 1
    return MARK_PLAIN, 0


def _order_label(order: dict, det: dict) -> str:
    """Подпись кнопки чата: кто и по какому товару.

    Раньше здесь стоял номер заказа первым — в списке из десяти чатов все
    кнопки начинались с «💬 #», и различать их приходилось по хвосту.
    """
    oid = str(order.get("id", ""))
    det = det or {}
    title = (order.get("title") or order.get("ad_title")
             or order.get("product_name") or det.get("title") or "")
    buyer = (order.get("buyer_name") or (order.get("buyer") or {}).get("name")
             or det.get("buyer") or "")
    title = "" if str(title) == "—" else str(title).strip()
    buyer = "" if str(buyer) == "—" else str(buyer).strip()

    mark, _ = _chat_state(det)
    if buyer and title:
        return f"{mark} {buyer[:16]} · {title[:24]}"
    return f"{mark} {buyer[:40] or title[:40] or f'Заказ #{oid}'}"


def _resolve_chat(uid: int, key: str) -> tuple[str, dict]:
    """(номер чата для API, что бот знает о заказе).

    Сообщения лежат под чатом, а не под заказом: GET /chats/{chat_id}/messages.
    Кнопки списка несут номер заказа — по нему находятся имя и товар, — а у
    заказа может быть свой номер чата. Фоновый опрос это уже учитывал, экран
    чата — нет, и там, где номера различаются, переписка не открывалась.
    """
    det = (get_settings(uid).get("known_order_details") or {}).get(str(key)) or {}
    return str(det.get("chat_id") or key), det


def _chat_header(key: str, det: dict) -> str:
    """Заголовок экрана чата: покупатель и товар, номер — мелким шрифтом."""
    buyer = str((det or {}).get("buyer") or "").strip()
    title = str((det or {}).get("title") or "").strip()
    who = " · ".join(p for p in (buyer, title) if p and p != "—")
    mark, _ = _chat_state(det)
    head = f"{mark} <b>{_esc(who)}</b>" if who else f"{mark} <b>Чат по заказу</b>"
    return f"{head}\n<code>#{_esc(key)}</code>"


def _mark_answered(uid: int, key: str) -> None:
    """Продавец ответил — гасим подсветку чата."""
    s = get_settings(uid)
    det = (s.get("known_order_details") or {}).get(str(key))
    if not det or not (det.get("waiting") or det.get("problem")):
        return
    det["waiting"] = 0
    det["problem"] = 0
    save_settings(uid, s)


def _build_chat_orders_keyboard(orders: list[dict], next_cursor: str | None,
                                watched: dict | None = None,
                                details: dict | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    details = details or {}
    # Проблемные — наверх: в списке на две страницы красный чат внизу второй
    # страницы виден не лучше, чем не помеченный вовсе.
    ordered = sorted(orders,
                     key=lambda o: -_chat_state(details.get(str(o.get("id", ""))) or {})[1])
    for order in ordered:
        oid = str(order.get("id", ""))
        builder.button(text=_order_label(order, details.get(oid) or {}),
                       callback_data=ChatCallback(chat_id=oid).pack())
    builder.adjust(1)
    if next_cursor:
        builder.button(text="Следующая →", callback_data=PaginationCallback(entity="chat_orders", cursor=next_cursor).pack())
    # Support and moderation live outside orders, so they get their own entry.
    # It must survive an empty order list — otherwise followed chats become
    # unreachable exactly when they are the only chats there are.
    n = len(watched or {})
    builder.button(text=f"🛟 Поддержка и модерация{f' · {n}' if n else ''}",
                   callback_data="wchats:list")
    builder.button(text="⬅️ Чаты", callback_data="menu:chats")
    return builder.as_markup()


# Чаты, которые есть у каждого продавца и живут вне заказов. Ищутся среди
# отслеживаемых по названию: номер у каждого магазина свой, а название задаёт
# сам продавец, когда добавляет чат.
_WELL_KNOWN = {
    "support": {"label": "Поддержка", "icon": "🛟", "title": "Чат с поддержкой",
                "words": ("поддерж", "support", "саппорт")},
    "market": {"label": "Юмаркет", "icon": "🏪", "title": "Чат с Юмаркет",
               "words": ("юмаркет", "yoomarket", "маркет", "модерац", "админ")},
}


def _find_well_known(watched: dict, kind: str) -> str:
    """Номер отслеживаемого чата, чьё название похоже на искомое, или ""."""
    words = _WELL_KNOWN[kind]["words"]
    for cid, info in (watched or {}).items():
        label = str((info or {}).get("label") or "").lower()
        if any(w in label for w in words):
            return str(cid)
    return ""


def _hub_kb(watched: dict, ar_on: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Список чатов", callback_data="chats:list")
    b.button(text=f"{_WELL_KNOWN['support']['icon']} Чат с поддержкой",
             callback_data="chats:kind:support")
    b.button(text=f"{_WELL_KNOWN['market']['icon']} Чат с Юмаркет",
             callback_data="chats:kind:market")
    b.button(text=f"📩 Автоответы {'🟢' if ar_on else '🔴'}",
             callback_data="ar:menu")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1, 2, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "menu:chats")
async def chats_hub(callback: CallbackQuery, state: FSMContext) -> None:
    """Раздел «Чаты»: переписка и автоответы в одном месте.

    Раньше нажатие сразу грузило список заказов — на это уходил запрос к
    маркетплейсу, а поддержка и автоответы прятались за ним. Хаб рисуется из
    того, что уже сохранено, поэтому открывается мгновенно.
    """
    await state.clear()
    s = get_settings(callback.from_user.id)
    watched = s.get("watched_chats") or {}
    conf = s.get("autoreplies", {}) or {}
    ar_on = bool(conf.get("enabled"))
    live = [r for r in (conf.get("rules") or []) if r.get("on", True)]

    details = s.get("known_order_details") or {}
    problem = waiting = 0
    for det in details.values():
        mark, _ = _chat_state(det or {})
        problem += mark == MARK_PROBLEM
        waiting += mark == MARK_WAITING

    lines = ["💬 <b>Чаты</b>", ""]
    if problem or waiting:
        parts = []
        if problem:
            parts.append(f"{MARK_PROBLEM} требуют внимания: <b>{problem}</b>")
        if waiting:
            parts.append(f"{MARK_WAITING} ждут ответа: <b>{waiting}</b>")
        lines.append("📋 " + " · ".join(parts))
    else:
        lines.append("📋 Переписка по заказам — в «Списке чатов».")
    for kind, meta in _WELL_KNOWN.items():
        cid = _find_well_known(watched, kind)
        mark, _ = _chat_state(watched.get(cid) or {}) if cid else (MARK_PLAIN, 0)
        icon = mark if mark != MARK_PLAIN else meta["icon"]
        lines.append(f"{icon} {meta['title']}: "
                     + (f"<code>#{_esc(cid)}</code>" if cid else "<i>не добавлен</i>"))
    if ar_on:
        extra = (f"правил: {len(live)}" if live or (conf.get("fallback") or {}).get("on")
                 else "⚠️ правил нет — бот молчит")
        lines.append(f"📩 Автоответы: 🟢 включены, {extra}")
    else:
        lines.append("📩 Автоответы: 🔴 выключены")

    await callback.message.edit_text("\n".join(lines),
                                     reply_markup=_hub_kb(watched, ar_on))
    await callback.answer()


@router.callback_query(F.data.startswith("chats:kind:"))
async def chats_well_known(callback: CallbackQuery, state: FSMContext) -> None:
    """Открыть поддержку / Юмаркет — или предложить добавить, если её нет."""
    kind = callback.data.rsplit(":", 1)[-1]
    meta = _WELL_KNOWN.get(kind)
    if not meta:
        await callback.answer("Неизвестный чат", show_alert=True)
        return
    watched = get_settings(callback.from_user.id).get("watched_chats") or {}
    cid = _find_well_known(watched, kind)
    if cid:
        await state.clear()
        await _wchat_screen(callback, cid)
        return

    # Номер такого чата бот сам узнать не может: в API нет списка чатов.
    await state.set_state(ReplyState.waiting_chat_id)
    await state.update_data(label=meta["label"])
    b = InlineKeyboardBuilder()
    b.button(text="📋 Другие чаты вне заказов", callback_data="wchats:list")
    b.button(text="❌ Отмена", callback_data="menu:chats")
    b.adjust(1)
    await callback.message.edit_text(
        f"{meta['icon']} <b>{meta['title']}</b>\n\n"
        "Такой чат ещё не добавлен. Пришлите его номер или ссылку — "
        "он есть в адресе чата в панели:\n"
        "<code>panel.yoomarket.net/chats/<b>1076867</b></code>\n\n"
        f"<i>Название подставлю сам: «{meta['label']}».</i>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "chats:list")
async def show_chats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю чаты...")
    s = get_settings(callback.from_user.id)
    watched = s.get("watched_chats") or {}
    details = s.get("known_order_details") or {}
    try:
        data = await api.get_orders()
        orders: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = data.get("meta", {}).get("next_cursor")
        if orders:
            text = "💬 <b>Чаты</b>\n" + _legend(orders, details)
        else:
            text = ("💬 <b>Чаты</b>\n\nПо заказам чатов пока нет."
                    + (f"\nОтслеживаемых чатов вне заказов: <b>{len(watched)}</b>."
                       if watched else ""))
        keyboard = _build_chat_orders_keyboard(orders, next_cursor, watched, details)
    except Exception as e:
        # The order list failing must not hide chats that do not depend on it
        text = f"❌ Заказы не загрузились: {e}"
        keyboard = _build_chat_orders_keyboard([], None, watched, details)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def _legend(orders: list[dict], details: dict) -> str:
    """Что означают цвета — но только те, которые сейчас есть в списке."""
    problem = waiting = 0
    for o in orders:
        mark, _ = _chat_state(details.get(str(o.get("id", ""))) or {})
        problem += mark == MARK_PROBLEM
        waiting += mark == MARK_WAITING
    if not problem and not waiting:
        return "Все отвечены. Выберите чат:"
    parts = []
    if problem:
        parts.append(f"{MARK_PROBLEM} требует внимания: <b>{problem}</b>")
    if waiting:
        parts.append(f"{MARK_WAITING} ждут ответа: <b>{waiting}</b>")
    return " · ".join(parts)


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
        s = get_settings(callback.from_user.id)
        details = s.get("known_order_details") or {}
        text = "💬 <b>Чаты</b>\n" + _legend(orders, details)
        keyboard = _build_chat_orders_keyboard(
            orders, next_cursor, s.get("watched_chats") or {}, details)
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
    await callback.message.edit_text("⏳ Загружаю чат...")
    real_id, det = _resolve_chat(callback.from_user.id, chat_id)
    try:
        data = await api.get_messages(real_id)
        messages: list[dict] = data.get("data") or data.get("items") or []
        messages = messages[-10:] if len(messages) > 10 else messages
        text = _chat_header(chat_id, det) + "\n\n" + _format_messages(messages)
        settings = get_settings(callback.from_user.id)
        quick_replies: list = settings.get("quick_replies", [])
        builder = InlineKeyboardBuilder()
        builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
        # quick replies each full-width (long text), then 2-col nav
        for i, qr in enumerate(quick_replies[:3]):
            builder.button(text=f"💬 {qr[:28]}", callback_data=f"qr:{chat_id}:{i}")
        builder.button(text="🔄 Обновить", callback_data=ChatCallback(chat_id=chat_id).pack())
        builder.button(text="⬅️ Чаты", callback_data="menu:chats")
        n_qr = len(quick_replies[:3])
        builder.adjust(1, *([1] * n_qr), 2)  # reply, each qr, then nav pair
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
    real_id, det = _resolve_chat(callback.from_user.id, chat_id)
    try:
        data = await api.get_messages(real_id)
        messages: list[dict] = data.get("data") or data.get("items") or []
        messages = messages[-10:] if len(messages) > 10 else messages
        text = _chat_header(chat_id, det) + "\n\n" + _format_messages(messages)
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

    real_id, det = _resolve_chat(message.from_user.id, chat_id)
    try:
        await api.send_message(real_id, text_to_send)
        who = str((det or {}).get("buyer") or "").strip()
        text = "✅ Отправлено" + (f" покупателю {_esc(who)}" if who and who != "—"
                                 else f" в чат #{_esc(chat_id)}")
        _mark_answered(message.from_user.id, chat_id)
    except Exception as e:
        text = _send_error(e)

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
    real_id, _det = _resolve_chat(callback.from_user.id, chat_id)
    try:
        await api.send_message(real_id, text)
        _mark_answered(callback.from_user.id, chat_id)
        await callback.answer("✅ Отправлено!", show_alert=True)
    except Exception as e:
        # Alerts are short and plain-text; strip the HTML the helper adds
        msg = _send_error(e).replace("<b>", "").replace("</b>", "")
        await callback.answer(msg[:200], show_alert=True)


# ---------------------------------------------------------------------------
# Chats outside orders — support, moderation
# ---------------------------------------------------------------------------

@router.message(Command("watch_chat"))
async def watch_chat(message: Message, api: YooMarketAPI) -> None:
    """/watch_chat 1076867 [название] — follow a chat that has no order.

    Support and moderation conversations belong to no order, and the API has no
    way to list chats, so their ids cannot be discovered — they are added here
    and then polled like any other chat.
    """
    parts = (message.text or "").split(maxsplit=2)
    settings = get_settings(message.from_user.id)
    watched: dict = settings.setdefault("watched_chats", {})

    if len(parts) < 2:
        if watched:
            lines = [f"• <code>{_esc(cid)}</code> — {_esc(i.get('label') or 'без названия')}"
                     for cid, i in watched.items()]
            body = "\n".join(lines)
        else:
            body = "<i>пока ни одного</i>"
        await message.answer(
            f"🛟 <b>Отслеживаемые чаты</b>\n\n{body}\n\n"
            f"Добавить: <code>/watch_chat 1076867 Поддержка</code>\n"
            f"Убрать: <code>/unwatch_chat 1076867</code>\n\n"
            f"<i>Номер возьмите из адреса чата в панели: "
            f"panel.yoomarket.net/chats/<b>1076867</b></i>")
        return

    chat_id = parts[1].strip().rstrip("/").split("/")[-1]
    label = parts[2].strip() if len(parts) > 2 else "Поддержка"
    await _add_watched(message, api, chat_id, label)


@router.message(Command("unwatch_chat"))
async def unwatch_chat(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер: <code>/unwatch_chat 1076867</code>")
        return
    chat_id = parts[1].strip()
    settings = get_settings(message.from_user.id)
    watched: dict = settings.setdefault("watched_chats", {})
    if watched.pop(chat_id, None) is None:
        await message.answer(f"Чат #{chat_id} и так не отслеживается")
        return
    save_settings(message.from_user.id, settings)
    await message.answer(f"✅ Чат #{chat_id} больше не отслеживается")


def _wchats_text(watched: dict) -> str:
    if not watched:
        return "<i>пока ни одного</i>"
    return "\n".join(f"• <b>{_esc(i.get('label') or 'Чат')}</b> — <code>#{_esc(c)}</code>"
                     for c, i in watched.items())


def _wchats_kb(watched: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    rows = sorted(watched.items(), key=lambda kv: -_chat_state(kv[1] or {})[1])
    for cid, info in rows[:20]:
        mark, _ = _chat_state(info or {})
        b.button(text=f"{mark if mark != MARK_PLAIN else '🛟'} "
                      f"{(info.get('label') or 'Чат')[:24]}",
                 callback_data=f"wchat:{cid}")
    b.adjust(1)
    b.button(text="➕ Добавить чат", callback_data="wchats:add")
    b.button(text="⬅️ Чаты", callback_data="menu:chats")
    b.adjust(1, 2)
    return b.as_markup()


@router.callback_query(F.data == "wchats:list")
async def wchats_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    watched = get_settings(callback.from_user.id).get("watched_chats") or {}
    await callback.message.edit_text(
        f"🛟 <b>Поддержка и модерация</b>\n\n{_wchats_text(watched)}\n\n"
        f"<i>Чаты вне заказов — их номер берётся из адреса в панели:\n"
        f"panel.yoomarket.net/chats/<b>1076867</b></i>",
        reply_markup=_wchats_kb(watched))
    await callback.answer()


@router.callback_query(F.data == "wchats:add")
async def wchats_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReplyState.waiting_chat_id)
    await state.update_data(label="")   # без подсказанного названия, спросим текстом
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="wchats:list")
    await callback.message.edit_text(
        "➕ <b>Добавить чат</b>\n\n"
        "Пришлите номер чата или ссылку на него, можно с названием:\n"
        "<code>1076867 Поддержка</code>\n"
        "<code>https://panel.yoomarket.net/chats/1076867</code>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(ReplyState.waiting_chat_id)
async def wchats_add_save(message: Message, state: FSMContext,
                          api: YooMarketAPI) -> None:
    # Название запоминается тем экраном, который спрашивал номер: из «Чата с
    # поддержкой» продавец шлёт одну цифру, и чат должен называться так, чтобы
    # кнопка потом его нашла.
    data = await state.get_data()
    preset = str(data.get("label") or "").strip()
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if not parts:
        await message.answer("❌ Пришлите номер чата")
        return
    chat_id = parts[0].strip().rstrip("/").split("/")[-1]
    label = (parts[1].strip() if len(parts) > 1 else "") or preset or "Поддержка"
    await _add_watched(message, api, chat_id, label)


@router.callback_query(F.data.startswith("wchat:"))
async def wchat_detail(callback: CallbackQuery) -> None:
    await _wchat_screen(callback, callback.data.split(":", 1)[1])


async def _wchat_screen(callback: CallbackQuery, cid: str) -> None:
    """Экран одного чата вне заказов.

    Отдельно от обработчика, потому что на него ведут и кнопки «Чат с
    поддержкой» / «Чат с Юмаркет»: подменять callback.data ради переиспользования
    нельзя — это pydantic-модель.
    """
    watched = get_settings(callback.from_user.id).get("watched_chats") or {}
    info = watched.get(cid) or {}
    digits = "".join(ch for ch in str(cid) if ch.isdigit())
    b = InlineKeyboardBuilder()
    b.button(text="📜 Показать историю", callback_data=f"wchat_hist:{cid}")
    # Reply in-bot via the panel chat API (support has no active order, so the
    # marketplace API refuses it — the panel token is used instead).
    b.button(text="✉️ Ответить", callback_data=f"sreply:{cid}")
    b.button(text="🌐 Открыть в панели",
             url=f"https://panel.yoomarket.net/chats/{digits or cid}")
    b.button(text="🗑 Убрать", callback_data=f"wchat_del:{cid}")
    b.button(text="⬅️ Назад", callback_data="wchats:list")
    b.adjust(1, 2, 2)
    await callback.message.edit_text(
        f"🛟 <b>{_esc(info.get('label') or 'Чат')}</b>\n\n"
        f"💬 Номер: <code>#{_esc(cid)}</code>\n\n"
        f"<i>Читать и отвечать можно прямо здесь. Ответ уходит через панель — "
        f"для этого нужен вход в панель по email.</i>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("sreply:"))
async def support_reply_init(callback: CallbackQuery, state: FSMContext) -> None:
    cid = callback.data.split(":", 1)[1]
    from storage import get_panel_creds
    creds = get_panel_creds(callback.from_user.id) or {}
    if not creds.get("chat_token"):
        b = InlineKeyboardBuilder()
        b.button(text="🌐 Войти в панель", callback_data="panel:sms_start")
        b.button(text="⬅️ Назад", callback_data=f"wchat:{cid}")
        b.adjust(1)
        await callback.message.edit_text(
            "🔑 <b>Нужен вход в панель</b>\n\n"
            "Ответ в поддержку идёт через панель, а для этого боту нужен токен, "
            "который выдаётся при входе по email. Войдите заново — токен "
            "сохранится, и ответы заработают.",
            reply_markup=b.as_markup())
        await callback.answer()
        return
    await state.set_state(ReplyState.waiting_support_reply)
    await state.update_data(support_chat_id=cid)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"wchat:{cid}")
    await callback.message.edit_text(
        f"✉️ Напишите ответ в чат #{_esc(cid)}:", reply_markup=b.as_markup())
    await callback.answer()


@router.message(ReplyState.waiting_support_reply)
async def support_reply_send(message: Message, state: FSMContext) -> None:
    import asyncio

    from automation.panel import panel_chat_send_sync
    from storage import get_panel_creds

    data = await state.get_data()
    cid = data.get("support_chat_id", "")
    await state.clear()
    text = (message.text or "").strip()
    if not cid or not text:
        await message.answer("❌ Пустой ответ.")
        return
    creds = get_panel_creds(message.from_user.id) or {}
    status = await message.answer("⏳ Отправляю в поддержку...")
    try:
        loop = asyncio.get_event_loop()
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, panel_chat_send_sync,
                                 creds.get("cookies", ""), cid, text,
                                 creds.get("chat_token", "")),
            timeout=60)
    except Exception as e:
        ok, msg = False, str(e)[:150]
    if ok:
        # Ответ отправлен — чат больше не ждёт нас.
        s = get_settings(message.from_user.id)
        info = (s.get("watched_chats") or {}).get(cid)
        if info and (info.get("waiting") or info.get("problem")):
            info["waiting"] = 0
            info["problem"] = 0
            save_settings(message.from_user.id, s)
    b = InlineKeyboardBuilder()
    b.button(text="💬 К чату", callback_data=f"wchat:{cid}")
    b.button(text="⬅️ Чаты", callback_data="menu:chats")
    b.adjust(1)
    await status.edit_text(
        (f"✅ {_esc(msg)}" if ok else f"❌ {_esc(msg)}"),
        reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("wchat_del:"))
async def wchat_del(callback: CallbackQuery) -> None:
    cid = callback.data.split(":", 1)[1]
    settings = get_settings(callback.from_user.id)
    watched = settings.setdefault("watched_chats", {})
    watched.pop(cid, None)
    save_settings(callback.from_user.id, settings)
    await callback.answer("Убран", show_alert=True)
    await callback.message.edit_text(
        f"🛟 <b>Поддержка и модерация</b>\n\n{_wchats_text(watched)}",
        reply_markup=_wchats_kb(watched))


@router.callback_query(F.data.startswith("wchat_hist:"))
async def wchat_history(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Send the messages already in the chat — following it only reports what
    arrives afterwards, so past ones would otherwise stay unseen."""
    cid = callback.data.split(":", 1)[1]
    if not api:
        await callback.answer("⚠️ Не настроен API-токен", show_alert=True)
        return
    await callback.answer("⏳ Загружаю историю...")
    try:
        data = await api.get_messages(cid)
        rows = data.get("data") or data.get("items") or []
    except Exception as e:
        await callback.message.answer(f"❌ Не удалось: {_esc(str(e)[:200])}")
        return
    if not rows:
        await callback.message.answer("Здесь пока нет сообщений.")
        return

    info = (get_settings(callback.from_user.id).get("watched_chats") or {}).get(cid) or {}
    label = _esc(info.get("label") or f"Чат #{cid}")
    await callback.message.answer(
        f"📜 <b>{label}</b> — последние {min(len(rows), 15)} из {len(rows)}")

    for msg in rows[-15:]:
        text = (msg.get("text") or msg.get("message") or "").strip()
        if not text:
            continue
        sender = msg.get("sender_type") or msg.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("type") or sender.get("role") or ""
        mine = bool(msg.get("is_mine") or msg.get("is_own")) or str(sender).lower() in (
            "me", "self", "own", "shop", "seller")
        who = "🟢 Вы" if mine else f"🛟 {label}"
        when = _fmt_msg_time(msg.get("created_at") or msg.get("date"))
        await callback.message.answer(
            f"{who}{('  •  🕐 ' + when) if when else ''}\n"
            f"<blockquote>{_esc(text[:900])}</blockquote>")


def _fmt_msg_time(raw) -> str:
    if not raw:
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%d.%m %H:%M")
    except Exception:
        return str(raw)[:16]


async def _add_watched(message: Message, api: YooMarketAPI,
                       chat_id: str, label: str) -> None:
    """Verify a chat is readable, then follow it from its newest message."""
    if not api:
        await message.answer("⚠️ Не настроен API-токен")
        return
    # People paste the id however it appears — <963101>, #963101, a full url.
    # Anything but the digits also lands in the error text below, where an
    # angle bracket would break the message's HTML and silence the reply.
    chat_id = "".join(ch for ch in str(chat_id) if ch.isdigit())
    if not chat_id:
        await message.answer("❌ В номере чата нет цифр. Пример: "
                             "<code>/watch_chat 1076867 Поддержка</code>")
        return
    status = await message.answer(f"⏳ Проверяю чат #{chat_id}...")
    try:
        data = await api.get_messages(chat_id)
        rows = data.get("data") or data.get("items") or []
    except Exception as e:
        await status.edit_text(
            f"❌ Чат #{chat_id} не читается:\n<code>{_esc(str(e)[:250])}</code>")
        return
    settings = get_settings(message.from_user.id)
    watched = settings.setdefault("watched_chats", {})
    # The largest id, not the last row: the baseline has to be the newest
    # message however the API happens to sort them, or following the chat
    # starts from the wrong point.
    watched[str(chat_id)] = {
        "label": label,
        "last_msg": _newest_id(rows) or None,
    }
    save_settings(message.from_user.id, settings)
    await status.edit_text(
        f"✅ <b>{_esc(label)}</b> добавлен\n\n"
        f"💬 <code>#{chat_id}</code> · сообщений: {len(rows)}\n\n"
        f"Новые сообщения придут уведомлениями. "
        f"Прошлые — кнопкой «📜 Показать историю».",
        reply_markup=_wchats_kb(watched))


@router.message(Command("chats_debug"))
async def chats_debug(message: Message, api: YooMarketAPI) -> None:
    """What the follower sees right now, per watched chat.

    Whether a support message would have produced a notification is otherwise
    only answerable by waiting for one to arrive.
    """
    watched = get_settings(message.from_user.id).get("watched_chats") or {}
    if not watched:
        await message.answer("Отслеживаемых чатов нет.")
        return
    if not api:
        await message.answer("⚠️ Не настроен API-токен")
        return

    out: list[str] = []
    for cid, info in watched.items():
        out.append(f"#{cid} — {info.get('label') or 'без названия'}")
        out.append(f"  запомнено последнее: {info.get('last_msg')}")
        try:
            data = await api.get_messages(cid)
        except Exception as e:
            out.append(f"  чат не читается: {str(e)[:90]}")
            continue
        rows = data.get("data") or data.get("items") or []
        out.append(f"  сообщений сейчас: {len(rows)}")
        out.append(f"  самое новое: {_newest_id(rows) or '—'}")
        for m in rows[-3:]:
            sender = m.get("sender_type") or m.get("sender") or "—"
            if isinstance(sender, dict):
                sender = sender.get("type") or sender.get("role") or "—"
            body = str(m.get("text") or m.get("message") or "")[:40]
            out.append(f"  • id={m.get('id')} от={sender} «{body}»")
    await message.answer(f"<code>{_esc(chr(10).join(out))[:3500]}</code>")
