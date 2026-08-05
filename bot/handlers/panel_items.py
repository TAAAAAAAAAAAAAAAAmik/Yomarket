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
from storage import get_panel_creds, get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)

_TIMEOUT = 40
_CAT_CACHE: dict[int, list[str]] = {}
_CATS_NAMES: dict[int, dict[int, str]] = {}   # uid -> {category_id: name}

# Ads deleted from the panel can still come back from GET /ads for a while, so
# their ids are remembered and filtered out — otherwise a removed listing keeps
# its button and stays in the count. This lives in settings, not memory: the
# container is rebuilt on every deploy, and an in-memory set was wiped each
# time, so every deleted item reappeared after the next update.
# Statuses the marketplace itself uses for a removed listing — filtered even if
# the id was never recorded, which covers items deleted straight from the site.
_GONE_STATUSES = {"deleted", "removed", "trashed", "trash", "destroyed"}


def _deleted_ids(uid: int) -> set[str]:
    return {str(x) for x in (get_settings(uid).get("deleted_ads") or [])}


def _mark_deleted(uid: int, item_id) -> None:
    s = get_settings(uid)
    lst = s.setdefault("deleted_ads", [])
    if str(item_id) not in lst:
        lst.append(str(item_id))
        del lst[:-1000]          # keep the set bounded
        save_settings(uid, s)


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _is_gone(ad: dict) -> bool:
    return str(ad.get("status", "")).lower() in _GONE_STATUSES


class PanelItemState(StatesGroup):
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

    from handlers.ads import _safe_edit
    await callback.answer()
    await _safe_edit(callback.message, "⏳ Загружаю категории...")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        names = await _category_names(
            api, callback.from_user.id, _wanted_cats(ads))
    except Exception as e:
        from handlers.ads import _load_error
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Повторить", callback_data="pitems:cats")
        b.button(text="⬅️ Назад", callback_data="menu:ads")
        b.adjust(1)
        await _safe_edit(callback.message, _load_error(e), b.as_markup())
        return

    gone = _deleted_ids(callback.from_user.id)
    ads = [a for a in ads
           if str(a.get("id")) not in gone and not _is_gone(a)]

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
    from handlers.ads import _safe_edit
    await callback.answer()
    await _safe_edit(callback.message, "⏳ Загружаю товары...")
    try:
        data = await api.get_ads()
        ads = data.get("data") or data.get("items") or []
        names = await _category_names(
            api, callback.from_user.id, _wanted_cats(ads))
    except Exception as e:
        from handlers.ads import _load_error
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Повторить", callback_data="pitems:cats")
        b.button(text="⬅️ Назад", callback_data="menu:ads")
        b.adjust(1)
        await _safe_edit(callback.message, _load_error(e), b.as_markup())
        return

    gone = _deleted_ids(callback.from_user.id)
    ads = [a for a in ads
           if str(a.get("id")) not in gone and not _is_gone(a)]
    if category:
        ads = [a for a in ads if _ad_category(a, names) == category]

    b = InlineKeyboardBuilder()
    lines = []
    for ad in ads[:40]:
        mark = _STATUS_LABELS.get(str(ad.get("status", "")).lower(), "•")
        title = str(ad.get("title") or f"Товар {ad.get('id')}")
        price = _ad_price(ad)
        stock = ad.get("stock")
        # Titles come from the marketplace; an unescaped '<' would make the
        # edit fail and freeze the "⏳ Загружаю товары..." placeholder.
        lines.append(
            f"{mark} <b>{_esc(title)}</b> — {price} ₽"
            + (f" · остаток {stock}" if stock is not None else ""))
        b.button(text=f"{mark} {title[:26]} — {price} ₽",
                 callback_data=f"pitem:{ad.get('id')}")
    b.adjust(1)
    b.button(text="📂 По категориям", callback_data="pitems:cats")
    b.button(text="⬅️ Назад", callback_data="menu:ads")
    b.adjust(2)

    header = f"📂 <b>{_esc(category)}</b>" if category else "📋 <b>Все товары</b>"
    body = "\n".join(lines) if lines else "Здесь пока пусто."
    await _safe_edit(
        callback.message,
        f"{header}\n\nТоваров: <b>{len(ads)}</b>\n\n{body[:3000]}",
        b.as_markup())


