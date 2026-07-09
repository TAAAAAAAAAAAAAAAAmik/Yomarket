from __future__ import annotations

import asyncio
import logging
import os
import ssl
from typing import Any, Awaitable, Callable

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from config import BOT_TOKEN
from api.yoomarket import YooMarketAPI
from storage import get_token
from handlers import accounts, admin, ads, auto_settings, balance, chats, create_ad, notifications, orders, packs, panel, panel_items, plugins, price_schedule, responders, selenium_settings, settings, start, stats, tools
from tasks import TaskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class AccessMiddleware:
    """Blocks banned users and (optionally) gates access behind a subscription.
    Owner and admins always pass."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from storage import (is_admin, is_blocked, has_active_subscription,
                             require_subscription_enabled, get_bot_price)
        user = data.get("event_from_user")
        if user:
            uid = user.id
            if is_blocked(uid) and not is_admin(uid):
                return  # banned — silently drop
            # Subscription gate: allow /start so the user can see the notice,
            # and always allow admins.
            if (require_subscription_enabled() and not is_admin(uid)
                    and not has_active_subscription(uid)):
                text = getattr(event, "text", "") or ""
                is_start = text.startswith("/start")
                if not is_start:
                    from storage import render_custom_text
                    price = get_bot_price()
                    price_line = f"\n💰 Стоимость: <b>{price} ₽</b>" if price else ""
                    msg = render_custom_text("subscription", price=price_line)
                    try:
                        from aiogram.types import CallbackQuery as _CQ, Message as _Msg
                        if isinstance(event, _CQ):
                            await event.answer("🔒 Нужна подписка", show_alert=True)
                        elif isinstance(event, _Msg):
                            await event.answer(msg)
                    except Exception:
                        pass
                    return
        return await handler(event, data)


class YooMarketMiddleware:
    """Injects per-user YooMarketAPI instance into handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            token = get_token(user.id)
            if token:
                api = YooMarketAPI(token)
                await api.start()
                try:
                    data["api"] = api
                    return await handler(event, data)
                finally:
                    await api.close()
        data["api"] = None
        return await handler(event, data)


async def main() -> None:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    session = AiohttpSession()
    session._connector_init["ssl"] = ssl_ctx

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # FSM state store: Redis if REDIS_URL is set (survives restarts), else memory
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        try:
            from aiogram.fsm.storage.redis import RedisStorage
            fsm_storage = RedisStorage.from_url(redis_url)
            logger.info("FSM storage: Redis")
        except Exception as e:
            logger.warning("Redis unavailable (%s), using memory FSM", e)
            fsm_storage = MemoryStorage()
    else:
        fsm_storage = MemoryStorage()
    dp = Dispatcher(storage=fsm_storage)

    if os.environ.get("DATABASE_URL", "").strip():
        logger.info("Data storage: PostgreSQL")
    else:
        logger.info("Data storage: JSON files (set DATABASE_URL for PostgreSQL)")

    task_manager = TaskManager(bot)
    dp["task_manager"] = task_manager

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.message.middleware(YooMarketMiddleware())
    dp.callback_query.middleware(YooMarketMiddleware())

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(balance.router)
    dp.include_router(ads.router)
    dp.include_router(create_ad.router)
    dp.include_router(panel_items.router)
    dp.include_router(packs.router)
    dp.include_router(accounts.router)
    dp.include_router(price_schedule.router)
    dp.include_router(orders.router)
    dp.include_router(chats.router)
    dp.include_router(settings.router)
    dp.include_router(notifications.router)
    dp.include_router(auto_settings.router)
    dp.include_router(selenium_settings.router)
    dp.include_router(responders.router)
    dp.include_router(plugins.router)
    dp.include_router(stats.router)
    dp.include_router(tools.router)
    dp.include_router(panel.router)

    logger.info("Bot starting…")
    try:
        await task_manager.start_all()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
