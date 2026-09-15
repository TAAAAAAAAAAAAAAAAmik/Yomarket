"""Заявка на вывод подтверждается журналом панели, а не кодом ответа.

Первое правило проекта: HTTP 200 — не доказательство. Здесь оно стоит
дороже всего: «бот сказал «отправлено», а в панели пусто» — это чужие
деньги, и чеклист называет это самым опасным расхождением.

Обратное враньё запрещено тоже. Заявка на выплату баланс сразу не
уменьшает и в журнале появляется не мгновенно, поэтому «не вижу» нельзя
подавать как «не создана».
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import balance as B          # noqa: E402
import stats_source as S                   # noqa: E402


class Readback(unittest.TestCase):
    def setUp(self):
        self._events = S.events_for
        self._invalidate = S.invalidate
        S.invalidate = lambda uid: None

    def tearDown(self):
        S.events_for = self._events
        S.invalidate = self._invalidate

    def _say(self, answer):
        async def events_for(uid, settings=None, force=False):
            if isinstance(answer, Exception):
                raise answer
            return answer

        S.events_for = events_for
        return asyncio.run(B._payout_readback(1, time.time() - 5))


class WhenTheLedgerConfirmsIt(Readback):
    def test_a_fresh_payout_row_is_proof(self):
        got = self._say(([{"kind": "payout", "ts": time.time(),
                           "amount": 500}], "panel", ""))
        self.assertIn("видна в журнале панели", got)

    def test_an_old_payout_row_is_not(self):
        """Иначе прошлая выплата подтвердит любую новую."""
        got = self._say(([{"kind": "payout", "ts": time.time() - 9999,
                           "amount": 500}], "panel", ""))
        self.assertIn("пока не видна", got)

    def test_other_rows_are_not_mistaken_for_a_payout(self):
        got = self._say(([{"kind": "sale", "ts": time.time(),
                           "amount": 500}], "panel", ""))
        self.assertIn("пока не видна", got)


class WhenItIsNotThereYet(Readback):
    def test_it_is_not_called_a_failure(self):
        """Заявка появляется в журнале не сразу — объявить провал значит
        толкнуть продавца на второй вывод тех же денег."""
        got = self._say(([], "panel", ""))
        self.assertNotIn("не создана", got.replace(
            "вывод не создан, и это надо решать в панели", ""))
        self.assertIn("появляется не сразу", got)

    def test_it_says_what_to_do_and_when(self):
        got = self._say(([], "panel", ""))
        self.assertIn("через несколько минут", got)


class WhenTheLedgerCannotBeRead(Readback):
    def test_a_local_source_is_not_passed_off_as_the_panel(self):
        """История бота о выплатах не знает: молчание в ней ничего не значит."""
        got = self._say(([], "local", "сессия панели истекла"))
        self.assertIn("недоступен", got)
        self.assertIn("сессия панели истекла", got)

    def test_a_crash_does_not_swallow_the_answer(self):
        got = self._say(RuntimeError("сеть легла"))
        self.assertIn("перечитать не удалось", got)
        self.assertIn("сеть легла", got)

    def test_neither_case_claims_the_payout_went_through(self):
        for answer in (([], "local", ""), RuntimeError("нет сети")):
            got = self._say(answer)
            self.assertNotIn("✅", got, got)


class TheHandlerActuallyUsesIt(unittest.TestCase):
    def test_the_readback_is_wired_into_the_payout(self):
        """Иначе всё вышеописанное — мёртвый код рядом с настоящими деньгами."""
        import inspect
        src = inspect.getsource(B.wd_execute)
        self.assertIn("_payout_readback", src)

    def test_it_runs_only_after_a_success(self):
        """На провале перечитывать нечего, а лишний запрос к панели на
        сорвавшемся выводе только путает отчёт."""
        import inspect
        src = inspect.getsource(B.wd_execute)
        self.assertIn("if ok else", src)

    def test_the_moment_before_the_request_is_remembered(self):
        """Без отметки времени старая выплата подтвердит новую."""
        import inspect
        src = inspect.getsource(B.wd_execute)
        self.assertIn("started = time.time()", src)


if __name__ == "__main__":
    unittest.main()
