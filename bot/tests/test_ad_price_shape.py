"""Цена объявления приходит объектом, а не числом.

`GET /ads/223960` отвечает ценой {"amount": 129, "base_amount": 129,
"currency": "RUB"}. Всё, что читало её как скаляр, получало 0 или «—»:
экран цен показывал «цена не указана», карточка товара — словарь, а ночное
расписание молча пропускало каждый товар, ни разу ничего не изменив.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from orderfields import ad_price                     # noqa: E402
from handlers import ads as A, prices as P           # noqa: E402
from keyboards.main import ads_list_keyboard         # noqa: E402

REAL = {"id": 223960, "title": "💫100 ЗВЁЗД💫", "status": "publish",
        "price": {"amount": 129, "base_amount": 129, "currency": "RUB"}}


class ReadingIt(unittest.TestCase):
    def test_the_shape_the_marketplace_actually_sends(self):
        self.assertEqual(ad_price(REAL), 129)

    def test_a_plain_number_still_works(self):
        self.assertEqual(ad_price({"price": 450}), 450)

    def test_and_a_string(self):
        self.assertEqual(ad_price({"price": "450.00"}), 450)

    def test_a_missing_price_is_none_not_zero(self):
        """Ноль — это «бесплатно», а не «не прислали»."""
        self.assertIsNone(ad_price({"id": 1, "title": "без цены"}))

    def test_zero_stays_zero(self):
        self.assertEqual(ad_price({"price": {"amount": 0, "currency": "RUB"}}), 0)


class ShowingIt(unittest.TestCase):
    def test_the_price_screen_reads_it(self):
        self.assertEqual(P._price_of(REAL), 129)

    def test_the_listing_card_does_not_print_a_dict(self):
        text = A._price_text(REAL)
        self.assertEqual(text, "129")
        self.assertNotIn("{", text)

    def test_a_listing_button_carries_the_number(self):
        kb = ads_list_keyboard([REAL], None)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("129 ₽" in t for t in labels), labels)

    def test_the_price_list_shows_it_rather_than_saying_it_is_unset(self):
        text = P._list_text([REAL], 0)
        self.assertIn("129 ₽", text)
        self.assertNotIn("цена не указана", text)


class ChangingIt(unittest.TestCase):
    """Раз цена читается объектом, число в запросе могло не подойти."""

    class Client:
        _is_bad_payload = staticmethod(
            lambda e: any(k in str(e).lower() for k in ("422", "valid")))

        def __init__(self, accept):
            self.accept = accept          # какая форма тела принимается
            self.tried: list[dict] = []

        async def _patch(self, path, json=None):
            self.tried.append(dict(json or {}))
            if not self.accept(json or {}):
                raise RuntimeError("422: The price field is not valid")
            return {"ok": True}

    def _run(self, accept):
        from api.yoomarket import YooMarketAPI
        client = self.Client(accept)
        coro = YooMarketAPI.update_price(client, "1", 139)
        try:
            asyncio.run(coro)
            ok = True
        except Exception:
            ok = False
        return ok, client.tried

    def test_the_documented_shape_is_tried_first_and_alone(self):
        ok, tried = self._run(lambda p: "price" in p and not isinstance(
            p["price"], dict))
        self.assertTrue(ok)
        self.assertEqual(tried, [{"price": 139}], "лишние запросы ни к чему")

    def test_an_object_is_tried_when_a_number_is_refused(self):
        ok, tried = self._run(lambda p: isinstance(p.get("price"), dict))
        self.assertTrue(ok, tried)
        self.assertEqual(tried[-1]["price"], {"amount": 139, "currency": "RUB"})

    def test_a_bare_amount_is_tried_too(self):
        ok, tried = self._run(lambda p: "amount" in p)
        self.assertTrue(ok, tried)

    def test_a_refusal_that_is_not_about_the_body_is_not_retried(self):
        """«Нет прав» повторится одинаково на любую форму — это просто шум."""
        from api.yoomarket import YooMarketAPI

        class Denying(self.Client):
            async def _patch(self, path, json=None):
                self.tried.append(dict(json or {}))
                raise RuntimeError("403: нет прав на это объявление")

        client = Denying(lambda p: False)
        with self.assertRaises(Exception):
            asyncio.run(YooMarketAPI.update_price(client, "1", 139))
        self.assertEqual(len(client.tried), 1, client.tried)

    def test_when_nothing_is_accepted_the_first_refusal_is_what_is_reported(self):
        ok, tried = self._run(lambda p: False)
        self.assertFalse(ok)
        self.assertEqual(len(tried), 3, tried)


class TheNightSchedule(unittest.TestCase):
    """Расписание считало цену через int(float(str(...))) — на объекте оно
    спотыкалось и пропускало товар. Ни одна ночная скидка не применялась."""

    def test_it_can_compute_a_new_price_from_the_real_shape(self):
        value = ad_price(REAL)
        self.assertIsNotNone(value, "иначе расписание пропустит товар")
        self.assertEqual(max(1, round(int(round(value)) * 0.9)), 116)


if __name__ == "__main__":
    unittest.main()
