from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_token, get_settings, save_settings

logger = logging.getLogger(__name__)


def _fmt_time(raw) -> str:
    if not raw:
        return ""
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.fromtimestamp(raw)
        else:
            s = str(raw)[:19]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return str(raw)[:16]
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return str(raw)[:16]


def _is_newer(msg_id: str, last_id: str) -> bool:
    try:
        return int(msg_id) > int(last_id)
    except (ValueError, TypeError):
        return msg_id > last_id


def _order_notify_kb(order_id: str, chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 К сделке", callback_data=f"chat:{chat_id}:")
    builder.button(text="↩️ Возврат", callback_data=f"order:{order_id}:refund")
    builder.adjust(2)
    return builder.as_markup()


def _message_notify_kb(chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
    builder.button(text="📋 К сделке", callback_data=f"chat:{chat_id}:")
    builder.adjust(2)
    return builder.as_markup()


class TaskManager:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}

    def start_for_user(self, user_id: int) -> None:
        if user_id in self._tasks and not self._tasks[user_id].done():
            self._tasks[user_id].cancel()
        self._tasks[user_id] = asyncio.create_task(self._user_loop(user_id))

    def stop_for_user(self, user_id: int) -> None:
        if user_id in self._tasks:
            self._tasks[user_id].cancel()
            del self._tasks[user_id]

    async def start_all(self) -> None:
        from storage import get_all_users
        for uid in get_all_users():
            self.start_for_user(uid)

    async def _user_loop(self, user_id: int) -> None:
        while True:
            try:
                await self._tick(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Task error for user %s: %s", user_id, e)
            await asyncio.sleep(60)

    async def _tick(self, user_id: int) -> None:
        token = get_token(user_id)
        if not token:
            return
        settings = get_settings(user_id)
        await self._process_orders(user_id, token, settings)

    async def _process_orders(self, user_id: int, token: str, settings: dict) -> None:
        api = YooMarketAPI(token)
        await api.start()
        try:
            data = await api.get_orders()
            orders = data.get("data") or data.get("items") or []

            known: dict = settings.get("known_orders", {})
            ar = settings.get("auto_reply", {})
            ae = settings.get("auto_events", {})
            rules = settings.get("auto_rules", [])
            responders_map = settings.get("responders", {})
            order_details: dict = settings.setdefault("known_order_details", {})

            for order in orders:
                oid = str(order.get("id", ""))
                if not oid:
                    continue

                status = str(order.get("status", ""))
                prev_status = known.get(oid)
                title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
                buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
                price = order.get("price") or order.get("total") or "—"
                time_raw = order.get("created_at") or order.get("date") or order.get("created")
                chat_id = str(order.get("chat_id") or oid)

                order_details[oid] = {"title": title, "buyer": buyer, "price": price, "chat_id": chat_id}

                if prev_status is None:
                    time_str = _fmt_time(time_raw)
                    time_part = f"  •  🕐 {time_str}" if time_str else ""
                    await self._notify(
                        user_id,
                        f"🛒 <b>Новая покупка!</b>\n\n"
                        f"👤 Покупатель: <b>{buyer}</b>\n"
                        f"💰 Сумма: <b>{price} ₽</b>{time_part}\n"
                        f"📦 Товар: <b>{title}</b>",
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )
                    if ar.get("enabled"):
                        msg = self._pick_message(title, ar.get("message", "Спасибо за заказ!"), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)

                elif prev_status != status and status in ("confirmed", "completed", "done"):
                    ev = ae.get("on_confirmed", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Заказ подтверждён!"), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    await self._notify(user_id, f"✅ Заказ #{oid} подтверждён\n📦 {title}")

                elif prev_status != status and status in ("refunded", "cancelled", "returned"):
                    ev = ae.get("on_refunded", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Возврат оформлен."), rules, responders_map)
                        await self._send_chat(api, chat_id, msg)
                    await self._notify(user_id, f"↩️ Заказ #{oid} — возврат\n📦 {title}")

                known[oid] = status

            settings["known_orders"] = known
            settings["known_order_ids"] = list(known.keys())
            settings["known_order_details"] = order_details

            await self._check_messages(user_id, api, settings)

            save_settings(user_id, settings)
        finally:
            await api.close()

    async def _check_messages(self, user_id: int, api: YooMarketAPI, settings: dict) -> None:
        known_orders: dict = settings.get("known_orders", {})
        known_messages: dict = settings.setdefault("known_messages", {})
        order_details: dict = settings.get("known_order_details", {})

        active = [
            oid for oid, st in known_orders.items()
            if st not in ("refunded", "cancelled", "returned")
        ][:15]

        for order_id in active:
            try:
                data = await api.get_messages(order_id)
                messages: list[dict] = data.get("data") or data.get("items") or []
                if not messages:
                    continue

                newest_id = str(messages[-1].get("id", ""))
                last_known_id = known_messages.get(order_id)

                if last_known_id is None:
                    known_messages[order_id] = newest_id
                    continue

                if not _is_newer(newest_id, last_known_id):
                    continue

                details = order_details.get(order_id, {})
                title = details.get("title", f"Заказ #{order_id}")
                buyer_name = details.get("buyer", "Покупатель")
                chat_id = details.get("chat_id", order_id)

                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known_id):
                        continue
                    sender = msg.get("sender_type") or msg.get("sender") or ""
                    if sender not in ("buyer", "client", "customer", "user"):
                        continue

                    time_str = _fmt_time(msg.get("created_at") or msg.get("date"))
                    time_part = f"  •  🕐 {time_str}" if time_str else ""
                    msg_text = (msg.get("text") or msg.get("message") or "—")[:200]

                    await self._notify(
                        user_id,
                        f"💬 <b>Новое сообщение</b>\n\n"
                        f"👤 <b>{buyer_name}</b>{time_part}\n"
                        f"📦 {title}\n\n"
                        f"<i>«{msg_text}»</i>",
                        reply_markup=_message_notify_kb(chat_id),
                    )

                known_messages[order_id] = newest_id

            except Exception as e:
                logger.warning("Message check for order %s: %s", order_id, e)

        settings["known_messages"] = known_messages

    def _pick_message(self, title: str, default: str, rules: list[dict], responders: dict | None = None) -> str:
        title_lower = title.lower()
        if responders:
            for game_name, message in responders.items():
                if game_name.lower() in title_lower:
                    return message
        for rule in rules:
            kw = rule.get("keyword", "").lower()
            if kw and kw in title_lower:
                return rule.get("message", default)
        return default

    async def _send_chat(self, api: YooMarketAPI, chat_id: str, text: str) -> None:
        try:
            await api.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Auto chat send failed (chat %s): %s", chat_id, e)

    async def _notify(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Notify failed (user %s): %s", user_id, e)
