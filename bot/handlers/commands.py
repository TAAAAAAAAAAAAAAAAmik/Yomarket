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

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import ui
from storage import get_token

logger = logging.getLogger(__name__)
router = Router()


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
    await message.answer(ui.screen(
        "🧡 <b>Поддержка</b>",
        [f"Пиши сюда: {ui.esc(contact)}",
         "",
         "<b>Что приложить к вопросу</b>",
         "• <code>/version</code> — версия и что бот про себя знает",
         "• номер заказа, если беда с конкретным заказом",
         "• что ты сделал и что увидел вместо ожидаемого",
         "",
         "<i>С этим отвечу сразу. Без этого придётся сначала спрашивать "
         "то же самое, а время идёт.</i>"],
        footer="<i>Если бот молчит на команды — <code>/version</code> "
               "покажет, слышит ли он Telegram вообще.</i>"),
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
