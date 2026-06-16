from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_settings, save_settings, get_shop_name

router = Router()


class PluginState(StatesGroup):
    # AutoStars
    stars_manual_buyer = State()
    stars_manual_amount = State()
    stars_set_amount = State()
    stars_set_note = State()
    # AutoRoblox
    roblox_manual_buyer = State()
    roblox_manual_amount = State()
    roblox_set_amount = State()
    roblox_set_note = State()
    # AutoGifts
    gifts_manual_buyer = State()
    gifts_set_type = State()
    gifts_set_note = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


def _plugins_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ AutoStars", callback_data="plugins:auto_stars")
    builder.button(text="🎮 AutoRoblox", callback_data="plugins:auto_roblox")
    builder.button(text="🎁 AutoGifts", callback_data="plugins:auto_gifts")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# AutoStars
# ---------------------------------------------------------------------------

def _stars_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_stars"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"⭐ <b>Telegram — Звёзды{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
    )


def _stars_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_stars"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:stars:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:stars:accumulated")
    builder.button(text="💎 Прибыль", callback_data="plugins:stars:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:stars:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:stars:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:stars:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:stars:toggle")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.button(text="⚙️ Настройки", callback_data="plugins:stars:settings")
    builder.adjust(1, 1, 2, 3, 2)
    return builder.as_markup()


def _stars_settings_text(settings: dict) -> str:
    p = settings["plugins"]["auto_stars"]
    amount = p.get("amount", 50)
    note = p.get("note") or "—"
    return (
        f"⚙️ <b>Настройки AutoStars</b>\n\n"
        f"⭐ Кол-во звёзд: <b>{amount}</b>\n"
        f"📝 Заметка: <i>{note}</i>"
    )


def _stars_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Кол-во звёзд", callback_data="plugins:stars:set_amount")
    builder.button(text="📝 Заметка", callback_data="plugins:stars:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_stars")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_stars")
async def stars_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    shop_name = get_shop_name(uid)
    await callback.message.edit_text(_stars_text(settings, shop_name), reply_markup=_stars_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:toggle")
async def stars_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["enabled"] = not settings["plugins"]["auto_stars"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_stars_text(settings, get_shop_name(uid)), reply_markup=_stars_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:settings")
async def stars_settings(callback: CallbackQuery) -> None:
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_stars_settings_text(settings), reply_markup=_stars_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "plugins:stars:set_amount")
async def stars_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_amount)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_stars"].get("amount", 50)
    await callback.message.edit_text(
        f"⭐ Кол-во звёзд (сейчас: <b>{cur}</b>)\n\nВведите число:",
        reply_markup=_cancel_kb("plugins:stars:settings"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_amount)
async def stars_set_amount_input(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
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
    await message.answer(f"✅ Кол-во звёзд: <b>{amount}</b>", reply_markup=_stars_settings_keyboard())


@router.callback_query(F.data == "plugins:stars:set_note")
async def stars_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_set_note)
    await callback.message.edit_text(
        "📝 Введите заметку для покупателя:",
        reply_markup=_cancel_kb("plugins:stars:settings"),
    )
    await callback.answer()


@router.message(PluginState.stars_set_note)
async def stars_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_stars"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка сохранена: <i>{note or '—'}</i>", reply_markup=_stars_settings_keyboard())


@router.callback_query(F.data == "plugins:stars:manual")
async def stars_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.stars_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача звёзд</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_stars"),
    )
    await callback.answer()


@router.message(PluginState.stars_manual_buyer)
async def stars_manual_buyer_input(message: Message, state: FSMContext) -> None:
    await state.update_data(buyer=message.text or "")
    p = get_settings(message.from_user.id)["plugins"]["auto_stars"]
    default_amount = p.get("amount", 50)
    await state.set_state(PluginState.stars_manual_amount)
    await message.answer(
        f"⭐ Кол-во звёзд (по умолчанию: {default_amount}):\n\nВведите число или отправьте 0 для значения по умолчанию:"
    )


@router.message(PluginState.stars_manual_amount)
async def stars_manual_amount_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buyer = data.get("buyer", "—")
    await state.clear()
    try:
        amount = int((message.text or "").strip())
        if amount == 0:
            amount = get_settings(message.from_user.id)["plugins"]["auto_stars"].get("amount", 50)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return
    await message.answer(
        f"⭐ <b>Выдача звёзд</b>\n\n"
        f"👤 Покупатель: <b>{buyer}</b>\n"
        f"⭐ Кол-во: <b>{amount}</b>\n\n"
        f"⚠️ Функция отправки звёзд будет доступна в следующем обновлении.",
    )


