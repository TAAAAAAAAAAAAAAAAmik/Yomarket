import logging

from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import delete_token, get_token, save_token, get_settings, save_settings, get_shop_name, save_shop_name, _DATA_DIR

router = Router()
logger = logging.getLogger(__name__)

# Поднимается при каждом значимом изменении: по ней видно, доехал ли код.
BOT_VERSION = "2026-09-01-trial-stack"

# Метка процесса, разная у каждого запуска. Два контейнера с одним токеном
# ведут каждый свой фоновый цикл, и продавец получает все уведомления
# дважды — при этом команды отвечают как обычно, потому что апдейт Telegram
# отдаёт только одному из них. Отличить это от ошибки в коде можно единственным
# способом: вызвать /version несколько раз и посмотреть, меняется ли метка.
import time as _time
import uuid as _uuid

INSTANCE_ID = _uuid.uuid4().hex[:6]
STARTED_AT = _time.time()


class AuthState(StatesGroup):
    waiting_for_token = State()


def _extract_shop(info: dict) -> tuple[str, str]:
    """Название магазина и баланс строкой из ответа /check.

    /check отдаёт {status, shop:{id,title}, integration:{…}, ts} — то есть
    только «кто вы», без денег. Баланс здесь возвращается прочерком
    намеренно: он читается из панели, где и лежит на самом деле.
    """
    shop = info.get("shop") or info.get("data") or info
    if isinstance(shop, dict):
        name = shop.get("name") or shop.get("shop_name") or shop.get("title") or "Магазин"
        balance = shop.get("balance") or shop.get("wallet") or shop.get("money") or "—"
    else:
        name = "Магазин"
        balance = "—"
    return str(name), str(balance)


async def _no_shop_screen(target: "Message | CallbackQuery",
                          state=None) -> bool:
    """Сказать, что магазин не подключён, и вернуть True.

    Меню без подключённого магазина — пустая витрина: половина разделов
    ответит «сначала подключи токен», вторая уйдёт в маркетплейс и вернётся
    с отказом. А выглядит оно как открытый доступ: восемь разделов и
    название магазина сверху. Человек, попавший сюда с витрины через
    «Поддержка → Документы → Назад», решает, что уже всё оплачено, — а
    следом, что бот сломан.

    Проверка живёт здесь, а не в `/menu`, именно поэтому: путей в меню два
    (команда и кнопка «Назад»), а проверял токен только один. Один экран с
    двумя разными поведениями расходится молча.
    """
    if get_token(_uid(target)):
        return False
    uid = _uid(target)
    if state is not None and _can_connect(uid):
        # Кто уже знает, где брать токен, вставит его не нажимая кнопку.
        # Но ждать токен от того, кому подключаться ещё нельзя, незачем:
        # он его пришлёт, а ворота ответят отказом.
        await state.set_state(AuthState.waiting_for_token)
    text = ui.screen(
        "🔌 <b>Магазин ещё не подключён</b>",
        ["Открывать пока нечего. Подключим — и меню появится."]
        if _can_connect(uid) else
        # Сказать, ЧТО делать, а не только что нельзя. «Нужна подписка» без
        # продолжения — тупик: человек уже здесь, значит хочет работать.
        ["Сначала открой доступ — бесплатно или по подписке.",
         "", "Потом подключим магазин, и всё заработает."])
    kb = _connect_kb(uid)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)
    return True


def _uid(target: "Message | CallbackQuery") -> int:
    return target.from_user.id


async def _send_menu(target: Message | CallbackQuery, user_id: int) -> None:
    from storage import is_admin, menu_header_html
    # Название магазина приходит с маркетплейса: одиночный `<` в нём роняет
    # отправку целиком, и продавец после подключения не видит меню вообще.
    name = ui.esc(get_shop_name(user_id) or "Магазин")
    text = (f"🏪 <b>{name}</b>\n\n{menu_header_html()} <b>Главное меню</b>\n"
            "Выбирай, куда идём:")
    kb = main_menu_keyboard(is_admin_user=is_admin(user_id))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


PANEL_URL = "https://panel.yoomarket.net"


async def _show_keyboard(message) -> None:
    """Показать постоянную клавиатуру — один раз на продавца.

    Постоянная клавиатура не исчезает сама: показанная однажды, она
    остаётся под полем ввода навсегда. Слать её на каждый `/menu` значит
    сыпать в переписку пустые сообщения ради того, что уже на экране.

    Приложить её к самому меню нельзя: там inline-кнопки, а в одном
    сообщении бывает только одна клавиатура. Поэтому отдельная строка — но
    она не пустая, а объясняет, что появилось и как это убрать.

    Только в ответ на СООБЩЕНИЕ: `edit_text` постоянную клавиатуру не
    принимает вовсе.
    """
    from storage import get_settings, save_settings
    settings = get_settings(message.from_user.id)
    if not settings.get("reply_keyboard", True):
        return                               # продавец её убрал
    if settings.get("reply_keyboard_shown"):
        return                               # уже под полем ввода
    from keyboards.reply import main_reply_keyboard
    try:
        await message.answer(
            "⌨️ Основные разделы теперь под полем ввода.\n"
            "<i>Убрать или вернуть — <code>/keyboard</code>.</i>",
            reply_markup=main_reply_keyboard())
    except Exception:                        # noqa: BLE001
        return                               # клавиатура — не повод падать
    settings["reply_keyboard_shown"] = True
    save_settings(message.from_user.id, settings)


