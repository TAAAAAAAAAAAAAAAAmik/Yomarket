from __future__ import annotations

import csv
import io
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from api.yoomarket import YooMarketAPI
import localtime as _lt
from storage import get_settings
from handlers.balance import _parse_check, _bump_spent, _RUB
from stats_source import (LOCAL, PANEL, day_start, events_for, invalidate,
                          source_note, summarize)

router = Router()
logger = logging.getLogger(__name__)

from orderfields import DONE as COMPLETED_STATUSES


async def _events(callback_or_uid, force: bool = False) -> tuple[list, str, str]:
    uid = (callback_or_uid if isinstance(callback_or_uid, int)
           else callback_or_uid.from_user.id)
    return await events_for(uid, force=force)


def _esc(value) -> str:
    """Titles come from the marketplace; a stray '<' would make Telegram reject
    the whole message and leave the «загружаю» text on screen."""
    import html
    return html.escape(str(value), quote=False)


def _sales(events: list, since: float = 0.0) -> list:
    """Продажи за отрезок времени, от свежих к старым."""
    out = [e for e in events if e.get("kind") == "sale" and float(e.get("ts") or 0) >= since]
    out.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
    return out


def _back_to_stats_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Статистика", callback_data="menu:stats")
    builder.adjust(1)
    return builder.as_markup()


def _stats_kb() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Выручка", callback_data="stats:revenue:7")
    builder.button(text="💳 Операции", callback_data="stats:ops")
    builder.button(text="📈 График (7 дней)", callback_data="stats:chart")
    builder.button(text="🏆 Топ товаров", callback_data="stats:top")
    builder.button(text="⬆️ Расходы на поднятия", callback_data="stats:bumpspend")
    builder.button(text="📊 По часам", callback_data="stats:hours")
    builder.button(text="👥 Повторные покупатели", callback_data="stats:buyers")
    builder.button(text="⭐ Отзывы", callback_data="stats:reviews")
    builder.button(text="📤 Экспорт заказов", callback_data="stats:export")
    builder.button(text="📊 Экспорт CSV", callback_data="stats:export_csv")
    builder.button(text="🔄 Обновить", callback_data="stats:reload")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(2, 2, 2, 2, 2, 2)
    return builder


@router.callback_query(F.data == "stats:reload")
async def reload_stats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """«Обновить» значит обновить: сбрасываем запомненный журнал и перерисовываем.

        Кнопка, перерисовывающая те же запомненные цифры, — обман: продавец
        жмёт её именно потому, что не верит показанному.
        """
    invalidate(callback.from_user.id)
    await show_stats(callback, api)


@router.callback_query(F.data == "menu:stats")
async def show_stats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю статистику...")

    uid = callback.from_user.id
    settings = get_settings(uid)
    events, source, panel_err = await _events(uid)

    now = time.time()
    d0 = day_start(now, settings)
    today = summarize(events, d0)
    week = summarize(events, now - 7 * 86400)
    month = summarize(events, now - 30 * 86400)
    total = summarize(events)

    # Списания за продвижение оценены только в журнале панели. Когда цифры
    # берутся из истории самого бота, тратой считается то, что он записал сам.
    d_spend = today["spend"] or (_bump_spent(settings, d0) if source == LOCAL else 0.0)
    d_net = today["revenue"] - d_spend

    shop_name = get_settings(uid).get("shop_name") or "—"
    balance_str = "—"
    ads_total = "—"
    if api:
        try:
            check_data = await api.check()
            name, bal, _ = _parse_check(check_data)
            shop_name = name or shop_name
            if bal:
                balance_str = f"{bal} ₽"
        except Exception as e:
            logger.warning("Stats check error: %s", e)
        try:
            ads_data = await api.get_ads()
            meta = ads_data.get("meta", {})
            ads_list = ads_data.get("data") or ads_data.get("items") or []
            ads_total = str(
                meta.get("total") or meta.get("count") or meta.get("total_count") or len(ads_list)
            )
        except Exception as e:
            logger.warning("Stats ads error: %s", e)

    # В /check денег нет вовсе — баланс живёт в панели.
    if balance_str == "—":
        from handlers.balance import _panel_balance
        panel_bal, _ = await _panel_balance(uid)
        if panel_bal:
            balance_str = f"{panel_bal} ₽"

    money_line = f"💵 Сегодня: <b>{_RUB(today['revenue'])} ₽</b> ({today['sales']})"
    if d_spend:
        money_line += f"  ·  чистыми {_RUB(d_net)} ₽"

    lines = [
        f"📊 <b>Статистика</b>",
        f"🏪 {_esc(shop_name)}",
        "",
        f"💰 Баланс: <b>{balance_str}</b>",
        f"🚀 Объявлений: <b>{ads_total}</b>",
        "",
        money_line,
        f"📆 7 дней: <b>{_RUB(week['revenue'])} ₽</b> ({week['sales']})",
        f"🗓 30 дней: <b>{_RUB(month['revenue'])} ₽</b> ({month['sales']})",
        "",
        "🛒 <b>За всё время</b>",
        f"├ Продаж: <b>{total['sales']}</b> на <b>{_RUB(total['revenue'])} ₽</b>",
    ]
    if total["spend"]:
        lines.append(f"├ ⬆️ Продвижение: <b>−{_RUB(total['spend'])} ₽</b>")
    if total["payout_count"]:
        lines.append(f"├ 💸 Выведено: <b>{_RUB(total['payouts'])} ₽</b> "
                     f"({total['payout_count']})")
    if total["refunds"]:
        lines.append(f"├ ↩️ Возвраты: <b>{total['refunds']}</b>")
    lines.append(f"└ 🟰 Чистыми: <b>{_RUB(total['revenue'] - total['spend'])} ₽</b>")
    lines.append("")
    lines.append(source_note(source, panel_err))

    await callback.message.edit_text("\n".join(lines),
                                     reply_markup=_stats_kb().as_markup())
    await callback.answer()


