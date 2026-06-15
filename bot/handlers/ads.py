from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api import YooMarketAPI
from keyboards import AdCallback, PaginationCallback, back_keyboard

router = Router()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_MAP = {
    "active": "активен",
    "inactive": "неактивен",
    "blocked": "заблокирован",
    "sold": "продан",
}


def _status(raw: str) -> str:
    return STATUS_MAP.get(raw, raw)


def _build_ads_keyboard(
    ads: list[dict],
    next_cursor: str | None,
    prev_cursor: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for ad in ads:
        ad_id = str(ad.get("id", ""))
        title = ad.get("title") or ad.get("name") or f"Объявление {ad_id}"
        builder.button(
            text=f"🔍 {title}",
            callback_data=AdCallback(ad_id=ad_id).pack(),
        )
    builder.adjust(1)

    nav: list = []
    if prev_cursor:
        nav.append(
            builder.button(
                text="← Предыдущая",
                callback_data=PaginationCallback(entity="ads", cursor=prev_cursor).pack(),
            )
        )
    if next_cursor:
        builder.button(
            text="Следующая страница →",
            callback_data=PaginationCallback(entity="ads", cursor=next_cursor).pack(),
        )

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    return builder.as_markup()


def _format_ads_text(ads: list[dict], page_label: str) -> str:
    if not ads:
        return "📦 Товаров не найдено."

    lines = [f"📦 Ваши товары ({page_label}):\n"]
    for idx, ad in enumerate(ads, 1):
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status = _status(ad.get("status", ""))
        lines.append(f"{idx}. {title} — {price} ₽ [{status}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.message(F.text == "📦 Товары")
async def show_ads(message: Message, api: YooMarketAPI) -> None:
    try:
        data = await api.get_ads()
        ads: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = (
            data.get("meta", {}).get("next_cursor")
            or data.get("next_cursor")
        )
        text = _format_ads_text(ads, "стр. 1")
        keyboard = _build_ads_keyboard(ads, next_cursor)
    except Exception as e:
        text = f"❌ Ошибка: {e}"
        keyboard = back_keyboard()

    await message.answer(text, reply_markup=keyboard)


@router.callback_query(PaginationCallback.filter(F.entity == "ads"))
async def paginate_ads(
    callback: CallbackQuery,
    callback_data: PaginationCallback,
    api: YooMarketAPI,
) -> None:
    try:
        data = await api.get_ads(cursor=callback_data.cursor)
        ads: list[dict] = data.get("data") or data.get("items") or []
        next_cursor: str | None = (
            data.get("meta", {}).get("next_cursor")
            or data.get("next_cursor")
        )
        text = _format_ads_text(ads, f"курсор: {callback_data.cursor[:8]}…")
        keyboard = _build_ads_keyboard(ads, next_cursor)
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
    try:
        ad = await api.get_ad(callback_data.ad_id)
        title = ad.get("title") or ad.get("name") or "—"
        price = ad.get("price", "—")
        status = _status(ad.get("status", ""))
        description = ad.get("description") or "—"
        text = (
            f"📦 <b>{title}</b>\n\n"
            f"💰 Цена: {price} ₽\n"
            f"📊 Статус: {status}\n\n"
            f"📝 Описание:\n{description}"
        )
    except Exception as e:
        text = f"❌ Ошибка: {e}"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К товарам", callback_data="back_to_menu")

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()
