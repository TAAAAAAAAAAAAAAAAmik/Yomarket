"""Панель владельца: подписки, чёрный список, статистика, цена бота,
рассылки, выдача админки. Доступ — только владельцу и выданным админам."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from storage import (
    PRICE_TIERS,
    POLICY_DOCS,
    CUSTOM_TEXTS, MENU_BUTTONS, add_admin, block_user, clear_custom_text,
    clear_header_emoji, count_subscribers, count_users, get_all_users,
    get_bot_price, get_custom_text, get_header_emoji, get_menu_labels,
    get_subscription, grant_subscription, is_admin, is_custom_text_set,
    is_owner, list_admins, list_blocked, remove_admin,
    require_subscription_enabled, reset_menu_labels, revoke_subscription,
    set_custom_text, set_header_emoji, set_menu_label,
    set_require_subscription, subscription_days_left, unblock_user,
)

router = Router()
logger = logging.getLogger(__name__)


class AdminState(StatesGroup):
    sub_user = State()
    sub_days = State()
    block_user = State()
    unblock_user = State()
    broadcast = State()
    add_admin = State()
    remove_admin = State()
    doc_url = State()
    tier_price = State()
    trial_days = State()
    menu_label = State()
    header_emoji = State()
    edit_text = State()


def _menu_kb(uid: int):
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin:stats")
    b.button(text="🎫 Выдать подписку", callback_data="admin:sub")
    b.button(text="🚫 Чёрный список", callback_data="admin:blacklist")
    b.button(text="💰 Тарифы", callback_data="admin:prices")
    b.button(text="📢 Рассылка", callback_data="admin:broadcast")
    b.button(text="🎨 Оформление", callback_data="admin:appearance")
    b.button(text="📝 Тексты сообщений", callback_data="admin:texts")
    b.button(text="📄 Правовые документы", callback_data="admin:docs")
    b.button(text="🔐 Требовать подписку", callback_data="admin:toggle_sub")
    if is_owner(uid):
        b.button(text="👑 Управление админами", callback_data="admin:admins")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    if is_owner(uid):
        b.adjust(2, 2, 2, 2, 1, 1)  # 9 actions (2-per-row) + nav on its own row
    else:
        b.adjust(2, 2, 2, 2, 1)  # 8 actions (2-per-row) + nav on its own row
    return b.as_markup()


async def _show_menu(msg, uid: int, edit: bool = True) -> None:
    req = "🟢 вкл" if require_subscription_enabled() else "🔴 выкл"
    text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{count_users()}</b>\n"
        f"🎫 С подпиской: <b>{count_subscribers()}</b>\n"
        f"💰 Тарифов задано: <b>{len(_prices())}</b> из {len(PRICE_TIERS)}\n"
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
    b.adjust(2)
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
    b.adjust(2, 2, 1)
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
    ui.lay(b)
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
    # Здороваемся с клиентом и сразу ставим его на шаг подключения. Состояние
    # «жду токен» здесь не мелочь: приветствие просит прислать токен, а без
    # состояния его ответ не поймает никто.
    try:
        from storage import get_token, render_custom_text
        await bot.send_message(
            target, render_custom_text("sub_granted", days=days, left=left),
            disable_web_page_preview=True)
        if not get_token(target):
            await _arm_token_state(state, target, bot)
    except Exception as e:
        logger.warning("sub_granted notify %s: %s", target, e)


async def _arm_token_state(state: FSMContext, target: int, bot: Bot) -> None:
    """Поставить ЧУЖУЮ форму в состояние «жду токен».

    Приветствие уходит клиенту, а не админу, который выдал подписку, — значит
    и состояние записывается по ключу клиента. По своему ключу оно ловило бы
    ответы админа, а клиента бот бы не услышал.
    """
    from aiogram.fsm.storage.base import StorageKey

    from handlers.start import AuthState

    storage = getattr(state, "storage", None)
    if storage is None:
        return
    key = StorageKey(bot_id=bot.id, chat_id=int(target), user_id=int(target))
    await storage.set_state(key, AuthState.waiting_for_token)


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


# ── Оформление (кнопки меню + кастом-эмодзи шапки) ──────────────────────────

_MENU_KEYS = {key: default for key, default, _cb in MENU_BUTTONS}


@router.callback_query(F.data == "admin:appearance")
async def appearance_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    he = get_header_emoji()
    header_line = (f"🔣 Кастом-эмодзи шапки: <b>задан</b> ({he.get('fallback')})"
                   if he else "🔣 Кастом-эмодзи шапки: <b>нет</b>")
    text = (
        "🎨 <b>Оформление</b>\n\n"
        "Настройте подписи кнопок главного меню (эмодзи + текст) и "
        "кастом-эмодзи в шапке.\n\n"
        f"{header_line}\n\n"
        "ℹ️ <i>Цвет кнопок Telegram менять не даёт — «покрасить» можно "
        "только цветными эмодзи в подписи (🔴🟢🟡🔵🟣🟠).</i>"
    )
    labels = get_menu_labels()
    b = InlineKeyboardBuilder()
    for key, _default, _cb in MENU_BUTTONS:
        b.button(text=f"✏️ {labels.get(key, _default)}", callback_data=f"admin:lbl:{key}")
    b.adjust(2)
    b.button(text="🔣 Кастом-эмодзи в шапку", callback_data="admin:hdremoji")
    if get_header_emoji():
        b.button(text="🗑 Убрать эмодзи шапки", callback_data="admin:hdrclear")
    b.button(text="♻️ Сбросить кнопки", callback_data="admin:lblreset")
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    if get_header_emoji():
        b.adjust(2, 2, 2, 2, 2, 1)  # 10 action buttons (2-per-row) + nav on its own row
    else:
        b.adjust(2, 2, 2, 2, 1, 1)  # 9 action buttons (2-per-row) + nav on its own row
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:lbl:"))
async def label_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[-1]
    if key not in _MENU_KEYS:
        await callback.answer()
        return
    await state.set_state(AdminState.menu_label)
    await state.update_data(label_key=key)
    cur = get_menu_labels().get(key, _MENU_KEYS[key])
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:appearance")
    await callback.message.edit_text(
        f"✏️ <b>Кнопка меню</b>\n\nТекущая: <b>{cur}</b>\n\n"
        "Отправьте новую подпись (эмодзи + текст).\n"
        "Пример: <code>🟢 Мои товары</code>\n\n"
        "<i>Цветные кружки для «покраски»: 🔴🟢🟡🔵🟣🟠⚫⚪🟤</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.menu_label)
async def label_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    label = (message.text or "").strip()[:40]
    data = await state.get_data()
    key = data.get("label_key")
    await state.clear()
    if not label or not key:
        await message.answer("❌ Пустая подпись.")
        return
    set_menu_label(key, label)
    b = InlineKeyboardBuilder()
    b.button(text="🎨 К оформлению", callback_data="admin:appearance")
    await message.answer(
        f"✅ Кнопка обновлена: <b>{label}</b>\n"
        "Изменение видно в главном меню (/start).",
        reply_markup=b.as_markup())


@router.callback_query(F.data == "admin:lblreset")
async def label_reset(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reset_menu_labels()
    await callback.answer("Кнопки сброшены к стандартным", show_alert=True)
    await appearance_menu(callback, _DummyState())


@router.callback_query(F.data == "admin:hdremoji")
async def header_emoji_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.header_emoji)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="admin:appearance")
    await callback.message.edit_text(
        "🔣 <b>Кастом-эмодзи в шапку меню</b>\n\n"
        "Отправьте сообщение, в котором есть <b>кастом-эмодзи</b> "
        "(премиум/из набора). Бот запомнит первый и поставит его в заголовок "
        "«Главное меню».\n\n"
        "<i>Обычные эмодзи тоже подойдут — возьму первый символ.</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.header_emoji)
async def header_emoji_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    # ищем в сообщении кастомное эмодзи
    emoji_id = None
    fallback = "🏠"
    for ent in (message.entities or []):
        if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            emoji_id = ent.custom_emoji_id
            # запасной значок — это тот текст, который эмодзи собой закрывает
            txt = message.text or ""
            fallback = txt[ent.offset:ent.offset + ent.length] or "🏠"
            break
    b = InlineKeyboardBuilder()
    b.button(text="🎨 К оформлению", callback_data="admin:appearance")
    if emoji_id:
        set_header_emoji(emoji_id, fallback)
        await message.answer(
            f"✅ Кастом-эмодзи сохранён (запасной символ: {fallback}).\n"
            "Откройте /start — увидите его в шапке меню.",
            reply_markup=b.as_markup())
    else:
        # кастомного эмодзи нет — берём в заголовок первый обычный значок
        # (id="" → menu_header_html нарисует запасной значок как есть)
        txt = (message.text or "").strip()
        if txt:
            set_header_emoji("", txt[0])
            await message.answer(
                f"✅ Символ шапки: <b>{txt[0]}</b> (обычный эмодзи).",
                reply_markup=b.as_markup())
        else:
            await message.answer(
                "❌ Не нашёл эмодзи в сообщении. Пришлите сообщение с эмодзи.",
                reply_markup=b.as_markup())


@router.callback_query(F.data == "admin:hdrclear")
async def header_emoji_clear(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    clear_header_emoji()
    await callback.answer("Эмодзи шапки убрано", show_alert=True)
    await appearance_menu(callback, _DummyState())


class _DummyState:
    async def clear(self): pass
    async def set_state(self, *a, **k): pass
    async def update_data(self, **k): pass
    async def get_data(self): return {}


# ── Редактор текстов сообщений (с кастом-эмодзи) ────────────────────────────

@router.callback_query(F.data == "admin:texts")
async def texts_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    b = InlineKeyboardBuilder()
    for key, meta in CUSTOM_TEXTS.items():
        mark = "✏️" if is_custom_text_set(key) else "○"
        b.button(text=f"{mark} {meta['title']}", callback_data=f"admin:txt:{key}")
    b.adjust(1)
    b.button(text="⬅️ Назад", callback_data="admin:menu")
    await callback.message.edit_text(
        "📝 <b>Тексты сообщений</b>\n\n"
        "Измените тексты, которые бот шлёт пользователям. "
        "Можно вставлять <b>кастом-эмодзи</b> и форматирование "
        "(жирный, курсив) — бот сохранит как есть.\n\n"
        "✏️ = изменён, ○ = стандартный",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:txt:"))
async def text_view(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    key = callback.data.split(":")[-1]
    meta = CUSTOM_TEXTS.get(key)
    if not meta:
        await callback.answer()
        return
    cur = get_custom_text(key)
    vars_line = ""
    if meta.get("vars"):
        vars_line = ("\n\n🔤 Доступные подстановки: "
                     + ", ".join(f"<code>{v}</code>" for v in meta["vars"])
                     + "\n<i>Вставьте их в текст — бот подставит значения.</i>")
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить", callback_data=f"admin:txtedit:{key}")
    if is_custom_text_set(key):
        b.button(text="♻️ Вернуть стандартный", callback_data=f"admin:txtreset:{key}")
    b.button(text="⬅️ К текстам", callback_data="admin:texts")
    if is_custom_text_set(key):
        b.adjust(2, 1)  # 2 actions on one row + nav on its own row
    else:
        b.adjust(1)
    # показываем живой предпросмотр: подстановки заполнены примерами
    sample = {"price": " 💰 500 ₽", "days": "30", "left": "30"}
    preview = get_custom_text(key)
    for v in meta.get("vars", []):
        vk = v.strip("{}")
        preview = preview.replace(v, str(sample.get(vk, v)))
    await callback.message.edit_text(
        f"📝 <b>{meta['title']}</b>\n\n"
        f"<b>Текущий текст:</b>\n{preview}{vars_line}",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:txtedit:"))
async def text_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[-1]
    meta = CUSTOM_TEXTS.get(key)
    if not meta:
        await callback.answer()
        return
    await state.set_state(AdminState.edit_text)
    await state.update_data(text_key=key)
    vars_line = ""
    if meta.get("vars"):
        vars_line = ("\n🔤 Подстановки: "
                     + ", ".join(f"<code>{v}</code>" for v in meta["vars"]))
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"admin:txt:{key}")
    await callback.message.edit_text(
        f"✏️ <b>{meta['title']}</b>\n\n"
        "Отправьте новый текст сообщением. Можно использовать "
        "<b>кастом-эмодзи</b>, жирный, курсив и переносы строк.{}".format(vars_line),
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdminState.edit_text)
async def text_edit_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("text_key")
    await state.clear()
    if not key or key not in CUSTOM_TEXTS:
        await message.answer("❌ Потерян ключ текста. Начните заново.")
        return
    # html_text сохраняет и разметку, и кастомные эмодзи как <tg-emoji>
    html = message.html_text if message.text else ""
    if not html.strip():
        await message.answer("❌ Пустой текст. Пришлите текст сообщением.")
        return
    set_custom_text(key, html)
    b = InlineKeyboardBuilder()
    b.button(text="👁 Посмотреть", callback_data=f"admin:txt:{key}")
    b.button(text="📝 К текстам", callback_data="admin:texts")
    ui.lay(b)
    await message.answer("✅ Текст сохранён!", reply_markup=b.as_markup())
    # показываем, как это будет выглядеть на самом деле
    try:
        await message.answer("<b>Так это увидят пользователи:</b>")
        await message.answer(html)
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:txtreset:"))
async def text_reset(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[-1]
    clear_custom_text(key)
    await callback.answer("Текст сброшен к стандартному", show_alert=True)
    await text_view(callback, state)


def _esc(value) -> str:
    """Ссылка приходит от человека и уходит в HTML-сообщение: одиночный «<»
    в ней уронил бы отправку целиком."""
    return ui.esc(value)


# ── Правовые документы ──────────────────────────────────────────────────────
#
# Соглашение, оферта и политика — общие на весь бот: они про продукт, а не
# про магазин конкретного продавца. Экран показывает, что задано, а что нет,
# потому что незаполненная ссылка означает отсутствующую кнопку у клиента —
# и об этом лучше узнать здесь, а не от него.

@router.callback_query(F.data == "admin:docs")
async def docs_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    from storage import get_policy_links
    links = get_policy_links()

    body = []
    b = InlineKeyboardBuilder()
    for key, title in POLICY_DOCS:
        url = links.get(key)
        body.append(f"{'✅' if url else '○'} {title}")
        body.append(f"   <code>{_esc(url)}</code>" if url
                    else "   <i>ссылка не задана — кнопки у клиента нет</i>")
        b.button(text=("✏️ " if url else "➕ ") + title.split(" ", 1)[-1],
                 callback_data=f"admin:docset:{key}")
    b.button(text="👁 Как это видит клиент", callback_data="menu:policy")
    b.button(text="⬅️ Назад", callback_data="admin:menu")

    await callback.message.edit_text(ui.screen(
        "📄 <b>Правовые документы</b>", body,
        footer="<i>Кнопка появляется у клиента только там, где задана ссылка: "
               "кнопка без адреса не «ведёт никуда», а роняет отправку экрана "
               "целиком — такую клавиатуру Telegram не принимает.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:docset:"))
async def docs_set_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[-1]
    title = dict(POLICY_DOCS).get(key)
    if not title:
        await callback.answer()
        return
    await state.set_state(AdminState.doc_url)
    await state.update_data(doc_key=key)
    from storage import get_policy_links
    cur = get_policy_links().get(key, "")

    b = InlineKeyboardBuilder()
    if cur:
        b.button(text="🗑 Убрать ссылку", callback_data=f"admin:docdel:{key}")
    b.button(text="⬅️ К документам", callback_data="admin:docs")
    await callback.message.edit_text(ui.screen(
        f"📄 <b>{title}</b>",
        [f"Сейчас: <code>{_esc(cur)}</code>" if cur else "Сейчас: не задана",
         "",
         "Пришлите ссылку на документ одним сообщением."],
        footer="<i>Адрес должен начинаться с http:// или https:// — другие "
               "Telegram в кнопке не принимает, и экран не отправится.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.message(AdminState.doc_url)
async def docs_set_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    key = str((await state.get_data()).get("doc_key") or "")
    title = dict(POLICY_DOCS).get(key)
    if not title:
        await state.clear()
        return

    url = (message.text or "").strip()
    # Проверка не придирка: кнопку с другим адресом Telegram отвергает, и
    # вместе с ней не уходит весь экран. Отказать здесь — значит объяснить;
    # пропустить — значит сломать экран у клиента и не узнать об этом.
    if not url.lower().startswith(("http://", "https://")):
        await message.answer(
            "❌ Ссылка должна начинаться с <code>http://</code> или "
            "<code>https://</code>.\n\nПришлите ещё раз:")
        return

    from storage import set_policy_link
    set_policy_link(key, url)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="👁 Как это видит клиент", callback_data="menu:policy")
    b.button(text="⬅️ К документам", callback_data="admin:docs")
    await message.answer(ui.screen(
        "✅ <b>Ссылка сохранена</b>",
        [f"{title}", f"<code>{_esc(url)}</code>"]),
        reply_markup=ui.lay(b).as_markup())


@router.callback_query(F.data.startswith("admin:docdel:"))
async def docs_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[-1]
    if key not in dict(POLICY_DOCS):
        await callback.answer()
        return
    from storage import clear_policy_link
    clear_policy_link(key)
    await state.clear()
    await callback.answer("Ссылка убрана — кнопки у клиента больше нет",
                          show_alert=True)
    await docs_menu(callback, state)


# ── Тарифы и пробный период ─────────────────────────────────────────────────
#
# Цена была одним числом, а документы обещают градацию по срокам со скидкой
# за длинный срок. Цена, которой клиент нигде не видит, обещанием не
# является — поэтому тарифы задаются здесь и показываются на экране
# «нужна подписка».

def _prices() -> dict:
    from storage import get_prices
    return get_prices()


@router.callback_query(F.data == "admin:prices")
async def prices_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    from storage import get_trial_days, price_lines

    prices = _prices()
    body = []
    b = InlineKeyboardBuilder()
    for days, label in PRICE_TIERS:
        price = prices.get(days)
        body.append(f"{'✅' if price else '○'} {label} — "
                    + (f"<b>{price} ₽</b>" if price else
                       "<i>не задан, клиенту не показывается</i>"))
        b.button(text=("✏️ " if price else "➕ ") + label,
                 callback_data=f"admin:tier:{days}")

    trial = get_trial_days()
    body += ["", f"🎁 Пробный период: "
             + (f"<b>{trial} дн.</b>" if trial else "<b>выключен</b>")]
    b.button(text="🎁 Пробный период", callback_data="admin:trial")
    b.button(text="⬅️ Назад", callback_data="admin:menu")

    shown = price_lines()
    footer = ("<i>Клиент увидит:</i>\n" + "\n".join(shown)) if shown else (
        "<i>Ни одного тарифа не задано — клиенту цена не показывается вовсе.</i>")
    await callback.message.edit_text(
        ui.screen("💰 <b>Тарифы</b>", body, footer=footer),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:tier:"))
async def tier_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    label = dict(PRICE_TIERS).get(days)
    if not label:
        await callback.answer()
        return
    await state.set_state(AdminState.tier_price)
    await state.update_data(tier_days=days)
    cur = _prices().get(days)

    b = InlineKeyboardBuilder()
    if cur:
        b.button(text="🗑 Убрать тариф", callback_data=f"admin:tierdel:{days}")
    b.button(text="⬅️ К тарифам", callback_data="admin:prices")
    await callback.message.edit_text(ui.screen(
        f"💰 <b>{label}</b>",
        [f"Сейчас: <b>{cur} ₽</b>" if cur else "Сейчас: тариф не задан", "",
         "Введите цену в рублях числом."],
        footer="<i>Ноль убирает тариф: «0 ₽» на экране читается как "
               "«бесплатно», а это обещание, за которое спросят.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.message(AdminState.tier_price)
async def tier_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    days = int((await state.get_data()).get("tier_days") or 0)
    if days not in dict(PRICE_TIERS):
        await state.clear()
        return
    try:
        price = int((message.text or "").strip().replace(" ", ""))
    except ValueError:
        await message.answer("❌ Нужно число в рублях. Введите ещё раз:")
        return
    if price < 0:
        await message.answer("❌ Цена не может быть отрицательной. Ещё раз:")
        return

    from storage import set_price
    set_price(days, price)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К тарифам", callback_data="admin:prices")
    label = dict(PRICE_TIERS)[days]
    await message.answer(ui.screen(
        "✅ <b>Тариф сохранён</b>",
        [f"{label} — " + (f"<b>{price} ₽</b>" if price else
                          "<i>убран, клиенту не показывается</i>")]),
        reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("admin:tierdel:"))
async def tier_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    if days in dict(PRICE_TIERS):
        from storage import set_price
        set_price(days, 0)
    await state.clear()
    await callback.answer("Тариф убран — клиенту он больше не показывается",
                          show_alert=True)
    await prices_menu(callback, state)


@router.callback_query(F.data == "admin:trial")
async def trial_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.trial_days)
    from storage import get_trial_days
    cur = get_trial_days()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К тарифам", callback_data="admin:prices")
    await callback.message.edit_text(ui.screen(
        "🎁 <b>Пробный период</b>",
        [f"Сейчас: <b>{cur} дн.</b>" if cur else "Сейчас: <b>выключен</b>", "",
         "Введите число дней. Ноль — выключить."],
        footer="<i>Выдаётся один раз на человека при первом /start. Отметка "
               "о выданной пробе переживает удаление данных: иначе "
               "/forget_me становится способом брать её бесконечно. Это "
               "оговорено в политике конфиденциальности.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.message(AdminState.trial_days)
async def trial_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Нужно число дней. Введите ещё раз:")
        return
    if days < 0:
        await message.answer("❌ Отрицательного срока не бывает. Ещё раз:")
        return

    from storage import set_trial_days
    set_trial_days(days)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К тарифам", callback_data="admin:prices")
    await message.answer(ui.screen(
        "✅ <b>Сохранено</b>",
        [f"Пробный период: <b>{days} дн.</b>" if days else
         "Пробный период <b>выключен</b> — бот о нём не упоминает."]),
        reply_markup=b.as_markup())
