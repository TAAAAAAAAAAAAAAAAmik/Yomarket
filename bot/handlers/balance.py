from aiogram import F, Router
from aiogram.types import CallbackQuery

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard

router = Router()


@router.callback_query(F.data == "menu:balance")
async def show_balance(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю баланс...")
    try:
        data = await api.check()
        shop = data.get("shop") or data
        name = shop.get("name") or shop.get("shop_name") or "—"
        balance = shop.get("balance", "—")
        pending = shop.get("pending_balance") or shop.get("pending")
        text = (
            f"🏪 <b>{name}</b>\n\n"
            f"💰 Баланс: <b>{balance} ₽</b>\n"
        )
        if pending is not None:
            text += f"⏳ В ожидании: <b>{pending} ₽</b>\n"
        text += "✅ Статус: Активен"
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()
