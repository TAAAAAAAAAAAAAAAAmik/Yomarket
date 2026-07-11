"""Auto-bump, auto-restore, auto-withdraw via YooMarket Integration API."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class SeleniumState(StatesGroup):
    waiting_bump_interval = State()
    waiting_withdraw_amount = State()


def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts)) if isinstance(ts, (int, float)) else datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return str(ts)[:16]


def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Auto-bump
# ---------------------------------------------------------------------------

def _bump_text(s: dict, creds=None) -> str:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    last_run = _fmt_ts(ab.get("last_bump_run"))
    return "\n".join([
        "⬆️ <b>Авто-поднятие</b>\n",
        f"Статус: {_st(on)}",
        f"Интервал: каждые {interval} ч",
        f"Последний запуск: {last_run}",
        "",
        "Поднимает все объявления через API.",
    ])


def _bump_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:bump")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:bump:toggle")
    b.button(text=f"⏱ Интервал: {interval} ч", callback_data="selenium:bump:set_interval")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(2, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:bump:menu")
async def bump_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_bump_text(s), reply_markup=_bump_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:toggle")
async def bump_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_bump"]["enabled"] = not s["auto_bump"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_bump_text(s), reply_markup=_bump_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:set_interval")
async def bump_set_interval(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_bump", {}).get("interval_hours", 24)
    await state.set_state(SeleniumState.waiting_bump_interval)
    await callback.message.edit_text(
        f"⏱ <b>Интервал поднятия</b>\n\nТекущий: {cur} ч\n\nВведите количество часов (например: 6, 12, 24):",
        reply_markup=_cancel_kb("selenium:bump:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_bump_interval)
async def bump_save_interval(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if hours < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число часов, например: 24")
        return
    s = get_settings(message.from_user.id)
    s["auto_bump"]["interval_hours"] = hours
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(f"✅ Интервал поднятия: <b>{hours} ч</b>")
    await message.answer(_bump_text(s), reply_markup=_bump_kb(s))


@router.callback_query(F.data == "selenium:run:bump")
async def run_bump(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    await callback.answer("⏳ Поднимаю объявления...", show_alert=False)
    await callback.message.edit_text("⏳ Поднимаю все объявления через API...")
    s = get_settings(callback.from_user.id)
    try:
        count, msg = await api.bump_all_ads()
        s["auto_bump"]["last_bump_run"] = _time.time()
        save_settings(callback.from_user.id, s)
        result_text = f"⬆️ <b>Поднятие завершено</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual bump error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    await callback.message.edit_text(result_text + "\n\n" + _bump_text(s), reply_markup=_bump_kb(s))


# ---------------------------------------------------------------------------
# Auto-restore
# ---------------------------------------------------------------------------

def _restore_text(s: dict, creds=None) -> str:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    last_run = _fmt_ts(ar.get("last_restore_run"))
    return "\n".join([
        "🔄 <b>Авто-восстановление</b>\n",
        f"Статус: {_st(on)}",
        f"Последний запуск: {last_run}",
        "",
        "Переактивирует проданные или истёкшие объявления через API.",
    ])


def _restore_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    on = s.get("auto_restore", {}).get("enabled", False)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:restore")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:restore:toggle")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(2, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:restore:menu")
async def restore_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_restore_text(s), reply_markup=_restore_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:restore:toggle")
async def restore_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_restore"]["enabled"] = not s["auto_restore"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_restore_text(s), reply_markup=_restore_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:run:restore")
async def run_restore(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    await callback.answer("⏳ Восстанавливаю объявления...", show_alert=False)
    await callback.message.edit_text("⏳ Восстанавливаю объявления через API...")
    s = get_settings(callback.from_user.id)
    try:
        count, msg = await api.restore_all_ads()
        s["auto_restore"]["last_restore_run"] = _time.time()
        save_settings(callback.from_user.id, s)
        result_text = f"🔄 <b>Восстановление завершено</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual restore error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    await callback.message.edit_text(result_text + "\n\n" + _restore_text(s), reply_markup=_restore_kb(s))


# ---------------------------------------------------------------------------
# Auto-withdraw
# ---------------------------------------------------------------------------

def _withdraw_text(s: dict, creds=None) -> str:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    return "\n".join([
        "💸 <b>Авто-вывод баланса</b>\n",
        f"Статус: {_st(on)}",
        f"Мин. сумма: {min_amount} ₽",
        "",
        "Выводит баланс через API, когда он превышает порог.",
    ])


def _withdraw_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:withdraw")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:withdraw:toggle")
    b.button(text=f"💰 Порог: {min_amount} ₽", callback_data="selenium:withdraw:set_amount")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(2, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:withdraw:menu")
async def withdraw_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_withdraw_text(s), reply_markup=_withdraw_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:toggle")
async def withdraw_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_withdraw"]["enabled"] = not s["auto_withdraw"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_withdraw_text(s), reply_markup=_withdraw_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:set_amount")
async def withdraw_set_amount(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_withdraw", {}).get("min_amount", 500)
    await state.set_state(SeleniumState.waiting_withdraw_amount)
    await callback.message.edit_text(
        f"💰 <b>Минимальная сумма для вывода</b>\n\nТекущая: {cur} ₽\n\nВведите новую сумму (₽):",
        reply_markup=_cancel_kb("selenium:withdraw:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_withdraw_amount)
async def withdraw_save_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число, например: 500")
        return
    s = get_settings(message.from_user.id)
    s["auto_withdraw"]["min_amount"] = amount
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(f"✅ Мин. сумма вывода: <b>{amount} ₽</b>")
    await message.answer(_withdraw_text(s), reply_markup=_withdraw_kb(s))


@router.callback_query(F.data == "selenium:run:withdraw")
async def run_withdraw(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    min_amount = s.get("auto_withdraw", {}).get("min_amount", 500)
    await callback.answer("⏳ Запускаю вывод...", show_alert=False)
    await callback.message.edit_text("⏳ Проверяю баланс и выполняю вывод через API...")
    try:
        success, msg = await api.withdraw_balance(min_amount)
        result_text = f"💸 <b>Авто-вывод</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual withdraw error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(result_text + "\n\n" + _withdraw_text(s), reply_markup=_withdraw_kb(s))
