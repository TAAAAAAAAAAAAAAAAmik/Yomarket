from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from automation.panel import YooMarketPanelHTTP, PanelSession
from storage import get_panel_creds, save_panel_creds, delete_panel_creds

logger = logging.getLogger(__name__)
router = Router()

# Хранит активные HTTP-сессии для входа: {user_id: YooMarketPanelHTTP}
_login_sessions: dict[int, YooMarketPanelHTTP] = {}


class PanelState(StatesGroup):
    waiting_phone = State()
    waiting_sms_code = State()
    waiting_cookies = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_text(creds: dict | None) -> str:
    if not creds:
        return "🔒 <b>Панель YooMarket</b>\n\nВы <b>не авторизованы</b> в панели продавца."
    login = creds.get("login", "")
    login_part = f"\n👤 Логин: <b>{login}</b>" if login else ""
    return (
        f"✅ <b>Панель YooMarket</b>\n"
        f"Вы <b>авторизованы</b> в панели продавца.{login_part}\n\n"
        f"<i>Если автоматизация не работает — обновите куки.</i>"
    )


def _menu_kb(creds: dict | None):
    b = InlineKeyboardBuilder()
    if creds:
        b.button(text="🔄 Проверить сессию", callback_data="panel:check")
        b.button(text="📲 Обновить вход (SMS)", callback_data="panel:sms_start")
        b.button(text="🍪 Обновить cookies вручную", callback_data="panel:cookies_start")
        b.button(text="🚪 Выйти из панели", callback_data="panel:logout")
    else:
        b.button(text="📲 Войти через SMS", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies вручную", callback_data="panel:cookies_start")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    b.adjust(1)
    return b.as_markup()


def _cancel_kb(back: str = "panel:menu"):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _refresh_menu(callback: CallbackQuery) -> None:
    creds = get_panel_creds(callback.from_user.id)
    await callback.message.edit_text(_status_text(creds), reply_markup=_menu_kb(creds))
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
            "⚠️ Сессия <b>истекла</b>.\n\nВойдите заново через SMS или вставьте новые cookies."
        )
    await _refresh_menu(callback)


# ---------------------------------------------------------------------------
# SMS login: step 1 — ask for phone
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:sms_start")
async def panel_sms_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_phone)
    await callback.message.edit_text(
        "📲 <b>Вход через SMS</b>\n\n"
        "Введите номер телефона или email, привязанный к аккаунту YooMarket:\n\n"
        "<i>Пример: +79001234567 или mail@example.com</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_phone)
async def panel_sms_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not phone:
        await message.answer("❌ Введите номер телефона или email")
        return

    uid = message.from_user.id
    # Закрываем старую сессию если была
    if uid in _login_sessions:
        try:
            await _login_sessions[uid].close()
        except Exception:
            pass

    http = YooMarketPanelHTTP()
    await http.start()
    _login_sessions[uid] = http

    status_msg = await message.answer("⏳ Отправляю SMS...")

    ok, err = await http.send_sms(phone)

    if not ok:
        await http.close()
        _login_sessions.pop(uid, None)
        await state.clear()
        await status_msg.edit_text(
            f"{err}\n\n"
            "Нажмите <b>«Вставить cookies вручную»</b> как альтернативный способ.",
        )
        b = InlineKeyboardBuilder()
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    await state.update_data(phone=phone)
    await state.set_state(PanelState.waiting_sms_code)
    await status_msg.edit_text(
        "✅ SMS отправлена!\n\n"
        "Введите <b>код из SMS</b>:",
        reply_markup=_cancel_kb(),
    )


# ---------------------------------------------------------------------------
# SMS login: step 2 — verify code
# ---------------------------------------------------------------------------

@router.message(PanelState.waiting_sms_code)
async def panel_sms_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("❌ Введите код из SMS")
        return

    uid = message.from_user.id
    http = _login_sessions.get(uid)
    if not http:
        await state.clear()
        await message.answer(
            "❌ Сессия входа истекла. Начните заново.",
            reply_markup=_cancel_kb("panel:menu"),
        )
        return

    status_msg = await message.answer("⏳ Проверяю код...")
    ok, result = await http.verify_code(code)

    await http.close()
    _login_sessions.pop(uid, None)
    await state.clear()

    if not ok:
        await status_msg.edit_text(f"❌ {result}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    data = await state.get_data()
    phone = data.get("phone", "")
    save_panel_creds(uid, {"login": phone, "cookies": result})
    await status_msg.edit_text(
        "✅ <b>Успешно вошли в панель!</b>\n\n"
        "Теперь доступны функции авто-поднятия и авто-восстановления товаров."
    )
    creds = get_panel_creds(uid)
    await message.answer(_status_text(creds), reply_markup=_menu_kb(creds))


# ---------------------------------------------------------------------------
# Manual cookies
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:cookies_start")
async def panel_cookies_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_cookies)
    await callback.message.edit_text(
        "🍪 <b>Вставить cookies вручную</b>\n\n"
        "Как получить cookies:\n"
        "1. Зайдите на <b>panel.yoomarket.net</b> с компьютера\n"
        "2. Войдите в аккаунт\n"
        "3. Нажмите F12 → вкладка <b>Console</b>\n"
        "4. Введите <code>document.cookie</code> и нажмите Enter\n"
        "5. Скопируйте весь результат и отправьте сюда\n\n"
        "<i>Строка будет выглядеть примерно так: session=abc123; token=xyz789</i>",
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
