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
        """Start background task for a user (cancels existing one first)."""
        if user_id in self._tasks and not self._tasks[user_id].done():
            self._tasks[user_id].cancel()
        self._tasks[user_id] = asyncio.create_task(self._user_loop(user_id))

    def stop_for_user(self, user_id: int) -> None:
        if user_id in self._tasks:
            self._tasks[user_id].cancel()
            del self._tasks[user_id]

    async def start_all(self) -> None:
        """Start tasks for all users that have tokens saved."""
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
                logger.error(f"Task error for user {user_id}: {e}")
            await asyncio.sleep(60)

    async def _tick(self, user_id: int) -> None:
        token = get_token(user_id)
        if not token:
            return
        settings = get_settings(user_id)

        # Auto-reply on new orders
        if settings.get("auto_reply", {}).get("enabled"):
            await self._auto_reply(user_id, token, settings)

    async def _auto_reply(self, user_id: int, token: str, settings: dict) -> None:
        api = YooMarketAPI(token)
        await api.start()
        try:
            data = await api.get_orders()
            orders = data.get("data") or data.get("items") or []
            known = set(str(x) for x in settings.get("known_order_ids", []))
            reply_msg = settings["auto_reply"].get("message", "Спасибо за заказ!")

            for order in orders:
                oid = str(order.get("id", ""))
                if not oid or oid in known:
                    continue
                # New order found
                known.add(oid)
                chat_id = str(order.get("chat_id") or oid)
                title = (
                    order.get("title")
                    or order.get("ad_title")
                    or order.get("product_name")
                    or "—"
                )
                buyer = order.get("buyer_name") or (order.get("buyer") or {}).get("name") or "—"
                price = order.get("price") or order.get("total") or "—"

                # Send auto-reply to chat
                try:
                    await api.send_message(chat_id, reply_msg)
                except Exception as e:
                    logger.warning(f"Auto-reply failed for order {oid}: {e}")

                # Notify user in Telegram
                try:
                    await self.bot.send_message(
                        user_id,
                        f"🛒 <b>Новый заказ #{oid}</b>\n"
                        f"📦 {title}\n"
                        f"👤 {buyer}\n"
                        f"💰 {price} ₽\n\n"
                        f"✉️ Авто-ответ отправлен",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(f"Notify failed for user {user_id}: {e}")

            settings["known_order_ids"] = list(known)
            save_settings(user_id, settings)
        finally:
            await api.close()
