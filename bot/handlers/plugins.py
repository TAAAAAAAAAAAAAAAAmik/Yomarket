from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
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
                creds.get("api_hash", "af142ec36cafbbfa89"),
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
    stars_set_amount = State()
    stars_set_note = State()
    stars_set_cookies = State()
    stars_set_mnemonic = State()
    stars_set_keyword = State()
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
    builder.button(text="💎 Прибыль", callback_data="plugins:stars:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:stars:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:stars:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:stars:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:stars:toggle")
    builder.button(text="⚙️ Настройки", callback_data="plugins:stars:settings")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(2, 2, 2, 1, 2)
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

def _creds_kb(has: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🍪 Задать cookies Fragment", callback_data="plugins:stars:set_cookies")
    b.button(text="🔐 Задать seed-фразу TON", callback_data="plugins:stars:set_mnemonic")
    if has:
        b.button(text="🧪 Проверить cookies", callback_data="plugins:stars:check_creds")
        b.button(text="🗑 Удалить данные", callback_data="plugins:stars:del_creds")
    b.button(text="⬅️ Назад", callback_data="plugins:stars:settings")
    b.adjust(2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data == "plugins:stars:creds")
async def stars_creds(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # Cancel из промптов cookies/seed ведёт сюда
    uid = callback.from_user.id
    creds = get_fragment_creds(uid) or {}
    has_c = bool(creds.get("cookies"))
    has_m = bool(creds.get("mnemonic"))
    await callback.message.edit_text(
        "🔑 <b>Данные Fragment</b>\n\n"
        f"🍪 Cookies: <b>{'🟢 заданы' if has_c else '🔴 нет'}</b>\n"
        f"🔐 Seed-фраза: <b>{'🟢 задана' if has_m else '🔴 нет'}</b>\n\n"
        "⚠️ <b>Это доступ к вашему TON-кошельку и Fragment.</b> "
        "Данные хранятся только у бота, в чат не выводятся. "
        "Сообщения с секретами удаляются автоматически.",
        reply_markup=_creds_kb(has_c and has_m),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:set_cookies")
async def stars_set_cookies_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_cookies)
    await callback.message.edit_text(
        "🍪 <b>Cookies Fragment</b>\n\n"
        "1. Войдите на <b>fragment.com</b> через TON-кошелёк\n"
        "2. F12 → Console → введите <code>document.cookie</code> → Enter\n"
        "3. Скопируйте результат и пришлите сюда\n\n"
        "<i>Формат: stel_token=...; stel_ssid=...; ...</i>",
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
            reply_markup=_creds_kb(False))
        return
    save_fragment_creds(message.from_user.id, {"cookies": cookies})
    creds = get_fragment_creds(message.from_user.id) or {}
    await message.answer(
        f"✅ Cookies сохранены ({len(cookies)} шт.).",
        reply_markup=_creds_kb(bool(creds.get("cookies") and creds.get("mnemonic"))),
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
            reply_markup=_creds_kb(False))
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
            reply_markup=_creds_kb(False))
        return
    save_fragment_creds(message.from_user.id, {"mnemonic": " ".join(words),
                                               "wallet_version": wv})
    creds = get_fragment_creds(message.from_user.id) or {}
    await message.answer(
        f"✅ Кошелёк сохранён.\n💼 Адрес: <code>{addr}</code>\n\n"
        "Пополните его TON для оплаты звёзд.",
        reply_markup=_creds_kb(bool(creds.get("cookies") and creds.get("mnemonic"))),
    )


@router.callback_query(F.data == "plugins:stars:check_creds")
async def stars_check_creds(callback: CallbackQuery) -> None:
    from automation.fragment import check_fragment_session_sync
    creds = get_fragment_creds(callback.from_user.id) or {}
    await callback.answer("⏳ Проверяю…")
    loop = asyncio.get_event_loop()
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, check_fragment_session_sync, creds.get("cookies")),
            timeout=25,
        )
    except Exception as e:
        ok, msg = False, str(e)[:80]
    await callback.message.answer(
        ("✅ " if ok else "⚠️ ") + f"Fragment: {msg}")


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


@router.callback_query(F.data == "plugins:stars:accumulated")
async def stars_accumulated(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:profit")
async def stars_profit(callback: CallbackQuery) -> None:
    await callback.answer("📊 Статистика прибыли появится в следующем обновлении", show_alert=True)


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


@router.callback_query(F.data == "plugins:stars:notifs")
async def stars_notifs(callback: CallbackQuery) -> None:
    await callback.answer("🔔 Настройки уведомлений появятся в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:replies")
async def stars_replies(callback: CallbackQuery) -> None:
    await callback.answer("💬 Авто-ответы появятся в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# AutoRoblox
# ---------------------------------------------------------------------------

def _roblox_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_roblox"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"🎮 <b>Roblox — Robux{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
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
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"🎁 <b>Telegram — Подарки{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
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
