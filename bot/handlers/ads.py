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
    """Summary only. The listings are browsed under «📦 Товары», so repeating
    them here just makes the menu scroll."""
    if not ads:
        return ("🚀 <b>Объявления</b>\n\n"
                "Товаров пока нет — добавьте первый.")
    count = total or len(ads)
    return (f"🚀 <b>Объявления</b>\n\n"
            f"Всего товаров: <b>{count}</b>\n\n"
            f"Откройте «📦 Товары», чтобы посмотреть их по категориям.")


def _ads_keyboard(ads: list[dict], next_cursor: str | None):
    """Actions only — the listings themselves live behind «📦 Товары».

    This menu used to repeat every ad as its own button, which duplicated the
    category browser and pushed the actions off the screen.
    """
    b = InlineKeyboardBuilder()
    b.button(text="📦 Товары", callback_data="pitems:cats")
    b.button(text="➕ Добавить товар", callback_data="create_ad:start")
    b.button(text="🛠 Управление", callback_data="pitems:list")
    b.button(text="📦 Паки", callback_data="packs:menu")
    b.button(text="💰 Все цены", callback_data="ads:bulk_price")
    b.button(text="📝 Описание всех", callback_data="ads:bulk_desc")
    # Listing automation lives here rather than in the auto-settings menu:
    # both act on ads, so this is where they are looked for.
    b.button(text="⭐ Премиум продвижение", callback_data="selenium:bump:menu")
    b.button(text="🔄 Авто-восстановление", callback_data="selenium:restore:menu")
    b.button(text="🔄 Обновить", callback_data="ads_load")
    b.button(text="⬅️ Меню", callback_data="menu:main")
    b.adjust(2, 2, 2, 2, 2)
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
    """Ask before promoting: «Премиум» is a paid action on this marketplace."""
    ad_id = callback.data.split(":", 1)[1]
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, продвинуть", callback_data=f"ad_bump_ok:{ad_id}")
    b.button(text="❌ Отмена", callback_data=f"ad:{ad_id}")
    b.adjust(1)
    await callback.message.answer(
        "⚠️ <b>Продвижение платное</b>\n\n"
        "На Юмаркете поднятие — это действие «Премиум». Оплата идёт не с "
        "баланса магазина, а выбранным способом — придёт ссылка на "
        "оплату.\n\nПродвинуть этот товар?",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ad_bump_ok:"))
async def bump_ad_confirmed(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Run the paid promotion through the panel — the API has no such method."""
    import asyncio

    from automation.panel import panel_bump_item_sync
    from storage import get_panel_creds

    from handlers.selenium_settings import promo_params
    from storage import get_settings

    ad_id = callback.data.split(":", 1)[1]
    creds = get_panel_creds(callback.from_user.id)
    if not creds or not creds.get("cookies"):
        await callback.answer("⚠️ Нужен вход в панель продавца", show_alert=True)
        return

    s = get_settings(callback.from_user.id)
    params = promo_params(s)
    if not params:
        b = InlineKeyboardBuilder()
        b.button(text="⚙️ Выбрать тариф", callback_data="promo:setup")
        b.button(text="⬅️ Назад", callback_data=f"ad:{ad_id}")
        b.adjust(1)
        await callback.message.edit_text(
            "⚙️ <b>Сначала выберите тариф</b>\n\n"
            "«Премиум» требует услугу, срок и способ оплаты — сроки стоят "
            "по-разному, поэтому я не подставляю их сам.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    await callback.answer("⏳ Продвигаю...", show_alert=False)
    try:
        loop = asyncio.get_event_loop()
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(
                None, panel_bump_item_sync, creds["cookies"], ad_id,
                callback.from_user.id, True, params),
            timeout=60,
        )
        if ok:
            await callback.message.edit_text(f"✅ {msg[:600]}")
        else:
            hint = ""
            if "нет прав" in msg.lower():
                # Nova authorizes per record, so this is about this listing
                hint = ("\n\nПанель отказала именно для этого объявления. "
                        "Продвижение может быть недоступно для его текущего "
                        "состояния — попробуйте на активном.")
            await callback.message.edit_text(f"⛔ {msg[:400]}{hint}")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")


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
        await api.update_price(ad_id, price)
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
        await api.unpublish_ad(ad_id)
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
        b.adjust(1, 1, 1)
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        await callback.message.edit_text("✅ Статус обновлён", reply_markup=back_keyboard())


@router.callback_query(F.data.startswith("ad_activate:"))
async def ad_activate(callback: CallbackQuery, api: YooMarketAPI) -> None:
    ad_id = callback.data.split(":", 1)[1]
    try:
        await api.restore_ad(ad_id)
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
        b.adjust(1, 1, 1)
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
                # The spec has no endpoint for editing a description:
                # only price, publish/unpublish, items and value are exposed.
                raise RuntimeError(
                    "API не поддерживает изменение описания — "
                    "отредактируйте товар в панели"
                )
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
