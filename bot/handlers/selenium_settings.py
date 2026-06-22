"""Handlers for panel automation: SMS/cookie login, auto-bump, auto-restore, auto-withdraw."""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_panel_creds, save_panel_creds, delete_panel_creds, get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

# HTTP SMS sessions: user_id -> YooMarketPanelHTTP instance
_http_sessions: dict[int, object] = {}


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class SeleniumState(StatesGroup):
    waiting_http_phone = State()
    waiting_http_code = State()
    waiting_cookie_string = State()
    waiting_bump_interval = State()
    waiting_withdraw_amount = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts)
        else:
            s = str(ts)[:19]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return str(ts)[:16]
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return str(ts)[:16]


def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


def _no_creds_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Настроить вход в панель", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Setup: choose method
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "selenium:setup:start")
async def setup_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    creds = get_panel_creds(callback.from_user.id)
    status = "✅ Сессия настроена" if creds else "❌ Не настроено"
    b = InlineKeyboardBuilder()
    b.button(text="📱 Войти через SMS", callback_data="selenium:setup:http")
    b.button(text="📋 Вставить cookies (с ПК)", callback_data="selenium:setup:cookie")
    if creds:
        b.button(text="🔍 Проверить сессию", callback_data="selenium:check_session")
        b.button(text="🗑 Удалить сессию", callback_data="selenium:setup:delete")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1)
    await callback.message.edit_text(
        f"🔑 <b>Вход в панель YooMarket</b>\n\n"
        f"Статус: {status}",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Method 1: HTTP SMS login (mobile-friendly, no browser)
# ---------------------------------------------------------------------------

# Stores HTTP panel sessions between phone and code steps
_http_sessions: dict[int, object] = {}


@router.callback_query(F.data == "selenium:setup:http")
async def setup_http_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SeleniumState.waiting_http_phone)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="selenium:setup:start")
    await callback.message.edit_text(
        "📱 <b>Вход по SMS</b>\n\n"
        "Введи номер телефона или e-mail от аккаунта <b>panel.yoomarket.net</b>:\n\n"
        "<i>Бот запросит SMS-код без запуска браузера</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_http_phone)
async def setup_http_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not phone:
        await message.answer("❌ Введи номер телефона или e-mail:")
        return

    uid = message.from_user.id
    wait_msg = await message.answer("⏳ Запрашиваю SMS-код...")

    from automation.panel import YooMarketPanelHTTP
    panel = YooMarketPanelHTTP()
    await panel.start()

    try:
        ok, err = await panel.send_sms(phone)
    except Exception as e:
        ok, err = False, str(e)

    if not ok:
        await panel.close()
        await state.clear()
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Попробовать снова", callback_data="selenium:setup:http")
        b.button(text="📋 Вставить cookies", callback_data="selenium:setup:cookie")
        b.button(text="⬅️ Назад", callback_data="selenium:setup:start")
        b.adjust(1)
        await wait_msg.edit_text(err, reply_markup=b.as_markup())
        return

    _http_sessions[uid] = panel
    await state.set_state(SeleniumState.waiting_http_code)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="selenium:http:cancel")
    await wait_msg.edit_text(
        f"📨 <b>SMS отправлено</b> на <code>{phone}</code>\n\nВведи код из SMS:",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "selenium:http:cancel")
async def http_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    panel = _http_sessions.pop(uid, None)
    if panel:
        try:
            await panel.close()
        except Exception:
            pass
    await state.clear()
    await callback.message.edit_text("❌ Вход отменён.", reply_markup=_no_creds_kb())
    await callback.answer()


@router.message(SeleniumState.waiting_http_code)
async def setup_http_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    uid = message.from_user.id

    panel = _http_sessions.pop(uid, None)
    if panel is None:
        await message.answer("❌ Сессия истекла. Начни заново.", reply_markup=_no_creds_kb())
        await state.clear()
        return

    wait_msg = await message.answer("⏳ Проверяю код...")

    try:
        ok, result = await panel.verify_code(code)
    except Exception as e:
        ok, result = False, str(e)
    finally:
        try:
            await panel.close()
        except Exception:
            pass

    await state.clear()

    if ok:
        save_panel_creds(uid, {"cookie_string": result})
        b = InlineKeyboardBuilder()
        b.button(text="🔍 Проверить сессию", callback_data="selenium:check_session")
        b.button(text="⬅️ К авто-функциям", callback_data="auto:menu")
        b.adjust(1)
        await wait_msg.edit_text(
            "✅ <b>Вход выполнен!</b>\n\nСессия сохранена. Авто-функции готовы к работе.",
            reply_markup=b.as_markup(),
        )
    else:
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Попробовать снова", callback_data="selenium:setup:http")
        b.button(text="📋 Вставить cookies", callback_data="selenium:setup:cookie")
        b.button(text="⬅️ Назад", callback_data="selenium:setup:start")
        b.adjust(1)
        await wait_msg.edit_text(
            f"❌ <b>Ошибка входа</b>\n\n{result}",
            reply_markup=b.as_markup(),
        )


