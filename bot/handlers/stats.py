from __future__ import annotations

import csv
import io
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings
from handlers.balance import _parse_check, _bump_spent, _RUB
from stats_source import (LOCAL, PANEL, day_start, events_for, invalidate,
                          source_note, summarize)

router = Router()
logger = logging.getLogger(__name__)

COMPLETED_STATUSES = ("confirmed", "completed", "done")


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
    """Sale events in the window, newest first."""
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
    """Refresh means refresh — drop the cached ledger, then redraw."""
    invalidate(callback.from_user.id)
    await show_stats(callback, api)


@router.callback_query(F.data == "menu:stats")
async def show_stats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю статистику...")

    uid = callback.from_user.id
    settings = get_settings(uid)
    events, source, panel_err = await _events(uid)

    now = time.time()
    d0 = day_start(now)
    today = summarize(events, d0)
    week = summarize(events, now - 7 * 86400)
    month = summarize(events, now - 30 * 86400)
    total = summarize(events)

    # Only the panel's ledger prices its own promotion charges; when the numbers
    # come from the bot's own history, the bump spend it tracked is the figure.
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

    # The API's /check carries no money; the panel is where the balance lives.
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
    """The panel's ledger as the panel wrote it — the raw truth behind the totals."""
    await callback.answer()
    await callback.message.edit_text("⏳ Читаю операции из панели...")
    events, source, err = await _events(callback.from_user.id)

    icons = {"sale": "🛒", "payout": "💸", "bump": "⬆️", "refund": "↩️",
             "in": "➕", "out": "➖", "other": "•", "order": "🧾"}
    rows = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)[:20]

    lines = ["💳 <b>Последние операции</b>", ""]
    if not rows:
        lines.append("Операций нет." if source == PANEL
                     else "Панель не отдала операции, а локальная история пуста.")
    for e in rows:
        when = datetime.fromtimestamp(float(e["ts"])).strftime("%d.%m %H:%M") \
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
    # comparing the figure against the panel counts the calendar day.
    since = day_start(now) if days == 1 else (now - days * 86400 if days else 0.0)
    agg = summarize(events, since)
    count, revenue = agg["sales"], agg["revenue"]

    by_title: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for e in _sales(events, since):
        title = str(e.get("title") or "—")[:35]
        by_title[title][0] += 1
        by_title[title][1] += float(e.get("amount") or 0)

    # Promotion spend for the same window, so revenue and the cost of chasing
    # it sit side by side. The panel's ledger prices each charge and can be
    # windowed exactly; the bot's own tracking only knows today's precisely.
    spent = agg["spend"]
    spent_exact = True
    if not spent and source == LOCAL:
        bs = settings.get("bump_schedule", {})
        if days == 1 and bs.get("spent_day") == datetime.now().strftime("%Y-%m-%d"):
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
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "stats:chart")
async def show_chart(callback: CallbackQuery) -> None:
    await callback.answer()

    events, source, err = await _events(callback.from_user.id)

    today = datetime.now().date()
    days_counts: dict[int, int] = defaultdict(int)
    days_revenue: dict[int, float] = defaultdict(float)

    for e in _sales(events, day_start() - 6 * 86400):
        sold = datetime.fromtimestamp(float(e["ts"])).date()
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
        # Bars scale by money, not by count: two 5 000 ₽ days and one 50 ₽ day
        # drew as equal columns, which is not what a sales chart is for.
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
        f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
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
    await callback.message.edit_text("⏳ Загружаю отзывы...")

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="stats:reviews")
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    b.adjust(2)

    if not api:
        await callback.message.edit_text(
            "⭐ <b>Отзывы</b>\n\n❌ API токен не настроен",
            reply_markup=b.as_markup(),
        )
        await callback.answer()
        return

    try:
        data = await api.get_reviews()
        reviews: list[dict] = data.get("data") or data.get("items") or []
        if not reviews:
            text = "⭐ <b>Отзывы</b>\n\nОтзывов пока нет."
        else:
            lines = [f"⭐ <b>Отзывы</b> (последние {min(len(reviews), 10)})\n"]
            for r in reviews[:10]:
                rating = r.get("rating") or r.get("score") or 0
                author = r.get("author") or r.get("buyer_name") or "Покупатель"
                comment = (r.get("text") or r.get("comment") or "—")[:120]
                try:
                    stars = "⭐" * int(rating)
                except (TypeError, ValueError):
                    stars = str(rating)
                lines.append(f"{stars or '—'} <b>{author}</b>")
                lines.append(f"   <i>{comment}</i>\n")
            text = "\n".join(lines)
    except Exception as e:
        text = f"⭐ <b>Отзывы</b>\n\n❌ Ошибка: {e}"

    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "stats:hours")
async def show_hours_chart(callback: CallbackQuery) -> None:
    await callback.answer()

    events, source, err = await _events(callback.from_user.id)

    hour_counts: dict[int, int] = defaultdict(int)
    # Local time, not UTC: a «пиковый час» three hours off tells the seller to
    # promote at the wrong time of day.
    for e in _sales(events):
        hour_counts[datetime.fromtimestamp(float(e["ts"])).hour] += 1

    total_count = sum(hour_counts.values())

    if not total_count:
        text = ("📊 <b>Статистика по часам</b>\n\nПродаж пока нет.\n\n"
                + source_note(source, err))
        await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())
        return

    bar_length = 8
    # normalize against the max 2-hour bucket sum (bars sum two hours)
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

    # Buyers are the one figure the panel's money ledger does not carry — it
    # records amounts, not who paid — so this screen stays on the bot's own
    # order history and says so.
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
    """The same ledger the screens are built from, as a spreadsheet.

    The export used to read the bot's own order history while the figures on
    screen came from the panel, so the file and the screen disagreed with each
    other. Both read the same events now.
    """
    await callback.answer("⏳ Генерирую CSV...", show_alert=False)

    events, source, _err = await _events(callback.from_user.id)
    rows = sorted(events, key=lambda e: float(e.get("ts") or 0), reverse=True)

    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, delimiter=";")     # Excel-RU splits on ';'
    writer.writerow(["Дата", "Тип", "Товар", "Покупатель", "Сумма ₽", "Статус"])

    for e in rows:
        ts = float(e.get("ts") or 0)
        date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"
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
    """What the panel's ledger actually says — read-only, changes nothing.

    Every real answer in this bot came from printing the server's own response
    instead of guessing at it. If a total looks wrong, this shows the rows it
    was summed from and how each one was read.
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
    today = summarize(events, day_start(now))
    week = summarize(events, now - 7 * 86400)
    tail = (f"\n\nсобытий: {len(events)} (источник {source}{', ' + err if err else ''})"
            f"\nсегодня: {today['sales']} продаж / {today['revenue']:.0f} ₽"
            f"\n7 дней: {week['sales']} продаж / {week['revenue']:.0f} ₽")

    await status.edit_text("📊 <b>Статистика: что отдаёт панель</b>\n\n"
                           f"<code>{_html.escape(report + tail)[:3500]}</code>")
