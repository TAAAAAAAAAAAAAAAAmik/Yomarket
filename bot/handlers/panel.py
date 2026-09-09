from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from automation.panel import YooMarketPanelHTTP, PanelSession, try_token_login
from storage import get_panel_creds, save_panel_creds, delete_panel_creds, get_token

logger = logging.getLogger(__name__)
router = Router()

# {продавец: YooMarketPanelHTTP} — живая HTTP-сессия между «выслать код» и
# «ввести код»: проверять код надо на той же сессии, потому что в ней лежат
# куки и CSRF-токен, которые выдала панель.
_login_sessions: dict[int, YooMarketPanelHTTP] = {}


class PanelState(StatesGroup):
    # почта → код, который панель пришлёт письмом
    waiting_email = State()
    waiting_code = State()
    waiting_cookies = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_text(creds: dict | None, has_token: bool = False) -> str:
    if not creds:
        return ("🔒 <b>Панель YooMarket</b>\n\n"
                "Ты <b>не авторизован</b> в панели продавца.\n\n"
                "Вход по твоей почте: придёт код, введёшь его здесь.\n"
                "<i>Пароль не нужен.</i>")
    login = creds.get("login", "")
    login_part = f"\n👤 Логин: <b>{login}</b>" if login else ""
    return (
        f"✅ <b>Панель YooMarket</b>\n"
        f"Ты <b>авторизован</b> в панели продавца.{login_part}\n\n"
        f"<i>Если автоматизация не работает — обнови вход.</i>"
    )


def _menu_kb(creds: dict | None, has_token: bool = False):
    """Только почта и код из письма.

    Вход по вставленным кукам и по токену убран намеренно. Просить продавца
    вставлять куки нельзя, и ни один из этих путей не даёт чат-токен,
    который панель выпускает при входе по почте, — а без него не работает
    ответ поддержке. Дорога одна, зато рабочая целиком.
    """
    b = InlineKeyboardBuilder()
    if creds:
        b.button(text="🔄 Проверить", callback_data="panel:check")
        b.button(text="🚪 Выйти", callback_data="panel:logout")
        b.button(text="📧 Войти заново по email", callback_data="panel:sms_start")
        b.button(text="⬅️ Настройки", callback_data="settings:menu")
        b.adjust(2, 1, 1)
    else:
        b.button(text="📧 Войти по email", callback_data="panel:sms_start")
        b.button(text="⬅️ Настройки", callback_data="settings:menu")
        ui.lay(b)
    return b.as_markup()


def _cancel_kb(back: str = "panel:menu"):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


async def _safe_edit(msg: Message, text: str, **kwargs) -> None:
    """Правка сообщения с откатом на текст без разметки.

    В диагностику попадают куски страниц и адреса; одного случайного тега
    хватало, чтобы отправка не прошла, — и продавец оставался смотреть на
    застывшее «выполняется»."""
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
    chat_token: str = "",
) -> None:
    """Сохранить свежую сессию панели и проверить её на Nova API.

    Общая часть обоих входов — по коду и по паролю: HTTP 200 на входе ещё не
    значит, что сессия рабочая, поэтому она сразу пробуется делом."""
    creds = {"login": login, "cookies": cookies}
    # Прежний чат-токен не трогаем, пока вход не выдал свежий
    if chat_token:
        creds["chat_token"] = chat_token
    elif (get_panel_creds(uid) or {}).get("chat_token"):
        creds["chat_token"] = get_panel_creds(uid)["chat_token"]
    save_panel_creds(uid, creds)

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
            "Создание товаров может не работать — пришли этот текст разработчику."
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
            "❌ Нет сохранённых куков. Войди заново.",
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
            "⚠️ Сессия <b>истекла</b>.\n\nВойди заново по email — придёт код."
        )
    await _refresh_menu(callback)


# ---------------------------------------------------------------------------
# Автовход по токену API
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
            "Попробуй войти через email или вставить cookies вручную."
        )

    creds = get_panel_creds(uid)
    has_token = True
    await callback.message.answer(_status_text(creds, has_token), reply_markup=_menu_kb(creds, has_token))