@router.callback_query(F.data == "stats:ops")
async def show_operations(callback: CallbackQuery) -> None:
    """Журнал панели как есть — то сырьё, из которого посчитаны итоги."""
    await callback.answer()
    await callback.message.edit_text("⏳ Читаю операции из панели...")
    events, source, err = await _events(callback.from_user.id)

    icons = {"sale": "🛒", "payout": "💸", "bump": "⬆️", "refund": "↩️",
             "in": "➕", "out": "➖", "other": "•", "order": "🧾"}
    s = get_settings(callback.from_user.id)
    rows = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)[:20]

    lines = ["💳 <b>Последние операции</b>", ""]
    if not rows:
        lines.append("Операций нет." if source == PANEL
                     else "Панель не отдала операции, а локальная история пуста.")
    for e in rows:
        when = _lt.fmt(float(e["ts"]), s) \
            if e.get("ts") else "—"
        kind = str(e.get("kind") or "other")
        sign = "−" if kind in ("payout", "bump", "out") else ""
        amount = e.get("amount")
        money = f"{sign}{_RUB(amount)} ₽" if amount is not None else "—"
        label = _esc(str(e.get("title") or e.get("type") or "").strip()[:32] or kind)
        lines.append(f"{icons.get(kind, '•')} <code>{when}</code> "
                     f"<b>{money}</b> — {label}")
    lines.append("")
    lines.append(source_note(source, err))

    await callback.message.edit_text("\n".join(lines),
                                     reply_markup=_back_to_stats_keyboard())


_PERIODS = {1: "сегодня", 7: "7 дней", 30: "30 дней", 0: "всё время"}


