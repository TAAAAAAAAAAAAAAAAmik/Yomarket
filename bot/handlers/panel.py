from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from automation.panel import YooMarketPanel, PanelSession, try_token_login
from storage import get_panel_creds, save_panel_creds, delete_panel_creds, get_token

logger = logging.getLogger(__name__)
router = Router()

# {user_id: (YooMarketPanel, page, context)}
_login_sessions: dict[int, tuple] = {}


class PanelState(StatesGroup):
    waiting_phone = State()
    waiting_sms_code = State()
    waiting_cookies = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_text(creds: dict | None, has_token: bool = False) -> str:
    if not creds:
        hint = "\n\n💡 У вас есть API-токен — попробуйте <b>автоматический вход</b>." if has_token else ""
        return f"🔒 <b>Панель YooMarket</b>\n\nВы <b>не авторизованы</b> в панели продавца.{hint}"
    login = creds.get("login", "")
    login_part = f"\n👤 Логин: <b>{login}</b>" if login else ""
    return (
        f"✅ <b>Панель YooMarket</b>\n"
        f"Вы <b>авторизованы</b> в панели продавца.{login_part}\n\n"
        f"<i>Если автоматизация не работает — обновите вход.</i>"
    )


def _menu_kb(creds: dict | None, has_token: bool = False):
    b = InlineKeyboardBuilder()
    if creds:
        b.button(text="🔄 Проверить", callback_data="panel:check")
        b.button(text="🚪 Выйти", callback_data="panel:logout")
        b.button(text="📧 Вход по email", callback_data="panel:sms_start")
        b.button(text="🍪 Обновить cookies", callback_data="panel:cookies_start")
        if has_token:
            b.button(text="🔑 Через токен", callback_data="panel:token_login")
    else:
        b.button(text="📧 Вход по email", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        if has_token:
            b.button(text="🔑 Через токен", callback_data="panel:token_login")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    # 2 columns for actions, "⬅️ Настройки" on its own row at the bottom
    if creds:
        b.adjust(2, 2, 1, 1) if has_token else b.adjust(2, 2, 1)
    else:
        b.adjust(2, 1, 1) if has_token else b.adjust(2, 1)
    return b.as_markup()


def _cancel_kb(back: str = "panel:menu"):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _refresh_menu(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    has_token = bool(get_token(uid))
    await callback.message.edit_text(
        _status_text(creds, has_token),
        reply_markup=_menu_kb(creds, has_token),
    )
    await callback.answer()


async def _close_session(uid: int) -> None:
    """Close and remove any active Playwright session for the user."""
    entry = _login_sessions.pop(uid, None)
    if entry:
        panel, _, _ = entry
        try:
            await panel.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:menu")
async def panel_menu(callback: CallbackQuery) -> None:
    await _refresh_menu(callback)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:logout")
async def panel_logout(callback: CallbackQuery) -> None:
    delete_panel_creds(callback.from_user.id)
    await callback.answer("✅ Вышли из панели", show_alert=True)
    await _refresh_menu(callback)


# ---------------------------------------------------------------------------
# Session check
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:check")
async def panel_check(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Проверяю...", show_alert=False)
    creds = get_panel_creds(callback.from_user.id)
    if not creds or not creds.get("cookies"):
        await callback.message.edit_text(
            "❌ Нет сохранённых куков. Войдите заново.",
            reply_markup=_menu_kb(None),
        )
        return

    ps = PanelSession(creds["cookies"])
    await ps.start()
    try:
        ok = await ps.check_session()
    finally:
        await ps.close()

    if ok:
        await callback.message.answer("✅ Сессия <b>активна</b> — всё работает!")
    else:
        await callback.message.answer(
            "⚠️ Сессия <b>истекла</b>.\n\nВойдите заново через email или вставьте новые cookies."
        )
    await _refresh_menu(callback)


# ---------------------------------------------------------------------------
# Auto login via API token
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:token_login")
async def panel_token_login(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    token = get_token(uid)
    if not token:
        await callback.answer("❌ API-токен не найден", show_alert=True)
        return

    await callback.answer()
    status_msg = await callback.message.answer("🔑 Пробую войти в панель через API-токен...")

    ok, result = await try_token_login(token)

    if ok and result:
        save_panel_creds(uid, {"login": "", "cookies": result})
        await status_msg.edit_text("✅ <b>Успешно!</b> Вошли в панель через API-токен.")
    else:
        await status_msg.edit_text(
            "⚠️ <b>Автоматический вход не сработал.</b>\n\n"
            "Панель использует отдельную авторизацию.\n"
            "Попробуйте войти через email или вставить cookies вручную."
        )

    creds = get_panel_creds(uid)
    has_token = True
    await callback.message.answer(_status_text(creds, has_token), reply_markup=_menu_kb(creds, has_token))


# ---------------------------------------------------------------------------
# Email login: step 1 — ask for email
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:sms_start")
async def panel_email_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_phone)
    await callback.message.edit_text(
        "📧 <b>Вход через email</b>\n\n"
        "Введите электронную почту, привязанную к аккаунту YooMarket:\n\n"
        "<i>Пример: mail@example.com</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_phone)
async def panel_email_input(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not email or "@" not in email:
        await message.answer("❌ Введите корректный email адрес")
        return

    uid = message.from_user.id
    await _close_session(uid)

    status_msg = await message.answer("⏳ Открываю браузер и страницу входа...")

    panel = YooMarketPanel()
    page = None
    context = None

    try:
        await asyncio.wait_for(panel.start(), timeout=20)

        await status_msg.edit_text("⏳ Загружаю страницу входа panel.yoomarket.net...")
        page, context = await asyncio.wait_for(panel.open_login_page(), timeout=30)

        await status_msg.edit_text("⏳ Ввожу email и нажимаю «Получить код»...")
        ok, err = await asyncio.wait_for(panel.submit_email(page, email), timeout=20)

    except asyncio.TimeoutError:
        await panel.close()
        await state.clear()
        await status_msg.edit_text(
            "⏰ <b>Превышено время ожидания.</b>\n\n"
            "Браузер не успел загрузить страницу. Попробуйте вставить cookies вручную."
        )
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    except Exception as e:
        await panel.close()
        await state.clear()
        emsg = str(e).lower()
        if "executable" in emsg or "playwright" in emsg or "browser" in emsg or "chromium" in emsg:
            # Chromium not installed (lean free-hosting image) — guide to cookies
            await status_msg.edit_text(
                "ℹ️ <b>Вход по email недоступен на этом хостинге</b> "
                "(нет браузера).\n\n"
                "Используйте <b>🍪 Вставить cookies</b> — это надёжнее и работает "
                "везде. Как получить cookies — покажу по кнопке ниже."
            )
        else:
            await status_msg.edit_text(f"❌ Ошибка при открытии браузера:\n<code>{str(e)[:300]}</code>")
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    if not ok:
        await panel.close()
        await state.clear()
        await status_msg.edit_text(f"❌ {err or 'Не удалось отправить код'}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    _login_sessions[uid] = (panel, page, context)
    await state.update_data(email=email)
    await state.set_state(PanelState.waiting_sms_code)
    await status_msg.edit_text(
        "✅ <b>Код отправлен на почту!</b>\n\n"
        "Введите <b>код из письма</b>:",
        reply_markup=_cancel_kb(),
    )


# ---------------------------------------------------------------------------
# Email login: step 2 — verify code
# ---------------------------------------------------------------------------

@router.message(PanelState.waiting_sms_code)
async def panel_email_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("❌ Введите код из письма")
        return

    uid = message.from_user.id
    entry = _login_sessions.get(uid)
    if not entry:
        await state.clear()
        await message.answer(
            "❌ Сессия входа истекла. Начните заново.",
            reply_markup=_cancel_kb("panel:menu"),
        )
        return

    panel, page, context = entry
    status_msg = await message.answer("⏳ Проверяю код...")

    try:
        ok, result = await asyncio.wait_for(panel.submit_code(page, context, code), timeout=18)
    except asyncio.TimeoutError:
        ok, result = False, "Превышено время ожидания. Попробуйте ещё раз."
    finally:
        _login_sessions.pop(uid, None)
        try:
            await asyncio.wait_for(panel.close(), timeout=5)
        except Exception:
            pass

    if not ok:
        await state.clear()
        await status_msg.edit_text(f"❌ {result}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    data = await state.get_data()
    email = data.get("email", "")
    await state.clear()
    save_panel_creds(uid, {"login": email, "cookies": result})

    # Immediately verify the cookies actually work for the Nova API
    from automation.panel import panel_check_session_sync
    loop = asyncio.get_event_loop()
    try:
        api_ok, api_detail = await asyncio.wait_for(
            loop.run_in_executor(None, panel_check_session_sync, result),
            timeout=30,
        )
    except Exception as e:
        api_ok, api_detail = False, f"проверка не удалась: {str(e)[:60]}"

    if api_ok:
        await status_msg.edit_text(
            "✅ <b>Успешно вошли в панель!</b>\n"
            "🔬 Куки проверены — API панели отвечает.\n\n"
            "Теперь доступно создание товаров и авто-функции."
        )
    else:
        await status_msg.edit_text(
            "⚠️ <b>Вход выполнен, но API панели не принял куки.</b>\n\n"
            f"Проверка:\n<code>{api_detail}</code>\n\n"
            "Создание товаров может не работать — пришлите этот текст разработчику."
        )
    creds = get_panel_creds(uid)
    has_token = bool(get_token(uid))
    await message.answer(_status_text(creds, has_token), reply_markup=_menu_kb(creds, has_token))


# ---------------------------------------------------------------------------
# Manual cookies
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:cookies_start")
async def panel_cookies_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_cookies)
    await callback.message.edit_text(
        "🍪 <b>Вставить cookies вручную</b>\n\n"
        "<b>С компьютера (проще):</b>\n"
        "1. Зайдите на <b>panel.yoomarket.net</b> и войдите\n"
        "2. Нажмите F12 → вкладка <b>Console</b>\n"
        "3. Введите <code>document.cookie</code> → Enter\n"
        "4. Скопируйте результат и отправьте сюда\n\n"
        "<b>С телефона (Chrome):</b>\n"
        "1. Войдите на <b>panel.yoomarket.net</b>\n"
        "2. В адресной строке введите:\n"
        "<code>javascript:copy(document.cookie)</code>\n"
        "3. Нажмите Enter — cookies скопируются в буфер\n"
        "4. Вставьте сюда\n\n"
        "<i>Или просто введите строку вида: key=value; key2=value2</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_cookies)
async def panel_cookies_save(message: Message, state: FSMContext) -> None:
    cookies = (message.text or "").strip()
    if not cookies or "=" not in cookies:
        await message.answer("❌ Неверный формат. Должна быть строка вида: key=value; key2=value2")
        return

    await state.clear()
    uid = message.from_user.id

    status_msg = await message.answer("⏳ Проверяю cookies...")
    ps = PanelSession(cookies)
    await ps.start()
    try:
        ok = await ps.check_session()
    finally:
        await ps.close()

    if ok:
        save_panel_creds(uid, {"login": "", "cookies": cookies})
        await status_msg.edit_text("✅ <b>Cookies сохранены и проверены!</b>")
    else:
        save_panel_creds(uid, {"login": "", "cookies": cookies})
        await status_msg.edit_text(
            "⚠️ Cookies сохранены, но проверка дала неоднозначный результат.\n"
            "Попробуйте запустить авто-функцию — если работает, всё в порядке."
        )

    creds = get_panel_creds(uid)
    await message.answer(_status_text(creds), reply_markup=_menu_kb(creds))