@router.callback_query(F.data == "plugins:stars:accumulated")
async def stars_accumulated(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:profit")
async def stars_profit(callback: CallbackQuery) -> None:
    await callback.answer("📊 Статистика прибыли появится в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:balance")
async def stars_balance(callback: CallbackQuery) -> None:
    await callback.answer("💰 Баланс звёзд появится в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:notifs")
async def stars_notifs(callback: CallbackQuery) -> None:
    await callback.answer("🔔 Настройки уведомлений появятся в следующем обновлении", show_alert=True)


@router.callback_query(F.data == "plugins:stars:replies")
async def stars_replies(callback: CallbackQuery) -> None:
    await callback.answer("💬 Авто-ответы появятся в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# AutoRoblox
# ---------------------------------------------------------------------------

def _roblox_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_roblox"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"🎮 <b>Roblox — Robux{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
    )


def _roblox_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_roblox"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:roblox:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:roblox:accumulated")
    builder.button(text="💎 Прибыль", callback_data="plugins:roblox:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:roblox:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:roblox:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:roblox:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:roblox:toggle")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.button(text="⚙️ Настройки", callback_data="plugins:roblox:settings")
    builder.adjust(1, 1, 2, 3, 2)
    return builder.as_markup()


def _roblox_settings_text(settings: dict) -> str:
    p = settings["plugins"]["auto_roblox"]
    robux = p.get("robux", 0)
    note = p.get("note") or "—"
    return (
        f"⚙️ <b>Настройки AutoRoblox</b>\n\n"
        f"🎮 Кол-во Robux: <b>{robux}</b>\n"
        f"📝 Заметка: <i>{note}</i>"
    )


def _roblox_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Кол-во Robux", callback_data="plugins:roblox:set_amount")
    builder.button(text="📝 Заметка", callback_data="plugins:roblox:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_roblox")
async def roblox_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(_roblox_text(settings, get_shop_name(uid)), reply_markup=_roblox_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:toggle")
async def roblox_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["enabled"] = not settings["plugins"]["auto_roblox"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_roblox_text(settings, get_shop_name(uid)), reply_markup=_roblox_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:settings")
async def roblox_settings(callback: CallbackQuery) -> None:
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_roblox_settings_text(settings), reply_markup=_roblox_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "plugins:roblox:set_amount")
async def roblox_set_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_set_amount)
    cur = get_settings(callback.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
    await callback.message.edit_text(
        f"🎮 Кол-во Robux (сейчас: <b>{cur}</b>)\n\nВведите число:",
        reply_markup=_cancel_kb("plugins:roblox:settings"),
    )
    await callback.answer()


@router.message(PluginState.roblox_set_amount)
async def roblox_set_amount_input(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
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
    await message.answer(f"✅ Кол-во Robux: <b>{amount}</b>", reply_markup=_roblox_settings_keyboard())


@router.callback_query(F.data == "plugins:roblox:set_note")
async def roblox_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_set_note)
    await callback.message.edit_text("📝 Введите заметку для покупателя:", reply_markup=_cancel_kb("plugins:roblox:settings"))
    await callback.answer()


@router.message(PluginState.roblox_set_note)
async def roblox_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_roblox"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка: <i>{note or '—'}</i>", reply_markup=_roblox_settings_keyboard())


@router.callback_query(F.data == "plugins:roblox:manual")
async def roblox_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.roblox_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача Robux</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_roblox"),
    )
    await callback.answer()