# ---------------------------------------------------------------------------
# Method 2: Cookie paste (desktop — F12 → document.cookie)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "selenium:setup:cookie")
async def setup_cookie_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SeleniumState.waiting_cookie_string)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="selenium:setup:start")
    await callback.message.edit_text(
        "📋 <b>Вставить cookies из браузера</b>\n\n"
        "<b>Как получить cookies:</b>\n"
        "1. Открой <b>panel.yoomarket.net</b> в браузере и войди\n"
        "2. Нажми <b>F12</b> → вкладка <b>Console</b>\n"
        "3. Введи команду: <code>document.cookie</code>\n"
        "4. Скопируй весь вывод и отправь сюда\n\n"
        "<i>Cookies выглядят примерно так:\n"
        "session=abc123; token=xyz; user_id=456</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_cookie_string)
async def setup_cookie_save(message: Message, state: FSMContext) -> None:
    cookie_string = (message.text or "").strip()
    if not cookie_string or "=" not in cookie_string:
        await message.answer(
            "❌ Это не похоже на cookies.\n\n"
            "Убедись, что скопировал вывод команды <code>document.cookie</code> из консоли браузера."
        )
        return
    await state.clear()
    uid = message.from_user.id
    save_panel_creds(uid, {"cookie_string": cookie_string})
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Проверить сессию", callback_data="selenium:check_session")
    b.button(text="⬅️ К авто-функциям", callback_data="auto:menu")
    b.adjust(1)
    await message.answer(
        "✅ <b>Cookies сохранены!</b>\n\n"
        "Теперь авто-поднятие, авто-восстановление и авто-вывод доступны.",
        reply_markup=b.as_markup(),
    )


# ---------------------------------------------------------------------------
# Delete saved credentials
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "selenium:setup:delete")
async def setup_delete(callback: CallbackQuery) -> None:
    delete_panel_creds(callback.from_user.id)
    await callback.answer("✅ Cookies удалены", show_alert=True)
    await setup_start(callback, None)


# ---------------------------------------------------------------------------
# Session check (HTTP-based, no browser)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "selenium:check_session")
async def check_session(callback: CallbackQuery) -> None:
    await callback.message.edit_text("⏳ Проверяю сессию...")
    creds = get_panel_creds(callback.from_user.id)
    if not creds or not creds.get("cookie_string"):
        await callback.message.edit_text("❌ Cookies не настроены.", reply_markup=_no_creds_kb())
        await callback.answer()
        return

    ok = await _http_check_session(creds["cookie_string"])

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить cookies", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1)
    if ok:
        await callback.message.edit_text(
            "✅ <b>Сессия активна!</b>\n\nАвто-функции через браузер работают.",
            reply_markup=b.as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Сессия истекла</b>\n\n"
            "Нужно обновить cookies — зайди на panel.yoomarket.net, скопируй document.cookie заново.",
            reply_markup=b.as_markup(),
        )
    await callback.answer()


async def _http_check_session(cookie_string: str) -> bool:
    """Check if cookies are valid via plain HTTP (no browser)."""
    import aiohttp
    cookies: dict[str, str] = {}
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(cookies=cookies, connector=connector, timeout=timeout) as sess:
            async with sess.get("https://panel.yoomarket.net/") as resp:
                final_url = str(resp.url)
                return "/login" not in final_url and "/auth" not in final_url
    except Exception as e:
        logger.warning("HTTP session check error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Auto-bump menu
# ---------------------------------------------------------------------------

def _bump_text(s: dict, creds) -> str:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    last_run = _fmt_ts(ab.get("last_bump_run"))
    lines = [
        "⬆️ <b>Авто-поднятие</b>\n",
        f"Статус: {_st(on)}",
        f"Интервал: каждые {interval} ч",
        f"Последний запуск: {last_run}",
        "",
        "Ставит все объявления на верх каждые N часов через браузер.",
    ]
    if not creds:
        lines.append("\n⚠️ <b>Нужно настроить вход в панель</b>")
    return "\n".join(lines)


def _bump_kb(s: dict, creds) -> InlineKeyboardMarkup:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:bump")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:bump:toggle")
    b.button(text=f"⏱ Интервал: {interval} ч", callback_data="selenium:bump:set_interval")
    b.button(text="🔑 Сменить cookies", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1, 2, 2)
    return b.as_markup()