# ---------------------------------------------------------------------------
# Вход по одноразовому коду — настоящий путь панели. Шаг 1: почта → выслать код
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:sms_start")
async def panel_email_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_email)
    await callback.message.edit_text(
        "📧 <b>Вход по коду</b>\n\n"
        "Введи <b>email</b> от аккаунта YooMarket — на него придёт код:\n\n"
        "<i>Пример: mail@example.com</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(PanelState.waiting_email)
async def panel_email_input(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not email or "@" not in email:
        await message.answer("❌ Введи корректный email адрес")
        return

    uid = message.from_user.id
    old = _login_sessions.pop(uid, None)
    if old:
        try:
            await old.close()
        except Exception:
            pass

    status_msg = await message.answer("⏳ Запрашиваю код на почту...")

    # Обычный HTTP: адрес и тело запроса сняты с самого сайта (POST /token с
    # пустым полем "code"), поэтому браузер здесь не нужен.
    http = YooMarketPanelHTTP()
    try:
        await http.start()
        ok, err = await asyncio.wait_for(http.send_code(email), timeout=60)
    except asyncio.TimeoutError:
        ok, err = False, "Панель не ответила вовремя."
    except Exception as e:
        ok, err = False, f"Ошибка запроса: {str(e)[:200]}"

    if not ok:
        try:
            await http.close()
        except Exception:
            pass
        await state.clear()
        await _safe_edit(status_msg, f"❌ {err or 'Не удалось отправить код'}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        ui.lay(b)
        await message.answer("Выбери действие:", reply_markup=b.as_markup())
        return

    _login_sessions[uid] = http

    await state.update_data(email=email)
    await state.set_state(PanelState.waiting_code)
    await status_msg.edit_text(
        "✅ <b>Код отправлен на почту!</b>\n\n"
        "Введи <b>код из письма</b>:",
        reply_markup=_cancel_kb(),
    )



# ---------------------------------------------------------------------------
# Вход по коду, шаг 2: проверяем код из письма
# ---------------------------------------------------------------------------

@router.message(PanelState.waiting_code)
async def panel_code_input(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("❌ Введи код из письма")
        return

    uid = message.from_user.id
    entry = _login_sessions.get(uid)
    if not entry:
        await state.clear()
        await message.answer(
            "❌ Сессия входа истекла. Начни заново.",
            reply_markup=_cancel_kb("panel:menu"),
        )
        return

    http = entry
    status_msg = await message.answer("⏳ Проверяю код...")
    try:
        ok, result = await asyncio.wait_for(http.verify_code(code), timeout=60)
    except asyncio.TimeoutError:
        ok, result = False, "Превышено время ожидания. Попробуй ещё раз."
    except Exception as e:
        ok, result = False, f"Ошибка запроса: {str(e)[:200]}"

    _login_sessions.pop(uid, None)
    try:
        await http.close()
    except Exception:
        pass

    data = await state.get_data()
    email = data.get("email", "")
    await state.clear()

    if not ok:
        await _safe_edit(status_msg, f"❌ {result}")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 Попробовать снова", callback_data="panel:sms_start")
        b.button(text="↩️ Назад", callback_data="panel:menu")
        ui.lay(b)
        await message.answer("Выбери действие:", reply_markup=b.as_markup())
        return

    await _finish_login(message, status_msg, uid, email, result,
                        getattr(http, "chat_token", ""))



# ---------------------------------------------------------------------------
# Manual cookies
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "panel:cookies_start")
async def panel_cookies_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PanelState.waiting_cookies)
    await callback.message.edit_text(
        "🍪 <b>Вставить cookies вручную</b>\n\n"
        "<b>С компьютера (проще):</b>\n"
        "1. Зайди на <b>panel.yoomarket.net</b> и войди\n"
        "2. Жми F12 → вкладка <b>Console</b>\n"
        "3. Введи <code>document.cookie</code> → Enter\n"
        "4. Скопируй результат и отправь сюда\n\n"
        "<b>С телефона (Chrome):</b>\n"
        "1. Войди на <b>panel.yoomarket.net</b>\n"
        "2. В адресной строке введи:\n"
        "<code>javascript:copy(document.cookie)</code>\n"
        "3. Жми Enter — cookies скопируются в буфер\n"
        "4. Вставь сюда\n\n"
        "<i>Или просто введи строку вида: key=value; key2=value2</i>",
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
            "Попробуй запустить авто-функцию — если работает, всё в порядке."
        )

    creds = get_panel_creds(uid)
    await message.answer(_status_text(creds), reply_markup=_menu_kb(creds))
