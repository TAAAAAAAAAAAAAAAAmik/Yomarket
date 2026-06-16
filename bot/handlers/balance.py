import logging
from aiogram import F, Router
from aiogram.types import CallbackQuery

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard

router = Router()
logger = logging.getLogger(__name__)


def _parse_check(data: dict) -> tuple[str, str, str | None]:
    """Parse /check response → (name, balance, pending)."""
    logger.info("CHECK raw response: %s", data)
    shop = data.get("shop") or data.get("data") or data
    if not isinstance(shop, dict):
        shop = data
    name = (
        shop.get("name") or shop.get("shop_name") or shop.get("title") or
        data.get("name") or "—"
    )
    # Try every possible balance field
    balance = (
        shop.get("balance") or shop.get("wallet") or shop.get("money") or
        shop.get("balance_rub") or shop.get("amount") or
        data.get("balance") or data.get("wallet") or data.get("money")
    )
    pending = (
        shop.get("pending_balance") or shop.get("pending") or
        shop.get("hold") or shop.get("frozen")
    )
    bal_str = str(balance) if balance is not None and balance != "" else "0"
    pend_str = str(pending) if pending is not None and pending != "" else None
    return str(name), bal_str, pend_str


@router.callback_query(F.data == "menu:balance")
async def show_balance(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю баланс...")
    try:
        data = await api.check()
        name, balance, pending = _parse_check(data)
        text = (
            f"🏪 <b>{name}</b>\n\n"
            f"💰 Баланс: <b>{balance} ₽</b>\n"
        )
        if pending:
            text += f"⏳ В ожидании: <b>{pending} ₽</b>\n"
        text += "✅ Статус: Активен"
    except Exception as e:
        logger.error("Balance error: %s", e)
        text = f"❌ Ошибка загрузки баланса:\n<code>{e}</code>"

    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()
