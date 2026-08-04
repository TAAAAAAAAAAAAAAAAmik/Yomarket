"""Последний рубеж: нажатие, которое не подошло ни одному обработчику.

aiogram на такое нажатие просто ничего не делает — Telegram оставляет кнопку в
загрузке, и человек видит «кнопка не работает». Чаще всего это устаревшая
кнопка: сообщение осталось от прошлой версии бота, а обработчика для неё уже
нет. Роутер включается последним, поэтому сюда попадает только то, что не
разобрал никто другой.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data)
async def unhandled_callback(callback: CallbackQuery) -> None:
    data = str(callback.data or "")
    logger.warning("unhandled callback: %r", data)
    await callback.answer()

    b = InlineKeyboardBuilder()
    b.button(text="🏠 Главное меню", callback_data="menu:main")
    b.adjust(1)
    try:
        await callback.message.answer(
            "🤷 <b>Эта кнопка ничего не делает</b>\n\n"
            f"Действие: <code>{data[:60]}</code>\n\n"
            "Обычно так бывает с кнопкой из старого сообщения — бот обновился, "
            "а сообщение осталось прежним. Откройте раздел заново.",
            reply_markup=b.as_markup())
    except Exception as e:
        logger.warning("could not report unhandled callback: %s", e)
