"""Молчание бота должно быть слышимым.

17.08 продавец отправил `/start` четыре раза и не получил ничего. Процесс при
этом мог быть жив: aiogram **проглатывает любую ошибку** получения обновлений
— пишет строчку в лог и повторяет попытку вечно. Самый частый случай такой
ошибки в этом проекте известен заранее: бота запустили дважды с одним
токеном, и Telegram отдаёт сообщения только одному.

Со стороны продавца это неотличимо от «бот сломался»: порт слушается, health
отвечает «ok», логи контейнера он не читает и не должен.

Здесь проверяется, что бот об этом **говорит**: пишет владельцу (отправка
работает — конфликтует только получение), показывает в `/health` и в
`/version`. И что говорит не чаще, чем нужно: попытка повторяется каждые
несколько секунд, и без выдержки чат завалило бы одинаковыми строчками.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "1:x")

import main as M                                    # noqa: E402

CONFLICT = ("Failed to fetch updates - TelegramConflictError: "
            "Telegram server says - Conflict: terminated by other "
            "getUpdates request; make sure that only one bot instance is "
            "running")
NETWORK = ("Failed to fetch updates - TelegramNetworkError: "
           "HTTP Client says - Request timeout error")


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def record(text: str, level=logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("aiogram.dispatcher", level, __file__, 1, text,
                             None, None)


class WatchCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.was = dict(M.POLLING)
        M.POLLING.update({"last_update": 0.0, "error": "", "error_at": 0.0,
                          "failing_since": 0.0, "told_at": 0.0})
        self.bot = FakeBot()
        self.watch = M.WatchPolling(self.bot)

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    async def emit(self, text, level=logging.ERROR):
        self.watch.emit(record(text, level))
        await asyncio.sleep(0)      # даём отправке уйти в задачу
        return self.bot.sent


class ADeafBotSaysSoInsteadOfGoingQuiet(WatchCase):
    async def test_the_seller_is_told(self):
        """Раньше об этом знал только лог контейнера."""
        sent = await self.emit(CONFLICT)
        self.assertEqual(len(sent), 1)

    async def test_the_message_goes_to_the_owner(self):
        from storage import OWNER_ID
        sent = await self.emit(CONFLICT)
        self.assertEqual(sent[0][0], OWNER_ID)

    async def test_a_second_instance_is_named_in_plain_words(self):
        """«Conflict: terminated by other getUpdates request» на экране
        продавца — отписка. Он должен прочитать, что делать."""
        sent = await self.emit(CONFLICT)
        said = sent[0][1]
        self.assertIn("дважды", said)
        self.assertIn("токен", said)
        self.assertNotIn("terminated by other", said)

    async def test_the_message_explains_why_it_arrived_at_all(self):
        """Сообщение «я не получаю сообщений», которое пришло, выглядит
        противоречием — пока не сказано, что ломается только приём."""
        sent = await self.emit(CONFLICT)
        self.assertIn("Отправлять", sent[0][1])

    async def test_another_failure_is_reported_as_it_came(self):
        """Незнакомую причину переводить нечем — но и молчать о ней нельзя."""
        sent = await self.emit(NETWORK)
        self.assertIn("Request timeout", sent[0][1])


class ItDoesNotFloodTheChat(WatchCase):
    """Попытка повторяется каждые несколько секунд. Без выдержки чат завалило
    бы одинаковыми строчками, и продавец выключил бы уведомления."""

    async def test_the_same_trouble_is_told_once(self):
        await self.emit(CONFLICT)
        await self.emit(CONFLICT)
        await self.emit(CONFLICT)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_after_the_pause_it_reminds(self):
        await self.emit(CONFLICT)
        M.POLLING["told_at"] = time.time() - 601
        await self.emit(CONFLICT)
        self.assertEqual(len(self.bot.sent), 2)

    async def test_a_recovery_resets_the_silence(self):
        """Связь восстановилась — следующий сбой должен быть слышен сразу,
        а не через десять минут."""
        await self.emit(CONFLICT)
        await self.emit("Connection established (tryings = 3, bot id = 1)",
                        logging.INFO)
        self.assertEqual(M.POLLING["error"], "")
        await self.emit(CONFLICT)
        self.assertEqual(len(self.bot.sent), 2)

    async def test_ordinary_log_lines_are_ignored(self):
        await self.emit("Run polling for bot @x id=1", logging.INFO)
        await self.emit("Update id=1 is handled.", logging.INFO)
        self.assertEqual(self.bot.sent, [])


class HealthTellsAliveApartFromHearing(unittest.TestCase):
    """«Жив» и «слышит» — разные вещи. Процесс держит порт и отвечает на
    health, ничего не получая от Telegram: именно так выглядит бот,
    запущенный дважды."""

    def setUp(self):
        self.was = dict(M.POLLING)

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    def test_a_healthy_bot_says_ok(self):
        M.POLLING.update({"error": "", "last_update": time.time()})
        got = M.health_payload()
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["polling"], "ok")

    def test_a_deaf_bot_does_not_say_ok(self):
        """Текст берётся полностью, как его присылает Telegram: по обрезанному
        причину не различить, и ответ будет «одна из двух» — а это уже другое
        поведение, проверяемое в test_webhook.py."""
        M.POLLING.update({"error": "TelegramConflictError: Telegram server "
                                   "says - Conflict: terminated by other "
                                   "getUpdates request"})
        got = M.health_payload()
        self.assertNotEqual(got["status"], "ok")
        self.assertIn("дважды", got["polling"])

    def test_it_says_how_long_ago_anything_arrived(self):
        M.POLLING.update({"error": "", "last_update": time.time() - 42})
        self.assertGreaterEqual(M.health_payload()["last_update_ago"], 42)

    def test_nothing_received_yet_is_not_reported_as_zero(self):
        """Ноль означал бы «только что», а это ровно наоборот."""
        M.POLLING.update({"error": "", "last_update": 0.0})
        self.assertIsNone(M.health_payload()["last_update_ago"])

    def test_the_version_is_still_there_for_the_deploy(self):
        """Выкат сверяет версию по этому же ответу — она не должна пропасть
        из-за нового поля."""
        from handlers import start
        self.assertEqual(M.health_payload()["version"], start.BOT_VERSION)


class ReceivingAnythingIsRemembered(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.was = dict(M.POLLING)
        M.POLLING["last_update"] = 0.0

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    async def test_a_passing_message_marks_the_time(self):
        async def handler(event, data):
            return "готово"

        got = await M.NoticeUpdates()(handler, object(), {})
        self.assertEqual(got, "готово")
        self.assertGreater(M.POLLING["last_update"], 0)

    async def test_the_mark_is_set_even_if_nobody_handled_it(self):
        """Отметка нужна о получении, а не об обработке: сообщение, которое
        никому не досталось, всё равно доказывает, что приём работает."""
        async def handler(event, data):
            return None

        await M.NoticeUpdates()(handler, object(), {})
        self.assertGreater(M.POLLING["last_update"], 0)


class TheWatcherIsWiredIn(unittest.TestCase):
    def test_it_listens_to_the_logger_that_knows(self):
        import inspect
        src = inspect.getsource(M.main)
        self.assertIn('logging.getLogger("aiogram.dispatcher")', src)
        self.assertIn("WatchPolling", src)

    def test_receiving_is_marked_before_the_routers(self):
        import inspect
        self.assertIn("NoticeUpdates()", inspect.getsource(M.main))

    def test_the_deploy_reports_a_deaf_bot_too(self):
        """Выкат может пройти, версия совпасть — а бот молчать."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "scripts", "deploy.sh")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"polling"', text)
        self.assertIn("НЕ получает сообщения", text)


if __name__ == "__main__":
    unittest.main()