def _hello_kb(uid: int = 0) -> "InlineKeyboardMarkup":
    """Кнопки под приветствием.

    Раньше приветствие сразу заканчивалось шагом 1: как достать токен из
    панели. Но человек на этом экране ещё не решил подключаться — он читает
    инструкцию к тому, чего не собирался делать, и закрывает бота. Сначала
    «что я умею», и только по нажатию — «как это включить».

    Кнопки проб показываются, только пока проба положена. Кнопка «3 дня»
    тому, кто их уже брал, — обещание невозможного: нажатие ответит
    отказом, и виноватым будет выглядеть бот.
    """
    from storage import has_active_subscription
    b = InlineKeyboardBuilder()
    # Одно действие, и оно первое. Подключение магазина отсюда снято: на
    # витрине человек решает, брать ли бота вообще, а не как его настроить.
    # Инструкция к тому, чего он пока не купил, — лишний шаг перед выбором.
    #
    # Но тому, у кого доступ УЖЕ открыт, продавать его второй раз нельзя:
    # он нажал «получить доступ», получил его — и снова видит ту же
    # кнопку, а того, что делать дальше, на экране нет вовсе.
    if uid and has_active_subscription(uid):
        main = ("🔌 Подключить магазин", "start:connect")
    else:
        main = ("🚀 Получить доступ", "access:menu")
    b.button(text=main[0], callback_data=main[1])
    b.button(text="🧡 Поддержка", callback_data="menu:help")
    return ui.lay(b, solo={main[1]}).as_markup()


def _can_connect(uid: int) -> bool:
    """Может ли этот человек подключить магазин прямо сейчас.

    Подключение — вход в оплаченную часть: фоновый цикл работает всем, у
    кого есть токен, и подписку он не спрашивает. Значит при включённых
    воротах подключение обязано быть за ними, иначе оплата обходится одной
    кнопкой.

    Пока ворота выключены, бот бесплатный для всех — это решение
    владельца, и мешать ему мы не вправе.
    """
    from storage import (has_active_subscription, is_admin,
                         require_subscription_enabled)
    if not require_subscription_enabled():
        return True
    return bool(is_admin(uid) or has_active_subscription(uid))


def _connect_kb(uid: int = 0) -> "InlineKeyboardMarkup":
    """Кнопки экранов, которые ПРО подключение.

    С витрины «Подключить магазин» снято, но здесь оно и есть смысл
    экрана: «магазин ещё не подключён» и «дни открыты, цепляй магазин» без
    этой кнопки — тупики, где сказано что делать и нечем.

    А тому, у кого доступа ещё нет, эта кнопка — обещание невозможного:
    ворота ответят «🔒 Нужна подписка», и виноватым будет выглядеть бот.
    Ему первым стоит «Получить доступ» — то, что действительно надо
    сделать.
    """
    b = InlineKeyboardBuilder()
    solo = {"start:connect"}
    if _can_connect(uid):
        b.button(text="🔌 Подключить магазин", callback_data="start:connect")
        b.button(text="🚀 Получить доступ", callback_data="access:menu")
    else:
        b.button(text="🚀 Получить доступ", callback_data="access:menu")
        solo = {"access:menu"}
    b.button(text="🧡 Поддержка", callback_data="menu:help")
    return ui.lay(b, solo=solo).as_markup()


def _plain(html_text: str) -> str:
    """Текст без разметки — для всплывающих окон: Telegram их не
    форматирует, и `<b>` доехало бы до человека как есть."""
    import re as _re
    return _re.sub(r"<[^>]+>", "", html_text)


def _left_line(uid: int) -> str:
    """«Осталось 29 дн.» или «Подписка навсегда» — одной строкой.

    Отдельной функцией, потому что это говорят три экрана, и сказать
    по-разному значит однажды сказать неправду на одном из них.
    """
    from storage import is_lifetime, subscription_days_left
    if is_lifetime(uid):
        return "Подписка — <b>навсегда</b>."
    left = subscription_days_left(uid)
    return (f"Осталось <b>{left} дн.</b>" if left > 0 else
            # Ноль дней — не «активна», а «кончается сегодня».
            "Подписка кончается <b>сегодня</b>.")


def _has_free_left(uid: int) -> bool:
    """Осталась ли этому продавцу хоть одна непрожитая проба."""
    from storage import (get_trial_channel, get_trial_days,
                         get_trial_free_days, trial_used)
    if not uid:
        return True                            # неизвестному показываем обе
    if get_trial_free_days() > 0 and not trial_used(uid, "free"):
        return True
    return bool(get_trial_days() > 0 and get_trial_channel()
                and not trial_used(uid, "channel"))