@router.callback_query(F.data.startswith("stats:revenue:"))
async def show_revenue(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        days = 7

    settings = get_settings(callback.from_user.id)
    events, source, err = await _events(callback.from_user.id)

    now = time.time()
    # «Сегодня» means since midnight, not «за последние 24 часа» — a seller
    # сверяя цифру с панелью, продавец считает календарные сутки.
    since = day_start(now, settings) if days == 1 else (now - days * 86400 if days else 0.0)
    agg = summarize(events, since)
    count, revenue = agg["sales"], agg["revenue"]

    by_title: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for e in _sales(events, since):
        title = str(e.get("title") or "—")[:35]
        by_title[title][0] += 1
        by_title[title][1] += float(e.get("amount") or 0)

    # Траты на продвижение за тот же отрезок — чтобы выручка и цена её
    # добычи стояли рядом. Журнал панели оценивает каждое списание и режется
    # по времени точно; собственный учёт бота точно знает только сегодняшний.
    spent = agg["spend"]
    spent_exact = True
    if not spent and source == LOCAL:
        bs = settings.get("bump_schedule", {})
        if days == 1 and bs.get("spent_day") == _lt.today_str(settings):
            spent = float(bs.get("spent_today", 0) or 0)
        else:
            spent = float(bs.get("spent_total", 0) or 0)
            spent_exact = (days == 0)

    period = _PERIODS.get(days, f"{days} дней")
    lines = [f"💰 <b>Выручка — {period}</b>\n"]
    lines.append(f"📦 Продаж: <b>{count}</b>")
    lines.append(f"💵 Выручка: <b>{_RUB(revenue)} ₽</b>")
    if count:
        lines.append(f"🧾 Средний чек: <b>{_RUB(revenue / count)} ₽</b>")
    if spent:
        tag = "" if spent_exact else " <i>(всего)</i>"
        lines.append(f"⬆️ Продвижение: <b>−{_RUB(spent)} ₽</b>{tag}")
        lines.append(f"🟰 Чистыми: <b>{_RUB(revenue - spent)} ₽</b>")
    if agg["payout_count"]:
        lines.append(f"💸 Выведено: <b>{_RUB(agg['payouts'])} ₽</b> "
                     f"({agg['payout_count']})")
    if count:
        top = sorted(by_title.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        lines.append("\n🏆 <b>Топ по выручке:</b>")
        for i, (title, (cnt, total)) in enumerate(top, 1):
            lines.append(f"{i}. {_esc(title)} — {int(cnt)} шт., <b>{_RUB(total)} ₽</b>")
    else:
        lines.append("\nПродаж за период не было.")
    lines.append("")
    lines.append(source_note(source, err))

    b = InlineKeyboardBuilder()
    for d in (1, 7, 30, 0):
        mark = "• " if d == days else ""
        b.button(text=f"{mark}{_PERIODS[d]}", callback_data=f"stats:revenue:{d}")
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    b.adjust(4, 1)

    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "stats:bumpspend")
async def show_bump_spend(callback: CallbackQuery) -> None:
    await callback.answer()
    bs = get_settings(callback.from_user.id).get("bump_schedule", {})
    spent_total = float(bs.get("spent_total", 0) or 0)
    spent_today = float(bs.get("spent_today", 0) or 0)
    bumps_total = int(bs.get("bumps_total", 0) or 0)
    ppb = float(bs.get("price_per_bump", 0) or 0)
    ceiling = float(bs.get("daily_ceiling", 0) or 0)
    lines = [
        "⬆️ <b>Расходы на поднятия (премки)</b>\n",
        f"💵 Всего потрачено: <b>{spent_total:.0f} ₽</b>",
        f"📅 Сегодня: <b>{spent_today:.0f} ₽</b>"
        + (f" из {ceiling:.0f} ₽" if ceiling else ""),
        f"🔢 Всего поднятий: <b>{bumps_total}</b>",
        f"🏷 Цена за поднятие: <b>{ppb:g} ₽</b>",
    ]
    if not bs.get("enabled"):
        lines.append("\n<i>Планировщик поднятий выключен.</i>")
    b = InlineKeyboardBuilder()
    b.button(text="⚙️ Настроить поднятия", callback_data="auto:menu")
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    ui.lay(b)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "stats:chart")
async def show_chart(callback: CallbackQuery) -> None:
    await callback.answer()

    events, source, err = await _events(callback.from_user.id)
    s = get_settings(callback.from_user.id)

    today = _lt.now(s).date()
    days_counts: dict[int, int] = defaultdict(int)
    days_revenue: dict[int, float] = defaultdict(float)

    for e in _sales(events, day_start(None, s) - 6 * 86400):
        sold = _lt.to_local(float(e["ts"]), s).date()
        delta = (today - sold).days
        if 0 <= delta < 7:
            idx = 6 - delta
            days_counts[idx] += 1
            days_revenue[idx] += float(e.get("amount") or 0)

    bar_length = 10
    max_rev = max(days_revenue.values(), default=0.0) or 1.0
    lines = []
    for i in range(7):
        day_date = today - timedelta(days=6 - i)
        label = day_date.strftime("%d.%m")
        # Столбцы меряются деньгами, а не числом продаж: два дня по 5 000 ₽ и
        # один на 50 ₽ рисовались одинаковыми — не за этим смотрят на график.
        rev = days_revenue.get(i, 0.0)
        filled = round((rev / max_rev) * bar_length)
        bar = "█" * filled + "░" * (bar_length - filled)
        lines.append(f"{label} {bar} {_RUB(rev):>6} ₽ ({days_counts.get(i, 0)})")

    total_count = sum(days_counts.values())
    total_revenue = sum(days_revenue.values())

    chart_text = "\n".join(lines)
    text = (
        f"📈 <b>График продаж (7 дней)</b>\n\n"
        f"<code>{chart_text}</code>\n\n"
        f"📦 Продаж: <b>{total_count}</b>\n"
        f"💵 Выручка: <b>{_RUB(total_revenue)} ₽</b>\n\n"
        f"{source_note(source, err)}"
    )

    await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())


