"""Когда суммы нет, видно почему — со всеми попытками сразу.

Баланс читается двумя путями: страница магазина, а если там не нашлось —
ресурс `balances`. Продавец увидел «Панель: вход есть — панель не показала
сумму». Эта фраза выбрасывала разбор первой попытки, где как раз
перечислены поля, найденные на странице магазина, — то есть ровно то, ради
чего причину и печатают. Разбираться пришлось бы вслепую ещё круг.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import balance as B          # noqa: E402
import storage                             # noqa: E402
from automation import panel as P          # noqa: E402

SHOP_FIELDS = "12: поля — Название, Владелец, Сумма продаж"


class Reason(unittest.TestCase):
    def setUp(self):
        self._creds = storage.get_panel_creds
        self._shop = P.panel_shop_balance_sync
        self._rows = P.panel_balances_sync
        storage.get_panel_creds = lambda uid: {"cookies": "c"}
        B.get_panel_creds = storage.get_panel_creds

    def tearDown(self):
        storage.get_panel_creds = self._creds
        P.panel_shop_balance_sync = self._shop
        P.panel_balances_sync = self._rows

    def run_it(self, shop, rows):
        self.asked = []

        def fake_shop(ck, sid="", shop_name=""):
            self.asked.append((sid, shop_name))
            return shop

        P.panel_shop_balance_sync = fake_shop
        P.panel_balances_sync = lambda ck: rows
        return asyncio.run(B._panel_balance(1))


class WhenTheShopPageHasNoBalanceField(Reason):
    ROWS_NO_AMOUNT = (True, [{"id": "3", "currency": "RUB", "amount": "",
                              "raw": {"fields": [
                                  {"name": "Валюта"}, {"name": "Тип"}]}}])

    def test_the_shop_pages_own_reason_survives(self):
        """Именно её и выбрасывали: в ней перечислены найденные поля."""
        shown, err = self.run_it((False, SHOP_FIELDS), self.ROWS_NO_AMOUNT)
        self.assertIsNone(shown)
        self.assertIn("Сумма продаж", err)

    def test_the_second_attempt_is_described_too(self):
        _shown, err = self.run_it((False, SHOP_FIELDS), self.ROWS_NO_AMOUNT)
        self.assertIn("строк 1", err)
        self.assertIn("суммы ни в одной", err)

    def test_the_fields_of_those_rows_are_listed(self):
        """По ним видно, как панель называет сумму, — вместо угадывания."""
        _shown, err = self.run_it((False, SHOP_FIELDS), self.ROWS_NO_AMOUNT)
        self.assertIn("Валюта", err)

    def test_the_old_bare_wording_is_gone(self):
        _shown, err = self.run_it((False, SHOP_FIELDS), self.ROWS_NO_AMOUNT)
        self.assertNotEqual(err, "панель не показала сумму")

    def test_a_failing_second_attempt_is_named_as_well(self):
        _shown, err = self.run_it((False, SHOP_FIELDS),
                                  (False, "401: сессия панели истекла"))
        self.assertIn("Сумма продаж", err)
        self.assertIn("сессия панели истекла", err)


class WhenTheBalanceIsThere(Reason):
    def test_the_shop_page_wins_and_says_nothing(self):
        shown, err = self.run_it((True, {"amount": 1234.5}), (True, []))
        self.assertEqual(shown, "1234.5")
        self.assertEqual(err, "")

    def test_a_rouble_row_is_used_when_the_page_has_none(self):
        shown, err = self.run_it(
            (False, SHOP_FIELDS),
            (True, [{"id": "3", "currency": "RUB", "amount": "980",
                     "raw": {}}]))
        self.assertEqual(shown, "980")
        self.assertEqual(err, "")

    def test_no_panel_login_is_its_own_answer(self):
        storage.get_panel_creds = lambda uid: None
        B.get_panel_creds = storage.get_panel_creds
        shown, err = self.run_it((True, {"amount": 1.0}), (True, []))
        self.assertIsNone(shown)
        self.assertIn("вход в панель", err)


if __name__ == "__main__":
    unittest.main()
