"""Итоги дня собираются и, если не собрались, об этом говорят.

Сводка падала на `st['refunded']` — в `summarize` это поле называется
`refunds`. KeyError ловился строкой выше и уходил в лог контейнера, так что
итоги дня не приходили никогда и молча: продавец решал, что за день не было
продаж. Пункт B5 чеклиста ровно про этот случай, и юнит-теста на текст
сводки не было вовсе.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import stats_source as S              # noqa: E402
import tasks.manager as M             # noqa: E402


class Report(unittest.TestCase):
    def setUp(self):
        self._events = S.events_for
        self._balance = M.shop_balance
        self.settings = {"daily_report": {"enabled": True, "hour": 0}}

        async def balance(uid, api):
            return 1500.0, "1 500 ₽"

        M.shop_balance = balance

    def tearDown(self):
        S.events_for = self._events
        M.shop_balance = self._balance

    def _text(self, events):
        async def events_for(uid, settings=None, force=False):
            return events, "panel", ""

        S.events_for = events_for
        tm = M.TaskManager.__new__(M.TaskManager)
        return asyncio.run(tm._daily_report_text(1, None, self.settings))

    @staticmethod
    def sale(amount=500.0):
        return {"kind": "sale", "ts": time.time(), "amount": amount}

    @staticmethod
    def refund():
        return {"kind": "refund", "ts": time.time(), "amount": 0.0}


class TheSummaryIsBuiltAtAll(Report):
    def test_a_day_with_a_refund_does_not_crash(self):
        """Именно на этом сводка и падала — на дне, где был возврат."""
        got = self._text([self.sale(), self.refund()])
        self.assertIn("Возвраты: 1", got)

    def test_a_day_without_refunds_does_not_mention_them(self):
        got = self._text([self.sale()])
        self.assertNotIn("Возвраты", got)

    def test_the_revenue_is_the_days_revenue(self):
        got = self._text([self.sale(500), self.sale(300)])
        self.assertIn("800", got)

    def test_the_balance_line_is_filled_not_a_dash(self):
        got = self._text([self.sale()])
        self.assertIn("1 500 ₽", got)

    def test_an_empty_day_is_still_a_report(self):
        got = self._text([])
        self.assertIn("Выручка", got)

    def test_yesterdays_sale_is_not_counted_into_today(self):
        old = {"kind": "sale", "ts": time.time() - 3 * 86400, "amount": 999.0}
        got = self._text([self.sale(500), old])
        self.assertIn("500", got)
        self.assertNotIn("999", got)


class WhenTheSummaryCannotBeBuilt(unittest.TestCase):
    """Отчёт, который не пришёл, продавцу невидим: он решит, что за день не
    было продаж. Так и жила опечатка — в логе контейнера её никто не читал."""

    def test_the_seller_is_told_once_a_day_not_only_the_log(self):
        import inspect
        src = inspect.getsource(M.TaskManager._tick_auto_locked)
        self.assertIn("Итоги дня не собрались", src)
        self.assertIn("last_fail_day", src)

    def test_the_reason_is_shown_not_swallowed(self):
        import inspect
        src = inspect.getsource(M.TaskManager._tick_auto_locked)
        self.assertIn("Причина:", src)

    def test_it_says_the_numbers_themselves_are_safe(self):
        """Иначе продавец решит, что потерялась выручка, а не сводка."""
        import inspect
        src = inspect.getsource(M.TaskManager._tick_auto_locked)
        self.assertIn("Цифры за день целы", src)


class TheKeyNamesMatchTheirSource(unittest.TestCase):
    """Опечатка в имени поля не ловится ничем, кроме сверки с источником."""

    def test_summarize_has_no_field_called_refunded(self):
        keys = set(S.summarize([]))
        self.assertIn("refunds", keys)
        self.assertNotIn("refunded", keys)

    def test_the_report_reads_only_fields_that_exist(self):
        import inspect
        import re
        src = inspect.getsource(M.TaskManager._daily_report_text)
        used = set(re.findall(r"st\[['\"](\w+)['\"]\]", src))
        self.assertTrue(used, "сводка вообще перестала читать summarize")
        self.assertLessEqual(used, set(S.summarize([])), used)


if __name__ == "__main__":
    unittest.main()