@router.callback_query(F.data == "stats:top")
async def show_top_products(callback: CallbackQuery) -> None:
    await callback.answer()

    events, source, err = await _events(callback.from_user.id)

    counts: Counter = Counter()
    revenue: dict[str, float] = defaultdict(float)
    for e in _sales(events):
        title = str(e.get("title") or "").strip()
        if not title or title == "—":
            continue
        counts[title] += 1
        revenue[title] += float(e.get("amount") or 0)

    if not counts:
        text = ("🏆 <b>Топ товаров</b>\n\nПродаж пока нет.\n\n"
                + source_note(source, err))
    else:
        lines = []
        for rank, (title, count) in enumerate(counts.most_common(10), start=1):
            lines.append(f"{rank}. {_esc(title[:38])} — <b>{count}</b> шт., "
                         f"{_RUB(revenue[title])} ₽")
        text = ("🏆 <b>Топ товаров (по продажам)</b>\n\n" + "\n".join(lines)
                + "\n\n" + source_note(source, err))

    await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())


_STATUS_MAP = {
    "confirmed": "✅ Выполнен", "completed": "✅ Выполнен", "done": "✅ Выполнен",
    "refunded": "↩️ Возврат", "cancelled": "↩️ Отменён", "returned": "↩️ Возврат",
    "active": "⏳ Активен", "new": "🆕 Новый", "work": "🔧 В работе",
}


