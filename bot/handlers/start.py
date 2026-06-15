from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from keyboards import main_menu_keyboard

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Добро пожаловать в YooMarket бот!\n\n"
        "Выберите раздел в меню ниже:",
        reply_markup=main_menu_keyboard(),
    )
