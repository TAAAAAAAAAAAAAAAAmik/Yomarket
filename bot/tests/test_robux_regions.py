"""Регионы Roblox: два семейства товаров и регион из описания.

У поставщика 26 регионов, и это **два разных товара**, снято живьём 19.08:

* три кошельковых кода — GL, RU, XBOX. Номинал в Robux: «1000 Robux»;
* двадцать три подарочные карты по странам — AE, AT, AU, BR, CA, CH, DK,
  EU, FR, HK, ID, JP, MX, MY, NO, NZ, PL, SA, SE, SG, UK, US, ZA. Номинал
  **в местной валюте**: «$10 Roblox gift card», «EUR 25», «IDR 500000».

Сколько Robux даст карта на $10, решает курс самого Roblox. Мы его не знаем,
и подставить сюда догадку значит однажды выдать не то, что оплачено.
Поэтому семейство определяется по региону, а не по числу в названии: принять
«$10» за десять Robux — ошибка в сотню раз, притом молчаливая.

Регион живёт в описании товара, потому что больше ему негде: у Юмаркета
выбора региона нет вовсе — 19.08 категория 14 отдала ноль фильтров, — а в
заказе от объявления приходит только `ad_id`.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import robux as R          # noqa: E402


def service(name, *items):
    return {"id": f"svc-{name}", "name": name, "type": "voucher",
            "fields": None,
            "items": [{"id": f"{name}-{i}", "name": n, "price": p,
                       "currency": "USD", "inStock": s}
                      for i, (n, p, s) in enumerate(items, 1)]}


# Названия — как они приходят от поставщика.
CATALOG = {"items": [
    service("Roblox Wallet Code | GL",
            ("200 Robux", 3.57, 1948), ("1000 Robux", 11.22, 994),
            ("10000 Robux", 98.94, 187)),
    service("Roblox Wallet Code | RU",
            ("Roblox code | 1000 Robux RU", 13.9563, 20)),
    service("Roblox Gift Card | US",
            ("$10 Roblox gift card", 9.435, 12),
            ("$25 Roblox gift card", 23.5, 0)),
    service("Roblox Gift Card | EU",
            ("EUR 25 Roblox gift card", 28.2234, 10)),
]}


class TheTwoFamiliesAreToldApartByRegion(unittest.TestCase):
    """Число в названии значит разное: у кодов это Robux, у карт — валюта."""

    def test_wallet_regions_are_named(self):
        for code in ("GL", "RU", "XBOX"):
            self.assertTrue(R.is_wallet_region(code), code)

    def test_a_country_is_a_card_region(self):
        for code in ("US", "EU", "BR", "ID"):
            self.assertFalse(R.is_wallet_region(code), code)

    def test_every_region_of_the_catalogue_is_listed(self):
        regs = {r["region"] for r in R.catalog_regions(CATALOG)}
        self.assertEqual(regs, {"GL", "RU", "US", "EU"})

    def test_each_region_says_which_family_it_is(self):
        kinds = {r["region"]: r["kind"] for r in R.catalog_regions(CATALOG)}
        self.assertEqual(kinds["GL"], "wallet")
        self.assertEqual(kinds["US"], "card")


class ACardsFaceValueIsReadWithItsCurrency(unittest.TestCase):
    """«10» бывает и долларами, и евро, и реалами — само по себе оно
    ничего не значит."""

    def test_the_symbol_and_the_code_both_work(self):
        self.assertEqual(R.face_value("$10 Roblox gift card"), (10.0, "USD"))
        self.assertEqual(R.face_value("EUR 25 Roblox gift card"), (25.0, "EUR"))
        self.assertEqual(R.face_value("AUD 10.37 Roblox gift card"),
                         (10.37, "AUD"))

    def test_robux_is_not_a_currency(self):
        """Три буквы брались из середины слова: «1000 Robux» читалось как
        тысяча валюты «ROB», то есть номинал в Robux притворялся картой."""
        self.assertEqual(R.face_value("1000 Robux"), (0.0, ""))
        self.assertEqual(R.face_value("200 Robux"), (0.0, ""))


class TheMatchUsesTheRightYardstick(unittest.TestCase):

    def test_a_wallet_region_matches_by_robux(self):
        row, why = R.match_denomination(CATALOG, 1000, "GL")
        self.assertEqual(why, "")
        self.assertEqual(row["denomination_id"], "Roblox Wallet Code | GL-2")

    def test_a_card_region_matches_by_face_value(self):
        row, why = R.match_denomination(CATALOG, 10, "US")
        self.assertEqual(why, "")
        self.assertEqual(row["face_currency"], "USD")
        self.assertEqual(row["face"], 10.0)

    def test_ten_dollars_is_not_ten_robux(self):
        """Ошибка в сотню раз. Разные регионы — разные мерки."""
        card, _ = R.match_denomination(CATALOG, 10, "US")
        wallet, why = R.match_denomination(CATALOG, 10, "GL")
        self.assertIsNotNone(card)
        self.assertIsNone(wallet, "десять Robux у поставщика не продаются")
        self.assertIn("Есть", why)

    def test_a_card_out_of_stock_is_refused_before_the_money(self):
        row, why = R.match_denomination(CATALOG, 25, "US")
        self.assertIsNone(row)
        self.assertIn("остаток", why)

    def test_the_refusal_lists_what_the_region_does_have(self):
        row, why = R.match_denomination(CATALOG, 777, "GL")
        self.assertIsNone(row)
        self.assertIn("200", why)
        self.assertIn("10000", why)


class NotZeroIsNotTheSameAsEnough(unittest.TestCase):
    """Живой случай 19.08: на счету 1.43 $, самый дешёвый номинал 2.74 $.

    Баланс не ноль, поэтому предупреждение о пустом счёте молчало — а выдать
    нельзя было ни одного кода, и первый же оплаченный заказ отвалился бы с
    «не хватает средств», причём по каждому покупателю отдельно.
    """

    def test_money_that_buys_nothing_is_counted_as_nothing(self):
        can = R.affordable(CATALOG, 1.43)
        self.assertEqual(can["count"], 0)
        self.assertEqual(can["total"], 0)
        self.assertGreater(can["cheapest"], 1.43)

    def test_the_cheapest_in_stock_is_named(self):
        can = R.affordable(CATALOG, 100)
        self.assertEqual(can["cheapest"], 3.57)
        self.assertEqual(can["cheapest_name"], "200 Robux")
        self.assertEqual(can["cheapest_region"], "GL")

    def test_a_sold_out_denomination_does_not_count_as_affordable(self):
        """У $25 US остаток ноль — купить его нельзя ни за какие деньги."""
        can = R.affordable(CATALOG, 1000)
        self.assertNotIn(23.5, [23.5] if can["total"] == 0 else [])
        rows = [r for r in R.denominations(CATALOG) if r["in_stock"] > 0]
        self.assertEqual(can["total"], len(rows))

    def test_how_many_of_the_cheapest_fit(self):
        can = R.affordable(CATALOG, 8.0)      # 2 × 3.57 = 7.14
        self.assertEqual(can["count"], 2)

    def test_an_empty_catalogue_answers_zero_not_a_crash(self):
        can = R.affordable({"items": []}, 100)
        self.assertEqual(can["cheapest"], 0.0)
        self.assertEqual(can["count"], 0)


class TheRegionLivesInTheDescriptionBecauseItHasNowhereElse(unittest.TestCase):
    """У Юмаркета выбора региона нет: категория отдаёт ноль фильтров, а в
    заказе от объявления приходит только `ad_id`."""

    def test_the_bot_writes_a_line_it_can_read_back(self):
        text = R.with_region("Код на пополнение.", "GL")
        self.assertEqual(R.region_from_description(text), ("GL", "строка"))

    def test_creating_twice_does_not_double_the_line(self):
        once = R.with_region("Описание", "GL")
        twice = R.with_region(once, "RU")
        self.assertEqual(twice.count(R.REGION_PREFIX), 1)
        self.assertEqual(R.region_from_description(twice), ("RU", "строка"))

    def test_the_line_wins_over_words_in_the_text(self):
        """«Глобальный аккаунт не нужен, код RU» прочлось бы наоборот."""
        text = R.with_region("Глобальный аккаунт не нужен", "RU")
        self.assertEqual(R.region_from_description(text), ("RU", "строка"))

    def test_an_old_listing_is_understood_by_its_words(self):
        got, how = R.region_from_description("Код для российского аккаунта")
        self.assertEqual(got, "RU")
        self.assertEqual(how, "слова")

    def test_two_regions_at_once_mean_we_do_not_know(self):
        """Выбрать из двух наугад значит купить код, который покупатель не
        активирует."""
        self.assertEqual(
            R.region_from_description("Глобальный или для России"), ("", ""))

    def test_nothing_at_all_is_admitted_not_guessed(self):
        self.assertEqual(R.region_from_description("Просто код"), ("", ""))

    def test_how_we_knew_is_returned_too(self):
        """Догадка, поданная как факт, в этом проекте уже стоила дней."""
        self.assertEqual(R.region_from_description(
            R.with_region("x", "GL"))[1], "строка")
        self.assertEqual(R.region_from_description("код xbox")[1], "слова")


if __name__ == "__main__":
    unittest.main()
