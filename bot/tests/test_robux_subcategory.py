"""Подкатегория для товара из плагина Robux подбирается на живых названиях.

Подсказка мастеру была `["robux", "roblox"]` — по здравому смыслу верная и
на настоящей панели не работающая. Живой список подкатегорий под категорией
Roblox, снятый 19.08:

    Аккаунты · Игровая валюта · Буст · Скины · Услуги · Roblox Studio
    Другое · Аренда · Обучение · Подписки · Кланы · Roblox Plus

Подкатегории с именем «Робуксы» **не существует**. Слово «robux» не
встречается ни в одном названии, а «roblox» совпадает сразу с двумя —
Roblox Studio и Roblox Plus, — то есть подбор честно отказывался выбирать и
спрашивал продавца. Robux по смыслу это «Игровая валюта», и без этого слова
автоподбор подкатегории не срабатывал вообще.

Тест держит именно живые названия: проверка на выдуманном списке
(«Робуксы», «Аккаунты») прошла бы и с прежней подсказкой, ничего не поймав.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import create_ad as C          # noqa: E402


def rows(*labels) -> list:
    return [{"label": name, "value": i} for i, name in enumerate(labels, 1)]


# Снято с панели 19.08 — менять только по новой живой проверке.
SUBCATEGORIES = rows("Аккаунты", "Игровая валюта", "Буст", "Скины", "Услуги",
                     "Roblox Studio", "Другое", "Аренда", "Обучение",
                     "Подписки", "Кланы", "Roblox Plus")
CATEGORIES = rows("7 Days to Die", "Roblox", "Rust", "Roblox Studio")


def hint() -> list:
    """Подсказка, которую плагин передаёт мастеру."""
    import inspect
    import re
    from handlers import plugins as P
    src = inspect.getsource(P.roblox_den_price)
    found = re.search(r"autopick=\[(.*?)\]", src, re.S)
    assert found, "плагин перестал передавать подсказку мастеру"
    return [w.strip().strip("\"'") for w in found.group(1).split(",")
            if w.strip()]


class TheSubcategoryIsPickedOnTheNamesThePanelActuallyHas(unittest.TestCase):

    def test_there_is_no_subcategory_called_robux(self):
        """Факт, с которого всё началось: имени «Робуксы» в панели нет."""
        self.assertFalse([o for o in SUBCATEGORIES
                          if "робукс" in o["label"].lower()
                          or "robux" in o["label"].lower()])

    def test_the_word_roblox_alone_matches_two_and_decides_nothing(self):
        """Studio и Plus — оба про Roblox. Взять первый значило бы выставить
        код валюты в чужом разделе."""
        self.assertIsNone(C._autopick_match(SUBCATEGORIES, ["robux", "roblox"]))

    def test_the_hint_carries_the_name_that_does_match(self):
        self.assertIn("игровая валюта", [w.lower() for w in hint()])

    def test_and_so_the_subcategory_is_chosen(self):
        got = C._autopick_match(SUBCATEGORIES, hint())
        self.assertIsNotNone(got, "подкатегория снова не подбирается")
        self.assertEqual(got["label"], "Игровая валюта")

    def test_the_category_still_resolves_to_roblox(self):
        """Слово добавлено в середину — категорию оно не должно перебивать."""
        got = C._autopick_match(CATEGORIES, hint())
        self.assertIsNotNone(got)
        self.assertEqual(got["label"], "Roblox")

    def test_a_panel_with_a_real_robux_section_wins_over_the_fallback(self):
        """Слова идут от узкого к широкому: заведись «Робуксы» — возьмётся
        она, без правки кода."""
        rich = SUBCATEGORIES + rows("Робуксы")
        got = C._autopick_match(rich, ["robux", "робуксы"] + hint())
        self.assertEqual(got["label"], "Робуксы")


if __name__ == "__main__":
    unittest.main()
