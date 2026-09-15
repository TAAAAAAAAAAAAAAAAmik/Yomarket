"""Списки статусов — одни на весь бот, а не свои в каждом файле.

Продавцу пришло напоминание:

    ⏰ Напоминание о заказе
    🧾 Заказ #1205518
    📊 Статус: ✅ Выполнен
    ⏳ Ждёт подтверждения уже 48 ч
    [✅ Подтвердить]  [🔄 Возврат]

Одно сообщение утверждает обе вещи сразу: заказ выполнен и заказ ждёт
подтверждения. Причина — пять рукописных списков статусов в разных файлах,
и ни в одном не было **`success`**, которым этот маркетплейс помечает
выполненный заказ.

Общий список в `orderfields` для этого и заведён, и рядом с его импортом в
`tasks/manager.py` даже стоял комментарий про «success». Применили его в
одном месте и не применили в остальных — а такие правки в этом проекте
живут ровно до следующего экрана.

Из одного корня росло сразу пять бед:

* напоминание про выполненный заказ (то, что увидел продавец);
* «✅ ЗАКАЗ ВЫПОЛНЕН» не приходило вовсе;
* автоответ покупателю «заказ подтверждён» не отправлялся;
* фильтр «Выполненные» показывал пусто при полном магазине выполненных;
* возвраты не считались, если маркетплейс назвал их `refund` или
  `expired`, а не `refunded`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from orderfields import BACK, DONE, status_ru      # noqa: E402

BOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TheStatusThisMarketplaceActuallyUses(unittest.TestCase):
    def test_success_counts_as_done(self):
        """Он и стоял в том напоминании как «✅ Выполнен»."""
        self.assertIn("success", DONE)

    def test_and_reads_as_done_on_screen(self):
        self.assertIn("Выполнен", status_ru("success"))

    def test_the_other_spellings_are_there_too(self):
        for st in ("completed", "complete", "done", "confirmed", "closed",
                   "delivered"):
            self.assertIn(st, DONE, st)

    def test_refunds_cover_more_than_one_word(self):
        for st in ("refunded", "returned", "refund", "cancelled", "canceled",
                   "rejected", "failed", "expired"):
            self.assertIn(st, BACK, st)

    def test_done_and_back_do_not_overlap(self):
        """Статус, попавший в оба, вёл бы себя по-разному в разных экранах."""
        self.assertEqual(set(DONE) & set(BACK), set())


class NoFileKeepsItsOwnList(unittest.TestCase):
    """Рукописный список — это следующая та же поломка через месяц."""

    FILES = ("tasks/manager.py", "handlers/orders.py", "handlers/balance.py",
             "handlers/stats.py", "stats_source.py")

    def literals(self, path):
        """Кортежи и списки строк, похожие на перечень статусов."""
        tree = ast.parse((BOT / path).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Tuple, ast.List)):
                continue
            values = [e.value for e in node.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(values) >= 2:
                out.append(values)
        return out

    def test_nobody_writes_out_the_done_statuses_again(self):
        marks = {"confirmed", "completed", "done"}
        for path in self.FILES:
            for values in self.literals(path):
                low = {v.lower() for v in values}
                self.assertFalse(
                    marks <= low,
                    f"{path}: свой список выполненных {values} — возьмите "
                    f"DONE из orderfields")

    def test_nor_the_refund_ones(self):
        marks = {"refunded", "cancelled", "returned"}
        for path in self.FILES:
            for values in self.literals(path):
                low = {v.lower() for v in values}
                self.assertFalse(
                    marks <= low,
                    f"{path}: свой список возвратов {values} — возьмите "
                    f"BACK из orderfields")


class TheFivePlacesThatUsedTheirOwn(unittest.TestCase):
    def source(self, path):
        return (BOT / path).read_text(encoding="utf-8")

    def test_the_reminder_skips_a_finished_order(self):
        src = self.source("tasks/manager.py")
        i = src.index("async def _check_reminders")
        block = src[i:i + 2500]
        self.assertIn("_DONE_STATUSES", block)
        self.assertIn("_BACK_STATUSES", block)

    def test_the_done_notification_uses_the_shared_list(self):
        self.assertIn("elif prev_status != status and status in _DONE_STATUSES:",
                      self.source("tasks/manager.py"))

    def test_the_refund_notification_too(self):
        self.assertIn("elif prev_status != status and status in _BACK_STATUSES:",
                      self.source("tasks/manager.py"))

    def test_the_done_filter_uses_it(self):
        self.assertIn("done_statuses = DONE", self.source("handlers/orders.py"))

    def test_the_refund_filter_uses_it(self):
        self.assertIn("refund_statuses = BACK",
                      self.source("handlers/orders.py"))


class TheReminderItself(unittest.TestCase):
    """Проверяется следствие, а не текст: пришло ли напоминание вообще."""

    def setUp(self):
        import asyncio
        import tasks.manager as M
        self.M, self.asyncio = M, asyncio
        self.sent: list = []
        self.saved: dict = {}

        class Fake(M.TaskManager):
            def __init__(inner):
                pass

            async def _notify(inner, uid, text, reply_markup=None, **kw):
                self.sent.append(text)

        self.mgr = Fake()
        self._save = M.save_settings
        M.save_settings = lambda uid, s: self.saved.update(s)

    def tearDown(self):
        self.M.save_settings = self._save

    def run_with(self, status):
        import time
        settings = {
            "reminders": {"enabled": True, "hours": 24},
            "known_orders": {"1205518": status},
            "known_order_details": {
                "1205518": {"seen_at": time.time() - 48 * 3600,
                            "title": "Аккаунт", "buyer": "stepapolin5"}},
        }
        self.sent.clear()
        self.asyncio.run(self.mgr._check_reminders(1, settings))
        return self.sent

    def test_a_finished_order_gets_no_reminder(self):
        """Ровно тот заказ, о котором продавцу написали в 02:08."""
        self.assertEqual(self.run_with("success"), [])

    def test_nor_a_refunded_one(self):
        self.assertEqual(self.run_with("expired"), [])

    def test_but_one_still_in_work_does(self):
        got = self.run_with("work")
        self.assertTrue(got)
        self.assertIn("48 ч", got[0])

    def test_and_an_unpaid_one_does_too(self):
        """Висящий неоплаченным заказ — как раз то, о чём напомнить стоит."""
        self.assertTrue(self.run_with("new"))


if __name__ == "__main__":
    unittest.main()
