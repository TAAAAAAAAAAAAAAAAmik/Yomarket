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


def _esc(value) -> str:
    """Название пака придумывает продавец — '<' в нём сломал бы всё сообщение."""
    import html
    return html.escape(str(value or ""))

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
    from handlers.selenium_settings import promo_pack
    ids = _packs(uid).get(name, [])
    s = get_settings(uid)
    scheduled = promo_pack(s) == name
    bs = s.get("bump_schedule", {})
    times = bs.get("times") or []

    lines = [f"📦 <b>{name}</b>\n", f"Объявлений: <b>{len(ids)}</b>"]
    if scheduled:
        # Состояние расписания видно на самом паке: иначе «поднимается ли он
        # сам» приходится выяснять на другом экране, а это про деньги.
        lines.append(
            "⏰ Поднимается по расписанию: "
            + (f"<b>{', '.join(times)}</b>" if bs.get("enabled") and times
               else "<b>расписание выключено</b>"))
    if ids:
        lines.append("\n" + "\n".join(f"• <code>{i}</code>" for i in ids[:30]))
    b = InlineKeyboardBuilder()
    b.button(text="⬆️ Поднять весь пак", callback_data=f"pack:bump:{idx}")
    b.button(text="➕ Добавить объявления", callback_data=f"pack:add:{idx}")
    b.button(text=("⏰ Расписание пака" if scheduled
                   else "⏰ Поднимать по времени"),
             callback_data=f"pack:sched:{idx}")
    if ids:
        b.button(text="🗑 Очистить", callback_data=f"pack:clear:{idx}")
    b.button(text="❌ Удалить пак", callback_data=f"pack:del:{idx}")
    b.button(text="⬅️ Паки", callback_data="packs:menu")
    b.adjust(2, 1, 1, 1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("pack:sched:"))
