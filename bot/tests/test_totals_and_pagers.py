"""«Всего товаров» и кнопки «Следующая →» — по настоящему курсору.

Курсор этот маркетплейс кладёт в `links.next_cursor`. Семь экранов читали
`meta.next_cursor` каждый у себя, и у всех семи листалка не появлялась:
заказы, чаты и цены обрывались на первой странице ровно так же, как товары.

А экран «🚀 Объявления» сообщает ровно одно — число товаров, — и брал его
как длину первой страницы: «Всего товаров: 15» при семидесяти трёх.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from api.yoomarket import next_cursor                      # noqa: E402


def run(coro):
    return asyncio.run(coro)


LIVE = {"data": [{"id": 1}],
        "meta": {"per_page": 15, "has_more": True},
        "links": {"next_cursor": "eyJpZCI6MjUwNjM5", "prev_cursor": None}}


class TheCursorIsFoundInTheLiveShape(unittest.TestCase):
    def test_the_live_answer_yields_its_cursor(self):
        self.assertEqual(next_cursor(LIVE), "eyJpZCI6MjUwNjM5")

    def test_the_old_place_alone_is_not_enough(self):
        """Именно из-за этого листалки и не было: в `meta` курсора нет."""
        self.assertEqual(LIVE["meta"].get("next_cursor"), None)

    def test_no_more_pages_no_cursor(self):
        self.assertEqual(next_cursor({"data": [], "meta": {"has_more": False},
                                      "links": {"next_cursor": "старый"}}), "")

    def test_an_address_is_not_a_cursor(self):
        self.assertEqual(next_cursor(
            {"links": {"next": "https://api.yoo.market/orders?page=2"}}), "")

    def test_junk_does_not_crash_it(self):
        self.assertEqual(next_cursor({}), "")
        self.assertEqual(next_cursor({"meta": None, "links": None}), "")


class TheAdsScreenCountsEveryPage(unittest.TestCase):
    """«Всего товаров» — единственное, что этот экран сообщает."""

    class Api:
        def __init__(self, pages=5, per=15):
            self.pages, self.per = pages, per
            self.bulk = 0

        async def get_ads(self, cursor=None):
            return dict(LIVE)

        async def get_all_ads(self, max_pages=25):
            self.bulk += 1
            return [{"id": i} for i in range(self.pages * self.per)]

    def screen(self, api):
        from handlers import ads as A

        class Msg:
            def __init__(s):
                s.texts: list = []

            async def edit_text(s, text, reply_markup=None, **kw):
                s.texts.append(text)
                return s

            async def answer(s, text, reply_markup=None, **kw):
                s.texts.append(text)
                return s

        class CB:
            def __init__(s):
                s.data, s.message = "menu:ads", Msg()
                s.from_user = type("U", (), {"id": 7})()

            async def answer(s, *a, **kw):
                pass

        cb = CB()
        run(A.ads_menu(cb, api))
        return cb.message.texts[-1]

    def test_the_real_total_is_shown(self):
        api = self.Api(pages=5, per=15)
        said = self.screen(api)
        self.assertIn("75", said, said)
        self.assertNotIn("Всего товаров: <b>15</b>", said)

    def test_the_marketplaces_own_total_wins_and_costs_nothing(self):
        """Если маркетплейс назовёт своё число — лишние страницы незачем."""
        api = self.Api()

        async def with_total(cursor=None):
            return {"data": [{"id": 1}], "meta": {"total": 73}}

        api.get_ads = with_total
        self.assertIn("73", self.screen(api))
        self.assertEqual(api.bulk, 0, "лишний обход страниц")


if __name__ == "__main__":
    unittest.main()
