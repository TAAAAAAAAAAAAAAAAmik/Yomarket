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


class WhatWasAnnouncedIsRemembered(unittest.TestCase):
    """save_settings стоял последней строкой try. Стоило упасть чему-нибудь
    после рассылки — и всё уже разосланное не записывалось: следующий проход
    считал заказы и письма новыми и уведомлял по второму разу."""

    class API:
        def __init__(self):
            self.calls = 0

        async def start(self):
            pass

        async def close(self):
            pass

        async def get_orders(self, cursor=None):
            return {"data": [{"id": "1200750", "status": "paid",
                              "title": "50 звёзд", "price": 60,
                              "chat_id": "1139042"}]}

        async def get_order(self, oid):
            return {"data": {"id": str(oid), "status": "paid",
                             "title": "50 звёзд", "price": 60,
                             "chat_id": "1139042"}}

        async def get_messages(self, cid):
            return {"data": []}

        async def send_message(self, cid, text):
            return {"ok": True}

    def _run(self, *, blow_up: bool):
        import tasks.manager as MM
        saved: list = []
        real_api, real_save = MM.YooMarketAPI, MM.save_settings
        api = self.API()
        MM.YooMarketAPI = lambda token=None: api

        def save(uid, s):
            saved.append(dict(s.get("known_orders") or {}))

        MM.save_settings = save
        tm = MM.TaskManager.__new__(MM.TaskManager)
        tm.notes = []

        async def notify(uid, text, **kw):
            tm.notes.append(text)

        tm._notify = notify

        async def boom(*a, **kw):
            raise RuntimeError("панель не ответила")

        async def quiet(*a, **kw):
            return None

        tm._check_messages = boom if blow_up else quiet
        tm._check_watched_chats = quiet
        tm._auto_confirm = quiet
        tm._restore_after_sale = quiet
        s = {"known_orders": {"999": "paid"}, "known_order_details": {},
             "orders_initialized": True, "notify_orders": {"enabled": True},
             "auto_reply": {"enabled": False},
             "plugins": {"auto_stars": {"enabled": False}}}
        try:
            asyncio.run(tm._process_orders(1, "tok", s))
        except Exception:
            pass
        finally:
            MM.YooMarketAPI, MM.save_settings = real_api, real_save
        return saved, tm.notes

    def test_a_quiet_pass_records_the_order(self):
        saved, notes = self._run(blow_up=False)
        self.assertTrue(any("НОВАЯ ПОКУПКА" in n for n in notes))
        self.assertIn("1200750", saved[-1])

    def test_a_failure_afterwards_still_records_it(self):
        """Иначе покупка объявляется заново на каждом проходе."""
        saved, notes = self._run(blow_up=True)
        self.assertTrue(any("НОВАЯ ПОКУПКА" in n for n in notes))
        self.assertTrue(saved, "настройки не сохранились вовсе")
        self.assertIn("1200750", saved[-1])


class OnlyOneProcessSendsPerShop(unittest.TestCase):
    """Сколько процессов запущено, столько копий уведомления и приходит.
    Команды при этом отвечают как обычно — апдейт Telegram достаётся
    только одному, — поэтому изнутри бота это неотличимо от ошибки."""

    def test_the_first_process_takes_the_lease(self):
        from tasks.manager import _claim_sender
        s = {}
        self.assertTrue(_claim_sender(s, now=1000.0, instance="aaa"))
        self.assertEqual(s["_sender"]["inst"], "aaa")

    def test_a_second_process_stays_quiet(self):
        from tasks.manager import _claim_sender
        s = {}
        _claim_sender(s, now=1000.0, instance="aaa")
        self.assertFalse(_claim_sender(s, now=1010.0, instance="bbb"))

    def test_the_owner_keeps_renewing(self):
        from tasks.manager import _claim_sender
        s = {}
        _claim_sender(s, now=1000.0, instance="aaa")
        self.assertTrue(_claim_sender(s, now=1120.0, instance="aaa"))
        self.assertEqual(s["_sender"]["ts"], 1120.0)

    def test_an_abandoned_lease_is_taken_over(self):
        """Упавший контейнер не должен заткнуть бота навсегда."""
        from tasks.manager import _claim_sender, _SENDER_LEASE_TTL
        s = {}
        _claim_sender(s, now=1000.0, instance="aaa")
        later = 1000.0 + _SENDER_LEASE_TTL + 1
        self.assertTrue(_claim_sender(s, now=later, instance="bbb"))
        self.assertEqual(s["_sender"]["inst"], "bbb")

    def test_a_silenced_tick_does_no_work_at_all(self):
        """Молчать — значит и не опрашивать: иначе второй процесс тратит
        лимиты маркетплейса и переписывает чужие отметки."""
        import tasks.manager as MM
        tm = MM.TaskManager.__new__(MM.TaskManager)
        tm._locks = {}
        touched: list[str] = []

        async def boom(*a, **kw):
            touched.append("polled")

        tm._process_orders = boom
        tm._check_reminders = boom
        tm._maybe_bump_schedule = boom
        real_token, real_get = MM.get_token, MM.get_settings
        MM.get_token = lambda uid: "tok"
        MM.get_settings = lambda uid: {
            "_sender": {"inst": "someone-else", "ts": __import__("time").time()}}
        try:
            asyncio.run(tm._tick(1))
        finally:
            MM.get_token, MM.get_settings = real_token, real_get
        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
