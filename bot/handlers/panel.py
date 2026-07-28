from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from automation.panel import (
    YooMarketPanel, YooMarketPanelHTTP, PanelSession, try_token_login,
)
from storage import get_panel_creds, save_panel_creds, delete_panel_creds, get_token

logger = logging.getLogger(__name__)
router = Router()

# {user_id: YooMarketPanelHTTP} — live HTTP session kept between the password
# step and the code step, so the code is verified on the same login attempt
# (holds cookies + CSRF, no browser).
_login_sessions: dict[int, YooMarketPanelHTTP] = {}


class PanelState(StatesGroup):
    # Main flow: email → code mailed by the marketplace.
    waiting_email = State()
    waiting_code = State()
    # Fallback: the panel's own password login.
    waiting_pw_email = State()
    waiting_pw_password = State()
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
        b.button(text="📧 Вход по коду", callback_data="panel:sms_start")
        b.button(text="🍪 Обновить cookies", callback_data="panel:cookies_start")
        b.button(text="🔍 Найти адрес входа", callback_data="panel:probe_ui")
        if has_token:
            b.button(text="🎟 Через токен", callback_data="panel:token_login")
    else:
        b.button(text="📧 Вход по коду", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="🔍 Найти адрес входа", callback_data="panel:probe_ui")
        if has_token:
            b.button(text="🎟 Через токен", callback_data="panel:token_login")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    # 2 columns for actions, "⬅️ Настройки" on its own row at the bottom
    if creds:
        b.adjust(2, 2, 1, 1, 1) if has_token else b.adjust(2, 2, 1, 1)
    else:
        b.adjust(2, 1, 1, 1) if has_token else b.adjust(2, 1, 1)
    return b.as_markup()


def _cancel_kb(back: str = "panel:menu"):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _safe_edit(msg: Message, text: str, **kwargs) -> None:
    """Edit a message, falling back to unformatted text if Telegram rejects the
    markup. Diagnostics carry page dumps and URLs; one stray tag used to make
    the send fail, leaving the user looking at a frozen "in progress" message."""
    try:
        await msg.edit_text(text, **kwargs)
        return
    except Exception as e:
        logger.warning("edit_text failed (%s), retrying as plain text", e)
    try:
        import html as _html
        plain = _html.unescape(re.sub(r"<[^>]+>", "", text))
        await msg.edit_text(plain[:4000], parse_mode=None, **kwargs)
    except Exception:
        logger.exception("edit_text failed even as plain text")


