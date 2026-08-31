"""Команды-ярлыки: то же, что кнопки главного меню, но с клавиатуры.

Продавец задал список команд в BotFather, и каждая обязана открывать
НАСТОЯЩИЙ экран, а не свой урезанный дубль. Две копии одного экрана — это
два места, где чинить одну беду, и одно из них обязательно забудут.

Поэтому команда зовёт тот же обработчик, что и кнопка. Разница у них одна:
кнопка ПРАВИТ сообщение, под которым нажата, а команде править нечего — ей
нужно новое. Её и подставляет `AsCallback`.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import ui
from storage import get_token

logger = logging.getLogger(__name__)
router = Router()

# Подписи кнопок нужны фильтру на уровне модуля.
from keyboards.reply import LABELS as _KB_LABELS  # noqa: E402


class _Screen:
    """Сообщение экрана: сначала отправляется, потом правится на месте.

    Экраны написаны под кнопку и говорят `edit_text`. Под командой править
    нечего, поэтому первый `edit_text` отправляет новое сообщение, а все
    следующие правят уже его — иначе «⏳ Загружаю…» и результат легли бы в
    чат двумя сообщениями, и продавец читал бы устаревшее первым.
    """

    def __init__(self, message: Message) -> None:
        self._src = message
        self._sent: Message | None = None
        self.from_user = message.from_user
        self.chat = getattr(message, "chat", None)
        self.bot = getattr(message, "bot", None)

    async def edit_text(self, text, reply_markup=None, **kw):
        if self._sent is None:
            self._sent = await self._src.answer(text, reply_markup=reply_markup,
                                                **kw)
        else:
            await self._sent.edit_text(text, reply_markup=reply_markup, **kw)
        return self._sent

    async def answer(self, text, reply_markup=None, **kw):
        return await self._src.answer(text, reply_markup=reply_markup, **kw)

    async def delete(self):                      # экранам иногда нужно и это
        if self._sent is not None:
            await self._sent.delete()


class AsCallback:
    """Команда, притворяющаяся нажатой кнопкой — ровно настолько, насколько
    это нужно экранам: `message`, `from_user`, `data` и пустой `answer`."""

    def __init__(self, message: Message, data: str) -> None:
        self.message = _Screen(message)
        self.from_user = message.from_user
        self.bot = getattr(message, "bot", None)
        self.data = data
        self.id = "cmd"

    async def answer(self, *a, **kw):
        """Кнопке Telegram нужен ответ, чтобы перестать «крутиться».
        У команды крутиться нечему."""
        return None


async def _needs_shop(message: Message, state: FSMContext) -> bool:
    """Сказать, что магазин не подключён, и вернуть True.

    Открывать раздел без токена нечем: экран уйдёт в маркетплейс и вернётся
    с отказом, который выглядит поломкой бота, а не отсутствием доступа.
    """
    if get_token(message.from_user.id):
        return False
    from handlers.start import AuthState, _hello_kb
    await state.set_state(AuthState.waiting_for_token)
    await message.answer(ui.screen(
        "🔌 <b>Магазин ещё не подключён</b>",
        ["Открывать пока нечего. Подключим — и раздел заработает."]),
        reply_markup=_hello_kb())
    return True


# --- Разделы главного меню --------------------------------------------------
#
# Каждая команда — вход в тот же экран, что и кнопка рядом с ней в меню.

@router.message(Command("ads"))
async def cmd_ads(message: Message, state: FSMContext, **data) -> None:
    if await _needs_shop(message, state):
        return
    from handlers.ads import ads_menu
    await ads_menu(AsCallback(message, "menu:ads"), data.get("api"))


@router.message(Command("orders"))
async def cmd_orders(message: Message, state: FSMContext, **data) -> None:
    if await _needs_shop(message, state):
        return
    from handlers.orders import show_orders
    await show_orders(AsCallback(message, "menu:orders"), data.get("api"))


@router.message(Command("chats"))
async def cmd_chats(message: Message, state: FSMContext) -> None:
    if await _needs_shop(message, state):
        return
    from handlers.chats import chats_hub
    await chats_hub(AsCallback(message, "menu:chats"), state)


@router.message(Command("balance"))
async def cmd_balance(message: Message, state: FSMContext, **data) -> None:
    if await _needs_shop(message, state):
        return
    from handlers.balance import show_balance
    await show_balance(AsCallback(message, "menu:balance"), data.get("api"))


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext, **data) -> None:
    if await _needs_shop(message, state):
        return
    from handlers.stats import show_stats
    await show_stats(AsCallback(message, "menu:stats"), data.get("api"))


@router.message(Command("stars"))
async def cmd_stars(message: Message, state: FSMContext) -> None:
    """Автовыдача звёзд. Токен здесь не нужен: экран показывает настройки
    плагина, а не данные магазина."""
    from handlers.plugins import stars_screen
    await stars_screen(AsCallback(message, "plugins:auto_stars"), state)


# --- Прокси -----------------------------------------------------------------
#
# Своего раздела у прокси не было: они настраивались по отдельности внутри
# AppRoute и внутри AutoStars, и продавец, задавший один, не догадывался о
# втором. Здесь оба видны сразу — со своими адресами и своими кнопками.

@router.message(Command("proxy"))
async def cmd_proxy(message: Message, state: FSMContext) -> None:
    """Мои прокси: где какой задан и куда идти его менять."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from automation.fragment import proxy_label
    from storage import get_ar_creds, get_fragment_creds

    uid = message.from_user.id
    ar = (get_ar_creds(uid) or {}).get("proxy", "")
    fr = (get_fragment_creds(uid) or {}).get("proxy", "")

    # Показываем только хост и порт: в строке прокси лежат логин и пароль,
    # и это такой же чужой доступ, как куки.
    body = [
        "Прокси нужен там, где у сервиса белый список адресов, а адрес "
        "сервера меняется. В список вписывается прокси, а не сервер.",
        "",
        f"📦 <b>AppRoute</b> — {ui.esc(proxy_label(ar))}",
        f"⭐ <b>AutoStars и Fragment</b> — {ui.esc(proxy_label(fr))}",
    ]
    if not ar and not fr:
        body += ["", "<i>Ни одного не задано. Пока сервисы отвечают — он и "
                     "не нужен.</i>"]

    b = InlineKeyboardBuilder()
    b.button(text="📦 Прокси AppRoute", callback_data="apr:proxy")
    b.button(text="⭐ Прокси AutoStars", callback_data="plugins:stars:set_proxy")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    await message.answer(
        ui.screen("🌐 <b>Мои прокси</b>", body,
                  footer="<i>🔒 Логин и пароль не показываю — только адрес "
                         "и порт.</i>"),
        reply_markup=ui.lay(b).as_markup())


