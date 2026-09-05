"""Вебхук на токене — самая незаметная причина немоты бота.

Пока он стоит, Telegram **не отдаёт обновления опросом вообще**, а отправка
при этом работает. Со стороны это выглядит так: уведомления о заказах
приходят, а на `/start` бот молчит — ровно то, что продавец прислал 17.08.

Отдельный урок оттуда же: обе причины отказа приходят как 409 Conflict, и
первая версия перевода уверенно называла одну из них — «у вас запущен второй
бот». Человеку с единственным сервером это отправляло искать несуществующее.
Догадка, поданная как факт, дороже, чем признанная неопределённость.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "1:x")

import main as M                                    # noqa: E402

WEBHOOK_409 = ("Failed to fetch updates - TelegramConflictError: Telegram "
               "server says - Conflict: can't use getUpdates method while "
               "webhook is active")
TWIN_409 = ("Failed to fetch updates - TelegramConflictError: Telegram "
            "server says - Conflict: terminated by other getUpdates request")
VAGUE_409 = ("Failed to fetch updates - TelegramConflictError: Telegram "
             "server says - Conflict: something new")


class Info:
    def __init__(self, url="", pending=0):
        self.url = url
        self.pending_update_count = pending


class FakeBot:
    def __init__(self, info=None, fail_delete=False):
        self.info = info or Info()
        self.fail_delete = fail_delete
        self.deleted: list = []
        self.sent: list = []

    async def get_webhook_info(self):
        return self.info

    async def delete_webhook(self, drop_pending_updates=None):
        if self.fail_delete:
            raise RuntimeError("нельзя")
        self.deleted.append(drop_pending_updates)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class ALeftoverWebhookIsRemovedNotEndured(unittest.TestCase):
    def setUp(self):
        self.was = dict(M.POLLING)

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    def test_a_webhook_is_taken_off(self):
        bot = FakeBot(Info("https://example.org/hook", 7))
        got = asyncio.run(M.clear_webhook(bot))
        self.assertEqual(got, "https://example.org/hook")
        self.assertEqual(len(bot.deleted), 1)

    def test_pending_messages_are_not_dropped(self):
        """Накопившиеся сообщения покупателей — это заказы."""
        bot = FakeBot(Info("https://example.org/hook", 7))
        asyncio.run(M.clear_webhook(bot))
        self.assertIn(bot.deleted[0], (False, None))

    def test_the_seller_is_told_because_it_changes_the_token(self):
        bot = FakeBot(Info("https://example.org/hook", 7))
        asyncio.run(M.clear_webhook(bot))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("вебхук", bot.sent[0][1].lower())

    def test_the_message_says_how_many_were_waiting(self):
        """«Не потеряны» — утверждение, которое надо подкрепить числом."""
        bot = FakeBot(Info("https://example.org/hook", 7))
        asyncio.run(M.clear_webhook(bot))
        self.assertIn("7", bot.sent[0][1])

    def test_without_a_webhook_nothing_happens(self):
        bot = FakeBot(Info(""))
        self.assertEqual(asyncio.run(M.clear_webhook(bot)), "")
        self.assertEqual(bot.deleted, [])
        self.assertEqual(bot.sent, [])

    def test_a_failed_removal_is_not_reported_as_success(self):
        bot = FakeBot(Info("https://example.org/hook"), fail_delete=True)
        self.assertEqual(asyncio.run(M.clear_webhook(bot)), "")
        self.assertEqual(bot.sent, [])

    def test_it_runs_before_polling_starts(self):
        """Снятый после начала опроса вебхук уже не поможет: опрос будет
        отказывать с первой секунды."""
        import inspect
        src = inspect.getsource(M.main)
        self.assertIn("clear_webhook(bot)", src)
        self.assertLess(src.index("clear_webhook(bot)"),
                        src.index("start_polling"))


class TheTwoReasonsForSilenceAreNotConfused(unittest.TestCase):
    """Обе приходят как 409 и лечатся противоположными действиями."""

    def setUp(self):
        self.was = dict(M.POLLING)

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    def why(self, error):
        M.POLLING["error"] = error
        return M.polling_trouble()

    def test_a_webhook_conflict_names_the_webhook_and_only_it(self):
        """Не «одна из двух причин», а именно эта: Telegram сказал прямо.
        Предлагать заодно поискать второй экземпляр — значит послать человека
        делать лишнюю работу."""
        said = self.why(WEBHOOK_409)
        self.assertIn("вебхук", said)
        self.assertNotIn("дважды", said)
        self.assertNotIn("второй", said)

    def test_a_second_instance_names_the_second_instance(self):
        said = self.why(TWIN_409)
        self.assertIn("дважды", said)
        self.assertNotIn("вебхук", said)

    def test_an_unclear_conflict_admits_both_are_possible(self):
        """Назвать одну наугад — отправить человека проверять не то. Это уже
        случилось: продавцу с одним сервером бот сказал «запущен дважды»."""
        said = self.why(VAGUE_409)
        self.assertIn("вебхук", said)
        self.assertIn("второй", said)
        self.assertIn("something new", said)

    def test_a_plain_network_failure_is_passed_through(self):
        said = self.why("Failed to fetch updates - TimeoutError: too slow")
        self.assertIn("too slow", said)
        self.assertNotIn("вебхук", said)

    def test_no_error_means_no_complaint(self):
        self.assertEqual(self.why(""), "")


class HealthShowsTheWebhookToo(unittest.TestCase):
    def setUp(self):
        self.was = dict(M.POLLING)

    def tearDown(self):
        M.POLLING.clear()
        M.POLLING.update(self.was)

    def test_a_standing_webhook_is_visible(self):
        M.POLLING.update({"error": "", "webhook": "https://example.org/hook"})
        self.assertEqual(M.health_payload()["webhook"],
                         "https://example.org/hook")

    def test_none_left_is_shown_as_empty(self):
        M.POLLING.update({"error": "", "webhook": ""})
        self.assertEqual(M.health_payload()["webhook"], "")


if __name__ == "__main__":
    unittest.main()
