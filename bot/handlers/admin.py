"""Owner/admin panel: subscriptions, blacklist, stats, bot price, broadcasts,
granting admin. Access limited to the owner and granted admins."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import (
    add_admin, block_user, count_subscribers, count_users, get_all_users,
    get_bot_price, get_subscription, grant_subscription, is_admin, is_owner,
    list_admins, list_blocked, remove_admin, require_subscription_enabled,
    revoke_subscription, set_bot_price, set_require_subscription,
    subscription_days_left, unblock_user,
)

router = Router()
logger = logging.getLogger(__name__)


class AdminState(StatesGroup):
    sub_user = State()
    sub_days = State()
    block_user = State()
    unblock_user = State()
    price = State()
    broadcast = State()
    add_admin = State()
    remove_admin = State()


def _menu_kb(uid: int):
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="🎫 Выдать подписку", callback_data="admin:sub")
    b.button(text="🚫 Чёрный список", callback_data="admin:blacklist")
    b.button(text="💰 Цена бота", callback_data="admin:price")
    b.button(text="📢 Рассылка", callback_data="admin:broadcast")
    b.button(text="🔐 Требовать подписку", callback_data="admin:toggle_sub")
    if is_owner(uid):
        b.button(text="👑 Управление админами", callback_data="admin:admins")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


async def _show_menu(msg, uid: int, edit: bool = True) -> None:
    req = "🟢 вкл" if require_subscription_enabled() else "🔴 выкл"
    text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{count_users()}</b>\n"
        f"🎫 С подпиской: <b>{count_subscribers()}</b>\n"
        f"💰 Цена бота: <b>{get_bot_price()} ₽</b>\n"
        f"🔐 Требовать подписку: <b>{req}</b>"
    )
    if edit:
        await msg.edit_text(text, reply_markup=_menu_kb(uid))
    else:
        await msg.answer(text, reply_markup=_menu_kb(uid))


@router.message(F.text == "/admin")
async def admin_cmd(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await _show_menu(message, message.from_user.id, edit=False)


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await _show_menu(callback.message, callback.from_user.id)
    await callback.answer()


# ── Статистика ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    subs_left = []
    for uid in get_all_users():
        d = subscription_days_left(uid)
        if d > 0:
            subs_left.append((uid, d))
    subs_left.sort(key=lambda x: x[1], reverse=True)
    lines = [
        "📊 <b>Статистика бота</b>\n",
        f"👥 Всего пользователей: <b>{count_users()}</b>",
        f"🎫 Активных подписок: <b>{count_subscribers()}</b>",
        f"🚫 В чёрном списке: <b>{len(list_blocked())}</b>",
        f"👑 Админов: <b>{len(list_admins())}</b>",
        f"💰 Цена бота: <b>{get_bot_price()} ₽</b>",
    ]
    if subs_left:
        lines.append("\n<b>Подписки (топ по остатку):</b>")
        for uid, d in subs_left[:15]:
            lines.append(f"• <code>{uid}</code> — {d} дн.")
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="admin:stats")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


# ── Подписки ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sub")
async def sub_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.sub_user)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:menu")
    await callback.message.edit_text(
        "🎫 <b>Выдать подписку</b>\n\nВведите <b>ID пользователя</b>:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.sub_user)
async def sub_user_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом. Введите ещё раз:")
        return
    await state.update_data(sub_target=target)
    await state.set_state(AdminState.sub_days)
    b = InlineKeyboardBuilder()
    for d in (7, 30, 90, 365):
        b.button(text=f"{d} дн.", callback_data=f"admin:subdays:{d}")
    b.button(text="❌ Отмена", callback_data="admin:menu")
    b.adjust(4, 1)
    await message.answer(
        f"🎫 Пользователь <code>{target}</code>\n\n"
        "Выберите срок или введите число дней:",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("admin:subdays:"))
async def sub_days_btn(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    await _do_grant(callback.message, state, days, callback.from_user.id,
                    callback.bot)
    await callback.answer()


@router.message(AdminState.sub_days)
async def sub_days_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
        if days <= 0 or days > 3650:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число дней (1–3650):")
        return
    await _do_grant(message, state, days, message.from_user.id, message.bot)


async def _do_grant(msg, state: FSMContext, days: int, admin_id: int, bot: Bot) -> None:
    data = await state.get_data()
    target = data.get("sub_target")
    await state.clear()
    if not target:
        await msg.answer("❌ Потеряна цель. Начните заново.")
        return
    grant_subscription(target, days, by=admin_id)
    left = subscription_days_left(target)
    b = InlineKeyboardBuilder()
    b.button(text="🎫 Ещё подписку", callback_data="admin:sub")
    b.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    b.adjust(1)
    text = (f"✅ Подписка выдана!\n\n"
            f"👤 <code>{target}</code>\n"
            f"➕ {days} дн.  →  всего <b>{left} дн.</b>")
    if hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text, reply_markup=b.as_markup())
        except Exception:
            await msg.answer(text, reply_markup=b.as_markup())
    else:
        await msg.answer(text, reply_markup=b.as_markup())
    # notify the client
    try:
        await bot.send_message(
            target,
            f"🎉 Вам выдана подписка на бота на <b>{days} дн.</b>!\n"
            f"Осталось: <b>{left} дн.</b>",
        )
    except Exception:
        pass


# ── Чёрный список ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:blacklist")
async def blacklist_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    blocked = list_blocked()
    lines = ["🚫 <b>Чёрный список</b>\n"]
    if blocked:
        for u in blocked[:30]:
            lines.append(f"• <code>{u}</code>")
    else:
        lines.append("Список пуст.")
    b = InlineKeyboardBuilder()
    b.button(text="➕ Заблокировать", callback_data="admin:block")
    b.button(text="➖ Разблокировать", callback_data="admin:unblock")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(2, 1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:block")
async def block_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.block_user)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:blacklist")
    await callback.message.edit_text("🚫 Введите ID для блокировки:",
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.message(AdminState.block_user)
async def block_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом:")
        return
    await state.clear()
    if is_owner(target) or is_admin(target):
        await message.answer("❌ Нельзя заблокировать админа.")
        return
    block_user(target)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Чёрный список", callback_data="admin:blacklist")
    await message.answer(f"🚫 Пользователь <code>{target}</code> заблокирован.",
                         reply_markup=b.as_markup())


@router.callback_query(F.data == "admin:unblock")
async def unblock_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.unblock_user)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:blacklist")
    await callback.message.edit_text("➖ Введите ID для разблокировки:",
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.message(AdminState.unblock_user)
async def unblock_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом:")
        return
    await state.clear()
    ok = unblock_user(target)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Чёрный список", callback_data="admin:blacklist")
    await message.answer(
        f"{'✅ Разблокирован' if ok else 'ℹ️ Не был в списке'}: <code>{target}</code>",
        reply_markup=b.as_markup())


# ── Цена бота ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:price")
async def price_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.price)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:menu")
    await callback.message.edit_text(
        f"💰 <b>Цена бота</b>\n\nТекущая: <b>{get_bot_price()} ₽</b>\n\n"
        "Введите новую цену (число):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.price)
async def price_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        price = int((message.text or "").strip().replace(" ", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число:")
        return
    await state.clear()
    set_bot_price(price)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    await message.answer(f"✅ Цена бота: <b>{price} ₽</b>", reply_markup=b.as_markup())


# ── Требовать подписку ──────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:toggle_sub")
async def toggle_sub(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    set_require_subscription(not require_subscription_enabled())
    state_txt = "включено" if require_subscription_enabled() else "выключено"
    await callback.answer(f"Требование подписки {state_txt}", show_alert=True)
    await _show_menu(callback.message, callback.from_user.id)


# ── Рассылка ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.broadcast)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:menu")
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправьте сообщение (текст, фото или видео с подписью) — "
        "оно будет разослано всем пользователям бота.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.broadcast)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    users = get_all_users()
    status = await message.answer(f"📤 Рассылка запущена для {len(users)} польз…")
    sent = failed = 0
    for uid in users:
        try:
            await message.send_copy(chat_id=uid)
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 20 == 0:
            await asyncio.sleep(0.5)  # rate-limit courtesy
        else:
            await asyncio.sleep(0.05)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Админ-панель", callback_data="admin:menu")
    await status.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        reply_markup=b.as_markup(),
    )


# ── Управление админами (только владелец) ──────────────────────────────────

@router.callback_query(F.data == "admin:admins")
async def admins_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer()  # invisible to granted admins — no hint it exists
        return
    await state.clear()
    admins = list_admins()
    lines = ["👑 <b>Админы</b>\n"]
    for a in admins:
        tag = " (владелец)" if is_owner(a) else ""
        lines.append(f"• <code>{a}</code>{tag}")
    b = InlineKeyboardBuilder()
    b.button(text="➕ Выдать админку", callback_data="admin:add_admin")
    b.button(text="➖ Забрать админку", callback_data="admin:rm_admin")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    b.adjust(2, 1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:add_admin")
async def add_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminState.add_admin)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:admins")
    await callback.message.edit_text("👑 Введите ID нового админа:",
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.message(AdminState.add_admin)
async def add_admin_input(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом:")
        return
    await state.clear()
    add_admin(target)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Админы", callback_data="admin:admins")
    await message.answer(f"👑 <code>{target}</code> теперь админ.",
                         reply_markup=b.as_markup())
    try:
        await message.bot.send_message(target, "👑 Вам выдали права админа бота! /admin")
    except Exception:
        pass


@router.callback_query(F.data == "admin:rm_admin")
async def rm_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminState.remove_admin)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:admins")
    await callback.message.edit_text("➖ Введите ID админа для снятия:",
                                     reply_markup=b.as_markup())
    await callback.answer()


@router.message(AdminState.remove_admin)
async def rm_admin_input(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    try:
        target = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом:")
        return
    await state.clear()
    ok = remove_admin(target)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Админы", callback_data="admin:admins")
    await message.answer(
        f"{'✅ Снят' if ok else 'ℹ️ Не был админом'}: <code>{target}</code>",
        reply_markup=b.as_markup())
