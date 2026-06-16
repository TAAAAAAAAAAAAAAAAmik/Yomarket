from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()


class AutoState(StatesGroup):
    waiting_reply_msg = State()
    waiting_withdraw_amount = State()


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------


def _auto_menu_keyboard(settings: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    ar = settings.get("auto_reply", {})
    ar_on = ar.get("enabled", False)
    builder.button(
        text=f"{'🟢' if ar_on else '🔴'} Автоответчик — {'ВКЛ' if ar_on else 'ВЫКЛ'}",
        callback_data="auto:toggle:reply",
    )
    if ar_on:
        builder.button(text="✏️ Изменить текст", callback_data="auto:set_reply_msg")

    rst = settings.get("auto_restore", {})
    rst_on = rst.get("enabled", False)
    builder.button(
        text=f"{'🟢' if rst_on else '🔴'} Авторестор товаров — {'ВКЛ' if rst_on else 'ВЫКЛ'}",
        callback_data="auto:toggle:restore",
    )

    bump = settings.get("auto_bump", {})
    bump_on = bump.get("enabled", False)
    builder.button(
        text=f"{'🟢' if bump_on else '🔴'} Автоподнятие — {'ВКЛ' if bump_on else 'ВЫКЛ'}",
        callback_data="auto:toggle:bump",
    )

    aw = settings.get("auto_withdraw", {})
    aw_on = aw.get("enabled", False)
    builder.button(
        text=f"{'🟢' if aw_on else '🔴'} Автовывод — {'ВКЛ' if aw_on else 'ВЫКЛ'}",
        callback_data="auto:toggle:withdraw",
    )

    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def _auto_menu_text(settings: dict) -> str:
    ar = settings.get("auto_reply", {})
    ar_on = ar.get("enabled", False)
    ar_msg = ar.get("message", "")

    rst_on = settings.get("auto_restore", {}).get("enabled", False)
    bump_on = settings.get("auto_bump", {}).get("enabled", False)
    aw = settings.get("auto_withdraw", {})
    aw_on = aw.get("enabled", False)
    aw_min = aw.get("min_amount", 500)

    lines = ["⚙️ <b>Авто-функции</b>\n"]

    # Auto-reply block
    status = "✅ <b>Автоответчик</b> — <b>ВКЛ</b>" if ar_on else "🔴 <b>Автоответчик</b> — ВЫКЛ"
    lines.append(status)
    if ar_on and ar_msg:
        short = ar_msg[:40] + ("…" if len(ar_msg) > 40 else "")
        lines.append(f'   Текст: "<i>{short}</i>"')
    lines.append("")

    # Auto-restore
    status = "✅ <b>Авторестор товаров</b> — <b>ВКЛ</b>" if rst_on else "🔴 <b>Авторестор товаров</b> — ВЫКЛ"
    lines.append(status)
    lines.append("")

    # Auto-bump
    status = "✅ <b>Автоподнятие</b> — <b>ВКЛ</b>" if bump_on else "🔴 <b>Автоподнятие</b> — ВЫКЛ"
    lines.append(status)
    lines.append("")

    # Auto-withdraw
    status = "✅ <b>Автовывод</b> — <b>ВКЛ</b>" if aw_on else "🔴 <b>Автовывод</b> — ВЫКЛ"
    lines.append(status)
    if aw_on:
        lines.append(f"   Мин. сумма: {aw_min} ₽")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "auto:menu")
async def auto_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(
        _auto_menu_text(settings),
        reply_markup=_auto_menu_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "auto:toggle:reply")
async def toggle_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["auto_reply"]["enabled"] = not settings["auto_reply"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(
        _auto_menu_text(settings),
        reply_markup=_auto_menu_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "auto:set_reply_msg")
async def set_reply_msg_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_reply_msg)
    await callback.message.answer(
        "✏️ Введите новый текст автоответа на заказы:"
    )
    await callback.answer()


@router.message(AutoState.waiting_reply_msg)
async def set_reply_msg_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст не может быть пустым. Попробуйте ещё раз:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["auto_reply"]["message"] = text
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Текст автоответа обновлён:\n<i>{text}</i>\n\nВозврат в меню авто-функций:",
        reply_markup=_auto_menu_keyboard(settings),
    )


@router.callback_query(F.data == "auto:toggle:restore")
async def toggle_restore(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    currently_on = settings.get("auto_restore", {}).get("enabled", False)
    if not currently_on:
        # Trying to enable — not supported yet
        await callback.answer(
            "⚠️ Функция недоступна в текущей версии API YooMarket",
            show_alert=True,
        )
        return
    # Turning off is always allowed
    settings["auto_restore"]["enabled"] = False
    save_settings(uid, settings)
    await callback.message.edit_text(
        _auto_menu_text(settings),
        reply_markup=_auto_menu_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "auto:toggle:bump")
async def toggle_bump(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    currently_on = settings.get("auto_bump", {}).get("enabled", False)
    if not currently_on:
        await callback.answer(
            "⚠️ Функция недоступна в текущей версии API YooMarket",
            show_alert=True,
        )
        return
    settings["auto_bump"]["enabled"] = False
    save_settings(uid, settings)
    await callback.message.edit_text(
        _auto_menu_text(settings),
        reply_markup=_auto_menu_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "auto:toggle:withdraw")
async def toggle_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    currently_on = settings.get("auto_withdraw", {}).get("enabled", False)
    settings["auto_withdraw"]["enabled"] = not currently_on
    save_settings(uid, settings)

    if settings["auto_withdraw"]["enabled"]:
        # Ask for minimum amount
        await state.set_state(AutoState.waiting_withdraw_amount)
        await callback.message.answer(
            "💰 Введите минимальную сумму для автовывода (в рублях):"
        )
        await callback.answer()
    else:
        await callback.message.edit_text(
            _auto_menu_text(settings),
            reply_markup=_auto_menu_keyboard(settings),
        )
        await callback.answer()


@router.message(AutoState.waiting_withdraw_amount)
async def set_withdraw_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое положительное число (сумма в рублях):")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["auto_withdraw"]["min_amount"] = amount
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Минимальная сумма автовывода: <b>{amount} ₽</b>",
        reply_markup=_auto_menu_keyboard(settings),
    )
