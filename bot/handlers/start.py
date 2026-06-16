from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import delete_token, get_token, save_token

router = Router()

MENU_TEXT = "🏠 <b>Главное меню</b>\nВыберите раздел:"


class AuthState(StatesGroup):
    waiting_for_token = State()


async def _send_menu(target: Message | CallbackQuery, shop_name: str = "") -> None:
    header = f"🏪 <b>{shop_name}</b>\n\n" if shop_name else ""
    text = header + MENU_TEXT
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=main_menu_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_menu_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    token = get_token(message.from_user.id)
    if token:
        api = YooMarketAPI(token)
        await api.start()
        try:
            info = await api.check()
            shop = info.get("shop") or info
            name = shop.get("name") or shop.get("shop_name") or "Магазин"
        except Exception:
            name = ""
        finally:
            await api.close()
        await _send_menu(message, name)
        return

    await state.set_state(AuthState.waiting_for_token)
    await message.answer(
        "👋 Добро пожаловать в <b>YooMarket бот</b>!\n\n"
        "Отправьте ваш <b>API токен</b> из панели YooMarket:\n"
        "<i>Мой магазин → Интеграции → API токен</i>"
    )


@router.message(AuthState.waiting_for_token)
async def process_token(message: Message, state: FSMContext) -> None:
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

    shop = info.get("shop") or info
    name = shop.get("name") or shop.get("shop_name") or "Магазин"
    balance = shop.get("balance", "—")

    await message.answer(
        f"✅ Авторизация успешна!\n\n"
        f"🏪 <b>{name}</b>\n"
        f"💰 Баланс: <b>{balance} ₽</b>"
    )
    await _send_menu(message, name)


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery, api: YooMarketAPI) -> None:
    name = ""
    if api:
        try:
            info = await api.check()
            shop = info.get("shop") or info
            name = shop.get("name") or shop.get("shop_name") or ""
        except Exception:
            pass
    await _send_menu(callback, name)


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer("🚪 Вы вышли. Для входа — /start")
