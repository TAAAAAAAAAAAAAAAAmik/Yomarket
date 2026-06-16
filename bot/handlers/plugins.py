from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings

router = Router()


class PluginState(StatesGroup):
    waiting_stars_amount = State()
    waiting_stars_note = State()
    waiting_roblox_amount = State()
    waiting_roblox_note = State()
    waiting_gifts_type = State()
    waiting_gifts_note = State()


# ---------------------------------------------------------------------------
# Keyboard / text helpers
# ---------------------------------------------------------------------------


def _plugins_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    builder.button(text="🎮 AutoRoblox", callback_data="plugins:auto_roblox")
    builder.button(text="🎁 AutoGifts", callback_data="plugins:auto_gifts")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def _back_to_plugins_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(1)
    return builder.as_markup()


# --- AutoStars ---

def _stars_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_stars"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'🔴 Выключить' if enabled else '🟢 Включить'}",
        callback_data="plugins:stars:toggle",
    )
    builder.button(text="✏️ Настроить кол-во", callback_data="plugins:stars:set_amount")
    builder.button(text="📝 Заметка для покупателя", callback_data="plugins:stars:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(1)
    return builder.as_markup()


def _stars_text(settings: dict) -> str:
    p = settings["plugins"]["auto_stars"]
    enabled = p.get("enabled", False)
    amount = p.get("amount", 50)
    note = p.get("note") or "—"
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    return (
        "⭐ <b>AutoStars</b>\n\n"
        "Автоматически отправляет Telegram Stars покупателю при новом заказе.\n\n"
        f"Статус: {status}\n"
        f"Кол-во звёзд: <b>{amount}</b>\n"
        f"Заметка: <i>{note}</i>"
    )


# --- AutoRoblox ---

def _roblox_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_roblox"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'🔴 Выключить' if enabled else '🟢 Включить'}",
        callback_data="plugins:roblox:toggle",
    )
    builder.button(text="✏️ Настроить кол-во Robux", callback_data="plugins:roblox:set_amount")
    builder.button(text="📝 Заметка для покупателя", callback_data="plugins:roblox:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(1)
    return builder.as_markup()


def _roblox_text(settings: dict) -> str:
    p = settings["plugins"]["auto_roblox"]
    enabled = p.get("enabled", False)
    robux = p.get("robux", 0)
    note = p.get("note") or "—"
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    return (
        "🎮 <b>AutoRoblox</b>\n\n"
        "Автоматически отправляет Robux покупателю при новом заказе.\n\n"
        f"Статус: {status}\n"
        f"Кол-во Robux: <b>{robux}</b>\n"
        f"Заметка: <i>{note}</i>"
    )


# --- AutoGifts ---

def _gifts_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_gifts"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'🔴 Выключить' if enabled else '🟢 Включить'}",
        callback_data="plugins:gifts:toggle",
    )
    builder.button(text="✏️ Тип подарка", callback_data="plugins:gifts:set_type")
    builder.button(text="📝 Заметка для покупателя", callback_data="plugins:gifts:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.adjust(1)
    return builder.as_markup()


def _gifts_text(settings: dict) -> str:
    p = settings["plugins"]["auto_gifts"]
    enabled = p.get("enabled", False)
    gift_type = p.get("gift_type") or "—"
    note = p.get("note") or "—"
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    return (
        "🎁 <b>AutoGifts</b>\n\n"
        "Автоматически отправляет подарок покупателю при новом заказе.\n\n"
        f"Статус: {status}\n"
        f"Тип подарка: <b>{gift_type}</b>\n"
        f"Заметка: <i>{note}</i>"
    )


# ---------------------------------------------------------------------------
# Handlers — menu
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "plugins:menu")
async def plugins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🔌 <b>Плагины</b>\n\n"
        "Автоматическая доставка цифровых товаров при новых заказах.",
        reply_markup=_plugins_menu_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# AutoStars handlers
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "plugins:auto_stars")
async def stars_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(
        _stars_text(settings),
        reply_markup=_stars_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:toggle")
async def stars_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["enabled"] = not settings["plugins"]["auto_stars"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(
        _stars_text(settings),
        reply_markup=_stars_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:set_amount")
async def stars_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_stars_amount)
    await callback.message.answer("⭐ Введите количество звёзд для отправки:")
    await callback.answer()


@router.message(PluginState.waiting_stars_amount)
async def stars_set_amount_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое положительное число:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["amount"] = amount
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Кол-во звёзд обновлено: <b>{amount}</b>",
        reply_markup=_stars_keyboard(settings),
    )


@router.callback_query(F.data == "plugins:stars:set_note")
async def stars_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_stars_note)
    await callback.message.answer("📝 Введите заметку для покупателя (AutoStars):")
    await callback.answer()


@router.message(PluginState.waiting_stars_note)
async def stars_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Заметка сохранена: <i>{note or '—'}</i>",
        reply_markup=_stars_keyboard(settings),
    )


# ---------------------------------------------------------------------------
# AutoRoblox handlers
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "plugins:auto_roblox")
async def roblox_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(
        _roblox_text(settings),
        reply_markup=_roblox_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:toggle")
async def roblox_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["enabled"] = not settings["plugins"]["auto_roblox"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(
        _roblox_text(settings),
        reply_markup=_roblox_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:set_amount")
async def roblox_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_roblox_amount)
    await callback.message.answer("🎮 Введите количество Robux:")
    await callback.answer()


@router.message(PluginState.waiting_roblox_amount)
async def roblox_set_amount_input(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        amount = int(text)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["robux"] = amount
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Кол-во Robux обновлено: <b>{amount}</b>",
        reply_markup=_roblox_keyboard(settings),
    )


@router.callback_query(F.data == "plugins:roblox:set_note")
async def roblox_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_roblox_note)
    await callback.message.answer("📝 Введите заметку для покупателя (AutoRoblox):")
    await callback.answer()


@router.message(PluginState.waiting_roblox_note)
async def roblox_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Заметка сохранена: <i>{note or '—'}</i>",
        reply_markup=_roblox_keyboard(settings),
    )


# ---------------------------------------------------------------------------
# AutoGifts handlers
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "plugins:auto_gifts")
async def gifts_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(
        _gifts_text(settings),
        reply_markup=_gifts_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:toggle")
async def gifts_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["enabled"] = not settings["plugins"]["auto_gifts"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(
        _gifts_text(settings),
        reply_markup=_gifts_keyboard(settings),
    )
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:set_type")
async def gifts_set_type_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_gifts_type)
    await callback.message.answer("🎁 Введите тип подарка:")
    await callback.answer()


@router.message(PluginState.waiting_gifts_type)
async def gifts_set_type_input(message: Message, state: FSMContext) -> None:
    gift_type = (message.text or "").strip()
    if not gift_type:
        await message.answer("❌ Тип подарка не может быть пустым:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["gift_type"] = gift_type
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Тип подарка сохранён: <b>{gift_type}</b>",
        reply_markup=_gifts_keyboard(settings),
    )


@router.callback_query(F.data == "plugins:gifts:set_note")
async def gifts_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.waiting_gifts_note)
    await callback.message.answer("📝 Введите заметку для покупателя (AutoGifts):")
    await callback.answer()


@router.message(PluginState.waiting_gifts_note)
async def gifts_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(
        f"✅ Заметка сохранена: <i>{note or '—'}</i>",
        reply_markup=_gifts_keyboard(settings),
    )