# --- Поддержка --------------------------------------------------------------

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Куда писать, если что-то не так.

    Отдельно — что приложить к вопросу. «Не работает» без версии и номера
    заказа означает ещё один круг переписки, а продавец в этот момент уже
    теряет деньги.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from storage import get_support_contact

    contact = get_support_contact()
    b = InlineKeyboardBuilder()
    if contact.startswith("@"):
        b.button(text=f"✍️ Написать {contact}",
                 url=f"https://t.me/{contact[1:]}")
    b.button(text="📄 Документы и условия", callback_data="menu:policy")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    from handlers.start import BOT_VERSION
    await message.answer(ui.screen(
        "🧡 <b>Поддержка</b>",
        [f"Пиши сюда: {ui.esc(contact)}",
         "",
         "<b>Что приложить к вопросу</b>",
         f"• код сборки: <code>{BOT_VERSION}</code>",
         "• номер заказа, если беда с конкретным заказом",
         "• что ты сделал и что увидел вместо ожидаемого",
         "",
         "<i>С этим отвечу сразу. Без этого придётся сначала спрашивать "
         "то же самое, а время идёт.</i>"]),
        reply_markup=ui.lay(b).as_markup())


# --- AutoPUBG ---------------------------------------------------------------

@router.message(Command("pubg"))
async def cmd_pubg(message: Message) -> None:
    """Плагина автовыдачи PUBG в боте нет.

    Команда в списке Telegram есть, и молчать на неё нельзя: продавец
    решит, что бот сломался. Но и рисовать экран несуществующего плагина
    нельзя тем более — выдумывать за поставщика номиналы значит обещать
    выдачу, которой не будет.

    Сказать правду и назвать, чем это чинится, дешевле обоих вариантов.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    b = InlineKeyboardBuilder()
    b.button(text="🧩 Что есть сейчас", callback_data="plugins:menu")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    await message.answer(ui.screen(
        "🎮 <b>AutoPUBG пока нет</b>",
        ["Такого плагина в боте не сделано — ни выдачи, ни настроек.",
         "",
         "Сейчас автовыдача умеет: <b>звёзды Telegram</b> и "
         "<b>гифт-карты кодами</b> — Robux, Apple, PSN, Steam, Xbox и ещё "
         "восемь.",
         "",
         "<b>Чтобы добавить PUBG</b>, нужно знать, что по нему есть у "
         "поставщика. Посмотри и покажи владельцу бота:",
         "<code>/apr_stock pubg</code>",
         "",
         "<i>Механизм готов: карта добавляется одной строкой. Не хватает "
         "только номиналов поставщика — угадывать их нельзя, промах здесь "
         "означает купленный не тот товар.</i>"]),
        reply_markup=ui.lay(b).as_markup())


# --- Журнал в группу --------------------------------------------------------
#
# Номер группы и номер темы нигде не показываются: их не «узнают», а
# приносят — командой, запущенной в нужной теме. Просить владельца найти
# `-1002…` и `message_thread_id` руками значит просить его ошибиться.

@router.message(Command("log_here"))
async def cmd_log_here(message: Message) -> None:
    """Привязать эту тему к виду событий. Без слова — показать, что есть."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    import logs
    from storage import (clear_log_topic, get_log_target, is_admin,
                         set_log_topic)

    # Кто это. При анонимной отправке — «от имени группы» — Telegram НЕ
    # называет человека: вместо него приходит служебный GroupAnonymousBot,
    # и проверить права не по чему. Молча выйти здесь нельзя: снаружи это
    # неотличимо от «команды не существует», а причина совсем другая.
    anon = getattr(message, "sender_chat", None)
    if anon is not None:
        await message.answer(ui.screen(
            "🕶 <b>Не вижу, кто ты</b>",
            ["Сообщение отправлено <b>от имени группы</b>, а в этом случае "
             "Telegram не называет человека — вместо него приходит "
             "служебный аккаунт.",
             "",
             "<b>Как отправить от себя:</b>",
             "1. Нажми на название группы сверху",
             "2. Карандаш ✏️ → <b>Администраторы</b>",
             "3. Найди себя (ты в списке сверху) → нажми",
             "4. Выключи <b>«Оставаться анонимным»</b> и сохрани",
             "",
             "<i>Потом повтори команду здесь же. Привяжешь все темы — "
             "анонимность можно вернуть, на журнал это не влияет.</i>"]))
        return

    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        # Тоже говорим вслух: если владелец завёл группу и пишет туда с
        # другого аккаунта, тишина отправит его чинить не то.
        await message.answer(ui.screen(
            "🔒 <b>Журналом распоряжается админ бота</b>",
            [f"Твой номер: <code>{uid}</code> — среди админов его нет.",
             "",
             "Админы добавляются в «👑 Админ-панель» в личке с ботом."]))
        return

    chat = getattr(message.chat, "id", 0)
    thread = getattr(message, "message_thread_id", None)
    parts = (message.text or "").split()
    want = parts[1].strip().lower() if len(parts) > 1 else ""
    known = {k for k, _t, _h in logs.KINDS}

    # Второе слово — «off»: отвязать этот вид, не трогая остальные.
    # «Ошибся темой» иначе лечилось бы выключением журнала целиком, то есть
    # потерей четырёх правильных привязок из-за одной неправильной.
    off = len(parts) > 2 and parts[2].strip().lower() in ("off", "выкл", "-")
    if want and want in known and off:
        had = clear_log_topic(want)
        await message.answer(ui.screen(
            "📋 <b>Отвязал</b>" if had else "📋 <b>Нечего отвязывать</b>",
            [f"<b>{logs.KIND_TITLES[want]}</b> "
             + ("больше никуда не пишется." if had else "и не был привязан."),
             "",
             "Остальные виды не тронуты — <code>/log_here</code> покажет."]))
        return

    if want and want in known:
        was = get_log_target().get("chat") or 0
        dropped = set_log_topic(want, chat, thread)
        where = f"тема <code>{thread}</code>" if thread else "общий поток"
        # «Привязал» — не доказательство. Пишем пробную запись и говорим,
        # дошла ли она: тема могла быть закрыта, а права отобраны.
        ok = await logs.log_event(
            message.bot, want,
            ["<i>проверка связи — так будут выглядеть записи</i>"])
        body = [f"<b>{logs.KIND_TITLES[want]}</b> → {where}",
                "",
                "Пробная запись отправлена — она выше." if ok else
                "Пробная запись не отправилась. Причина в личке у владельца."]
        if dropped:
            # Номер темы принадлежит своей группе. Промолчать о том, что
            # журнал переехал целиком, значит оставить владельца с четырьмя
            # привязками, указывающими в никуда.
            names = ", ".join(logs.KIND_TITLES.get(k, k) for k in dropped)
            body += ["", f"⚠️ <b>Журнал переехал сюда из группы "
                         f"<code>{was}</code>.</b>",
                     f"Слетели привязки: {names} — их номера тем "
                     f"принадлежали прежней группе и здесь ничего не значат.",
                     "",
                     "Привяжи их заново в нужных темах, либо вернись в "
                     "прежнюю группу и повтори там."]
        await message.answer(ui.screen(
            "📋 <b>Тема привязана</b>" if ok else
            "⚠️ <b>Привязал, но запись не прошла</b>", body))
        return

    target = get_log_target()
    topics = target.get("topics") or {}
    rows = []
    for kind, title, hint in logs.KINDS:
        bound = topics.get(kind)
        rows.append(("✅" if bound else "—")
                    + f" <code>{kind}</code> · {title}"
                    + (f"  (тема {bound})" if bound else "")
                    + f"\n     <i>{hint}</i>")
    body = [f"Группа: <code>{target.get('chat') or 'не задана'}</code>", ""]
    body += rows
    if want:
        body = [f"⚠️ Вида «{ui.esc(want)}» нет. Возможные — ниже.", ""] + body
    if target.get("error"):
        body += ["", f"❗ Последняя беда: <code>{ui.esc(target['error'])}</code>"]

    b = InlineKeyboardBuilder()
    b.button(text="🗑 Выключить журнал", callback_data="log:off")
    await message.answer(ui.screen(
        "📋 <b>Журнал в группу</b>", body,
        footer="<i>Зайди в нужную тему и напиши там "
               "<code>/log_here вид</code> — например "
               "<code>/log_here trial</code>.\n"
               "Ошибся темой — просто повтори в правильной. Отвязать "
               "совсем: <code>/log_here trial off</code>.</i>"),
        reply_markup=ui.lay(b).as_markup())