@router.callback_query(F.data == "stats:export")
async def export_orders(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("⏳ Генерирую файл...", show_alert=False)

    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    all_orders: list[dict] = []
    if api:
        try:
            data = await api.get_orders()
            all_orders = data.get("data") or data.get("items") or []
        except Exception as e:
            logger.warning("Export: API error: %s", e)

    lines = [
        "YooMarket — Экспорт заказов",
        f"Дата: {_lt.now(s).strftime('%d.%m.%Y %H:%M')}",
        f"Всего заказов: {len(known_orders)}",
        "=" * 40,
        "",
    ]

    if all_orders:
        for order in all_orders:
            oid = str(order.get("id", ""))
            title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
            buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
            price = order.get("price") or order.get("total") or "—"
            status = _STATUS_MAP.get(str(order.get("status", "")), str(order.get("status", "")))
            created = order.get("created_at") or order.get("date") or ""
            lines += [
                f"ID: {oid}", f"Товар: {title}", f"Покупатель: {buyer}",
                f"Сумма: {price} ₽", f"Статус: {status}",
                f"Создан: {str(created)[:19] if created else '—'}", "-" * 30, "",
            ]
    else:
        for oid, status_raw in known_orders.items():
            det = order_details.get(oid, {})
            status = _STATUS_MAP.get(status_raw, status_raw)
            lines += [
                f"ID: {oid}", f"Товар: {det.get('title', '—')}",
                f"Покупатель: {det.get('buyer', '—')}",
                f"Сумма: {det.get('price', '—')} ₽", f"Статус: {status}",
                "-" * 30, "",
            ]

    content = "\n".join(lines).encode("utf-8")
    file = BufferedInputFile(content, filename="orders_export.txt")
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    await callback.message.answer_document(
        file,
        caption=f"📤 <b>Экспорт заказов</b>\nВсего: <b>{len(known_orders)}</b>",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "stats:reviews")
async def reviews_menu(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await _show_review(callback, 0, refresh=True)


# Отзывы читаются из панели одним запросом на несколько страниц, а листает
# продавец по одному. Перечитывать панель на каждое нажатие — секунда
# ожидания на кнопку; держим прочитанное недолго и обновляем по кнопке.
_REVIEWS: dict[int, tuple[float, list, bool, str]] = {}
_REVIEWS_TTL = 300


async def _load_reviews(uid: int, refresh: bool
                        ) -> tuple[list, bool, str]:
    """(отзывы, есть ли ещё непрочитанные, ошибка)."""
    hit = _REVIEWS.get(uid)
    if hit and not refresh and time.time() - hit[0] < _REVIEWS_TTL:
        return hit[1], hit[2], hit[3]

    from storage import get_panel_creds
    from automation.panel import panel_reviews_sync
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return [], False, ("Отзывы бот читает из панели, а вход в неё не "
                           "выполнен.\nНастройки → 🌐 Панель продавца.")
    import asyncio as _aio
    loop = _aio.get_event_loop()
    try:
        ok, got = await _aio.wait_for(
            loop.run_in_executor(None, panel_reviews_sync,
                                 creds["cookies"], "", 6, 50),
            timeout=90)
    except Exception as e:
        return [], False, f"Панель не ответила: {str(e)[:120]}"
    if not ok or not isinstance(got, dict):
        # Что именно не нашлось — видно, а не «отзывов пока нет».
        return [], False, f"Не нашёл отзывы в панели.\n{str(got)[:300]}"
    rows = got.get("reviews") or []
    more = bool(got.get("more"))
    _REVIEWS[uid] = (time.time(), rows, more, "")
    return rows, more, ""


def _stars(rating) -> str:
    if isinstance(rating, (int, float)) and 1 <= rating <= 5:
        full = int(rating)
        return "⭐" * full + "☆" * (5 - full) + f"  {rating:g}/5"
    return "оценки нет"


def _review_kb(i: int, total: int, more: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = 0
    if i > 0:
        b.button(text="⏮ Самый новый", callback_data="stats:rev:0")
        b.button(text="◀️ Новее", callback_data=f"stats:rev:{i - 1}")
        row += 2
    if i + 1 < total:
        b.button(text="Старее ▶️", callback_data=f"stats:rev:{i + 1}")
        row += 1
    b.button(text="🔄 Обновить", callback_data="stats:reviews")
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    b.adjust(*([row] if row else []), 2)
    return b.as_markup()


@router.message(Command("reviews_debug"))
async def reviews_debug(message: Message) -> None:
    """/reviews_debug — какие поля панель кладёт в строку отзыва.

    Отзывы пришли без текста. Имена полей у панели свои, и подбирать их
    списком мы уже пробовали; читаем, что там на самом деле.
    """
    from storage import get_panel_creds
    from automation.panel import panel_review_fields_sync
    import asyncio as _aio

    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Вход в панель не выполнен: "
                             "Настройки → 🌐 Панель продавца")
        return
    status = await message.answer("⏳ Читаю строку отзыва…")
    loop = _aio.get_event_loop()
    try:
        ok, got = await _aio.wait_for(
            loop.run_in_executor(None, panel_review_fields_sync,
                                 creds["cookies"], ""),
            timeout=60)
    except Exception as e:
        await status.edit_text(f"❌ {_esc(str(e)[:200])}")
        return
    if not ok or not isinstance(got, dict):
        await status.edit_text(f"❌ {_esc(str(got)[:400])}")
        return
    lines = [f"🔍 <b>Поля отзыва</b> · ресурс <code>{_esc(got['resource'])}</code>",
             ""]
    for f in got.get("fields") or []:
        lines.append(f"<code>{_esc(f['attribute'])}</code> "
                     f"({f['component']}, {f['len']} симв.)")
        if f["value"]:
            lines.append(f"   {_esc(f['value'])}")
    await status.edit_text("\n".join(lines)[:3800] or "полей нет")


@router.callback_query(F.data.startswith("stats:rev:"))
async def show_review_at(callback: CallbackQuery) -> None:
    try:
        i = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        i = 0
    await _show_review(callback, i)


async def _show_review(callback: CallbackQuery, i: int,
                       refresh: bool = False) -> None:
    """Один отзыв на экран: ник, оценка, текст — и листание.

    Списком по десять они читались как лента: понять, кто и что написал,
    можно было только вглядываясь. Порядок — от самого нового, как их
    отдаёт панель.
    """
    if refresh:
        await callback.message.edit_text("⏳ Загружаю отзывы…")
    rows, more, err = await _load_reviews(callback.from_user.id, refresh)
    if err:
        await callback.message.edit_text(
            f"⭐ <b>Отзывы</b>\n\n{_esc(err)}",
            reply_markup=_review_kb(0, 0, False))
        await callback.answer()
        return
    if not rows:
        await callback.message.edit_text(
            "⭐ <b>Отзывы</b>\n\nОтзывов пока нет.",
            reply_markup=_review_kb(0, 0, False))
        await callback.answer()
        return

    i = max(0, min(i, len(rows) - 1))
    r = rows[i] if isinstance(rows[i], dict) else {}
    # «из N» — это сколько прочитано, а не сколько их в магазине. Если
    # страницы кончились раньше отзывов, так и пишем: иначе счётчик врёт.
    total = f"{len(rows)}+" if more else str(len(rows))
    body = [f"⭐ <b>Отзыв {i + 1} из {total}</b>"]
    if more and i + 1 == len(rows):
        body.append("<i>Более старые не загружены — панель отдаёт их "
                    "постранично.</i>")
    body += ["",
             f"👤 <b>{_esc(r.get('author') or 'Покупатель')}</b>",
             _stars(r.get("rating"))]
    if r.get("title"):
        body.append(f"📦 {_esc(r['title'])}")
    when = r.get("ts")
    if when:
        body.append(f"🕐 {_lt.fmt(float(when), get_settings(callback.from_user.id))}")
    body.append("")
    body.append(_esc(r.get("text") or "").strip() or "<i>без текста</i>")

    await callback.message.edit_text("\n".join(body)[:3900],
                                     reply_markup=_review_kb(i, len(rows), more))
    await callback.answer()


@router.callback_query(F.data == "stats:hours")
async def show_hours_chart(callback: CallbackQuery) -> None:
    await callback.answer()

    events, source, err = await _events(callback.from_user.id)

    s = get_settings(callback.from_user.id)
    hour_counts: dict[int, int] = defaultdict(int)
    # Local time, not UTC: a «пиковый час» three hours off tells the seller to
    # продвигаться не в те часы.
    for e in _sales(events):
        hour_counts[_lt.to_local(float(e["ts"]), s).hour] += 1

    total_count = sum(hour_counts.values())

    if not total_count:
        text = ("📊 <b>Статистика по часам</b>\n\nПродаж пока нет.\n\n"
                + source_note(source, err))
        await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())
        return

    bar_length = 8
    # приводим к самому большому двухчасовому ведру (столбец — сумма за два часа)
    buckets = [hour_counts.get(b * 2, 0) + hour_counts.get(b * 2 + 1, 0)
               for b in range(12)]
    max_count = max(buckets) or 1
    lines = []
    for block in range(12):
        h_start = block * 2
        h_end = h_start + 1
        count = buckets[block]
        filled = max(0, min(bar_length, round((count / max_count) * bar_length)))
        bar = "█" * filled + "░" * (bar_length - filled)
        label = f"{h_start:02d}-{h_end:02d}"
        lines.append(f"{label} {bar} {count}")

    peak_hour = max(hour_counts, key=hour_counts.get)
    chart_text = "\n".join(lines)
    text = (
        f"📊 <b>Статистика по часам</b>\n\n"
        f"<code>{chart_text}</code>\n\n"
        f"📦 Всего продаж: <b>{total_count}</b>\n"
        f"🔝 Пиковый час: <b>{peak_hour:02d}:00–{peak_hour:02d}:59</b> "
        f"({hour_counts[peak_hour]})\n\n"
        f"{source_note(source, err)}"
    )

    await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())