def _access_kb(uid: int) -> "InlineKeyboardMarkup":
    """Кнопки экрана «Получить доступ»: заплатить или взять бесплатно.

    Пробы стоят здесь же, а не за общей кнопкой «Получить бесплатно».
    Экран, на котором написано «🎁 3 дня — просто так», и кнопка, ведущая
    к тому же тексту ещё раз, — лишний шаг между решением и действием.

    Каждая проба своей кнопкой, и на ней написано, СКОЛЬКО дней: «взять
    бесплатно» заставляет вспоминать, что за этим скрывается.

    Кнопка пробы тому, кто её уже брал, — обещание невозможного: нажмёт и
    получит отказ, а виноватым будет выглядеть бот. Пробы независимы,
    поэтому и проверяются по отдельности: взявший три дня всё ещё может
    добрать семь за подписку.

    А вот скрывать их обе по факту действующей подписки нельзя: у взявшего
    три дня она есть, своя же пробная, и вместе с кнопкой пропадал
    единственный путь к проверке подписки на канал. Прячет пробы только
    ОПЛАЧЕННАЯ подписка — ровно та, поверх которой `start_trial` и
    отказывает.

    «Прошу счёт» снят: он просил подождать, пока с человеком свяжутся, и
    половина не дожидалась. Теперь срок и реквизиты он видит сам.
    """
    from storage import (get_trial_channel, get_trial_days,
                         get_trial_free_days, paid_now, trial_used)
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить подписку", callback_data="sub:buy")
    free_days, long_days = get_trial_free_days(), get_trial_days()
    # Кнопка пробы тому, кто уже ЗАПЛАТИЛ, ведёт к отказу: поверх оплаченной
    # подписки проба не выдаётся. Поверх пробной — выдаётся, они складываются.
    got = bool(uid and paid_now(uid))
    solo = {"sub:buy"}
    if uid and not got and free_days > 0 and not trial_used(uid, "free"):
        b.button(text=f"🎁 {free_days} дня бесплатно",
                 callback_data="trial:free")
        solo.add("trial:free")
    # Без заданного канала кнопка «за подписку» вела бы в пустоту: подписаться
    # не на что, и проверять нечего.
    if (uid and not got and long_days > 0 and get_trial_channel()
            and not trial_used(uid, "channel")):
        b.button(text=f"📣 +{long_days} дней за подписку на канал",
                 callback_data="trial:offer")
        solo.add("trial:offer")
    b.button(text="⬅️ Назад", callback_data="start:hello")
    return ui.lay(b, solo=solo).as_markup()


def _welcome_kb(back: bool = False) -> "InlineKeyboardMarkup":
    """Кнопки под шагом 1 — подключением магазина.

    До этого под экраном не было ни одной кнопки: человек читал инструкцию
    и должен был сам догадаться скопировать ссылку из текста, сходить в
    браузер и вернуться. Кнопка-ссылка убирает из этой цепочки три шага, а
    «Не нахожу токен» — единственная причина, по которой здесь застревают.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Открыть панель", url=PANEL_URL)
    # На самом экране помощи вторая кнопка ведёт назад, а не по кругу в него же.
    if back:
        b.button(text="⬅️ К подключению", callback_data="start:back")
    else:
        b.button(text="❓ Не нахожу токен", callback_data="start:token_help")
    return ui.lay(b).as_markup()


@router.callback_query(F.data == "start:connect")
async def start_connect(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать шаг 1 — как достать токен."""
    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    await callback.message.edit_text(
        render_custom_text("connect"), reply_markup=_welcome_kb(),
        disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "start:token_help")
async def token_help(callback: CallbackQuery, state: FSMContext) -> None:
    """Подробная подсказка. Ожидание токена при этом не сбрасывается:
    человек уходит читать и возвращается вставить — форма должна его дождаться."""
    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    await callback.message.edit_text(
        render_custom_text("token_help"), reply_markup=_welcome_kb(back=True),
        disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "start:back")
async def back_to_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    await callback.message.edit_text(
        render_custom_text("connect"), reply_markup=_welcome_kb(),
        disable_web_page_preview=True)
    await callback.answer()


def _channel_url(channel: str) -> str:
    """Ссылка на канал из того, что вписал владелец.

    Числовой id ссылкой не сделать: `t.me/-100…` никуда не ведёт, а кнопка
    с битым адресом роняет всю клавиатуру целиком — экран не придёт вовсе.
    """
    name = channel.strip().lstrip("@")
    return f"https://t.me/{name}" if name and not name.lstrip("-").isdigit() else ""


def _trial_kb() -> "InlineKeyboardMarkup":
    from storage import get_trial_channel
    b = InlineKeyboardBuilder()
    url = _channel_url(get_trial_channel())
    if url:
        b.button(text="📣 Открыть канал", url=url)
    b.button(text="✅ Я подписался", callback_data="trial:check")
    # Назад — туда, откуда сюда попадают: на экран доступа, а не на витрину.
    b.button(text="⬅️ Назад", callback_data="access:menu")
    return ui.lay(b).as_markup()


