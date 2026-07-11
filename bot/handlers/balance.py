import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class WithdrawState(StatesGroup):
    waiting_amount = State()


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


def _kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💸 Вывести средства", callback_data="balance:withdraw")
    b.button(text="📋 Активные выводы", callback_data="balance:active")
    b.button(text="📜 История выводов", callback_data="balance:history")
    b.button(text="🔄 Обновить", callback_data="menu:balance")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(2, 1, 2)
    return b.as_markup()


@router.callback_query(F.data == "menu:balance")
async def show_balance(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю баланс...")
    if not api:
        await callback.message.edit_text(
            "❌ Нужен API-токен. Отправьте /start и введите токен.",
            reply_markup=back_keyboard())
        await callback.answer()
        return
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
        kb = _kb()
    except Exception as e:
        logger.error("Balance error: %s", e)
        text = f"❌ Ошибка загрузки баланса:\n<code>{e}</code>"
        kb = back_keyboard()

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ── Вывод средств ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "balance:withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    bal_str = "?"
    if api:
        try:
            amount, bal_str = await api.get_balance()
        except Exception:
            pass
    await state.set_state(WithdrawState.waiting_amount)
    b = InlineKeyboardBuilder()
    b.button(text="💸 Вывести всё", callback_data="balance:withdraw_all")
    b.button(text="❌ Отмена", callback_data="menu:balance")
    b.adjust(1)
    await callback.message.edit_text(
        f"💸 <b>Вывод средств</b>\n\nДоступно: <b>{bal_str} ₽</b>\n\n"
        "Введите сумму для вывода или нажмите «Вывести всё»:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


def _log_withdrawal(uid: int, amount: float, wtype: str, ok: bool) -> None:
    s = get_settings(uid)
    hist = s.setdefault("withdrawal_history", [])
    hist.insert(0, {
        "amount": amount, "ts": time.time(), "type": wtype,
        "status": "requested" if ok else "failed",
    })
    del hist[100:]  # keep last 100
    save_settings(uid, s)


async def _do_withdraw(msg, uid: int, api: YooMarketAPI, amount: float | None) -> None:
    if not api:
        await msg.answer("❌ Нужен API-токен.")
        return
    status = await msg.answer("⏳ Оформляю вывод…")
    try:
        ok, result = await api.withdraw_balance(amount=amount)
    except Exception as e:
        ok, result = False, f"Ошибка: {str(e)[:120]}"
    # log the requested amount (0 = full — we log the balance if known)
    log_amt = amount if amount is not None else 0.0
    _log_withdrawal(uid, log_amt, "manual", ok)
    b = InlineKeyboardBuilder()
    b.button(text="📜 История", callback_data="balance:history")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(2)
    await status.edit_text(
        (f"✅ {result}" if ok else f"❌ {result}"), reply_markup=b.as_markup())


@router.callback_query(F.data == "balance:withdraw_all")
async def withdraw_all(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    await state.clear()
    await callback.answer()
    await _do_withdraw(callback.message, callback.from_user.id, api, None)


@router.message(WithdrawState.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext, api: YooMarketAPI) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите сумму числом, например: <b>500</b>")
        return
    await state.clear()
    await _do_withdraw(message, message.from_user.id, api, amount)


# ── Активные выводы ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "balance:active")
async def active_withdrawals(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("⏳")
    items = []
    if api:
        try:
            items = await api.get_withdrawals()
        except Exception as e:
            logger.warning("get_withdrawals: %s", e)
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="balance:active")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(2)
    active_st = ("pending", "processing", "new", "in_progress", "wait", "created")
    active = [w for w in items
              if str(w.get("status", "")).lower() in active_st]
    if active:
        lines = [f"📋 <b>Активные выводы</b> ({len(active)})\n"]
        for w in active[:20]:
            amt = w.get("amount") or w.get("sum") or "—"
            st = w.get("status", "—")
            lines.append(f"• <b>{amt} ₽</b> — {st}")
        text = "\n".join(lines)
    elif items:
        text = "📋 <b>Активные выводы</b>\n\nНет выводов в обработке."
    else:
        text = ("📋 <b>Активные выводы</b>\n\n"
                "API не отдаёт список выводов. Смотрите статус в панели "
                "или в «Истории выводов» бота.")
    await callback.message.edit_text(text, reply_markup=b.as_markup())


# ── История выводов ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "balance:history")
async def withdrawal_history(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer()
    uid = callback.from_user.id
    # prefer the API history; fall back to the local log
    api_items = []
    if api:
        try:
            api_items = await api.get_withdrawals()
        except Exception:
            pass
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="balance:history")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(2)
    if api_items:
        lines = [f"📜 <b>История выводов</b> ({len(api_items)})\n"]
        for w in api_items[:25]:
            amt = w.get("amount") or w.get("sum") or "—"
            st = w.get("status", "—")
            date = w.get("created_at") or w.get("date") or ""
            date = str(date)[:16]
            lines.append(f"• <b>{amt} ₽</b> — {st}  {date}")
        text = "\n".join(lines)
    else:
        hist = get_settings(uid).get("withdrawal_history", [])
        if not hist:
            text = "📜 <b>История выводов</b>\n\nВыводов пока не было."
        else:
            lines = [f"📜 <b>История выводов</b> ({len(hist)})\n"
                     "<i>(локальный журнал бота)</i>\n"]
            for w in hist[:25]:
                amt = w.get("amount", 0)
                amt_str = f"{amt:.0f} ₽" if amt else "всё"
                wtype = "🤖" if w.get("type") == "auto" else "✋"
                st = "✅" if w.get("status") == "requested" else "❌"
                try:
                    date = datetime.fromtimestamp(w.get("ts", 0)).strftime("%d.%m %H:%M")
                except Exception:
                    date = ""
                lines.append(f"{st} {wtype} <b>{amt_str}</b>  {date}")
            text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=b.as_markup())
