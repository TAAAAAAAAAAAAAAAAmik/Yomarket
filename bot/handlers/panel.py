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

# Хранит активные Playwright-сессии для входа: {user_id: {"panel", "page", "context"}}
_login_sessions: dict[int, dict] = {}


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
        b.button(text="🔄 Проверить сессию", callback_data="panel:check")
        if has_token:
            b.button(text="🔑 Обновить через токен", callback_data="panel:token_login")
        b.button(text="📧 Обновить вход (email)", callback_data="panel:sms_start")
        b.button(text="🍪 Обновить cookies вручную", callback_data="panel:cookies_start")
        b.button(text="🚪 Выйти из панели", callback_data="panel:logout")
    else:
        if has_token:
            b.button(text="🔑 Войти автоматически (через токен)", callback_data="panel:token_login")
        b.button(text="📧 Войти через email", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies вручную", callback_data="panel:cookies_start")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    b.adjust(1)
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

    # Закрываем старую сессию
    old = _login_sessions.pop(uid, None)
    if old:
        try:
            await old["panel"].close()
        except Exception:
            pass

    status_msg = await message.answer("⏳ Открываю страницу входа...")

    panel = YooMarketPanel()
    try:
        await asyncio.wait_for(panel.start(), timeout=15)
        page, context = await asyncio.wait_for(panel.open_login_page(), timeout=30)
    except asyncio.TimeoutError:
        try:
            await panel.close()
        except Exception:
            pass
        await status_msg.edit_text(
            "⏳ Браузер не успел запуститься.\n\n"
            "На сервере нужно выполнить: <code>playwright install chromium</code>\n\n"
            "Пока используйте вход через cookies 👇"
        )
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        await state.clear()
        return
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка браузера: {e}\n\nИспользуйте вход через cookies 👇"
        )
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        await state.clear()
        return

    await status_msg.edit_text("⏳ Отправляю код на почту...")
    try:
        ok, err = await asyncio.wait_for(panel.submit_email(page, email), timeout=20)
    except asyncio.TimeoutError:
        ok, err = False, "Страница не ответила вовремя"

    if not ok:
        await panel.close()
        await state.clear()
        await status_msg.edit_text(f"❌ {err}\n\nИспользуйте вход через cookies 👇")
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    _login_sessions[uid] = {"panel": panel, "page": page, "context": context}
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
    sess = _login_sessions.get(uid)
    if not sess:
        await state.clear()
        await message.answer(
            "❌ Сессия входа истекла. Начните заново.",
            reply_markup=_cancel_kb("panel:menu"),
        )
        return

    status_msg = await message.answer("⏳ Проверяю код...")
    ok, result = await sess["panel"].submit_code(sess["page"], sess["context"], code)

    await sess["panel"].close()
    _login_sessions.pop(uid, None)

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
    await status_msg.edit_text(
        "✅ <b>Успешно вошли в панель!</b>\n\n"
        "Теперь доступны функции авто-поднятия и авто-восстановления товаров."
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

    # Быстрая проверка что куки рабочие
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
        # Сохраняем в любом случае — может check_session даёт ложный результат
        save_panel_creds(uid, {"login": "", "cookies": cookies})
        await status_msg.edit_text(
            "⚠️ Cookies сохранены, но проверка дала неоднозначный результат.\n"
            "Попробуйте запустить авто-функцию — если работает, всё в порядке."
        )

    creds = get_panel_creds(uid)
    await message.answer(_status_text(creds), reply_markup=_menu_kb(creds))