@router.callback_query(F.data == "selenium:bump:menu")
async def bump_menu(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    s = get_settings(uid)
    await callback.message.edit_text(_bump_text(s, creds), reply_markup=_bump_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:toggle")
async def bump_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    s = get_settings(uid)
    s["auto_bump"]["enabled"] = not s["auto_bump"].get("enabled", False)
    save_settings(uid, s)
    await callback.message.edit_text(_bump_text(s, creds), reply_markup=_bump_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:set_interval")
async def bump_set_interval(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_bump", {}).get("interval_hours", 24)
    await state.set_state(SeleniumState.waiting_bump_interval)
    await callback.message.edit_text(
        f"⏱ <b>Интервал поднятия</b>\n\nТекущий: {cur} ч\n\nВведите количество часов (например: 6, 12, 24):",
        reply_markup=_cancel_kb("selenium:bump:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_bump_interval)
async def bump_save_interval(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if hours < 1:
            raise ValueError
        s = get_settings(message.from_user.id)
        s["auto_bump"]["interval_hours"] = hours
        save_settings(message.from_user.id, s)
        await state.clear()
        await message.answer(f"✅ Интервал поднятия: <b>{hours} ч</b>")
        creds = get_panel_creds(message.from_user.id)
        await message.answer(_bump_text(s, creds), reply_markup=_bump_kb(s, creds))
    except ValueError:
        await message.answer("❌ Введите целое число часов, например: 24")


@router.callback_query(F.data == "selenium:run:bump")
async def run_bump(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    await callback.answer("⏳ Запускаю поднятие...", show_alert=False)
    await callback.message.edit_text("⏳ Поднимаю объявления через браузер...")
    try:
        from automation.panel import YooMarketPanel
        panel = YooMarketPanel(creds.get("cookie_string", ""))
        await panel.start()
        try:
            count, msg = await panel.bump_all_ads()
        finally:
            await panel.close()
        import time
        s = get_settings(uid)
        s["auto_bump"]["last_bump_run"] = time.time()
        save_settings(uid, s)
        result_text = f"⬆️ <b>Авто-поднятие завершено</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual bump error for user %s: %s", uid, e)
        result_text = f"❌ Ошибка при поднятии: {e}"
    s = get_settings(uid)
    creds = get_panel_creds(uid)
    await callback.message.edit_text(
        result_text + "\n\n" + _bump_text(s, creds),
        reply_markup=_bump_kb(s, creds),
    )


# ---------------------------------------------------------------------------
# Auto-restore menu
# ---------------------------------------------------------------------------

def _restore_text(s: dict, creds) -> str:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    last_run = _fmt_ts(ar.get("last_restore_run"))
    lines = [
        "🔄 <b>Авто-восстановление</b>\n",
        f"Статус: {_st(on)}",
        f"Последний запуск: {last_run}",
        "",
        "Переактивирует проданные или истёкшие объявления через браузер.",
    ]
    if not creds:
        lines.append("\n⚠️ <b>Нужно настроить вход в панель</b>")
    return "\n".join(lines)


def _restore_kb(s: dict, creds) -> InlineKeyboardMarkup:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:restore")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:restore:toggle")
    b.button(text="🔑 Сменить cookies", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1, 1, 2)
    return b.as_markup()


@router.callback_query(F.data == "selenium:restore:menu")
async def restore_menu(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    s = get_settings(uid)
    await callback.message.edit_text(_restore_text(s, creds), reply_markup=_restore_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:restore:toggle")
async def restore_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    s = get_settings(uid)
    s["auto_restore"]["enabled"] = not s["auto_restore"].get("enabled", False)
    save_settings(uid, s)
    await callback.message.edit_text(_restore_text(s, creds), reply_markup=_restore_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:run:restore")
async def run_restore(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    await callback.answer("⏳ Запускаю восстановление...", show_alert=False)
    await callback.message.edit_text("⏳ Восстанавливаю объявления через браузер...")
    try:
        from automation.panel import YooMarketPanel
        panel = YooMarketPanel(creds.get("cookie_string", ""))
        await panel.start()
        try:
            count, msg = await panel.restore_sold_ads()
        finally:
            await panel.close()
        import time
        s = get_settings(uid)
        s["auto_restore"]["last_restore_run"] = time.time()
        save_settings(uid, s)
        result_text = f"🔄 <b>Авто-восстановление завершено</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual restore error for user %s: %s", uid, e)
        result_text = f"❌ Ошибка при восстановлении: {e}"
    s = get_settings(uid)
    creds = get_panel_creds(uid)
    await callback.message.edit_text(
        result_text + "\n\n" + _restore_text(s, creds),
        reply_markup=_restore_kb(s, creds),
    )


# ---------------------------------------------------------------------------
# Auto-withdraw menu
# ---------------------------------------------------------------------------

def _withdraw_text(s: dict, creds) -> str:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    lines = [
        "💸 <b>Авто-вывод баланса</b>\n",
        f"Статус: {_st(on)}",
        f"Мин. сумма: {min_amount} ₽",
        "",
        "Переводит баланс, когда он превышает указанный порог.",
    ]
    if not creds:
        lines.append("\n⚠️ <b>Нужно настроить вход в панель</b>")
    return "\n".join(lines)


def _withdraw_kb(s: dict, creds) -> InlineKeyboardMarkup:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:withdraw")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:withdraw:toggle")
    b.button(text=f"💰 Порог: {min_amount} ₽", callback_data="selenium:withdraw:set_amount")
    b.button(text="🔑 Сменить cookies", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1, 2, 2)
    return b.as_markup()


@router.callback_query(F.data == "selenium:withdraw:menu")
async def withdraw_menu(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    s = get_settings(uid)
    await callback.message.edit_text(_withdraw_text(s, creds), reply_markup=_withdraw_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:toggle")
async def withdraw_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    s = get_settings(uid)
    s["auto_withdraw"]["enabled"] = not s["auto_withdraw"].get("enabled", False)
    save_settings(uid, s)
    await callback.message.edit_text(_withdraw_text(s, creds), reply_markup=_withdraw_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:set_amount")
async def withdraw_set_amount(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_withdraw", {}).get("min_amount", 500)
    await state.set_state(SeleniumState.waiting_withdraw_amount)
    await callback.message.edit_text(
        f"💰 <b>Минимальная сумма для вывода</b>\n\nТекущая: {cur} ₽\n\nВведите новую сумму (₽):",
        reply_markup=_cancel_kb("selenium:withdraw:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_withdraw_amount)
async def withdraw_save_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount < 1:
            raise ValueError
        s = get_settings(message.from_user.id)
        s["auto_withdraw"]["min_amount"] = amount
        save_settings(message.from_user.id, s)
        await state.clear()
        await message.answer(f"✅ Мин. сумма вывода: <b>{amount} ₽</b>")
        creds = get_panel_creds(message.from_user.id)
        await message.answer(_withdraw_text(s, creds), reply_markup=_withdraw_kb(s, creds))
    except ValueError:
        await message.answer("❌ Введите целое число, например: 500")


@router.callback_query(F.data == "selenium:run:withdraw")
async def run_withdraw(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds:
        await callback.answer("⚠️ Сначала настройте вход в панель", show_alert=True)
        return
    s = get_settings(uid)
    min_amount = s.get("auto_withdraw", {}).get("min_amount", 500)
    await callback.answer("⏳ Запускаю вывод...", show_alert=False)
    await callback.message.edit_text("⏳ Проверяю баланс и выполняю вывод через браузер...")
    try:
        from automation.panel import YooMarketPanel
        panel = YooMarketPanel(creds.get("cookie_string", ""))
        await panel.start()
        try:
            success, msg = await panel.withdraw_balance(min_amount)
        finally:
            await panel.close()
        result_text = f"💸 <b>Авто-вывод</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual withdraw error for user %s: %s", uid, e)
        result_text = f"❌ Ошибка при выводе: {e}"
    s = get_settings(uid)
    creds = get_panel_creds(uid)
    await callback.message.edit_text(
        result_text + "\n\n" + _withdraw_text(s, creds),
        reply_markup=_withdraw_kb(s, creds),
    )
