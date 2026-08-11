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

    def test_the_action_is_named_in_russian_too(self):
        self.assertIn("подтвердить", self._why())

    def test_it_does_not_send_the_seller_to_do_it_by_hand(self):
        self.assertIn("ни вручную", self._why())

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
