"""Мастер создания товара: нумерация шагов и дорога назад.

Экраны говорили «Шаг 1/4 … Шаг 4/4», а следом показывали «Шаг 5/5»:
продавцу обещали четыре шага и приводили на пятый. Считалось это в пяти
местах по памяти, поэтому расхождение и появилось — и появилось бы снова.

Вторая беда была молчаливее: из мастера вело только «Отмена». Опечатка в
цене на четвёртом шаге означала пройти всё заново, то есть исправить её
было нельзя — при том что введённое никуда не девалось, оно лежало в
состоянии.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import create_ad as C          # noqa: E402


def callbacks(kb) -> list:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)


class CB:
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.answered: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answered.append(text)


class FSM:
    def __init__(self, **data):
        self.data = dict(data)
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)
        return dict(self.data)

    async def clear(self):
        self.data.clear()
        self.state = None


class TheStepCountIsToldTheSameEverywhere(unittest.TestCase):
    """«Шаг N из 5» считается из одного списка, а не набирается руками."""

    def test_every_step_names_the_same_total(self):
        for step in C._STEPS:
            self.assertIn(f"из {len(C._STEPS)}", C._step_header(step))

    def test_the_numbers_follow_the_order_of_the_wizard(self):
        for n, step in enumerate(C._STEPS, start=1):
            self.assertIn(f"Шаг {n} из", C._step_header(step))

    def test_the_bar_fills_up_as_the_steps_pass(self):
        """Полоса — не украшение: по ней видно, сколько осталось."""
        first = C._step_header(C._STEPS[0])
        last = C._step_header(C._STEPS[-1])
        self.assertEqual(first.count("▰"), 1)
        self.assertEqual(last.count("▰"), len(C._STEPS))
        self.assertEqual(last.count("▱"), 0)

    def test_no_screen_hardcodes_a_step_number(self):
        """Ровно так и разошлось: «4» стояло в тексте четырёх экранов.

        Комментарии не в счёт — в них номера упомянуты нарочно, чтобы было
        видно, как выглядела прежняя нумерация.
        """
        import inspect
        import re
        code = [line for line in inspect.getsource(C).split("\n")
                if not line.lstrip().startswith("#")]
        self.assertEqual(re.findall(r"Шаг \d+/\d+", "\n".join(code)), [])


class TheWizardLetsYouGoBack(unittest.TestCase):
    """Иначе одна опечатка стоит всего пути заново."""

    def test_every_step_but_the_first_offers_a_way_back(self):
        self.assertNotIn("title", C._STEP_BACK)
        for step in C._STEPS[1:]:
            self.assertIn(step, C._STEP_BACK)
            self.assertIn(f"create_ad:back:{C._STEP_BACK[step]}",
                          callbacks(C._step_kb(step)))

    def test_going_back_keeps_what_was_typed(self):
        """Введённое уже в состоянии — терять его незачем."""
        fsm = FSM(title="1000 Robux", price=350)
        cb = CB("create_ad:back:price")
        asyncio.run(C.create_ad_back(cb, fsm))
        self.assertEqual(fsm.data["title"], "1000 Robux")
        self.assertIn("350", cb.message.texts[-1])

    def test_going_back_lands_on_the_right_step(self):
        fsm = FSM(title="X")
        cb = CB("create_ad:back:title")
        asyncio.run(C.create_ad_back(cb, fsm))
        self.assertIs(fsm.state, C.CreateAdState.title)
        self.assertIn("Шаг 1 из", cb.message.texts[-1])

    def test_an_unknown_step_is_refused_not_guessed(self):
        fsm = FSM()
        cb = CB("create_ad:back:whatever")
        asyncio.run(C.create_ad_back(cb, fsm))
        self.assertEqual(cb.message.texts, [])
        self.assertTrue(cb.answered)

    def test_the_quantity_step_keeps_its_skip_button(self):
        """Возврат не должен отбирать то, что было на самом шаге."""
        fsm = FSM(description="d")
        cb = CB("create_ad:back:quantity")
        asyncio.run(C.create_ad_back(cb, fsm))
        self.assertIn("create_ad:qty:1", callbacks(cb.message.kbs[-1]))


class RoundingSomeonesPriceIsSaidOutLoud(unittest.TestCase):
    """`500.9` молча становились `500`. Про чужие деньги молчать нельзя
    даже в мелочи: продавец узнал бы о своей цене от покупателя."""

    def price(self, text):
        said: list = []

        class Msg:
            def __init__(self):
                self.text = text
                self.from_user = type("U", (), {"id": 1})()

            async def answer(_self, t, reply_markup=None, **kw):
                said.append(t)

        fsm = FSM()
        asyncio.run(C.ad_price(Msg(), fsm))
        return fsm.data.get("price"), said

    def test_kopecks_are_dropped_but_named(self):
        price, said = self.price("500.9")
        self.assertEqual(price, 500)
        self.assertTrue(any("500" in s and "⚠️" in s for s in said),
                        "об округлении не сказано")

    def test_a_whole_price_says_nothing_extra(self):
        price, said = self.price("500")
        self.assertEqual(price, 500)
        self.assertFalse([s for s in said if "⚠️" in s])

    def test_a_price_that_rounds_to_zero_is_refused(self):
        price, said = self.price("0.4")
        self.assertIsNone(price)
        self.assertTrue(any("числом" in s for s in said))


class WhatTheSellerTypedIsEscapedBeforeItIsShown(unittest.TestCase):
    """Одиночный `<` в названии роняет отправку целиком, и вместо
    предпросмотра продавец видит молчание. Это уже случалось здесь с
    ответом панели."""

    def test_a_broken_tag_in_the_title_does_not_reach_html(self):
        said = C._preview({"title": "Robux <1000", "price": 100,
                           "description": "ок", "quantity": 1})
        self.assertIn("&lt;1000", said)
        self.assertNotIn("<1000", said)

    def test_the_description_is_escaped_too(self):
        said = C._preview({"title": "t", "price": 1,
                           "description": "a < b & c", "quantity": 1})
        self.assertIn("&lt;", said)
        self.assertIn("&amp;", said)

    def test_a_missing_photo_is_named_a_blocker_not_a_detail(self):
        said = C._preview({"title": "t", "price": 1, "description": "d",
                           "quantity": 1})
        self.assertIn("не создать", said)


if __name__ == "__main__":
    unittest.main()
