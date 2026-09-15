"""Сумма на экране пишется разрядами, и знак рубля в ней один.

Баланс показался как «586226 ₽». Число верное, но прочитать его можно
только пересчитав цифры по три, а ошибиться разрядом при выводе средств —
это ошибиться в сумме на порядок. Рядом обнаружилось второе: `shop_balance`
отдаёт строку уже со знаком рубля, а экраны вывода добавляли свой, и
получалось «586226 ₽ ₽».

Форматирование одно на весь бот — `orderfields.money`. Разбор и показ
разведены нарочно: наружу из `_panel_balance` уходит голое число, которое
ещё будут превращать во `float`; пробел в разрядах сделал бы баланс нулём и
«выводить нечего» при полном счёте.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.balance as B          # noqa: E402
from orderfields import money as M    # noqa: E402


class TheFormatterItself(unittest.TestCase):
    def test_thousands_are_separated(self):
        self.assertEqual(M(586226.0), "586 226")

    def test_a_small_number_is_left_alone(self):
        self.assertEqual(M(980.0), "980")

    def test_millions_too(self):
        self.assertEqual(M(1234567.0), "1 234 567")

    def test_kopecks_survive(self):
        self.assertEqual(M(1234.56), "1 234,56")

    def test_a_round_sum_has_no_trailing_zeroes(self):
        self.assertEqual(M(1234.0), "1 234")

    def test_the_real_shop_balance_reads_as_roubles_not_thousands(self):
        """«586 226» выглядит как полмиллиона. Это 586 рублей."""
        self.assertEqual(M(586.226), "586,226")

    def test_the_third_decimal_is_not_rounded_away(self):
        """Панель показывает три знака; «586,23 ₽» с ней уже расходится, а
        совпадение с панелью — весь смысл этого экрана."""
        self.assertNotEqual(M(586.226), "586,23")

    def test_nothing_is_an_empty_string_not_a_zero(self):
        self.assertEqual(M(None), "")


class TheBalanceScreen(unittest.TestCase):
    def test_a_bare_number_is_dressed_up_for_the_screen(self):
        self.assertEqual(B._shown("586226"), "586 226")

    def test_a_panel_string_is_parsed_first(self):
        self.assertEqual(B._shown("1 234,56 ₽"), "1 234,56")

    def test_the_panels_own_balance_comes_back_as_the_panel_wrote_it(self):
        self.assertEqual(B._shown("586,226 ₽"), "586,226")

    def test_the_kopecks_survive_the_round_trip_through_a_bare_string(self):
        """Между чтением панели и экраном сумма живёт строкой с точкой —
        `.2f` округлял её там до 586.23 ещё до показа."""
        self.assertEqual(B._shown(B._plain(586.226)), "586,226")

    def test_something_unparseable_is_shown_as_it_came(self):
        """Лучше показать непонятное, чем подменить его пустотой."""
        self.assertEqual(B._shown("нет данных"), "нет данных")

    def test_the_screen_uses_it(self):
        import inspect
        src = inspect.getsource(B.show_balance)
        self.assertIn("_shown(balance)", src)
        self.assertIn("_shown(pending)", src)


class TheRoubleSignIsNotDoubled(unittest.TestCase):
    """`shop_balance` возвращает строку со знаком рубля. Экраны вывода
    добавляли свой — «586226 ₽ ₽»."""

    def setUp(self):
        import tasks.manager as TM
        self._real = TM.shop_balance

        async def fake(uid, api, why=None):
            return 586226.0, "586 226 ₽"

        TM.shop_balance = fake
        self._settings = B.get_settings
        B.get_settings = lambda uid: {"auto_withdraw": {
            "panel_balance_id": "1", "panel_values": {"system": "card"}}}

    def tearDown(self):
        import tasks.manager as TM
        TM.shop_balance = self._real
        B.get_settings = self._settings

    class Msg:
        def __init__(self):
            self.text = ""

        async def edit_text(self, text, reply_markup=None, **kw):
            self.text = text

        async def answer(self, text, reply_markup=None, **kw):
            self.text = text

    class State:
        async def set_state(self, *a, **kw):
            pass

    def test_the_amount_screen_shows_one_sign(self):
        msg = self.Msg()
        asyncio.run(B._ask_amount(msg, 1, None, self.State()))
        self.assertIn("586 226 ₽", msg.text)
        self.assertNotIn("₽ ₽", msg.text)


class TheOneStringEveryScreenReuses(unittest.TestCase):
    """`shop_balance` — единственный источник строки баланса: её печатают
    итоги дня, уведомление о пороге и оба экрана вывода. Формат правится
    здесь, иначе четыре места разъезжаются."""

    def setUp(self):
        self._real = B._panel_balance

    def tearDown(self):
        B._panel_balance = self._real

    def run_it(self, panel_answer):
        import tasks.manager as TM

        async def fake_panel(uid):
            return panel_answer, ""

        B._panel_balance = fake_panel

        class API:
            async def get_balance(self):
                return 0.0, "—"

        return asyncio.run(TM.shop_balance(1, API()))

    def test_the_string_carries_separators_and_one_rouble_sign(self):
        amount, shown = self.run_it("586226")
        self.assertEqual(shown, "586 226 ₽")

    def test_the_number_beside_it_stays_a_number(self):
        """По нему решают «выводить нечего» — пробел сделал бы его нулём."""
        amount, _ = self.run_it("586226")
        self.assertEqual(amount, 586226.0)

    def test_an_unreadable_balance_is_still_a_dash(self):
        async def no_panel(uid):
            return None, "нужен вход в панель продавца"

        B._panel_balance = no_panel
        import tasks.manager as TM

        class API:
            async def get_balance(self):
                return 0.0, "—"

        why: list = []
        amount, shown = asyncio.run(TM.shop_balance(1, API(), why))
        self.assertEqual(shown, "—")
        self.assertIn("вход в панель", why[0])


class TheLastScreenBeforeRealMoney(unittest.TestCase):
    """«5862 ₽» и «58620 ₽» без пробела различаются одним взглядом."""

    def setUp(self):
        self._settings = B.get_settings
        B.get_settings = lambda uid: {"auto_withdraw": {
            "panel_balance_id": "1", "panel_values": {"system": "card",
                                                      "card": "1234567890123456"}}}

    def tearDown(self):
        B.get_settings = self._settings

    def test_the_confirmation_shows_the_sum_with_separators(self):
        msg = TheRoubleSignIsNotDoubled.Msg()
        asyncio.run(B._confirm_payout(msg, 1, 58620))
        self.assertIn("58 620 ₽", msg.text)

    def test_the_requisites_are_still_masked(self):
        """Экран подтверждения нужен, чтобы заметить чужую карту, а не
        чтобы светить свою в переписке."""
        msg = TheRoubleSignIsNotDoubled.Msg()
        asyncio.run(B._confirm_payout(msg, 1, 58620))
        self.assertNotIn("1234567890123456", msg.text)


class ThePanelHelperStillReturnsAPlainNumber(unittest.TestCase):
    """Пробел в разрядах здесь сломал бы `float` в выводе средств, и баланс
    стал бы нулём: «выводить нечего» при полном счёте."""

    def test_the_reader_does_not_format(self):
        import inspect
        src = inspect.getsource(B._panel_balance)
        self.assertNotIn("_RUB", src)
        self.assertNotIn("_shown", src)

    def test_the_float_round_trip_survives(self):
        for raw in ("586226", "1234.56", "0"):
            float(raw)   # именно так его читает `shop_balance`


if __name__ == "__main__":
    unittest.main()
