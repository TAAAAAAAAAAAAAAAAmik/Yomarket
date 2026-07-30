"""Multi-account management: several YooMarket shops in one bot."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import (
    add_account,
    get_accounts,
    get_active_account,
    get_shop_name,
    remove_account,
    save_shop_name,
    set_active_account,
)

router = Router()
logger = logging.getLogger(__name__)


class AccountState(StatesGroup):
    waiting_name = State()
    waiting_token = State()


def _names(user_id: int) -> list[str]:
    return sorted(get_accounts(user_id).keys())


async def _render_menu(msg, user_id: int, edit: bool = True) -> None:
    accounts = _names(user_id)
    active = get_active_account(user_id)
    b = InlineKeyboardBuilder()
    lines = ["👥 <b>Аккаунты YooMarket</b>\n"]
    if not accounts:
        lines.append("Нет добавленных аккаунтов. Отправьте /start и введите токен.")
    else:
        lines.append("Нажмите на аккаунт, чтобы переключиться:")
        for i, name in enumerate(accounts):
            mark = " ✅" if name == active else ""
            b.button(text=f"🏪 {name}{mark}", callback_data=f"acc:switch:{i}")
    b.button(text="➕ Добавить аккаунт", callback_data="acc:add")
    if len(accounts) > 1:
        b.button(text="🗑 Удалить текущий", callback_data="acc:del")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    b.adjust(1)
    text = "\n".join(lines)
    if edit:
        await msg.edit_text(text, reply_markup=b.as_markup())
    else:
        await msg.answer(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "acc:menu")
async def accounts_menu(callback: CallbackQuery) -> None:
    await _render_menu(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("acc:switch:"))
async def switch_account(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
        name = _names(uid)[idx]
    except (ValueError, IndexError):
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    if name == get_active_account(uid):
        await callback.answer("Уже активен")
        return
    set_active_account(uid, name)
    shop = get_shop_name(uid) or name
    await callback.answer(f"✅ Переключено на «{shop}»", show_alert=True)
    await _render_menu(callback.message, uid)


@router.callback_query(F.data == "acc:add")
async def add_account_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AccountState.waiting_name)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="acc:menu")
    await callback.message.edit_text(
        "➕ <b>Новый аккаунт</b>\n\n"
        "Введите название (например: «Магазин 2»):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AccountState.waiting_name)
async def add_account_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()[:30]
    if not name:
        await message.answer("❌ Название не может быть пустым:")
        return
    if name in get_accounts(message.from_user.id):
        await message.answer("❌ Аккаунт с таким названием уже есть. Введите другое:")
        return
    await state.update_data(acc_name=name)
    await state.set_state(AccountState.waiting_token)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="acc:menu")
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "Теперь отправьте <b>API токен</b> этого магазина:\n"
        "<i>Мой магазин → Интеграции → API токен</i>",
        reply_markup=b.as_markup(),
    )


@router.message(AccountState.waiting_token)
async def add_account_token(message: Message, state: FSMContext, **data) -> None:
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым:")
        return
    try:  # токен — секрет, убираем из истории чата
        await message.delete()
    except Exception:
        pass

    status = await message.answer("⏳ Проверяю токен...")
    api = YooMarketAPI(token)
    await api.start()
    try:
        info = await api.check()
    except Exception as e:
        await status.edit_text(
            f"❌ Токен не подошёл: <code>{str(e)[:150]}</code>\n\nОтправьте другой токен:"
        )
        return
    finally:
        await api.close()

    st_data = await state.get_data()
    name = st_data.get("acc_name", "Магазин")
    await state.clear()

    uid = message.from_user.id
    add_account(uid, name, token, make_active=True)

    shop = info.get("shop") or info.get("data") or info
    shop_name = (shop.get("name") or shop.get("shop_name") or name) if isinstance(shop, dict) else name
    save_shop_name(uid, str(shop_name))

    task_manager = data.get("task_manager")
    if task_manager:
        task_manager.start_for_user(uid)

    await status.edit_text(f"✅ Аккаунт <b>{name}</b> добавлен и активирован!\n🏪 {shop_name}")
    await _render_menu(message, uid, edit=False)


@router.callback_query(F.data == "acc:del")
async def delete_account_confirm(callback: CallbackQuery) -> None:
    active = get_active_account(callback.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data="acc:del2")
    b.button(text="❌ Отмена", callback_data="acc:menu")
    b.adjust(1)
    await callback.message.edit_text(
        f"🗑 Удалить аккаунт <b>{active}</b>?\n\n"
        "Токен и настройки этого аккаунта будут забыты ботом "
        "(сам магазин YooMarket не пострадает).",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "acc:del2")
async def delete_account_do(callback: CallbackQuery, **data) -> None:
    uid = callback.from_user.id
    active = get_active_account(uid)
    if remove_account(uid, active):
        # Stop this shop's background loops, keep the others running.
        task_manager = data.get("task_manager")
        if task_manager:
            task_manager.stop_for_account(uid, active)
            task_manager.start_for_user(uid)  # reconcile (new active account)
        await callback.answer(f"🗑 «{active}» удалён", show_alert=True)
    else:
        await callback.answer("Не удалось удалить", show_alert=True)
    await _render_menu(callback.message, uid)
