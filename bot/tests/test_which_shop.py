"""Баланс читается у СВОЕГО магазина, а не у первого попавшегося.

Одна сессия панели видит все магазины продавца. Читатель баланса
перебирал их подряд и брал первую же страницу с полем баланса — пока
магазин один, этого не видно. У продавца с двумя аккаунтами под вторым
магазином показались бы деньги первого.

Цифра из чужого магазина хуже прочерка: прочерк видно, а её нет. Поэтому
имя магазина приходит из `/check` того же аккаунта, а если сопоставить не
вышло — возвращается отказ со списком найденных.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P     # noqa: E402


def shop_row(rid, title):
    return {"id": rid, "title": title}


def page(fields):
    return {"resource": {"fields": fields}}


def money_field(name, value):
    return {"name": name, "attribute": "balance", "value": value}


class FakePanel:
    """Отвечает как Nova: список магазинов и страница каждого."""

    def __init__(self, shops, pages):
        self.shops, self.pages = shops, pages
        self.asked: list[str] = []

    def get(self, url, **kw):
        outer = self

        class R:
            status_code = 200

            def json(self_inner):
                if url.endswith("/nova-api/shops"):
                    return {"resources": outer.shops}
                sid = url.rsplit("/", 1)[-1]
                outer.asked.append(sid)
                return outer.pages.get(sid, {"resource": {"fields": []}})

        return R()


class Bench(unittest.TestCase):
    def setUp(self):
        self._mk = P._make_panel_requests_session
        self._hd = P._panel_xsrf_headers
        P._panel_xsrf_headers = lambda s, c: {}

    def tearDown(self):
        P._make_panel_requests_session = self._mk
        P._panel_xsrf_headers = self._hd

    def run_it(self, shops, pages, shop_name=""):
        self.fake = FakePanel(shops, pages)
        P._make_panel_requests_session = lambda c: self.fake
        return P.panel_shop_balance_sync("c", "", shop_name)

    TWO = [shop_row(11, "Spike"), shop_row(22, "Второй магазин")]
    PAGES = {"11": page([money_field("Баланс", "586,226 ₽")]),
             "22": page([money_field("Баланс", "0 ₽")])}


class TwoShopsUnderOneLogin(Bench):
    def test_the_second_shop_gets_its_own_balance(self):
        ok, got = self.run_it(self.TWO, self.PAGES, "Второй магазин")
        self.assertTrue(ok, got)
        self.assertEqual(got["amount"], 0.0)

    def test_and_not_the_first_shops_money(self):
        """Ровно то, что показал бы прежний перебор по порядку."""
        ok, got = self.run_it(self.TWO, self.PAGES, "Второй магазин")
        self.assertNotEqual(got["amount"], 586.226)

    def test_the_first_shop_still_gets_its_own(self):
        ok, got = self.run_it(self.TWO, self.PAGES, "Spike")
        self.assertTrue(ok, got)
        self.assertEqual(got["amount"], 586.226)

    def test_the_other_shops_page_is_not_even_opened(self):
        self.run_it(self.TWO, self.PAGES, "Второй магазин")
        self.assertEqual(self.fake.asked, ["22"])

    def test_the_answer_says_which_shop_it_came_from(self):
        _ok, got = self.run_it(self.TWO, self.PAGES, "Spike")
        self.assertEqual(got["shop_name"], "Spike")

    def test_the_match_ignores_case(self):
        _ok, got = self.run_it(self.TWO, self.PAGES, "spike")
        self.assertEqual(got["amount"], 586.226)


class WhenTheShopCannotBeTold(Bench):
    """Не знаем, какой из двух — молчим об этом вслух."""

    def test_an_unknown_name_is_a_refusal_not_a_guess(self):
        ok, err = self.run_it(self.TWO, self.PAGES, "Третий")
        self.assertFalse(ok)

    def test_no_name_at_all_is_a_refusal_too(self):
        ok, err = self.run_it(self.TWO, self.PAGES, "")
        self.assertFalse(ok)

    def test_the_refusal_names_the_shops_it_found(self):
        _ok, err = self.run_it(self.TWO, self.PAGES, "")
        self.assertIn("Spike", err)
        self.assertIn("Второй магазин", err)

    def test_it_points_at_the_diagnostic(self):
        _ok, err = self.run_it(self.TWO, self.PAGES, "")
        self.assertIn("accounts_debug", err)

    def test_two_shops_with_the_same_word_are_not_resolved_by_luck(self):
        shops = [shop_row(11, "Магазин"), shop_row(22, "Магазин 2")]
        ok, err = self.run_it(shops, self.PAGES, "Магазин")
        self.assertFalse(ok, err)


class OneShopNeedsNoChoosing(Bench):
    """Раньше имя было не нужно, и ломать это нельзя: у большинства
    продавцов магазин один."""

    ONE = [shop_row(11, "Spike")]

    def test_it_reads_the_only_shop_without_a_name(self):
        ok, got = self.run_it(self.ONE, self.PAGES, "")
        self.assertTrue(ok, got)
        self.assertEqual(got["amount"], 586.226)

    def test_even_when_the_name_does_not_match(self):
        """Выбирать не из чего — придираться к имени незачем."""
        ok, got = self.run_it(self.ONE, self.PAGES, "что-то другое")
        self.assertTrue(ok, got)


class AnEmptyBalanceFieldIsNotAMissingOne(Bench):
    """У нового магазина с нулём панель отдаёт поле пустым. Молчаливый
    пропуск заканчивался отчётом «поля — …», то есть «баланса не нашлось»
    там, где он нашёлся и пуст."""

    def test_the_reason_says_the_field_was_empty(self):
        pages = {"11": page([money_field("Баланс", "")])}
        ok, err = self.run_it([shop_row(11, "Spike")], pages, "Spike")
        self.assertFalse(ok)
        self.assertIn("пустое", err)

    def test_it_does_not_claim_the_field_is_absent(self):
        pages = {"11": page([money_field("Баланс", "")])}
        _ok, err = self.run_it([shop_row(11, "Spike")], pages, "Spike")
        self.assertIn("Баланс", err)


class TheScreenAsksForItsOwnShop(unittest.TestCase):
    def test_the_balance_reader_passes_the_shop_name(self):
        import inspect
        from handlers import balance as B
        src = inspect.getsource(B._panel_balance)
        self.assertIn("get_shop_name(uid)", src)


if __name__ == "__main__":
    unittest.main()