@router.callback_query(lambda c: c.data == "log:off")
async def log_off(callback) -> None:
    from storage import clear_log_target, is_admin
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    clear_log_target()
    await callback.message.edit_text(ui.screen(
        "📋 <b>Журнал выключен</b>",
        ["Записи в группу больше не идут.",
         "",
         "Включить обратно — <code>/log_here вид</code> в нужной теме."]))
    await callback.answer()


@router.callback_query(lambda c: c.data == "sub:order")
async def sub_order(callback) -> None:
    """«Прошу счёт» — заявка владельцу.

    Раньше экран подписки просто советовал написать владельцу. Половина не
    писала: искать, кому именно, — отдельное усилие в момент, когда бот
    только что отказал. Здесь усилие ровно одно — нажать кнопку, а найти
    человека уже задача владельца, у которого в группе появилась заявка.
    """
    import logs
    from storage import get_support_contact, price_lines

    ok = await logs.log_event(callback.bot, "order",
                              ["Просит выставить счёт на подписку."],
                              user=callback.from_user)
    rows = price_lines()
    body = ["Заявка ушла владельцу — он напишет сюда же." if ok else
            "Заявку записать не вышло, поэтому напиши напрямую: "
            f"{ui.esc(get_support_contact())}"]
    if rows:
        body += ["", *rows]
    await callback.message.answer(ui.screen(
        "💳 <b>Заявка на оплату</b>", body,
        footer=f"<i>Если ответа долго нет — {ui.esc(get_support_contact())}</i>"))
    await callback.answer("Заявка отправлена" if ok else "Напиши в поддержку")


