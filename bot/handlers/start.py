from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from api.yoomarket import YooMarketAPI
from keyboards.main import main_menu_keyboard
from storage import get_token, save_token, delete_token

router = Router()


class AuthState(StatesGroup):
    waiting_for_token = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    token = get_token(message.from_user.id)
    if token:
        api = YooMarketAPI(token)
        await api.start()
        try:
            info = await api.check()
        except Exception:
            info = None
        finally:
            await api.close()

        if info:
            shop = info.get("shop", {})
            name = shop.get("name", "Магазин")
            balance = shop.get("balance", "—")
            await message.answer(
                f"👋 Добро пожаловать, <b>{name}</b>!\n"
                f"💰 Баланс: <b>{balance} ₽</b>\n\n"
                f"Выберите раздел:",
                reply_markup=main_menu_keyboard(),
            )
            return

    await state.set_state(AuthState.waiting_for_token)
    await message.answer(
        "👋 Добро пожаловать в <b>YooMarket бот</b>!\n\n"
        "Для входа отправьте ваш <b>API токен</b> из панели YooMarket:\n"
        "<i>Мой магазин → Интеграции → API токен</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AuthState.waiting_for_token)
async def process_token(message: Message, state: FSMContext) -> None:
    token = message.text.strip() if message.text else ""
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
            f"❌ Ошибка авторизации: <code>{e}</code>\n\n"
            "Проверьте токен и отправьте снова:"
        )
        return
    finally:
        await api.close()

    save_token(message.from_user.id, token)
    await state.clear()

    shop = info.get("shop", {})
    name = shop.get("name", "Магазин")
    balance = shop.get("balance", "—")

    await message.answer(
        f"✅ Авторизация успешна!\n\n"
        f"🏪 <b>{name}</b>\n"
        f"💰 Баланс: <b>{balance} ₽</b>\n\n"
        f"Выберите раздел:",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    delete_token(message.from_user.id)
    await message.answer(
        "🚪 Вы вышли из аккаунта.\n"
        "Для входа используйте /start",
        reply_markup=ReplyKeyboardRemove(),
    )
