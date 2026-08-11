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
    """Parse a 'k=v; k2=v2' cookie string into a dict."""
    out: dict = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


async def deliver_stars(uid: int, username: str, quantity: int) -> tuple[bool, str]:
    """Run the blocking Fragment purchase in a thread. Returns (ok, message)."""
    from automation.fragment import buy_stars_sync
    creds = get_fragment_creds(uid)
    if not creds or not creds.get("cookies") or not creds.get("mnemonic"):
        return False, ("Не настроены данные Fragment.\n"
                       "Плагины → AutoStars → ⚙️ Настройки → 🔑 Данные Fragment")
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, buy_stars_sync,
                creds["cookies"], creds["mnemonic"], username, quantity,
                creds.get("wallet_version", "v4r2"),
                # Пусто — бот подберёт хеш сам; чужой даёт «Bad request».
                creds.get("api_hash", ""),
                proxy=creds.get("proxy", ""),
            ),
            timeout=180,
        )
    except asyncio.TimeoutError:
        return False, "⏱ Fragment/TON не ответили за 180 секунд"
    except Exception as e:
        return False, f"Ошибка выдачи: {str(e)[:150]}"


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
    # AutoRoblox
    roblox_manual_buyer = State()
    roblox_manual_amount = State()
    roblox_set_amount = State()
    roblox_set_note = State()
    # AutoGifts
    gifts_manual_buyer = State()
    gifts_set_type = State()
    gifts_set_note = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


def _plugins_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    builder.button(text="🎮 AutoRoblox", callback_data="plugins:auto_roblox")
    builder.button(text="🎁 AutoGifts", callback_data="plugins:auto_gifts")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(2, 1, 1)
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
        "🍪 <b>Cookies Fragment</b>\n\n"
        "1. Войдите на <b>fragment.com</b> через TON-кошелёк\n"
        "2. F12 → Console → введите <code>document.cookie</code> → Enter\n"
        "3. Скопируйте результат и пришлите сюда\n\n"
        "<i>Формат: stel_token=...; stel_ssid=...; ...</i>\n\n"
        "⚠️ <code>document.cookie</code> не покажет <b>stel_token</b> и "
        "<b>stel_ssid</b> — они HttpOnly. Их берут по одной кнопкой выше "
        "(Application → Cookies → fragment.com).\n\n" + _PHONE_HELP,
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
          "<b>Вручную это заполнять не нужно.</b> Бот сам читает хеш со "
          "страницы Fragment перед каждой покупкой. Если он не читается — "
          "дело почти всегда в куках: они истекли, и страница отдаётся как "
          "гостю. Тогда помогает не хеш, а свежие куки.\n\n"
          "<i>Если всё же хотите задать вручную и есть компьютер:</i> "
          "F12 → вкладка <b>Network</b> → на fragment.com сделайте любое "
          "действие → найдите запрос <code>api?hash=…</code> → скопируйте "
          "значение после <code>hash=</code>.",
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
    ok, msg = await deliver_stars(message.from_user.id, buyer, amount)
    b = InlineKeyboardBuilder()
    b.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    if ok:
        await status.edit_text(
            f"✅ <b>Звёзды выданы!</b>\n\n{msg}", reply_markup=b.as_markup())
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
    loop = asyncio.get_event_loop()
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(
                buy_stars_sync, creds["cookies"], creds["mnemonic"],
                username, qty, creds.get("wallet_version", "v4r2"),
                creds.get("api_hash", ""), report=spend)),
            timeout=200)
    except Exception as e:
        ok, msg = False, str(e)[:150]

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
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    # Тумблер ничего не включает: выдачи Robux ещё нет, и фоновый цикл этот
    # раздел не читает. «🟢 Автовыдача включена» на таком экране — обещание,
    # которого никто не выполнит.
    status = ("🟢 Настройки сохранены" if enabled
              else "🔴 Настройки не сохранены")
    return (
        f"🎮 <b>Roblox — Robux{name_part}</b>\n\n"
        f"🚧 <b>Раздел готовится.</b> Выдача Robux появится в следующем "
        f"обновлении — сейчас можно только сохранить настройки.\n\n"
        f"{status}\n"
        f"{note}"
    )


