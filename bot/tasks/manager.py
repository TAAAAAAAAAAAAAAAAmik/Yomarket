from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from api.yoomarket import YooMarketAPI
from storage import get_token, get_settings, save_settings

logger = logging.getLogger(__name__)


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
        has_any = (
            settings.get("auto_reply", {}).get("enabled") or
            settings.get("auto_events", {}).get("on_confirmed", {}).get("enabled") or
            settings.get("auto_events", {}).get("on_refunded", {}).get("enabled")
        )
        if not has_any:
            return
        await self._process_orders(user_id, token, settings)

    async def _process_orders(self, user_id: int, token: str, settings: dict) -> None:
        api = YooMarketAPI(token)
        await api.start()
        try:
            data = await api.get_orders()
            orders = data.get("data") or data.get("items") or []

            known: dict = settings.get("known_orders", {})  # {order_id: status}
            ar = settings.get("auto_reply", {})
            ae = settings.get("auto_events", {})
            rules = settings.get("auto_rules", [])
            responders = settings.get("responders", {})

            for order in orders:
                oid = str(order.get("id", ""))
                if not oid:
                    continue

                status = str(order.get("status", ""))
                prev_status = known.get(oid)
                title = order.get("title") or order.get("ad_title") or order.get("product_name") or "—"
                buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
                price = order.get("price") or order.get("total") or "—"
                chat_id = str(order.get("chat_id") or oid)

                # NEW order
                if prev_status is None and ar.get("enabled"):
                    msg = self._pick_message(title, ar.get("message", "Спасибо за заказ!"), rules, responders)
                    await self._send_chat(api, chat_id, msg)
                    await self._notify(user_id, (
                        f"🛒 <b>Новый заказ #{oid}</b>\n"
                        f"📦 {title}\n👤 {buyer}\n💰 {price} ₽\n"
                        f"✉️ Авто-ответ отправлен"
                    ))

                # Status changed → confirmed
                elif prev_status != status and status in ("confirmed", "completed", "done"):
                    ev = ae.get("on_confirmed", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Заказ подтверждён!"), rules, responders)
                        await self._send_chat(api, chat_id, msg)
                        await self._notify(user_id, f"✅ Заказ #{oid} подтверждён")

                # Status changed → refunded
                elif prev_status != status and status in ("refunded", "cancelled", "returned"):
                    ev = ae.get("on_refunded", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(title, ev.get("message", "Возврат оформлен."), rules, responders)
                        await self._send_chat(api, chat_id, msg)
                        await self._notify(user_id, f"↩️ Заказ #{oid} — возврат")

                known[oid] = status

            settings["known_orders"] = known
            # keep backward compat
            settings["known_order_ids"] = list(known.keys())
            save_settings(user_id, settings)
        finally:
            await api.close()

    def _pick_message(self, title: str, default: str, rules: list[dict], responders: dict | None = None) -> str:
        title_lower = title.lower()
        # Check specific responders first (by ad title match)
        if responders:
            for game_name, message in responders.items():
                if game_name.lower() in title_lower:
                    return message
        # Then check rules (keyword-based)
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

    async def _notify(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Notify failed (user %s): %s", user_id, e)