async def pack_schedule(callback: CallbackQuery) -> None:
    """Привязать расписание продвижения к этому паку.

    Расписание в боте **одно на магазин**, а не по одному на пак. Строить
    второе означало бы завести вторую дорогу к тратам: свой потолок, свой
    счётчик, своя история — и два места, где можно ошибиться деньгами.
    Поэтому пак не заводит собственный будильник, а становится тем набором,
    который поднимает общее расписание.

    Отсюда обязанность сказать вслух, что предыдущая привязка заменяется:
    молча переключить расписание с одного пака на другой — значит начать
    платить не за те товары.
    """
    from handlers.selenium_settings import promo_pack

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
        await callback.answer("Пак пуст — сначала добавьте объявления",
                              show_alert=True)
        return

    s = get_settings(uid)
    was = promo_pack(s)
    ab = s.setdefault("auto_bump", {})
    ab["only_pack"] = name
    # Прежний ручной выбор товаров больше не действует — он бы противоречил
    # паку, и было бы непонятно, что из двух победит.
    ab.pop("only_items", None)
    save_settings(uid, s)

    bs = s.get("bump_schedule", {})
    times = bs.get("times") or []
    head = [f"⏰ <b>Расписание для пака «{_esc(name)}»</b>", ""]
    if was and was != name:
        head.append(f"⚠️ Раньше по расписанию поднимался пак «{_esc(was)}» — "
                    f"теперь поднимается этот.")
        head.append("")
    head.append(f"Товаров в паке: <b>{len(ids)}</b>")
    head.append("Состав берётся из пака на каждом запуске — добавите товар, "
                "и он начнёт подниматься сам.")
    head.append("")
    head.append(
        f"Часы запуска: <b>{', '.join(times) if times else 'не заданы'}</b>"
        if bs.get("enabled") or times else
        "Часы запуска пока не заданы, и расписание выключено.")
    head.append("")
    head.append("Расписание в боте одно. Задать часы, потолок трат в день и "
                "включить его — на следующем экране.")

    b = InlineKeyboardBuilder()
    b.button(text="🕐 Часы и потолок трат", callback_data="sched:menu")
    b.button(text="📦 К паку", callback_data=f"pack:view:{idx}")
    b.adjust(1)
    await callback.message.edit_text("\n".join(head), reply_markup=b.as_markup())
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
async def pack_bump_ask(callback: CallbackQuery) -> None:
    """Спросить, прежде чем тратить: «Премиум» платный, и за каждый товар.

    Раньше эта кнопка дёргала поднятие через API, которого у Юмаркета нет.
    Каждое объявление падало, а экран рисовал «✅ Пак поднят · Поднято: 0» —
    заголовок утверждал успех, которого не было.
    """
    from handlers.selenium_settings import promo_params, promo_price
    from storage import get_panel_creds

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

    if not get_panel_creds(uid):
        await callback.answer(
            "Продвижение идёт через панель — сначала войдите в неё: "
            "Настройки → 🌐 Панель продавца", show_alert=True)
        return

    s = get_settings(uid)
    if not promo_params(s):
        b = InlineKeyboardBuilder()
        b.button(text="⚙️ Выбрать тариф", callback_data="promo:setup")
        b.button(text="⬅️ К паку", callback_data=f"pack:view:{idx}")
        b.adjust(1)
        await callback.message.edit_text(
            "⚙️ <b>Сначала выберите тариф</b>\n\n"
            "«Премиум» требует услугу, срок и способ оплаты — сроки стоят "
            "по-разному, поэтому бот не подставляет их сам.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    price = promo_price(s)
    total = f" — ориентировочно <b>{price * len(ids)} ₽</b>" if price else ""
    b = InlineKeyboardBuilder()
    b.button(text=f"💵 Поднять {len(ids)} шт.", callback_data=f"pack:bumpok:{idx}")
    b.button(text="❌ Отмена", callback_data=f"pack:view:{idx}")
    b.adjust(1)
    await callback.message.edit_text(
        f"⭐ <b>Премиум для пака «{_esc(name)}»</b>\n\n"
        f"Товаров: <b>{len(ids)}</b>{total}\n\n"
        "Это платная услуга Юмаркета, и оплачивается она за каждый товар "
        "отдельно.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("pack:bumpok:"))
async def pack_bump(callback: CallbackQuery) -> None:
    """Поднять пак — через панель, по одному товару, с честным итогом."""
    import asyncio

    from automation.panel import panel_bump_item_sync
    from handlers.selenium_settings import promo_params
    from storage import get_panel_creds

    uid = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    name = _pack_by_index(uid, idx)
    ids = _packs(uid).get(name or "", [])
    if not ids:
        await callback.answer("Пак пуст", show_alert=True)
        return
    creds = get_panel_creds(uid)
    params = promo_params(get_settings(uid))
    if not creds or not params:
        await callback.answer("Нужен вход в панель и выбранный тариф",
                              show_alert=True)
        return

    await callback.answer("⏳ Продвигаю…")
    await callback.message.edit_text(
        f"⏳ Поднимаю пак «{_esc(name)}» ({len(ids)} шт.)…")
    loop = asyncio.get_event_loop()
    done, failed = [], []
    for ad_id in ids:
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_bump_item_sync,
                                     creds["cookies"], str(ad_id), uid, True,
                                     params),
                timeout=60)
        except Exception as e:
            ok, msg = False, str(e)[:80]
        (done if ok else failed).append((ad_id, str(msg)[:80]))

    b = InlineKeyboardBuilder()
    b.button(text="📦 К паку", callback_data=f"pack:view:{idx}")
    b.button(text="⬅️ Паки", callback_data="packs:menu")
    b.adjust(2)
    # Заголовок по факту: «поднят» при нуле поднятых — это ложь, за которую
    # продавец платит вниманием, а иногда и деньгами.
    if done and not failed:
        head = f"⬆️ <b>Пак «{_esc(name)}» поднят</b>"
    elif done:
        head = f"⚠️ <b>Пак «{_esc(name)}» поднят частично</b>"
    else:
        head = f"❌ <b>Пак «{_esc(name)}» не поднят</b>"
    lines = [head, "", f"✅ Поднято: <b>{len(done)}</b> из {len(ids)}"]
    if failed:
        lines.append(f"❌ Не вышло: <b>{len(failed)}</b>")
        seen = []
        for _ad, why in failed:
            if why not in seen:
                seen.append(why)
        for why in seen[:3]:
            lines.append(f"<i>{_esc(why)}</i>")
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
