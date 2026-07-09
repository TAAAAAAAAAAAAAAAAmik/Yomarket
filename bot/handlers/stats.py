from __future__ import annotations

import csv
import io
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
from storage import get_settings
from handlers.balance import _parse_check

router = Router()
logger = logging.getLogger(__name__)

COMPLETED_STATUSES = ("confirmed", "completed", "done")


def _back_to_stats_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Статистика", callback_data="menu:stats")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "menu:stats")
async def show_stats(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю статистику...")

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})

    completed = sum(1 for s in known_orders.values() if s in COMPLETED_STATUSES)
    refunded = sum(1 for s in known_orders.values() if s in ("refunded", "cancelled", "returned"))
    active = len(known_orders) - completed - refunded
    total = len(known_orders)

    balance_str = "—"
    shop_name = "—"
    ads_total = "—"

    if api:
        try:
            check_data = await api.check()
            shop_name, balance_str, _ = _parse_check(check_data)
            balance_str = f"{balance_str} ₽"
        except Exception as e:
            logger.warning("Stats balance error: %s", e)

        try:
            ads_data = await api.get_ads()
            meta = ads_data.get("meta", {})
            ads_list = ads_data.get("data") or ads_data.get("items") or []
            ads_total = str(
                meta.get("total") or meta.get("count") or meta.get("total_count") or len(ads_list)
            )
        except Exception as e:
            logger.warning("Stats ads error: %s", e)

    text = (
        f"📊 <b>Статистика</b>\n"
        f"🏪 {shop_name}\n\n"
        f"💰 Баланс: <b>{balance_str}</b>\n"
        f"🚀 Объявлений: <b>{ads_total}</b>\n\n"
        f"🛒 <b>Заказы (за всё время)</b>\n"
        f"├ Всего: <b>{total}</b>\n"
        f"├ ✅ Выполнено: <b>{completed}</b>\n"
        f"├ ⏳ Активные: <b>{active}</b>\n"
        f"└ ↩️ Возвраты: <b>{refunded}</b>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="menu:stats")
    builder.button(text="💰 Выручка", callback_data="stats:revenue:7")
    builder.button(text="📈 График (7 дней)", callback_data="stats:chart")
    builder.button(text="🏆 Топ товаров", callback_data="stats:top")
    builder.button(text="📤 Экспорт заказов", callback_data="stats:export")
    builder.button(text="⭐ Отзывы", callback_data="stats:reviews")
    builder.button(text="📊 По часам", callback_data="stats:hours")
    builder.button(text="👥 Повторные покупатели", callback_data="stats:buyers")
    builder.button(text="📊 Экспорт CSV", callback_data="stats:export_csv")
    builder.button(text="⬅️ Главное меню", callback_data="menu:main")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


_PERIODS = {1: "сегодня", 7: "7 дней", 30: "30 дней", 0: "всё время"}


@router.callback_query(F.data.startswith("stats:revenue:"))
async def show_revenue(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        days = 7

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})
    details: dict = settings.get("known_order_details", {})

    now = datetime.now(tz=timezone.utc)
    count = 0
    revenue = 0
    by_title: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [count, sum]

    for oid, status in known_orders.items():
        if status not in COMPLETED_STATUSES:
            continue
        det = details.get(str(oid)) or details.get(oid)
        if not det:
            continue
        seen_at = det.get("seen_at")
        if days > 0:
            if not seen_at:
                continue
            try:
                dt = datetime.fromtimestamp(int(seen_at), tz=timezone.utc)
            except (ValueError, OSError):
                continue
            if (now - dt) > timedelta(days=days):
                continue
        try:
            price = int(float(str(det.get("price", 0))))
        except (ValueError, TypeError):
            price = 0
        count += 1
        revenue += price
        title = str(det.get("title") or "—")[:35]
        by_title[title][0] += 1
        by_title[title][1] += price

    period = _PERIODS.get(days, f"{days} дней")
    lines = [f"💰 <b>Выручка — {period}</b>\n"]
    lines.append(f"📦 Продаж: <b>{count}</b>")
    lines.append(f"💵 Выручка: <b>{revenue} ₽</b>")
    if count:
        lines.append(f"🧾 Средний чек: <b>{revenue // count} ₽</b>")
        top = sorted(by_title.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        lines.append("\n🏆 <b>Топ по выручке:</b>")
        for i, (title, (cnt, total)) in enumerate(top, 1):
            lines.append(f"{i}. {title} — {cnt} шт., <b>{total} ₽</b>")
    else:
        lines.append("\nНет выполненных заказов за период.")

    b = InlineKeyboardBuilder()
    for d in (1, 7, 30, 0):
        mark = "• " if d == days else ""
        b.button(text=f"{mark}{_PERIODS[d]}", callback_data=f"stats:revenue:{d}")
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    b.adjust(4, 1)

    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "stats:chart")