def _item_kb(item_id: str):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Название", callback_data=f"pitem_title:{item_id}")
    b.button(text="📦 Остатки", callback_data=f"pitem_stock:{item_id}")
    b.button(text="🚀 На модерацию", callback_data=f"pitem_show:{item_id}")
    b.button(text="🙈 Скрыть", callback_data=f"pitem_hide:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"pitem_del:{item_id}")
    # Promotion lived only in the other listing view, so a card opened from
    # «📦 Товары» had no way to reach it
    b.button(text="⭐ Премиум продвижение", callback_data=f"ad_bump:{item_id}")
    b.button(text="⬅️ К товарам", callback_data="pitems:list")
    b.adjust(2, 2, 1, 1, 1)
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


# ── Название ───────────────────────────────────────────────────────────────

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
        # Remember it persistently: the API can keep returning a deleted ad
        # briefly, and the container is rebuilt on every deploy — an in-memory
        # note would be lost and the ad would come back.
        _mark_deleted(uid, item_id)
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


@router.message(Command("scan"))
async def scan_page(message: Message) -> None:  # noqa: C901
    """/scan /support — read a panel page and find the endpoints it calls.

    A page that shows messages must fetch them from somewhere; pointing this at
    the support chat gives that address without needing a browser.
    """
    import re as _re_mod

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите страницу панели, например:\n<code>/scan /support</code>\n\n"
            "Адрес возьмите из строки браузера, когда открыт нужный чат.")
        return
    page_path = parts[1].strip()
    # Accept a full URL pasted from the address bar, not just a path
    page_path = _re_mod.sub(r"^https?://[^/]+", "", page_path)
    if not page_path.startswith("/"):
        page_path = "/" + page_path

    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return

    import html as _html
    import re as _re

    from automation.panel import PANEL_URL, _make_panel_requests_session

    def _fetch() -> str:
        session = _make_panel_requests_session(creds["cookies"])
        out = []
        try:
            r = session.get(PANEL_URL + page_path, timeout=(6, 15))
        except Exception as e:
            return f"не открылась: {str(e)[:120]}"
        html = r.text
        out.append(f"{page_path} → {r.status_code}, {len(html)}б")

        # Endpoints named right in the page
        inline = set(_re.findall(
            r'["\'`](/[a-z0-9/_-]*(?:chat|support|message|ticket|dialog)[a-z0-9/_-]*)["\'`]',
            html, _re.I))
        if inline:
            out.append(f"в HTML: {sorted(inline)[:12]}")

        srcs = list(dict.fromkeys(
            _re.findall(r'src="(/[^"]+\.js[^"]*)"', html)
            + _re.findall(r'"(/[^"]+\.js)"', html)))[:6]
        out.append(f"скриптов: {len(srcs)}")
        hits = set()
        for src in srcs:
            try:
                js = session.get(PANEL_URL + src, timeout=(6, 20)).text
            except Exception:
                continue
            for m in _re.findall(
                    r'["\'`](/[a-z0-9/_-]*(?:chat|support|message|ticket|dialog|notif)[a-z0-9/_-]*)["\'`]',
                    js, _re.I):
                if len(m) < 60:
                    hits.add(m)
        out.append(f"в JS: {sorted(hits)[:20] or 'ничего'}")

        # For a page like /chats/1076867 the messages come from a sibling
        # address — probe the usual shapes directly.
        m = _re.match(r"^/([a-z-]+)/(\d+)", page_path)
        if m:
            section, oid = m.group(1), m.group(2)
            out.append("\n— проверка адресов сообщений —")
            for cand in (
                f"/api/{section}/{oid}/messages",
                f"/{section}/{oid}/messages",
                f"/api/{section}/{oid}",
                f"/nova-api/{section}/{oid}",
                f"/{section}/{oid}/list",
                f"/api/{section}/{oid}/list",
            ):
                try:
                    rr = session.get(PANEL_URL + cand, timeout=(5, 10),
                                     allow_redirects=False)
                except Exception:
                    continue
                if rr.status_code in (404, 405):
                    continue
                body = rr.text[:130].replace("\n", " ")
                out.append(f"{cand} → {rr.status_code}: {body}")
        return "\n".join(out)

    status = await message.answer(f"⏳ Читаю {page_path}...")
    try:
        loop = asyncio.get_event_loop()
        report = await asyncio.wait_for(loop.run_in_executor(None, _fetch),
                                        timeout=90)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    await status.edit_text(
        f"🔍 <b>{_html.escape(page_path)}</b>\n\n"
        f"<code>{_html.escape(report)[:3500]}</code>")


