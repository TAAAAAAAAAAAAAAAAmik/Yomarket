"""Автопринятие берёт заказ в работу — и только тот, который можно.

Три беды разом. Функция срабатывала лишь на первом появлении заказа, а
появляется он часто неоплаченным: деньги доходили следующим проходом, и
второго шанса не было — при включённом тумблере не срабатывало ничего.
В списке разрешённых статусов при этом стояли «new» и «created», то есть
неоплаченные: панель на такой заказ прямо предупреждает «не выдавайте
товар». А статус после действия бот назначал себе сам, хотя что делает
`POST /orders/{id}/work` на этом маркетплейсе — не выяснено.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import tasks.manager as M      # noqa: E402


class API:
    """Заказ, который может поменять статус между проходами."""

    def __init__(self, statuses, after="work"):
        self.statuses = list(statuses)
        self.after = after
        self.worked: list[str] = []
        self.pass_no = -1

    def _order(self, status):
        return {"id": "1200750", "status": status, "title": "50 звёзд",
                "price": 60, "chat_id": "1139042"}

    async def start(self):
        self.pass_no += 1

    async def close(self):
        pass

    async def get_orders(self, cursor=None):
        idx = min(self.pass_no, len(self.statuses) - 1)
        return {"data": [self._order(self.statuses[idx])]}

    async def get_order(self, oid):
        # После work_order отдаём то, что маркетплейс сделал на самом деле.
        if self.worked:
            return {"data": self._order(self.after)}
        idx = min(self.pass_no, len(self.statuses) - 1)
        return {"data": self._order(self.statuses[idx])}

    async def work_order(self, oid):
        self.worked.append(str(oid))
        return {"ok": True}

    async def get_messages(self, cid):
        return {"data": []}

    async def send_message(self, cid, text):
        return {"ok": True}


def run(statuses, *, after="work", enabled=True, extra=None, passes=None):
    api = API(statuses, after)
    real_api, real_save = M.YooMarketAPI, M.save_settings
    M.YooMarketAPI = lambda token=None: api
    M.save_settings = lambda uid, s: None
    tm = M.TaskManager.__new__(M.TaskManager)
    tm.notes: list[str] = []

    async def notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = notify
    s = {"known_orders": {"999": "paid"}, "known_order_details": {},
         "orders_initialized": True,
         "auto_accept": {"enabled": enabled},
         "notify_orders": {"enabled": True},
         "auto_reply": {"enabled": False},
         "plugins": {"auto_stars": {"enabled": False}}}
    s.update(extra or {})
    try:
        for _ in range(passes if passes is not None else len(statuses)):
            asyncio.run(tm._process_orders(1, "tok", s))
    finally:
        M.YooMarketAPI, M.save_settings = real_api, real_save
    return s, api, tm.notes


class OnlyAPaidOrderIsTakenIntoWork(unittest.TestCase):
    def test_a_paid_order_is_taken(self):
        _s, api, _n = run(["paid"])
        self.assertEqual(api.worked, ["1200750"])

    def test_an_unpaid_one_is_left_alone(self):
        """Панель на неоплаченный заказ прямо пишет «не выдавайте товар»."""
        for status in ("created", "new", "pending"):
            _s, api, _n = run([status])
            self.assertEqual(api.worked, [], status)

    def test_a_disabled_switch_takes_nothing(self):
        _s, api, _n = run(["paid"], enabled=False)
        self.assertEqual(api.worked, [])


class PaymentArrivingLaterStillCounts(unittest.TestCase):
    """Заказ увиден неоплаченным, деньги дошли следующим проходом. Раньше
    автопринятие жило только в ветке «увиден впервые» — второго шанса не
    было, и при включённом тумблере не срабатывало ничего."""

    def test_it_is_taken_when_the_money_arrives(self):
        _s, api, _n = run(["created", "paid"])
        self.assertEqual(api.worked, ["1200750"])

    def test_and_only_once(self):
        _s, api, _n = run(["created", "paid", "paid"])
        self.assertEqual(len(api.worked), 1, api.worked)


class TheStatusIsReadBackNotAssumed(unittest.TestCase):
    def test_an_ordinary_outcome_is_recorded(self):
        s, _api, _n = run(["paid"], after="work")
        self.assertEqual(s["known_orders"]["1200750"], "work")

    def test_a_surprising_outcome_is_recorded_as_it_is(self):
        s, _api, _n = run(["paid"], after="success")
        self.assertEqual(s["known_orders"]["1200750"], "success")

    def test_the_card_shows_what_really_happened(self):
        _s, _api, notes = run(["paid"], after="success")
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertIn("взят в работу автоматически", card)
        self.assertIn("Выполнен", card)


class ATakeThatMeansFulfilmentIsNoticed(unittest.TestCase):
    """Если после «взять в работу» заказ сразу «выполнен» — на этом
    маркетплейсе это не «начал заниматься», а заявление покупателю, что
    товар выдан. Это наблюдение, а не догадка: записываем и меняем
    поведение только после него."""

    def test_the_seller_is_warned(self):
        _s, _api, notes = run(["paid"], after="success")
        self.assertTrue(any("ОТЧЁТ О ВЫДАЧЕ" in n for n in notes), notes)

    def test_it_is_remembered(self):
        s, _api, _n = run(["paid"], after="success")
        self.assertTrue(s["auto_accept"]["means_fulfilled"])

    def test_an_ordinary_outcome_changes_nothing(self):
        s, _api, notes = run(["paid"], after="work")
        self.assertFalse(s["auto_accept"].get("means_fulfilled"))
        self.assertFalse([n for n in notes if "ОТЧЁТ О ВЫДАЧЕ" in n])

    def test_the_warning_comes_once_not_every_order(self):
        s, _api, notes = run(["paid"], after="success")
        again = run(["paid"], after="success",
                    extra={"auto_accept": dict(s["auto_accept"])})[2]
        self.assertFalse([n for n in again if "ОТЧЁТ О ВЫДАЧЕ" in n], again)

    def test_afterwards_an_order_awaiting_stars_is_left_alone(self):
        """Отчитаться о выдаче за невыданный товар нельзя — покупатель
        пойдёт в арбитраж."""
        _s, api, _n = run(
            ["paid"],
            extra={"auto_accept": {"enabled": True, "means_fulfilled": True},
                   "plugins": {"auto_stars": {
                       "enabled": True,
                       "pending": {"1200750": {"quantity": 50}}}}})
        self.assertEqual(api.worked, [])

    def test_but_an_ordinary_order_is_still_taken(self):
        _s, api, _n = run(
            ["paid"],
            extra={"auto_accept": {"enabled": True, "means_fulfilled": True},
                   "plugins": {"auto_stars": {"enabled": True, "pending": {}}}})
        self.assertEqual(api.worked, ["1200750"])

    def test_before_the_observation_nothing_is_held_back(self):
        """Пока не подтверждено — не мешаем работать: гипотеза не повод
        выключать функцию, за которую заплатили."""
        _s, api, _n = run(
            ["paid"],
            extra={"auto_accept": {"enabled": True},
                   "plugins": {"auto_stars": {
                       "enabled": True,
                       "pending": {"1200750": {"quantity": 50}}}}})
        self.assertEqual(api.worked, ["1200750"])


class AFailedTakeDoesNotPretend(unittest.TestCase):
    def test_a_refusal_leaves_the_order_as_it_was(self):
        class Broken(API):
            async def work_order(self, oid):
                raise RuntimeError("panel refused")

        api = Broken(["paid"])
        real_api, real_save = M.YooMarketAPI, M.save_settings
        M.YooMarketAPI = lambda token=None: api
        M.save_settings = lambda uid, s: None
        tm = M.TaskManager.__new__(M.TaskManager)
        tm.notes = []

        async def notify(uid, text, **kw):
            tm.notes.append(text)

        tm._notify = notify
        s = {"known_orders": {"999": "paid"}, "known_order_details": {},
             "orders_initialized": True, "auto_accept": {"enabled": True},
             "notify_orders": {"enabled": True},
             "auto_reply": {"enabled": False},
             "plugins": {"auto_stars": {"enabled": False}}}
        try:
            asyncio.run(tm._process_orders(1, "tok", s))
        finally:
            M.YooMarketAPI, M.save_settings = real_api, real_save
        self.assertEqual(s["known_orders"]["1200750"], "paid")
        card = next(n for n in tm.notes if "НОВАЯ ПОКУПКА" in n)
        self.assertNotIn("взят в работу автоматически", card)


if __name__ == "__main__":
    unittest.main()
