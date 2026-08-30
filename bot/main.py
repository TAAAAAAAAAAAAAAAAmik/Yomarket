from __future__ import annotations

import asyncio
import json
import logging
import time as _t
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
from handlers import accounts, admin, ads, approute, auto_settings, autopilot, balance, chats, commands, create_ad, fallback, notifications, nsgifts, orders, packs, panel, panel_items, plugins, policy, prices, responders, selenium_settings, settings, start, stats
from tasks import TaskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class AccessMiddleware:
    """Отсекает заблокированных и, если включена подписка, — тех, у кого её нет.
    Владелец и админы проходят всегда."""

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
                return  # заблокирован — молча не отвечаем
            # Проверка подписки: /start пропускаем всегда — иначе человек не увидит
            # даже сообщения о том, что подписка нужна. Админы проходят без проверки.
            if (require_subscription_enabled() and not is_admin(uid)
                    and not has_active_subscription(uid)):
                text = getattr(event, "text", "") or ""
                is_start = text.startswith("/start")
                if not is_start:
                    from storage import price_lines, render_custom_text
                    # Тарифы по срокам, а не одно число: документы обещают
                    # скидку за длинный срок, и клиент должен её видеть.
                    rows = price_lines()
                    price_line = ("\n\n" + "\n".join(rows)) if rows else ""
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

    Продавец вызвал `/fragment_cookies` и получил в ответ «📷 Отправь фото
    или жми „Без фото“»: за час до этого он начал создавать объявление и
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


# Состояние получения обновлений. Собирается в dict, а не в текст: правило
# «не управляйте логикой по тексту собственного отчёта» здесь тоже действует.
POLLING: dict = {
    "last_update": 0.0,     # когда пришло последнее обновление от Telegram
    "error": "",            # чем именно кончилась последняя попытка
    "error_at": 0.0,
    "failing_since": 0.0,
    "told_at": 0.0,         # когда об этом сказали продавцу
    "webhook": "",          # вебхук, найденный при запуске (он ломает опрос)
}

# У отказа получать обновления две разные причины, и обе приходят как 409.
# Различать их обязательно: они лечатся противоположными действиями, а
# подсказка «у вас запущен второй бот» человеку с одним сервером отправляет
# искать несуществующее. Первая версия этого кода так и делала.
_TWIN_MARKS = ("terminated by other getupdates",)
_WEBHOOK_MARKS = ("webhook is active", "can't use getupdates")


def polling_trouble() -> str:
    """Что мешает боту получать сообщения. Пусто — ничего.

    Отдельной функцией, потому что ответ нужен и `/health`, и `/version`, и
    сообщению продавцу.
    """
    if not POLLING["error"]:
        return ""
    low = POLLING["error"].lower()
    if any(m in low for m in _WEBHOOK_MARKS):
        return ("на токене стоит вебхук — с ним Telegram не отдаёт сообщения "
                "опросом вовсе. Снимается при запуске автоматически; если "
                "видишь это, снять не удалось")
    if any(m in low for m in _TWIN_MARKS):
        return ("бота запустили дважды с одним токеном — Telegram отдаёт "
                "сообщения только одному, и второй молчит")
    if "conflict" in low:
        # Обе причины дают 409. Назвать одну наугад — отправить человека
        # проверять не то, а это дороже, чем признать неопределённость.
        return ("Telegram отказал в получении сообщений (конфликт). Причины "
                "две: где-то запущен второй бот с этим токеном или на токене "
                "стоит вебхук. Что именно — скажет строка ниже: "
                + POLLING["error"][:150])
    return POLLING["error"][:200]


