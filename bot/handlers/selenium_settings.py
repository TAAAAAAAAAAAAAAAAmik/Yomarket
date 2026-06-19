"""Handlers for Playwright/browser automation features: auto-bump, auto-restore, auto-withdraw."""
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

# Stores active login sessions: user_id -> (panel, page, context)
_login_sessions: dict[int, tuple] = {}


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class SeleniumState(StatesGroup):
    waiting_phone = State()
    waiting_sms_code = State()
    waiting_bump_interval = State()
    waiting_withdraw_amount = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _fmt_ts(ts) -> str:
    """Format a Unix timestamp or ISO string to human-readable."""
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
# Setup flow: enter panel login and password
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "selenium:setup:start")
async def setup_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SeleniumState.waiting_phone)
    await callback.message.edit_text(
        "📱 <b>Вход в панель YooMarket</b>\n\n"
        "Введи номер телефона или e-mail от аккаунта на <b>panel.yoomarket.net</b>:\n\n"
        "<i>Бот откроет браузер, заполнит форму и запросит SMS-код</i>",
        reply_markup=_cancel_kb("auto:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_phone)
async def setup_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not phone:
        await message.answer("❌ Введи номер телефона или e-mail:")
        return

    uid = message.from_user.id
    wait_msg = await message.answer("⏳ Открываю браузер и захожу на страницу входа...")

    try:
        from automation.panel import YooMarketPanel
        panel = YooMarketPanel()
        await panel.start()
        page, context = await panel.open_login_page()

        ok, err = await panel.submit_phone(page, phone)

        if not ok:
            await panel.close()
            await wait_msg.edit_text(f"❌ {err}\n\nПопробуй ещё раз — введи номер телефона:")
            return

        if err == "__already_logged_in__":
            cookies = await context.cookies()
            cookie_string = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies
                if c.get("domain", "").endswith("yoomarket.net")
            ) or "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            save_panel_creds(uid, {"cookie_string": cookie_string})
            await panel.close()
            await state.clear()
            await wait_msg.edit_text(
                "✅ <b>Вошли без SMS!</b>\n\nАвто-функции через браузер готовы.",
                reply_markup=_no_creds_kb(),
            )
            return

        # SMS code field appeared — store session and ask for code
        _login_sessions[uid] = (panel, page, context)
        await state.set_state(SeleniumState.waiting_sms_code)
        await wait_msg.edit_text(
            f"📨 <b>SMS отправлено</b> на <code>{phone}</code>\n\n"
            "Введи код из SMS:",
            reply_markup=_cancel_kb("selenium:cancel_login"),
        )

    except Exception as e:
        logger.error("setup_phone error: %s", e)
        await wait_msg.edit_text(f"❌ Ошибка браузера: {e}\n\nПопробуй ещё раз:")


@router.callback_query(F.data == "selenium:cancel_login")
async def cancel_login(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    if uid in _login_sessions:
        panel, page, context = _login_sessions.pop(uid)
        try:
            await page.close()
            await context.close()
            await panel.close()
        except Exception:
            pass
    await state.clear()
    await callback.message.edit_text("❌ Вход отменён.", reply_markup=_no_creds_kb())
    await callback.answer()


@router.message(SeleniumState.waiting_sms_code)
async def setup_sms_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    uid = message.from_user.id

    if uid not in _login_sessions:
        await message.answer("❌ Сессия входа истекла. Начни заново.", reply_markup=_no_creds_kb())
        await state.clear()
        return

    wait_msg = await message.answer("⏳ Проверяю код...")
    panel, page, context = _login_sessions.pop(uid)

    try:
        ok, result = await panel.submit_code(page, context, code)
    except Exception as e:
        ok, result = False, str(e)

    try:
        await page.close()
        await context.close()
        await panel.close()
    except Exception:
        pass

    await state.clear()

    if ok:
        save_panel_creds(uid, {"cookie_string": result})
        b = InlineKeyboardBuilder()
        b.button(text="✅ Проверить сессию", callback_data="selenium:check_session")
        b.button(text="⬅️ Назад", callback_data="auto:menu")
        b.adjust(1)
        await wait_msg.edit_text(
            "✅ <b>Вход выполнен!</b>\n\nСессия сохранена. Авто-функции через браузер готовы к работе.",
            reply_markup=b.as_markup(),
        )
    else:
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Попробовать снова", callback_data="selenium:setup:start")
        b.button(text="⬅️ Назад", callback_data="auto:menu")
        b.adjust(1)
        await wait_msg.edit_text(
            f"❌ <b>Ошибка входа</b>\n\n{result}",
            reply_markup=b.as_markup(),
        )


@router.callback_query(F.data == "selenium:check_session")
async def check_session(callback: CallbackQuery) -> None:
    await callback.message.edit_text("⏳ Проверяю сессию...")
    creds = get_panel_creds(callback.from_user.id)
    if not creds:
        await callback.message.edit_text("❌ Сессия не настроена.", reply_markup=_no_creds_kb())
        await callback.answer()
        return
    try:
        from automation.panel import YooMarketPanel
        panel = YooMarketPanel(creds.get("cookie_string", ""))
        await panel.start()
        ok = await panel.check_session()
        await panel.close()
    except Exception as e:
        ok = False
        logger.error("Session check error: %s", e)
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Войти заново", callback_data="selenium:setup:start")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(1)
    if ok:
        await callback.message.edit_text(
            "✅ <b>Сессия активна!</b>\n\nАвто-функции через браузер работают.",
            reply_markup=b.as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Сессия истекла</b>\n\nНужно войти заново.",
            reply_markup=b.as_markup(),
        )
    await callback.answer()


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
        "Авто-поднятие ставит все объявления на верх каждые N часов через браузер.",
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
    b.button(text=f"⏱ Интервал: {interval}ч", callback_data="selenium:bump:set_interval")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.button(text="🔑 Сменить аккаунт", callback_data="selenium:setup:start")
    if not creds:
        b.button(text="🔑 Настроить вход", callback_data="selenium:setup:start")
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
            raise ValueError("Too small")
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
        panel = YooMarketPanel(creds["login"], creds["password"])
        await panel.start()
        try:
            count, msg = await panel.bump_all_ads()
        finally:
            await panel.close()
        # Update last_bump_run timestamp
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
        "Авто-восстановление переактивирует проданные или истёкшие объявления через браузер.",
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
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.button(text="🔑 Сменить аккаунт", callback_data="selenium:setup:start")
    if not creds:
        b.button(text="🔑 Настроить вход", callback_data="selenium:setup:start")
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
        panel = YooMarketPanel(creds["login"], creds["password"])
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
        "Авто-вывод переводит баланс, когда он превышает указанный порог.",
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
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.button(text="🔑 Сменить аккаунт", callback_data="selenium:setup:start")
    if not creds:
        b.button(text="🔑 Настроить вход", callback_data="selenium:setup:start")
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
            raise ValueError("Too small")
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
        panel = YooMarketPanel(creds["login"], creds["password"])
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
