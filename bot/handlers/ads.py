from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import AdCallback, PaginationCallback, back_keyboard

router = Router()

STATUS_EMOJI = {
    "active": "🟢 Активен",
    "inactive": "🔴 Неактивен",
    "blocked": "⛔ Заблокирован",
    "sold": "✅ Продан",
}


class AdEditState(StatesGroup):
    waiting_new_price = State()
    waiting_bulk_percent = State()
    waiting_bulk_description = State()


def _status(raw: str) -> str:
    return STATUS_EMOJI.get(raw, f"⚪ {raw}")


def _fmt_list(ads: list[dict], total: int | None) -> str:
    if not ads:
        return "📦 Товаров не найдено."
    header = "📦 <b>Ваши товары</b>"
    if total:
        header += f" (всего: {total})"
    lines = [header, ""]
    for i, ad in enumerate(ads, 1):
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status = _status(ad.get("status", ""))
        lines.append(f"{i}. <b>{title}</b>\n   💰 {price} ₽  |  {status}")
    return "\n".join(lines)


def _ads_keyboard(ads: list[dict], next_cursor: str | None):
    b = InlineKeyboardBuilder()
    # 1 per row: the item list (long labels) + optional "more"
    for ad in ads:
        ad_id = str(ad.get("id", ""))
        title = ad.get("title") or ad.get("name") or f"Товар {ad_id}"
        price = ad.get("price", "")
        label = f"{title[:28]} — {price} ₽" if price else title[:35]
        b.button(text=label, callback_data=AdCallback(ad_id=ad_id).pack())
    n_list = len(ads)
    if next_cursor:
        b.button(text="Ещё товары ▶️", callback_data=PaginationCallback(entity="ads", cursor=next_cursor).pack())
        n_list += 1
    # 2 columns: fixed actions
    b.button(text="➕ Добавить товар", callback_data="create_ad:start")
    b.button(text="📦 Товары", callback_data="pitems:cats")
    b.button(text="🛠 Управление", callback_data="pitems:list")
    b.button(text="📦 Паки", callback_data="packs:menu")
    b.button(text="💰 Все цены", callback_data="ads:bulk_price")
    b.button(text="📝 Описание всех", callback_data="ads:bulk_desc")
    # nav on its own row
    b.button(text="🔄 Обновить", callback_data="ads_load")
    b.button(text="⬅️ Меню", callback_data="menu:main")
    b.adjust(*([1] * n_list), 2, 2, 2, 2)
    return b.as_markup()