@router.message(PluginState.roblox_manual_buyer)
async def roblox_manual_buyer_input(message: Message, state: FSMContext) -> None:
    await state.update_data(buyer=message.text or "")
    await state.set_state(PluginState.roblox_manual_amount)
    default = get_settings(message.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
    await message.answer(f"🎮 Кол-во Robux (по умолчанию: {default}), 0 = значение по умолчанию:")


@router.message(PluginState.roblox_manual_amount)
async def roblox_manual_amount_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buyer = data.get("buyer", "—")
    await state.clear()
    try:
        amount = int((message.text or "").strip())
        if amount == 0:
            amount = get_settings(message.from_user.id)["plugins"]["auto_roblox"].get("robux", 0)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return
    await message.answer(
        f"🎮 <b>Выдача Robux</b>\n\n"
        f"👤 Покупатель: <b>{buyer}</b>\n"
        f"🎮 Кол-во: <b>{amount}</b>\n\n"
        f"⚠️ Функция отправки Robux будет доступна в следующем обновлении."
    )


@router.callback_query(F.data.in_({"plugins:roblox:accumulated", "plugins:roblox:profit",
                                    "plugins:roblox:balance", "plugins:roblox:notifs", "plugins:roblox:replies"}))
async def roblox_stub(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# AutoGifts
# ---------------------------------------------------------------------------

def _gifts_text(settings: dict, shop_name: str = "") -> str:
    p = settings["plugins"]["auto_gifts"]
    enabled = p.get("enabled", False)
    note = p.get("note") or "—"
    name_part = f" • {shop_name}" if shop_name else ""
    status = "🟢 Автовыдача включена" if enabled else "🔴 Автовыдача выключена"
    return (
        f"🎁 <b>Telegram — Подарки{name_part}</b>\n\n"
        f"{status}\n"
        f"{note}\n\n"
        "Раздел управления плагином.\n"
        "Контроль, настройки, ручная выдача — всё здесь."
    )


def _gifts_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = settings["plugins"]["auto_gifts"].get("enabled", False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Ручная выдача", callback_data="plugins:gifts:manual")
    builder.button(text="📦 Выдать накопленные", callback_data="plugins:gifts:accumulated")
    builder.button(text="💎 Прибыль", callback_data="plugins:gifts:profit")
    builder.button(text="💰 Баланс", callback_data="plugins:gifts:balance")
    builder.button(text="🔔 Уведомления", callback_data="plugins:gifts:notifs")
    builder.button(text="💬 Ответы", callback_data="plugins:gifts:replies")
    builder.button(text="▶️ Включить" if not enabled else "⏸ Выключить", callback_data="plugins:gifts:toggle")
    builder.button(text="⬅️ Назад", callback_data="plugins:menu")
    builder.button(text="⚙️ Настройки", callback_data="plugins:gifts:settings")
    builder.adjust(1, 1, 2, 3, 2)
    return builder.as_markup()


def _gifts_settings_text(settings: dict) -> str:
    p = settings["plugins"]["auto_gifts"]
    gift_type = p.get("gift_type") or "—"
    note = p.get("note") or "—"
    return (
        f"⚙️ <b>Настройки AutoGifts</b>\n\n"
        f"🎁 Тип подарка: <b>{gift_type}</b>\n"
        f"📝 Заметка: <i>{note}</i>"
    )


def _gifts_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Тип подарка", callback_data="plugins:gifts:set_type")
    builder.button(text="📝 Заметка", callback_data="plugins:gifts:set_note")
    builder.button(text="⬅️ Назад", callback_data="plugins:auto_gifts")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "plugins:auto_gifts")
async def gifts_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = callback.from_user.id
    settings = get_settings(uid)
    await callback.message.edit_text(_gifts_text(settings, get_shop_name(uid)), reply_markup=_gifts_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:toggle")
async def gifts_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["enabled"] = not settings["plugins"]["auto_gifts"].get("enabled", False)
    save_settings(uid, settings)
    await callback.message.edit_text(_gifts_text(settings, get_shop_name(uid)), reply_markup=_gifts_keyboard(settings))
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:settings")
async def gifts_settings(callback: CallbackQuery) -> None:
    settings = get_settings(callback.from_user.id)
    await callback.message.edit_text(_gifts_settings_text(settings), reply_markup=_gifts_settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "plugins:gifts:set_type")
async def gifts_set_type_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_set_type)
    await callback.message.edit_text("🎁 Введите тип подарка:", reply_markup=_cancel_kb("plugins:gifts:settings"))
    await callback.answer()


@router.message(PluginState.gifts_set_type)
async def gifts_set_type_input(message: Message, state: FSMContext) -> None:
    gift_type = (message.text or "").strip()
    if not gift_type:
        await message.answer("❌ Тип не может быть пустым:")
        return
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["gift_type"] = gift_type
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Тип подарка: <b>{gift_type}</b>", reply_markup=_gifts_settings_keyboard())


@router.callback_query(F.data == "plugins:gifts:set_note")
async def gifts_set_note_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_set_note)
    await callback.message.edit_text("📝 Введите заметку для покупателя:", reply_markup=_cancel_kb("plugins:gifts:settings"))
    await callback.answer()


@router.message(PluginState.gifts_set_note)
async def gifts_set_note_input(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    uid = message.from_user.id
    settings = get_settings(uid)
    settings["plugins"]["auto_gifts"]["note"] = note
    save_settings(uid, settings)
    await state.clear()
    await message.answer(f"✅ Заметка: <i>{note or '—'}</i>", reply_markup=_gifts_settings_keyboard())


@router.callback_query(F.data == "plugins:gifts:manual")
async def gifts_manual_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PluginState.gifts_manual_buyer)
    await callback.message.edit_text(
        "🚀 <b>Ручная выдача подарка</b>\n\nВведите @username или Telegram ID покупателя:",
        reply_markup=_cancel_kb("plugins:auto_gifts"),
    )
    await callback.answer()


@router.message(PluginState.gifts_manual_buyer)
async def gifts_manual_buyer_input(message: Message, state: FSMContext) -> None:
    buyer = (message.text or "").strip()
    await state.clear()
    gift_type = get_settings(message.from_user.id)["plugins"]["auto_gifts"].get("gift_type") or "—"
    await message.answer(
        f"🎁 <b>Выдача подарка</b>\n\n"
        f"👤 Покупатель: <b>{buyer}</b>\n"
        f"🎁 Тип: <b>{gift_type}</b>\n\n"
        f"⚠️ Функция отправки подарков будет доступна в следующем обновлении."
    )


@router.callback_query(F.data.in_({"plugins:gifts:accumulated", "plugins:gifts:profit",
                                    "plugins:gifts:balance", "plugins:gifts:notifs", "plugins:gifts:replies"}))
async def gifts_stub(callback: CallbackQuery) -> None:
    await callback.answer("⚠️ Функция появится в следующем обновлении", show_alert=True)


# ---------------------------------------------------------------------------
# Plugins main menu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "plugins:menu")
async def plugins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🧩 <b>Плагины</b>\n\n"
        "Автоматическая доставка цифровых товаров при новых заказах.",
        reply_markup=_plugins_menu_keyboard(),
    )
    await callback.answer()