def _roblox_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_roblox"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:roblox:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:roblox:accumulated")
    builder.button(text="💎 Прибыль", callback_data="plugins:roblox:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:roblox:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:roblox:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:roblox:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:roblox:toggle")
    builder.button(text="⚙️ Настройки", callback_data="plugins:roblox:settings")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(2, 2, 2, 1, 2)
    return builder.as_markup()


def _roblox_settings_text(settings: dict) -> str:
    p = settings["plugins"]["auto_roblox"]
    robux = p.get("robux", 0)
    note = p.get("note") or "—"
    return (
        f"⚙️ <b>Настройки AutoRoblox</b>\n\n"
        f"🎮 Кол-во Robux: <b>{robux}</b>\n"
        f"📝 Заметка: <i>{note}</i>"
    )


def _roblox_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Кол-во Robux", callback_data="plugins:roblox:set_amount")
    builder.button(text="📝 Заметка", callback_data="plugins:roblox:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    builder.adjust(2, 1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_roblox")
async def roblox_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(_roblox_text(settings, get_shop_name(uid)), reply_markup=_roblox_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:toggle")
async def roblox_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["enabled"] = not settings["plugins"]["auto_roblox"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_roblox_text(settings, get_shop_name(uid)), reply_markup=_roblox_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:settings")
async def roblox_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_roblox_settings_text(settings), reply_markup=_roblox_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:set_amount")
async def roblox_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_set_amount)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
    await callback.message.edit_text(
        f"🎮 Кол-во Robux (сейчас: <b>{cur}</b>)\n\nВведите число:",
        reply_markup=_cancel_kb("plugins:roblox:settings"),
    )
    await callback.answer()


@router.message(PluginState.roblox_set_amount)
async def roblox_set_amount_input(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["robux"] = amount
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Кол-во Robux: <b>{amount}</b>", reply_markup=_roblox_settings_keyboard())


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
    await message.answer(f"✅ Заметка: <i>{note or '—'}</i>", reply_markup=_roblox_settings_keyboard())


@router.callback_query(F.data == "plugins:roblox:manual")
async def roblox_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача Robux</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_roblox"),
    )
    await callback.answer()


@router.message(PluginState.roblox_manual_buyer)
async def roblox_manual_buyer_input(message: Message, state: FSMContext) -> None:
    await state.update_data(buyer=message.text or "")
    await state.set_state(PluginState.roblox_manual_amount)
    default = get_settings(message.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
    await message.answer(f"🎮 Кол-во Robux (по умолчанию: {default}), 0 = значение по умолчанию:")


@router.message(PluginState.roblox_manual_amount)
async def roblox_manual_amount_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buyer = data.get("buyer", "—")
    await state.clear()
    try:
        amount = int((message.text or "").strip())
        if amount == 0:
            amount = get_settings(message.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return
    await message.answer(
        f"🎮 <b>Выдача Robux</b>\n\n"
        f"👤 Покупатель: <b>{buyer}</b>\n"
        f"🎮 Кол-во: <b>{amount}</b>\n\n"
        f"⚠️ Функция отправки Robux будет доступна в следующем обновлении."
    )


@router.callback_query(F.data.in_({"plugins:roblox:accumulated", "plugins:roblox:profit",
                                    "plugins:roblox:balance", "plugins:roblox:notifs", "plugins:roblox:replies"}))
async def roblox_stub(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# AutoGifts
# ---------------------------------------------------------------------------

def _gifts_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_gifts"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = ("🟢 Настройки сохранены" if enabled
              else "🔴 Настройки не сохранены")
    return (
        f"🎁 <b>Telegram — Подарки{name_part}</b>\n\n"
        f"🚧 <b>Раздел готовится.</b> Отправка подарков появится в следующем "
        f"обновлении — сейчас можно только сохранить настройки.\n\n"
        f"{status}\n"
        f"{note}"
    )


def _gifts_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_gifts"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:gifts:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:gifts:accumulated")
    builder.button(text="💎 Прибыль", callback_data="plugins:gifts:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:gifts:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:gifts:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:gifts:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:gifts:toggle")
    builder.button(text="⚙️ Настройки", callback_data="plugins:gifts:settings")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(2, 2, 2, 1, 2)
    return builder.as_markup()


def _gifts_settings_text(settings: dict) -> str:
    p = settings["plugins"]["auto_gifts"]
    gift_type = p.get("gift_type") or "—"
    note = p.get("note") or "—"
    return (
        f"⚙️ <b>Настройки AutoGifts</b>\n\n"
        f"🎁 Тип подарка: <b>{gift_type}</b>\n"
        f"📝 Заметка: <i>{note}</i>"
    )


def _gifts_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Тип подарка", callback_data="plugins:gifts:set_type")
    builder.button(text="📝 Заметка", callback_data="plugins:gifts:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_gifts")
    builder.adjust(2, 1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_gifts")
async def gifts_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(_gifts_text(settings, get_shop_name(uid)), reply_markup=_gifts_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:toggle")
async def gifts_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["enabled"] = not settings["plugins"]["auto_gifts"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_gifts_text(settings, get_shop_name(uid)), reply_markup=_gifts_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:settings")
async def gifts_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_gifts_settings_text(settings), reply_markup=_gifts_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:set_type")
async def gifts_set_type_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_set_type)
    await callback.message.edit_text("🎁 Введите тип подарка:", reply_markup=_cancel_kb("plugins:gifts:settings"))
    await callback.answer()


@router.message(PluginState.gifts_set_type)
async def gifts_set_type_input(message: Message, state: FSMContext) -> None:
    gift_type = (message.text or "").strip()
    if not gift_type:
        await message.answer("❌ Тип не может быть пустым:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["gift_type"] = gift_type
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Тип подарка: <b>{gift_type}</b>", reply_markup=_gifts_settings_keyboard())


@router.callback_query(F.data == "plugins:gifts:set_note")
async def gifts_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_set_note)
    await callback.message.edit_text("📝 Введите заметку для покупателя:", reply_markup=_cancel_kb("plugins:gifts:settings"))
    await callback.answer()


@router.message(PluginState.gifts_set_note)
async def gifts_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка: <i>{note or '—'}</i>", reply_markup=_gifts_settings_keyboard())


@router.callback_query(F.data == "plugins:gifts:manual")
async def gifts_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача подарка</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_gifts"),
    )
    await callback.answer()


@router.message(PluginState.gifts_manual_buyer)
async def gifts_manual_buyer_input(message: Message, state: FSMContext) -> None:
    buyer = (message.text or "").strip()
    await state.clear()
    gift_type = get_settings(message.from_user.id)["plugins"]["auto_gifts"].get("gift_type") or "—"
    await message.answer(
        f"🎁 <b>Выдача подарка</b>\n\n"
        f"👤 Покупатель: <b>{buyer}</b>\n"
        f"🎁 Тип: <b>{gift_type}</b>\n\n"
        f"⚠️ Функция отправки подарков будет доступна в следующем обновлении."
    )


@router.callback_query(F.data.in_({"plugins:gifts:accumulated", "plugins:gifts:profit",
                                    "plugins:gifts:balance", "plugins:gifts:notifs", "plugins:gifts:replies"}))
async def gifts_stub(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# Plugins main menu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "plugins:menu")
async def plugins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🧩 <b>Плагины</b>\n\n"
        "Автоматическая доставка цифровых товаров при новых заказах.",
        reply_markup=_plugins_menu_keyboard(),
    )
    await callback.answer()
