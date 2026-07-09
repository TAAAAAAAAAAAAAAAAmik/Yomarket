from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import delete_token, get_token, save_token, get_settings, save_settings, get_shop_name, save_shop_name, _DATA_DIR

router = Router()

# Bumped on every meaningful code change — lets us confirm which version is running.
BOT_VERSION = "2026-06-24-v3"


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
    from storage import is_admin
    name = get_shop_name(user_id) or "Магазин"
    text = f"🏪 <b>{name}</b>\n\n🏠 <b>Главное меню</b>\nВыберите раздел:"
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

    await state.set_state(AuthState.waiting_for_token)
    await message.answer(
        "👋 Добро пожаловать в <b>YooMarket бот</b>!\n\n"
        "Отправьте ваш <b>API токен</b> из панели YooMarket:\n"
        "<i>Мой магазин → Интеграции → API токен</i>"
    )


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    """Show running bot version and data directory — for debugging deploys."""
    import os
    data_files = []
    try:
        for f in ("tokens.json", "settings.json", "panel_creds.json"):
            p = os.path.join(_DATA_DIR, f)
            data_files.append(f"{'✅' if os.path.exists(p) else '❌'} {f}")
    except Exception:
        pass
    await message.answer(
        f"🤖 <b>Версия бота:</b> <code>{BOT_VERSION}</code>\n"
        f"📁 <b>Данные:</b> <code>{_DATA_DIR}</code>\n\n"
        + "\n".join(data_files)
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

    await message.answer(
        f"✅ Авторизация успешна!\n\n"
        f"🏪 <b>{name}</b>\n"
        f"💰 Баланс: <b>{balance} ₽</b>"
    )

    task_manager = data.get("task_manager")
    if task_manager:
        task_manager.start_for_user(message.from_user.id)

    await _send_menu(message, message.from_user.id)


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery) -> None:
    await _send_menu(callback, callback.from_user.id)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer("🚪 Вы вышли. Для входа — /start")
