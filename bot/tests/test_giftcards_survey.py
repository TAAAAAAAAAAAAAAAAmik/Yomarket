"""Замер каталога: какой раздел можно заводить картой, а какой нельзя.

Карта заводится одной декларацией — и потому соблазн «объявить и
посмотреть» велик. Цена ошибки лежит не в коде: раздел, где номинал
разбирается не у всех услуг, даёт товар, который бот не выдаст, и узнает об
этом продавец **после того, как покупатель заплатил**.

Замер уже однажды решил этот вопрос правильно: 20.08 по нему взяли Xbox,
Steam, Amazon и Razer (номинал разобран у всех, всё в наличии) и отвергли
Tinder — 0 из 796 названий деньгами — и Valorant, 57 %, где VP смешаны с
деньгами. Но делался он разовой пробой, и повторить его было нечем: здесь
он становится функцией, у которой есть тесты.

Названия в тестах — живые, из каталога AppRoute. На выдуманных «Card 10
USD» проходит что угодно, включая разбор, который на настоящем каталоге
молчит.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import giftcards as G          # noqa: E402


def service(subcategory: str, name: str, *nominals) -> dict:
    """Услуга поставщика с номиналами: (имя, цена, остаток)."""
    return {
        "id": f"svc-{abs(hash(name)) % 10000}",
        "name": name,
        "subcategoryName": subcategory,
        "items": [{"id": f"den-{i}", "name": n, "price": p, "inStock": stock}
                  for i, (n, p, stock) in enumerate(nominals, start=1)],
    }


class TheUnitIsGuessedFromTheNamesNotInvented(unittest.TestCase):
    """Мера задаётся в декларации, а декларацию ещё только предстоит
    написать: померить раздел, не зная его меры, иначе нельзя."""

    def test_the_unit_before_the_number_is_found(self):
        """«VP 240 Valorant» — единица слева от числа. Смотреть только
        вправо значило бы угадать название игры вместо единицы."""
        self.assertEqual(G.guess_unit(
            ["VP 240 Valorant", "VP 500 Valorant", "VP 1000 Valorant"]), "VP")

    def test_the_unit_after_the_number_is_found_too(self):
        self.assertEqual(G.guess_unit(
            ["EA SPORTS FC 24: 2800 FC Points", "FIFA: 1050 FC Points"]), "FC")

    def test_a_currency_is_not_taken_for_a_unit(self):
        """Деньги — своя мера, и подменять её выдуманной «USD» нельзя."""
        self.assertEqual(G.guess_unit(
            ["Apple Gift Card 10 USD | TR", "Apple Gift Card 25 USD | TR"]), "")

    def test_a_period_is_not_taken_for_a_unit_either(self):
        self.assertEqual(G.guess_unit(
            ["1 Month Tinder Plus", "3 Month Tinder Gold"]), "")

    def test_the_suppliers_own_spelling_is_kept(self):
        """Написание уйдёт в название товара: «240 vp» читается как
        небрежность, а продаёт это продавец, не мы."""
        self.assertEqual(G.guess_unit(["Roblox 800 Robux"]), "Robux")


class ASectionIsCalledReadyOnlyWhenEveryNominalIsUnderstood(unittest.TestCase):
    """«Почти всё» здесь не годится: одна неразобранная услуга — это
    оплаченный заказ, который бот не выдаст."""

    def survey(self, *rows):
        return {r["subcategory"]: r for r in G.survey({"items": list(rows)})}

    def test_a_fully_parsed_money_section_is_ready(self):
        got = self.survey(service(
            "Google Play Gift Cards", "Google Play Gift Card 5 USD | US",
            ("Google Play Gift Card 5 USD", 0.02, 40),
        ))["Google Play Gift Cards"]
        self.assertTrue(got["ready"])
        self.assertEqual(got["measure"], "деньги")
        self.assertEqual(got["cheapest"], 0.02)

    def test_a_section_with_one_unreadable_name_is_not_ready(self):
        got = self.survey(
            service("Mixed", "Mixed Gift Card 10 USD", ("10 USD", 1.0, 5)),
            service("Mixed", "Mixed Special Edition", ("special", 1.0, 5)),
        )["Mixed"]
        self.assertFalse(got["ready"])
        self.assertEqual(int(got["share"] * 100), 50)

    def test_a_section_with_nothing_in_stock_is_not_ready(self):
        """Заводить товар, которого у поставщика нет, значит выставить на
        витрину заказ, который нечем закрыть."""
        got = self.survey(service(
            "Sold Out", "Sold Out Card 10 USD", ("10 USD", 1.0, 0),
        ))["Sold Out"]
        self.assertFalse(got["ready"])
        self.assertEqual(got["in_stock"], 0)

    def test_an_already_declared_section_is_named_not_offered(self):
        got = self.survey(service(
            "Apple Gift Cards", "Apple Gift Card 10 USD | TR",
            ("Apple Gift Card 10 USD", 0.4, 9)))["Apple Gift Cards"]
        self.assertEqual(got["card"], "Apple")
        self.assertFalse(got["ready"], "уже заведённую карту предлагать незачем")

    def test_the_measure_can_be_a_period(self):
        """Tinder мерялся деньгами и давал ноль. Мера «срок» у нас есть —
        значит раздел не «плохой», а просто не той мерой меренный."""
        got = self.survey(
            service("Tinder", "1 Month Tinder Plus", ("1 Month", 3.0, 4)),
            service("Tinder", "3 Month Tinder Gold", ("3 Month", 7.0, 2)),
        )["Tinder"]
        self.assertEqual(got["measure"], "срок")
        self.assertEqual(got["share"], 1.0)

    def test_the_units_measure_wins_where_money_fails(self):
        got = self.survey(
            service("Valorant", "VP 240 Valorant", ("VP 240", 1.9, 3)),
            service("Valorant", "VP 500 Valorant", ("VP 500", 3.8, 3)),
        )["Valorant"]
        self.assertEqual(got["measure"], "VP")

    def test_the_exact_subcategory_string_is_returned(self):
        """Оно же уходит в декларацию: `subcategory` сверяется с точностью
        до символа, и пересказ по памяти здесь стоит целой карты."""
        got = self.survey(service(
            "Razer Gold Gift Cards", "Razer Gold 10 USD | GL",
            ("10 USD", 0.1, 12)))
        self.assertIn("Razer Gold Gift Cards", got)

    def test_ready_sections_come_first(self):
        rows = G.survey({"items": [
            service("Broken", "Broken Special", ("x", 1.0, 1)),
            service("Good", "Good Card 5 USD", ("5 USD", 0.5, 3)),
        ]})
        self.assertEqual(rows[0]["subcategory"], "Good")

    def test_an_empty_catalog_says_nothing_rather_than_breaking(self):
        self.assertEqual(G.survey({}), [])
        self.assertEqual(G.survey({"items": []}), [])


class TheReportTellsWhatToDoWithEachSection(unittest.TestCase):
    """Замер без вывода — это отчёт, по которому нельзя действовать."""

    def lines(self, rows):
        from handlers import plugins as P
        return "\n".join(P._survey_lines(rows))

    def row(self, **over):
        base = {"subcategory": "Google Play Gift Cards", "services": 209,
                "nominals": 209, "in_stock": 209, "measure": "деньги",
                "share": 1.0, "cheapest": 0.02, "card": "", "ready": True}
        base.update(over)
        return base

    def test_a_ready_section_shows_the_string_to_declare(self):
        got = self.lines([self.row()])
        self.assertIn("Google Play Gift Cards", got)
        self.assertIn("0.02", got)

    def test_a_weak_section_says_why_it_cannot_be_declared(self):
        got = self.lines([self.row(ready=False, share=0.57, measure="VP",
                                   subcategory="Valorant")])
        self.assertIn("57 %", got)
        self.assertIn("Valorant", got)

    def test_nothing_new_is_said_plainly(self):
        """Пустой список «готовых» не должен выглядеть как поломка."""
        got = self.lines([self.row(ready=False, card="Apple")])
        self.assertIn("Ни одного нового раздела", got)
        self.assertIn("Уже заведены", got)



class TheDryRunAnswersWithoutSpendingAnything(unittest.TestCase):
    """«Два плагина работают — значит и остальные тоже?» Движок общий и
    правда проверен живой выдачей дважды. А `words` (по ним узнаётся заказ)
    и разбор номинала у каждой карты свои, и ошибка в них видна **не
    отказом, а тишиной**: заказ оплачен, выдача его не заметила.

    Проверять это покупкой по каждой карте — по заказу на карту и деньги за
    каждый. То же самое читается с витрины бесплатно, и здесь проверяется
    именно оно.
    """

    CATALOG = {"items": [
        {"name": "Xbox Gift Card 10 USD | US",
         "subcategoryName": "Xbox Gift Cards", "countryCode": "US",
         "items": [{"id": "d1", "name": "Xbox Gift Card 10 USD",
                    "price": 8.0, "inStock": 5}]},
        {"name": "Steam Wallet Code 5 USD | GL",
         "subcategoryName": "Steam Wallet Gift Cards", "countryCode": "GLOB",
         "items": [{"id": "d2", "name": "Steam Wallet Code 5 USD",
                    "price": 4.5, "inStock": 3}]},
    ]}

    def run_it(self, *titles):
        return G.dry_run(list(titles), self.CATALOG)

    def test_a_listing_the_bot_knows_is_marked_with_its_regions(self):
        row = self.run_it("Xbox Gift Card 10 USD (US)")[0]
        self.assertEqual(row["card"], "Xbox")
        self.assertEqual(row["regions"], ["US"])
        self.assertEqual(row["why"], "")

    def test_an_unknown_listing_is_named_as_such(self):
        """Самый дорогой случай: заказ оплачен, а бот его не заметил."""
        row = self.run_it("Аккаунт Minecraft навсегда")[0]
        self.assertEqual(row["card"], "")
        self.assertIn("не узнала", row["why"])

    def test_a_listing_without_a_nominal_is_named_too(self):
        row = self.run_it("Xbox подарочная карта")[0]
        self.assertEqual(row["card"], "Xbox")
        self.assertIn("не видно номинала", row["why"])

    def test_a_nominal_the_supplier_does_not_have_is_named(self):
        """Товар на витрине есть, купить его нечем — и узнать об этом надо
        до покупателя, а не от него."""
        row = self.run_it("Xbox Gift Card 999 USD (US)")[0]
        self.assertEqual(row["card"], "Xbox")
        self.assertIn("нет в наличии", row["why"])

    def test_each_card_takes_its_own_listing(self):
        rows = self.run_it("Xbox Gift Card 10 USD (US)",
                           "Steam Wallet 5 USD (GL)")
        self.assertEqual([r["card"] for r in rows], ["Xbox", "Steam"])

    def test_the_sellers_own_keyword_is_honoured(self):
        """Слово-опознаватель — требование: без него заказ в выдачу не идёт,
        и сухой прогон обязан считать так же, иначе он врёт в утешительную
        сторону."""
        settings = {"plugins": {"gift_cards": {
            "xbox": {"enabled": True, "keyword": "иксбокс"}}}}
        rows = G.dry_run(["Xbox Gift Card 10 USD (US)"], self.CATALOG, settings)
        self.assertEqual(rows[0]["card"], "")

    def test_nothing_on_the_shelf_is_not_an_error(self):
        self.assertEqual(G.dry_run([], self.CATALOG), [])


class TheDryRunReportSaysWhatToDo(unittest.TestCase):
    def lines(self, rows):
        from handlers import plugins as P
        return "\n".join(P._dry_run_lines(rows))

    def test_the_count_is_stated_first(self):
        got = self.lines([
            {"title": "A", "card": "Xbox", "nominal": "10 USD",
             "regions": ["US"], "why": ""},
            {"title": "B", "card": "", "nominal": "", "regions": [],
             "why": "ни одна карта не узнала товар по названию"},
        ])
        self.assertIn("<b>1</b> из <b>2</b>", got)

    def test_the_unservable_ones_come_first(self):
        """Их и надо чинить; список узнанных — утешение, а не работа."""
        got = self.lines([
            {"title": "Хороший", "card": "Xbox", "nominal": "10 USD",
             "regions": ["US"], "why": ""},
            {"title": "Плохой", "card": "", "nominal": "", "regions": [],
             "why": "ни одна карта не узнала товар по названию"},
        ])
        self.assertLess(got.index("Плохой"), got.index("Хороший"))

    def test_the_region_caveat_is_not_hidden(self):
        """Проверка отвечает «узнаю», а не «выдам наверняка», и умолчать об
        этом значит пообещать больше проверенного."""
        got = self.lines([{"title": "A", "card": "Xbox", "nominal": "10 USD",
                           "regions": ["US"], "why": ""}])
        self.assertIn("Регион товара здесь не проверяется", got)

if __name__ == "__main__":
    unittest.main()
