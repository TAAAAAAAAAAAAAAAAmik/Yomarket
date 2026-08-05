"""Прайс-лист: цены всех товаров на одном экране. Только просмотр.

Менять цену бот не умеет — намеренно. Через панель это невозможно (у
позиции нет такого поля), а через API не проверено ни разу, и правка,
которая может не примениться, хуже её отсутствия: продавец считает, что
цена новая, а торгует по старой.
"""
from __future__ import annotations

import html
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI

router = Router()
logger = logging.getLogger(__name__)

PER_PAGE = 10
# Сколько страниц маркетплейса вычитывать за раз. Прайс нужен целиком, но и
# качать бесконечно нельзя: у магазина с тысячей товаров это минуты ожидания.
MAX_PAGES = 6
_CACHE_TTL = 60

# uid → (когда загружено, список товаров)
_cache: dict[int, tuple[float, list[dict]]] = {}

# Со списка никуда не уходят, но страницу помним: «Обновить» должно оставлять
# на той же, а не бросать в начало длинного прайса.
_page_of: dict[int, int] = {}


def _price_of(ad: dict) -> int:
    """Цена объявления числом. 0 — если её в ответе нет.

    Маркетплейс присылает её объектом {"amount": …, "currency": …}, так что
    разбор общий с заказами — свой бы отстал от первого же изменения формата.
    """
    from orderfields import ad_price
    value = ad_price(ad)
    return int(round(value)) if value is not None else 0


def _title_of(ad: dict) -> str:
    return str(ad.get("title") or ad.get("name") or f"Товар {ad.get('id', '')}")


async def _load_ads(api: YooMarketAPI, uid: int, force: bool = False) -> list[dict]:
    """Все товары магазина, страница за страницей."""
    hit = _cache.get(uid)
    if hit and not force and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    ads: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        data = await api.get_ads(cursor=cursor) if cursor else await api.get_ads()
        chunk = data.get("data") or data.get("items") or []
        ads.extend(a for a in chunk if isinstance(a, dict))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not chunk:
            break
    _cache[uid] = (time.time(), ads)
    return ads


def _list_text(ads: list[dict], page: int) -> str:
    if not ads:
        return ("💰 <b>Прайс-лист</b>\n\n"
                "Товаров нет — добавить можно в «🚀 Объявления».")
    pages = max(1, (len(ads) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))

    priced = [p for p in (_price_of(a) for a in ads) if p]
    head = [f"💰 <b>Прайс-лист</b> — товаров: {len(ads)}"]
    if priced:
        head.append(f"<i>от {min(priced)} до {max(priced)} ₽</i>")
    lines = head + [""]

    for i, ad in enumerate(ads[page * PER_PAGE:(page + 1) * PER_PAGE],
                           start=page * PER_PAGE + 1):
        price = _price_of(ad)
        shown = f"{price} ₽" if price else "цена не указана"
        lines.append(f"{i}. {html.escape(_title_of(ad))} — <b>{shown}</b>")
    if pages > 1:
        lines += ["", f"<i>Страница {page + 1} из {pages}</i>"]
    lines += ["", "<i>Цена меняется на сайте — бот её только показывает.</i>"]
    return "\n".join(lines)


def _list_kb(ads: list[dict], page: int):
    b = InlineKeyboardBuilder()
    pages = max(1, (len(ads) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    if pages > 1:
        if page > 0:
            b.button(text="◀️ Назад", callback_data=f"prices:l:{page - 1}")
        if page < pages - 1:
            b.button(text="Ещё ▶️", callback_data=f"prices:l:{page + 1}")
    b.button(text="🔄 Обновить", callback_data="prices:reload")
    b.button(text="⬅️ Меню", callback_data="menu:main")
    b.adjust(*([2] if pages > 1 else []), 2)
    return b.as_markup()


async def _edit(message, text: str, kb=None) -> None:
    """Заменить текст экрана или, если не вышло, прислать новым сообщением.

    Отказ Telegram оставил бы на экране «⏳ Загружаю…» навсегда.
    """
    try:
        await message.edit_text(text[:4000], reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await message.answer(text[:4000], reply_markup=kb)
    except Exception:
        logger.warning("Прайс: не удалось показать экран")


async def _show_list(message, uid: int, api: YooMarketAPI, page: int = 0,
                     force: bool = False) -> None:
    try:
        ads = await _load_ads(api, uid, force=force)
    except Exception as e:
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Повторить", callback_data="prices:reload")
        b.button(text="⬅️ Меню", callback_data="menu:main")
        b.adjust(1)
        await _edit(message, "❌ <b>Не удалось загрузить товары</b>\n\n"
                             f"<code>{html.escape(str(e)[:200])}</code>",
                    b.as_markup())
        return
    await _edit(message, _list_text(ads, page), _list_kb(ads, page))


@router.callback_query(F.data == "prices:menu")
async def open_prices(callback: CallbackQuery, api: YooMarketAPI,
                      state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    _page_of[callback.from_user.id] = 0
    await _edit(callback.message, "⏳ Загружаю прайс…")
    await _show_list(callback.message, callback.from_user.id, api, 0)


@router.callback_query(F.data == "prices:reload")
async def reload_prices(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("Обновляю…")
    uid = callback.from_user.id
    await _show_list(callback.message, uid, api, _page_of.get(uid, 0),
                     force=True)


@router.callback_query(F.data.startswith("prices:l:"))
async def list_page(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer()
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        page = 0
    _page_of[callback.from_user.id] = page
    await _show_list(callback.message, callback.from_user.id, api, page)


@router.message(Command("prices"))
async def cmd_prices(message: Message, api: YooMarketAPI,
                     state: FSMContext) -> None:
    await state.clear()
    _page_of[message.from_user.id] = 0
    sent = await message.answer("⏳ Загружаю прайс…")
    await _show_list(sent, message.from_user.id, api, 0)