@router.callback_query(F.data == "stats:buyers")
async def show_repeat_buyers(callback: CallbackQuery) -> None:
    await callback.answer()

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})
    known_order_details: dict = settings.get("known_order_details", {})

    buyer_counts: Counter = Counter()
    buyer_revenue: dict[str, int] = defaultdict(int)

    for order_id, status in known_orders.items():
        if status not in COMPLETED_STATUSES:
            continue
        details = known_order_details.get(str(order_id)) or known_order_details.get(order_id)
        if not details:
            continue
        buyer = details.get("buyer")
        if not buyer:
            continue
        buyer = str(buyer)
        buyer_counts[buyer] += 1
        try:
            price = int(float(str(details.get("price", 0))))
        except (ValueError, TypeError):
            price = 0
        buyer_revenue[buyer] += price

    # Покупатели — единственная цифра, которой в денежном журнале панели нет:
    # он записывает суммы, а не то, кто платил. Поэтому экран считает по
    # собственной истории заказов и прямо об этом говорит.
    if not buyer_counts:
        text = ("👥 <b>Повторные покупатели</b>\n\nПока нет данных.\n\n"
                "<i>Покупателей знает только сам бот — считаются заказы, "
                "пришедшие с момента его запуска.</i>")
        await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())
        return

    top = buyer_counts.most_common(10)
    lines = [f"👥 <b>Топ покупателей</b> (по кол-ву заказов)\n"]
    for rank, (buyer, count) in enumerate(top, start=1):
        revenue = buyer_revenue[buyer]
        plural = "заказов" if count % 10 in (0, 5, 6, 7, 8, 9) or 11 <= count % 100 <= 19 else ("заказ" if count % 10 == 1 else "заказа")
        lines.append(f"{rank}. {buyer} — <b>{count} {plural}</b>, {revenue} ₽")

    text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())