@router.callback_query(F.data.in_({"menu:ads", "ads_load"}))
async def ads_menu(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю объявления...")
    try:
        data = await api.get_ads()
        ads: list[dict] = data.get("data") or data.get("items") or []
        meta = data.get("meta", {})
        next_cursor: str | None = meta.get("next_cursor")
        total: int | None = meta.get("total")
        text = _fmt_list(ads, total)
        keyboard = _ads_keyboard(ads, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка загрузки: {e}\n\n💡 Попробуйте управление через панель."
        b = InlineKeyboardBuilder()
        b.button(text="🛠 Управление (панель)", callback_data="pitems:list")
        b.button(text="➕ Добавить товар", callback_data="create_ad:start")
        b.button(text="⬅️ Главное меню", callback_data="menu:main")
        b.adjust(2, 1)
        keyboard = b.as_markup()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(PaginationCallback.filter(F.entity == "ads"))
async def paginate_ads(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        data = await api.get_ads(cursor=callback_data.cursor)
        ads: list[dict] = data.get("data") or data.get("items") or []
        meta = data.get("meta", {})
        next_cursor: str | None = meta.get("next_cursor")
        total: int | None = meta.get("total")
        text = _fmt_list(ads, total)
        keyboard = _ads_keyboard(ads, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AdCallback.filter())
async def show_ad_detail(
    callback: CallbackQuery,
    callback_data: AdCallback,
    api: YooMarketAPI,
    state: FSMContext,
) -> None:
    await state.clear()  # "Отмена" из ввода цены ведёт сюда
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        ad = await api.get_ad(callback_data.ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status_raw = ad.get("status", "")
        status = _status(status_raw)
        description = ad.get("description") or "Нет описания"
        views = ad.get("views_count", "—")
        category = ad.get("category") or "—"
        text = (
            f"📦 <b>{title}</b>\n\n"
            f"💰 Цена: <b>{price} ₽</b>\n"
            f"📊 Статус: {status}\n"
            f"🏷 Категория: {category}\n"
            f"👁 Просмотры: {views}\n\n"
            f"📝 <b>Описание:</b>\n{description}"
        )
        b = InlineKeyboardBuilder()
        b.button(text="⬆️ Поднять товар", callback_data=f"ad_bump:{callback_data.ad_id}")
        b.button(text="✏️ Изменить цену", callback_data=f"ad_price:{callback_data.ad_id}")
        if status_raw == "active":
            b.button(text="⏸ Приостановить", callback_data=f"ad_pause:{callback_data.ad_id}")
        elif status_raw in ("inactive", "disabled", "paused"):
            b.button(text="▶️ Активировать", callback_data=f"ad_activate:{callback_data.ad_id}")
        b.button(text="⬅️ К товарам", callback_data="ads_load")
        b.adjust(2, 1, 1)
        keyboard = b.as_markup()
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ad_bump:"))
async def bump_ad(callback: CallbackQuery, api: YooMarketAPI) -> None:
    ad_id = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Поднимаю товар...", show_alert=False)
    try:
        await api.bump_ad(ad_id)
        await callback.answer("✅ Товар поднят!", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data.startswith("ad_price:"))
async def ad_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    ad_id = callback.data.split(":", 1)[1]
    await state.set_state(AdEditState.waiting_new_price)
    await state.update_data(ad_id=ad_id)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"ad:{ad_id}")
    await callback.message.edit_text(
        "✏️ Введите новую цену (₽):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdEditState.waiting_new_price)
async def ad_price_save(message: Message, state: FSMContext, api: YooMarketAPI) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        price = int(float(raw))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите цену числом, например: <b>500</b>")
        return
    data = await state.get_data()
    ad_id = data.get("ad_id", "")
    await state.clear()
    try:
        await api.update_ad(ad_id, price=price)
        b = InlineKeyboardBuilder()
        b.button(text="📦 К товару", callback_data=f"ad:{ad_id}")
        b.button(text="⬅️ Все товары", callback_data="ads_load")
        b.adjust(2)
        await message.answer(f"✅ Цена обновлена: <b>{price} ₽</b>", reply_markup=b.as_markup())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=back_keyboard())


@router.callback_query(F.data.startswith("ad_pause:"))
async def ad_pause(callback: CallbackQuery, api: YooMarketAPI) -> None:
    ad_id = callback.data.split(":", 1)[1]
    try:
        await api.update_ad(ad_id, status="inactive")
        await callback.answer("⏸ Товар приостановлен", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)
    try:
        ad = await api.get_ad(ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status_raw = ad.get("status", "")
        status = _status(status_raw)
        text = (
            f"📦 <b>{title}</b>\n\n"
            f"💰 Цена: <b>{price} ₽</b>\n"
            f"📊 Статус: {status}"
        )
        b = InlineKeyboardBuilder()
        b.button(text="⬆️ Поднять товар", callback_data=f"ad_bump:{ad_id}")
        b.button(text="✏️ Изменить цену", callback_data=f"ad_price:{ad_id}")
        if status_raw in ("inactive", "disabled", "paused"):
            b.button(text="▶️ Активировать", callback_data=f"ad_activate:{ad_id}")
        b.button(text="⬅️ К товарам", callback_data="ads_load")
        b.adjust(2, 1, 1)
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        await callback.message.edit_text("✅ Статус обновлён", reply_markup=back_keyboard())


@router.callback_query(F.data.startswith("ad_activate:"))
async def ad_activate(callback: CallbackQuery, api: YooMarketAPI) -> None:
    ad_id = callback.data.split(":", 1)[1]
    try:
        await api.update_ad(ad_id, status="active")
        await callback.answer("▶️ Товар активирован", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)
    try:
        ad = await api.get_ad(ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status_raw = ad.get("status", "")
        status = _status(status_raw)
        text = (
            f"📦 <b>{title}</b>\n\n"
            f"💰 Цена: <b>{price} ₽</b>\n"
            f"📊 Статус: {status}"
        )
        b = InlineKeyboardBuilder()
        b.button(text="⬆️ Поднять товар", callback_data=f"ad_bump:{ad_id}")
        b.button(text="✏️ Изменить цену", callback_data=f"ad_price:{ad_id}")
        if status_raw == "active":
            b.button(text="⏸ Приостановить", callback_data=f"ad_pause:{ad_id}")
        b.button(text="⬅️ К товарам", callback_data="ads_load")
        b.adjust(2, 1, 1)
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        await callback.message.edit_text("✅ Статус обновлён", reply_markup=back_keyboard())


@router.callback_query(F.data == "ads:bulk_price")
async def bulk_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdEditState.waiting_bulk_percent)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="ads_load")
    await callback.message.edit_text(
        "💰 <b>Изменить цены всех товаров</b>\n\n"
        "Введите процент изменения:\n"
        "• <b>+10</b> — поднять на 10%\n"
        "• <b>-15</b> — снизить на 15%\n"
        "• <b>20</b> — поднять на 20%",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "ads:bulk_desc")
async def bulk_desc_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdEditState.waiting_bulk_description)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="ads_load")
    await callback.message.edit_text(
        "📝 <b>Изменить описание всех товаров</b>\n\n"
        "Введите новый текст описания (будет применён ко всем активным товарам):",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(AdEditState.waiting_bulk_description)
async def bulk_desc_save(message: Message, state: FSMContext, api: YooMarketAPI) -> None:
    desc = (message.text or "").strip()
    if not desc:
        await message.answer("❌ Введите текст описания")
        return
    await state.clear()
    await message.answer("⏳ Обновляю описания...")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        count = 0
        for ad in ads:
            ad_id = ad.get("id")
            if not ad_id:
                continue
            try:
                await api.update_ad(ad_id, description=desc)
                count += 1
            except Exception:
                pass
        b = InlineKeyboardBuilder()
        b.button(text="📦 Мои товары", callback_data="ads_load")
        b.adjust(1)
        await message.answer(f"✅ Описание обновлено у <b>{count}</b> товаров", reply_markup=b.as_markup())
    except Exception as e:
        from keyboards.main import back_keyboard
        await message.answer(f"❌ Ошибка: {e}", reply_markup=back_keyboard())


@router.message(AdEditState.waiting_bulk_percent)
async def bulk_price_save(message: Message, state: FSMContext, api: YooMarketAPI) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        percent = float(raw)
        if percent == 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число, например: <b>+10</b> или <b>-15</b>")
        return
    await state.clear()
    await message.answer("⏳ Обновляю цены...")
    try:
        count, msg = await api.bulk_change_prices(percent)
        b = InlineKeyboardBuilder()
        b.button(text="📦 Мои товары", callback_data="ads_load")
        b.adjust(1)
        await message.answer(msg, reply_markup=b.as_markup())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=back_keyboard())