def _welcome_text() -> str:
    """Приветствие с настоящими числами: сроки проб и цена.

    Числа берутся из настроек, а не пишутся в тексте: владелец меняет их в
    админке, и приветствие, обещающее прежние, — то же враньё, только на
    первом экране.
    """
    from storage import (get_prices, get_bot_price, get_trial_days,
                         get_trial_free_days, render_custom_text)
    prices = [p for p in get_prices().values() if p] or (
        [get_bot_price()] if get_bot_price() else [])
    price = f" — от {min(prices)} ₽" if prices else ""
    free_days, long_days = get_trial_free_days(), get_trial_days()
    return render_custom_text("welcome", цена=price,
                              проба=free_days, неделя=long_days,
                              всего=free_days + long_days)


def _trial_offer_text() -> str:
    from storage import get_trial_days
    return ui.screen(
        "🎁 <b>Неделя бесплатно</b>",
        [f"Полный доступ на <b>{get_trial_days()} дн.</b> — без оплаты и без "
         "карты.",
         "",
         "Условие одно: подпишись на канал и нажми «Я подписался».",
         "",
         "<i>Даётся один раз. Отписаться потом можно — неделю это не "
         "отнимет.</i>"])


@router.callback_query(F.data == "trial:offer")
async def trial_offer(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать канал и кнопку проверки.

    Без этого экрана «7 дней за подписку» вела прямо на проверку: не
    подписан — получи отказ и ищи канал сам. Ссылку надо дать до отказа, а
    не вместо него.
    """
    from storage import trial_used
    if trial_used(callback.from_user.id, "channel"):
        await callback.answer("Неделю за подписку уже брали — она даётся "
                              "один раз", show_alert=True)
        return
    await callback.message.edit_text(_trial_offer_text(),
                                     reply_markup=_trial_kb())
    await callback.answer()


@router.callback_query(F.data == "trial:free")
async def trial_free(callback: CallbackQuery, state: FSMContext) -> None:
    """Короткая проба — без условий, по одному нажатию.

    Отметка «уже брал» у каждой пробы своя: взявший три дня добирает потом
    семь за подписку на канал, вместе десять. Общая отметка обесценивала бы
    условие — подписываться было бы уже не за что.
    """
    import logs
    from storage import (get_trial_free_days, paid_now, start_trial,
                         trial_used)

    uid = callback.from_user.id
    if trial_used(uid, "free"):
        await callback.answer("Эти дни уже брали — они даются один раз",
                              show_alert=True)
        return
    days = start_trial(uid, get_trial_free_days(), kind="free")
    if not days:
        # Причин отказа две, и они разные. «Пробный период не выдаётся»
        # тому, у кого доступ уже оплачен, читается как поломка: он видел
        # кнопку, нажал, и бот ответил про что-то своё. Спрашивается именно
        # оплата: пробная подписка выдаче второй пробы не мешает.
        await callback.answer(
            f"У тебя уже есть доступ. {_plain(_left_line(uid))}"
            if paid_now(uid) else
            "Пробный период сейчас выключен", show_alert=True)
        return
    await logs.log_event(callback.bot, "trial",
                         [f"Открыт пробный период: <b>{days} дн.</b>",
                          "Без условий, по кнопке."],
                         user=callback.from_user)
    await callback.message.edit_text(ui.screen(
        f"🎁 <b>{days} дня доступа открыты</b>",
        ["Полный доступ — без оплаты.",
         "",
         "Цепляй магазин, и автоматика заработает сразу."]),
        reply_markup=_connect_kb(uid))
    await callback.answer("Готово")
    await state.set_state(AuthState.waiting_for_token)


@router.callback_query(F.data.in_({"access:menu", "menu:prices"}))
async def show_access(callback: CallbackQuery) -> None:
    """Как получить доступ: бесплатные способы и тарифы.

    Показываются только тарифы, которым владелец назначил цену. Строка
    «1 месяц — 0 ₽» читалась бы как «бесплатно», а это обещание, за которое
    спросят. Пустой список тоже объясняется, а не выдаётся за отсутствие
    платного доступа.
    """
    from storage import (get_support_contact, get_trial_channel,
                         get_trial_days, get_trial_free_days,
                         has_active_subscription, paid_now, price_lines,
                         trial_used)
    uid = callback.from_user.id
    free_days, long_days = get_trial_free_days(), get_trial_days()
    # Доступ и оплата — разные вопросы, и отвечают они на разное.
    # «Доступ уже открыт» говорится по доступу; пробы прячутся по ОПЛАТЕ:
    # взявший три дня приходит сюда за неделей, и подписка у него есть —
    # своя же пробная. Пробы складываются, скрывать вторую не за что.
    got_access = has_active_subscription(uid)
    paid = paid_now(uid)
    body: list[str] = []
    if got_access:
        body += [f"✅ <b>Доступ уже открыт.</b> {_left_line(uid)}", ""]

    free_rows = []
    if not paid and free_days > 0 and not trial_used(uid, "free"):
        free_rows.append(f"🎁 <b>{free_days} дня</b> — просто так")
    if (not paid and long_days > 0 and get_trial_channel()
            and not trial_used(uid, "channel")):
        free_rows.append(f"📣 <b>+{long_days} дней</b> — за подписку на канал")
    if free_rows:
        body += ["<b>Бесплатно</b>"] + free_rows + [""]

    rows = price_lines()
    body.append("<b>Продлить</b>" if got_access else "<b>По подписке</b>")
    body += rows or ["<i>Цены пока не назначены — напиши, договоримся.</i>"]

    await callback.message.edit_text(ui.screen(
        "🚀 <b>Получить доступ</b>", body,
        footer=f"<i>Вопросы — {ui.esc(get_support_contact())}</i>"),
        reply_markup=_access_kb(uid))
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def show_help(callback: CallbackQuery) -> None:
    """Экран поддержки. Собирается в `support.py` — он же у команды.

    Экранов было два, свой у команды и свой у кнопки, и тексты разъехались:
    один звал прислать код сборки служебной командой, второй нет. Сверять
    их было нечем — это были разные куски кода.
    """
    from support import support_kb, support_text
    uid = callback.from_user.id
    await callback.message.edit_text(support_text(uid),
                                     reply_markup=support_kb(uid))
    await callback.answer()


@router.callback_query(F.data == "start:hello")
async def back_to_hello(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        _welcome_text(), reply_markup=_hello_kb(callback.from_user.id),
        disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "trial:check")
async def trial_check(callback: CallbackQuery, state: FSMContext) -> None:
    """Проверить подписку и выдать неделю."""
    import logs
    import trialgate
    from storage import get_support_contact, render_custom_text, trial_used

    uid = callback.from_user.id
    if trial_used(uid, "channel"):
        # Отметка не удаляется вместе с данными — иначе `/forget_me` стал бы
        # способом брать неделю бесконечно.
        await callback.answer("Неделю за подписку уже брали — она даётся "
                              "один раз", show_alert=True)
        return

    days, why = await trialgate.grant_for_subscription(callback.bot, uid)
    if not days and not why:
        await callback.answer(
            "Подписки не вижу. Подпишись и нажми ещё раз", show_alert=True)
        return
    if not days:
        # Проверка сорвалась И выдать не вышло. Чаще всего — потому что
        # доступ уже есть, и об этом надо сказать прямо: «не вышло, напиши
        # в поддержку» отправляет человека решать несуществующую беду.
        from storage import paid_now
        await callback.answer(
            f"У тебя уже есть доступ. {_plain(_left_line(uid))}"
            if paid_now(uid) else
            f"Не вышло открыть пробный период — напиши "
            f"{get_support_contact()}", show_alert=True)
        return

    await logs.log_event(
        callback.bot, "trial",
        [f"Открыт пробный период: <b>{days} дн.</b>"]
        + ([f"⚠️ Подписку проверить НЕ вышло: <code>{ui.esc(why)}</code>. "
            "Выдано без проверки."] if why else ["Подписка на канал есть."]),
        user=callback.from_user)
    await callback.message.edit_text(ui.screen(
        "🎁 <b>Неделя открыта</b>",
        [f"Полный доступ на <b>{days} дн.</b>",
         "",
         "Цепляй магазин — и автоматика заработает сразу."]))
    await callback.answer("Готово")
    # Сказали «цепляй магазин» — значит кнопка для этого обязана быть под
    # сообщением, а не остаться на витрине, куда ещё надо вернуться.
    await callback.message.answer(_welcome_text(),
                                  reply_markup=_connect_kb(callback.from_user.id),
                                  disable_web_page_preview=True)
    await state.set_state(AuthState.waiting_for_token)



@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    token = get_token(message.from_user.id)
    if token:
        api = YooMarketAPI(token)
        await api.start()
        try:
            info = await api.check()
            name, _ = _extract_shop(info)
            save_shop_name(message.from_user.id, name)
        except Exception:
            pass
        finally:
            await api.close()
        await _send_menu(message, message.from_user.id)
        return

    # Пробный период больше не выдаётся молча: обе пробы — кнопки на самой
    # витрине. Тихая выдача обесценивала условие («подпишись — дадим
    # неделю»), потому что доступ и так уже был, а выбор между тремя днями
    # и неделей делал за человека бот.
    import logs
    from storage import trial_used

    await logs.log_event(message.bot, "users",
                         ["Зашёл в бота впервые."
                          if not trial_used(message.from_user.id, "free")
                          else
                          "Вернулся, магазин не подключён."],
                         user=message.from_user)

    # Состояние ставим сразу, хотя инструкции ещё не показали: если человек
    # уже знает, где брать токен, и вставит его не нажимая кнопку — форма
    # обязана его принять, а не промолчать.
    await state.set_state(AuthState.waiting_for_token)

    # Состояний три, а не два. Раньше `/start` смотрел только на токен, и
    # тот, кто уже взял пробу или заплатил, снова видел витрину с кнопкой
    # «Получить доступ» — то есть предложение купить то, что у него уже
    # есть, и ни слова о том, что делать дальше.
    from storage import has_active_subscription, subscription_days_left
    uid = message.from_user.id
    if has_active_subscription(uid):
        left = subscription_days_left(uid)
        await message.answer(ui.screen(
            "✅ <b>Доступ открыт</b>",
            [_left_line(uid), "",
             "Остался один шаг: подключить магазин — и автоматика "
             "заработает сразу."]),
            reply_markup=_connect_kb(uid))
        return
    await message.answer(_welcome_text(),
                         reply_markup=_hello_kb(uid),
                         disable_web_page_preview=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    """Главное меню по команде.

    Кнопка «⬅️ Назад» есть не на каждом экране, и из глубины настроек
    возвращаться было нечем, кроме `/start`. А `/start` для этого не годится:
    он идёт в маркетплейс за названием магазина, то есть ждёт сеть там, где
    от бота ждут мгновенной кнопки, и молчит, если маркетплейс не ответил.

    Состояние чистить не надо: брошенную форму закрывает middleware
    `CommandsEscapeForms` — он срабатывает на любую команду и раньше
    фильтров.
    """
    if await _no_shop_screen(message, state):
        return
    await _send_menu(message, message.from_user.id)
    await _show_keyboard(message)


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    """Версия работающего бота и где лежат его данные — диагностика выката."""
    import os
    import storage

    uid = message.from_user.id

    # 1. Storage backend
    if storage._USE_DB:
        backend = "🟢 PostgreSQL (постоянная БД)"
        db_ok = "?"
        try:
            storage._db_read_raw("tokens")  # touch the DB
            db_ok = "✅ подключение работает"
        except Exception as e:
            db_ok = f"❌ ошибка: {str(e)[:60]}"
        storage_lines = [f"💾 Хранилище: {backend}", f"   {db_ok}"]
    elif storage.ephemeral_disk():
        # Railway: диск пересобирается вместе с контейнером.
        backend = "🔴 JSON-файлы (на Railway стираются при выкате!)"
        storage_lines = [
            f"💾 Хранилище: {backend}",
            "   ⚠️ DATABASE_URL не задан — данные сотрутся при редеплое!",
            f"   📁 {_DATA_DIR}",
        ]
    else:
        # Свой сервер: файлы никуда не денутся, и пугать нечем. База
        # надёжнее по другим причинам — резервные копии, две машины, — но
        # «данные сотрутся» здесь было бы неправдой.
        backend = "🟡 JSON-файлы"
        storage_lines = [
            f"💾 Хранилище: {backend}",
            "   Переживают перезапуск бота и перезагрузку сервера.",
            "   DATABASE_URL не задан — с базой было бы надёжнее,",
            "   но здесь данные не пропадают.",
            f"   📁 {_DATA_DIR}",
        ]

    # 2. Redis (FSM)
    redis_on = bool(os.environ.get("REDIS_URL", "").strip())
    redis_line = "🧩 FSM: 🟢 Redis" if redis_on else "🧩 FSM: ⚪ память (сбрасывается при рестарте)"

    # 3. Лежит ли прямо сейчас токен ИМЕННО этого продавца
    has_token = bool(storage.get_token(uid))
    accounts = storage.get_accounts(uid)
    token_line = (f"🔑 Твой токен: {'✅ сохранён' if has_token else '❌ нет'}"
                  f"  ({len(accounts)} аккаунт(ов))")
    panel = storage.get_panel_creds(uid)
    panel_line = f"🌐 Куки панели: {'✅ есть' if panel and panel.get('cookies') else '❌ нет'}"

    # Seed-фраза TON — это доступ к чужому кошельку, и лежит она в том же
    # хранилище, что и всё остальное. Видеть, зашифрована ли она на самом
    # деле, важнее, чем верить, что «наверное, да».
    has_seed = bool((storage.get_fragment_creds(uid) or {}).get("mnemonic"))
    if not has_seed:
        seed_line = "🔐 Seed-фраза TON: не сохранена"
    elif storage.encryption_on():
        seed_line = "🔐 Seed-фраза TON: ✅ зашифрована"
    else:
        seed_line = ("🔐 Seed-фраза TON: ⚠️ <b>в открытом виде</b> — "
                     "задай SECRET_KEY в переменных окружения")

    # Время видно только внутри «Настроек», а зависит от него многое: час
    # итогов дня, окно ночного режима, граница суток в статистике. Вопрос
    # «какое время в боте» не должен требовать хождения по экранам.
    import localtime as _lt
    _s = storage.get_settings(uid)
    time_line = (f"🕐 Твоё время: <b>{_lt.now(_s).strftime('%d.%m %H:%M')}</b>"
                 f" · {_lt.offset_label(_s)}")

    uptime = int((_time.time() - STARTED_AT) / 60)

    # Получаем ли мы вообще сообщения. Если это читают — получаем; но строка
    # нужна для случая «отвечает через раз», когда обновления достаются то
    # нам, то второму экземпляру.
    try:
        from main import POLLING, polling_trouble
        trouble = polling_trouble()
        if trouble:
            polling_line = f"🔇 <b>Приём сообщений:</b> {trouble}"
        elif POLLING.get("last_update"):
            ago = int(_time.time() - POLLING["last_update"])
            polling_line = f"📡 Приём сообщений: 🟢 последнее {ago} с назад"
        else:
            polling_line = "📡 Приём сообщений: 🟢 работает"
        if POLLING.get("webhook"):
            # Снять не удалось — значит опрос не заработает, и это важнее
            # всего остального в этой строке.
            polling_line += ("\n🔌 На токене висит вебхук — снять его не "
                             "вышло, опросом сообщения не придут")
    except Exception:
        polling_line = ""

    await message.answer(
        f"🤖 <b>Версия:</b> <code>{BOT_VERSION}</code>\n"
        f"🆔 Процесс: <code>{INSTANCE_ID}</code> · PID {os.getpid()} · "
        f"работает {uptime} мин\n"
        f"<i>Вызови /version раз пять подряд. Если метка процесса меняется — "
        f"запущено несколько ботов на одном токене, и все уведомления "
        f"приходят по столько же раз.</i>\n\n"
        + "\n".join(storage_lines)
        + f"\n{redis_line}\n\n"
        + f"{token_line}\n{panel_line}\n{seed_line}\n{time_line}"
        + (f"\n{polling_line}" if polling_line else "")
    )


@router.message(Command("sent"))
async def cmd_sent(message: Message) -> None:
    """Что ЭТОТ процесс отправил за последнее время.

    Продавец видит дубль — а здесь одна запись: значит вторую копию прислал
    другой процесс. Две записи подряд — беда внутри одного. Различить это
    иначе нельзя: в Telegram обе копии выглядят одинаково.
    """
    import storage
    from tasks.manager import _SENT_LOG, _SENDER_LEASE_TTL

    import localtime as _lt
    _settings = storage.get_settings(message.from_user.id)
    lease = (_settings.get("_sender") or {})
    owner = str(lease.get("inst") or "—")
    age = _time.time() - float(lease.get("ts") or 0)
    mine = owner == INSTANCE_ID

    lines = [f"📤 <b>Отправлено этим процессом</b>  <code>{INSTANCE_ID}</code>",
             "",
             f"{'🟢' if mine else '🟡'} Рассылку ведёт: <code>{owner}</code>"
             + (" — это я" if mine else " — другой процесс"),
             f"   продлена {int(age)} с назад "
             f"(аренда {int(_SENDER_LEASE_TTL)} с)", ""]
    if not _SENT_LOG:
        lines.append("<i>этот процесс ещё ничего не отправлял</i>")
    seen: dict[str, int] = {}
    for ts, head in _SENT_LOG[-15:]:
        seen[head] = seen.get(head, 0) + 1
    import html as _h
    for ts, head in _SENT_LOG[-15:]:
        when = _lt.fmt(ts, _settings, "%H:%M:%S")
        mark = "‼️" if seen.get(head, 0) > 1 else "•"
        lines.append(f"{mark} <code>{when}</code> {_h.escape(head)}")
    lines += ["", "<i>‼️ — этот процесс отправил одно и то же дважды. "
              "Если дубль в чате есть, а здесь запись одна — вторую копию "
              "прислал другой процесс.</i>"]
    await message.answer("\n".join(lines)[:3900])


async def _edit(message: Message, text: str,
                markup: InlineKeyboardMarkup | None = None) -> None:
    """Переписать сообщение «⏳ проверяю» результатом.

    Раньше каждый шаг подключения добавлял в чат новое сообщение, и к
    третьей попытке экран был завален «⏳ Проверяю токен...» вперемешку с
    отказами — а какой из них последний, приходилось искать глазами.
    """
    try:
        await message.edit_text(text, reply_markup=markup,
                                disable_web_page_preview=True)
    except Exception:                            # noqa: BLE001
        await message.answer(text, reply_markup=markup,
                             disable_web_page_preview=True)


def _hidden_note(hidden: bool) -> str:
    """Что стало с сообщением, в котором был токен.

    Сказать «удалено» там, где удалить не вышло, — ровно та ложь, из-за
    которой продавец оставит ключ от своего магазина висеть в переписке.
    """
    return ("<i>🔒 Сообщение с токеном я убрал из переписки.</i>" if hidden else
            "<i>⚠️ Сообщение с токеном стереть не вышло — сотри его сам, "
            "иначе оно так и останется висеть в переписке.</i>")


async def _hide_token(message: Message) -> bool:
    """Убрать из переписки сообщение с токеном.

    Токен остаётся в истории чата навсегда: у продавца в телефоне, у того,
    кому он покажет экран, и в резервной копии Telegram. Удалить его —
    единственное, что мы можем с этим сделать. Отвечает, получилось ли:
    обещать безопасность, которой не случилось, хуже, чем не обещать
    ничего (Telegram не даёт удалять сообщения старше 48 часов, и это не
    единственная причина отказа).
    """
    try:
        await message.delete()
        return True
    except Exception as e:                       # noqa: BLE001 — причин много
        logger.info("сообщение с токеном не удалилось: %s", e)
        return False


_WAIT = ui.screen("🔑 <b>Цепляю магазин</b>",
                  ["⏳ Спрашиваю Юмаркет, признаёт ли он этот токен…"])


def _retry_kb(again: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под отказом. `again` — предложить тот же токен ещё раз.

    Она к месту ровно там, где токен ни при чём: предлагать «повторить» на
    отказе «токен не признан» — обещать, что со второго раза выйдет.
    """
    b = InlineKeyboardBuilder()
    if again:
        b.button(text="🔁 Повторить", callback_data="start:retry")
    b.button(text="🌐 Открыть панель", url=PANEL_URL)
    b.button(text="❓ Не нахожу токен", callback_data="start:token_help")
    return ui.lay(b).as_markup()


@router.message(AuthState.waiting_for_token)
async def process_token(message: Message, state: FSMContext, **data) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Пустое сообщение. Пришли токен одной строкой:",
                             reply_markup=_retry_kb())
        return

    # Сначала убрать токен с экрана, потом идти в сеть: проверка занимает
    # секунды, и всё это время строка висит в переписке.
    hidden = await _hide_token(message)
    wait = await message.answer(_WAIT)
    await _connect(message.from_user.id, token, wait, state,
                   data.get("task_manager"), hidden)


