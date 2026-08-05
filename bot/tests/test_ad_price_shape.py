"""Цена объявления приходит объектом, а не числом.

`GET /ads/223960` отвечает ценой {"amount": 129, "base_amount": 129,
"currency": "RUB"}. Прочитанная как скаляр, она превращалась в 0 или «—»:
прайс показывал «цена не указана», а карточка товара — словарь.
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
