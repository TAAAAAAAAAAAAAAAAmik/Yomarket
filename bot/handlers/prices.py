"""Экран «Цены»: цена каждого товара — отдельно и в два касания.

Расписание цен меняет всё оптом, а торговаться приходится по одному товару:
один демпингуют, другой держит цену, третий трогать нельзя. Здесь список
цен целиком перед глазами и правка того товара, который выбрали.

Что показано после правки — ответ маркетплейса, а не наша арифметика: если
он округлил или не принял цену, на экране будет правда.
"""
from __future__ import annotations

import html
import logging
import time
from decimal import Decimal, ROUND_HALF_UP

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

PER_PAGE = 8
# Сколько страниц маркетплейса вычитывать за раз. Экран цен нужен целиком —
# ходить за каждой страницей отдельно здесь незачем, но и качать бесконечно
# тоже: у магазина с тысячей товаров это минуты ожидания.
MAX_PAGES = 6
_CACHE_TTL = 60          # список цен живёт минуту, правка сбрасывает его сразу
_UNDO_TTL = 7 * 86400    # столько помним цену «до правок» для отката

# uid → (когда загружено, список товаров)
_cache: dict[int, tuple[float, list[dict]]] = {}

# Шаги быстрой правки. Проценты, а не рубли: у товара за 90 ₽ и за 5000 ₽
# «минус 50 рублей» значит совершенно разное.
STEPS = (-10, -5, 5, 10)


class PriceState(StatesGroup):
    waiting_exact = State()


# ---------------------------------------------------------------------------
# данные
# ---------------------------------------------------------------------------