@router.callback_query(F.data == "start:retry")
async def retry_token(callback: CallbackQuery, state: FSMContext, **data) -> None:
    """Повторить проверку тем же токеном.

    Кнопка появляется только там, где токен ни при чём: маркетплейс не
    ответил или попросил сбавить темп. Без неё продавец оказывался в
    тупике — сообщение с токеном мы уже удалили, а панель показывает токен
    ровно один раз, и второй раз скопировать его неоткуда.
    """
    token = str((await state.get_data()).get("token") or "")
    if not token:
        await callback.answer("Токен я уже не помню — пришли его ещё раз",
                              show_alert=True)
        return
    await _edit(callback.message, _WAIT)
    await _connect(callback.from_user.id, token, callback.message, state,
                   data.get("task_manager"), hidden=True)
    await callback.answer()


async def _connect(uid: int, token: str, wait: Message, state: FSMContext,
                   task_manager, hidden: bool) -> None:
    """Спросить Юмаркет про токен и показать результат в сообщении `wait`."""
    api = YooMarketAPI(token)
    await api.start()
    try:
        info = await api.check()
    except Exception as e:                       # noqa: BLE001
        await api.close()
        # Английский код ошибки на этом экране — отписка: это первое, что
        # видит человек, ещё ничего в боте не сделавший.
        from api.yoomarket import auth_trouble
        why, what, ours = auth_trouble(str(e), token)
        # «Токен ни при чём» и «пришлите токен ещё раз» в одном сообщении —
        # это экран, спорящий сам с собой. Что делать дальше, зависит от
        # того, в токене ли дело.
        if ours:
            nxt = "Пришли токен ещё раз — жду прямо здесь."
            await state.update_data(token=None)
        else:
            nxt = ("Токен я запомнил — жми «Повторить» через пару минут. "
                   "Заново копировать его из панели не надо.")
            await state.update_data(token=token)
        await _edit(wait, ui.screen(
            f"⚠️ <b>{why}</b>", [what, "", nxt],
            footer=f"<i>Ответ маркетплейса: <code>{ui.esc(str(e))[:120]}</code></i>",
        ), _retry_kb(again=not ours))
        return
    finally:
        await api.close()

    save_token(uid, token)
    await state.clear()

    name, balance = _extract_shop(info)
    save_shop_name(uid, name)

    # В журнал уходит НАЗВАНИЕ магазина, а не токен: токен здесь под рукой,
    # и записать его было бы проще всего — поэтому оговорка нужна прямо тут.
    import logs
    await logs.log_event(wait.bot, "account",
                         [f"Подключил магазин: <b>{ui.esc(name)}</b>"],
                         user=uid)

    if task_manager:
        task_manager.start_for_user(uid)

    # Шаг 2 подключения — вход в панель. Тому, кто уже вошёл, шага не
    # показываем: проводить человека по сделанному незачем.
    from storage import get_panel_creds, render_custom_text
    # Название магазина приходит с маркетплейса и уходит в HTML-сообщение:
    # одиночный `<` в нём уронил бы отправку целиком.
    safe_name = ui.esc(name)
    if get_panel_creds(uid):
        await _edit(wait, ui.screen(
            "✅ <b>Токен обновлён</b>",
            [f"🏪 <b>{safe_name}</b>"
             # Только если в ответе была сумма: «— ₽» — это не баланс.
             + (f"   ·   💰 <b>{ui.esc(balance)} ₽</b>" if balance != "—" else "")],
            footer=_hidden_note(hidden)))
        await _send_menu(wait, uid)
        return

    b = InlineKeyboardBuilder()
    b.button(text="📧 Войти по email", callback_data="panel:sms_start")
    b.button(text="⏭ Позже", callback_data="menu:main")
    ui.lay(b)
    await _edit(wait, render_custom_text(
        "token_ok", name=safe_name, balance=ui.esc(str(balance)))
        + "\n\n" + _hidden_note(hidden), b.as_markup())


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    if await _no_shop_screen(callback, state):
        return
    await _send_menu(callback, callback.from_user.id)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer("🚪 Вышел. Обратно — /start")
