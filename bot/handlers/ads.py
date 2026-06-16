from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import (
    AdCallback,
    PaginationCallback,
    ads_list_keyboard,
    ads_menu_keyboard,
    back_keyboard,
)

router = Router()

STATUS_EMOJI = {
    "active": "🟢 Активен",
    "inactive": "🔴 Неактивен",
    "blocked": "⛔ Заблокирован",
    "sold": "✅ Продан",
}


def _status(raw: str) -> str:
    return STATUS_EMOJI.get(raw, f"⚪ {raw}")


def _fmt_list(ads: list[dict], total: int | None) -> str:
    if not ads:
        return "📦 Товаров не найдено."
    header = f"📦 <b>Ваши товары</b>"
    if total:
        header += f" (всего: {total})"
    lines = [header, ""]
    for i, ad in enumerate(ads, 1):
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status = _status(ad.get("status", ""))
        lines.append(f"{i}. <b>{title}</b>\n   💰 {price} ₽  |  {status}")
    return "\n".join(lines)


@router.callback_query(F.data == "menu:ads")
async def ads_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📦 <b>Товары</b>\n\nНажми кнопку чтобы загрузить все объявления с YooMarket:",
        reply_markup=ads_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "ads_load")
async def load_ads(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю товары...")
    try:
        data = await api.get_ads()
        ads: list[dict] = data.get("data") or data.get("items") or []
        meta = data.get("meta", {})
        next_cursor: str | None = meta.get("next_cursor")
        total: int | None = meta.get("total")
        text = _fmt_list(ads, total)
        keyboard = ads_list_keyboard(ads, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка загрузки: {e}"
        keyboard = back_keyboard()
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
        keyboard = ads_list_keyboard(ads, next_cursor)
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
) -> None:
    await callback.message.edit_text("⏳ Загружаю...")
    try:
        ad = await api.get_ad(callback_data.ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status = _status(ad.get("status", ""))
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
        builder = InlineKeyboardBuilder()
        builder.button(text="⬆️ Поднять товар", callback_data=f"ad_bump:{callback_data.ad_id}")
        builder.button(text="⬅️ К товарам", callback_data="ads_load")
        builder.adjust(1)
        keyboard = builder.as_markup()
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
