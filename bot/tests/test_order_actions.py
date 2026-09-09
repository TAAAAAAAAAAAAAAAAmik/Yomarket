"""Отказ маркетплейса по заказу объясняется и запоминается.

На тестовом заказе кнопка «✅ Подтвердить» ответила
«Incorrect Order Status (incorrect_status)». Две беды разом: английский код
ошибки на экране продавца — отписка, а кнопка, которая в этом статусе
заведомо не сработает, — обещание невозможного, то же самое, что совет
ответить в закрытый чат.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                  # noqa: E402
from autoreply import explain_error             # noqa: E402
from keyboards.main import order_actions_keyboard  # noqa: E402
import handlers.orders as O                     # noqa: E402


def presses(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data]


class TheCodeIsTranslated(unittest.TestCase):
    def test_incorrect_status_is_said_in_russian(self):
        why, fixable = explain_error("Incorrect Order Status (incorrect_status)")
        self.assertNotIn("incorrect", why.lower())
        self.assertIn("статус", why)

    def test_it_is_not_offered_as_something_to_fix_by_hand(self):
        """Статус заказа продавец руками не переставит."""
        _why, fixable = explain_error("incorrect_status")
        self.assertFalse(fixable)

    def test_an_unknown_code_still_comes_through(self):
        why, _f = explain_error("some_new_code")
        self.assertIn("some_new_code", why)


class TheRefusalNamesTheActualStatus(unittest.TestCase):
    """«Заказ сейчас в другом статусе» без названия статуса — половина
    ответа: продавцу всё равно идти смотреть на сайт."""

    def setUp(self):
        self.saved = {}
        self._get = storage.get_settings
        self._save = storage.save_settings
        storage.get_settings = lambda uid: self.saved
        storage.save_settings = lambda uid, s: self.saved.update(s)

    def tearDown(self):
        storage.get_settings = self._get
        storage.save_settings = self._save

    class API:
        def __init__(self, status="refunded", boom=False):
            self.status, self.boom = status, boom

        async def get_order(self, oid):
            if self.boom:
                raise RuntimeError("сеть")
            return {"data": {"id": oid, "status": self.status}}

    def _why(self, err="incorrect_status", **kw):
        return asyncio.run(
            O._why_refused(1, self.API(**kw), "77", "confirm", err))

    def test_the_status_is_read_back_and_named(self):
        got = self._why()
        self.assertIn("Возврат", got)

    def test_the_reason_fits_the_situation(self):
        """«Заказ в другом статусе» одинаково описывает подтверждённый,
        закрытый и неоплаченный — а делать с ними надо разное."""
        self.assertIn("оформлен возврат", self._why())

    def test_an_unknown_status_gets_the_general_wording(self):
        """Незнакомый статус не подгоняется под знакомый."""
        got = self._why(status="quantum_superposition")
        self.assertIn("ни вручную", got)

    def test_another_error_is_left_alone(self):
        """Дочитывать заказ на каждую ошибку — лишний запрос и лишний шум."""
        self.assertEqual(self._why(err="too_many_requests"), "")

    def test_an_unreadable_order_adds_nothing_rather_than_guessing(self):
        self.assertEqual(self._why(boom=True), "")

    def test_the_refusal_is_remembered(self):
        self._why()
        self.assertIn("refunded",
                      self.saved.get("action_refusals", {}).get("confirm", []))

    def test_the_same_refusal_is_not_stored_twice(self):
        self._why()
        self._why()
        self.assertEqual(
            self.saved["action_refusals"]["confirm"].count("refunded"), 1)


class EachRefusalIsExplainedByItsOwnSituation(unittest.TestCase):
    """Продавцу нужно разное действие в каждом случае, а фраза была одна."""

    def _say(self, status):
        return O.refusal_reason("confirm", status)

    def test_an_already_confirmed_order_says_so(self):
        self.assertIn("уже подтверждён", self._say("confirmed"))

    def test_a_completed_order_is_not_offered_for_confirming_twice(self):
        self.assertIn("уже подтверждён", self._say("success"))

    def test_a_refunded_order_says_the_money_went_back(self):
        self.assertIn("возврат", self._say("refunded"))

    def test_a_cancelled_order_says_it_is_closed(self):
        self.assertIn("закрыт", self._say("cancelled"))

    def test_an_unpaid_order_says_there_is_no_money_yet(self):
        got = self._say("new")
        self.assertIn("не оплачен", got)
        self.assertNotIn("уже подтверждён", got)

    def test_an_unknown_status_is_not_dressed_up_as_a_known_one(self):
        got = self._say("нечто")
        self.assertIn("ни вручную", got)


class AnUnpaidOrderIsWarnedAboutBeforeTheRequest(unittest.TestCase):
    """Подтвердить неоплаченный — заявить, что сделка закрыта, пока денег
    нет. Узнавать об этом из английского кода после нажатия поздно.

    Но это предупреждение, а не запрет: оплата могла дойти между проходами
    опроса, и решает продавец.
    """

    def setUp(self):
        self.state = {"known_orders": {"77": "new"}}
        self._get = O.get_settings
        O.get_settings = lambda uid: self.state

    def tearDown(self):
        O.get_settings = self._get

    class CB:
        def __init__(self):
            self.text = ""
            self.kb = None
            self.message = self

        async def edit_text(self, text, reply_markup=None, **kw):
            self.text, self.kb = text, reply_markup

        async def answer(self, *a, **kw):
            pass

        from_user = type("U", (), {"id": 1})()

    class API:
        def __init__(self):
            self.confirmed = []

        async def confirm_order(self, oid):
            self.confirmed.append(oid)
            return {"ok": True}

    def _press(self, action="confirm"):
        from keyboards.main import OrderCallback
        cb, api = self.CB(), self.API()
        asyncio.run(O.handle_order_action(
            cb, OrderCallback(order_id="77", action=action), api))
        return cb, api

    def test_nothing_is_sent_to_the_marketplace_yet(self):
        _cb, api = self._press()
        self.assertEqual(api.confirmed, [])

    def test_the_seller_is_told_why(self):
        cb, _api = self._press()
        self.assertIn("не оплачен", cb.text)

    def test_a_way_through_is_offered(self):
        """Запрет здесь был бы неправ: деньги могли дойти только что."""
        cb, _api = self._press()
        self.assertTrue(any("confirm_anyway" in b.callback_data
                            for row in cb.kb.inline_keyboard for b in row),
                        cb.kb)

    def test_insisting_actually_sends_it(self):
        from keyboards.main import OrderCallback
        cb, api = self.CB(), self.API()
        asyncio.run(O.confirm_anyway(
            cb, OrderCallback(order_id="77", action="confirm_anyway"), api))
        self.assertEqual(api.confirmed, ["77"])

    def test_a_paid_order_goes_straight_through(self):
        self.state["known_orders"]["77"] = "paid"
        _cb, api = self._press()
        self.assertEqual(api.confirmed, ["77"])

    def test_an_order_the_poller_never_saw_is_not_blocked(self):
        """Молчание памяти — не доказательство неоплаты."""
        self.state["known_orders"] = {}
        _cb, api = self._press()
        self.assertEqual(api.confirmed, ["77"])

    def test_a_refund_is_not_held_up_by_this(self):
        """Возврат неоплаченного — как раз нормальный случай."""
        cb, _api = self._press(action="refund")
        self.assertNotIn("не оплачен", cb.text)


class AButtonThatCannotWorkIsNotOffered(unittest.TestCase):
    """Кнопка, которая в этом статусе ответит отказом, — то же обещание
    невозможного, что и «ответьте вручную» в закрытый чат."""

    REFUSED = {"confirm": ["refunded"]}

    def _has(self, action, **kw):
        kb = order_actions_keyboard("77", "99", **kw)
        return any(f"a={action}" in p or f":{action}" in p or action in p
                   for p in presses(kb))

    def test_confirm_is_hidden_in_a_status_that_refused_it(self):
        self.assertFalse(self._has("confirm", status="refunded",
                                   refused=self.REFUSED))

    def test_it_stays_in_a_status_that_never_refused(self):
        self.assertTrue(self._has("confirm", status="paid",
                                  refused=self.REFUSED))

    def test_a_refusal_for_one_action_does_not_hide_another(self):
        self.assertTrue(self._has("refund", status="refunded",
                                  refused=self.REFUSED))

    def test_with_no_observations_nothing_is_hidden(self):
        """Догадка не повод убирать кнопку: список пополняется только
        настоящим отказом."""
        self.assertTrue(self._has("confirm", status="refunded"))

    def test_the_chat_and_back_buttons_always_stay(self):
        kb = order_actions_keyboard("77", "99", "refunded",
                                    {"confirm": ["refunded"],
                                     "refund": ["refunded"]})
        self.assertGreaterEqual(len(presses(kb)), 2)


if __name__ == "__main__":
    unittest.main()
