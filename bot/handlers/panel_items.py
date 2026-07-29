"""Product management through the seller panel (Nova API): list, edit,
hide/show, clone, delete. Works even when the Integration API can't."""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_panel_creds

router = Router()
logger = logging.getLogger(__name__)

_TIMEOUT = 40
_CAT_CACHE: dict[int, list[str]] = {}
_CATS_NAMES: dict[int, dict[int, str]] = {}   # uid -> {category_id: name}
# Ads deleted from the panel can still come back from GET /ads for a while, so
# they are remembered and filtered out — otherwise a removed listing keeps its
# button and stays in the count.
_DELETED: dict[int, set[str]] = {}


class PanelItemState(StatesGroup):
    waiting_price = State()
    waiting_title = State()
    waiting_stock = State()


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


@router.callback_query(F.data == "pitems:cats")
async def list_categories(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Seller's categories, grouped from the API.

    The panel's item rows carry no category at all — only the API does, as
    category_id, which the reference list turns into a name.
    """
    if not api:
        await callback.message.edit_text(
            "⚠️ Не настроен API-токен.", reply_markup=_no_session_kb())
        await callback.answer()
        return

    await callback.message.edit_text("⏳ Загружаю категории...")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        names = await _category_names(
            api, callback.from_user.id, _wanted_cats(ads))
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Не удалось загрузить объявления:\n<code>{str(e)[:200]}</code>",
            reply_markup=_no_session_kb())
        await callback.answer()
        return

    gone = _DELETED.get(callback.from_user.id, set())
    if gone:
        ads = [a for a in ads if str(a.get("id")) not in gone]

    groups: dict[str, int] = {}
    for ad in ads:
        groups[_ad_category(ad, names)] = groups.get(_ad_category(ad, names), 0) + 1

    if not groups:
        await callback.message.edit_text(
            "📦 Товаров пока нет.", reply_markup=_no_session_kb())
        await callback.answer()
        return

    ordered = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    _CAT_CACHE[callback.from_user.id] = [name for name, _ in ordered]

    b = InlineKeyboardBuilder()
    for i, (name, cnt) in enumerate(ordered[:40]):
        b.button(text=f"📂 {name[:24]} ({cnt})", callback_data=f"pcat:{i}")
    b.adjust(1)
    b.button(text="📋 Все товары", callback_data="pitems:allads")
    b.button(text="🔄 Обновить", callback_data="pitems:cats")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(2, 1)
    await callback.message.edit_text(
        f"📦 <b>Товары по категориям</b>\n\n"
        f"Всего товаров: <b>{len(ads)}</b>\n"
        f"Категорий: <b>{len(ordered)}</b>\n\nВыберите категорию:",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("pcat:"))
async def list_category_items(callback: CallbackQuery, api: YooMarketAPI) -> None:
    names_cache = _CAT_CACHE.get(callback.from_user.id) or []
    try:
        idx = int(callback.data.split(":", 1)[1])
        wanted = names_cache[idx]
    except (ValueError, IndexError):
        await callback.answer("Список устарел — обновите категории",
                              show_alert=True)
        return
    await _render_ads(callback, api, category=wanted)


@router.callback_query(F.data == "pitems:allads")
async def list_all_ads(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await _render_ads(callback, api, category=None)


_STATUS_LABELS = {
    "active": "🟢", "published": "🟢", "moderate": "🕓", "moderation": "🕓",
    "draft": "📝", "inactive": "🔴", "hidden": "🙈", "sold": "💤",
    "archived": "📦", "fraud": "⛔",
}


async def _render_ads(callback: CallbackQuery, api: YooMarketAPI,
                      category: str | None) -> None:
    """List ads, optionally limited to one category."""
    if not api:
        await callback.answer("⚠️ Не настроен API-токен", show_alert=True)
        return
    await callback.message.edit_text("⏳ Загружаю товары...")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        names = await _category_names(
            api, callback.from_user.id, _wanted_cats(ads))
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка загрузки:\n<code>{str(e)[:200]}</code>",
            reply_markup=_no_session_kb())
        await callback.answer()
        return

    gone = _DELETED.get(callback.from_user.id, set())
    if gone:
        ads = [a for a in ads if str(a.get("id")) not in gone]
    if category:
        ads = [a for a in ads if _ad_category(a, names) == category]

    b = InlineKeyboardBuilder()
    lines = []
    for ad in ads[:40]:
        mark = _STATUS_LABELS.get(str(ad.get("status", "")).lower(), "•")
        title = str(ad.get("title") or f"Товар {ad.get('id')}")
        price = _ad_price(ad)
        stock = ad.get("stock")
        lines.append(
            f"{mark} <b>{title}</b> — {price} ₽"
            + (f" · остаток {stock}" if stock is not None else ""))
        b.button(text=f"{mark} {title[:26]} — {price} ₽",
                 callback_data=f"pitem:{ad.get('id')}")
    b.adjust(1)
    b.button(text="📂 По категориям", callback_data="pitems:cats")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(2)

    header = f"📂 <b>{category}</b>" if category else "📋 <b>Все товары</b>"
    body = "\n".join(lines) if lines else "Здесь пока пусто."
    await callback.message.edit_text(
        f"{header}\n\nТоваров: <b>{len(ads)}</b>\n\n{body[:3000]}",
        reply_markup=b.as_markup())
    await callback.answer()


def _item_kb(item_id: str):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Цена", callback_data=f"pitem_price:{item_id}")
    b.button(text="✏️ Название", callback_data=f"pitem_title:{item_id}")
    b.button(text="📦 Остатки", callback_data=f"pitem_stock:{item_id}")
    b.button(text="🚀 На модерацию", callback_data=f"pitem_show:{item_id}")
    b.button(text="🙈 Скрыть", callback_data=f"pitem_hide:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"pitem_del:{item_id}")
    b.button(text="⬅️ К товарам", callback_data="pitems:list")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data == "pitems:list")
async def list_items(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Flat list of every listing — same view as «Все товары»."""
    await _render_ads(callback, api, category=None)


_TYPE_LABELS = {
    "simple": "Ограниченная выдача",
    "unlimited": "Безлимитная",
    "auto-delivery": "Авто-выдача",
    "auto-value": "Авто-выбор",
}
_STATUS_TEXT = {
    "active": "🟢 активен", "published": "🟢 опубликован",
    "moderate": "🕓 на модерации", "moderation": "🕓 на модерации",
    "draft": "📝 черновик", "inactive": "🔴 неактивен",
    "hidden": "🙈 скрыт", "sold": "💤 продан", "archived": "📦 в архиве",
    "fraud": "⛔ заблокирован",
}


@router.callback_query(F.data.startswith("pitem:"))
async def item_detail(callback: CallbackQuery, state: FSMContext,
                      api: YooMarketAPI) -> None:
    """Show the listing itself, not just its id.

    Everything here comes from GET /ads/{id}; without it the screen was a bare
    "Товар #219206" with buttons, which tells the seller nothing.
    """
    await state.clear()  # "Отмена" из ввода цены/названия ведёт сюда
    item_id = callback.data.split(":", 1)[1]
    await callback.answer()

    text = f"🛠 <b>Товар #{item_id}</b>"
    if api:
        try:
            ad = await api.get_ad(item_id)
            ad = ad.get("data") or ad
            title = str(ad.get("title") or f"Товар #{item_id}")
            price = _ad_price(ad)
            stock = ad.get("stock")
            status = _STATUS_TEXT.get(str(ad.get("status", "")).lower(),
                                      str(ad.get("status") or "—"))
            kind = _TYPE_LABELS.get(str(ad.get("type", "")),
                                    str(ad.get("type") or "—"))
            lines = [
                f"📦 <b>{title}</b>",
                "",
                f"💰 Цена: <b>{price} ₽</b>",
                f"📊 Статус: {status}",
                f"⚙️ Тип: {kind}",
            ]
            if stock is not None:
                lines.append(f"📥 Остаток: <b>{stock}</b>")
            if ad.get("views") is not None:
                lines.append(f"👁 Просмотров: {ad['views']}")
            lines.append(f"\n<code>#{item_id}</code>")
            text = "\n".join(lines)
        except Exception as e:
            logger.info("get_ad(%s) failed: %s", item_id, e)
            text = (f"🛠 <b>Товар #{item_id}</b>\n\n"
                    f"<i>Детали не загрузились: {str(e)[:120]}</i>")

    await _safe_edit_item(callback, text, _item_kb(item_id))


async def _safe_edit_item(callback: CallbackQuery, text: str, markup) -> None:
    """Edit, or send a fresh message when the old one cannot be edited
    (a photo message, or identical text)."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.info("item edit failed (%s), sending new", e)
        try:
            await callback.message.answer(text, reply_markup=markup)
        except Exception:
            logger.exception("item message could not be sent")


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

async def _stock_left(api: YooMarketAPI, item_id: str,
                      ad: dict | None = None) -> tuple[bool, str]:
    """How much stock an ad has. Returns (has_stock, human_note).

    Publishing is refused by the marketplace when an ad has nothing to sell, so
    this is checked BEFORE the publish call — otherwise the user just gets a
    rejection with no idea what to fix.
    On any error it returns True: a failed check must not block publishing.
    """
    try:
        if ad is None:
            ad = await api.get_ad(item_id)
        inner = ad.get("data") or ad
        ad_type = str(inner.get("type") or "")

        if ad_type == "auto-delivery":
            data = await api.get_ad_items(item_id)
            rows = data.get("data") or data.get("items") or []
            free = [r for r in rows
                    if str((r or {}).get("status", "available")) == "available"]
            return bool(free), f"позиций в наличии: {len(free)}"

        if ad_type == "auto-value":
            val = await api.get_ad_value(item_id)
            inner_v = val.get("data") or val
            stock = inner_v.get("stock")
            return bool(stock), f"остаток: {stock}"

        stock = inner.get("stock")
        if stock is None:
            return True, ""
        return bool(stock), f"остаток: {stock}"
    except Exception as e:
        logger.info("stock check skipped for %s: %s", item_id, e)
        return True, ""


async def _toggle(callback: CallbackQuery, public: bool,
                  api: YooMarketAPI | None = None) -> None:
    from automation.panel import panel_publish_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    if public and api:
        # One fetch serves both checks below.
        try:
            ad = await api.get_ad(item_id)
        except Exception as e:
            logger.info("get_ad(%s) failed: %s", item_id, e)
            ad = None

        # Already queued or already live? Say so instead of submitting again.
        status = str(((ad or {}).get("data") or ad or {}).get("status", "")).lower()
        if status in ("moderate", "moderation", "pending", "review"):
            await callback.answer(
                "🕓 Товар уже отправлен на модерацию", show_alert=True)
            return
        if status in ("active", "published"):
            await callback.answer("🟢 Товар уже опубликован", show_alert=True)
            return

        # Refuse to publish an empty listing — offer to fill it instead.
        has_stock, note = await _stock_left(api, item_id, ad)
        if not has_stock:
            b = InlineKeyboardBuilder()
            b.button(text="📦 Добавить остатки",
                     callback_data=f"pitem_stock:{item_id}")
            b.button(text="⬅️ К товару", callback_data=f"pitem:{item_id}")
            b.adjust(1)
            await callback.answer("Нет остатков", show_alert=True)
            await callback.message.edit_text(
                f"📦 <b>Нельзя опубликовать — нет остатков</b>\n\n"
                f"Товар #{item_id}: {note}.\n\n"
                f"Сначала добавьте позиции, потом публикуйте.",
                reply_markup=b.as_markup(),
            )
            return

    await callback.answer("⏳ Выполняю...")
    result, err = await _run(uid, panel_publish_item_sync, item_id, uid, public)
    if result and result[0]:
        if public:
            # Publishing does not put the ad on sale — it queues it for review,
            # and it goes live only once that passes.
            text = (f"✅ Товар #{item_id} отправлен на модерацию "
                    f"({result[1]})\n\n"
                    f"🕓 Появится в маркете после проверки.")
        else:
            text = f"✅ Товар #{item_id} скрыт ({result[1]})"
    else:
        detail = result[1] if result else err
        text = f"❌ Товар #{item_id}: не удалось.\n\n{detail}"
    await callback.message.edit_text(text, reply_markup=_item_kb(item_id))


@router.callback_query(F.data.startswith("pitem_show:"))
async def item_show(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await _toggle(callback, public=True, api=api)


@router.callback_query(F.data.startswith("pitem_hide:"))
async def item_hide(callback: CallbackQuery) -> None:
    await _toggle(callback, public=False)


@router.callback_query(F.data == "pitems:pubhidden")
async def publish_all_hidden(callback: CallbackQuery, api: YooMarketAPI) -> None:
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
    empty: list[str] = []
    last_err = ""
    for it in hidden:
        # Skip listings with nothing to sell instead of publishing them into
        # a rejection — they are reported separately so they can be filled.
        if api:
            has_stock, _note = await _stock_left(api, it["id"])
            if not has_stock:
                empty.append(str(it["id"]))
                continue
        r, e = await _run(uid, panel_publish_item_sync, it["id"], uid, True)
        if r and r[0]:
            ok += 1
        else:
            fail += 1
            last_err = (r[1] if r else e) or ""
    b = InlineKeyboardBuilder()
    b.button(text="🔄 К товарам", callback_data="pitems:list")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(2)
    text = (f"🌍 <b>Отправлено на модерацию</b>\n\n"
            f"✅ Товаров: <b>{ok}</b>\n"
            f"<i>Появятся в маркете после проверки.</i>")
    if empty:
        text += (f"\n📦 Пропущено без остатков: <b>{len(empty)}</b>"
                 f"\n<i>#{', #'.join(empty[:10])}</i>")
    if fail:
        text += f"\n❌ Не удалось: <b>{fail}</b>"
        if last_err:
            text += f"\n<i>{last_err[:120]}</i>"
    await callback.message.edit_text(text, reply_markup=b.as_markup())


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
async def item_delete_do(callback: CallbackQuery, api: YooMarketAPI) -> None:
    from automation.panel import panel_delete_item_sync

    item_id = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    await callback.answer("⏳ Удаляю...")
    result, err = await _run(uid, panel_delete_item_sync, item_id, uid)

    if result and result[0]:
        # Remember it: the API can keep returning a deleted ad briefly, which
        # would put its button and its count straight back.
        _DELETED.setdefault(uid, set()).add(str(item_id))
        await callback.answer(f"✅ Товар #{item_id} удалён", show_alert=True)
        # Straight back to the refreshed list, so the row is actually gone
        await _render_ads(callback, api, category=None)
        return

    detail = result[1] if result else err
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К товарам", callback_data="pitems:list")
    await callback.message.edit_text(
        f"❌ Не удалось удалить:\n{detail}", reply_markup=b.as_markup())


# ---------------------------------------------------------------------------
# Stock — has to be filled before an ad can go on sale
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("pitem_stock:"))
async def item_stock_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the stock to add. Goes through the Integration API, not the
    panel: /ads/{id}/items and /ads/{id}/value are the documented routes."""
    item_id = callback.data.split(":", 1)[1]
    await state.update_data(item_id=item_id)
    await state.set_state(PanelItemState.waiting_stock)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=f"pitem:{item_id}")
    await callback.message.edit_text(
        "📦 <b>Добавить остатки</b>\n\n"
        "<b>Авто-выдача</b> — пришлите позиции, <b>по одной в строке</b>:\n"
        "<code>KEY-1111\nKEY-2222\nKEY-3333</code>\n\n"
        "<b>Авто-выбор</b> — пришлите просто число, на сколько пополнить:\n"
        "<code>500</code>\n\n"
        "<i>Тип товара определю сам.</i>",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.message(PanelItemState.waiting_stock)
async def item_stock_save(message: Message, state: FSMContext,
                          api: YooMarketAPI) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Пришлите позиции или число")
        return
    if not api:
        await state.clear()
        await message.answer("⚠️ Не настроен API-токен — остатки идут через него")
        return

    data = await state.get_data()
    item_id = data.get("item_id", "")
    await state.clear()
    status = await message.answer("⏳ Добавляю остатки...")

    try:
        if ad is None:
            ad = await api.get_ad(item_id)
        inner = ad.get("data") or ad
        ad_type = str(inner.get("type") or "")

        if ad_type == "auto-value" or (text.isdigit() and ad_type != "auto-delivery"):
            await api.refill_ad_value(item_id, float(text))
            done = f"остаток пополнен на {text}"
        else:
            items = [ln.strip() for ln in text.splitlines() if ln.strip()]
            await api.add_ad_items(item_id, items)
            done = f"добавлено позиций: {len(items)}"

        b = InlineKeyboardBuilder()
        b.button(text="🚀 Отправить на модерацию",
                 callback_data=f"pitem_show:{item_id}")
        b.button(text="⬅️ К товару", callback_data=f"pitem:{item_id}")
        b.adjust(1)
        await status.edit_text(
            f"✅ <b>Готово</b> — {done}.\n\n"
            f"Теперь товар можно отправить на модерацию.",
            reply_markup=b.as_markup(),
        )
    except Exception as e:
        b = InlineKeyboardBuilder()
        b.button(text="⬅️ К товару", callback_data=f"pitem:{item_id}")
        await status.edit_text(
            f"❌ Не удалось добавить остатки:\n<code>{str(e)[:300]}</code>",
            reply_markup=b.as_markup(),
        )


def _ad_price(ad: dict) -> int:
    """Price of an ad. GET /ads returns it nested:
    {"amount": 149, "base_amount": 149, "currency": "RUB"} — reading it as a
    plain number silently skipped every ad in bulk operations."""
    p = ad.get("price")
    if isinstance(p, dict):
        p = p.get("amount", p.get("base_amount", 0))
    try:
        return int(float(str(p or 0)))
    except (TypeError, ValueError):
        return 0


async def _category_names(api: YooMarketAPI, uid: int,
                          wanted: set[int] | None = None) -> dict[int, str]:
    """category_id -> name, cached per user.

    The flat reference only covers the top of the tree, so ids the ads actually
    use are asked for individually rather than by walking every branch.
    """
    names = _CATS_NAMES.setdefault(uid, {})
    if not names:
        try:
            for c in await api.get_categories():
                cid = c.get("id")
                label = c.get("name") or c.get("title")
                if cid is not None and label:
                    names[int(cid)] = str(label)
        except Exception as e:
            logger.info("categories fetch failed: %s", e)

    missing = {c for c in (wanted or ()) if c not in names}
    for cid in sorted(missing):
        try:
            label = await api.resolve_category(cid)
        except Exception as e:
            logger.info("resolve_category(%s) failed: %s", cid, e)
            label = ""
        if label:
            names[cid] = label

    # Anything still unresolved lives deeper in the tree — walk it, bounded,
    # and only for the ids that are actually needed.
    missing = {c for c in (wanted or ()) if c not in names}
    if missing:
        try:
            names.update(await api.find_categories(missing))
        except Exception as e:
            logger.info("find_categories failed: %s", e)
    return names


def _wanted_cats(ads: list[dict]) -> set[int]:
    """The category ids these ads actually use — only those need resolving."""
    out: set[int] = set()
    for ad in ads:
        cid = ad.get("category_id")
        try:
            if cid not in (None, "", 0):
                out.add(int(cid))
        except (TypeError, ValueError):
            continue
    return out


# Titles read like "100 звезд" — a quantity plus the goods in the genitive.
# Stripping the number leaves that case ("Звезд"), which is not a category
# name, so the common goods on this marketplace are mapped back to nominative.
_GOODS_FORMS = {
    # English and Russian titles for the same goods share one group
    "звезд": "Звезды", "звёзд": "Звезды", "звезды": "Звезды",
    "звёзды": "Звезды", "stars": "Звезды", "star": "Звезды",
    "подписчиков": "Подписчики", "подписчика": "Подписчики",
    "просмотров": "Просмотры", "просмотра": "Просмотры",
    "лайков": "Лайки", "лайка": "Лайки",
    "монет": "Монеты", "монеты": "Монеты",
    "ключей": "Ключи", "ключа": "Ключи",
    "аккаунтов": "Аккаунты", "аккаунта": "Аккаунты",
    "робуксов": "Робуксы", "гемов": "Гемы", "алмазов": "Алмазы",
    "кристаллов": "Кристаллы", "рублей": "Рубли", "донатов": "Донат",
    "бустов": "Бусты", "реакций": "Реакции", "премиума": "Премиум",
}


def _label_from_title(title: str) -> str:
    """A category label derived from the ad's own title.

    Titles carry decoration and quantities — "💠100 ЗВЕЗД💠⚡МОМЕНТАЛЬНО⚡",
    "100 звезд", "500 звезд №219206" — so symbols and numbers are dropped and
    the goods word is looked for anywhere in what remains. All three land on
    «Звезды» instead of each becoming its own category.
    """
    # Strip emoji and punctuation, keeping letters and digits
    text = re.sub(r"[^\w\s]", " ", str(title or ""), flags=re.UNICODE)
    words = [w for w in text.split() if not w.isdigit()]
    # Drop quantity prefixes glued to a word ("100зв" stays, "100" goes)
    words = [re.sub(r"^\d+", "", w) for w in words]
    words = [w for w in words if w]
    if not words:
        return "Прочее"

    lowered = [w.lower() for w in words]
    for w in lowered:
        form = _GOODS_FORMS.get(w)
        if form:
            return form
    # Two-word goods names ("telegram stars")
    for i in range(len(lowered) - 1):
        form = _GOODS_FORMS.get(f"{lowered[i]} {lowered[i + 1]}")
        if form:
            return form

    # Nothing recognised: keep the wording, minus the noise words
    _NOISE = {"моментально", "быстро", "дешево", "новый", "акция", "хит",
              "лучшая", "цена", "instant", "fast", "cheap"}
    kept = [w for w in words if w.lower() not in _NOISE] or words
    text = " ".join(kept)[:30]
    # capitalize() would lowercase the rest and turn "Telegram Stars" into
    # "Telegram stars"
    return text[0].upper() + text[1:]


def _ad_category(ad: dict, names: dict[int, str]) -> str:
    """Category label of an ad.

    Prefers the real name; falls back to a label derived from the title rather
    than showing a bare id, which tells the seller nothing.
    """
    cid = ad.get("category_id")
    if cid not in (None, "", 0):
        try:
            label = names.get(int(cid))
        except (TypeError, ValueError):
            label = None
        if label:
            return label
    return _label_from_title(ad.get("title", ""))


@router.message(Command("ads_debug"))
async def ads_debug(message: Message, api: YooMarketAPI) -> None:
    """Show one ad exactly as the Integration API returns it.

    The panel's item rows carry no category at all, so grouping has to come
    from the API — this prints its field names instead of guessing them.
    """
    if not api:
        await message.answer("⚠️ Не настроен API-токен")
        return

    import html as _html
    import json as _json

    status = await message.answer("⏳ Читаю объявление из API...")
    try:
        data = await api.get_ads()
        rows = data.get("data") or data.get("items") or []
        if not rows:
            report = f"API вернул пусто: {_json.dumps(data, ensure_ascii=False)[:400]}"
        else:
            ad = rows[0]
            lines = [f"всего объявлений: {len(rows)}",
                     f"ключи: {list(ad.keys())}", ""]
            for k, v in ad.items():
                if isinstance(v, (dict, list)):
                    v = _json.dumps(v, ensure_ascii=False)
                lines.append(f"• {k} = {str(v)[:100]}")
            # Also check the reference list actually resolves this ad's
            # category — an id shown raw means the lookup came up empty.
            _CATS_NAMES.pop(message.from_user.id, None)
            cid = ad.get("category_id")
            names = await _category_names(
                api, message.from_user.id, {int(cid)} if cid else set())
            lines += ["", f"категорий в справочнике: {len(names)}",
                      f"category_id {cid} → {names.get(int(cid)) if cid else None!r}"]
            # Shape of the reference itself: is it a tree, is it paginated?
            try:
                raw = await api.categories_raw()
                rows = raw.get("data") or raw.get("items") or []
                lines.append(f"meta: {_json.dumps(raw.get('meta'), ensure_ascii=False)[:120]}")
                lines.append(f"links: {_json.dumps(raw.get('links'), ensure_ascii=False)[:120]}")
                if rows:
                    lines.append(f"пример категории: "
                                 f"{_json.dumps(rows[0], ensure_ascii=False)[:200]}")
            except Exception as e:
                lines.append(f"categories_raw: {str(e)[:120]}")
            report = "\n".join(lines)
    except Exception as e:
        report = f"ошибка: {str(e)[:250]}"
    await status.edit_text(
        f"🔍 <b>Объявление в API</b>\n\n<code>{_html.escape(report)[:3500]}</code>")


@router.message(Command("items_debug"))
async def items_debug(message: Message) -> None:
    """Show how the panel actually describes an item.

    Field names differ per panel, and guessing them is what left items showing
    as "Товар <id>" with no category. This prints the real ones.
    """
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return

    import json as _json

    from automation.panel import PANEL_URL, _make_panel_requests_session

    def _fetch() -> str:
        session = _make_panel_requests_session(creds["cookies"])
        r = session.get(f"{PANEL_URL}/nova-api/items",
                        params={"perPage": "1"}, timeout=(6, 12))
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:200]}"
        rows = (r.json() or {}).get("resources") or []
        if not rows:
            return "Панель вернула пустой список"
        row = rows[0]
        out = [f"ключи записи: {list(row.keys())}",
               f"title записи: {row.get('title')!r}"]
        fields = row.get("fields")
        if isinstance(fields, dict):
            fields = list(fields.values())
        from automation.panel import _html_badges, _strip_html

        for f in (fields or [])[:14]:
            if not isinstance(f, dict):
                continue
            val = f.get("value")
            if isinstance(val, (dict, list)):
                val = _json.dumps(val, ensure_ascii=False)[:80]
            name = str(f.get("name") or "")
            line = f"• {f.get('attribute')} | {name} = {str(val)[:80]}"
            # These columns render HTML; show what is actually read out of them
            if isinstance(val, str) and "<" in val:
                badges = _html_badges(val)
                line += (f"\n   → бейджи: {badges}" if badges
                         else f"\n   → текст: {_strip_html(val)[:80]}")
            out.append(line)
        return "\n".join(out)

    status = await message.answer("⏳ Читаю структуру товара...")
    try:
        loop = asyncio.get_event_loop()
        report = await asyncio.wait_for(loop.run_in_executor(None, _fetch),
                                        timeout=40)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    import html as _html
    await status.edit_text(
        f"🔍 <b>Структура товара</b>\n\n<code>{_html.escape(report)[:3500]}</code>")
