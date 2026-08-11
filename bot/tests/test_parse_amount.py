"""Сумма читается в том виде, в каком её пишет панель.

Баланс не показывался при том, что поле «Баланс» бот находил — и на
странице магазина, и в строках `balances`. Ломалось на разборе: чистка
убирала пробелы и запятые по списку, а «1 234,56 ₽» превращалось в
«1234.56₽», на котором `float` падает молча. Символ валюты, слово «руб»,
узкий пробел, разряды точкой — каждый хоронил сумму целиком, и продавец
видел прочерк.

Разборов было два, в разных файлах, и оба неполные. Теперь один.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from orderfields import parse_amount as P     # noqa: E402


class TheShapesThePanelActuallyUses(unittest.TestCase):
    def test_a_plain_number(self):
        self.assertEqual(P("980"), 980.0)

    def test_a_rouble_sign_does_not_kill_it(self):
        """Ровно этот случай и ронял баланс."""
        self.assertEqual(P("1 234,56 ₽"), 1234.56)

    def test_a_non_breaking_space_in_the_thousands(self):
        self.assertEqual(P("12 345 ₽"), 12345.0)

    def test_a_narrow_space_too(self):
        self.assertEqual(P("12 345"), 12345.0)

    def test_the_word_roubles(self):
        self.assertEqual(P("12 345 руб."), 12345.0)

    def test_html_around_it(self):
        """Nova отдаёт ComputedField разметкой."""
        self.assertEqual(P("<span>2 500 ₽</span>"), 2500.0)

    def test_zero_is_a_number_not_nothing(self):
        """Пустой баланс — это ноль, а не «прочитать не удалось»."""
        self.assertEqual(P("0,00 ₽"), 0.0)
        self.assertEqual(P(0), 0.0)

    def test_a_negative_amount(self):
        self.assertEqual(P("-150,5"), -150.5)


class TwoAmountsInOneFieldAreNotGluedTogether(unittest.TestCase):
    """Поле «Баланс» на странице магазина содержит две суммы.

    Пробелы снимались ДО того, как выделено число, и «5 862,26 ₽ · 338 242 ₽»
    читалось как 586226338242 — баланс в полтриллиона на экране продавца.
    Прочерк хотя бы честен; выдуманная сумма хуже, чем её отсутствие.
    """

    def test_only_the_first_amount_is_taken(self):
        self.assertEqual(P("5 862,26 ₽ · 338 242 ₽"), 5862.26)

    def test_with_words_between_them_too(self):
        self.assertEqual(P("Баланс: 5 862,26 ₽ Заморожено: 338 242 ₽"),
                         5862.26)

    def test_thousands_of_the_first_are_not_lost(self):
        """Обрыв на разрядах был бы обратной ошибкой: 5 вместо 5 862."""
        self.assertEqual(P("12 345 ₽ и ещё 678 ₽"), 12345.0)

    def test_a_group_that_is_not_three_digits_ends_the_number(self):
        self.assertEqual(P("5 862 26"), 5862.0)


class SeparatorsBothWays(unittest.TestCase):
    """«1,234.56» и «1 234,56» — одно и то же число, записанное по-разному."""

    def test_comma_thousands_dot_decimal(self):
        self.assertEqual(P("1,234.56"), 1234.56)

    def test_space_thousands_comma_decimal(self):
        self.assertEqual(P("1 234,56"), 1234.56)

    def test_comma_as_thousands_alone(self):
        self.assertEqual(P("1,234"), 1234.0)

    def test_comma_as_decimal_alone(self):
        self.assertEqual(P("1,23"), 1.23)

    def test_dot_decimal_alone(self):
        self.assertEqual(P("1234.5"), 1234.5)


class ThingsThatAreNotAmounts(unittest.TestCase):
    def test_nothing_at_all(self):
        for bad in (None, "", "   "):
            self.assertIsNone(P(bad), bad)

    def test_words_are_not_numbers(self):
        self.assertIsNone(P("нет данных"))

    def test_a_boolean_is_not_a_balance(self):
        """`True` тихо превращался в 1.0 и показывался как рубль."""
        self.assertIsNone(P(True))
        self.assertIsNone(P(False))


class WrappedInAnObject(unittest.TestCase):
    """Этот API отдаёт деньги объектом: {"amount": 99.99, "currency": "RUB"}."""

    def test_the_amount_key(self):
        self.assertEqual(P({"amount": 99.99, "currency": "RUB"}), 99.99)

    def test_other_names_for_the_same_thing(self):
        for key in ("value", "sum", "balance", "total"):
            self.assertEqual(P({key: "1 500 ₽"}), 1500.0, key)

    def test_an_object_with_nothing_useful(self):
        self.assertIsNone(P({"currency": "RUB"}))


class BothReadersUseThisOne(unittest.TestCase):
    """Разборов было два, в разных файлах, и оба неполные: починка одного
    оставляла второй как был."""

    def test_the_balance_screen_delegates(self):
        import inspect
        from handlers import balance as B
        self.assertIn("parse_amount", inspect.getsource(B._money))

    def test_the_shop_page_reader_delegates(self):
        import inspect
        from automation import panel as PA
        src = inspect.getsource(PA.panel_shop_balance_sync)
        self.assertIn("parse_amount", src)
        self.assertNotIn('.replace("₽", "")', src)

    def test_an_unparsed_value_is_named_in_the_reason(self):
        """Поле то самое, а сумма потеряна — самое обидное молчание."""
        import inspect
        from automation import panel as PA
        self.assertIn("не разобрано как число",
                      inspect.getsource(PA.panel_shop_balance_sync))


if __name__ == "__main__":
    unittest.main()