# --- Постоянная клавиатура --------------------------------------------------
#
# Нажатие приходит обычным текстом, поэтому ловится по подписи. Подписи и
# маршруты лежат в одном списке (`keyboards/reply.py`): разъехавшись, они
# дали бы кнопку, которая нажимается и молчит.


@router.message(F.text.in_(_KB_LABELS))
async def kb_tap(message: Message, state: FSMContext, **data) -> None:
    from keyboards.reply import BY_LABEL
    kind = BY_LABEL[(message.text or "").strip()]
    handler = {
        "orders": cmd_orders, "chats": cmd_chats,
        "balance": cmd_balance, "stats": cmd_stats,
    }.get(kind)
    if handler is None:                     # «Меню»
        from handlers.start import cmd_menu
        await cmd_menu(message, state)
        return
    if kind == "chats":                     # у него другая подпись
        await handler(message, state)
        return
    await handler(message, state, **data)


@router.message(Command("keyboard"))
async def cmd_keyboard(message: Message) -> None:
    """Включить или убрать постоянную клавиатуру.

    Кнопка, которую нельзя убрать, — не удобство, а навязанный интерфейс:
    на маленьком экране клавиатура занимает треть переписки, и кому-то она
    мешает больше, чем помогает.
    """
    from keyboards.reply import hide_keyboard, main_reply_keyboard
    from storage import get_settings, save_settings

    settings = get_settings(message.from_user.id)
    on = not bool(settings.get("reply_keyboard", True))
    settings["reply_keyboard"] = on
    # Отметку «уже показывали» снимаем вместе с выключением: иначе
    # включённая обратно клавиатура не приехала бы ни разу, и `/keyboard`
    # отвечал бы «включена» при пустом поле ввода.
    settings["reply_keyboard_shown"] = on
    save_settings(message.from_user.id, settings)
    await message.answer(
        ui.screen("⌨️ <b>Клавиатура включена</b>" if on else
                  "⌨️ <b>Клавиатура убрана</b>",
                  ["Основные разделы — под полем ввода." if on else
                   "Разделы остались в «Меню» и в командах.",
                   "",
                   "Переключить обратно — <code>/keyboard</code>."]),
        reply_markup=main_reply_keyboard() if on else hide_keyboard())
