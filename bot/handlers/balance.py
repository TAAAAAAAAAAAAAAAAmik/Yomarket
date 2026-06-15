from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from api import YooMarketAPI
from keyboards import back_keyboard

router = Router()


@router.message(F.text == "💰 Баланс")
async def show_balance(message: Message, api: YooMarketAPI) -> None:
    try:
        data = await api.check()
        shop_name = data.get("name") or data.get("shop_name") or "—"
        balance = data.get("balance", 0)
        text = (
            f"🏪 Магазин: {shop_name}\n"
            f"💰 Баланс: {balance} ₽\n"
            f"✅ Статус: Активен"
        )
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    await message.answer(text, reply_markup=back_keyboard())


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery) -> None:
    from keyboards import main_menu_keyboard

    await callback.message.answer(
        "Главное меню:", reply_markup=main_menu_keyboard()
    )
    await callback.answer()
