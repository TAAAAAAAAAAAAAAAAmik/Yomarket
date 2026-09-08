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


class Msg:
    def __init__(self):
        self.texts: list = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self


class CB:
    def __init__(self, data=""):
        self.data, self.message = data, Msg()
        self.from_user = type("U", (), {"id": 7})()

    async def answer(self, *a, **kw):
        pass


class OrdersApi:
    """Маркетплейс отвечает как живой: курсор в `links`."""

    def __init__(self, with_next=True):
        self.with_next = with_next

    async def get_orders(self, cursor=None):
        body = {"data": [{"id": 1, "status": "paid",
                          "price": {"amount": 100}}],
                "meta": {"per_page": 15, "has_more": self.with_next}}
        if self.with_next:
            body["links"] = {"next_cursor": "eyJpZCI6MQ", "prev_cursor": None}
        return body


def buttons(kb) -> list:
    return [b.text for row in (kb.inline_keyboard if kb else []) for b in row]


class TheOrdersScreenOpensAndPages(unittest.TestCase):
    """Экран заказов не проверял никто, и одна опечатка в имени уронила его
    целиком: «cannot access local variable 'next_cursor'». Проверка на то,
    что экран ОТКРЫВАЕТСЯ, стоит дешевле любого разбора."""

    def open(self, api):
        from handlers import orders as O
        cb = CB("menu:orders")
        run(O.show_orders(cb, api))
        return cb

    def test_it_opens_without_an_error(self):
        cb = self.open(OrdersApi())
        said = cb.message.texts[-1]
        self.assertNotIn("Ошибка", said, said)
        self.assertNotIn("next_cursor", said, said)

    def test_the_next_button_appears_on_the_live_shape(self):
        """Курсор в `links.next_cursor`, и раньше кнопки не было вовсе."""
        cb = self.open(OrdersApi(with_next=True))
        self.assertTrue(any("Следующая" in b for b in buttons(cb.message.kbs[-1])),
                        buttons(cb.message.kbs[-1]))

    def test_and_not_on_the_last_page(self):
        cb = self.open(OrdersApi(with_next=False))
        self.assertFalse(any("Следующая" in b for b in buttons(cb.message.kbs[-1])))


class TheChatsScreenOpensAndPages(unittest.TestCase):
    def open(self, api):
        from handlers import chats as CH
        cb = CB("chats:list")
        run(CH.show_chats(cb, api))
        return cb

    def test_it_opens_without_an_error(self):
        cb = self.open(OrdersApi())
        said = cb.message.texts[-1]
        self.assertNotIn("Ошибка", said, said)
        self.assertNotIn("next_cursor", said, said)

    def test_the_next_button_appears(self):
        cb = self.open(OrdersApi(with_next=True))
        self.assertTrue(any("Следующая" in b for b in buttons(cb.message.kbs[-1])),
                        buttons(cb.message.kbs[-1]))


if __name__ == "__main__":
    unittest.main()
