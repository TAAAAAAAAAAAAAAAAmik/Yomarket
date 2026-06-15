from __future__ import annotations

import asyncio
import logging
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
from handlers import ads, balance, chats, orders, start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(YooMarketMiddleware())
    dp.callback_query.middleware(YooMarketMiddleware())

    dp.include_router(start.router)
    dp.include_router(balance.router)
    dp.include_router(ads.router)
    dp.include_router(orders.router)
    dp.include_router(chats.router)

    logger.info("Bot starting…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
