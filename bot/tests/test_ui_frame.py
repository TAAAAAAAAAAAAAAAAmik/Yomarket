"""Каркас экранов — один на весь бот.

Уведомления рисовались карточкой и читались за секунду; экраны меню
собирались вручную, сто двадцать семь мест с `"\n".join(...)`, и у каждого
был свой отступ после заголовка, свой подвал, свой предел обрезки. Разнобой
виден не по отдельному экрану, а по переходу между ними.

Здесь проверяется то, на чём такой каркас обычно и ломается: съеденные
пустые строки, обрыв посреди слова и чужой текст, роняющий отправку.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import ui                                        # noqa: E402


class TheFrameKeepsTheBlankLinesOnPurpose(unittest.TestCase):
    """Пустая строка в теле — это отступ между блоками, а не пустое место.
    Фильтр по правдивости однажды съел их все разом, и экран слипся."""

    def test_a_blank_line_survives(self):
        got = ui.screen("Ш", ["раз", "", "два"])
        self.assertIn("раз\n\nдва", got)

    def test_a_none_is_dropped(self):
        """`None` — это «строки нет»: так удобно писать условные блоки."""
        got = ui.screen("Ш", ["раз", None, "два"])
        self.assertIn("раз\nдва", got)


class EveryScreenPutsTheSameThingInTheSamePlace(unittest.TestCase):
    def test_the_rule_separates_the_head_from_the_body(self):
        got = ui.screen("🎁 <b>Карты</b>", ["тело"]).splitlines()
        self.assertEqual(got[0], "🎁 <b>Карты</b>")
        self.assertEqual(got[1], ui.RULE)
        self.assertEqual(got[2], "тело")

    def test_the_shop_name_goes_into_the_head(self):
        got = ui.screen("🎁 <b>Карты</b>", [], subtitle="Мой магазин")
        self.assertIn(f"</b> {ui.SEP} Мой магазин", got)

    def test_without_a_shop_there_is_no_dangling_separator(self):
        got = ui.screen("🎁 <b>Карты</b>", [])
        self.assertNotIn(ui.SEP, got.splitlines()[0])

    def test_the_footer_is_set_off_by_its_own_rule(self):
        got = ui.screen("Ш", ["тело"], footer="подвал")
        self.assertTrue(got.endswith(f"\n{ui.RULE}\nподвал"))

    def test_no_footer_means_no_second_rule(self):
        """Пустая линейка в конце читается как оборванный экран."""
        got = ui.screen("Ш", ["тело"])
        self.assertEqual(got.count(ui.RULE), 1)


class NothingIsCutInTheMiddleOfAWord(unittest.TestCase):
    """«…"subcategor» — так обрывался отчёт у продавца. Пересылая его в
    поддержку, он отправлял огрызок."""

    def test_a_long_screen_is_cut_on_a_boundary(self):
        got = ui.clip("раз два три четыре пять шесть", 14)
        self.assertTrue(got.endswith("…"))
        self.assertFalse(got[:-1].endswith(" "))

    def test_a_short_screen_is_left_alone(self):
        self.assertEqual(ui.clip("коротко", 100), "коротко")

    def test_one_limit_for_everyone(self):
        """Пределов было три — 3500, 3900 и 4000, — то есть правила не было,
        было три случая. И все три ниже потолка Telegram в 4096."""
        self.assertLess(ui.LIMIT, 4096)


class SomeoneElsesTextCannotBreakTheSend(unittest.TestCase):
    """Одиночный `<` в названии магазина роняет отправку целиком, и продавец
    не получает ничего."""

    def test_the_subtitle_is_escaped(self):
        got = ui.screen("Ш", [], subtitle="Магазин <Спайк>")
        self.assertIn("&lt;Спайк&gt;", got)
        self.assertNotIn("<Спайк>", got)

    def test_the_title_keeps_our_own_markup(self):
        """Заголовок наш собственный: экранировать его значит показать
        продавцу теги буквами — ровно то, от чего уходили в отчётах."""
        got = ui.screen("🎁 <b>Карты</b>", [])
        self.assertIn("<b>Карты</b>", got)

    def test_esc_survives_none(self):
        self.assertEqual(ui.esc(None), "")


class WhatOnlyTheOwnerSees(unittest.TestCase):
    """Код сборки и советы набрать служебную команду — не для продавца.

    Он купил подписку на сервис, а не экскурсию по серверу: метка сборки
    ему ничего не объясняет, а `/cat_debug` у него попросту нет — наберёт и
    получит молчание, как на несуществующую команду.

    Но и выбрасывать это нельзя: читает метку владелец, и без неё «кнопка
    не работает» и «в контейнере старый код» снова станут неотличимы.
    Поэтому одно место на весь бот — здесь и проверяется, что оно молчит
    продавцу и говорит админу.
    """

    def setUp(self):
        import storage
        self.storage = storage
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._owner = storage.is_owner
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage.is_owner = lambda uid: False

    def tearDown(self):
        self.storage._load_admin = self._load
        self.storage._save_admin = self._save
        self.storage.is_owner = self._owner

    def test_the_seller_gets_nothing_at_all(self):
        """Пустая строка, а не «—»: место, где метки нет, не занято."""
        self.assertEqual(ui.build_mark(7), "")
        self.assertEqual(ui.admin_hint(7, "проверь /cat_debug"), "")

    def test_the_admin_gets_the_build_code(self):
        from handlers.start import BOT_VERSION
        self.storage.add_admin(7)
        self.assertIn(BOT_VERSION, ui.build_mark(7))
        self.assertIn("/cat_debug", ui.admin_hint(7, "проверь /cat_debug"))

    def test_nobody_is_nobody(self):
        """Экраны, у которых номера под рукой нет, зовут с нулём."""
        self.assertEqual(ui.build_mark(0), "")
        self.assertEqual(ui.build_mark(None), "")

    def test_a_blank_line_inside_a_hint_stays_a_blank_line(self):
        """Пустая строка — намеренный отступ. Фильтр по правдивости
        однажды уже съел все разделители разом."""
        self.storage.add_admin(7)
        self.assertEqual(ui.admin_hint(7, "раз", "", "два"), "раз\n\nдва")


if __name__ == "__main__":
    unittest.main()
