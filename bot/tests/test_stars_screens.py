"""Четыре экрана AutoStars, которые раньше отвечали «в следующем обновлении».

Кнопка-заглушка хуже отсутствующей: продавец на неё рассчитывает. Здесь
проверяется, что каждая из четырёх делает обещанное — и делает по данным, а
не по прикидке.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import plugins as P          # noqa: E402
from tasks import manager as M             # noqa: E402


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


class CB:
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self):
        self.data: dict = {}

    async def set_state(self, s):
        pass

    async def clear(self):
        pass

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def buttons(kb) -> list[str]:
    return [] if kb is None else [b.text for row in kb.inline_keyboard
                                  for b in row]


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


def base_settings(**stars) -> dict:
    p = {"enabled": True, "amount": 50, "pending": {}, "delivered": [],
         "stuck": {}, "log": [], "notify": {}, "texts": {}}
    p.update(stars)
    return {"plugins": {"auto_stars": p}, "known_order_details": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.store = base_settings()
        P.get_settings = lambda uid: self.store
        P.save_settings = lambda uid, s: None

    def run_(self, coro):
        return asyncio.run(coro)


class NothingSaysComingSoon(Base):
    """Ни один из четырёх экранов больше не отговаривается обновлением."""

    def test_all_four_answer_with_a_screen(self):
        for handler in (P.stars_accumulated, P.stars_profit, P.stars_notifs):
            cb = CB("x")
            self.run_(handler(cb))
            self.assertTrue(cb.message.texts, handler.__name__)
            for alert in cb.alerts:
                self.assertNotIn("следующем обновлении", alert, handler.__name__)
        cb = CB("x")
        self.run_(P.stars_replies(cb, FSM()))
        self.assertTrue(cb.message.texts)


class Accumulated(Base):
    def test_an_empty_queue_says_so(self):
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("Пусто", cb.message.last)

    def test_orders_waiting_for_a_username_are_listed(self):
        self.store = base_settings(pending={
            "77": {"quantity": 100, "asked_at": time.time() - 7200}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("#77", cb.message.last)
        self.assertIn("100⭐", cb.message.last)
        self.assertIn("Ждут ник", cb.message.last)

    def test_a_failed_order_can_be_handed_out_from_here(self):
        """У сорвавшегося есть ник — значит выдать можно прямо сейчас."""
        self.store = base_settings(stuck={
            "88": {"username": "vasya", "quantity": 50,
                   "reason": "Bad request", "ts": time.time()}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("Сорвались", cb.message.last)
        self.assertIn("plugins:stars:retry:88", callbacks(cb.message.kbs[-1]))

    def test_the_reason_it_failed_is_shown(self):
        self.store = base_settings(stuck={
            "88": {"username": "v", "quantity": 50,
                   "reason": "нет прав", "ts": time.time()}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("нет прав", cb.message.last)

    def test_waiting_orders_get_no_hand_out_button(self):
        """Ника нет — выдавать некому, кнопка вела бы в никуда."""
        self.store = base_settings(pending={"77": {"quantity": 100}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertFalse([c for c in callbacks(cb.message.kbs[-1])
                          if "retry" in c])


class RetryingAFailedOrder(Base):
    def setUp(self):
        super().setUp()
        self.store = base_settings(stuck={
            "88": {"username": "vasya", "quantity": 50,
                   "reason": "Bad request", "ts": time.time()}})
        P.get_fragment_creds = lambda uid: {"cookies": {"a": "b"},
                                            "mnemonic": "w " * 24}
        P.save_settings = lambda uid, s: self.store.update(s)
        import automation.fragment as FR
        self.fr = FR
        self._buy = FR.buy_stars_sync

    def tearDown(self):
        self.fr.buy_stars_sync = self._buy

    def _buy_returns(self, ok, msg, ton=0.08):
        def fake(cookies, mnemonic, username, qty, wallet_version="v4r2",
                 api_hash="", wait_confirm=True, report=None):
            if report is not None and ok:
                report["ton"] = ton
            return ok, msg
        self.fr.buy_stars_sync = fake

    def test_a_successful_retry_clears_the_order(self):
        self._buy_returns(True, "готово")
        self.run_(P.stars_retry(CB("plugins:stars:retry:88")))
        p = self.store["plugins"]["auto_stars"]
        self.assertNotIn("88", p["stuck"])
        self.assertIn("88", p["delivered"])

    def test_and_records_what_it_cost(self):
        self._buy_returns(True, "готово", ton=0.0812)
        self.run_(P.stars_retry(CB("plugins:stars:retry:88")))
        log = self.store["plugins"]["auto_stars"]["log"]
        self.assertEqual(len(log), 1, log)
        self.assertAlmostEqual(log[0]["ton"], 0.0812, places=5)
        self.assertEqual(log[0]["qty"], 50)

    def test_a_failed_retry_keeps_the_order_in_the_list(self):
        """Неудачная попытка — не повод потерять заказ."""
        self._buy_returns(False, "снова Bad request")
        cb = CB("plugins:stars:retry:88")
        self.run_(P.stars_retry(cb))
        self.assertIn("88", self.store["plugins"]["auto_stars"]["stuck"])
        self.assertIn("Bad request", cb.message.last)

    def test_missing_credentials_are_named_before_anything_is_tried(self):
        P.get_fragment_creds = lambda uid: {}
        cb = CB("plugins:stars:retry:88")
        self.run_(P.stars_retry(cb))
        self.assertTrue(any("Fragment" in a for a in cb.alerts), cb.alerts)

    def test_an_order_that_is_already_gone_is_not_bought_twice(self):
        self._buy_returns(True, "готово")
        cb = CB("plugins:stars:retry:404")
        self.run_(P.stars_retry(cb))
        self.assertFalse(self.store["plugins"]["auto_stars"]["log"])


class Profit(Base):
    def test_with_no_deliveries_it_does_not_invent_numbers(self):
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("нечего считать", cb.message.last)

    def test_it_counts_stars_roubles_and_ton(self):
        now = time.time()
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "revenue": 139.0,
             "ts": now - 100},
            {"order_id": "2", "qty": 50, "ton": 0.08, "revenue": 79.0,
             "ts": now - 200},
        ])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        text = cb.message.last
        self.assertIn("150⭐", text, text)
        self.assertIn("218 ₽", text, text)
        self.assertIn("0.2400 TON", text, text)

    def test_yesterday_is_not_counted_as_today(self):
        now = time.time()
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "ts": now - 200},
            {"order_id": "2", "qty": 500, "ton": 0.80, "ts": now - 3 * 86400},
        ])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        day = cb.message.last.split("За 30 дней")[0]
        self.assertIn("100⭐", day, day)
        self.assertNotIn("600⭐", day, day)

    def test_it_does_not_pretend_to_know_the_ton_rate(self):
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "revenue": 139.0,
             "ts": time.time()}])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("курс TON он не знает", cb.message.last)

    def test_an_order_without_a_known_price_does_not_break_the_sum(self):
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "ts": time.time()},
            {"order_id": "2", "qty": 50, "ton": 0.08, "revenue": 79.0,
             "ts": time.time()}])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("79 ₽", cb.message.last)


class Notifications(Base):
    def test_every_kind_can_be_switched_off(self):
        cb = CB("x")
        self.run_(P.stars_notifs(cb))
        data = callbacks(cb.message.kbs[-1])
        for key in ("done", "failed", "low_balance"):
            self.assertIn(f"plugins:stars:ntog:{key}", data)

    def test_toggling_sticks(self):
        saved = {}
        P.save_settings = lambda uid, s: saved.update(s)
        self.run_(P.stars_notif_toggle(CB("plugins:stars:ntog:done")))
        self.assertIs(self.store["plugins"]["auto_stars"]["notify"]["done"],
                      False)
        self.assertTrue(saved)

    def test_the_manager_obeys_the_switch(self):
        self.assertTrue(M.stars_notify_on(self.store, "done"))
        self.store["plugins"]["auto_stars"]["notify"]["done"] = False
        self.assertFalse(M.stars_notify_on(self.store, "done"))

    def test_an_unset_switch_means_on(self):
        self.assertTrue(M.stars_notify_on({"plugins": {"auto_stars": {}}},
                                          "failed"))


class Replies(Base):
    def test_the_standard_texts_are_shown(self):
        cb = CB("x")
        self.run_(P.stars_replies(cb, FSM()))
        self.assertIn("Запрос ника", cb.message.last)
        self.assertIn("стандартный", cb.message.last)

    def test_a_custom_text_replaces_the_standard_one(self):
        saved = {}
        P.save_settings = lambda uid, s: saved.update(s)
        st = FSM()
        st.data["reply_key"] = "done"
        msg = Screen()
        msg.text = "Спасибо! {qty}⭐ уже у @{username}"
        msg.from_user = type("U", (), {"id": 1})()
        self.run_(P.stars_reply_save(msg, st))
        self.assertEqual(
            M.stars_text(self.store, "done", qty=100, username="vasya"),
            "Спасибо! 100⭐ уже у @vasya")

    def test_a_dash_restores_the_standard(self):
        self.store["plugins"]["auto_stars"]["texts"]["done"] = "своё"
        st = FSM()
        st.data["reply_key"] = "done"
        msg = Screen()
        msg.text = "-"
        msg.from_user = type("U", (), {"id": 1})()
        self.run_(P.stars_reply_save(msg, st))
        self.assertIn("Готово", M.stars_text(self.store, "done", qty=1,
                                             username="x"))

    def test_a_typo_in_a_placeholder_does_not_kill_the_delivery(self):
        """Текст пишет человек — {опечатка} не должна ронять выдачу."""
        self.store["plugins"]["auto_stars"]["texts"]["done"] = "Готово {колво}⭐"
        out = M.stars_text(self.store, "done", qty=100, username="v")
        self.assertEqual(out, "Готово {колво}⭐")

    def test_the_standard_ask_still_carries_no_at_sign(self):
        """Иначе бот вычитает «@username» из своего же вопроса как ответ."""
        self.assertNotIn("@", M.stars_text({}, "ask"))


if __name__ == "__main__":
    unittest.main()