_KIND_RU = {"sale": "Продажа", "payout": "Вывод", "bump": "Продвижение",
            "refund": "Возврат", "order": "Заказ", "in": "Поступление",
            "out": "Списание", "other": "Прочее"}


@router.callback_query(F.data == "stats:export_csv")
async def export_orders_csv(callback: CallbackQuery) -> None:
    """Тот же журнал, по которому построены экраны, — таблицей.

    Раньше выгрузка читала собственную историю заказов бота, а цифры на
    экране приходили из панели: файл и экран расходились между собой.
    Теперь у обоих один источник.
    """
    await callback.answer("⏳ Генерирую CSV...", show_alert=False)

    events, source, _err = await _events(callback.from_user.id)
    s = get_settings(callback.from_user.id)
    rows = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)

    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, delimiter=";")     # Excel-RU splits on ';'
    writer.writerow(["Дата", "Тип", "Товар", "Покупатель", "Сумма ₽", "Статус"])

    for e in rows:
        ts = float(e.get("ts") or 0)
        date_str = _lt.fmt(ts, s, "%d.%m.%Y %H:%M") or "—"
        kind = str(e.get("kind") or "other")
        amount = e.get("amount")
        signed = -amount if (amount is not None and kind in
                             ("payout", "bump", "out")) else amount
        status = e.get("status") or ""
        writer.writerow([
            date_str, _KIND_RU.get(kind, kind),
            e.get("title") or e.get("type") or "",
            e.get("buyer") or "",
            "" if signed is None else f"{signed:.2f}".replace(".", ","),
            _STATUS_MAP.get(str(status), str(status)),
        ])

    content = output.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(content, filename="yoomarket_stats.csv")
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    where = "панель" if source == PANEL else "история бота"
    await callback.message.answer_document(
        file,
        caption=f"📊 <b>Экспорт CSV</b>\nСтрок: <b>{len(rows)}</b> · источник: {where}",
        reply_markup=b.as_markup(),
    )


@router.message(Command("stats_debug"))
async def stats_debug(message: Message) -> None:
    """Что на самом деле лежит в журнале панели — только чтение, ничего не меняет.

    Каждый настоящий ответ в этом проекте получался тем, что печатался
    ответ сервера, а не догадка о нём. Если итог выглядит неверным, здесь
    видно, из каких строк он сложен и как прочитана каждая.
    """
    import asyncio as _a
    import html as _html
    from storage import get_panel_creds

    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет входа в панель — «Настройки» → «Панель продавца»")
        return

    status = await message.answer("⏳ Смотрю, что панель считает операциями…")
    from automation.panel import panel_stats_probe_sync
    try:
        report = await _a.wait_for(
            _a.get_event_loop().run_in_executor(
                None, panel_stats_probe_sync, creds["cookies"]),
            timeout=120)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"

    invalidate(message.from_user.id)
    events, source, err = await _events(message.from_user.id, force=True)
    now = time.time()
    today = summarize(events, day_start(now, s))
    week = summarize(events, now - 7 * 86400)
    tail = (f"\n\nсобытий: {len(events)} (источник {source}{', ' + err if err else ''})"
            f"\nсегодня: {today['sales']} продаж / {today['revenue']:.0f} ₽"
            f"\n7 дней: {week['sales']} продаж / {week['revenue']:.0f} ₽")

    await status.edit_text("📊 <b>Статистика: что отдаёт панель</b>\n\n"
                           f"<code>{_html.escape(report + tail)[:3500]}</code>")
