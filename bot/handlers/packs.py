"""Ad packs: group listings into named packs and bump the whole pack at once."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class PackState(StatesGroup):
    new_name = State()


def _packs(uid: int) -> dict:
    return get_settings(uid).get("ad_packs", {})


def _pack_names(uid: int) -> list[str]:
    return list(_packs(uid).keys())


@router.callback_query(F.data == "packs:menu")
async def packs_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render_menu(callback.message, callback.from_user.id)
    await callback.answer()


async def _render_menu(msg, uid: int) -> None:
    packs = _packs(uid)
    lines = ["📦 <b>Паки объявлений</b>\n"]
    if packs:
        lines.append("Соберите объявления в пак и поднимайте разом:")
        for name, ids in packs.items():
            lines.append(f"• <b>{name}</b> — {len(ids)} шт.")
    else:
        lines.append("Паков пока нет. Создайте первый.")
    b = InlineKeyboardBuilder()
    for i, name in enumerate(_pack_names(uid)):
        b.button(text=f"📦 {name[:24]}", callback_data=f"pack:view:{i}")
    b.adjust(1)
    b.button(text="➕ Новый пак", callback_data="pack:new")
    b.button(text="⬅️ Объявления", callback_data="menu:ads")
    b.adjust(1)
    await msg.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "pack:new")
async def pack_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PackState.new_name)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="packs:menu")
    await callback.message.edit_text(
        "➕ <b>Новый пак</b>\n\nВведите название пака:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PackState.new_name)
async def pack_new_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()[:30]
    await state.clear()
    if not name:
        await message.answer("❌ Название пустое.")
        return
    uid = message.from_user.id
    s = get_settings(uid)
    packs = s.setdefault("ad_packs", {})
    if name in packs:
        await message.answer("❌ Пак с таким именем уже есть.")
        return
    packs[name] = []
    save_settings(uid, s)
    b = InlineKeyboardBuilder()
    b.button(text="📦 К паку", callback_data=f"pack:view:{len(packs)-1}")
    b.button(text="⬅️ Паки", callback_data="packs:menu")
    b.adjust(2)
    await message.answer(f"✅ Пак «{name}» создан. Добавьте в него объявления.",
                         reply_markup=b.as_markup())


def _pack_by_index(uid: int, idx: int):
    names = _pack_names(uid)
    if 0 <= idx < len(names):
        return names[idx]
    return None


@router.callback_query(F.data.startswith("pack:view:"))
async def pack_view(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    ids = _packs(uid).get(name, [])
    lines = [f"📦 <b>{name}</b>\n", f"Объявлений: <b>{len(ids)}</b>"]
    if ids:
        lines.append("\n" + "\n".join(f"• <code>{i}</code>" for i in ids[:30]))
    b = InlineKeyboardBuilder()
    b.button(text="⬆️ Поднять весь пак", callback_data=f"pack:bump:{idx}")
    b.button(text="➕ Добавить объявления", callback_data=f"pack:add:{idx}")
    if ids:
        b.button(text="🗑 Очистить", callback_data=f"pack:clear:{idx}")
    b.button(text="❌ Удалить пак", callback_data=f"pack:del:{idx}")
    b.button(text="⬅️ Паки", callback_data="packs:menu")
    b.adjust(2, 1, 1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("pack:add:"))
async def pack_add_list(callback: CallbackQuery, api: YooMarketAPI) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    if not api:
        await callback.answer("Нужен API-токен для списка товаров", show_alert=True)
        return
    await callback.answer("⏳ Загружаю товары…")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки: {e}")
        return
    in_pack = set(str(i) for i in _packs(uid).get(name, []))
    b = InlineKeyboardBuilder()
    for ad in ads[:40]:
        ad_id = str(ad.get("id", ""))
        if not ad_id:
            continue
        title = (ad.get("title") or ad.get("name") or ad_id)[:24]
        mark = "✅ " if ad_id in in_pack else "➕ "
        b.button(text=f"{mark}{title}", callback_data=f"pack:tog:{idx}:{ad_id}")
    b.adjust(1)
    b.button(text="✅ Готово", callback_data=f"pack:view:{idx}")
    b.adjust(1)
    await callback.message.edit_text(
        f"📦 <b>{name}</b> — отметьте объявления для пака:",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("pack:tog:"))
async def pack_toggle(callback: CallbackQuery, api: YooMarketAPI) -> None:
    uid = callback.from_user.id
    parts = callback.data.split(":")
    try:
        idx = int(parts[2]); ad_id = parts[3]
    except (ValueError, IndexError):
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    s = get_settings(uid)
    ids = [str(i) for i in s["ad_packs"].get(name, [])]
    if ad_id in ids:
        ids.remove(ad_id)
    else:
        ids.append(ad_id)
    s["ad_packs"][name] = ids
    save_settings(uid, s)
    await callback.answer("Обновлено")
    # re-render the add list
    await pack_add_list(callback, api)


@router.callback_query(F.data.startswith("pack:clear:"))
async def pack_clear(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    s = get_settings(uid)
    s["ad_packs"][name] = []
    save_settings(uid, s)
    await callback.answer("Пак очищен")
    await pack_view(callback)


@router.callback_query(F.data.startswith("pack:del:"))
async def pack_del(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    s = get_settings(uid)
    s["ad_packs"].pop(name, None)
    save_settings(uid, s)
    await callback.answer(f"Пак «{name}» удалён", show_alert=True)
    await _render_menu(callback.message, uid)


@router.callback_query(F.data.startswith("pack:bump:"))
async def pack_bump(callback: CallbackQuery, api: YooMarketAPI) -> None:
    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    if name is None:
        await callback.answer("Пак не найден", show_alert=True)
        return
    ids = _packs(uid).get(name, [])
    if not ids:
        await callback.answer("Пак пуст", show_alert=True)
        return
    if not api:
        await callback.answer("Нужен API-токен", show_alert=True)
        return
    await callback.message.edit_text(f"⏳ Поднимаю пак «{name}» ({len(ids)} шт.)…")
    ok = fail = 0
    last_err = ""
    for ad_id in ids:
        try:
            await api.bump_ad(ad_id)
            ok += 1
        except Exception as e:
            fail += 1
            last_err = str(e)
    b = InlineKeyboardBuilder()
    b.button(text="📦 К паку", callback_data=f"pack:view:{idx}")
    b.button(text="⬅️ Паки", callback_data="packs:menu")
    b.adjust(2)
    text = f"⬆️ <b>Пак «{name}» поднят</b>\n\n✅ Поднято: <b>{ok}</b>"
    if fail:
        text += f"\n❌ Ошибок: <b>{fail}</b>"
        if last_err:
            text += f"\n<i>{last_err[:80]}</i>"
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()
