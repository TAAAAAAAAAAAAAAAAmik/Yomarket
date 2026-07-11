from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()


@router.callback_query(F.data == "settings:menu")
async def settings_menu(callback: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Уведомления", callback_data="notif:menu")
    b.button(text="⚙️ Авто-функции", callback_data="auto:menu")
    b.button(text="🕐 Расписание цен", callback_data="pricesched:menu")
    b.button(text="👥 Аккаунты", callback_data="acc:menu")
    b.button(text="🛠 Инструменты", callback_data="tools:menu")
    b.button(text="🌐 Панель продавца", callback_data="panel:menu")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(2, 2, 2, 1)
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\nВыберите раздел:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()
