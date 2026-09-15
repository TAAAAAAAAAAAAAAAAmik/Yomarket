"""Reading a stars order — both answers spend the seller's money.

Getting «is this a stars order» wrong means it silently never delivers. Getting
the quantity wrong means buying somebody four thousand stars because the title
mentioned a year.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import stars as S          # noqa: E402


class Recognising(unittest.TestCase):
    def test_the_sellers_own_listing(self):
        self.assertTrue(S.is_stars_order("🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫"))

    def test_the_spellings_that_used_to_be_missed(self):
        """Only «звёзд» with the ё triggered — everything else went past."""
        for title in ("100 звезд Telegram",
                      "Telegram Stars 100",
                      "STARS 500 быстро",
                      "⭐ 100 ⭐ автовыдача",
                      "Звёздочки 50 шт"):
            self.assertTrue(S.is_stars_order(title), title)

    def test_something_that_is_not_stars(self):
        for title in ("Аккаунт Steam 4.000.000",
                      "Вирты Black Russia",
                      ""):
            self.assertFalse(S.is_stars_order(title), title)

    def test_a_custom_keyword_is_obeyed_exactly(self):
        """A seller who typed their own word meant it."""
        self.assertTrue(S.is_stars_order("Пополнение TG 100", keyword="пополнение"))
        self.assertFalse(S.is_stars_order("100 звёзд", keyword="пополнение"))

    def test_the_default_keyword_does_not_narrow_it_back_down(self):
        """«звёзд» is what the setting shipped with — it must not mean «only»."""
        self.assertTrue(S.is_stars_order("Telegram Stars 100", keyword="звёзд"))


class Counting(unittest.TestCase):
    def test_the_number_next_to_the_word_wins(self):
        self.assertEqual(S.star_quantity("🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫"), 100)
        self.assertEqual(S.star_quantity("⭐ 500 ⭐ моментально"), 500)
        self.assertEqual(S.star_quantity("Telegram Stars — 750 шт"), 750)

    def test_a_year_is_not_a_quantity(self):
        """This bought 2024 stars for a listing that sells 100."""
        self.assertEqual(S.star_quantity("Stars 2024 — 100 шт"), 100)
        self.assertEqual(S.star_quantity("Звёзды 100 шт, продаём с 2019 года"), 100)

    def test_a_price_is_not_a_quantity(self):
        self.assertEqual(S.star_quantity("100 звёзд за 199 ₽"), 100)
        self.assertEqual(S.star_quantity("500 звёзд — 899 руб"), 500)

    def test_a_level_or_a_schedule_is_not_a_quantity(self):
        self.assertEqual(S.star_quantity("⭐100 звёзд, выдача 24/7"), 100)
        self.assertEqual(S.star_quantity("Stars 1000 · 5 lvl"), 1000)

    def test_thousands_written_with_separators(self):
        self.assertEqual(S.star_quantity("1 000 звёзд"), 1000)
        self.assertEqual(S.star_quantity("Stars 2.500"), 2500)

    def test_below_what_fragment_accepts_is_not_taken_as_the_count(self):
        """Fragment refuses under fifty, so «10» in a title is something else."""
        self.assertEqual(S.star_quantity("Звёзды: 10 минут, 100 шт"), 100)

    def test_a_title_with_no_number_falls_back(self):
        self.assertEqual(S.star_quantity("Звёзды Telegram", default=50), 50)
        self.assertEqual(S.star_quantity("", default=75), 75)

    def test_the_fallback_is_not_used_when_a_count_is_there(self):
        self.assertEqual(S.star_quantity("100 звёзд", default=50), 100)

    def test_the_nearest_number_wins_when_several_qualify(self):
        self.assertEqual(S.star_quantity("Дёшево 999 — 250 звёзд"), 250)


if __name__ == "__main__":
    unittest.main()
