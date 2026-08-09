"""Уведомление уходит один раз.

У дублей две разные причины, и лечатся они по-разному. Первая в коде:
при ошибке отправки бот слал сообщение повторно без разметки — но ошибкой
считался и таймаут, а при таймауте сообщение обычно уже доставлено.
Вторая снаружи: два контейнера с одним токеном ведут каждый свой фоновый
цикл. Отличить их можно только по метке процесса в /version, поэтому она
и появилась.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from tasks.manager import TaskManager, _is_formatting_error   # noqa: E402


class Bot:
    def __init__(self, fail_with: Exception | None = None,
                 fail_times: int = 1):
        self.fail_with = fail_with
        self.left = fail_times
        self.sent: list[dict] = []

    async def send_message(self, uid, text, parse_mode=None, reply_markup=None):
        if self.fail_with is not None and self.left > 0:
            self.left -= 1
            raise self.fail_with
        self.sent.append({"text": text, "parse_mode": parse_mode})
        return {"ok": True}


def notify(bot: Bot, text: str = "<b>Заказ</b> #1") -> Bot:
    tm = TaskManager.__new__(TaskManager)
    tm.bot = bot
    asyncio.run(tm._notify(1, text))
    return bot


class AFormattingErrorIsWorthRetryingPlain(unittest.TestCase):
    """Ровно так падало сообщение с «<ник>» внутри: Telegram отверг всю
    отправку, покупатель и продавец не получили ничего."""

    def test_the_second_try_goes_out_without_tags(self):
        bot = notify(Bot(RuntimeError(
            'Bad Request: can\'t parse entities: Unsupported start tag "ник"')))
        self.assertEqual(len(bot.sent), 1)
        self.assertNotIn("<b>", bot.sent[0]["text"])

    def test_and_it_carries_the_words(self):
        bot = notify(Bot(RuntimeError("can't parse entities")))
        self.assertIn("Заказ", bot.sent[0]["text"])


class ATimeoutIsNotWorthRetrying(unittest.TestCase):
    """Разрыв связи не значит «не дошло»: сообщение обычно уже доставлено,
    и повтор приходит продавцу вторым — без разметки."""

    def test_a_timeout_sends_nothing_more(self):
        bot = notify(Bot(TimeoutError("read timeout")))
        self.assertEqual(bot.sent, [], "это и есть второй дубль")

    def test_a_dropped_connection_does_not_double(self):
        bot = notify(Bot(ConnectionError("connection reset by peer")))
        self.assertEqual(bot.sent, [])

    def test_a_network_hiccup_does_not_double(self):
        bot = notify(Bot(OSError("network is unreachable")))
        self.assertEqual(bot.sent, [])


class TheErrorIsJudgedByItsText(unittest.TestCase):
    def test_parse_failures_are_recognised(self):
        for text in ("Bad Request: can't parse entities",
                     'Unsupported start tag "ник" at byte offset 458',
                     "can't find end tag corresponding to start tag b",
                     "Unclosed start tag at byte offset 12"):
            self.assertTrue(_is_formatting_error(RuntimeError(text)), text)

    def test_transport_failures_are_not(self):
        for exc in (TimeoutError("timed out"),
                    ConnectionError("reset by peer"),
                    OSError("unreachable"),
                    RuntimeError("Too Many Requests: retry after 5")):
            self.assertFalse(_is_formatting_error(exc), repr(exc))


class ASuccessfulSendIsSentOnce(unittest.TestCase):
    def test_no_retry_when_nothing_failed(self):
        bot = notify(Bot())
        self.assertEqual(len(bot.sent), 1)

    def test_the_formatting_survives(self):
        bot = notify(Bot())
        self.assertEqual(bot.sent[0]["parse_mode"], "HTML")
        self.assertIn("<b>", bot.sent[0]["text"])


class TheProcessIsIdentifiable(unittest.TestCase):
    """Два контейнера с одним токеном — уведомления идут дважды, а команды
    отвечают как обычно: апдейт Telegram отдаёт только одному из них.
    Без метки процесса это неотличимо от ошибки в коде."""

    def test_version_carries_a_process_mark(self):
        from handlers.start import INSTANCE_ID
        self.assertTrue(INSTANCE_ID)
        self.assertEqual(len(INSTANCE_ID), 6)

    def test_the_screen_explains_what_a_changing_mark_means(self):
        import inspect
        import handlers.start as S
        src = inspect.getsource(S.cmd_version)
        self.assertIn("INSTANCE_ID", src)
        self.assertIn("несколько ботов", src)


if __name__ == "__main__":
    unittest.main()