class WatchPolling(logging.Handler):
    """Сделать молчание бота слышимым.

    aiogram **проглатывает любую ошибку** получения обновлений: пишет строчку
    в лог и повторяет попытку вечно. Процесс жив, порт слушает, health
    отвечает «ok» — а бот при этом глухой, и продавец видит просто тишину в
    ответ на `/start`. Логи контейнера он не читает и не должен.

    Поэтому мы слушаем сам логгер aiogram: ошибку получения обновлений
    записываем в `POLLING`, а продавцу пишем сообщением — отправка при этом
    работает, потому что конфликтует только `getUpdates`, а не `sendMessage`.
    """

    def __init__(self, bot):
        super().__init__(level=logging.INFO)
        self.bot = bot

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:                                  # pragma: no cover
            return
        now = _t.time()
        if record.levelno >= logging.ERROR and "Failed to fetch updates" in text:
            POLLING["error"] = text.split("- ", 1)[-1][:300]
            POLLING["error_at"] = now
            if not POLLING["failing_since"]:
                POLLING["failing_since"] = now
            self._tell(now)
        elif "Connection established" in text and POLLING["error"]:
            POLLING["error"] = ""
            POLLING["failing_since"] = 0.0
            POLLING["told_at"] = 0.0

    def _tell(self, now: float) -> None:
        """Сказать владельцу — один раз в десять минут, не чаще.

        Реже нельзя: пока это молчит, бот выглядит просто сломанным. Чаще —
        и чат завалит одинаковыми строчками, потому что попытка повторяется
        каждые несколько секунд.
        """
        if now - POLLING["told_at"] < 600:
            return
        POLLING["told_at"] = now
        why = polling_trouble()
        text = ("🔇 <b>Бот не получает сообщения</b>\n\n"
                f"{why}\n\n"
                "<i>Отправлять я по-прежнему могу — это сообщение тому "
                "доказательство. Не приходят именно входящие.</i>")
        try:
            import asyncio as _a
            from storage import OWNER_ID
            _a.create_task(self.bot.send_message(OWNER_ID, text))
        except Exception:                                  # pragma: no cover
            logger.exception("не смог сказать владельцу про молчание")


async def clear_webhook(bot) -> str:
    """Снять вебхук, если он остался на токене. Возвращает снятый адрес.

    Это самая незаметная причина немоты. Пока вебхук стоит, Telegram **не
    отдаёт обновления опросом вообще** — а отправка при этом работает, и со
    стороны выглядит так: уведомления о заказах приходят, а на `/start` бот
    не отвечает. Ровно этот случай и разбирался 17.08, причём подсказка про
    «второй экземпляр» уводила в сторону: сервер был один.

    Бот опрашивающий, поэтому оставшийся вебхук — всегда помеха, и он
    снимается. Но не молча: снятие вебхука меняет настройку токена, и
    продавец должен об этом узнать.

    `drop_pending_updates=False` намеренно: накопившиеся сообщения покупателей
    — это заказы, терять их нельзя.
    """
    try:
        info = await bot.get_webhook_info()
    except Exception as e:                                 # pragma: no cover
        logger.warning("не удалось спросить про вебхук: %s", e)
        return ""
    url = str(getattr(info, "url", "") or "")
    POLLING["webhook"] = url
    if not url:
        return ""

    pending = getattr(info, "pending_update_count", 0) or 0
    logger.error("На токене стоит вебхук %s — опрос с ним не работает. Снимаю.",
                 url)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:                                 # pragma: no cover
        logger.exception("вебхук снять не удалось: %s", e)
        return ""
    POLLING["webhook"] = ""

    text = ("🔌 <b>Снял вебхук с токена</b>\n\n"
            f"<code>{url[:120]}</code>\n\n"
            "Пока он стоял, Telegram не отдавал боту ни одного сообщения — "
            "при этом отправка работала, и со стороны это выглядело как "
            "«уведомления приходят, а на команды бот не отвечает».\n"
            f"Непрочитанных сообщений в очереди: <b>{pending}</b> — они не "
            "потеряны, заберу их опросом.")
    try:
        from storage import OWNER_ID
        await bot.send_message(OWNER_ID, text)
    except Exception:                                      # pragma: no cover
        logger.exception("не смог сказать владельцу про вебхук")
    return url


class NoticeUpdates:
    """Запоминает, когда бот в последний раз что-то получал.

    Без этого «жив» и «слышит» неразличимы: процесс может держать порт и
    отвечать на health, ничего не получая от Telegram.
    """

    async def __call__(self, handler, event, data):
        POLLING["last_update"] = _t.time()
        return await handler(event, data)


def _install_error_reporter(dp: Dispatcher) -> None:
    """Показывать сбой обработчика пользователю, а не только в логах контейнера.

    Необработанное исключение внутри обработчика aiogram просто пишет в лог.
    Для человека это выглядит как «кнопка не нажимается»: нажатие не
    подтверждено, экран не изменился, объяснений нет. Логи контейнера продавец
    не читает — и не должен.
    """
    import time as _time
    import html as _html
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

        # Текст ошибки — чужой: в нём бывает разметка, из-за которой она и
        # случилась. 19.08 отказ Telegram содержал обрывок тега, рассказ о
        # нём падал на том же самом обрыве, и продавец не увидел ни отчёта,
        # ни сообщения о сбое — экран просто замер.
        where = _html.escape(getattr(cq, "data", "") or "сообщение")
        why = _html.escape(str(exc)[:250] or type(exc).__name__)
        text = (f"⚠️ <b>Сбой</b>\n\n"
                f"Действие: <code>{where[:60]}</code>\n"
                f"Причина: <code>{why}</code>\n\n"
                "<i>Это сообщение вместо молчания: раньше такая ошибка просто "
                "ничего не делала.</i>")
        # Бот берётся из самого события, а не запоминается при установке:
        # запомненный — это лишний способ отправить ответ не туда.
        target = getattr(event.update, "bot", None) or getattr(cq, "bot", None)
        try:
            if cq is not None:
                await cq.answer()          # снять «часики» с кнопки
            if target is not None:
                try:
                    await target.send_message(user.id, text)
                except Exception:
                    # Последняя попытка — без разметки вовсе. Рассказ о
                    # сбое, падающий сам, оставляет продавца в тишине.
                    await target.send_message(user.id, text, parse_mode=None)
        except Exception:
            logger.warning("could not report error to %s: %s", user.id,
                           traceback.format_exc(limit=1))
        return True