async def _refresh_menu(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    has_token = bool(get_token(uid))
    await callback.message.edit_text(
        _status_text(creds, has_token),
        reply_markup=_menu_kb(creds, has_token),
    )
    await callback.answer()


async def _finish_login(
    message: Message, status_msg: Message, uid: int, login: str, cookies: str,
) -> None:
    """Store the fresh panel session and confirm it works against the Nova API.
    Shared by both login flows (code and password)."""
    save_panel_creds(uid, {"login": login, "cookies": cookies})

    from automation.panel import panel_check_session_sync
    loop = asyncio.get_event_loop()
    try:
        api_ok, api_detail = await asyncio.wait_for(
            loop.run_in_executor(None, panel_check_session_sync, cookies),
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
    await message.answer(
        _status_text(creds, has_token), reply_markup=_menu_kb(creds, has_token)
    )


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
# OTP login (the panel's real flow): step 1 — email → send code
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:sms_start")
async def panel_email_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_email)
    await callback.message.edit_text(
        "📧 <b>Вход по коду</b>\n\n"
        "Введите <b>email</b> от аккаунта YooMarket — на него придёт код:\n\n"
        "<i>Пример: mail@example.com</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_email)
async def panel_email_input(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not email or "@" not in email:
        await message.answer("❌ Введите корректный email адрес")
        return

    uid = message.from_user.id
    old = _login_sessions.pop(uid, None)
    if old:
        try:
            await old.close()
        except Exception:
            pass

    status_msg = await message.answer(
        "⏳ Открываю страницу входа в браузере...\n"
        "<i>Первый запуск может занять до минуты.</i>"
    )

    # Driven with a real browser: the site's login cannot be reproduced with
    # plain requests, and the browser also records the requests it makes, which
    # is what the HTTP version will later be built from.
    panel = YooMarketPanel()
    page = context = None
    try:
        await asyncio.wait_for(panel.start(), timeout=60)
        page, context = await asyncio.wait_for(panel.open_login_page(), timeout=120)
        await status_msg.edit_text("⏳ Ввожу email и запрашиваю код...")
        ok, err = await asyncio.wait_for(panel.submit_email(page, email), timeout=60)
    except asyncio.TimeoutError:
        ok, err = False, "Браузер не успел загрузить страницу входа."
    except Exception as e:
        emsg = str(e)
        if any(k in emsg.lower() for k in
               ("executable", "playwright", "browser", "chromium")):
            err = ("Браузер не установлен в этом образе.\n"
                   "Нужна пересборка с Chromium — она уже в коде, "
                   "дождитесь окончания деплоя.")
        elif "memory" in emsg.lower() or "killed" in emsg.lower():
            err = ("Браузеру не хватило памяти на бесплатном тарифе (512 МБ).\n"
                   "Нужен тариф побольше или вход через cookies.")
        else:
            err = f"Ошибка браузера:\n<code>{emsg[:300]}</code>"
        ok = False

    if not ok:
        try:
            await panel.close()
        except Exception:
            pass
        await state.clear()
        seen = "\n".join(panel.captured[:15])
        pages = "\n".join(panel.page_debug[:3])
        extra = f"\n\n<b>Запросы:</b>\n<code>{seen[:700]}</code>" if seen else ""
        if pages:
            extra += f"\n\n<b>Что на странице:</b>\n<code>{pages[:700]}</code>"
        await _safe_edit(status_msg, f"❌ {err or 'Не удалось отправить код'}{extra}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    _login_sessions[uid] = (panel, page, context)
    await state.update_data(email=email)
    await state.set_state(PanelState.waiting_code)
    await status_msg.edit_text(
        "✅ <b>Код отправлен на почту!</b>\n\n"
        "Введите <b>код из письма</b>:",
        reply_markup=_cancel_kb(),
    )


# ---------------------------------------------------------------------------
# Password login — kept for the panel's own admin login
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:pw_start")
async def panel_pw_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_pw_email)
    await callback.message.edit_text(
        "🔑 <b>Вход по паролю</b>\n\n"
        "Запасной вариант, если у аккаунта задан пароль.\n\n"
        "Введите <b>email</b>:",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_pw_email)
async def panel_pw_email_input(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not email or "@" not in email:
        await message.answer("❌ Введите корректный email адрес")
        return

    await state.update_data(email=email)
    await state.set_state(PanelState.waiting_pw_password)
    await message.answer(
        "🔑 Теперь введите <b>пароль</b>:\n\n"
        "<i>Сообщение с паролем будет удалено автоматически.</i>",
        reply_markup=_cancel_kb(),
    )


# ---------------------------------------------------------------------------
# OTP login: step 2 — verify the code from the letter
# ---------------------------------------------------------------------------

@router.message(PanelState.waiting_code)
async def panel_code_input(message: Message, state: FSMContext) -> None:
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
        ok, result = await asyncio.wait_for(
            panel.submit_code(page, context, code), timeout=60
        )
    except asyncio.TimeoutError:
        ok, result = False, "Превышено время ожидания. Попробуйте ещё раз."
    except Exception as e:
        ok, result = False, f"Ошибка запроса: {str(e)[:200]}"

    # What the page actually called — this is the recipe for the HTTP version.
    seen = "\n".join(panel.captured[:15])
    logger.info("LOGIN REQUESTS:\n%s", seen)

    _login_sessions.pop(uid, None)
    try:
        await panel.close()
    except Exception:
        pass

    data = await state.get_data()
    email = data.get("email", "")
    await state.clear()

    if not ok:
        extra = f"\n\n<b>Запросы страницы:</b>\n<code>{seen[:900]}</code>" if seen else ""
        await _safe_edit(status_msg, f"❌ {result}{extra}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    await _finish_login(message, status_msg, uid, email, result)
    if seen:
        await message.answer(
            "🔎 <b>Запросы, которые сделал вход</b> — по ним сделаем "
            f"версию без браузера:\n<code>{seen[:900]}</code>"
        )


# ---------------------------------------------------------------------------
# Login step 2 — password → POST /login (may hand off to the code step)
# ---------------------------------------------------------------------------

@router.message(PanelState.waiting_pw_password)
async def panel_password_input(message: Message, state: FSMContext) -> None:
    password = message.text or ""
    if not password.strip():
        await message.answer("❌ Введите пароль")
        return

    data = await state.get_data()
    email = data.get("email", "")
    uid = message.from_user.id

    # Wipe the password message so the plaintext secret doesn't linger in chat.
    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer("⏳ Вхожу в панель...")

    old = _login_sessions.pop(uid, None)
    if old:
        try:
            await old.close()
        except Exception:
            pass

    http = YooMarketPanelHTTP()
    try:
        await http.start()
        status, result = await asyncio.wait_for(
            http.login_password(email, password), timeout=45
        )
    except asyncio.TimeoutError:
        status, result = "error", "Превышено время ожидания. Попробуйте ещё раз."
    except Exception as e:
        status, result = "error", f"Ошибка запроса: {str(e)[:200]}"

    # The panel wants the emailed code as a second factor — keep the session
    # alive so the code lands on the very same login attempt.
    if status == "code":
        _login_sessions[uid] = http
        await state.set_state(PanelState.waiting_code)
        await status_msg.edit_text(
            f"📨 <b>{result}</b>\n\nВведите <b>код из письма</b>:",
            reply_markup=_cancel_kb(),
        )
        return

    try:
        await http.close()
    except Exception:
        pass
    await state.clear()

    if status != "ok":
        await status_msg.edit_text(f"❌ {result}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="🔍 Что на странице входа", callback_data="panel:probe_ui")
        b.button(text="🍪 Вставить cookies", callback_data="panel:cookies_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        b.adjust(1)
        await message.answer("Выберите действие:", reply_markup=b.as_markup())
        return

    await _finish_login(message, status_msg, uid, email, result)


# ---------------------------------------------------------------------------
# Diagnostics: what the panel's login screen is actually made of
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:probe_ui")
async def panel_probe_ui(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Ищу адрес входа...")
    status_msg = await callback.message.answer(
        "⏳ Читаю код страницы входа yoomarket.net...")

    http = YooMarketPanelHTTP()
    try:
        await http.start()
        report = await asyncio.wait_for(http.probe_login_ui(), timeout=90)
    except asyncio.TimeoutError:
        report = "не успел за 90 секунд"
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    finally:
        try:
            await http.close()
        except Exception:
            pass

    await _safe_edit(
        status_msg,
        f"🔍 <b>Адреса входа yoomarket.net</b>\n\n<code>{report[:3500]}</code>",
    )


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