def _round(value: float) -> int:
    """Округление к ближайшему, половина — вверх. Как считают деньги.

    Встроенный round() округляет к чётному: 544.5 → 544. То есть «+10%
    от 495 ₽» давало бы 544 ₽, и объяснить продавцу эту копейку нечем.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price_of(ad: dict) -> int:
    for key in ("price", "cost", "amount"):
        raw = ad.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(float(str(raw).replace(",", ".").replace(" ", "")))
        except (TypeError, ValueError):
            continue
    return 0


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


def _invalidate(uid: int) -> None:
    _cache.pop(uid, None)


def _find(ads: list[dict], ad_id: str) -> dict | None:
    for ad in ads:
        if str(ad.get("id", "")) == str(ad_id):
            return ad
    return None


# ---------------------------------------------------------------------------
# откат
# ---------------------------------------------------------------------------

def _undo_store(uid: int) -> tuple[dict, dict]:
    s = get_settings(uid)
    store = s.setdefault("price_undo", {})
    now = time.time()
    for ad_id in [k for k, v in store.items()
                  if now - float((v or {}).get("ts", 0)) > _UNDO_TTL]:
        store.pop(ad_id, None)
    return s, store


def _remember(uid: int, ad_id: str, was: int) -> None:
    """Запомнить цену до правок — но только первую.

    Иначе три нажатия «−5%» подряд затрут исходную цену, и вернуться будет
    некуда: откат приведёт туда, где уже успели наследить.
    """
    s, store = _undo_store(uid)
    if str(ad_id) not in store and was > 0:
        store[str(ad_id)] = {"was": int(was), "ts": time.time()}
        save_settings(uid, s)


def _undo_price(uid: int, ad_id: str) -> int:
    _s, store = _undo_store(uid)
    return int((store.get(str(ad_id)) or {}).get("was", 0) or 0)


def _forget(uid: int, ad_id: str) -> None:
    s, store = _undo_store(uid)
    if store.pop(str(ad_id), None) is not None:
        save_settings(uid, s)


# ---------------------------------------------------------------------------
# экран списка
# ---------------------------------------------------------------------------

def _list_text(ads: list[dict], page: int) -> str:
    if not ads:
        return ("💰 <b>Цены</b>\n\n"
                "Товаров нет — добавить можно в «🚀 Объявления».")
    pages = max(1, (len(ads) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    lines = [f"💰 <b>Цены</b> — товаров: {len(ads)}", ""]
    for i, ad in enumerate(ads[page * PER_PAGE:(page + 1) * PER_PAGE],
                           start=page * PER_PAGE + 1):
        price = _price_of(ad)
        shown = f"{price} ₽" if price else "цена не указана"
        lines.append(f"{i}. {html.escape(_title_of(ad))} — <b>{shown}</b>")
    lines += ["", "Нажмите на товар, чтобы изменить его цену."]
    if pages > 1:
        lines.append(f"<i>Страница {page + 1} из {pages}</i>")
    return "\n".join(lines)


def _list_kb(ads: list[dict], page: int):
    b = InlineKeyboardBuilder()
    pages = max(1, (len(ads) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    for ad in ads[page * PER_PAGE:(page + 1) * PER_PAGE]:
        price = _price_of(ad)
        title = _title_of(ad)
        label = f"{title[:26]} — {price} ₽" if price else title[:34]
        b.button(text=label, callback_data=f"prices:ad:{ad.get('id', '')}")
    b.adjust(1)
    if pages > 1:
        if page > 0:
            b.button(text="◀️ Назад", callback_data=f"prices:l:{page - 1}")
        if page < pages - 1:
            b.button(text="Ещё ▶️", callback_data=f"prices:l:{page + 1}")
    b.button(text="🔄 Обновить", callback_data="prices:reload")
    b.button(text="⬅️ Меню", callback_data="menu:main")
    b.adjust(1, *([2] if pages > 1 else []), 2)
    return b.as_markup()


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
        logger.warning("Цены: не удалось показать экран")


# ---------------------------------------------------------------------------
# карточка цены
# ---------------------------------------------------------------------------

def _card_text(ad: dict, was: int, note: str = "") -> str:
    price = _price_of(ad)
    lines = [f"💰 <b>{html.escape(_title_of(ad))}</b>", ""]
    lines.append(f"Цена: <b>{price} ₽</b>" if price
                 else "Цена: <b>не указана</b>")
    if was and was != price:
        diff = price - was
        sign = "+" if diff > 0 else "−"
        pct = (diff / was * 100) if was else 0
        lines.append(f"Было: {was} ₽  ({sign}{abs(diff)} ₽, "
                     f"{sign}{abs(pct):.0f}%)")
    if note:
        lines += ["", note]
    lines += ["", "<i>Шаги считаются от текущей цены.</i>"]
    return "\n".join(lines)


def _card_kb(ad_id: str, was: int, page: int):
    b = InlineKeyboardBuilder()
    for step in STEPS:
        sign = "+" if step > 0 else "−"
        b.button(text=f"{sign}{abs(step)}%",
                 callback_data=f"prices:p:{ad_id}:{step}")
    b.adjust(4)
    b.button(text="✏️ Точная цена", callback_data=f"prices:set:{ad_id}")
    if was:
        b.button(text=f"↩️ Вернуть {was} ₽",
                 callback_data=f"prices:undo:{ad_id}")
    b.button(text="⬅️ К списку", callback_data=f"prices:l:{page}")
    b.adjust(4, 1, 1, 1)
    return b.as_markup()


# Со списка на карточку и обратно — чтобы «Назад» вело на ту же страницу,
# с которой ушли, а не в начало длинного списка.
_page_of: dict[int, int] = {}


async def _show_card(message, uid: int, api: YooMarketAPI, ad_id: str,
                     note: str = "", expect: int | None = None) -> None:
    try:
        ad = await api.get_ad(ad_id)
    except Exception as e:
        ads = _cache.get(uid, (0, []))[1]
        ad = _find(ads, ad_id)
        if ad is None:
            await _edit(message, f"❌ Товар не открылся: "
                                 f"<code>{html.escape(str(e)[:150])}</code>")
            return
    ad = ad.get("data") if isinstance(ad.get("data"), dict) else ad
    # Галочку ставим по факту, а не по намерению: маркетплейс отвечает «ок» и
    # на цену, которую потом не применяет, а «✅ обновлено» при старой цене на
    # сайте — худшее, что бот может сказать.
    if expect is not None and _price_of(ad) != expect:
        note = (f"⚠️ <b>Цена не изменилась.</b> Просили {expect} ₽, "
                f"у товара осталось {_price_of(ad)} ₽ — маркетплейс принял "
                f"запрос, но цену не применил.")
    was = _undo_price(uid, ad_id)
    await _edit(message, _card_text(ad, was, note),
                _card_kb(ad_id, was, _page_of.get(uid, 0)))


# ---------------------------------------------------------------------------
# обработчики
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "prices:menu")
async def open_prices(callback: CallbackQuery, api: YooMarketAPI,
                      state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    _page_of[callback.from_user.id] = 0
    await _edit(callback.message, "⏳ Загружаю цены…")
    await _show_list(callback.message, callback.from_user.id, api, 0)


@router.callback_query(F.data == "prices:reload")
async def reload_prices(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("Обновляю…")
    uid = callback.from_user.id
    await _show_list(callback.message, uid, api, _page_of.get(uid, 0),
                     force=True)


@router.message(Command("prices"))
async def cmd_prices(message: Message, api: YooMarketAPI,
                     state: FSMContext) -> None:
    await state.clear()
    _page_of[message.from_user.id] = 0
    sent = await message.answer("⏳ Загружаю цены…")
    await _show_list(sent, message.from_user.id, api, 0)


@router.callback_query(F.data.startswith("prices:l:"))
async def list_page(callback: CallbackQuery, api: YooMarketAPI,
                    state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        page = 0
    _page_of[callback.from_user.id] = page
    await _show_list(callback.message, callback.from_user.id, api, page)


@router.callback_query(F.data.startswith("prices:ad:"))
async def open_card(callback: CallbackQuery, api: YooMarketAPI,
                    state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    ad_id = callback.data.split(":", 2)[2]
    await _show_card(callback.message, callback.from_user.id, api, ad_id)


@router.callback_query(F.data.startswith("prices:p:"))
async def step_price(callback: CallbackQuery, api: YooMarketAPI) -> None:
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Не понял кнопку", show_alert=True)
        return
    ad_id, raw_step = parts[2], parts[-1]
    try:
        step = float(raw_step)
    except ValueError:
        await callback.answer("Не понял шаг", show_alert=True)
        return

    uid = callback.from_user.id
    try:
        ad = await api.get_ad(ad_id)
        ad = ad.get("data") if isinstance(ad.get("data"), dict) else ad
    except Exception as e:
        await callback.answer(f"❌ {str(e)[:80]}", show_alert=True)
        return
    current = _price_of(ad)
    if current <= 0:
        await callback.answer(
            "У товара не указана цена — задайте её точным числом",
            show_alert=True)
        return
    new_price = max(1, _round(current * (1 + step / 100)))
    if new_price == current:
        # На дешёвом товаре шаг может не дотянуть до рубля. Молчать здесь
        # нельзя: кнопка выглядела бы сломанной.
        await callback.answer(
            f"{step:+.0f}% от {current} ₽ — меньше рубля, цена не изменилась",
            show_alert=True)
        return
    await _apply(callback, api, uid, ad_id, current, new_price)


@router.callback_query(F.data.startswith("prices:undo:"))
async def undo_price(callback: CallbackQuery, api: YooMarketAPI) -> None:
    uid = callback.from_user.id
    ad_id = callback.data.split(":", 2)[2]
    was = _undo_price(uid, ad_id)
    if not was:
        await callback.answer("Возвращать не к чему", show_alert=True)
        return
    try:
        ad = await api.get_ad(ad_id)
        ad = ad.get("data") if isinstance(ad.get("data"), dict) else ad
        current = _price_of(ad)
    except Exception:
        current = 0
    try:
        await api.update_price(ad_id, was)
    except Exception as e:
        await callback.answer(f"❌ {str(e)[:120]}", show_alert=True)
        return
    _forget(uid, ad_id)
    _invalidate(uid)
    await callback.answer(f"↩️ Вернул {was} ₽")
    note = (f"↩️ Вернул прежнюю цену: <b>{was} ₽</b>"
            + (f" (было {current} ₽)" if current and current != was else ""))
    await _show_card(callback.message, uid, api, ad_id, note, expect=was)


@router.callback_query(F.data.startswith("prices:set:"))
async def exact_start(callback: CallbackQuery, state: FSMContext) -> None:
    ad_id = callback.data.split(":", 2)[2]
    await state.set_state(PriceState.waiting_exact)
    await state.update_data(ad_id=ad_id)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"prices:ad:{ad_id}")
    await _edit(callback.message,
                "✏️ <b>Новая цена</b>\n\n"
                "Пришлите числом — например <b>450</b>.\n\n"
                "Можно и относительно текущей:\n"
                "• <b>+50</b> или <b>-30</b> — прибавить или убавить рубли\n"
                "• <b>-15%</b> — изменить на процент",
                b.as_markup())
    await callback.answer()


def _parse_price(raw: str, current: int) -> tuple[int, str]:
    """Разобрать «450», «+50», «-15%». Возвращает (цена, ошибка)."""
    text = (raw or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return 0, "Пустое сообщение"
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    relative = text[:1] in "+-"
    try:
        value = float(text)
    except ValueError:
        return 0, "Это не похоже на число"
    if percent:
        if not relative:
            return 0, "Процент нужен со знаком: <b>-15%</b> или <b>+10%</b>"
        if current <= 0:
            return 0, "У товара нет цены — задайте её числом"
        price = _round(current * (1 + value / 100))
    elif relative:
        if current <= 0:
            return 0, "У товара нет цены — задайте её числом"
        price = _round(current + value)
    else:
        price = _round(value)
    if price < 1:
        return 0, "Цена должна быть хотя бы 1 ₽"
    if price > 10_000_000:
        return 0, "Слишком большая цена — проверьте число"
    return int(price), ""


@router.message(PriceState.waiting_exact)
async def exact_save(message: Message, state: FSMContext,
                     api: YooMarketAPI) -> None:
    data = await state.get_data()
    ad_id = str(data.get("ad_id") or "")
    uid = message.from_user.id
    try:
        ad = await api.get_ad(ad_id)
        ad = ad.get("data") if isinstance(ad.get("data"), dict) else ad
        current = _price_of(ad)
    except Exception:
        current = 0

    price, err = _parse_price(message.text or "", current)
    if err:
        # Состояние держим: человек ошибся в числе, а не передумал — пусть
        # просто пришлёт ещё раз, не проходя весь путь заново.
        await message.answer(f"❌ {err}. Пришлите цену ещё раз.")
        return
    await state.clear()

    if current > 0:
        _remember(uid, ad_id, current)
    try:
        await api.update_price(ad_id, price)
    except Exception as e:
        await message.answer(f"❌ Цена не изменилась: "
                             f"<code>{html.escape(str(e)[:200])}</code>")
        return
    _invalidate(uid)
    sent = await message.answer("⏳ Сохраняю…")
    note = f"✅ Новая цена: <b>{price} ₽</b>"
    await _show_card(sent, uid, api, ad_id, note, expect=price)


async def _apply(callback: CallbackQuery, api: YooMarketAPI, uid: int,
                 ad_id: str, current: int, new_price: int) -> None:
    _remember(uid, ad_id, current)
    try:
        await api.update_price(ad_id, new_price)
    except Exception as e:
        await callback.answer(f"❌ {str(e)[:120]}", show_alert=True)
        return
    _invalidate(uid)
    await callback.answer(f"{current} ₽ → {new_price} ₽")
    await _show_card(callback.message, uid, api, ad_id,
                     f"✅ {current} ₽ → <b>{new_price} ₽</b>", expect=new_price)
