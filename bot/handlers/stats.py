from __future__ import annotations

import logging
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
from storage import get_settings
from handlers.balance import _parse_check

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:stats")
async def show_stats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю статистику...")

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})

    completed = sum(1 for s in known_orders.values() if s in ("confirmed", "completed", "done"))
    refunded = sum(1 for s in known_orders.values() if s in ("refunded", "cancelled", "returned"))
    active = len(known_orders) - completed - refunded
    total = len(known_orders)

    balance_str = "—"
    shop_name = "—"
    ads_total = "—"

    if api:
        try:
            check_data = await api.check()
            shop_name, balance_str, _ = _parse_check(check_data)
            balance_str = f"{balance_str} ₽"
        except Exception as e:
            logger.warning("Stats balance error: %s", e)

        try:
            ads_data = await api.get_ads()
            meta = ads_data.get("meta", {})
            ads_list = ads_data.get("data") or ads_data.get("items") or []
            ads_total = str(
                meta.get("total") or meta.get("count") or meta.get("total_count") or len(ads_list)
            )
        except Exception as e:
            logger.warning("Stats ads error: %s", e)

    text = (
        f"📊 <b>Статистика</b>\n"
        f"🏪 {shop_name}\n\n"
        f"💰 Баланс: <b>{balance_str}</b>\n"
        f"🚀 Объявлений: <b>{ads_total}</b>\n\n"
        f"🛒 <b>Заказы (за всё время)</b>\n"
        f"├ Всего: <b>{total}</b>\n"
        f"├ ✅ Выполнено: <b>{completed}</b>\n"
        f"├ ⏳ Активные: <b>{active}</b>\n"
        f"└ ↩️ Возвраты: <b>{refunded}</b>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="menu:stats")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()
