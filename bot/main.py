from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject
from typing import Any, Awaitable, Callable

from config import BOT_TOKEN, YOOMARKET_TOKEN
from api import YooMarketAPI
from handlers import ads, balance, chats, orders, start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Middleware — injects the YooMarketAPI instance into handler data
# ---------------------------------------------------------------------------


class YooMarketMiddleware:
    def __init__(self, api: YooMarketAPI) -> None:
        self.api = api

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["api"] = self.api
        return await handler(event, data)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    api = YooMarketAPI(YOOMARKET_TOKEN)
    await api.start()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Register middleware on both message and callback_query update types
    dp.message.middleware(YooMarketMiddleware(api))
    dp.callback_query.middleware(YooMarketMiddleware(api))

    # Include routers
    dp.include_router(start.router)
    dp.include_router(balance.router)
    dp.include_router(ads.router)
    dp.include_router(orders.router)
    dp.include_router(chats.router)

    logger.info("Bot starting…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await api.close()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
