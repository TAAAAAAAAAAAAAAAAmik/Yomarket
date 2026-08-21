from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import (
    get_settings, save_settings, get_shop_name,
    get_fragment_creds, save_fragment_creds, delete_fragment_creds,
)

router = Router()
logger = logging.getLogger(__name__)


def _parse_cookies(text: str) -> dict:
    """Куки из того, что человек скопировал, — в каком угодно виде.

    Раньше разбиралась только строка `k=v; k2=v2` из `document.cookie`, и
    экран её же и советовал. Беда в том, что `document.cookie` **не
    показывает** `stel_token` и `stel_ssid` — они HttpOnly, — то есть
    массовая вставка приносила ровно ту куку, без которой можно обойтись, а
    две главные продавец каждый раз вбивал руками. Куки Fragment живут
    часами, так что «каждый раз» — это по нескольку раз в день.

    Полный набор есть в заголовке `Cookie:` любого запроса к fragment.com и
    в том, что даёт «Copy as cURL». Разбираем и то и другое: обёртки
    (`-b '…'`, `-H 'cookie: …'`, `Cookie: …`) снимаются, дальше всё те же
    пары. Чужие куки в наборе не мешают — сохраняются только знакомые имена,
    и это делает вызывающий.
    """
    raw = str(text or "").strip()
    if not raw:
        return {}
    # Из cURL берём только то, что относится к кукам: в нём есть и другие
    # заголовки со знаком «равно», и они бы разобрались как куки.
    chunks: list[str] = []
    for m in re.finditer(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", raw, re.S):
        chunks.append(m.group(2))
    for m in re.finditer(r"(?:-H|--header)\s+(['\"])\s*cookie\s*:\s*(.*?)\1",
                         raw, re.S | re.I):
        chunks.append(m.group(2))
    if not chunks:
        # Не cURL — значит либо голая строка кук, либо заголовок с именем.
        chunks.append(re.sub(r"^\s*cookie\s*:\s*", "", raw, flags=re.I))

    out: dict = {}
    for chunk in chunks:
        for part in chunk.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            # Имя куки — не предложение с пробелами: так отсеиваются куски
            # команды, случайно попавшие в вставку.
            if k and v and re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", k):
                out[k] = v
    return out


# Повторять покупку нельзя — деньги могли уйти. Один текст на оба ручных
# пути, чтобы совет не разошёлся с тем, что бот делает сам.
_DO_NOT_REPEAT = (
    "Повторять не следует: деньги могли уйти. Проверьте на fragment.com, "
    "начислены ли звёзды, и выдавайте заново, только если их нет."
)


async def deliver_stars(uid: int, username: str,
                        quantity: int) -> tuple[bool, str, bool]:
    """Покупка звёзд в отдельном потоке → (успех, текст, повторять нельзя).

    Третье значение появилось не для красоты. Раньше исходов было два, и
    оборванное ожидание попадало в «не вышло» вместе с обычным отказом — а
    под «не вышло» стоит кнопка «🔁 Повторить». Поток покупки при этом
    продолжает работать и может отправить деньги уже после обрыва: нажатие
    покупало звёзды второй раз, за деньги продавца.
    """
    from automation.fragment import BUY_TIMEOUT_SECS, buy_stars_sync
    creds = get_fragment_creds(uid)
    if not creds or not creds.get("cookies") or not creds.get("mnemonic"):
        return False, ("Не настроены данные Fragment.\n"
                       "Плагины → AutoStars → ⚙️ Настройки → 🔑 Данные Fragment"), False
    loop = asyncio.get_event_loop()
    spend: dict = {}
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(
                None, functools.partial(
                    buy_stars_sync,
                    creds["cookies"], creds["mnemonic"], username, quantity,
                    creds.get("wallet_version", "v4r2"),
                    # Пусто — бот подберёт хеш сам; чужой даёт «Bad request».
                    creds.get("api_hash", ""),
                    report=spend,
                    proxy=creds.get("proxy", "")),
            ),
            timeout=BUY_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        return False, (f"⏱ Покупка не завершилась за "
                       f"{BUY_TIMEOUT_SECS // 60} мин, и чем она кончилась — "
                       f"неизвестно.\n\n{_DO_NOT_REPEAT}"), True
    except Exception as e:
        # У TimeoutError пустой str(); имя класса некрасиво, но это факт, а
        # пустая причина на экране — то же, что её отсутствие.
        return False, f"Ошибка выдачи: {str(e)[:150] or type(e).__name__}", False
    if not ok and spend.get("sent_onchain"):
        ton = float(spend.get("ton") or 0)
        return False, (f"{msg}\n\n⚠️ Списано {ton:.4f} TON — перевод ушёл, "
                       f"а выдача не подтверждена.\n{_DO_NOT_REPEAT}"), True
    return ok, msg, False


class PluginState(StatesGroup):
    # AutoStars
    stars_manual_buyer = State()
    stars_manual_amount = State()
    stars_whois = State()
    stars_set_amount = State()
    stars_set_note = State()
    stars_set_cookies = State()
    stars_set_one_cookie = State()
    stars_set_hash = State()
    stars_set_proxy = State()
    stars_set_mnemonic = State()
    stars_set_keyword = State()
    stars_set_reply = State()
    # AutoRoblox. Ручная выдача спрашивает номер заказа, а не @username:
    # код уходит в чат заказа, аккаунт покупателя тут ни при чём.
    roblox_set_keyword = State()
    roblox_set_note = State()
    roblox_manual_order = State()
    roblox_set_ad_title = State()
    roblox_set_ad_text = State()
    roblox_ads_rate = State()
    # Гифт-карты. Состояния общие на все карты реестра: `slug` лежит в
    # данных состояния, а не в его имени. Иначе двадцать пять карт по
    # четыре поля добавили бы сотню состояний к нынешним девяноста трём.
    gc_field = State()
    gc_manual = State()
    gc_price = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _read_catalog(creds: dict) -> tuple[bool, object, str]:
    """Каталог поставщика через общий кеш — одна дверь на все экраны.

    Читать его каждым экраном заново нельзя: у `GET /services` лимит два
    запроса в минуту на кабинет, а выбор номинала для нового товара проходит
    три экрана подряд. Подробности и кеш — в `handlers.approute.read_catalog`.
    """
    from handlers.approute import read_catalog

    return await read_catalog(creds)


def _plugins_menu_keyboard(settings: dict | None = None) -> InlineKeyboardMarkup:
    """Меню плагинов.

    Включённые карты выносятся сюда своими кнопками: та, которой продавец
    торгует каждый день, должна открываться в одно нажатие, а не через
    общий список из восьми.

    Кнопки берутся из реестра по признаку «включена», а не перечисляются
    руками. Строчка вида «если это Apple — покажи Apple» вернула бы нас к
    тому, от чего ушли: восемь карт означали бы восемь таких строк, и
    девятая однажды не появилась бы вовсе.
    """
    from automation.giftcards import enabled_cards

    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    pinned = enabled_cards(settings or {})
    # Robux переехал под гифт-карты: это такая же карта, просто номинал у
    # неё в игровой единице. Отдельная кнопка вела бы во второй экран того
    # же самого — продавец видел бы два раздела про одно, с раздельными
    # тумблерами и журналами, и не понимал бы, какой из них работает.
    for gift in pinned:
        builder.button(text=f"{gift.emoji} {gift.title}",
                       callback_data=f"plugins:gc:{gift.slug}")
    # Общий список остаётся всегда: через него включают карту в первый раз,
    # и через него же видно те, что выключены. Без него карта, которую ещё
    # не включали, была бы недостижима.
    builder.button(text="🎁 Все гифт-карты", callback_data="plugins:gifts")
    # Прежний поставщик переехал сюда из раздела Robux: там он был не к
    # месту — поставщик выбран, и сравнение закупочной цены не то, ради чего
    # открывают экран выдачи. Но убрать его совсем значило бы оставить экран
    # ns.gifts вообще без входа: попасть в него можно было бы только
    # командой, а про команду продавцу неоткуда узнать.
    builder.button(text="🔑 ns.gifts (сравнить цену)", callback_data="ns:creds")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    # Включённые карты — по две в ряд, остальное по одной.
    builder.adjust(1, *([2] * ((len(pinned) + 1) // 2)), 1, 1, 1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# AutoStars
# ---------------------------------------------------------------------------

def _stars_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_stars"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"⭐ <b>Telegram — Звёзды{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
    )


def _stars_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_stars"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:stars:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:stars:accumulated")
    # Проверка ника — рядом с выдачей, а не в настройках: ею пользуются
    # перед каждой ручной выдачей на незнакомый ник.
    builder.button(text="🔎 Проверить ник", callback_data="plugins:stars:whois")
    builder.button(text="💎 Прибыль", callback_data="plugins:stars:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:stars:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:stars:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:stars:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:stars:toggle")
    builder.button(text="⚙️ Настройки", callback_data="plugins:stars:settings")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(2, 1, 2, 2, 1, 2)
    return builder.as_markup()


def _stars_settings_text(settings: dict, has_creds: bool = False) -> str:
    p = settings["plugins"]["auto_stars"]
    amount = p.get("amount", 50)
    note = p.get("note") or "—"
    keyword = p.get("keyword") or ""
    ask = "да" if p.get("ask_username", True) else "нет"
    creds = "🟢 настроены" if has_creds else "🔴 не настроены"
    warn = "🟢 вкл" if p.get("low_balance_warn", True) else "🔴 выкл"
    left = int(p.get("low_balance_deliveries", 2) or 2)
    kw_line = (f"🔍 Своё слово в заказе: <code>{keyword}</code>"
               if keyword else
               "🔍 Узнаёт заказы сам: «звёзд», «звезд», «stars», «⭐»")
    return (
        f"⚙️ <b>Настройки AutoStars</b>\n\n"
        f"⭐ Кол-во по умолчанию: <b>{amount}</b>\n"
        f"🔑 Данные Fragment: <b>{creds}</b>\n"
        f"{kw_line}\n"
        f"👤 Спрашивать @username: <b>{ask}</b>\n"
        f"⚠️ Предупреждать о балансе: <b>{warn}</b>"
        + (f" (когда осталось ≤ {left} выдач)" if p.get("low_balance_warn", True)
           else "")
        + f"\n📝 Заметка: <i>{note}</i>\n\n"
        "<i>Число звёзд бот берёт из заголовка — то, что стоит рядом со словом "
        "«звёзд»/«stars», а не первое попавшееся число: год или цена в названии "
        "иначе превращались бы в количество к покупке. Не нашёл — возьмёт "
        "значение по умолчанию.</i>"
    )


def _stars_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    ask = settings["plugins"]["auto_stars"].get("ask_username", True)
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Данные Fragment", callback_data="plugins:stars:creds")
    builder.button(text="✏️ Кол-во по умолчанию", callback_data="plugins:stars:set_amount")
    builder.button(text="🔍 Ключевое слово", callback_data="plugins:stars:set_keyword")
    builder.button(
        text=("👤 Спрашивать username: вкл" if ask else "👤 Спрашивать username: выкл"),
        callback_data="plugins:stars:toggle_ask",
    )
    warn = settings["plugins"]["auto_stars"].get("low_balance_warn", True)
    builder.button(
        text=("⚠️ Предупреждать о балансе: вкл" if warn
              else "⚠️ Предупреждать о балансе: выкл"),
        callback_data="plugins:stars:toggle_warn",
    )
    builder.button(text="📝 Заметка", callback_data="plugins:stars:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_stars")
async def stars_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    shop_name = get_shop_name(uid)
    await callback.message.edit_text(_stars_text(settings, shop_name), reply_markup=_stars_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:toggle")
async def stars_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["enabled"] = not settings["plugins"]["auto_stars"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_stars_text(settings, get_shop_name(uid)), reply_markup=_stars_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:settings")
async def stars_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # Cancel из промптов ведёт сюда — сбрасываем ввод
    uid = callback.from_user.id
    settings = get_settings(uid)
    creds = get_fragment_creds(uid)
    has = bool(creds and creds.get("cookies") and creds.get("mnemonic"))
    await callback.message.edit_text(
        _stars_settings_text(settings, has),
        reply_markup=_stars_settings_keyboard(settings),
    )
    await callback.answer()


# ── Данные Fragment (cookies + seed-фраза) ──────────────────────────────────

# The three cookies Fragment logs a seller in with. They are asked for one at
# a time because two of them are HttpOnly: `document.cookie` simply does not
# contain them, so pasting that string left the session half-built and the
# failure only showed up at the first delivery.
FRAGMENT_COOKIES = (
    ("stel_token", "🔑 stel_token"),
    ("stel_ssid", "🆔 stel_ssid"),
    ("stel_ton_token", "💎 stel_ton_token"),
)


def _creds_kb(has: bool, cookies: dict | None = None) -> InlineKeyboardMarkup:
    cookies = cookies or {}
    b = InlineKeyboardBuilder()
    for i, (name, label) in enumerate(FRAGMENT_COOKIES):
        mark = "✅" if cookies.get(name) else "▫️"
        b.button(text=f"{mark} {label}", callback_data=f"plugins:stars:ck:{i}")
    b.button(text="🔐 Seed-фраза TON", callback_data="plugins:stars:set_mnemonic")
    b.button(text="#️⃣ api-hash (обычно сам)", callback_data="plugins:stars:set_hash")
    b.button(text="🌐 Прокси", callback_data="plugins:stars:set_proxy")
    b.button(text="📋 Вставить всё строкой", callback_data="plugins:stars:set_cookies")
    if has:
        b.button(text="🧪 Проверить вход", callback_data="plugins:stars:check_creds")
        b.button(text="🗑 Удалить данные", callback_data="plugins:stars:del_creds")
    b.button(text="⬅️ Назад", callback_data="plugins:stars:settings")
    b.adjust(1, 1, 1, 1, 1, 1, 1, 2, 1)
    return b.as_markup()


@router.callback_query(F.data == "plugins:stars:creds")
async def stars_creds(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # Cancel из промптов cookies/seed ведёт сюда
    uid = callback.from_user.id
    creds = get_fragment_creds(uid) or {}
    cookies = creds.get("cookies") or {}
    has_m = bool(creds.get("mnemonic"))
    missing = [label for name, label in FRAGMENT_COOKIES if not cookies.get(name)]
    ready = not missing and has_m
    lines = ["🔑 <b>Данные Fragment</b>\n"]
    for name, label in FRAGMENT_COOKIES:
        lines.append(f"{'🟢' if cookies.get(name) else '🔴'} {label}")
    lines.append(f"{'🟢' if has_m else '🔴'} 🔐 Seed-фраза TON")
    lines.append(f"{'🟢' if creds.get('api_hash') else '⚪'} #️⃣ api-hash "
                 f"{'(задан)' if creds.get('api_hash') else '(подберётся сам)'}")
    from automation.fragment import proxy_label
    lines.append(f"{'🟢' if creds.get('proxy') else '⚪'} 🌐 Прокси: "
                 f"{html.escape(proxy_label(creds.get('proxy', '')))}")
    extra = [k for k in cookies if k not in dict(FRAGMENT_COOKIES)]
    if extra:
        lines.append(f"\n<i>Ещё сохранено cookies: {len(extra)}</i>")
    lines.append("")
    if missing or not has_m:
        lines.append("Заполните по одному — каждая кнопка спрашивает "
                     "что-то одно.")
    else:
        lines.append("Всё на месте. Проверьте вход кнопкой ниже.")
    lines.append("")
    lines.append("⚠️ <b>Это доступ к вашему TON-кошельку и Fragment.</b> "
                 "Данные хранятся только у бота, в чат не выводятся. "
                 "Сообщения с секретами удаляются автоматически.")
    await callback.message.edit_text(
        "\n".join(lines), reply_markup=_creds_kb(ready, cookies))
    await callback.answer()


# Куки Fragment — HttpOnly, и без инструментов разработчика их не прочитать.
# «Возьмите на компьютере» — не ответ тому, у кого компьютера нет, поэтому
# здесь названы браузеры, в которых это делается с телефона.
_PHONE_HELP = (
    "📱 <b>Если компьютера нет</b>\n"
    "<i>Лучше снимать куки на компьютере: Fragment привязывает сессию не "
    "только к куке, но и к тому, чем вы представились. Бот ходит "
    "десктопным <code>Mozilla/5.0</code>, и сессия, выданная телефону, ему "
    "может не подойти. Если сняли с телефона и «Access denied» не уходит — "
    "проверьте /fragment_debug: он пробует три разных User-Agent и "
    "показывает, какой из них Fragment признаёт.</i>\n\n"
    "Нужны инструменты разработчика — они есть в мобильных браузерах:\n"
    "• <b>Kiwi Browser</b> (Android) — меню → «Инструменты разработчика»\n"
    "• <b>Яндекс Браузер</b> (Android) — адрес вида "
    "<code>view-source:</code> и расширения из Chrome Web Store\n"
    "• iPhone — <b>Safari</b> + Mac по кабелю, либо приложение "
    "<b>Web Inspector</b>\n"
    "Дальше — как на компьютере: Application → Cookies → fragment.com."
)

# Where each cookie is found, because two of them are HttpOnly and the console
# trick does not reveal them.
_COOKIE_HELP = {
    "stel_token": ("Основной токен сессии Fragment.\n\n"
                   "F12 → вкладка <b>Application</b> (в Firefox — "
                   "<b>Хранилище</b>) → Cookies → <code>https://fragment.com</code> "
                   "→ строка <code>stel_token</code> → скопируйте <b>Value</b>."),
    "stel_ssid": ("Идентификатор сессии.\n\n"
                  "Там же: Application → Cookies → fragment.com → "
                  "<code>stel_ssid</code> → Value."),
    "stel_ton_token": ("Токен привязанного TON-кошелька — появляется после "
                       "входа на Fragment через кошелёк.\n\n"
                       "Application → Cookies → fragment.com → "
                       "<code>stel_ton_token</code> → Value."),
}


@router.callback_query(F.data.startswith("plugins:stars:ck:"))
async def stars_set_one_cookie_prompt(callback: CallbackQuery,
                                      state: FSMContext) -> None:
    """Ask for a single cookie.

    Pasting `document.cookie` cannot work here: stel_token and stel_ssid are
    HttpOnly, so that string is missing exactly the values the session needs,
    and nothing said so until the first delivery failed.
    """
    try:
        idx = int(callback.data.split(":")[-1])
        name, label = FRAGMENT_COOKIES[idx]
    except (ValueError, IndexError):
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    await state.set_state(PluginState.stars_set_one_cookie)
    await state.update_data(cookie_name=name)
    creds = get_fragment_creds(callback.from_user.id) or {}
    have = (creds.get("cookies") or {}).get(name)
    await callback.message.edit_text(
        f"{label}\n\n"
        + (f"Сейчас: <b>задан</b> ({len(str(have))} символов)\n\n" if have else "")
        + _COOKIE_HELP.get(name, "Пришлите значение этой cookie.")
        + "\n\n" + _PHONE_HELP
        + "\n\nПришлите <b>только значение</b> — без названия и без "
          "<code>=</code>. Сообщение сразу удалится.",
        reply_markup=_cancel_kb("plugins:stars:creds"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_one_cookie)
async def stars_set_one_cookie_input(message: Message,
                                     state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("cookie_name") or ""
    raw = (message.text or "").strip()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    creds = get_fragment_creds(message.from_user.id) or {}
    cookies = dict(creds.get("cookies") or {})
    # Pasted as «stel_token=abc» or as a whole cookie string — take what fits
    # rather than saving the name as part of the value.
    if "=" in raw:
        parsed = _parse_cookies(raw)
        if parsed.get(name):
            raw = parsed[name]
        elif len(parsed) == 1:
            raw = next(iter(parsed.values()))
        cookies.update({k: v for k, v in parsed.items() if k != name and v})
    raw = raw.strip().strip(";").strip()
    if not name or not raw:
        await message.answer("❌ Пустое значение — ничего не сохранил.",
                             reply_markup=_creds_kb(False, cookies))
        return

    # Куки вводятся по одной, и перепутать поля легко — у значений
    # одинаково «технический» вид. Но отличаются они надёжно, так что
    # значение кладётся туда, откуда оно на самом деле: молча сохранить его
    # не в своё поле значит отдать Fragment набор, на который он ответит как
    # гостю, и дальше искать причину в куках, хеше и кошельке по очереди.
    from automation.fragment import guess_cookie_name
    looks = guess_cookie_name(raw)
    moved = ""
    if looks and looks != name:
        moved = (f"\n\n↪️ Это значение похоже на <b>{looks}</b>, а не на "
                 f"<b>{name}</b> — сохранил его как {looks}. "
                 f"Проверьте, что в остальные поля попало своё.")
        name = looks
    cookies[name] = raw
    save_fragment_creds(message.from_user.id, {"cookies": cookies})
    creds = get_fragment_creds(message.from_user.id) or {}
    left = [lbl for n, lbl in FRAGMENT_COOKIES if not cookies.get(n)]
    if not creds.get("mnemonic"):
        left.append("🔐 Seed-фраза TON")
    await message.answer(
        f"✅ Сохранено: <b>{name}</b>" + moved
        + (f"\n\nОсталось заполнить: {', '.join(left)}" if left
           else "\n\nВсё готово — проверьте вход кнопкой «🧪 Проверить вход»."),
        reply_markup=_creds_kb(not left and bool(creds.get("mnemonic")), cookies),
    )


@router.callback_query(F.data == "plugins:stars:set_cookies")
async def stars_set_cookies_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_cookies)
    await callback.message.edit_text(
        "🍪 <b>Cookies Fragment — все три одной вставкой</b>\n\n"
        "1. Войдите на <b>fragment.com</b> через TON-кошелёк\n"
        "2. F12 → вкладка <b>Network</b> → обновите страницу → щёлкните "
        "любой запрос к <code>fragment.com</code>\n"
        "3. <b>Request Headers</b> → строка <code>Cookie:</code> → "
        "скопируйте <b>всё значение</b> и пришлите сюда\n\n"
        "Годится и «Copy as cURL» целиком — разберу сам.\n\n"
        "<i>Так приходят и <b>stel_token</b> с <b>stel_ssid</b>: в "
        "<code>document.cookie</code> их нет, они HttpOnly, и именно из-за "
        "этого их раньше приходилось вбивать по одной. Кнопки выше никуда "
        "не делись — если удобнее по одной, они работают.</i>\n\n"
        "⚠️ Сообщение с куками удалю сразу после разбора.\n\n" + _PHONE_HELP,
        reply_markup=_cancel_kb("plugins:stars:creds"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_cookies)
async def stars_set_cookies_input(message: Message, state: FSMContext) -> None:
    cookies = _parse_cookies(message.text or "")
    await state.clear()
    try:  # remove the message containing cookies from the chat
        await message.delete()
    except Exception:
        pass
    if not cookies:
        await message.answer(
            "❌ Не распознал cookies. Нужен формат <code>k=v; k2=v2</code>.",
            reply_markup=_creds_kb(False, (get_fragment_creds(
                message.from_user.id) or {}).get("cookies")))
        return
    # Merge, never replace: the HttpOnly ones were entered by hand and are not
    # in this string, and dropping them would undo that work silently.
    existing = dict((get_fragment_creds(message.from_user.id) or {}).get(
        "cookies") or {})
    existing.update(cookies)
    save_fragment_creds(message.from_user.id, {"cookies": existing})
    creds = get_fragment_creds(message.from_user.id) or {}
    left = [lbl for n, lbl in FRAGMENT_COOKIES if not existing.get(n)]
    await message.answer(
        f"✅ Разобрал {len(cookies)} cookie."
        + (f"\n\n⚠️ Не хватает: {', '.join(left)} — их не бывает в "
           f"<code>document.cookie</code>, задайте по одной."
           if left else ""),
        reply_markup=_creds_kb(
            not left and bool(creds.get("mnemonic")), existing),
    )


@router.callback_query(F.data == "plugins:stars:set_mnemonic")
async def stars_set_mnemonic_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_mnemonic)
    await callback.message.edit_text(
        "🔐 <b>Seed-фраза TON-кошелька</b>\n\n"
        "Пришлите <b>24 слова</b> через пробел — это кошелёк, с которого "
        "будут оплачиваться звёзды.\n\n"
        "⚠️ Держите на нём ровно столько TON, сколько нужно для продаж. "
        "Сообщение будет немедленно удалено.",
        reply_markup=_cancel_kb("plugins:stars:creds"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_mnemonic)
async def stars_set_mnemonic_input(message: Message, state: FSMContext) -> None:
    words = (message.text or "").split()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    if len(words) not in (12, 24):
        await message.answer(
            "❌ Нужно 24 слова (или 12). Проверьте seed-фразу.",
            reply_markup=_creds_kb(False, (get_fragment_creds(
                message.from_user.id) or {}).get("cookies")))
        return
    # validate the phrase derives a wallet before saving
    try:
        from automation.fragment import _wallet_from_mnemonic
        wv = get_settings(message.from_user.id)["plugins"]["auto_stars"].get("wallet_version", "v4r2")
        wallet = _wallet_from_mnemonic(" ".join(words), wv)
        addr = wallet.address.to_string(True, True, True)
    except Exception as e:
        await message.answer(
            f"❌ Seed-фраза не подошла: {str(e)[:80]}",
            reply_markup=_creds_kb(False, (get_fragment_creds(
                message.from_user.id) or {}).get("cookies")))
        return
    save_fragment_creds(message.from_user.id, {"mnemonic": " ".join(words),
                                               "wallet_version": wv})
    creds = get_fragment_creds(message.from_user.id) or {}
    await message.answer(
        f"✅ Кошелёк сохранён.\n💼 Адрес: <code>{addr}</code>\n\n"
        "Пополните его TON для оплаты звёзд.",
        reply_markup=_creds_kb(
            bool(creds.get("cookies") and creds.get("mnemonic")),
            creds.get("cookies")),
    )


@router.callback_query(F.data == "plugins:stars:check_creds")
async def stars_check_creds(callback: CallbackQuery) -> None:
    """Check the session — and keep the api hash if the check had to find one.

    Fragment stamps every request with a per-session hash. The hardcoded one
    belonged to somebody else's session, which is answered with «Bad request»;
    discovering the right one is most of what this check is for.
    """
    from automation.fragment import (check_fragment_session_sync, _same_wallet,
                                     wallet_address_sync, wallet_on_page_sync)
    uid = callback.from_user.id
    creds = get_fragment_creds(uid) or {}
    await callback.answer("⏳ Проверяю…")
    loop = asyncio.get_event_loop()
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, check_fragment_session_sync,
                                 creds.get("cookies"), creds.get("api_hash", "")),
            timeout=40,
        )
    except Exception as e:
        ok, msg = False, str(e)[:80]

    # Сверка кошельков. Покупку разрешает не вход через Telegram, а
    # привязанный TON-кошелёк: сессия может быть живой, а покупка — отвечать
    # «Access denied», и без двух адресов рядом это неразличимо.
    wallets = ""
    if creds.get("cookies"):
        try:
            on_page, mine = await asyncio.wait_for(asyncio.gather(
                loop.run_in_executor(None, functools.partial(
                    wallet_on_page_sync, creds["cookies"])),
                loop.run_in_executor(None, wallet_address_sync,
                                     creds.get("mnemonic", ""),
                                     creds.get("wallet_version", "v4r2"))),
                timeout=40)
        except Exception:
            on_page, mine = "", ""
        if mine or on_page:
            lines = ["", "<b>Кошельки</b>"]
            lines.append(f"На Fragment: <code>{html.escape(on_page)}</code>"
                         if on_page else
                         "На Fragment: <b>не вижу подключённого</b>")
            lines.append(f"У бота: <code>{html.escape(mine)}</code>" if mine
                         else "У бота: <b>seed-фраза не задана</b>")
            if on_page and mine:
                lines.append("✅ Это один кошелёк — покупать можно."
                             if _same_wallet(on_page, mine) else
                             "❌ <b>Это разные кошельки.</b> Fragment примет "
                             "оплату только с того, который к нему подключён — "
                             "отсюда «Access denied» при покупке.")
            elif not on_page:
                lines.append("⚠️ Подключите TON-кошелёк на fragment.com и "
                             "заново скопируйте куку "
                             "<code>stel_ton_token</code> — без неё покупка "
                             "запрещена, даже когда вход через Telegram есть.")
            wallets = "\n".join(lines)
    if isinstance(msg, dict):
        if msg.get("api_hash"):
            save_fragment_creds(uid, {"api_hash": msg["api_hash"]})
        text = ("✅ " if ok else "⚠️ ") + str(msg.get("message", ""))
        # Что делать — по-человечески и без devtools: с телефона F12 не нажать,
        # а именно этим заканчивался прошлый ответ.
        if msg.get("how"):
            text += f"\n\n{msg['how']}"
        # Что бот увидел своими глазами. Без этого «не подошёл хеш» — тупик:
        # непонятно, истекли куки или изменилась страница.
        report = [str(line) for line in (msg.get("report") or [])]
        if report:
            body = "\n".join(html.escape(line) for line in report[:14])
            text += f"\n\n<b>Что увидел бот:</b>\n<code>{body}</code>"
        await callback.message.answer(text + wallets)
        return
    await callback.message.answer(("✅ " if ok else "⚠️ ")
                                  + f"Fragment: {msg}" + wallets)


@router.message(Command("fragment_debug"))
async def fragment_debug(message: Message) -> None:
    """/fragment_debug — почему Fragment не признаёт сессию. Только чтение."""
    from automation.fragment import probe_session_sync

    creds = get_fragment_creds(message.from_user.id) or {}
    if not creds.get("cookies"):
        await message.answer("⚠️ Куки Fragment не заданы: Плагины → AutoStars "
                             "→ 🔑 Данные Fragment")
        return
    status = await message.answer("⏳ Пробую разные способы представиться…")
    loop = asyncio.get_event_loop()
    try:
        lines = await asyncio.wait_for(
            loop.run_in_executor(None, probe_session_sync, creds["cookies"]),
            timeout=90)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    body = "\n".join(html.escape(str(x)) for x in lines)[:3500]
    await status.edit_text(f"🔍 <b>Сессия Fragment</b>\n<code>{body}</code>")


@router.message(Command("fragment_cookies"))
async def fragment_cookies(message: Message) -> None:
    """/fragment_cookies — переписывает ли страница покупки наши куки.

    Проба под конкретную версию, а не украшение отчёта: рабочий клиент
    продавца страницу не читает вовсе, а мы читаем — и если её `Set-Cookie`
    ложатся поверх кук продавца, то «Access denied» на заявке при
    проходящем поиске получателя объясняется этим целиком. Только чтение,
    денег не тратит; значения кук наружу не выходят, только имена.
    """
    from automation.fragment import page_rewrites_cookies_sync

    creds = get_fragment_creds(message.from_user.id) or {}
    if not creds.get("cookies"):
        await message.answer("⚠️ Куки Fragment не заданы: Плагины → AutoStars "
                             "→ 🔑 Данные Fragment")
        return
    status = await message.answer("⏳ Открываю страницу покупки и сравниваю "
                                  "куки до и после…")
    loop = asyncio.get_event_loop()
    try:
        lines = await asyncio.wait_for(
            loop.run_in_executor(None, page_rewrites_cookies_sync,
                                 creds["cookies"], creds.get("proxy", "")),
            timeout=60)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    body = "\n".join(html.escape(str(x)) for x in lines)[:3500]
    await status.edit_text(
        f"🍪 <b>Страница покупки и наши куки</b>\n<code>{body}</code>")


@router.message(Command("fragment_js"))
async def fragment_js(message: Message) -> None:
    """/fragment_js — как сайт зовёт свой API, по его же коду. Только чтение.

    Отдельной командой, а не хвостом к другой: ответ длинный, а в общем
    отчёте он обрезался ровно на том месте, ради которого затевался.
    """
    from automation.fragment import probe_page_api_sync

    creds = get_fragment_creds(message.from_user.id) or {}
    if not creds.get("cookies"):
        await message.answer("⚠️ Куки Fragment не заданы: Плагины → AutoStars "
                             "→ 🔑 Данные Fragment")
        return
    status = await message.answer("⏳ Читаю страницу покупки и её скрипты…")
    loop = asyncio.get_event_loop()
    try:
        lines = await asyncio.wait_for(
            loop.run_in_executor(None, probe_page_api_sync, creds["cookies"]),
            timeout=180)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    body = "\n".join(html.escape(str(x)) for x in lines)[:3800]
    await status.edit_text(f"🔍 <b>Код страницы покупки</b>\n<code>{body}</code>")


@router.message(Command("stars_probe"))
async def stars_probe(message: Message) -> None:
    """/stars_probe <ник> [кол-во] [контрольный ник] — где ломается покупка.

    Ничего не оплачивает: доходит до заявки, а деньги двигает только
    подписанная транзакция после неё.
    """
    from automation.fragment import probe_buy_sync

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Укажите ник: <code>/stars_probe NO0RD</code>")
        return
    username = parts[1].lstrip("@")
    try:
        qty = int(parts[2]) if len(parts) > 2 else 50
    except ValueError:
        qty = 50
    # Третьим словом — чужой ник для контрольной заявки. Без него берётся
    # @durov: заявка денег не двигает, но ник должен быть настоящим.
    control = next((p.lstrip("@") for p in parts[2:] if not p.isdigit()),
                   "durov")
    creds = get_fragment_creds(message.from_user.id) or {}
    if not creds.get("cookies"):
        await message.answer("⚠️ Куки Fragment не заданы")
        return
    status = await message.answer("⏳ Пробую варианты запроса…")
    loop = asyncio.get_event_loop()
    try:
        lines = await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(
                probe_buy_sync, creds["cookies"], username, qty,
                creds.get("api_hash", ""), control,
                creds.get("proxy", ""))),
            # Вариантов шесть, на каждый по два хеша и по два запроса, плюс
            # чтение страниц. Прежних 120 с на это уже не хватает, а обрыв по
            # таймауту выглядит как отказ Fragment и путает следствие.
            timeout=300)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    # Раздел «что говорит страница» переехал в /fragment_debug: он два
    # прогона подряд сообщал, что имён методов в разметке не видно, — а их
    # там нет вовсе, включая работающий searchStarsRecipient. Вывода из него
    # не следует никакого, зато он занимал место в отчёте.
    body = "\n".join(html.escape(str(x)) for x in lines)[:3800]
    await status.edit_text(f"🔍 <b>Покупка {qty}⭐ для @{html.escape(username)}"
                           f"</b>\n<code>{body}</code>")


@router.callback_query(F.data == "plugins:stars:set_hash")
async def stars_set_hash_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    creds = get_fragment_creds(callback.from_user.id) or {}
    cur = creds.get("api_hash") or ""
    await state.set_state(PluginState.stars_set_hash)
    await callback.message.edit_text(
        "#️⃣ <b>api-hash Fragment</b>\n\n"
        + (f"Сейчас: <code>{cur}</code>\n\n" if cur else "")
        + "Fragment помечает этим хешем каждый запрос, и у каждой сессии он "
          "свой — чужой отвечает «Bad request».\n\n"
          "Обычно заполнять не нужно: бот сам читает хеш со страницы "
          "Fragment перед каждой покупкой. Но если задать его здесь — "
          "<b>бот возьмёт именно его</b> и страницу читать не станет.\n\n"
          "Это и есть способ подставить хеш из браузера, где покупка "
          "проходит: F12 → вкладка <b>Network</b> → на fragment.com "
          "нажмите покупку → найдите запрос <code>api?…</code> → "
          "скопируйте значение после <code>hash=</code>. На Android "
          "инструменты разработчика есть в <b>Kiwi Browser</b>.\n\n"
          "Хеш живёт столько же, сколько сессия: протухнет — очистите поле "
          "и бот снова начнёт брать его сам.",
        reply_markup=_cancel_kb("plugins:stars:creds"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_hash)
async def stars_set_hash_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await state.clear()
    # Pasted as a whole URL or as «hash=…» — take the value out of it.
    m = re.search(r"hash=([0-9a-zA-Z]+)", raw)
    if m:
        raw = m.group(1)
    raw = raw.strip().strip(";").strip()
    creds = get_fragment_creds(message.from_user.id) or {}
    if not raw or len(raw) < 8:
        await message.answer(
            "❌ Не похоже на hash — нужно значение после <code>hash=</code>.",
            reply_markup=_creds_kb(False, creds.get("cookies")))
        return
    save_fragment_creds(message.from_user.id, {"api_hash": raw})
    creds = get_fragment_creds(message.from_user.id) or {}
    cookies = creds.get("cookies") or {}
    ready = (all(cookies.get(n) for n, _l in FRAGMENT_COOKIES)
             and bool(creds.get("mnemonic")))
    await message.answer(
        f"✅ api-hash сохранён.\n\nПроверьте вход кнопкой «🧪 Проверить вход».",
        reply_markup=_creds_kb(ready, cookies))


@router.callback_query(F.data == "plugins:stars:set_proxy")
async def stars_set_proxy_prompt(callback: CallbackQuery,
                                 state: FSMContext) -> None:
    from automation.fragment import proxy_label

    creds = get_fragment_creds(callback.from_user.id) or {}
    cur = proxy_label(creds.get("proxy", ""))
    await state.set_state(PluginState.stars_set_proxy)
    await callback.message.edit_text(
        "🌐 <b>Прокси для Fragment</b>\n\n"
        f"Сейчас: <code>{html.escape(cur)}</code>\n\n"
        "Fragment выдаёт сессию браузеру на вашем адресе, а бот живёт в "
        "дата-центре. Похоже, из-за этого куки у бота «протухают» за "
        "полчаса, а покупка отказывает с первой секунды. Прокси с обычным "
        "домашним или мобильным адресом это проверяет и, если версия верна, "
        "чинит.\n\n"
        "Формат:\n"
        "<code>http://логин:пароль@хост:порт</code>\n"
        "<code>socks5://логин:пароль@хост:порт</code>\n\n"
        "Отправьте <code>-</code>, чтобы убрать прокси.\n\n"
        "⚠️ Логин и пароль нигде не показываются — в отчётах видны только "
        "хост и порт.",
        reply_markup=_cancel_kb("plugins:stars:creds"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_proxy)
async def stars_set_proxy_input(message: Message, state: FSMContext) -> None:
    from automation.fragment import proxy_label

    raw = (message.text or "").strip()
    await state.clear()
    uid = message.from_user.id
    # Строка с прокси несёт логин и пароль — в переписке ей не место.
    try:
        await message.delete()
    except Exception:
        pass
    creds = get_fragment_creds(uid) or {}
    cookies = creds.get("cookies") or {}
    ready = (all(cookies.get(n) for n, _l in FRAGMENT_COOKIES)
             and bool(creds.get("mnemonic")))
    if raw in ("-", "—", "нет"):
        save_fragment_creds(uid, {"proxy": ""})
        await message.answer("✅ Прокси убран.",
                             reply_markup=_creds_kb(ready, cookies))
        return
    if not re.match(r"^(https?|socks5h?)://\S+:\d{2,5}/?$", raw):
        await message.answer(
            "❌ Не похоже на адрес прокси. Нужен вид "
            "<code>http://логин:пароль@хост:порт</code>.",
            reply_markup=_creds_kb(ready, cookies))
        return
    from automation.fragment import proxy_problem
    trouble = proxy_problem(raw)
    if trouble:
        await message.answer(f"❌ Не сохраняю: {trouble}",
                             reply_markup=_creds_kb(ready, cookies))
        return
    save_fragment_creds(uid, {"proxy": raw})
    await message.answer(
        f"✅ Прокси сохранён: <code>{html.escape(proxy_label(raw))}</code>\n\n"
        "Проверьте командой /stars_probe — в первой строке отчёта будет "
        "видно, с какого адреса бот выходит наружу.",
        reply_markup=_creds_kb(ready, cookies))


@router.callback_query(F.data == "plugins:stars:del_creds")
async def stars_del_creds(callback: CallbackQuery, state: FSMContext) -> None:
    delete_fragment_creds(callback.from_user.id)
    await callback.answer("🗑 Данные Fragment удалены", show_alert=True)
    await stars_creds(callback, state)


@router.callback_query(F.data == "plugins:stars:toggle_warn")
async def stars_toggle_warn(callback: CallbackQuery) -> None:
    """Warn while the wallet can still be topped up, not at the checkout."""
    uid = callback.from_user.id
    s = get_settings(uid)
    p = s["plugins"]["auto_stars"]
    p["low_balance_warn"] = not p.get("low_balance_warn", True)
    p["balance_checked_at"] = 0          # look again on the next cycle
    save_settings(uid, s)
    creds = get_fragment_creds(uid)
    has = bool(creds and creds.get("cookies") and creds.get("mnemonic"))
    await callback.message.edit_text(
        _stars_settings_text(s, has), reply_markup=_stars_settings_keyboard(s))
    await callback.answer(
        "Скажу заранее, когда TON будет заканчиваться"
        if p["low_balance_warn"] else "Молчу про баланс")


@router.callback_query(F.data == "plugins:stars:toggle_ask")
async def stars_toggle_ask(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(uid)
    p = s["plugins"]["auto_stars"]
    p["ask_username"] = not p.get("ask_username", True)
    save_settings(uid, s)
    creds = get_fragment_creds(uid)
    has = bool(creds and creds.get("cookies") and creds.get("mnemonic"))
    await callback.message.edit_text(
        _stars_settings_text(s, has), reply_markup=_stars_settings_keyboard(s))
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:set_keyword")
async def stars_set_keyword_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_keyword)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_stars"].get("keyword", "звёзд")
    await callback.message.edit_text(
        f"🔍 Ключевое слово в заголовке заказа (сейчас: <code>{cur}</code>)\n\n"
        "Заказы, чей заголовок содержит это слово, будут обрабатываться "
        "автовыдачей звёзд. Введите слово:",
        reply_markup=_cancel_kb("plugins:stars:settings"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_keyword)
async def stars_set_keyword_input(message: Message, state: FSMContext) -> None:
    kw = (message.text or "").strip().lower()
    await state.clear()
    if not kw:
        await message.answer("❌ Слово не может быть пустым.")
        return
    uid = message.from_user.id
    s = get_settings(uid)
    s["plugins"]["auto_stars"]["keyword"] = kw
    save_settings(uid, s)
    await message.answer(
        f"✅ Ключевое слово: <code>{kw}</code>",
        reply_markup=_stars_settings_keyboard(s))


@router.callback_query(F.data == "plugins:stars:set_amount")
async def stars_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_amount)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_stars"].get("amount", 50)
    await callback.message.edit_text(
        f"⭐ Кол-во звёзд (сейчас: <b>{cur}</b>)\n\nВведите число:",
        reply_markup=_cancel_kb("plugins:stars:settings"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_amount)
async def stars_set_amount_input(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое положительное число:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["amount"] = amount
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Кол-во звёзд: <b>{amount}</b>", reply_markup=_stars_settings_keyboard(settings))


@router.callback_query(F.data == "plugins:stars:set_note")
async def stars_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_note)
    await callback.message.edit_text(
        "📝 Введите заметку для покупателя:",
        reply_markup=_cancel_kb("plugins:stars:settings"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_note)
async def stars_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка сохранена: <i>{note or '—'}</i>", reply_markup=_stars_settings_keyboard(settings))


@router.callback_query(F.data == "plugins:stars:whois")
async def stars_whois_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_whois)
    await callback.message.edit_text(
        "🔎 <b>Проверить ник</b>\n\n"
        "Введите @username получателя — проверю, находит ли его Fragment и "
        "примет ли на него заявку.\n\n"
        "<i>Ничего не оплачивается: деньги уходят только при отправке "
        "подписанной транзакции, а до неё здесь дело не доходит.</i>",
        reply_markup=_cancel_kb("plugins:auto_stars"),
    )
    await callback.answer()


@router.message(PluginState.stars_whois)
async def stars_whois_input(message: Message, state: FSMContext) -> None:
    from automation.fragment import probe_recipient_sync

    nick = (message.text or "").strip()
    await state.clear()
    creds = get_fragment_creds(message.from_user.id) or {}
    if not creds.get("cookies"):
        await message.answer("⚠️ Куки Fragment не заданы: Плагины → AutoStars "
                             "→ ⚙️ Настройки → 🔑 Данные Fragment")
        return
    qty = get_settings(message.from_user.id)["plugins"]["auto_stars"].get(
        "amount", 50)
    status = await message.answer("⏳ Спрашиваю Fragment…")
    loop = asyncio.get_event_loop()
    try:
        lines = await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(
                probe_recipient_sync, creds["cookies"], nick, qty,
                creds.get("api_hash", ""), creds.get("proxy", ""))),
            timeout=90)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Ещё ник", callback_data="plugins:stars:whois")
    b.button(text="⬅️ AutoStars", callback_data="plugins:auto_stars")
    b.adjust(1)
    body = "\n".join(html.escape(str(x)) for x in lines)[:3500]
    await status.edit_text(f"🔎 <b>Проверка получателя</b>\n\n{body}",
                           reply_markup=b.as_markup())


@router.callback_query(F.data == "plugins:stars:manual")
async def stars_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача звёзд</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_stars"),
    )
    await callback.answer()


@router.message(PluginState.stars_manual_buyer)
async def stars_manual_buyer_input(message: Message, state: FSMContext) -> None:
    await state.update_data(buyer=message.text or "")
    p = get_settings(message.from_user.id)["plugins"]["auto_stars"]
    default_amount = p.get("amount", 50)
    await state.set_state(PluginState.stars_manual_amount)
    await message.answer(
        f"⭐ Кол-во звёзд (по умолчанию: {default_amount}):\n\nВведите число или отправьте 0 для значения по умолчанию:"
    )


@router.message(PluginState.stars_manual_amount)
async def stars_manual_amount_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buyer = str(data.get("buyer", "")).strip()
    await state.clear()
    try:
        amount = int((message.text or "").strip())
        if amount == 0:
            amount = get_settings(message.from_user.id)["plugins"]["auto_stars"].get("amount", 50)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return

    status = await message.answer(
        f"⭐ <b>Выдаю звёзды…</b>\n\n"
        f"👤 Получатель: <b>{buyer}</b>\n"
        f"⭐ Кол-во: <b>{amount}</b>\n\n"
        f"<i>Покупка через Fragment и подтверждение в TON — до 3 минут…</i>"
    )
    ok, msg, no_repeat = await deliver_stars(message.from_user.id, buyer, amount)
    b = InlineKeyboardBuilder()
    b.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    if ok:
        await status.edit_text(
            f"✅ <b>Звёзды выданы!</b>\n\n{msg}", reply_markup=b.as_markup())
    elif no_repeat:
        # Кнопки «Повторить» здесь нет намеренно: чем кончилась покупка,
        # неизвестно, и нажатие купило бы звёзды второй раз. Предлагать
        # действие, которое может стоить денег на пустом месте, — это то же
        # обещание невозможного, что и совет ответить в закрытый чат.
        b.adjust(1)
        await status.edit_text(
            f"⚠️ <b>Чем кончилась выдача — неизвестно</b>\n\n{msg}",
            reply_markup=b.as_markup())
    else:
        b.button(text="🔁 Повторить", callback_data="plugins:stars:manual")
        b.adjust(1)
        await status.edit_text(
            f"❌ <b>Не удалось выдать</b>\n\n{msg}", reply_markup=b.as_markup())


def _pending_text(p: dict) -> str:
    """Заказы, по которым звёзды ещё не ушли, и почему.

    Ждущий ника и сорвавшийся — разные беды: первому надо написать
    покупателю, второго можно выдать прямо сейчас. Одним списком их
    показывать бессмысленно.
    """
    pending = p.get("pending") or {}
    stuck = p.get("stuck") or {}
    if not pending and not stuck:
        return ("📦 <b>Накопленные заказы</b>\n\n"
                "Пусто — всё выдано.\n\n"
                "<i>Сюда попадают заказы, которые ждут ник покупателя или "
                "сорвались при выдаче.</i>")
    lines = ["📦 <b>Накопленные заказы</b>", ""]
    if stuck:
        lines.append(f"⛔ <b>Сорвались ({len(stuck)})</b> — можно выдать сейчас:")
        for oid, s in list(stuck.items())[:10]:
            when = _ago(s.get("ts"))
            lines.append(f"· #{html.escape(str(oid))} — {int(s.get('quantity') or 0)}⭐ "
                         f"на @{html.escape(str(s.get('username') or '—'))}{when}")
            reason = str(s.get("reason") or "")[:80]
            if reason:
                lines.append(f"  <i>{html.escape(reason)}</i>")
        lines.append("")
    if pending:
        lines.append(f"⏳ <b>Ждут ник ({len(pending)})</b>:")
        for oid, s in list(pending.items())[:10]:
            asked = _ago(s.get("asked_at"))
            tries = int(s.get("tries") or 0)
            tail = f", попыток {tries}" if tries else ""
            lines.append(f"· #{html.escape(str(oid))} — "
                         f"{int(s.get('quantity') or 0)}⭐{asked}{tail}")
        lines.append("")
        lines.append("<i>Бот сам напомнит покупателю и выдаст, как только "
                     "получит ник.</i>")
    return "\n".join(lines)


def _ago(ts) -> str:
    try:
        delta = time.time() - float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if delta < 0 or not ts:
        return ""
    hours = int(delta // 3600)
    if hours < 1:
        return f", {max(1, int(delta // 60))} мин назад"
    if hours < 48:
        return f", {hours} ч назад"
    return f", {hours // 24} дн назад"


def _pending_kb(p: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for oid, s in list((p.get("stuck") or {}).items())[:6]:
        qty = int(s.get("quantity") or 0)
        who = str(s.get("username") or "")
        b.button(text=f"🚀 Выдать #{oid} — {qty}⭐",
                 callback_data=f"plugins:stars:retry:{oid}")
        if not who:
            break
    b.adjust(1)
    b.button(text="🔄 Обновить", callback_data="plugins:stars:accumulated")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    b.adjust(1, *([1] * 5), 2)
    return b.as_markup()


@router.callback_query(F.data == "plugins:stars:accumulated")
async def stars_accumulated(callback: CallbackQuery) -> None:
    p = get_settings(callback.from_user.id)["plugins"]["auto_stars"]
    await callback.message.edit_text(_pending_text(p), reply_markup=_pending_kb(p))
    await callback.answer()


@router.callback_query(F.data.startswith("plugins:stars:retry:"))
async def stars_retry(callback: CallbackQuery) -> None:
    """Выдать сорвавшийся заказ ещё раз — руками, из накопленных."""
    from automation.fragment import buy_stars_sync

    uid = callback.from_user.id
    order_id = callback.data.rsplit(":", 1)[1]
    s = get_settings(uid)
    p = s["plugins"]["auto_stars"]
    entry = (p.get("stuck") or {}).get(order_id)
    if not entry:
        await callback.answer("Этого заказа уже нет в списке", show_alert=True)
        await stars_accumulated(callback)
        return
    creds = get_fragment_creds(uid) or {}
    if not creds.get("cookies") or not creds.get("mnemonic"):
        await callback.answer("Сначала заполните «🔑 Данные Fragment»",
                              show_alert=True)
        return

    username = str(entry.get("username") or "").lstrip("@")
    qty = int(entry.get("quantity") or 0)
    await callback.answer("⏳ Отправляю…")
    status = await callback.message.answer(
        f"⏳ Выдаю {qty}⭐ на @{username} по заказу #{order_id}…")
    spend: dict = {}
    no_repeat = False
    loop = asyncio.get_event_loop()
    try:
        from automation.fragment import BUY_TIMEOUT_SECS
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(
                buy_stars_sync, creds["cookies"], creds["mnemonic"],
                username, qty, creds.get("wallet_version", "v4r2"),
                creds.get("api_hash", ""), report=spend,
                proxy=creds.get("proxy", ""))),
            timeout=BUY_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        # Ровно то же, что и в автоматической выдаче: оборванное ожидание —
        # это неизвестность, а не отказ. Поток продолжает работать и может
        # отправить деньги уже после обрыва.
        ok, msg, no_repeat = False, (
            f"⏱ Покупка не завершилась за {BUY_TIMEOUT_SECS // 60} мин, и чем "
            f"она кончилась — неизвестно.\n\n{_DO_NOT_REPEAT}"), True
    except Exception as e:
        ok, msg = False, str(e)[:150] or type(e).__name__
    if not ok and spend.get("sent_onchain"):
        ton = float(spend.get("ton") or 0)
        msg = (f"{msg}\n\n⚠️ Списано {ton:.4f} TON — перевод ушёл, а выдача "
               f"не подтверждена.\n{_DO_NOT_REPEAT}")
        no_repeat = True

    s = get_settings(uid)          # перечитываем: фон мог тронуть настройки
    p = s["plugins"]["auto_stars"]
    if ok:
        p.get("stuck", {}).pop(order_id, None)
        p.setdefault("delivered", []).append(order_id)
        from tasks.manager import _log_delivery
        _log_delivery(s, order_id, qty, username, float(spend.get("ton") or 0))
        save_settings(uid, s)
        await status.edit_text(f"✅ <b>Выдано</b>\n\n{html.escape(str(msg)[:600])}",
                               reply_markup=_pending_kb(p))
    elif no_repeat:
        # Заказ остаётся в списке — кнопка выдачи нужна: продавец проверит
        # начисление на fragment.com и решит сам. Но пометка о том, что
        # деньги могли уйти, остаётся в записи, а не только на этом экране:
        # через час он его уже не увидит.
        entry = (p.get("stuck") or {}).get(order_id)
        if isinstance(entry, dict):
            entry["unknown"] = True
            entry["reason"] = str(msg)[:200]
            save_settings(uid, s)
        await status.edit_text(
            f"⚠️ <b>Чем кончилась выдача — неизвестно</b>\n\n"
            f"{html.escape(str(msg)[:500])}",
            reply_markup=_pending_kb(p))
    else:
        # Заказ остаётся в списке: неудачная попытка — не повод его потерять.
        await status.edit_text(
            f"❌ <b>Не вышло</b>\n\n{html.escape(str(msg)[:400])}",
            reply_markup=_pending_kb(p))


def _profit_text(p: dict) -> str:
    """Сколько выдано и сколько на этом заработано — по журналу выдач."""
    log = [e for e in (p.get("log") or []) if isinstance(e, dict)]
    if not log:
        return ("💎 <b>Прибыль</b>\n\n"
                "Пока нечего считать — выдач не было.\n\n"
                "<i>Здесь появится, сколько звёзд выдано, сколько получено с "
                "заказов и сколько TON на это ушло. Считается по факту: "
                "выручка — из заказа, расход — из суммы, которую подписал "
                "кошелёк.</i>")
    now = time.time()

    def summarize(rows: list) -> tuple[int, int, float, float]:
        stars = sum(int(e.get("qty") or 0) for e in rows)
        ton = sum(float(e.get("ton") or 0) for e in rows)
        rub = sum(float(e.get("revenue") or 0) for e in rows)
        return len(rows), stars, ton, rub

    day = [e for e in log if now - float(e.get("ts") or 0) < 86400]
    month = [e for e in log if now - float(e.get("ts") or 0) < 30 * 86400]

    lines = ["💎 <b>Прибыль AutoStars</b>", ""]
    for title, rows in (("За сутки", day), ("За 30 дней", month),
                        ("Всего", log)):
        n, stars, ton, rub = summarize(rows)
        if not n:
            lines.append(f"<b>{title}</b>: выдач нет")
            continue
        lines.append(f"<b>{title}</b>: {n} выдач, {stars}⭐")
        if rub:
            lines.append(f"   получено <b>{rub:.0f} ₽</b>")
        lines.append(f"   потрачено <b>{ton:.4f} TON</b>")
    known = [e for e in log if e.get("revenue")]
    if known:
        rub = sum(float(e.get("revenue") or 0) for e in known)
        stars = sum(int(e.get("qty") or 0) for e in known)
        if stars:
            lines += ["", f"<i>Средняя цена продажи: {rub / stars:.2f} ₽ за "
                          f"звезду (по {len(known)} заказам с известной "
                          f"суммой).</i>"]
    lines += ["", "<i>Расход в рублях бот не считает: курс TON он не знает, а "
                  "выдумывать его нельзя. Сравните TON с тем, во сколько "
                  "обошлось пополнение кошелька.</i>"]
    return "\n".join(lines)


@router.callback_query(F.data == "plugins:stars:profit")
async def stars_profit(callback: CallbackQuery) -> None:
    p = get_settings(callback.from_user.id)["plugins"]["auto_stars"]
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="plugins:stars:profit")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    b.adjust(2)
    await callback.message.edit_text(_profit_text(p), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:balance")
async def stars_balance(callback: CallbackQuery) -> None:
    from automation.fragment import get_wallet_balance_sync
    uid = callback.from_user.id
    creds = get_fragment_creds(uid)
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="plugins:stars:balance")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    b.adjust(2)
    if not creds or not creds.get("mnemonic"):
        await callback.message.edit_text(
            "💰 <b>Баланс TON-кошелька</b>\n\n"
            "🔴 Seed-фраза не настроена.\n"
            "Настройки → 🔑 Данные Fragment → 🔐 Задать seed-фразу TON",
            reply_markup=b.as_markup(),
        )
        await callback.answer()
        return
    await callback.answer("⏳ Запрашиваю баланс…")
    loop = asyncio.get_event_loop()
    try:
        ok, res = await asyncio.wait_for(
            loop.run_in_executor(
                None, get_wallet_balance_sync,
                creds["mnemonic"], creds.get("wallet_version", "v4r2"),
            ),
            timeout=25,
        )
    except Exception as e:
        ok, res = False, str(e)[:80]
    if ok:
        ton = res["ton"]
        # грубая оценка: ~0.0155 TON за звезду (можно уточнить)
        approx_stars = int(ton / 0.016) if ton > 0 else 0
        warn = "\n\n⚠️ <b>Мало TON — пополните кошелёк!</b>" if ton < 1 else ""
        text = (
            "💰 <b>Баланс TON-кошелька</b>\n\n"
            f"💎 <b>{ton:.4f} TON</b>\n"
            f"≈ хватит на <b>{approx_stars}</b>⭐ (ориентировочно){warn}\n\n"
            f"💼 <code>{res['address']}</code>"
        )
    else:
        text = f"💰 <b>Баланс TON-кошелька</b>\n\n❌ {res}"
    await callback.message.edit_text(text, reply_markup=b.as_markup())


# Какие уведомления бот шлёт продавцу по звёздам. Провал молчать не должен
# никогда, но удачная выдача в потоке из тридцати заказов — это шум.
_STARS_NOTIFS = (
    ("done", "✅ Об удачной выдаче"),
    ("failed", "❌ О неудаче"),
    ("low_balance", "⚠️ О нехватке TON"),
)


def _notifs_text(p: dict) -> str:
    n = p.get("notify") or {}
    lines = ["🔔 <b>Уведомления AutoStars</b>", ""]
    for key, title in _STARS_NOTIFS:
        lines.append(f"{'🟢' if n.get(key, True) else '🔴'} {title}")
    lines += ["", "<i>Выдача и напоминания покупателю от этого не зависят — "
                  "молчит только лента продавца.</i>"]
    return "\n".join(lines)


def _notifs_kb(p: dict) -> InlineKeyboardMarkup:
    n = p.get("notify") or {}
    b = InlineKeyboardBuilder()
    for key, title in _STARS_NOTIFS:
        mark = "🟢" if n.get(key, True) else "🔴"
        b.button(text=f"{mark} {title}", callback_data=f"plugins:stars:ntog:{key}")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "plugins:stars:notifs")
async def stars_notifs(callback: CallbackQuery) -> None:
    p = get_settings(callback.from_user.id)["plugins"]["auto_stars"]
    await callback.message.edit_text(_notifs_text(p), reply_markup=_notifs_kb(p))
    await callback.answer()


@router.callback_query(F.data.startswith("plugins:stars:ntog:"))
async def stars_notif_toggle(callback: CallbackQuery) -> None:
    key = callback.data.rsplit(":", 1)[1]
    if key not in dict(_STARS_NOTIFS):
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    uid = callback.from_user.id
    s = get_settings(uid)
    n = s["plugins"]["auto_stars"].setdefault("notify", {})
    n[key] = not n.get(key, True)
    save_settings(uid, s)
    await callback.answer("Включено" if n[key] else "Выключено")
    p = s["plugins"]["auto_stars"]
    await callback.message.edit_text(_notifs_text(p), reply_markup=_notifs_kb(p))


# Что бот пишет покупателю на каждом шаге. Подставляются только эти поля —
# обещать в подсказке больше, чем подставляется, хуже, чем не обещать.
_STARS_REPLIES = (
    ("ask", "❓ Запрос ника", "—"),
    ("remind", "🔔 Напоминание", "—"),
    ("sending", "⏳ Отправляю", "{qty}, {username}"),
    ("done", "✅ Готово", "{qty}, {username}"),
    ("failed", "⚠️ Не вышло", "{qty}, {username}"),
)


def _replies_text(p: dict) -> str:
    from tasks.manager import STARS_TEXTS
    texts = p.get("texts") or {}
    lines = ["💬 <b>Ответы покупателю</b>", ""]
    for key, title, fields in _STARS_REPLIES:
        own = str(texts.get(key) or "").strip()
        body = own or STARS_TEXTS.get(key, "")
        mark = "✏️" if own else "▫️"
        lines.append(f"{mark} <b>{title}</b>"
                     + ("" if own else " <i>(стандартный)</i>"))
        lines.append(f"<i>{html.escape(body[:160])}</i>")
        if fields != "—":
            lines.append(f"<code>{html.escape(fields)}</code>")
        lines.append("")
    lines.append("<i>Нажмите, чтобы заменить. Пустое сообщение вернёт "
                 "стандартный текст.</i>")
    return "\n".join(lines)


def _replies_kb(p: dict) -> InlineKeyboardMarkup:
    texts = p.get("texts") or {}
    b = InlineKeyboardBuilder()
    for key, title, _f in _STARS_REPLIES:
        mark = "✏️" if str(texts.get(key) or "").strip() else "▫️"
        b.button(text=f"{mark} {title}", callback_data=f"plugins:stars:rep:{key}")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "plugins:stars:replies")
async def stars_replies(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    p = get_settings(callback.from_user.id)["plugins"]["auto_stars"]
    await callback.message.edit_text(_replies_text(p), reply_markup=_replies_kb(p))
    await callback.answer()


@router.callback_query(F.data.startswith("plugins:stars:rep:"))
async def stars_reply_edit(callback: CallbackQuery, state: FSMContext) -> None:
    from tasks.manager import STARS_TEXTS
    key = callback.data.rsplit(":", 1)[1]
    titles = {k: (title, fields) for k, title, fields in _STARS_REPLIES}
    if key not in titles:
        await callback.answer("Неизвестный текст", show_alert=True)
        return
    title, fields = titles[key]
    p = get_settings(callback.from_user.id)["plugins"]["auto_stars"]
    own = str((p.get("texts") or {}).get(key) or "").strip()
    await state.set_state(PluginState.stars_set_reply)
    await state.update_data(reply_key=key)
    hint = ("" if fields == "—" else
            f"\n\nМожно подставить: <code>{html.escape(fields)}</code>")
    await callback.message.edit_text(
        f"💬 <b>{title}</b>\n\n"
        f"Сейчас{' (свой)' if own else ' (стандартный)'}:\n"
        f"<i>{html.escape(own or STARS_TEXTS.get(key, ''))}</i>"
        + hint
        + "\n\nПришлите новый текст. Чтобы вернуть стандартный — "
          "отправьте <code>-</code>.",
        reply_markup=_cancel_kb("plugins:stars:replies"))
    await callback.answer()


@router.message(PluginState.stars_set_reply)
async def stars_reply_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = str(data.get("reply_key") or "")
    await state.clear()
    uid = message.from_user.id
    s = get_settings(uid)
    texts = s["plugins"]["auto_stars"].setdefault("texts", {})
    raw = (message.text or "").strip()
    if raw in ("-", "—", ""):
        texts[key] = ""
        said = "Вернул стандартный текст."
    else:
        texts[key] = raw[:900]
        said = "Сохранил."
    save_settings(uid, s)
    p = s["plugins"]["auto_stars"]
    await message.answer(f"✅ {said}\n\n" + _replies_text(p),
                         reply_markup=_replies_kb(p))


# ---------------------------------------------------------------------------
# AutoRoblox
# ---------------------------------------------------------------------------

def _roblox_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_roblox"]
    enabled = p.get("enabled", False)
    note = p.get("note") or ""
    # Название магазина приходит с маркетплейса, заметку пишет продавец.
    # Одиночный «<» в любом из них роняет отправку целиком — и роняет
    # он тот самый экран, через который заметку и правят: выйти из
    # такой ловушки было бы уже нечем.
    name_part = f" • {html.escape(shop_name)}" if shop_name else ""
    # Теперь тумблер действительно включает выдачу: фоновый цикл читает этот
    # раздел и покупает код по оплаченному заказу. Но на живом заказе это
    # ещё не проверялось, и молчать об этом нельзя — продавец должен знать,
    # что первая выдача будет первой.
    status = ("🟢 <b>Автовыдача включена</b> — бот сам купит код по "
              "оплаченному заказу" if enabled
              else "🔴 Автовыдача выключена — заказы выдаёте вручную")
    lines = [
        f"🎮 <b>Roblox — Robux{name_part}</b>",
        "",
        "⚠️ <b>На живом заказе выдача ещё не проверялась.</b> Первые "
        "покупки посмотрите глазами: бот пишет о каждой, и об удачной, и "
        "о несостоявшейся.",
        "",
        "<b>Что здесь выдаётся.</b> У поставщика Robux бывают только "
        "<b>кодами</b>: покупатель получает код и активирует его сам на "
        "roblox.com. Зачисления прямо на аккаунт у поставщика нет, поэтому "
        "ник покупателя не нужен — в отличие от звёзд.",
        "",
        "<b>Номиналы фиксированные.</b> Продавать «сколько угодно Robux» "
        "нельзя: есть только те номиналы, что лежат в каталоге. Заказ на "
        "число, которого там нет, бот не подменит ближайшим — он скажет об "
        "этом, а не выдаст другую сумму.",
        "",
        "<b>Регион кода имеет значение.</b> Глобальный и российский коды не "
        "взаимозаменяемы; какой из них продаёте, выбирается в настройках.",
        "",
        "<b>Заказы, оплаченные до включения, сами не подхватываются.</b> "
        "Автовыдача срабатывает на новый заказ и на приход оплаты — то есть "
        "на перемену. Со старыми бот так поступает нарочно: иначе после "
        "чистки хранилища он скупил бы коды по всем прежним заказам разом, "
        "включая давно выданные вручную. Такой заказ выдаётся кнопкой "
        "«🚀 Выдать вручную» — она купит и отправит код в его чат.",
        "",
        status,
    ]
    if note:
        lines.append(f"📝 {html.escape(note)}")
    return "\n".join(lines)


def _roblox_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Кнопки раздела Robux.

    Шесть кнопок отсюда убраны, и это не упрощение. «📦 Выдать накопленные»,
    «💎 Прибыль», «🔔 Уведомления», «💬 Ответы» отвечали всплывающим
    «функция появится в следующем обновлении», а «🚀 Ручная
    выдача» спрашивала @username покупателя — понятие, взятое у звёзд, где
    выдача идёт на аккаунт. У Robux выдаётся код, и слать его некуда, кроме
    чата заказа. Кнопка, которая заведомо не сработает, — то же обещание
    невозможного, что и совет ответить в закрытый чат.

    ns.gifts убран отсюда же: поставщик выбран, а сравнение цены — не то,
    ради чего продавец открывает раздел выдачи. Его каталог никуда не делся
    и открывается командой `/ns_stock`.

    «💰 Баланс» вернулся — но уже не обещанием. Раньше он отвечал «функция
    появится в следующем обновлении», теперь спрашивает кабинет AppRoute и
    показывает, что тот ответил. Кнопка здесь потому, что на этих деньгах
    держится вся выдача: кончатся — встанут разом все заказы.
    """
    enabled = settings["plugins"]["auto_roblox"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    # Доступ к поставщику — отсюда же: искать его отдельной командой продавцу
    # неоткуда, а без него в этом разделе не работает ничего.
    builder.button(text="🔑 Поставщик AppRoute", callback_data="apr:creds")
    builder.button(text="📦 Номиналы и остатки", callback_data="apr:stock")
    builder.button(text="💰 Баланс у поставщика",
                   callback_data="plugins:roblox:balance")
    # Товары по номиналам поставщика. Витрина, где продаётся 500 Robux,
    # автовыдаче недоступна: такого номинала у поставщика нет, а подменять
    # бот не станет. Поэтому объявления удобнее заводить прямо отсюда — из
    # того же каталога, из которого потом покупается код.
    builder.button(text="🏷 Создать товары по номиналам",
                   callback_data="plugins:roblox:make_ads")
    builder.button(text="🚀 Выдать вручную", callback_data="plugins:roblox:manual")
    builder.button(text="📜 Журнал выдач", callback_data="plugins:roblox:log")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить",
                   callback_data="plugins:roblox:toggle")
    builder.button(text="⚙️ Настройки", callback_data="plugins:roblox:settings")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(2, 1, 1, 2, 2, 1)
    return builder.as_markup()


_ROBUX_REGIONS = {
    "GL": "🌍 глобальный — подходит большинству аккаунтов",
    "RU": "🇷🇺 российский — только для аккаунтов с регионом RU",
}


def _roblox_settings_text(settings: dict) -> str:
    """Настройки раздела.

    Поля «Кол-во Robux» здесь больше нет, и это осознанно: количество
    диктует заказ покупателя, а не наша настройка. Число, заданное заранее,
    означало бы «выдать столько, сколько написано у нас», то есть выдать не
    то, что оплачено. Если в названии заказа количества не видно — бот
    скажет об этом, а не подставит запасное значение.
    """
    p = settings["plugins"]["auto_roblox"]
    region = str(p.get("region") or "GL").upper()
    keyword = p.get("keyword") or ""
    note = p.get("note") or "—"
    lines = [
        "⚙️ <b>Настройки AutoRoblox</b>",
        "",
        f"🌐 Регион кода: <b>{region}</b> — {_ROBUX_REGIONS.get(region, '')}",
    ]
    if keyword:
        lines.append(f"🔤 Слово-опознаватель: <code>{html.escape(keyword)}</code> — "
                     f"в работу пойдут только заказы с ним")
    else:
        lines.append("🔤 Слово-опознаватель: <i>не задано</i> — узнаём заказ "
                     "по словам «robux», «робукс», «roblox»")
        lines.append("   ⚠️ Если у вас есть товары вида «Roblox аккаунт», "
                     "задайте слово: иначе они тоже попадут в выдачу Robux.")
    lines.append(f"📝 Заметка: <i>{html.escape(note)}</i>")

    from automation.robux import DEFAULT_AD_TEXT, DEFAULT_AD_TITLE
    title = p.get("ad_title") or ""
    text = p.get("ad_text") or ""
    lines += ["", "🏷 <b>Заготовки для новых товаров</b>"]
    lines.append(f"   Название: <code>{html.escape(title or DEFAULT_AD_TITLE)}</code>"
                 + ("" if title else " <i>(наша)</i>"))
    short = (text or DEFAULT_AD_TEXT).split("\n")[0][:60]
    lines.append(f"   Описание: <code>{html.escape(short)}…</code>"
                 + ("" if text else " <i>(наше)</i>"))
    return "\n".join(lines)


def _roblox_settings_keyboard(settings: dict | None = None) -> InlineKeyboardMarkup:
    region = str(((settings or {}).get("plugins", {})
                  .get("auto_roblox", {}) or {}).get("region") or "GL").upper()
    other = "RU" if region == "GL" else "GL"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🌐 Регион: {region} → сменить на {other}",
                   callback_data=f"plugins:roblox:region:{other}")
    builder.button(text="🔤 Слово-опознаватель",
                   callback_data="plugins:roblox:set_keyword")
    builder.button(text="📝 Заметка", callback_data="plugins:roblox:set_note")
    builder.button(text="🏷 Название товара",
                   callback_data="plugins:roblox:set_ad_title")
    builder.button(text="📄 Описание товара",
                   callback_data="plugins:roblox:set_ad_text")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_roblox")
async def roblox_screen(callback: CallbackQuery, state: FSMContext) -> None:
    """Старый вход в раздел Robux — ведёт на его же экран среди гифт-карт.

    Кнопка осталась в сообщениях, отправленных до переезда, и нажимать её
    будут ещё долго. Прежний экран правил раздел `auto_roblox`, который
    выдачей больше не управляет: тумблер там включался бы, а выдача его не
    читала — то самое молчаливое враньё, ради которого написан CLAUDE.md.
    Поэтому вход не удалён, а перенаправлен.
    """
    from automation.giftcards import ROBUX

    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_gift_card_text(settings, ROBUX),
                                     reply_markup=_gift_card_keyboard(settings, ROBUX))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:toggle")
async def roblox_toggle(callback: CallbackQuery) -> None:
    """Включение автовыдачи. Без ключа поставщика она не включается.

    «🟢 Автовыдача включена — бот сам купит код» при незаданном ключе —
    обещание невыполнимого: покупать не на что. Раньше это выяснялось
    только на живом оплаченном заказе, то есть в самый неподходящий момент
    и уже при ждущем покупателе.

    Выключается тумблер всегда: запрещать выключение нельзя ни по какой
    причине.
    """
    from storage import get_ar_creds

    uid = callback.from_user.id
    settings = get_settings(uid)
    p = settings["plugins"]["auto_roblox"]
    want = not p.get("enabled", False)
    if want:
        creds = get_ar_creds(uid)
        if not creds or not creds.get("api_key"):
            await callback.answer(
                "Сначала ключ AppRoute: без него покупать не на что — "
                "кнопка «🔑 Поставщик AppRoute» выше", show_alert=True)
            return
    p["enabled"] = want
    save_settings(uid, settings)
    await callback.message.edit_text(_roblox_text(settings, get_shop_name(uid)), reply_markup=_roblox_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:settings")
async def roblox_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_roblox_settings_text(settings),
                                     reply_markup=_roblox_settings_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data.startswith("plugins:roblox:region:"))
async def roblox_region(callback: CallbackQuery) -> None:
    """Регион кода. Глобальный и российский не взаимозаменяемы: код не того
    региона покупатель активировать не сможет, и узнаем мы об этом от него."""
    want = callback.data.split(":")[-1].upper()
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["region"] = "RU" if want == "RU" else "GL"
    save_settings(uid, settings)
    await callback.message.edit_text(_roblox_settings_text(settings),
                                     reply_markup=_roblox_settings_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:set_keyword")
async def roblox_set_keyword_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_set_keyword)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_roblox"].get("keyword") or "—"
    await callback.message.edit_text(
        f"🔤 <b>Слово-опознаватель</b>\n\nСейчас: <code>{html.escape(cur)}</code>\n\n"
        f"Без него заказ узнаётся по словам «robux», «робукс», «roblox». "
        f"Это удобно, но если у вас продаются ещё и аккаунты Roblox, они "
        f"попадут в ту же выдачу.\n\n"
        f"Пришлите своё слово — тогда в работу пойдут <b>только</b> заказы "
        f"с ним. Точка «.» очистит настройку.",
        reply_markup=_cancel_kb("plugins:roblox:settings"),
    )
    await callback.answer()


@router.message(PluginState.roblox_set_keyword)
async def roblox_set_keyword_input(message: Message, state: FSMContext) -> None:
    word = (message.text or "").strip()
    if word == ".":
        word = ""
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["keyword"] = word
    save_settings(uid, settings)
    await state.clear()
    said = (f"✅ Слово-опознаватель: <code>{html.escape(word)}</code>" if word
            else "✅ Слово убрано — узнаём заказ по обычным написаниям.")
    await message.answer(said, reply_markup=_roblox_settings_keyboard(settings))


@router.callback_query(F.data == "plugins:roblox:manual")
async def roblox_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Ручная выдача. Спрашивается номер заказа, а не покупатель.

    Код уходит в чат заказа — единственное место, куда его вообще можно
    отправить. Прежняя версия спрашивала @username: понятие взято у звёзд,
    где выдача идёт на аккаунт Telegram.
    """
    await state.set_state(PluginState.roblox_manual_order)
    await callback.message.edit_text(
        "🚀 <b>Выдать Robux вручную</b>\n\n"
        "Пришлите <b>номер заказа</b> — бот подберёт номинал, купит код и "
        "отправит его в чат этого заказа.\n\n"
        "Тумблер и слово-опознаватель при этом не проверяются: раз вы "
        "назвали заказ сами, значит он ваш. А вот оплату бот проверит — "
        "по неоплаченному выдавать нельзя.\n\n"
        "Покупка пойдёт тем же путём, что и автоматическая: сухой прогон, "
        "покупка, запись кода, отправка. Отчёт придёт сюда.",
        reply_markup=_cancel_kb("plugins:auto_roblox"))
    await callback.answer()


@router.message(PluginState.roblox_manual_order)
async def roblox_manual_order_input(message: Message, state: FSMContext) -> None:
    order_id = "".join(ch for ch in (message.text or "") if ch.isdigit())
    await state.clear()
    if not order_id:
        await message.answer("❌ В сообщении не видно номера заказа.")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    p = settings["plugins"]["auto_roblox"]
    if order_id in (p.get("delivered") or []):
        # Второй раз — это вторая покупка. Молча пропустить нельзя: продавец
        # решит, что выдача идёт.
        await message.answer(
            f"⚠️ Заказ <b>{order_id}</b> уже выдан — второй раз бот покупать "
            f"не станет.\nЕсли код потерялся, он есть в «📜 Журнал выдач».",
            reply_markup=_roblox_keyboard(settings))
        return
    from storage import get_ar_creds
    creds = get_ar_creds(uid)
    if not creds or not creds.get("api_key"):
        # Поставить в очередь можно, купить — нет. Отвечать «куплю на
        # ближайшем проходе», зная, что покупать не на что, значит послать
        # продавца ждать отчёт, который придёт отказом.
        await message.answer(
            "❌ Ключ AppRoute не задан — покупать не на что.\n\n"
            "Задайте его кнопкой «🔑 Поставщик AppRoute» и поставьте заказ "
            "в очередь снова.",
            reply_markup=_roblox_keyboard(settings))
        return
    forced: list = p.setdefault("force", [])
    if order_id not in forced:
        forced.append(order_id)
    save_settings(uid, settings)
    await message.answer(
        f"✅ Заказ <b>{order_id}</b> поставлен на выдачу.\n\n"
        f"Куплю на ближайшем проходе — это меньше минуты. Отчёт придёт "
        f"отдельным сообщением: и если получится, и если нет.",
        reply_markup=_roblox_keyboard(settings))


@router.callback_query(F.data == "plugins:roblox:balance")
async def roblox_balance(callback: CallbackQuery) -> None:
    """Баланс кабинета и что на него можно выдать.

    Отдельный экран, а не общий `/apr_balance`, ради последней строки: «не
    ноль» и «хватит» — разные вещи. При 1.43 $ на счету и самом дешёвом
    номинале в 2.74 $ прежний экран молчал, а первая же выдача отвалилась бы
    с «не хватает средств», причём по каждому оплаченному заказу отдельно.
    """
    from automation.approute import balance_sync
    from automation.robux import affordable
    from handlers.approute import _accounts, _balance_text
    from storage import get_ar_creds

    uid = callback.from_user.id
    settings = get_settings(uid)
    creds = get_ar_creds(uid)
    if not creds or not creds.get("api_key"):
        await callback.answer("Ключ AppRoute не задан", show_alert=True)
        return
    await callback.answer("⏳ Спрашиваю кабинет…")
    await callback.message.edit_text("⏳ Спрашиваю баланс у поставщика…")
    ok, text = await _balance_text(creds)

    lines = [text]
    if ok:
        loop = asyncio.get_event_loop()
        # Сумма берётся из данных, а не вычитывается из собственного отчёта:
        # разбор своей же прозы вместо структуры в этом проекте уже отвечал
        # «есть ли деньги» наугад.
        try:
            money_ok, money = await asyncio.wait_for(
                loop.run_in_executor(None, balance_sync, creds), timeout=60)
        except Exception:
            money_ok, money = False, None
        usd = 0.0
        if money_ok:
            for acc in _accounts(money):
                if str(acc.get("currency") or "").upper() == "USD":
                    usd = float(acc.get("amount") or 0)
                    break
        got, catalog, _age = await _read_catalog(creds)
        if got:
            can = affordable(catalog, usd)
            lines.append("")
            if can["cheapest"] <= 0:
                lines.append("У поставщика не нашлось ни одного номинала в "
                             "наличии — считать не на чем.")
            elif can["count"] <= 0:
                lines.append(
                    f"⚠️ <b>Выдать нельзя ни одного кода.</b> Самый дешёвый "
                    f"сейчас — {html.escape(can['cheapest_name'])} "
                    f"({can['cheapest_region']}) за ${can['cheapest']:.2f}, а "
                    f"на счету меньше. Первый же оплаченный заказ отвалится "
                    f"с «не хватает средств».")
            else:
                lines.append(
                    f"Хватит на <b>{can['count']}</b> шт. самого дешёвого — "
                    f"{html.escape(can['cheapest_name'])} "
                    f"({can['cheapest_region']}) по ${can['cheapest']:.2f}.")
                lines.append(f"По карману номиналов: <b>{can['total']}</b> "
                             f"из тех, что в наличии.")
        else:
            lines.append("")
            lines.append("<i>Каталог сейчас не прочитан — сколько это кодов, "
                         "сказать не могу. У поставщика лимит: два таких "
                         "запроса в минуту.</i>")
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Доступ к поставщику", callback_data="apr:creds")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=b.as_markup())


@router.callback_query(F.data == "plugins:roblox:log")
async def roblox_log(callback: CallbackQuery) -> None:
    """Журнал выдач — здесь же лежат коды, не ушедшие в закрытый чат."""
    p = get_settings(callback.from_user.id)["plugins"]["auto_roblox"]
    rows = p.get("log") or []
    if not rows:
        await callback.answer("Выдач ещё не было", show_alert=True)
        return
    lines = ["📜 <b>Журнал выдач Robux</b>", ""]
    for e in rows[:15]:
        head = f"#{e.get('order')} · {e.get('robux') or '—'} Robux · {e.get('state')}"
        lines.append(f"<b>{html.escape(head)}</b>")
        if e.get("codes"):
            lines.append("   код: " + ", ".join(
                f"<code>{html.escape(str(c))}</code>" for c in e["codes"]))
        if e.get("why"):
            lines.append(f"   {html.escape(str(e['why']))}")
        lines.append("")
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:make_ads")
async def roblox_make_ads_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор номинала, с которого начнётся обычное создание объявления.

    Своего создания товаров здесь нет и не нужно: мастер уже написан и
    умеет то, чего каталог не знает — категории панели, обязательные поля,
    фото, публикацию. Плагин только подставляет в него название, количество
    и описание того номинала, который бот действительно умеет выдать.

    Смысл именно в этом: витрина и каталог обязаны сойтись. Объявление на
    500 Robux автовыдаче недоступно — такого номинала у поставщика нет, а
    подменять ближайшим бот не станет.
    """
    from automation.robux import catalog_regions
    from storage import get_ar_creds

    await state.clear()  # Cancel с шага цены ведёт сюда — закрываем форму
    uid = callback.from_user.id
    creds = get_ar_creds(uid)
    if not creds or not creds.get("api_key"):
        await callback.answer("Сначала ключ AppRoute — без него каталога нет",
                              show_alert=True)
        return
    await callback.answer("⏳ Читаю каталог…")
    await callback.message.edit_text("⏳ Смотрю, какие номиналы есть у поставщика…")

    ok, catalog, age = await _read_catalog(creds)
    settings = get_settings(uid)
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_roblox_keyboard(settings))
        return

    # Регион спрашивается ПЕРВЫМ, а не берётся из настройки: у Юмаркета
    # выбора региона нет вовсе, и единственное место, где он переживёт
    # создание товара, — описание. Значит знать его надо до того, как
    # описание собрано.
    regions = [r for r in catalog_regions(catalog) if r["in_stock"] > 0]
    if not regions:
        await callback.message.edit_text(
            "❌ У поставщика не нашлось ни одного региона Roblox с ненулевым "
            "остатком.", reply_markup=_roblox_keyboard(settings))
        return
    await state.update_data(ar_catalog_at=time.time())

    b = InlineKeyboardBuilder()
    for r in regions:
        mark = "💠" if r["kind"] == "wallet" else "💳"
        b.button(text=f"{mark} {r['region']} · {r['in_stock']}",
                 callback_data=f"plugins:roblox:reg:{r['region']}")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(3)
    wallet = [r["region"] for r in regions if r["kind"] == "wallet"]
    lines = [
        "🌐 <b>Регион кода</b>" + (f" · <i>{age}</i>" if age else ""), "",
        "У поставщика их несколько, и это <b>два разных товара</b>:", "",
        f"💠 <b>{', '.join(wallet) or '—'}</b> — кошельковые коды, номинал "
        f"прямо в Robux.",
        "💳 <b>остальные</b> — подарочные карты страны, номинал в её валюте "
        "($10, EUR 25). Сколько Robux даст такая карта, решает курс самого "
        "Roblox — бот его не знает и выдавать по нему не станет.",
        "",
        "Рядом с регионом — сколько номиналов в наличии.",
        "",
        "<i>Регион уйдёт в описание товара отдельной строкой: у Юмаркета "
        "оттуда его читает выдача. Своё поле «Регион» у панели тоже есть, "
        "и мастер спросит его, если сам не подберёт.</i>",
    ]
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("plugins:roblox:reg:"))
async def roblox_region_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """Регион выбран — показываем номиналы именно этого региона."""
    from automation.robux import denominations, is_wallet_region
    from storage import get_ar_creds

    region = callback.data.split(":")[-1].strip().upper()
    uid = callback.from_user.id
    settings = get_settings(uid)
    creds = get_ar_creds(uid)
    await callback.answer("⏳ Читаю номиналы…")
    ok, catalog, age = await _read_catalog(creds)
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_roblox_keyboard(settings))
        return

    rows = [r for r in denominations(catalog, region) if r["in_stock"] > 0]
    if not rows:
        await callback.message.edit_text(
            f"❌ В регионе {region} нет номиналов с ненулевым остатком.",
            reply_markup=_roblox_keyboard(settings))
        return
    wallet = is_wallet_region(region)
    await state.update_data(ar_region=region, ar_rows=[
        {"id": r["denomination_id"], "robux": r["robux"], "face": r["face"],
         "cur": r["face_currency"], "name": r["name"], "price": r["price"]}
        for r in rows])

    b = InlineKeyboardBuilder()
    for r in rows:
        # `denominationId` — UUID в 36 символов, вместе с префиксом это 55
        # байт из 64 разрешённых. Влезает, и выбор получается однозначным:
        # у карт одинаковый номинал бывает в разных валютах.
        title = (f"{r['robux']} Robux" if wallet
                 else f"{r['face']:g} {r['face_currency']}")
        b.button(text=f"{title} · ${r['price']:.2f}",
                 callback_data=f"plugins:roblox:den:{r['denomination_id']}")
    b.button(text="⬅️ Другой регион", callback_data="plugins:roblox:make_ads")
    b.adjust(2)
    what = ("кошельковые коды — номинал в Robux" if wallet
            else "подарочные карты — номинал в валюте страны")
    lines = [f"🏷 <b>Номиналы · регион {region}</b>"
             + (f" · <i>{age}</i>" if age else ""), "", f"Это {what}.", "",
             "Цены рядом — <b>закупочные, в долларах</b>. Свою цену в рублях "
             "назначите на следующем шаге: курс боту брать неоткуда, и "
             "выдумывать его он не станет."]
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("plugins:roblox:den:"))
async def roblox_den_pick(callback: CallbackQuery, state: FSMContext) -> None:
    den_id = callback.data.split(":", 3)[-1].strip()
    data = await state.get_data()
    rows = data.get("ar_rows") or []
    row = next((r for r in rows if r.get("id") == den_id), None)
    if not row:
        # Список номиналов живёт в состоянии формы, и оно теряется при
        # перезапуске бота. Молча промолчать нельзя: продавец нажал кнопку и
        # ждёт ответа.
        await callback.answer("Список устарел — откройте регионы заново",
                              show_alert=True)
        return
    await state.set_state(PluginState.roblox_ads_rate)
    await state.update_data(ar_den=row)
    title = (f"{row['robux']} Robux" if row.get("robux")
             else f"{row['face']:g} {row['cur']}")
    await callback.message.edit_text(
        f"🏷 <b>{html.escape(title)}</b> · регион "
        f"{html.escape(str(data.get('ar_region') or ''))}\n\n"
        f"Закупка: ${row['price']:.2f}\n\n"
        f"Назовите цену в рублях, за которую продаёте — одним числом.\n\n"
        f"Дальше откроется обычное создание объявления: название и описание "
        f"подставлю по вашим заготовкам, а количество, фото и категорию "
        f"мастер спросит сам.",
        reply_markup=_cancel_kb("plugins:roblox:make_ads"))
    await callback.answer()


@router.message(PluginState.roblox_ads_rate)
async def roblox_den_price(message: Message, state: FSMContext) -> None:
    """Цена принята — передаём управление обычному мастеру объявления.

    Управление отдаётся на шаге количества, а не на предпросмотре. Разница
    не косметическая: изменить количество с предпросмотра нельзя — там есть
    правка названия, цены, описания и фото, а количества нет. Товар,
    заведённый отсюда с зашитой единицей, продался бы один раз и пропал с
    витрины, и продавец узнал бы об этом по исчезнувшему объявлению.

    Заодно мастер сам спросит фото и сам предупредит, что панель без
    картинки объявление может не принять.
    """
    data = await state.get_data()
    den = data.get("ar_den") or {}
    region = str(data.get("ar_region") or "").upper()
    digits = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not digits or not den:
        await message.answer("❌ Нужно одно число — цена в рублях.")
        return
    price = int(digits)
    await state.clear()

    # Заготовки продавца. Пустые — берутся наши; регион дописывается
    # отдельной строкой в любом случае, потому что читать его потом будет
    # выдача — она смотрит в описание, а не в панель. Своё поле «Регион»
    # (`filter__8`) у панели есть и обязательно, но это её собственное
    # требование к карточке, а не источник для выдачи.
    from automation.robux import (DEFAULT_AD_TEXT, DEFAULT_AD_TITLE,
                                  fill_template, with_region)
    # Через `.get`, а не по ключу: у продавца, чьи настройки записаны до
    # появления заготовок, раздела может не быть вовсе, и создание товара
    # упало бы на ровном месте — с непонятным ему KeyError.
    p = ((get_settings(message.from_user.id) or {}).get("plugins") or {}
         ).get("auto_roblox") or {}
    nominal = (f"{den['robux']} Robux" if den.get("robux")
               else f"{den['face']:g} {den['cur']}")
    filled = functools.partial(
        fill_template, robux=int(den.get("robux") or 0), nominal=nominal,
        region=region, price=price)
    ad_title = filled(p.get("ad_title") or DEFAULT_AD_TITLE)[:100]
    ad_text = with_region(filled(p.get("ad_text") or DEFAULT_AD_TEXT), region)

    from handlers.create_ad import CreateAdState
    await state.set_state(CreateAdState.quantity)
    await state.update_data(
        title=ad_title,
        price=price,
        # Раздел витрины здесь известен заранее — товар заводится из плагина
        # Robux, и выбирать «Roblox» руками по каждому номиналу продавцу
        # незачем. Слова от узкого к широкому: «robux» отличает валюту от
        # аккаунтов и подарочных карт, «roblox» подходит, когда отдельного
        # раздела под валюту в панели нет. Подойдёт под слово ровно один
        # вариант — мастер возьмёт его сам и скажет об этом; несколько или
        # ни одного — спросит, как раньше.
        #
        # «Игровая валюта» стоит в середине не для красоты: подкатегории с
        # именем «Робуксы» в панели **нет**. Живой список под категорией
        # Roblox, снятый 19.08, — Аккаунты, Игровая валюта, Буст, Скины,
        # Услуги, Roblox Studio, Другое, Аренда, Обучение, Подписки, Кланы,
        # Roblox Plus. Слова «robux» там не встречается вовсе, а «roblox»
        # совпадает сразу с двумя (Studio и Plus), то есть подкатегория без
        # этого слова не подбиралась вообще. На категорию оно не влияет:
        # среди названий игр «игровой валюты» нет, и там срабатывает
        # «roblox».
        autopick=["robux", "игровая валюта", "roblox"],

        # Без домена: панель запрещает ссылки в описании, кроме своего
        # белого списка, и «roblox.com» роняло создание товара отказом 422 на
        # последнем шаге — когда продавец уже всё ввёл. Проверено живьём
        # 19.08. Куда идти, покупатель прочитает в самом коде выдачи.
        description=ad_text,
    )
    b = InlineKeyboardBuilder()
    b.button(text="1️⃣ Пропустить (кол-во = 1)", callback_data="create_ad:qty:1")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    await message.answer(
        f"✅ Цена: <b>{price} ₽</b> за <b>{html.escape(nominal)}</b> "
        f"({html.escape(region)})\n\n"
        f"Название: <code>{html.escape(ad_title)}</code>\n"
        f"Регион уйдёт в описание отдельной строкой — оттуда его потом "
        f"читает выдача.\n\n"
        f"Сколько таких кодов выставляем? Введите число или пропустите — "
        f"но тогда объявление продастся один раз и уйдёт с витрины.",
        reply_markup=b.as_markup())


def _template_help(what: str, now: str, default: str) -> str:
    """Экран заготовки. Подстановки перечислены те, что и правда работают."""
    from automation.robux import TEMPLATE_FIELDS
    lines = [f"🏷 <b>{what} для новых товаров</b>", ""]
    if now:
        lines.append(f"Сейчас: <code>{html.escape(now)}</code>")
    else:
        lines.append(f"Сейчас наша: <code>{html.escape(default)}</code>")
    lines += ["", "Можно подставить:"]
    for mark, about in TEMPLATE_FIELDS.items():
        lines.append(f"   <code>{html.escape(mark)}</code> — {about}")
    lines += ["",
              "Пришлите свой вариант одним сообщением. Точка «.» вернёт наш.",
              "",
              "<i>Регион бот допишет отдельной строкой сам — у Юмаркета "
              "выбора региона нет, и из описания его потом читает выдача.</i>"]
    return "\n".join(lines)


@router.callback_query(F.data == "plugins:roblox:set_ad_title")
async def roblox_set_ad_title_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    from automation.robux import DEFAULT_AD_TITLE
    await state.set_state(PluginState.roblox_set_ad_title)
    p = get_settings(callback.from_user.id)["plugins"]["auto_roblox"]
    await callback.message.edit_text(
        _template_help("Название", p.get("ad_title") or "", DEFAULT_AD_TITLE),
        reply_markup=_cancel_kb("plugins:roblox:settings"))
    await callback.answer()


@router.message(PluginState.roblox_set_ad_title)
async def roblox_set_ad_title_input(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value == ".":
        value = ""
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["ad_title"] = value[:100]
    save_settings(uid, settings)
    await state.clear()
    said = (f"✅ Название: <code>{html.escape(value[:100])}</code>" if value
            else "✅ Вернул нашу заготовку названия.")
    await message.answer(said, reply_markup=_roblox_settings_keyboard(settings))


@router.callback_query(F.data == "plugins:roblox:set_ad_text")
async def roblox_set_ad_text_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    from automation.robux import DEFAULT_AD_TEXT
    await state.set_state(PluginState.roblox_set_ad_text)
    p = get_settings(callback.from_user.id)["plugins"]["auto_roblox"]
    await callback.message.edit_text(
        _template_help("Описание", p.get("ad_text") or "", DEFAULT_AD_TEXT),
        reply_markup=_cancel_kb("plugins:roblox:settings"))
    await callback.answer()


@router.message(PluginState.roblox_set_ad_text)
async def roblox_set_ad_text_input(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value == ".":
        value = ""
    uid = message.from_user.id
    settings = get_settings(uid)
    # Ссылки панель не принимает, и отказ приходит 422 на последнем шаге
    # мастера — когда введено уже всё. Сказать здесь дешевле.
    if value:
        from automation.panel import link_trouble
        found = link_trouble(value)
        if found:
            await message.answer(
                f"❌ Панель не примет описание со ссылкой: "
                f"<code>{html.escape(found)}</code>\n\n"
                f"Уберите ссылку и пришлите заготовку ещё раз.")
            return
    settings["plugins"]["auto_roblox"]["ad_text"] = value
    save_settings(uid, settings)
    await state.clear()
    said = ("✅ Описание сохранено." if value
            else "✅ Вернул наше описание.")
    await message.answer(said, reply_markup=_roblox_settings_keyboard(settings))


@router.callback_query(F.data == "plugins:roblox:set_note")
async def roblox_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_set_note)
    await callback.message.edit_text("📝 Введите заметку для покупателя:", reply_markup=_cancel_kb("plugins:roblox:settings"))
    await callback.answer()


@router.message(PluginState.roblox_set_note)
async def roblox_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка: <i>{html.escape(note) or '—'}</i>",
                         reply_markup=_roblox_settings_keyboard(settings))


# ---------------------------------------------------------------------------
# Гифт-карты: один набор экранов на все карты реестра
#
# «Одинаковые менюшки и т.д, просто ид покупки меняется» — поэтому здесь нет
# ни одного экрана, написанного под конкретную карту. Всё, что отличает Apple
# от Xbox, лежит в декларации (`automation/giftcards.py`), а экран собирается
# из неё.
#
# `callback_data` — `plugins:gc:<slug>:<действие>`, и это не случайная форма:
# у Telegram на неё **64 байта**, а карт может быть двадцать пять. Регион в
# callback не кладётся вовсе — их бывает 63 у одной карты, для них отдельный
# ввод.
#
# Раньше на этом месте была заглушка **другого товара** — «Telegram —
# Подарки», с полями «Тип подарка» и «Заметка» и пятью кнопками, честно
# отвечавшими «функция появится в следующем обновлении». Обещание, которого
# бот не сдержит, убрано вместе с ними: понадобятся телеграм-подарки — это
# отдельный плагин с другим поставщиком, у них с картами общего ничего.
# ---------------------------------------------------------------------------


def _gift_cards_text(settings: dict, shop_name: str = "") -> str:
    """Список карт как витрина: включённые с их счётом, остальные — числом.

    Прежний экран заканчивался строкой «на живом заказе выдача ещё не
    проверялась». Она была правдой ровно до первой выдачи, а потом
    превратилась в неправду, которую продавец видел каждый день. Теперь
    сказано то, что есть в его журналах: сколько кодов ушло по каждой карте.
    """
    from automation.giftcards import ago, card_conf, cards, stats

    facts = stats(settings)
    name_part = f" · {html.escape(shop_name)}" if shop_name else ""
    lines = [f"🎁 <b>Гифт-карты{name_part}</b>", ""]
    on = [c for c in cards() if card_conf(settings, c.slug).get("enabled")]
    if on:
        for gift in on:
            conf = card_conf(settings, gift.slug)
            mine = facts["per_card"].get(gift.slug) or {}
            region = str(conf.get("region") or "").upper() or "из описания"
            done = mine.get("delivered") or 0
            when = ago(mine.get("last_at") or 0)
            tail = (f" · выдано {done}" + (f", {when}" if when else "")
                    if done else " · выдач пока нет")
            lines.append(f"{gift.emoji} <b>{html.escape(gift.title)}</b> — "
                         f"{html.escape(region)}{tail}")
    else:
        lines.append("Пока не включена ни одна карта — а включается она в "
                     "одно нажатие.")
    off = [c for c in cards() if c not in on]
    if off:
        names = ", ".join(html.escape(c.title) for c in off[:6])
        more = " и другие" if len(off) > 6 else ""
        lines += ["", f"Доступно ещё <b>{len(off)}</b>: {names}{more}."]
    lines += [
        "",
        "Покупатель платит — бот сам покупает код у поставщика и присылает "
        "его в чат заказа. Ник и логин не нужны: код покупатель активирует "
        "сам.",
    ]
    return "\n".join(lines)


def _gift_cards_keyboard(settings: dict) -> InlineKeyboardMarkup:
    from automation.giftcards import card_conf, cards

    builder = InlineKeyboardBuilder()
    on = [c for c in cards() if card_conf(settings, c.slug).get("enabled")]
    for gift in on:
        builder.button(text=f"{gift.emoji} {gift.title}",
                       callback_data=f"plugins:gc:{gift.slug}")
    if len(on) < len(cards()):
        builder.button(text="➕ Добавить карту", callback_data="plugins:gifts:add")
    builder.button(text="🧪 Проверка на моих товарах",
                   callback_data="plugins:gifts:dryrun")
    builder.button(text="🔬 Что ещё можно завести",
                   callback_data="plugins:gifts:survey")
    builder.button(text="🔑 Поставщик AppRoute", callback_data="apr:creds")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    # Карты — по две в ряд: названия короткие, а тринадцать штук по одной
    # превращали экран в два экрана. Всё, что ниже, — по одной: у этих
    # кнопок подписи длинные, и вторая в ряд обрезалась бы многоточием.
    cards_rows = [2] * ((len(on) + 1) // 2)
    builder.adjust(*(cards_rows + [1, 1, 1, 1, 1]))
    return builder.as_markup()


def _gift_card_text(settings: dict, gift) -> str:
    from automation.giftcards import ago, card_conf

    conf = card_conf(settings, gift.slug)
    enabled = conf.get("enabled")
    region = str(conf.get("region") or "").upper()
    keyword = str(conf.get("keyword") or "")
    note = str(conf.get("note") or "")
    log = conf.get("log") or []
    delivered = conf.get("delivered") or []
    lines = [
        f"{gift.emoji} <b>{html.escape(gift.title)}</b>",
        "",
        ("🟢 <b>Автовыдача включена</b> — бот сам купит код по оплаченному "
         "заказу" if enabled
         else "🔴 Автовыдача выключена — заказы выдаёте вручную"),
    ]
    # Счёт выдач стоял в самом низу, после всех настроек. Это главный ответ
    # экрана — работает ли оно и когда сработало в последний раз, — и место
    # ему рядом со статусом. Остальной текст продавец одобрил, и он не
    # тронут.
    if delivered:
        when = ago(max((float(e.get("at") or 0) for e in log
                        if isinstance(e, dict)), default=0.0))
        lines.append(f"⚡ Выдано кодов: <b>{len(delivered)}</b>"
                     + (f" · последний {when}" if when else ""))
    failed = sum(1 for e in log
                 if isinstance(e, dict) and str(e.get("state")) == "не выдан")
    if failed:
        # Рядом с «выдано 12» три несостоявшихся молчать не могут: это тот
        # самый бодрый отчёт, от которого здесь уходят.
        lines.append(f"⚠️ Не выдано: <b>{failed}</b> — причина у каждого "
                     f"в журнале")
    lines.append("")
    # Разница между «мерено» и «похоже, что разберётся» — это разница между
    # проверкой и надеждой, и продавец вправе её видеть. У меренных карт все
    # названия семейства разобрались на живом каталоге; у остальных
    # основание слабее: каталог показывает их номиналы деньгами, и только.
    if not getattr(gift, "measured", ""):
        lines += [
            "⚠️ <b>Разбор номинала у этой карты не мерян на живом каталоге.</b>",
            "   Семейство выбрано по тому, что номиналы у него в деньгах. "
            "Померить — «🔬 Что ещё можно завести» в меню гифт-карт: один "
            "запрос, покажет долю разобранных названий.",
            "",
        ]
    lines += [
        f"🌐 Регион: <b>{html.escape(region) if region else 'из описания товара'}</b>",
    ]
    if not region:
        lines.append("   Бот читает строку «Регион кода: XX» из описания "
                     "товара — выдача смотрит туда, а не в панель.")
    if keyword:
        lines.append(f"🔤 Слово-опознаватель: <code>{html.escape(keyword)}</code>"
                     f" — в работу пойдут только заказы с ним")
    else:
        words = ", ".join(gift.words[:4])
        lines.append(f"🔤 Слово-опознаватель: <i>не задано</i> — узнаём заказ "
                     f"по словам: {html.escape(words)}")
        lines.append("   ⚠️ Если у вас есть товары вида «аккаунт» с тем же "
                     "словом, задайте своё: иначе они тоже попадут в выдачу.")
    if note:
        lines.append(f"📝 Заметка покупателю: <i>{html.escape(note)}</i>")
    if log:
        lines += ["", f"📜 Записей в журнале: <b>{len(log)}</b> — там коды, "
                      f"номиналы и причины отказов"]
    return "\n".join(lines)


def _gift_card_keyboard(settings: dict, gift) -> InlineKeyboardMarkup:
    from automation.giftcards import card_conf

    enabled = card_conf(settings, gift.slug).get("enabled")
    slug = gift.slug
    builder = InlineKeyboardBuilder()
    builder.button(text="⏸ Выключить" if enabled else "▶️ Включить",
                   callback_data=f"plugins:gc:{slug}:toggle")
    builder.button(text="⚙️ Настройки", callback_data=f"plugins:gc:{slug}:cfg")
    builder.button(text="🚀 Выдать вручную",
                   callback_data=f"plugins:gc:{slug}:manual")
    builder.button(text="📜 Журнал выдач", callback_data=f"plugins:gc:{slug}:log")
    builder.button(text="📦 Наличие и баланс",
                   callback_data=f"plugins:gc:{slug}:stock")
    # Создание товара — здесь же. Витрина и каталог обязаны сойтись:
    # объявление на номинал, которого у поставщика нет, автовыдаче
    # недоступно, а узнал бы об этом продавец из отказа, когда покупатель
    # уже заплатил.
    builder.button(text="🏷 Создать товар",
                   callback_data=f"plugins:gc:{slug}:make")
    builder.button(text="⬅️ Гифт-карты", callback_data="plugins:gifts")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def _gift_cfg_keyboard(gift) -> InlineKeyboardMarkup:
    slug = gift.slug
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Регион", callback_data=f"plugins:gc:{slug}:region")
    builder.button(text="🔤 Слово-опознаватель",
                   callback_data=f"plugins:gc:{slug}:kw")
    builder.button(text="💬 Автоответ", callback_data=f"plugins:gc:{slug}:greet")
    builder.button(text="📝 Заметка", callback_data=f"plugins:gc:{slug}:note")
    builder.button(text="🏷 Название товара",
                   callback_data=f"plugins:gc:{slug}:adtitle")
    builder.button(text="📄 Описание товара",
                   callback_data=f"plugins:gc:{slug}:adtext")
    builder.button(text="⬅️ Назад", callback_data=f"plugins:gc:{slug}")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def _gift_cfg_text(settings: dict, gift) -> str:
    from automation.giftcards import card_conf

    conf = card_conf(settings, gift.slug)
    title = str(conf.get("ad_title") or "") or gift.ad_title
    text = str(conf.get("ad_text") or "") or gift.ad_text
    own_title = " <i>(наша)</i>" if not conf.get("ad_title") else ""
    own_text = " <i>(наше)</i>" if not conf.get("ad_text") else ""
    return "\n".join([
        f"⚙️ <b>Настройки — {html.escape(gift.title)}</b>",
        "",
        f"🌐 Регион: <b>{html.escape(str(conf.get('region') or '') or '—')}</b>",
        f"🔤 Слово в названии: "
        f"<code>{html.escape(str(conf.get('keyword') or '—'))}</code>",
        f"💬 Автоответ до кода: "
        f"<i>{html.escape(str(conf.get('greeting') or 'молчим'))}</i>",
        f"📝 Заметка к коду: <i>{html.escape(str(conf.get('note') or '—'))}</i>",
        "",
        "🏷 <b>Заготовки для новых товаров</b>",
        f"   Название: <code>{html.escape(title)}</code>{own_title}",
        f"   Описание: <code>{html.escape(text.split(chr(10))[0][:60])}…</code>"
        f"{own_text}",
    ])


def _gift_or_none(data: str):
    """Карта из `callback_data` вида `plugins:gc:<slug>[:действие]`."""
    from automation.giftcards import card

    parts = str(data or "").split(":")
    return card(parts[2]) if len(parts) > 2 else None


@router.callback_query(F.data == "plugins:gifts")
async def gifts_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(
        _gift_cards_text(settings, get_shop_name(uid)),
        reply_markup=_gift_cards_keyboard(settings))
    await callback.answer()


def _survey_lines(rows: list[dict]) -> list[str]:
    """Замер каталога словами. Отдельно от экрана — чтобы проверять текст,
    а не разметку вокруг него."""
    lines = ["🔬 <b>Разделы поставщика</b>", ""]
    ready = [r for r in rows if r["ready"]]
    if ready:
        lines.append("<b>Готовы к заведению</b> — номинал разбирается у всех "
                     "услуг до одной:")
        for r in ready[:8]:
            lines.append(
                f"✅ <code>{html.escape(r['subcategory'])}</code>\n"
                f"   мера: {html.escape(r['measure'])} · в наличии "
                f"{r['in_stock']} из {r['nominals']} · от "
                f"<b>{r['cheapest']:.2f} $</b>")
        lines.append("")
    else:
        lines += ["Ни одного нового раздела с полным разбором номинала.", ""]

    known = [r for r in rows if r["card"]]
    if known:
        lines.append("<b>Уже заведены:</b> " + ", ".join(
            html.escape(r["card"]) for r in known[:12]))
        lines.append("")

    weak = [r for r in rows if not r["ready"] and not r["card"]][:6]
    if weak:
        lines.append("<b>Заводить нельзя</b> — номинал понят не везде, а "
                     "непонятый номинал это оплаченный заказ без выдачи:")
        for r in weak:
            lines.append(
                f"⚠️ <code>{html.escape(r['subcategory'])}</code> — "
                f"{round(r['share'] * 100)} % по мере "
                f"«{html.escape(r['measure'] or 'не подобрана')}», услуг "
                f"{r['services']}")
    return lines


def _dry_run_lines(rows: list[dict]) -> list[str]:
    """Сухой прогон словами. Отдельно от экрана — чтобы проверять текст."""
    good = [r for r in rows if r["regions"]]
    bad = [r for r in rows if not r["regions"]]
    lines = ["🧪 <b>Проверка на ваших товарах</b>", ""]
    lines.append(f"Узнаю и найду у поставщика: <b>{len(good)}</b> из "
                 f"<b>{len(rows)}</b>")
    lines.append("")
    if bad:
        lines.append("<b>Эти выдать не смогу:</b>")
        for r in bad[:12]:
            lines.append(f"❌ {html.escape(r['title'][:60])}\n"
                         f"   {html.escape(r['why'])}"
                         + (f" (карта: {html.escape(r['card'])})"
                            if r["card"] else ""))
        lines.append("")
    if good:
        lines.append("<b>Эти узнаю:</b>")
        for r in good[:12]:
            lines.append(
                f"✅ {html.escape(r['title'][:60])}\n"
                f"   {html.escape(r['card'])} · "
                f"{html.escape(r['nominal'])} · регионы: "
                f"{html.escape(', '.join(r['regions'][:6]))}")
    lines.append("")
    lines.append("<i>Регион товара здесь не проверяется: он лежит в описании "
                 "и читается при выдаче. Проверка отвечает «узнаю, и номинал "
                 "такой у поставщика есть» — не «выдам наверняка».</i>")
    return lines


@router.callback_query(F.data == "plugins:gifts:dryrun")
async def gifts_dry_run(callback: CallbackQuery, state: FSMContext) -> None:
    """Сухой прогон по витрине продавца: какие его товары бот узнаёт.

    Движок общий и проверен живой выдачей, а `words` и разбор номинала у
    каждой карты свои — и ошибка в них видна не отказом, а тишиной: заказ
    оплачен, выдача его не заметила. Проверять это покупкой по каждой карте
    дорого и долго; здесь то же самое читается с витрины, не потратив
    ничего.
    """
    from automation.giftcards import dry_run
    from automation.panel import panel_list_items_sync
    from storage import get_ar_creds, get_panel_creds

    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    creds = get_ar_creds(uid)
    panel = get_panel_creds(uid)
    if not creds or not creds.get("api_key"):
        await callback.answer("Сначала ключ AppRoute — без него каталога нет",
                              show_alert=True)
        return
    if not panel or not panel.get("cookies"):
        await callback.answer("Нужен вход в панель — оттуда берутся ваши товары",
                              show_alert=True)
        return

    await callback.answer("⏳ Смотрю ваши товары…")
    await callback.message.edit_text("⏳ Читаю витрину и каталог…")
    loop = asyncio.get_event_loop()
    try:
        ok_items, items = await asyncio.wait_for(
            loop.run_in_executor(None, panel_list_items_sync, panel["cookies"]),
            timeout=40)
    except Exception as e:
        ok_items, items = False, str(e)[:200]
    if not ok_items:
        await callback.message.edit_text(
            f"❌ Товары не прочитаны: {html.escape(str(items)[:300])}",
            reply_markup=_gift_cards_keyboard(settings))
        return

    ok, catalog, age = await _read_catalog(creds)
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_gift_cards_keyboard(settings))
        return

    titles = [str(i.get("title") or "") for i in (items or [])
              if isinstance(i, dict)]
    rows = dry_run(titles, catalog, settings)
    lines = _dry_run_lines(rows)
    if age:
        lines.append(f"<i>{html.escape(age)}</i>")
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=_gift_cards_keyboard(settings))


@router.callback_query(F.data == "plugins:gifts:survey")
async def gifts_survey(callback: CallbackQuery, state: FSMContext) -> None:
    """Замер каталога: какой раздел можно заводить картой, а какой нельзя.

    Карта заводится одной декларацией, и соблазн «объявить и посмотреть»
    велик. Цена ошибки не в коде: раздел, где номинал разбирается не у всех
    услуг, даёт товар, который бот не выдаст — и узнает об этом продавец
    после оплаты покупателем. Поэтому сначала замер, потом декларация.
    """
    from automation.giftcards import survey
    from storage import get_ar_creds

    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    creds = get_ar_creds(uid)
    if not creds or not creds.get("api_key"):
        await callback.answer("Сначала ключ AppRoute — без него каталога нет",
                              show_alert=True)
        return
    await callback.answer("⏳ Читаю каталог…")
    await callback.message.edit_text("⏳ Меряю разделы поставщика…")
    ok, catalog, age = await _read_catalog(creds)
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_gift_cards_keyboard(settings))
        return

    rows = survey(catalog)
    lines = _survey_lines(rows)
    if age:
        lines.append("")
        lines.append(f"<i>{html.escape(age)}</i>")
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=_gift_cards_keyboard(settings))


@router.callback_query(F.data == "plugins:gifts:add")
async def gifts_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Карты, которые продавец ещё не включил."""
    from automation.giftcards import card_conf, cards

    await state.clear()
    settings = get_settings(callback.from_user.id)
    builder = InlineKeyboardBuilder()
    for gift in cards():
        if not card_conf(settings, gift.slug).get("enabled"):
            builder.button(text=f"{gift.emoji} {gift.title}",
                           callback_data=f"plugins:gc:{gift.slug}")
    builder.button(text="⬅️ Назад", callback_data="plugins:gifts")
    builder.adjust(1)
    await callback.message.edit_text(
        "➕ <b>Добавить карту</b>\n\nВыберите товар — откроется его экран, "
        "там же включается автовыдача.",
        reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+$"))
async def gift_card_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_gift_card_text(settings, gift),
                                     reply_markup=_gift_card_keyboard(settings, gift))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:toggle$"))
async def gift_toggle(callback: CallbackQuery) -> None:
    """Включение автовыдачи. Без ключа поставщика она не включается.

    Тумблер, обещающий выдачу без ключа, — обещание, которого бот не
    сдержит: первый же оплаченный заказ отвалился бы молча.
    """
    from automation.giftcards import card_conf
    from storage import get_ar_creds

    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    uid = callback.from_user.id
    settings = get_settings(uid)
    conf = card_conf(settings, gift.slug)
    if not conf.get("enabled"):
        creds = get_ar_creds(uid) or {}
        if not creds.get("api_key"):
            await callback.answer(
                "Сначала задайте ключ AppRoute: 🔑 Поставщик AppRoute. "
                "Без него покупать не на что.", show_alert=True)
            return
    conf["enabled"] = not conf.get("enabled")
    save_settings(uid, settings)
    await callback.message.edit_text(_gift_card_text(settings, gift),
                                     reply_markup=_gift_card_keyboard(settings, gift))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:cfg$"))
async def gift_cfg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_gift_cfg_text(settings, gift),
                                     reply_markup=_gift_cfg_keyboard(gift))
    await callback.answer()


# Что спрашивается у продавца и куда кладётся. Одно состояние на все поля и
# все карты: состояний в этом боте уже девяносто три, и заводить по четыре
# на каждую из двадцати пяти карт значит утроить их число на ровном месте.
_GIFT_FIELDS = {
    "region": ("🌐 Введите регион кода — две буквы, как у поставщика "
               "(US, TR, AE…).\n\nМожно оставить пустым: тогда бот возьмёт "
               "регион из описания товара, где сам его и пишет.", "region"),
    # Название «слово-опознаватель» продавцы понимают по-разному: одни
    # думают, что это слово для покупателя, другие — что бот будет искать
    # его в описании. Поэтому объяснение начинается с того, ГДЕ бот смотрит,
    # и показывает пример на их же товаре.
    "kw": ("🔤 <b>Слово в названии товара</b>\n\n"
           "Бот смотрит на <b>название объявления на витрине</b> и решает, "
           "этой ли карте отдать заказ.\n\n"
           "Например, слово <code>эпл</code>:\n"
           "   ✅ «Эпл гифт карта 10$» — заберёт\n"
           "   ❌ «Apple Gift Card 10$» — пропустит, слова нет\n\n"
           "Пока слово не задано, бот узнаёт заказ по обычным написаниям "
           "названия карты. Задавать своё стоит, только если у вас есть "
           "другие товары с тем же словом — скажем, аккаунты, — и они "
           "попадают в выдачу по ошибке.\n\n"
           "Точка «.» очистит настройку.", "keyword"),
    "greet": ("💬 <b>Автоответ покупателю</b>\n\n"
              "Уходит в чат заказа сразу, как заказ взят в работу — "
              "<b>до кода</b>.\n\n"
              "Нужен не всегда: обычно код приходит через секунды, и "
              "«принял заказ» следом за ним выглядит лишним. Но у долгих "
              "номиналов поставщик отвечает «принято, код будет позже», и "
              "ожидание тянется минутами — вот там молчание похоже на "
              "поломку.\n\n"
              "Точка «.» очистит — тогда бот молчит до самого кода.",
              "greeting"),
    "note": ("📝 Введите заметку — она уйдёт покупателю вместе с кодом.",
             "note"),
    "adtitle": ("🏷 Введите заготовку названия товара.\n\nПодстановки: "
                "{номинал}, {регион}, {цена}, {карта}", "ad_title"),
    "adtext": ("📄 Введите заготовку описания товара.\n\nПодстановки: "
               "{номинал}, {регион}, {цена}, {карта}\n\n⚠️ Ссылки панель не "
               "принимает — отказ придёт на последнем шаге мастера.",
               "ad_text"),
}


@router.callback_query(F.data.regexp(
    r"^plugins:gc:[a-z0-9_]+:(region|kw|greet|note|adtitle|adtext)$"))
async def gift_field_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    gift = _gift_or_none(callback.data)
    action = str(callback.data).split(":")[3]
    if not gift or action not in _GIFT_FIELDS:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    prompt, field = _GIFT_FIELDS[action]
    await state.set_state(PluginState.gc_field)
    await state.update_data(gc_slug=gift.slug, gc_field=field)
    await callback.message.edit_text(
        prompt, reply_markup=_cancel_kb(f"plugins:gc:{gift.slug}:cfg"))
    await callback.answer()


@router.message(PluginState.gc_field)
async def gift_field_input(message: Message, state: FSMContext) -> None:
    from automation.giftcards import card, card_conf

    data = await state.get_data()
    gift = card(data.get("gc_slug") or "")
    field = str(data.get("gc_field") or "")
    await state.clear()
    allowed = {name for _prompt, name in _GIFT_FIELDS.values()}
    if not gift or field not in allowed:
        await message.answer("Экран устарел — откройте карту заново.")
        return
    value = (message.text or "").strip()
    if field == "region":
        value = value.upper()
    uid = message.from_user.id
    settings = get_settings(uid)
    card_conf(settings, gift.slug)[field] = value[:200]
    save_settings(uid, settings)
    await message.answer(_gift_cfg_text(settings, gift),
                         reply_markup=_gift_cfg_keyboard(gift))


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:log$"))
async def gift_log(callback: CallbackQuery) -> None:
    """Журнал выдач: что купили, почём, чем кончилось.

    Печатается состояние каждой записи, а не только удачные: «ничего не
    произошло» без причины — самая частая поломка этого проекта.
    """
    from automation.giftcards import card_conf

    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    conf = card_conf(get_settings(callback.from_user.id), gift.slug)
    log = conf.get("log") or []
    lines = [f"📜 <b>Журнал — {html.escape(gift.title)}</b>", ""]
    if not log:
        lines.append("Пока пусто.")
    for entry in log[:15]:
        state_ = html.escape(str(entry.get("state") or "—"))
        nominal = html.escape(str(entry.get("nominal") or ""))
        line = (f"#{html.escape(str(entry.get('order') or '—'))} · "
                f"{nominal} · <b>{state_}</b>")
        if entry.get("price"):
            line += f" · {entry['price']} $"
        lines.append(line)
        if entry.get("codes"):
            lines.append("   код: " + ", ".join(
                f"<code>{html.escape(str(c))}</code>" for c in entry["codes"]))
        if entry.get("why"):
            lines.append(f"   причина: {html.escape(str(entry['why'])[:200])}")
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"plugins:gc:{gift.slug}")
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:manual$"))
async def gift_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Ручная выдача спрашивает номер заказа, а не покупателя.

    Код уходит в чат заказа — аккаунт покупателя тут ни при чём. Заказ
    ставится в очередь, а покупает по-прежнему фоновый цикл: второй путь к
    деньгам пришлось бы снабдить тем же порядком записей, и однажды он бы
    с ним разъехался.
    """
    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    await state.set_state(PluginState.gc_manual)
    await state.update_data(gc_slug=gift.slug)
    await callback.message.edit_text(
        f"🚀 <b>Выдать вручную — {html.escape(gift.title)}</b>\n\n"
        f"Введите номер заказа. Бот проверит оплату, подберёт номинал и "
        f"купит код на ближайшем проходе — и отчитается в любом случае, "
        f"и если получится, и если нет.",
        reply_markup=_cancel_kb(f"plugins:gc:{gift.slug}"))
    await callback.answer()


@router.message(PluginState.gc_manual)
async def gift_manual_input(message: Message, state: FSMContext) -> None:
    from automation.giftcards import card, card_conf

    data = await state.get_data()
    gift = card(data.get("gc_slug") or "")
    await state.clear()
    if not gift:
        await message.answer("Экран устарел — откройте карту заново.")
        return
    order_id = (message.text or "").strip()
    if not order_id:
        await message.answer("Номер заказа пустой.")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    conf = card_conf(settings, gift.slug)
    queue = conf.setdefault("force", [])
    if order_id not in queue:
        queue.append(order_id)
    save_settings(uid, settings)
    await message.answer(
        f"✅ Заказ #{html.escape(order_id)} поставлен в очередь "
        f"{html.escape(gift.title)}.\n\nВыдача идёт фоновым проходом — он "
        f"бывает раз в минуту. Отчёт придёт в любом случае.",
        reply_markup=_gift_card_keyboard(settings, gift))


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:stock$"))
async def gift_stock(callback: CallbackQuery) -> None:
    """Наличие и баланс: хватит ли денег на эту карту.

    «Не ноль» и «хватит» — разные вещи, и разница стоит оплаченного заказа.

    Каталог читается **по кнопке, а не при открытии экрана**: у
    `GET /services` лимит два запроса в минуту на кабинет, и читать его на
    каждом открытии значило бы отнимать лимит у выдачи.
    """
    from automation.approute import balance_sync
    from automation.giftcards import affordable, card_conf, regions
    from storage import get_ar_creds

    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    uid = callback.from_user.id
    creds = get_ar_creds(uid) or {}
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=f"plugins:gc:{gift.slug}")
    if not creds.get("api_key"):
        await callback.message.edit_text(
            "🔑 Ключ AppRoute не задан — читать каталог нечем.",
            reply_markup=builder.as_markup())
        await callback.answer()
        return
    await callback.message.edit_text("⏳ Читаю каталог поставщика…")
    loop = asyncio.get_event_loop()
    ok, catalog, age = await _read_catalog(creds)
    if not ok:
        await callback.message.edit_text(
            f"❌ {html.escape(str(catalog)[:400])}",
            reply_markup=builder.as_markup())
        await callback.answer()
        return
    money = 0.0
    bal_ok, accounts = await loop.run_in_executor(None, balance_sync, creds)
    if bal_ok:
        for row in ((accounts or {}).get("items") or []):
            if str(row.get("currency")) == "USD":
                money = float(row.get("available") or row.get("balance") or 0)
    conf = card_conf(get_settings(uid), gift.slug)
    region = str(conf.get("region") or "").upper()
    facts = affordable(catalog, gift, money, region)
    regs = regions(catalog, gift)
    lines = [f"📦 <b>{html.escape(gift.title)} у поставщика</b>"
             + (f" · <i>{age}</i>" if age else ""), "",
             f"Регионов: <b>{len(regs)}</b>"]
    if region:
        lines.append(f"Смотрим регион: <b>{html.escape(region)}</b>")
    if facts["cheapest"]:
        lines += [
            f"Самый дешёвый в наличии: {html.escape(facts['cheapest_name'])} "
            f"({html.escape(facts['cheapest_region'])}) — "
            f"<b>{facts['cheapest']:.2f} $</b>",
            "",
            f"💰 Баланс: <b>{money:.2f} $</b>",
            f"Хватит примерно на <b>{facts['count']}</b> таких кодов; "
            f"по карману номиналов: <b>{facts['total']}</b>",
        ]
        if not facts["count"]:
            lines.append("⚠️ На самый дешёвый номинал денег уже не хватает — "
                         "первая же выдача отвалится «не хватает средств».")
    else:
        lines.append("⚠️ Ни одного номинала в наличии"
                     + (f" в регионе {html.escape(region)}" if region else ""))
    await callback.message.edit_text("\n".join(lines)[:4000],
                                     reply_markup=builder.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Создание товара по номиналу — один экран на все карты
# ---------------------------------------------------------------------------

@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:make$"))
async def gift_make_regions(callback: CallbackQuery, state: FSMContext) -> None:
    """Регион спрашивается первым — раньше номинала.

    От него зависит и какие номиналы показывать, и что уйдёт в описание
    товара: выдача читает регион оттуда. Значит знать его надо до того, как
    описание собрано.

    20.08 выяснилось, что своё поле «Регион» у панели всё-таки есть —
    `filter__8`, и оно обязательное. Описание оно не отменяет: выдача читает
    описание, а не панель. Но регион теперь уходит и подсказкой в мастер,
    чтобы не спрашивать продавца дважды об одном и том же.
    """
    from automation.giftcards import regions
    from storage import get_ar_creds

    await state.clear()
    gift = _gift_or_none(callback.data)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    uid = callback.from_user.id
    creds = get_ar_creds(uid)
    if not creds or not creds.get("api_key"):
        await callback.answer("Сначала ключ AppRoute — без него каталога нет",
                              show_alert=True)
        return
    await callback.answer("⏳ Читаю каталог…")
    await callback.message.edit_text("⏳ Смотрю, что есть у поставщика…")

    ok, catalog, age = await _read_catalog(creds)
    settings = get_settings(uid)
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_gift_card_keyboard(settings, gift))
        return

    rows = [r for r in regions(catalog, gift) if r["in_stock"] > 0]
    if not rows:
        await callback.message.edit_text(
            f"❌ У поставщика нет номиналов «{html.escape(gift.title)}» "
            f"с ненулевым остатком.",
            reply_markup=_gift_card_keyboard(settings, gift))
        return

    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"{r['region']} · {r['in_stock']}",
                 callback_data=f"plugins:gc:{gift.slug}:reg:{r['region']}")
    b.button(text="⬅️ Назад", callback_data=f"plugins:gc:{gift.slug}")
    b.adjust(4)
    await callback.message.edit_text(
        f"🌐 <b>Регион — {html.escape(gift.title)}</b>"
        + (f" · <i>{age}</i>" if age else "") + "\n\n"
        f"Рядом с регионом — сколько номиналов в наличии.\n\n"
        f"<i>Регион уйдёт в описание товара отдельной строкой — оттуда его "
        f"читает выдача. У панели есть и своё поле «Регион»; оно "
        f"обязательное, и мастер спросит его, если сам не подберёт.</i>",
        reply_markup=b.as_markup())


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:reg:[A-Za-z0-9]+$"))
async def gift_make_nominals(callback: CallbackQuery, state: FSMContext) -> None:
    """Номиналы выбранного региона."""
    from automation.giftcards import denominations, nominal_text
    from storage import get_ar_creds

    gift = _gift_or_none(callback.data)
    region = callback.data.split(":")[-1].upper()
    uid = callback.from_user.id
    settings = get_settings(uid)
    if not gift:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    await callback.answer("⏳ Читаю номиналы…")
    ok, catalog, age = await _read_catalog(get_ar_creds(uid))
    if not ok:
        await callback.message.edit_text(
            f"❌ Каталог не прочитан: {html.escape(str(catalog))}",
            reply_markup=_gift_card_keyboard(settings, gift))
        return

    rows = [d for d in denominations(catalog, gift, region) if d["in_stock"] > 0]
    if not rows:
        await callback.message.edit_text(
            f"❌ В регионе {html.escape(region)} нет номиналов в наличии.",
            reply_markup=_gift_card_keyboard(settings, gift))
        return
    await state.update_data(gc_slug=gift.slug, gc_region=region, gc_rows=[
        {"id": d["denomination_id"], "value": d["value"],
         "measure": d["measure"], "name": d["name"], "price": d["price"]}
        for d in rows])

    b = InlineKeyboardBuilder()
    for d in rows[:60]:
        b.button(text=f"{nominal_text(d['value'], d['measure'])} · ${d['price']:.2f}",
                 callback_data=f"plugins:gc:{gift.slug}:den:{d['denomination_id']}")
    b.button(text="⬅️ Другой регион", callback_data=f"plugins:gc:{gift.slug}:make")
    b.adjust(2)
    await callback.message.edit_text(
        f"🏷 <b>Номиналы · {html.escape(gift.title)} · "
        f"{html.escape(region)}</b>" + (f" · <i>{age}</i>" if age else "")
        + "\n\n"
        f"Цены рядом — <b>закупочные, в долларах</b>. Свою цену в рублях "
        f"назначите на следующем шаге: курс боту брать неоткуда, и "
        f"выдумывать его он не станет.", reply_markup=b.as_markup())


@router.callback_query(F.data.regexp(r"^plugins:gc:[a-z0-9_]+:den:[0-9a-f-]+$"))
async def gift_make_price(callback: CallbackQuery, state: FSMContext) -> None:
    from automation.giftcards import nominal_text

    gift = _gift_or_none(callback.data)
    den_id = callback.data.split(":")[-1]
    data = await state.get_data()
    row = next((r for r in (data.get("gc_rows") or [])
                if r.get("id") == den_id), None)
    if not gift or not row:
        # Список живёт в состоянии формы, а оно теряется при перезапуске
        # бота. Промолчать нельзя: продавец нажал и ждёт ответа.
        await callback.answer("Список устарел — откройте регионы заново",
                              show_alert=True)
        return
    await state.set_state(PluginState.gc_price)
    await state.update_data(gc_den=row)
    await callback.message.edit_text(
        f"🏷 <b>{html.escape(nominal_text(row['value'], row['measure']))}</b> · "
        f"{html.escape(gift.title)} · "
        f"{html.escape(str(data.get('gc_region') or ''))}\n\n"
        f"Закупка: ${row['price']:.2f}\n\n"
        f"Назовите цену в рублях, за которую продаёте — одним числом.\n\n"
        f"Дальше откроется обычное создание объявления: название и описание "
        f"подставлю по вашим заготовкам, а количество, фото и раздел мастер "
        f"спросит сам.",
        reply_markup=_cancel_kb(f"plugins:gc:{gift.slug}:make"))
    await callback.answer()


@router.message(PluginState.gc_price)
async def gift_make_handoff(message: Message, state: FSMContext) -> None:
    """Цена принята — управление уходит обычному мастеру объявления.

    Своего создания товаров здесь нет и не нужно: мастер уже умеет то, чего
    каталог не знает — разделы панели, обязательные поля, фото, публикацию.
    Плагин подставляет только то, что знает сам.
    """
    from automation.giftcards import (card, card_conf, fill_template,
                                      nominal_text, with_region)

    data = await state.get_data()
    gift = card(data.get("gc_slug") or "")
    den = data.get("gc_den") or {}
    region = str(data.get("gc_region") or "").upper()
    digits = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not gift or not den or not digits:
        await message.answer("❌ Нужно одно число — цена в рублях.")
        return
    price = int(digits)
    await state.clear()

    conf = card_conf(get_settings(message.from_user.id), gift.slug)
    nominal = nominal_text(den["value"], den["measure"])
    filled = functools.partial(fill_template, nominal=nominal, region=region,
                               price=price)
    ad_title = filled(conf.get("ad_title") or gift.ad_title)[:100]
    ad_text = with_region(filled(conf.get("ad_text") or gift.ad_text), region)

    from handlers.create_ad import CreateAdState
    await state.set_state(CreateAdState.quantity)
    # Регион идёт подсказкой вместе с разделами: у панели есть своё поле
    # «Регион», и оно обязательное — но узнаётся это только из отказа, потому
    # что обязательность зависит от раздела. Подсказка сработает, только если
    # ровно один вариант подойдёт; иначе мастер спросит, а не решит за
    # продавца, в каком регионе его товар.
    await state.update_data(title=ad_title, price=price, description=ad_text,
                            autopick=list(gift.autopick) + [region.lower()])
    b = InlineKeyboardBuilder()
    b.button(text="1️⃣ Пропустить (кол-во = 1)", callback_data="create_ad:qty:1")
    b.button(text="❌ Отмена", callback_data="menu:ads")
    b.adjust(1)
    await message.answer(
        f"✅ Цена: <b>{price} ₽</b> за <b>{html.escape(nominal)}</b> "
        f"({html.escape(region)})\n\n"
        f"Название: <code>{html.escape(ad_title)}</code>\n"
        f"Регион уйдёт в описание отдельной строкой — оттуда его читает "
        f"выдача.\n\n"
        f"Сколько таких кодов выставляем? Введите число или пропустите — "
        f"но тогда объявление продастся один раз и уйдёт с витрины.",
        reply_markup=b.as_markup())


# ---------------------------------------------------------------------------
# Plugins main menu
# ---------------------------------------------------------------------------

def _plugins_menu_text(settings: dict, shop_name: str = "") -> str:
    """Экран плагинов — короткая витрина, а не отчёт.

    Счёт выдач отсюда убран по решению продавца 21.08: это первый экран, и
    открывают его, чтобы включить плагин, а не свериться с цифрами. Цифры
    остались там, где они по делу, — на экране гифт-карт и в карточке
    каждой карты, вместе с журналом.

    Что осталось: пять строк. Каждая — про устройство бота, а не про
    будущие доходы. «Продажи вырастут» проверить нельзя, а «заказы не ждут
    вас до утра» продавец проверит в первую же ночь, и после этого поверит
    остальному.

    Число карт нарочно не названо. Оно меняется каждую неделю, а цифра в
    тексте — нет: разойдясь однажды, экран начинает врать по мелочи.

    **Про звёзды сказано «свой плагин», а не «уходят сами».** Плагин
    AutoStars в боте есть — экран, кошелёк TON, настройки; а вот сама
    выдача заблокирована снаружи: Fragment отвечает `Access denied` на
    `initBuyStarsRequest` (пункт A1 в `CHECKLIST.md`, разобрано пробами
    10.08). Написать «звёзды летят покупателю сами» значит продать чужому
    продавцу то, чего сегодня нет, — и получить возвраты вместо подписок.
    Когда выдача пойдёт живьём, строку надо усилить.
    """
    name_part = f" · {html.escape(shop_name)}" if shop_name else ""
    return "\n".join([
        f"🧩 <b>Плагины{name_part}</b>",
        "",
        "Покупатель оплатил — бот сам купил товар у поставщика и отправил "
        "его в чат заказа. Без вас.",
        "",
        "🎁 Гифт-карты: Apple, Xbox, Steam, PlayStation, Roblox, Amazon…",
        "⭐ Telegram Stars — свой плагин, с кошельком TON",
        "🌙 Ночью и в выходные заказы не ждут вас",
        "⏱ Ни поставщика, ни ручного копирования",
        "🆕 Плагины прибавляются — новые появятся здесь же",
    ])


@router.callback_query(F.data == "plugins:menu")
async def plugins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(
        _plugins_menu_text(settings, get_shop_name(uid)),
        reply_markup=_plugins_menu_keyboard(settings),
    )
    await callback.answer()