class YooMarketMiddleware:
    """Кладёт в данные обработчика клиент Integration API этого продавца."""

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
    # Где живут незаконченные формы: Redis, если задан REDIS_URL (переживает
    # перезапуск), иначе память — там они пропадают вместе с процессом
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
    # Раньше всех: отметка «мы что-то получили» нужна и тогда, когда
    # сообщение никому не досталось.
    dp.message.outer_middleware(NoticeUpdates())
    dp.callback_query.outer_middleware(NoticeUpdates())
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.message.middleware(YooMarketMiddleware())
    dp.callback_query.middleware(YooMarketMiddleware())

    _install_error_reporter(dp)
    # Слушаем логгер aiogram: он единственный знает, что получение обновлений
    # не работает, и по умолчанию рассказывает об этом только логу.
    logging.getLogger("aiogram.dispatcher").addHandler(WatchPolling(bot))

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
    dp.include_router(approute.router)
    dp.include_router(plugins.router)
    dp.include_router(stats.router)
    dp.include_router(panel.router)
    dp.include_router(policy.router)
    # Команды-ярлыки. Позже разделов намеренно: их обработчики
    # вызываются отсюда напрямую, и роутер нужен только ради имён
    # команд, которых больше нигде нет.
    dp.include_router(commands.router)
    # Последним: ловит нажатия, которые не разобрал никто. Без него такое
    # нажатие молча пропадает, и это неотличимо от сломанной кнопки.
    dp.include_router(fallback.router)

    logger.info("Bot starting…")
    # До опроса: с вебхуком на токене getUpdates не работает вовсе, и бот
    # будет молчать, не подавая никаких признаков поломки.
    await clear_webhook(bot)
    await _start_health_server()  # для Koyeb/Render/Fly (health-check по $PORT)
    try:
        await task_manager.start_all()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


def health_payload() -> dict:
    """Что отдаёт `/health`. Вынесено отдельно, чтобы это можно было
    проверить тестом: от этого ответа зависит, сможет ли выкат отличить
    «код доехал» от «поднялась старая сборка»."""
    trouble = polling_trouble()
    last = POLLING["last_update"]
    return {
        # «Жив» и «слышит» — разные вещи. Процесс может держать порт и
        # отвечать сюда, ничего не получая от Telegram: именно так выглядит
        # запущенный дважды бот. Поэтому статус не всегда «ok».
        "status": "deaf" if trouble else "ok",
        "version": start.BOT_VERSION,
        "storage": "postgres" if os.environ.get("DATABASE_URL") else "files",
        "polling": trouble or "ok",
        "webhook": POLLING["webhook"] or "",
        "last_update_ago": (int(_t.time() - last) if last else None),
    }


async def _start_health_server() -> None:
    """Крошечный HTTP-сервер на $PORT: платформам, требующим открытый порт
    (Koyeb, Render, Fly), он говорит, что контейнер жив. Без `PORT` не
    поднимается вовсе.

    Отдаёт **версию бота**, и это не украшение. Правило проекта: после
    выката проверить, доехал ли код, — иначе «функция не работает» и «код не
    задеплоен» неразличимы. Пока версию показывал только `/version` в чате,
    проверить это мог лишь человек руками; теперь то же самое может сделать
    скрипт обновления (`scripts/deploy.sh`) и не отчитаться об успехе там,
    где поднялась старая сборка.
    """
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        from aiohttp import web
        app = web.Application()
        async def _ok(_req):
            # `ensure_ascii=False`: иначе версия с кириллицей уехала бы
            # экранированной, и выкат не узнал бы её при сверке — то есть
            # отчитался бы о неудаче там, где всё в порядке.
            return web.json_response(
                health_payload(),
                dumps=lambda o: json.dumps(o, ensure_ascii=False))
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
