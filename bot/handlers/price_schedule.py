"""Day/night price schedule: change all prices by N% inside a time window,
restore them automatically when the window ends."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class PriceSchedState(StatesGroup):
    waiting_percent = State()
    waiting_window = State()


def _menu_text(ps: dict) -> str:
    status = "🟢 Включено" if ps.get("enabled") else "🔴 Выключено"
    percent = float(ps.get("percent", -10))
    sign = "+" if percent >= 0 else ""
    night = " (сейчас действует ночная цена)" if ps.get("night_active") else ""
    return (
        "🕐 <b>Расписание цен</b>\n\n"
        f"Статус: <b>{status}</b>{night}\n"
        f"Окно: <b>{int(ps.get('from_hour', 22)):02d}:00 – "
        f"{int(ps.get('to_hour', 8)):02d}:00</b>\n"
        f"Изменение цены в окне: <b>{sign}{percent:.0f}%</b>\n\n"
        "<i>В заданное окно бот изменит цены всех товаров на указанный "
        "процент, а после окна вернёт их обратно. Проверка каждые 30 минут.</i>"
    )


def _menu_kb(ps: dict):
    b = InlineKeyboardBuilder()
    toggle = "🔴 Выключить" if ps.get("enabled") else "🟢 Включить"
    b.button(text=toggle, callback_data="pricesched:toggle")
    b.button(text="📊 Изменить процент", callback_data="pricesched:percent")
    b.button(text="🕐 Изменить окно", callback_data="pricesched:window")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "pricesched:menu")
async def menu(callback: CallbackQuery) -> None:
    ps = get_settings(callback.from_user.id).get("price_schedule", {})
    await callback.message.edit_text(_menu_text(ps), reply_markup=_menu_kb(ps))
    await callback.answer()


@router.callback_query(F.data == "pricesched:toggle")
async def toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(uid)
    ps = s.setdefault("price_schedule", {})
    ps["enabled"] = not ps.get("enabled")
    save_settings(uid, s)
    state_txt = "включено" if ps["enabled"] else "выключено"
    await callback.answer(f"Расписание цен {state_txt}")
    await callback.message.edit_text(_menu_text(ps), reply_markup=_menu_kb(ps))


@router.callback_query(F.data == "pricesched:percent")
async def percent_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PriceSchedState.waiting_percent)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="pricesched:menu")
    await callback.message.edit_text(
        "📊 Введите процент изменения цены в окне:\n\n"
        "• <b>-10</b> — снизить на 10% (ночная скидка)\n"
        "• <b>+15</b> — поднять на 15% (прайм-тайм)",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PriceSchedState.waiting_percent)
async def percent_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        percent = float(raw)
        if percent == 0 or not -90 <= percent <= 300:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от -90 до +300, например: <b>-10</b>")
        return
    await state.clear()
    uid = message.from_user.id
    s = get_settings(uid)
    s.setdefault("price_schedule", {})["percent"] = percent
    save_settings(uid, s)
    ps = s["price_schedule"]
    await message.answer(_menu_text(ps), reply_markup=_menu_kb(ps))


@router.callback_query(F.data == "pricesched:window")
async def window_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PriceSchedState.waiting_window)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="pricesched:menu")
    await callback.message.edit_text(
        "🕐 Введите окно действия в формате <b>ЧЧ-ЧЧ</b>:\n\n"
        "• <b>22-8</b> — с 22:00 до 08:00 (ночь, через полночь)\n"
        "• <b>9-18</b> — с 09:00 до 18:00 (день)",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PriceSchedState.waiting_window)
async def window_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(":", "")
    try:
        part_from, _, part_to = raw.partition("-")
        from_h, to_h = int(part_from), int(part_to)
        if not (0 <= from_h <= 23 and 0 <= to_h <= 23) or from_h == to_h:
            raise ValueError
    except ValueError:
        await message.answer("❌ Формат: <b>22-8</b> (часы 0–23, разные)")
        return
    await state.clear()
    uid = message.from_user.id
    s = get_settings(uid)
    ps = s.setdefault("price_schedule", {})
    ps["from_hour"] = from_h
    ps["to_hour"] = to_h
    save_settings(uid, s)
    await message.answer(_menu_text(ps), reply_markup=_menu_kb(ps))
