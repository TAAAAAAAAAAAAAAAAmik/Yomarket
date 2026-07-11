"""Product management through the seller panel (Nova API): list, edit,
hide/show, clone, delete. Works even when the Integration API can't."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import get_panel_creds

router = Router()
logger = logging.getLogger(__name__)

_TIMEOUT = 40


class PanelItemState(StatesGroup):
    waiting_price = State()
    waiting_title = State()


def _no_session_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Войти в панель", callback_data="panel:sms_start")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(1)
    return b.as_markup()


async def _run(uid: int, fn, *args):
    """Run a blocking panel function in a thread with a hard deadline."""
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return None, "no_session"
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, fn, creds["cookies"], *args),
            timeout=_TIMEOUT,
        ), ""
    except asyncio.TimeoutError:
        return None, f"⏱ Панель не ответила за {_TIMEOUT} секунд."
    except Exception as e:
        return None, f"Ошибка: {str(e)[:150]}"


@router.callback_query(F.data == "pitems:list")
async def list_items(callback: CallbackQuery) -> None:
    from automation.panel import panel_list_items_sync

    await callback.message.edit_text("⏳ Загружаю товары из панели...")
    result, err = await _run(callback.from_user.id, panel_list_items_sync)
    if err == "no_session":
        await callback.message.edit_text(
            "❌ Нет сессии панели. Войдите через email.",
            reply_markup=_no_session_kb(),
        )
        await callback.answer()
        return
    if result is None:
        await callback.message.edit_text(f"❌ {err}", reply_markup=_no_session_kb())
        await callback.answer()
        return

    ok, items = result
    if not ok:
        await callback.message.edit_text(
            f"❌ Не удалось получить товары:\n{items}",
            reply_markup=_no_session_kb(),
        )
        await callback.answer()
        return

    hidden = [it for it in items if it.get("public") is False]
    b = InlineKeyboardBuilder()
    for it in items[:40]:
        pub = it.get("public")
        mark = "🌍 " if pub is True else ("🙈 " if pub is False else "")
        label = f"{mark}{(it['title'] or ('Товар ' + it['id']))[:26]}"
        if it.get("price"):
            label += f" — {it['price']} ₽"
        b.button(text=label, callback_data=f"pitem:{it['id']}")
    b.adjust(1)
    if hidden:
        b.button(text=f"🌍 Опубликовать все скрытые ({len(hidden)})",
                 callback_data="pitems:pubhidden")
    b.button(text="➕ Добавить товар", callback_data="create_ad:start")
    b.button(text="🔄 Обновить", callback_data="pitems:list")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(1)
    status_hint = ""
    if any(it.get("public") is not None for it in items):
        status_hint = "\n🌍 = виден в маркете, 🙈 = скрыт"
    await callback.message.edit_text(
        f"🛠 <b>Товары в панели</b> (всего: {len(items)})"
        f"{status_hint}\n\n"
        "Нажмите на товар для управления:",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


def _item_kb(item_id: str):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Цена", callback_data=f"pitem_price:{item_id}")
    b.button(text="✏️ Название", callback_data=f"pitem_title:{item_id}")
    b.button(text="🌍 Показать", callback_data=f"pitem_show:{item_id}")
    b.button(text="🙈 Скрыть", callback_data=f"pitem_hide:{item_id}")
    b.button(text="📋 Клонировать", callback_data=f"pitem_clone:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"pitem_del:{item_id}")
    b.button(text="⬅️ К товарам", callback_data="pitems:list")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data.startswith("pitem:"))
async def item_detail(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # "Отмена" из ввода цены/названия ведёт сюда
    item_id = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        f"🛠 <b>Товар #{item_id}</b>\n\nВыберите действие:",
        reply_markup=_item_kb(item_id),
    )
    await callback.answer()


# ── Цена / Название ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pitem_price:"))
async def edit_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    item_id = callback.data.split(":", 1)[1]
    await state.set_state(PanelItemState.waiting_price)
    await state.update_data(item_id=item_id)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"pitem:{item_id}")
    await callback.message.edit_text(
        f"✏️ Товар #{item_id} — введите новую цену (₽):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PanelItemState.waiting_price)
async def edit_price_save(message: Message, state: FSMContext) -> None:
    from automation.panel import panel_update_item_sync

    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        price = int(float(raw))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите цену числом, например: <b>500</b>")
        return
    data = await state.get_data()
    item_id = data.get("item_id", "")
    await state.clear()
    uid = message.from_user.id

    status = await message.answer("⏳ Меняю цену...")
    result, err = await _run(uid, panel_update_item_sync, item_id,
                             {"price": price}, uid)
    if result and result[0]:
        await status.edit_text(
            f"✅ Цена товара #{item_id} обновлена: <b>{price} ₽</b>",
            reply_markup=_item_kb(item_id),
        )
    else:
        detail = result[1] if result else err
        await status.edit_text(
            f"❌ Не удалось изменить цену:\n{detail}",
            reply_markup=_item_kb(item_id),
        )


@router.callback_query(F.data.startswith("pitem_title:"))
async def edit_title_start(callback: CallbackQuery, state: FSMContext) -> None:
    item_id = callback.data.split(":", 1)[1]
    await state.set_state(PanelItemState.waiting_title)
    await state.update_data(item_id=item_id)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"pitem:{item_id}")
    await callback.message.edit_text(
        f"✏️ Товар #{item_id} — введите новое название (макс. 32 символа):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PanelItemState.waiting_title)
async def edit_title_save(message: Message, state: FSMContext) -> None:
    from automation.panel import panel_update_item_sync

    title = (message.text or "").strip()[:32]
    if not title:
        await message.answer("❌ Название не может быть пустым:")
        return
    data = await state.get_data()
    item_id = data.get("item_id", "")
    await state.clear()
    uid = message.from_user.id

    status = await message.answer("⏳ Меняю название...")
    result, err = await _run(uid, panel_update_item_sync, item_id,
                             {"title": title}, uid)
    if result and result[0]:
        await status.edit_text(
            f"✅ Название товара #{item_id} обновлено:\n<b>{title}</b>",
            reply_markup=_item_kb(item_id),
        )
    else:
        detail = result[1] if result else err
        await status.edit_text(
            f"❌ Не удалось изменить название:\n{detail}",
            reply_markup=_item_kb(item_id),
        )


# ── Показать / Скрыть ───────────────────────────────────────────────────────

async def _toggle(callback: CallbackQuery, public: bool) -> None:
    from automation.panel import panel_publish_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    await callback.answer("⏳ Выполняю...")
    result, err = await _run(uid, panel_publish_item_sync, item_id, uid, public)
    verb = "показан" if public else "скрыт"
    if result and result[0]:
        text = f"✅ Товар #{item_id} {verb} ({result[1]})"
    else:
        detail = result[1] if result else err
        text = f"❌ Товар #{item_id}: не удалось.\n\n{detail}"
    await callback.message.edit_text(text, reply_markup=_item_kb(item_id))


@router.callback_query(F.data.startswith("pitem_show:"))
async def item_show(callback: CallbackQuery) -> None:
    await _toggle(callback, public=True)


@router.callback_query(F.data.startswith("pitem_hide:"))
async def item_hide(callback: CallbackQuery) -> None:
    await _toggle(callback, public=False)


@router.callback_query(F.data == "pitems:pubhidden")
async def publish_all_hidden(callback: CallbackQuery) -> None:
    """Publish every currently-hidden item in one tap."""
    from automation.panel import panel_list_items_sync, panel_publish_item_sync
    uid = callback.from_user.id
    await callback.answer("⏳")
    result, err = await _run(uid, panel_list_items_sync)
    if not (result and result[0]):
        await callback.message.edit_text(
            f"❌ {result[1] if result else err}", reply_markup=_no_session_kb())
        return
    hidden = [it for it in result[1] if it.get("public") is False]
    if not hidden:
        await callback.answer("Скрытых товаров нет", show_alert=True)
        await list_items(callback)
        return
    await callback.message.edit_text(
        f"⏳ Публикую {len(hidden)} скрытых товаров…")
    ok = fail = 0
    last_err = ""
    for it in hidden:
        r, e = await _run(uid, panel_publish_item_sync, it["id"], uid, True)
        if r and r[0]:
            ok += 1
        else:
            fail += 1
            last_err = (r[1] if r else e) or ""
    b = InlineKeyboardBuilder()
    b.button(text="🔄 К товарам", callback_data="pitems:list")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(1)
    text = f"🌍 <b>Публикация завершена</b>\n\n✅ Опубликовано: <b>{ok}</b>"
    if fail:
        text += f"\n❌ Не удалось: <b>{fail}</b>"
        if last_err:
            text += f"\n<i>{last_err[:120]}</i>"
    await callback.message.edit_text(text, reply_markup=b.as_markup())


# ── Клонировать ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pitem_clone:"))
async def item_clone(callback: CallbackQuery) -> None:
    from automation.panel import panel_clone_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    await callback.answer("⏳ Клонирую...")
    await callback.message.edit_text(f"⏳ Клонирую товар #{item_id}...")
    result, err = await _run(uid, panel_clone_item_sync, item_id, uid)
    if result and result[0]:
        new_id = result[1]
        b = InlineKeyboardBuilder()
        if str(new_id).isdigit():
            b.button(text="🛠 К новому товару", callback_data=f"pitem:{new_id}")
        b.button(text="⬅️ К товарам", callback_data="pitems:list")
        b.adjust(1)
        await callback.message.edit_text(
            f"✅ Товар #{item_id} клонирован → <b>#{new_id}</b>",
            reply_markup=b.as_markup(),
        )
    else:
        detail = result[1] if result else err
        await callback.message.edit_text(
            f"❌ Не удалось клонировать:\n{detail}",
            reply_markup=_item_kb(item_id),
        )


# ── Удаление (с подтверждением) ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("pitem_del:"))
async def item_delete_confirm(callback: CallbackQuery) -> None:
    item_id = callback.data.split(":", 1)[1]
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=f"pitem_del2:{item_id}")
    b.button(text="❌ Отмена", callback_data=f"pitem:{item_id}")
    b.adjust(1)
    await callback.message.edit_text(
        f"🗑 Удалить товар <b>#{item_id}</b> из панели?\n\n"
        "⚠️ Это действие необратимо.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pitem_del2:"))
async def item_delete_do(callback: CallbackQuery) -> None:
    from automation.panel import panel_delete_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    await callback.answer("⏳ Удаляю...")
    result, err = await _run(uid, panel_delete_item_sync, item_id, uid)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К товарам", callback_data="pitems:list")
    if result and result[0]:
        await callback.message.edit_text(
            f"✅ Товар #{item_id} удалён.", reply_markup=b.as_markup())
    else:
        detail = result[1] if result else err
        await callback.message.edit_text(
            f"❌ Не удалось удалить:\n{detail}", reply_markup=b.as_markup())