@router.message(Command("panel_debug"))
async def panel_debug(message: Message) -> None:
    """List what the panel exposes, so support chats can be located.

    Order chats come from the Integration API, which has no way to list chats —
    only to read one by id. Anything outside an order (support, moderation)
    therefore has to come from the panel, if it exposes it at all.
    """
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return

    import html as _html
    import re as _re

    from automation.panel import PANEL_URL, _make_panel_requests_session

    def _fetch() -> str:
        session = _make_panel_requests_session(creds["cookies"])
        out = []
        for path in ("/nova-api/navigation", "/nova-api/resources"):
            try:
                r = session.get(PANEL_URL + path, timeout=(6, 12))
                keys = sorted(set(_re.findall(r'"uriKey"\s*:\s*"([^"]+)"', r.text)))
                out.append(f"{path} → {r.status_code}, ресурсов: {len(keys)}")
                if keys:
                    out.append("  " + ", ".join(keys))
            except Exception as e:
                out.append(f"{path}: {str(e)[:80]}")

        # Anything chat-shaped is what we are after
        # "notifies" is the likeliest home for support and moderation notices
        for guess in ("notifies", "chats", "messages", "dialogs", "tickets",
                      "support", "emails"):
            try:
                r = session.get(f"{PANEL_URL}/nova-api/{guess}",
                                params={"perPage": "2"}, timeout=(6, 10),
                                allow_redirects=False)
                if r.status_code == 404:
                    continue
                out.append(f"\n/nova-api/{guess} → {r.status_code}")
                if r.status_code == 200:
                    rows = (r.json() or {}).get("resources") or []
                    out.append(f"  записей: {len(rows)}")
                    if rows:
                        row = rows[0]
                        out.append(f"  title: {row.get('title')!r}")
                        fields = row.get("fields")
                        if isinstance(fields, dict):
                            fields = list(fields.values())
                        for f in (fields or [])[:8]:
                            if isinstance(f, dict):
                                val = str(f.get("value"))[:60]
                                out.append(f"  • {f.get('attribute')} | "
                                           f"{f.get('name')} = {val}")
                else:
                    out.append(f"  {r.text[:120]}")
            except Exception as e:
                out.append(f"/nova-api/{guess}: {str(e)[:60]}")

        # The support chat is not a Nova resource, so look for the panel's own
        # endpoint — the same approach that found the login route.
        out.append("\n— свои адреса панели —")
        for path in ("/api/chats", "/api/support", "/chats", "/support",
                     "/api/messages", "/api/tickets", "/api/chat/messages",
                     "/nova-vendor/chat", "/api/notifications"):
            try:
                r = session.get(PANEL_URL + path, timeout=(5, 8),
                                allow_redirects=False)
                if r.status_code in (404, 405):
                    continue
                body = r.text[:100].replace("\n", " ")
                out.append(f"{path} → {r.status_code}: {body}")
            except Exception:
                continue

        # And what the panel's own scripts call
        try:
            html = session.get(PANEL_URL + "/", timeout=(6, 12)).text
            srcs = _re.findall(r'src="(/[^"]+\.js[^"]*)"', html)[:4]
            hits = set()
            for src in srcs:
                js = session.get(PANEL_URL + src, timeout=(6, 15)).text
                for m in _re.findall(
                        r'["\'`](/[a-z0-9/_-]*(?:chat|support|message|ticket)[a-z0-9/_-]*)["\'`]',
                        js, _re.I):
                    if len(m) < 60:
                        hits.add(m)
            out.append(f"в JS панели: {sorted(hits)[:15] or 'ничего'}")
        except Exception as e:
            out.append(f"JS: {str(e)[:60]}")
        return "\n".join(out) or "пусто"

    status = await message.answer("⏳ Смотрю ресурсы панели...")
    try:
        loop = asyncio.get_event_loop()
        report = await asyncio.wait_for(loop.run_in_executor(None, _fetch),
                                        timeout=60)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    await status.edit_text(
        f"🔍 <b>Ресурсы панели</b>\n\n<code>{_html.escape(report)[:3500]}</code>")