async def show_chart(callback: CallbackQuery) -> None:
    await callback.answer()

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})
    known_order_details: dict = settings.get("known_order_details", {})

    now = datetime.now(tz=timezone.utc)
    days_counts: dict[int, int] = defaultdict(int)
    days_revenue: dict[int, int] = defaultdict(int)

    for order_id, status in known_orders.items():
        if status not in COMPLETED_STATUSES:
            continue
        details = known_order_details.get(str(order_id)) or known_order_details.get(order_id)
        if not details:
            continue
        seen_at = details.get("seen_at")
        if not seen_at:
            continue
        try:
            order_dt = datetime.fromtimestamp(int(seen_at), tz=timezone.utc)
        except (ValueError, OSError):
            continue
        delta_days = (now.date() - order_dt.date()).days
        if 0 <= delta_days < 7:
            day_index = 6 - delta_days
            days_counts[day_index] += 1
            try:
                price = int(float(str(details.get("price", 0))))
            except (ValueError, TypeError):
                price = 0
            days_revenue[day_index] += price

    bar_length = 10
    max_count = max(days_counts.values(), default=1) or 1
    lines = []
    for i in range(7):
        day_date = (now - timedelta(days=6 - i)).date()
        label = day_date.strftime("%d.%m")
        count = days_counts.get(i, 0)
        filled = round((count / max_count) * bar_length)
        bar = "█" * filled + "░" * (bar_length - filled)
        lines.append(f"{label} {bar} {count}")

    total_count = sum(days_counts.values())
    total_revenue = sum(days_revenue.values())

    chart_text = "\n".join(lines)
    text = (
        f"📈 <b>График продаж (7 дней)</b>\n\n"
        f"<code>{chart_text}</code>\n\n"
        f"📦 Выполнено заказов: <b>{total_count}</b>\n"
        f"💵 Выручка: <b>{total_revenue} ₽</b>"
    )

    await callback.message.edit_text(text, reply_markup=_back_to_stats_keyboard())


@router.callback_query(F.data == "stats:top")
async def show_top_products(callback: CallbackQuery) -> None:
    await callback.answer()

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})
    known_order_details: dict = settings.get("known_order_details", {})

    title_counts: Counter = Counter()

    for order_id, status in known_orders.items():
        if status not in COMPLETED_STATUSES:
            continue
        details = known_order_details.get(str(order_id)) or known_order_details.get(order_id)
        if not details:
            continue
        title = details.get("title")
        if title:
            title_counts[str(title)] += 1

    if not title_counts:
        text = "🏆 <b>Топ товаров</b>\n\nНет данных о выполненных заказах."
    else:
        top = title_counts.most_common(10)
        lines = []
        for rank, (title, count) in enumerate(top, start=1):
            lines.append(f"{rank}. {title} — <b>{count}</b> шт.")
        text = "🏆 <b>Топ товаров (по продажам)</b>\n\n" + "\n".join(lines)

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
    b.adjust(1)

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

    settings = get_settings(callback.from_user.id)
    known_orders: dict = settings.get("known_orders", {})
    known_order_details: dict = settings.get("known_order_details", {})

    hour_counts: dict[int, int] = defaultdict(int)

    for order_id, status in known_orders.items():
        if status not in COMPLETED_STATUSES:
            continue
        details = known_order_details.get(str(order_id)) or known_order_details.get(order_id)
        if not details:
            continue
        seen_at = details.get("seen_at")
        if not seen_at:
            continue
        try:
            order_dt = datetime.fromtimestamp(int(seen_at), tz=timezone.utc)
        except (ValueError, OSError):
            continue
        hour_counts[order_dt.hour] += 1

    total_count = sum(hour_counts.values())

    if not total_count:
        text = "📊 <b>Статистика по часам</b>\n\nНет данных о выполненных заказах."
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
        f"📦 Всего выполнено: <b>{total_count}</b>\n"
        f"🔝 Пиковый час: <b>{peak_hour:02d}:00–{peak_hour:02d}:59</b> ({hour_counts[peak_hour]} заказов)"
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

    if not buyer_counts:
        text = "👥 <b>Повторные покупатели</b>\n\nНет данных о выполненных заказах."
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


@router.callback_query(F.data == "stats:export_csv")
async def export_orders_csv(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Генерирую CSV...", show_alert=False)

    s = get_settings(callback.from_user.id)
    known_orders: dict = s.get("known_orders", {})
    order_details: dict = s.get("known_order_details", {})

    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output)
    writer.writerow(["ID", "Товар", "Покупатель", "Сумма", "Статус", "Дата"])

    for oid, status_raw in known_orders.items():
        det = order_details.get(str(oid), order_details.get(oid, {}))
        title = det.get("title", "—")
        buyer = det.get("buyer", "—")
        price = det.get("price", "—")
        status = _STATUS_MAP.get(status_raw, status_raw)
        seen_at = det.get("seen_at")
        if seen_at:
            try:
                date_str = datetime.fromtimestamp(int(seen_at), tz=timezone.utc).strftime("%d.%m.%Y %H:%M")
            except (ValueError, OSError):
                date_str = "—"
        else:
            date_str = "—"
        writer.writerow([oid, title, buyer, price, status, date_str])

    content = output.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(content, filename="orders_export.csv")
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Статистика", callback_data="menu:stats")
    await callback.message.answer_document(
        file,
        caption=f"📊 <b>Экспорт CSV</b>\nВсего: <b>{len(known_orders)}</b>",
        reply_markup=b.as_markup(),
    )
