"""Очередь «ждём @ник» не должна глушить чат навсегда.

Пока заказ стоит в очереди AutoStars, ЛЮБОЕ письмо покупателя в его чате
считается ответом с ником: оно не доходит ни до уведомления продавцу, ни
до правил автоответа. Для возвращённого заказа ждать нечего, а перехват
раньше оставался — и продавец видел ровно тишину.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import autoreply as ar                                        # noqa: E402
from tasks.manager import TaskManager, _stars_drop_if_closed   # noqa: E402


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


class FakeAPI:
    def __init__(self, rows=None, send_error: str = ""):
        self.rows = rows or []
        self.send_error = send_error
        self.sent: list[str] = []

    async def get_messages(self, cid):
        return {"data": self.rows}

    async def send_message(self, cid, text):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.sent.append(text)
        return {"ok": True}


def manager() -> TaskManager:
    tm = TaskManager.__new__(TaskManager)
    tm.notes: list[str] = []

    async def _notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = _notify
    return tm


def settings(status="refunded") -> dict:
    return {
        "known_orders": {"77": status},
        "known_messages": {"77": "1"},
        "known_order_details": {"77": {"chat_id": "99", "seen_at": time.time(),
                                       "title": "50 ЗВЁЗД"}},
        "notify_messages": {"enabled": False},
        "autoreplies": {
            "enabled": True,
            "rules": [{"id": "r1", "on": True, "keywords": ["долго"],
                       "text": "Извините за ожидание."}],
            "cooldown_min": 0, "max_per_order": 10,
        },
        "plugins": {"auto_stars": {
            "enabled": True, "amount": 50,
            "pending": {"77": {"quantity": 50, "chat_id": "99",
                               "asked_at": time.time() - 3 * 86400,
                               "title": "50 ЗВЁЗД"}},
        }},
    }


LETTER = [{"id": "5", "sender_type": "buyer", "text": "долго",
           "created_at": stamp(120)}]


class AClosedOrderLetsGoOfTheChat(unittest.TestCase):
    def _run(self, status="refunded"):
        s = settings(status)
        api = FakeAPI(LETTER)
        tm = manager()
        asyncio.run(tm._check_messages(1, api, s))
        return s, api, tm.notes

    def test_the_order_leaves_the_queue(self):
        s, _api, _notes = self._run()
        self.assertEqual(s["plugins"]["auto_stars"]["pending"], {})

    def test_the_letter_reaches_the_rules_instead_of_being_eaten(self):
        _s, api, _notes = self._run()
        self.assertEqual(api.sent, ["Извините за ожидание."], api.sent)

    def test_the_seller_is_told_the_order_was_dropped(self):
        _s, _api, notes = self._run()
        self.assertTrue(any("СНЯТ С ОЧЕРЕДИ" in n for n in notes), notes)

    def test_a_completed_order_is_released_too(self):
        s, _api, _notes = self._run("success")
        self.assertEqual(s["plugins"]["auto_stars"]["pending"], {})

    def test_a_live_order_keeps_waiting_for_its_username(self):
        """Оплаченный заказ ник ещё ждёт — снимать его нельзя."""
        s, api, _notes = self._run("paid")
        self.assertIn("77", s["plugins"]["auto_stars"]["pending"])
        self.assertIn("Не разобрал ник", " ".join(api.sent))

    def test_and_its_letter_does_not_reach_the_rules(self):
        _s, api, _notes = self._run("paid")
        self.assertNotIn("Извините за ожидание.", api.sent)


class TheSweepClearsClosedOrdersWithoutWaitingForAnyLetter(unittest.TestCase):
    def _sweep(self, status="refunded"):
        s = settings(status)
        tm = manager()
        note = asyncio.run(tm._stars_pending_sweep(1, FakeAPI(), s, time.time()))
        return s, note

    def test_a_refunded_order_is_swept_out(self):
        s, _note = self._sweep()
        self.assertEqual(s["plugins"]["auto_stars"]["pending"], {})

    def test_the_sweep_reports_what_it_removed(self):
        _s, note = self._sweep()
        self.assertIn("СНЯТЫ С ОЧЕРЕДИ", note)
        self.assertIn("#77", note)

    def test_a_paid_order_survives_the_sweep(self):
        s, _note = self._sweep("paid")
        self.assertIn("77", s["plugins"]["auto_stars"]["pending"])

    def test_an_order_the_bot_knows_nothing_about_is_left_alone(self):
        """Неизвестный статус — не повод выбрасывать оплаченный заказ."""
        s = settings("paid")
        s["known_orders"] = {}
        self.assertEqual(_stars_drop_if_closed(s, "77"), "")
        self.assertIn("77", s["plugins"]["auto_stars"]["pending"])


class AFailedRequestForTheUsernameIsNotSwallowed(unittest.TestCase):
    """Покупатель не получал ни звёзд, ни просьбы прислать ник, а продавец —
    ни строчки: исход этой отправки не смотрели вовсе."""

    def _run(self):
        s = settings("paid")
        api = FakeAPI(LETTER, send_error="no_active_orders_in_chat")
        tm = manager()
        asyncio.run(tm._check_messages(1, api, s))
        return tm.notes

    def test_the_seller_hears_about_it(self):
        notes = self._run()
        self.assertTrue(any("НЕ СПРОСИЛ НИК" in n for n in notes), notes)

    def test_the_reason_is_in_russian(self):
        notes = self._run()
        note = next(n for n in notes if "НЕ СПРОСИЛ НИК" in n)
        self.assertIn("закрыл чат", note)
        self.assertNotIn("no_active_orders", note)


class AutoConfirmDoesNotReportWhatWasNotDelivered(unittest.TestCase):
    """«Участник магазина сообщил, что выполнил заказ ✅» — это заявление
    перед покупателем и площадкой. Заказ, стоящий в очереди на выдачу
    звёзд, товара не получил: выдача падает, деньги уходят продавцу,
    покупатель идёт в арбитраж."""

    class ConfirmAPI:
        def __init__(self):
            self.confirmed: list[str] = []

        async def confirm_order(self, oid):
            self.confirmed.append(str(oid))
            return {"ok": True}

    def _run(self, *, pending: bool, times: int = 1):
        s = settings("paid")
        s["known_orders"]["77"] = "work"
        s["known_order_details"]["77"]["work_at"] = time.time() - 48 * 3600
        s["auto_confirm"] = {"enabled": True, "hours": 24}
        if not pending:
            s["plugins"]["auto_stars"]["pending"] = {}
        api = self.ConfirmAPI()
        tm = manager()
        for _ in range(times):
            asyncio.run(tm._auto_confirm(1, api, s))
        return s, api, tm.notes

    def test_an_order_awaiting_stars_is_not_confirmed(self):
        _s, api, _notes = self._run(pending=True)
        self.assertEqual(api.confirmed, [])

    def test_the_seller_is_told_why(self):
        _s, _api, notes = self._run(pending=True)
        self.assertTrue(any("ТОВАР НЕ ВЫДАН" in n for n in notes), notes)

    def test_the_warning_is_not_repeated_every_pass(self):
        _s, _api, notes = self._run(pending=True, times=4)
        held = [n for n in notes if "ТОВАР НЕ ВЫДАН" in n]
        self.assertEqual(len(held), 1, held)

    def test_an_order_with_nothing_pending_is_confirmed(self):
        _s, api, _notes = self._run(pending=False)
        self.assertEqual(api.confirmed, ["77"])

    def test_the_confirmation_says_what_triggered_it(self):
        """Отличить его от подтверждения из AutoStars и от ручного нажатия
        было нельзя — источник записи в чате оставался загадкой."""
        _s, _api, notes = self._run(pending=False)
        note = next(n for n in notes if "Авто-подтверждение" in n)
        self.assertIn("больше 24 ч", note)


    def test_the_held_mark_is_cleared_once_the_queue_lets_go(self):
        """Возвращённый заказ подтверждения не дождётся — без уборки его
        отметка осталась бы в настройках навсегда."""
        s, _api, _notes = self._run(pending=True)
        self.assertIn("77", s["auto_confirm"]["held"])
        s["plugins"]["auto_stars"]["pending"] = {}
        asyncio.run(manager()._auto_confirm(1, self.ConfirmAPI(), s))
        self.assertEqual(s["auto_confirm"]["held"], {})

    def test_a_second_hold_warns_again_after_that(self):
        """Уборка не должна превращаться в повторное молчание: если заказ
        снова попал в очередь, продавцу об этом говорят."""
        s, _api, _notes = self._run(pending=True)
        s["plugins"]["auto_stars"]["pending"] = {}
        asyncio.run(manager()._auto_confirm(1, self.ConfirmAPI(), s))
        s["known_orders"]["77"] = "work"
        s["plugins"]["auto_stars"]["pending"] = {"77": {"quantity": 50}}
        tm = manager()
        asyncio.run(tm._auto_confirm(1, self.ConfirmAPI(), s))
        self.assertTrue(any("ТОВАР НЕ ВЫДАН" in n for n in tm.notes), tm.notes)

    def test_a_disabled_plugin_holds_nothing_back(self):
        """Выключённая автовыдача ничего не ждёт — заказ подтверждается."""
        s = settings("paid")
        s["known_orders"]["77"] = "work"
        s["known_order_details"]["77"]["work_at"] = time.time() - 48 * 3600
        s["auto_confirm"] = {"enabled": True, "hours": 24}
        s["plugins"]["auto_stars"]["enabled"] = False
        api = self.ConfirmAPI()
        asyncio.run(manager()._auto_confirm(1, api, s))
        self.assertEqual(api.confirmed, ["77"])


if __name__ == "__main__":
    unittest.main()