@router.message(Command("promo_debug"))
async def promo_debug(message: Message) -> None:
    """Dump the «Премиум» action exactly as the panel describes it.

    Every earlier attempt read a summary of these fields, and the summary drops
    precisely the keys that would say what «Оплата» is. This prints the raw
    JSON so the answer stops being a guess.
    """
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return

    import html as _html
    import json as _json

    from automation.panel import (PANEL_URL, _find_promo_action,
                                  _make_panel_requests_session,
                                  _normalize_options, _panel_xsrf_headers,
                                  panel_list_items_sync)

    def _fetch() -> str:
        ok, items = panel_list_items_sync(creds["cookies"])
        if not ok:
            return f"объявления не прочитались: {items}"
        ids = [i.get("id") for i in items if i.get("id")]
        if not ids:
            return "в панели нет объявлений"
        item_id = str(ids[0])

        session = _make_panel_requests_session(creds["cookies"])
        hdrs = _panel_xsrf_headers(session, creds["cookies"])
        r = session.get(f"{PANEL_URL}/nova-api/items/actions",
                        params={"resources": item_id}, headers=hdrs,
                        timeout=(6, 12), allow_redirects=False)
        if r.status_code != 200:
            return f"actions → {r.status_code}: {r.text[:200]}"
        actions = [a for a in ((r.json() or {}).get("actions") or [])
                   if isinstance(a, dict)]
        a = _find_promo_action(actions)
        if not a:
            return f"действие не найдено среди {[x.get('name') for x in actions]}"

        out = [f"объявление #{item_id}", f"action: {a.get('uriKey')}"]
        fields = [f for f in (a.get("fields") or []) if isinstance(f, dict)]
        for f in fields:
            out.append("\n— " + str(f.get("attribute")) + " —")
            out.append(_json.dumps(f, ensure_ascii=False)[:1400])

        # A field with no options is dependent: show what asking the panel to
        # resolve it actually returns, so the route can be seen rather than
        # guessed at.
        from automation.panel import panel_action_field_options_sync

        picked = {}
        for f in fields:
            if _normalize_options(f) and f.get("value") not in (None, ""):
                picked[f.get("attribute")] = f.get("value")
        for f in fields:
            if _normalize_options(f):
                continue
            opts, trace = panel_action_field_options_sync(
                creds["cookies"], item_id, a.get("uriKey") or "",
                f.get("attribute") or "", picked,
                f.get("dependentComponentKey") or "", f.get("dependsOn") or {})
            out.append(f"\n— синхронизация {f.get('attribute')} —")
            out.append(f"значения: {picked}")
            out.append(f"варианты: {opts or 'нет'}")
            out.append(f"лог: {trace}")
        return "\n".join(out)

    status = await message.answer("⏳ Читаю действие «Премиум»...")
    try:
        loop = asyncio.get_event_loop()
        report = await asyncio.wait_for(loop.run_in_executor(None, _fetch),
                                        timeout=90)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"

    # One field's JSON alone can exceed a Telegram message, so send in pieces
    text = _html.escape(report)
    for i in range(0, min(len(text), 12000), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()


def _pos_raw_sync(url: str, shop: str) -> str:
    """Blocking: report where a storefront page keeps its offers.

    Read-only and unauthenticated. Answers the one question that cannot be
    answered from the outside: is the listing inside the HTML at all? If the
    shop name is in the bytes, the data is there and the parser is at fault; if
    it is not, the page fills itself in later and the HTML is the wrong thing
    to read no matter how the parser is written.
    """
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning

    from automation.market import (MARKET_URL, _json_blobs, _normalize,
                                   _offer_lists, _offers_from_text, _offers_in,
                                   _seller_of, _visible_text, find_position,
                                   get_page, with_page)

    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    if not url.startswith("http"):
        url = MARKET_URL + ("" if url.startswith("/") else "/") + url
    try:
        r = requests.get(url, timeout=(6, 25), verify=False, headers={
            "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
    except Exception as e:
        return f"запрос не прошёл: {str(e)[:200]}"
    html = r.text
    out = [f"HTTP {r.status_code}, {len(html)}б, "
           f"тип: {r.headers.get('content-type', '?')[:40]}"]

    # 1. Where a Next.js page could be keeping it
    nd = re.search(r'id="__NEXT_DATA__"', html)
    chunks = re.findall(r'__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
    out.append(f"__NEXT_DATA__: {'есть' if nd else 'нет'}; "
               f"RSC-чанков: {len(chunks)}, в них {sum(len(c) for c in chunks)}б")
    blobs = _json_blobs(html)
    lists = []
    for b in blobs:
        lists.extend(_offer_lists(b))
    out.append(f"распознано JSON-блоков: {len(blobs)}, "
               f"списков-офферов: {len(lists)}"
               + (f", длиннейший: {max(len(x) for x in lists)}" if lists else ""))
    out.append(f"карточек по разметке: {len(_offers_from_text(html))}")

    # 2. Is the listing in the bytes at all?
    text = _visible_text(html)
    marks = [m for m in (shop, "отзыв", "₽") if m]
    found = {m: (m in html) for m in marks}
    out.append("в HTML: " + ", ".join(f"{k}={'да' if v else 'НЕТ'}"
                                      for k, v in found.items()))
    out.append(f"видимого текста: {len(text.strip())}б")

    # 3. How much of the list came in one response, and where we are in it.
    # The seller had to scroll to reach their own card, so the question is not
    # only "did it parse" but "did the whole list arrive" — a listing that
    # loads in batches would put them past the end of what we can see.
    rows = _normalize(_offers_from_text(html)) if not lists else \
        _normalize(max(lists, key=len))
    if rows:
        mine = find_position(rows, seller=shop) if shop else None
        out.append(f"разобрано карточек: {len(rows)}; "
                   f"наше место: {mine['pos'] if mine else 'НЕ НАЙДЕНО'}")
        out.append("продавцы: " + ", ".join(r["seller"][:14] or "?"
                                            for r in rows[:12])
                   + (" …" if len(rows) > 12 else ""))
    lazy = sorted(set(re.findall(
        r'(?i)(page=\d+|[?&]offset=|показать ещё|показать еще|load[_-]?more'
        r'|infinite|IntersectionObserver|useInfiniteQuery|hasNextPage)', html)))
    out.append("признаки подгрузки порциями: "
               + (", ".join(lazy[:8]) if lazy else "не вижу"))

    # Does ?page=2 actually give a different listing? If it comes back
    # identical, positions past the first screen have to be reached some other
    # way, and no amount of parsing the first page would tell us that.
    ok2, html2 = get_page(with_page(url, 2))
    if ok2:
        rows2, _ = (_offers_in(html2) if lists else
                    (_offers_from_text(html2), ""))
        first_of = lambda rr: ", ".join(  # noqa: E731
            (_seller_of(x) if isinstance(x, dict) else "")[:12] for x in rr[:4])
        out.append(f"страница 2: {len(html2)}б, карточек: {len(rows2)}, "
                   f"начало: {first_of(rows2) or '—'}")
        out.append("  → " + ("та же страница, page= не работает"
                             if first_of(rows2) == first_of(rows)
                             else "другая — постраничное чтение работает"))
    else:
        out.append(f"страница 2: {html2[:80]}")

    # 4. What the payload around our shop looks like — the shape to parse
    if shop and shop in html:
        i = html.index(shop)
        out.append("")
        out.append(f"--- окрестности «{shop}» ---")
        out.append(html[max(0, i - 300):i + 300])

    # 5. A JSON endpoint would beat parsing any markup
    apis = sorted(set(re.findall(
        r'["\'](/api/[\w/\-.]+|https?://[\w.\-]*(?:api|graphql)[\w/\-.]*)["\']',
        html)))
    if apis:
        out.append("")
        out.append("адреса API на странице: " + ", ".join(apis[:12]))
    return "\n".join(out)


@router.message(Command("pos_raw"))
async def pos_raw(message: Message) -> None:
    """/pos_raw <ссылка> — показать, где на странице витрины лежат предложения.

    Read-only. Существует потому, что страницу нельзя посмотреть из среды
    разработки — вывод этой команды заменяет взгляд на реальную разметку.
    """
    import html as _html

    from storage import get_shop_name

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите страницу витрины:\n"
            "<code>/pos_raw https://yoomarket.net/categories/...</code>")
        return
    shop = get_shop_name(message.from_user.id) or ""
    status = await message.answer("⏳ Смотрю, что отдаёт страница...")
    loop = asyncio.get_event_loop()
    try:
        report = await asyncio.wait_for(
            loop.run_in_executor(None, _pos_raw_sync, parts[1].strip(), shop),
            timeout=90)
    except Exception as e:
        report = f"не удалось: {str(e)[:200]}"
    text = _html.escape(report)
    for i in range(0, min(len(text), 10500), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()


def _pos_api_sync(url: str, shop: str) -> str:
    """Blocking: find the request the storefront makes to fill its listing.

    The category page ships no offers at all — 81 KB of application shell — so
    the list arrives over a separate call made from the browser. That call has
    to be found, and it is findable: its address is compiled into the page's own
    JavaScript. The same trick already located the chat API
    (panel_chat_debug_sync).

    Read-only: every request here is a GET, and nothing is stored.
    """
    import json as _json
    from urllib.parse import parse_qsl, urlparse

    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning

    from automation.market import MARKET_URL, _json_blobs, _offer_lists

    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    if not url.startswith("http"):
        url = MARKET_URL + ("" if url.startswith("/") else "/") + url
    parts = urlparse(url)
    slugs = [p for p in parts.path.split("/") if p and p != "categories"]
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    hdrs = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": url, "Origin": MARKET_URL,
            "Accept-Language": "ru-RU,ru;q=0.9"}
    out = [f"путь: {slugs}", f"параметры: {params}"]

    try:
        html = requests.get(url, timeout=(6, 25), verify=False, headers={
            "User-Agent": hdrs["User-Agent"],
            "Accept": "text/html"}).text
    except Exception as e:
        return f"страница не открылась: {str(e)[:150]}"

    # --- 1. What the page itself was given -----------------------------------
    build_id = ""
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            nd = _json.loads(m.group(1))
            build_id = str(nd.get("buildId") or "")
            props = (nd.get("props") or {}).get("pageProps") or {}
            out.append(f"buildId: {build_id or '—'}; "
                       f"pageProps: {sorted(props)[:12]}")
            # Anything in there shaped like an address or an id we could reuse
            flat = _json.dumps(nd, ensure_ascii=False)
            urls = sorted(set(re.findall(
                r'"(https?://[\w.\-]+[^"]{0,40})"', flat)))
            if urls:
                out.append("адреса в __NEXT_DATA__: " + ", ".join(urls[:8]))
            ids = re.findall(r'"(category_?id|categoryId|id)":\s*(\d+)', flat)
            if ids:
                out.append(f"идентификаторы: {ids[:6]}")
        except Exception as e:
            out.append(f"__NEXT_DATA__ не разобрался: {str(e)[:60]}")

    # --- 2. The endpoint compiled into the page's JavaScript ------------------
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    bases, paths = set(), set()
    for src in list(dict.fromkeys(scripts))[:12]:
        full = src if src.startswith("http") else MARKET_URL + src
        try:
            js = requests.get(full, timeout=(6, 20), verify=False,
                              headers={"User-Agent": hdrs["User-Agent"]}).text
        except Exception:
            continue
        for b in re.findall(r'["\'`](https?://[\w.\-]*api[\w.\-]*)["\'`/]', js):
            bases.add(b)
        for p in re.findall(
                r'["\'`](/(?:api|v\d)/[\w/\-{}$.]{2,60})["\'`]', js):
            paths.add(p)
        for p in re.findall(
                r'["\'`](/[\w/\-]*(?:categor|offer|product|search|catalog'
                r'|item|lot|filter)[\w/\-{}$.]{0,40})["\'`]', js, re.I):
            if len(p) < 70:
                paths.add(p)
        if "graphql" in js.lower():
            bases.add("(в бандле есть graphql)")
    out.append(f"базы из JS: {sorted(bases)[:10] or 'ничего'}")
    out.append(f"пути из JS: {sorted(paths)[:24] or 'ничего'}")

    # --- 3. Try them, and say which one actually answers with the listing -----
    def _try(full_url: str, label: str = "") -> str:
        try:
            r = requests.get(full_url, headers=hdrs, timeout=(6, 20),
                             verify=False, allow_redirects=False)
        except Exception as e:
            return f"  {full_url[:90]} → {str(e)[:50]}"
        body = r.text or ""
        mark = ""
        if shop and shop in body:
            mark = "  ★★★ ЕСТЬ НАШ МАГАЗИН"
        elif "отзыв" in body or '"price"' in body:
            mark = "  ★ похоже на выдачу"
        offers = 0
        try:
            for blob in ([r.json()] if "json" in
                         r.headers.get("content-type", "") else
                         _json_blobs(body)):
                for lst in _offer_lists(blob):
                    offers = max(offers, len(lst))
        except Exception:
            pass
        if offers:
            mark += f"  [офферов: {offers}]"
        return (f"  {label or full_url[:90]} → {r.status_code}, "
                f"{len(body)}б{mark}\n     {body[:160].strip()}")

    out.append("\n— пробую —")
    tried = set()

    # Next.js Pages Router serves the page's own props as JSON
    if build_id and len(out) < 60:
        path = parts.path.strip("/")
        nd_url = f"{MARKET_URL}/_next/data/{build_id}/{path}.json"
        if parts.query:
            nd_url += "?" + parts.query
        out.append(_try(nd_url, f"/_next/data/{build_id}/…"))

    # Whatever the bundles named, with this page's own parameters
    query = "&".join(f"{k}={v}" for k, v in params.items())
    for base in list(sorted(bases))[:3] + [MARKET_URL, "https://api.yoo.market"]:
        if not base.startswith("http"):
            continue
        for p in sorted(paths)[:12]:
            if "{" in p or "$" in p:
                continue
            full = base.rstrip("/") + p
            if slugs and p.rstrip("/").endswith(("categories", "category")):
                full += "/" + "/".join(slugs)
            full += ("&" if "?" in full else "?") + query if query else ""
            if full in tried or len(tried) > 22:
                continue
            tried.add(full)
            out.append(_try(full))

    # And the shapes such an API usually takes, in case the bundles hid theirs
    if slugs:
        guesses = [
            f"/api/categories/{'/'.join(slugs)}/offers",
            f"/api/categories/{'/'.join(slugs)}/products",
            f"/api/categories/{'/'.join(slugs)}",
            f"/api/offers?category={slugs[-1]}",
            f"/api/products?category={slugs[-1]}",
            f"/api/search?category={slugs[-1]}",
        ]
        for base in (MARKET_URL, "https://api.yoo.market",
                     "https://api.yoo.market/v1"):
            for g in guesses:
                full = base + g
                full += ("&" if "?" in full else "?") + query if query else ""
                if full in tried or len(tried) > 40:
                    continue
                tried.add(full)
                line = _try(full)
                if "→ 404" not in line and "→ 403" not in line:
                    out.append(line)
    return "\n".join(out)


@router.message(Command("pos_api"))
async def pos_api(message: Message) -> None:
    """/pos_api <ссылка> — найти запрос, которым витрина набирает список.

    Read-only. Нужна потому, что страница категории не содержит предложений:
    их подгружает браузер отдельным запросом, и его адрес зашит в JS страницы.
    """
    import html as _html

    from storage import get_shop_name

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажите страницу витрины:\n"
            "<code>/pos_api https://yoomarket.net/categories/...</code>")
        return
    shop = get_shop_name(message.from_user.id) or ""
    status = await message.answer("⏳ Ищу, откуда витрина берёт список...")
    loop = asyncio.get_event_loop()
    try:
        report = await asyncio.wait_for(
            loop.run_in_executor(None, _pos_api_sync, parts[1].strip(), shop),
            timeout=240)
    except Exception as e:
        report = f"не удалось: {str(e)[:200]}"
    text = _html.escape(report)
    for i in range(0, min(len(text), 14000), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()


async def _pos_debug_watches(message: Message) -> None:
    """Dry-run every watch: position, thresholds and the decision — no charge.

    `evaluate` is called on a throwaway copy of each watch so a diagnostic run
    cannot move the real state (an alert marked as delivered, a cooldown
    started) and make the next scheduled run behave differently.
    """
    import copy
    import html as _html
    import time as _time

    from automation.market import fetch_listing
    from automation.position import evaluate, is_due, watches
    from storage import get_settings, get_shop_name

    s = get_settings(message.from_user.id)
    pp = s.setdefault("promo_position", {})
    ws = watches(pp)
    if not ws:
        await message.answer(
            "Наблюдений нет. Добавьте товар в «Объявления» → «Премиум "
            "продвижение» → «По позиции», либо укажите адрес:\n"
            "<code>/pos_debug https://yoomarket.net/...</code>")
        return

    shop = get_shop_name(message.from_user.id) or ""
    status = await message.answer(f"⏳ Проверяю {len(ws)} наблюдений...")
    now = _time.time()
    out = [f"магазин: {shop or '—'}",
           f"режим: {'поднимать' if pp.get('auto_promote') else 'только сигнал'}",
           f"интервал: {pp.get('interval_hours', 1)} ч, "
           f"пауза: {pp.get('cooldown_hours', 6)} ч, "
           f"лимит/сутки: {pp.get('daily_limit', 3)}", ""]
    loop = asyncio.get_event_loop()
    for i, w in enumerate(ws, 1):
        out.append(f"{i}. {(w.get('title') or w.get('url'))[:46]}")
        out.append(f"   порог места: {w.get('max_position')}, "
                   f"порог цены: {w.get('undercut_guard') or '—'}, "
                   f"сигнал цены: {w.get('min_price') or '—'}")
        out.append(f"   товар в панели: {w.get('item_id') or 'НЕ ПРИВЯЗАН'}, "
                   f"по расписанию: {'да' if is_due(w, pp, now) else 'ещё рано'}")
        try:
            ok, res = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_listing, w.get("url", ""),
                                     shop, w.get("category_id")),
                timeout=120)
        except Exception as e:
            ok, res = False, str(e)[:120]
        if not ok:
            out.append(f"   ❌ {res}")
            out.append("")
            continue
        probe = copy.deepcopy(w)
        v = evaluate(probe, res["offers"], shop=shop, pp=pp, now=now)
        out.append(f"   предложений: {len(res['offers'])}, "
                   f"наше место: {v.pos if v.found else 'не нашёл'}, "
                   f"дешевле всех: {v.cheapest}")
        out.append(f"   решение: {'ПОДНЯЛ БЫ' if v.promote else 'не поднимать'}"
                   f" — {v.reason}")
        for line in v.lines:
            out.append(f"   сказал бы: {re.sub(r'<[^>]+>', '', line)[:90]}")
        out.append("")
    text = _html.escape("\n".join(out))
    for j in range(0, min(len(text), 7000), 3500):
        await message.answer(f"<code>{text[j:j + 3500]}</code>")
    await status.delete()


@router.message(Command("pos_debug"))
async def pos_debug(message: Message) -> None:
    """/pos_debug <ссылка> — прочитать список предложений и наши позиции.

    Read-only. Position is not in the seller API — it exists only on the public
    storefront — so the page the buyer sees is parsed and reported here before
    anything is wired to it.
    """
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        # Without an address: replay every configured watch and show what the
        # trigger would decide right now. Nothing is charged — this is the
        # answer to "почему не поднял" without spending to find out.
        await _pos_debug_watches(message)
        return
    url = parts[1].strip()

    import html as _html

    from automation.market import cheapest, fetch_listing, find_position
    from storage import get_shop_name

    shop = get_shop_name(message.from_user.id) or ""
    status = await message.answer("⏳ Читаю список предложений...")
    try:
        loop = asyncio.get_event_loop()
        ok, res = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_listing, url, shop), timeout=120)
    except Exception as e:
        ok, res = False, str(e)[:200]
    if not ok:
        await status.edit_text(f"❌ {_html.escape(str(res))[:600]}")
        return

    offers = res["offers"]
    mine = find_position(offers, seller=shop) if shop else None
    lines = [f"страница: {res['note']}", f"предложений: {len(offers)}",
             f"магазин в боте: {shop or '—'}",
             f"дешевле всех: {cheapest(offers)}", ""]
    for row in offers[:15]:
        mark = "👉" if mine and row["pos"] == mine["pos"] else "  "
        lines.append(f"{mark}{row['pos']:>2}. {row['price']} ₽ | "
                     f"{row['seller'][:18]} | {row['title'][:34]}")
    lines.append("")
    lines.append(f"наша позиция: {mine['pos'] if mine else 'не нашёл по магазину'}")
    text = _html.escape("\n".join(lines))
    for i in range(0, min(len(text), 7000), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()


@router.message(Command("chat_debug"))
async def chat_debug(message: Message) -> None:
    """/chat_debug 1076867 — find how the panel sends a support message.

    Read-only: sends nothing. Reveals the token, the reachable endpoints and
    the send route, so replying to support can be wired to the real request.
    """
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер чата: <code>/chat_debug 1076867</code>")
        return
    chat_id = parts[1].strip().rstrip("/").split("/")[-1]
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return
    import html as _html
    from automation.panel import panel_chat_probe_sync
    from storage import get_token
    api_token = get_token(message.from_user.id) or ""
    status = await message.answer("⏳ Разбираю, как панель шлёт сообщения...")
    try:
        loop = asyncio.get_event_loop()
        _ok, report = await asyncio.wait_for(
            loop.run_in_executor(None, panel_chat_probe_sync,
                                 creds["cookies"], chat_id, api_token),
            timeout=90)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    text = _html.escape(str(report))
    for i in range(0, min(len(text), 10000), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()


@router.message(Command("withdraw_debug"))
async def withdraw_debug(message: Message) -> None:
    """Reveal what the panel exposes for withdrawal — a read-only probe.

    The Integration API has no withdrawal endpoint, so a payout has to go
    through the panel, and its exact shape is unknown. This shows the finance
    resources and the create-withdrawal form so the real request is captured
    instead of guessed. It moves no money.
    """
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет сессии панели — войдите в «Панель продавца»")
        return

    import html as _html

    from automation.panel import panel_finance_probe_sync

    status = await message.answer("⏳ Ищу, где в панели вывод средств...")
    try:
        loop = asyncio.get_event_loop()
        ok, report = await asyncio.wait_for(
            loop.run_in_executor(None, panel_finance_probe_sync, creds["cookies"]),
            timeout=90)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"

    text = _html.escape(str(report))
    for i in range(0, min(len(text), 12000), 3500):
        await message.answer(f"<code>{text[i:i + 3500]}</code>")
    await status.delete()
