from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import delete_token, get_token, save_token, get_settings, save_settings, get_shop_name, save_shop_name, _DATA_DIR

router = Router()

# Bumped on every meaningful code change — lets us confirm which version is running.
BOT_VERSION = "2026-08-02-expired-only"


class AuthState(StatesGroup):
    waiting_for_token = State()


def _extract_shop(info: dict) -> tuple[str, str]:
    """Returns (name, balance_str) from /check response."""
    shop = info.get("shop") or info.get("data") or info
    if isinstance(shop, dict):
        name = shop.get("name") or shop.get("shop_name") or shop.get("title") or "Магазин"
        balance = shop.get("balance") or shop.get("wallet") or shop.get("money") or "—"
    else:
        name = "Магазин"
        balance = "—"
    return str(name), str(balance)


async def _send_menu(target: Message | CallbackQuery, user_id: int) -> None:
    from storage import is_admin, menu_header_html
    name = get_shop_name(user_id) or "Магазин"
    text = (f"🏪 <b>{name}</b>\n\n{menu_header_html()} <b>Главное меню</b>\n"
            "Выберите раздел:")
    kb = main_menu_keyboard(is_admin_user=is_admin(user_id))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    token = get_token(message.from_user.id)
    if token:
        api = YooMarketAPI(token)
        await api.start()
        try:
            info = await api.check()
            name, _ = _extract_shop(info)
            save_shop_name(message.from_user.id, name)
        except Exception:
            pass
        finally:
            await api.close()
        await _send_menu(message, message.from_user.id)
        return

    from storage import render_custom_text
    await state.set_state(AuthState.waiting_for_token)
    # The greeting links to the panel; a link preview would bury the steps.
    await message.answer(render_custom_text("welcome"),
                         disable_web_page_preview=True)


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    """Show running bot version and where/how data is stored — deploy diagnostics."""
    import os
    import storage

    uid = message.from_user.id

    # 1. Storage backend
    if storage._USE_DB:
        backend = "🟢 PostgreSQL (постоянная БД)"
        db_ok = "?"
        try:
            storage._db_read_raw("tokens")  # touch the DB
            db_ok = "✅ подключение работает"
        except Exception as e:
            db_ok = f"❌ ошибка: {str(e)[:60]}"
        storage_lines = [f"💾 Хранилище: {backend}", f"   {db_ok}"]
    else:
        backend = "🔴 JSON-файлы (эфемерно на Railway!)"
        storage_lines = [
            f"💾 Хранилище: {backend}",
            "   ⚠️ DATABASE_URL не задан — данные сотрутся при редеплое!",
            f"   📁 {_DATA_DIR}",
        ]

    # 2. Redis (FSM)
    redis_on = bool(os.environ.get("REDIS_URL", "").strip())
    redis_line = "🧩 FSM: 🟢 Redis" if redis_on else "🧩 FSM: ⚪ память (сбрасывается при рестарте)"

    # 3. Is THIS user's token actually stored right now?
    has_token = bool(storage.get_token(uid))
    accounts = storage.get_accounts(uid)
    token_line = (f"🔑 Ваш токен: {'✅ сохранён' if has_token else '❌ нет'}"
                  f"  ({len(accounts)} аккаунт(ов))")
    panel = storage.get_panel_creds(uid)
    panel_line = f"🌐 Куки панели: {'✅ есть' if panel and panel.get('cookies') else '❌ нет'}"

    await message.answer(
        f"🤖 <b>Версия:</b> <code>{BOT_VERSION}</code>\n\n"
        + "\n".join(storage_lines)
        + f"\n{redis_line}\n\n"
        + f"{token_line}\n{panel_line}"
    )


@router.message(AuthState.waiting_for_token)
async def process_token(message: Message, state: FSMContext, **data) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым. Отправьте токен:")
        return

    await message.answer("⏳ Проверяю токен...")
    api = YooMarketAPI(token)
    await api.start()
    try:
        info = await api.check()
    except Exception as e:
        await api.close()
        await message.answer(
            f"❌ Ошибка авторизации: <code>{e}</code>\n\nПроверьте токен и отправьте снова:"
        )
        return
    finally:
        await api.close()

    save_token(message.from_user.id, token)
    await state.clear()

    name, balance = _extract_shop(info)
    save_shop_name(message.from_user.id, name)

    task_manager = data.get("task_manager")
    if task_manager:
        task_manager.start_for_user(message.from_user.id)

    # Step 2 of onboarding: the panel login. Skipped for someone who is already
    # logged in — no point walking them through a step that is done.
    from storage import get_panel_creds, render_custom_text
    if get_panel_creds(message.from_user.id):
        await message.answer(
            f"✅ <b>Токен обновлён</b>\n\n🏪 <b>{name}</b>\n"
            f"💰 Баланс: <b>{balance} ₽</b>")
        await _send_menu(message, message.from_user.id)
        return

    b = InlineKeyboardBuilder()
    b.button(text="📧 Войти по email", callback_data="panel:sms_start")
    b.button(text="⏭ Позже", callback_data="menu:main")
    b.adjust(1)
    # The shop name comes from the marketplace and goes into an HTML message —
    # a '<' in it would make Telegram reject the whole send.
    import html as _html
    await message.answer(
        render_custom_text("token_ok", name=_html.escape(name),
                           balance=_html.escape(str(balance))),
        reply_markup=b.as_markup(), disable_web_page_preview=True)


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery) -> None:
    await _send_menu(callback, callback.from_user.id)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer("🚪 Вы вышли. Для входа — /start")
