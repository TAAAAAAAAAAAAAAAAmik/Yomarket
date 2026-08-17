from __future__ import annotations

import asyncio
import logging
import os
import re
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
from handlers import accounts, admin, ads, auto_settings, autopilot, balance, chats, create_ad, fallback, notifications, nsgifts, orders, packs, panel, panel_items, plugins, prices, responders, selenium_settings, settings, start, stats
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


_COMMAND_RE = re.compile(r"^/[a-zA-Z0-9_]{1,32}(@[A-Za-z0-9_]+)?(\s|$)")


class CommandsEscapeForms:
    """Команда работает всегда, даже если на экране висит незаконченная форма.

    Продавец вызвал `/fragment_cookies` и получил в ответ «📷 Отправьте фото
    или нажмите „Без фото“»: за час до этого он начал создавать объявление и
    не довёл до конца. Экран, ждущий ввода, ловит **любое** сообщение —
    команду в том числе, — и команда не выполняется вовсе.

    Экранов таких девяносто три. То есть одна брошенная форма выключала в
    боте все команды разом, а выглядело это как поломка той команды, которую
    в этот момент набрали. Ровно та же тишина, что была у команд-двойников,
    только причина другая.

    Middleware внешний: он должен отработать раньше фильтров, иначе состояние
    уже перехватит сообщение. Брошенная форма при этом не исчезает молча —
    об этом говорится одной строкой: пропажа наполовину заполненной формы
    без объяснения удивляла бы не меньше.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        text = getattr(event, "text", "") or ""
        if state is not None and _COMMAND_RE.match(text):
            try:
                current = await state.get_state()
            except Exception:                      # хранилище FSM недоступно
                current = None
            if current:
                await state.clear()
                # Одного `clear()` мало, и это стоило отдельного круга.
                # Фильтр состояния сверяется НЕ с хранилищем, а с
                # `data["raw_state"]`, которое aiogram прочитал один раз ещё
                # до нас — в своём FSM-middleware на уровне update. Форма
                # честно закрывалась, бот честно писал «выполняю команду», а
                # фильтр по-прежнему видел старое состояние и отдавал
                # сообщение той же форме. Продавец получал обе строки подряд:
                # «форма закрыта» и снова «отправьте фото».
                data["raw_state"] = None
                try:
                    await event.answer(
                        "↩️ Незаконченная форма закрыта — выполняю команду.")
                except Exception:
                    pass
        return await handler(event, data)


_LAST_ERROR_AT: dict[int, float] = {}


def _install_error_reporter(dp: Dispatcher) -> None:
    """Показывать сбой обработчика пользователю, а не только в логах контейнера.

    Необработанное исключение внутри обработчика aiogram просто пишет в лог.
    Для человека это выглядит как «кнопка не нажимается»: нажатие не
    подтверждено, экран не изменился, объяснений нет. Логи контейнера продавец
    не читает — и не должен.
    """
    import time as _time
    import traceback

    from aiogram.types import ErrorEvent

    @dp.errors()
    async def _report(event: ErrorEvent) -> bool:
        exc = event.exception
        logger.exception("handler failed: %s", exc)

        update = event.update
        cq = getattr(update, "callback_query", None)
        user = getattr(cq, "from_user", None) or getattr(
            getattr(update, "message", None), "from_user", None)
        if not user:
            return True

        # Один рассказ об ошибке в 15 секунд: упавший API иначе завалит чат
        # одинаковыми сообщениями.
        now = _time.time()
        if now - _LAST_ERROR_AT.get(user.id, 0) < 15:
            return True
        _LAST_ERROR_AT[user.id] = now

        where = getattr(cq, "data", "") or "сообщение"
        text = (f"⚠️ <b>Сбой</b>\n\n"
                f"Действие: <code>{where[:60]}</code>\n"
                f"Причина: <code>{str(exc)[:250] or type(exc).__name__}</code>\n\n"
                "<i>Это сообщение вместо молчания: раньше такая ошибка просто "
                "ничего не делала.</i>")
        # Бот берётся из самого события, а не запоминается при установке:
        # запомненный — это лишний способ отправить ответ не туда.
        target = getattr(event.update, "bot", None) or getattr(cq, "bot", None)
        try:
            if cq is not None:
                await cq.answer()          # снять «часики» с кнопки
            if target is not None:
                await target.send_message(user.id, text)
        except Exception:
            logger.warning("could not report error to %s: %s", user.id,
                           traceback.format_exc(limit=1))
        return True


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

    import storage as _storage
    if _storage._USE_DB:
        logger.info("Data storage: PostgreSQL")
    else:
        logger.info("Data storage: JSON files (set DATABASE_URL for PostgreSQL)")

    task_manager = TaskManager(bot)
    dp["task_manager"] = task_manager

    # Внешний и первый: он должен отработать до фильтров состояний, иначе
    # брошенная форма перехватит команду раньше, чем мы успеем вмешаться.
    dp.message.outer_middleware(CommandsEscapeForms())
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.message.middleware(YooMarketMiddleware())
    dp.callback_query.middleware(YooMarketMiddleware())

    _install_error_reporter(dp)

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(balance.router)
    dp.include_router(ads.router)
    dp.include_router(create_ad.router)
    dp.include_router(panel_items.router)
    dp.include_router(packs.router)
    dp.include_router(accounts.router)
    dp.include_router(prices.router)
    dp.include_router(orders.router)
    dp.include_router(chats.router)
    dp.include_router(settings.router)
    dp.include_router(notifications.router)
    dp.include_router(autopilot.router)
    dp.include_router(auto_settings.router)
    dp.include_router(selenium_settings.router)
    dp.include_router(responders.router)
    dp.include_router(nsgifts.router)
    dp.include_router(plugins.router)
    dp.include_router(stats.router)
    dp.include_router(panel.router)
    # Последним: ловит нажатия, которые не разобрал никто. Без него такое
    # нажатие молча пропадает, и это неотличимо от сломанной кнопки.
    dp.include_router(fallback.router)

    logger.info("Bot starting…")
    await _start_health_server()  # для Koyeb/Render/Fly (health-check по $PORT)
    try:
        await task_manager.start_all()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


async def _start_health_server() -> None:
    """Tiny HTTP server on $PORT so platforms that require a listening port
    (Koyeb, Render, Fly) keep the container alive. No-op if PORT is unset."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        from aiohttp import web
        app = web.Application()
        async def _ok(_req):
            return web.Response(text="OK — bot running")
        app.router.add_get("/", _ok)
        app.router.add_get("/health", _ok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port))
        await site.start()
        logger.info("Health server on :%s", port)
    except Exception as e:
        logger.warning("Health server failed: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
