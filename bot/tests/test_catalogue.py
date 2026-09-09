"""Каталог разделов читается, в какой бы обёртке он ни приехал.

Продавец: «разделы не выходят». Экран выбора раздела показывал «каталог
витрины сейчас не читается» — и это был не сбой сети. Разбор был в одну
строчку, `body["data"]`, и стоило каталогу приехать под другим ключом или
уровнем глубже, как список получался пустой.

Ломало это сразу обе дороги к разделу: и кнопки, которыми продавец
называет раздел руками, и автоматику — она превращает найденный раздел в
адрес через тот же каталог. Семь кругов правок поиска соседей были
бесполезны, пока каталог пуст: находить раздел и не уметь превратить его
в адрес — одно и то же, что не находить.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import market as M     # noqa: E402


SECTIONS = [{"id": 7, "slug": "telegram", "title": "Telegram",
             "children": [{"id": 512, "slug": "zvezdy", "title": "Звёзды"}]},
            {"id": 8, "slug": "steam", "title": "Steam"}]


class Answering(unittest.TestCase):
    """Витрина отвечает тем, что положили в `self.body`."""

    def setUp(self):
        import requests
        self._get = requests.get
        self.asked: list[str] = []
        self.body: object = {}
        self.status = 200
        outer = self

        class R:
            def __init__(self_inner):
                self_inner.status_code = outer.status
                self_inner.content = b"x" * 10
                self_inner.text = "текст"

            def json(self_inner):
                if isinstance(outer.body, Exception):
                    raise outer.body
                return outer.body

        def fake(url, params=None, **kw):
            outer.asked.append(url)
            return R()

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get = self._get


class WhateverKeyItArrivesUnder(Answering):
    def test_the_plain_data_key(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(len(M.category_tree()), 2)

    def test_a_bare_list(self):
        self.body = SECTIONS
        self.assertEqual(len(M.category_tree()), 2)

    def test_one_level_deeper(self):
        """Ровно то, чего прежний разбор не переживал."""
        self.body = {"data": {"categories": SECTIONS}}
        self.assertEqual(len(M.category_tree()), 2)

    def test_under_a_different_name_entirely(self):
        self.body = {"result": {"items": SECTIONS}}
        self.assertEqual(len(M.category_tree()), 2)

    def test_an_empty_answer_is_still_empty(self):
        self.body = {"data": []}
        self.assertEqual(M.category_tree(), [])

    def test_junk_is_not_mistaken_for_a_catalogue(self):
        """Список из двух чисел — не разделы."""
        self.body = {"data": [1, 2, 3]}
        self.assertEqual(M.category_tree(), [])

    def test_rows_without_a_slug_but_with_a_name_still_count(self):
        self.body = {"data": [{"id": 1, "title": "Игры"},
                              {"id": 2, "title": "Аккаунты"}]}
        self.assertEqual(len(M.category_tree()), 2)


class WhenTheFirstAddressFails(Answering):
    def test_a_non_200_does_not_end_the_search(self):
        self.status = 404
        self.body = {"data": SECTIONS}
        self.assertEqual(M.category_tree(), [])
        self.assertGreater(len(self.asked), 1, "остальные адреса не пробовали")

    def test_unparseable_json_does_not_crash(self):
        self.body = ValueError("не JSON")
        self.assertEqual(M.category_tree(), [])

    def test_the_first_good_answer_stops_the_walk(self):
        self.body = {"data": SECTIONS}
        M.category_tree()
        self.assertEqual(len(self.asked), 1)


class TheButtonsThatComeOutOfIt(Answering):
    def test_the_top_level_becomes_buttons(self):
        self.body = {"data": SECTIONS}
        rows = M.category_children("")
        self.assertEqual([r["slug"] for r in rows], ["telegram", "steam"])

    def test_a_game_with_sections_is_marked_as_a_step_deeper(self):
        self.body = {"data": SECTIONS}
        rows = M.category_children("")
        self.assertTrue(rows[0]["has_children"])
        self.assertFalse(rows[1]["has_children"])

    def test_the_sections_inside_a_game(self):
        """Продавец про это и сказал: «в телеграме это звёзды, премиум»."""
        self.body = {"data": SECTIONS}
        rows = M.category_children("telegram")
        self.assertEqual([r["slug"] for r in rows], ["zvezdy"])

    def test_an_empty_catalogue_gives_no_buttons(self):
        self.body = {"data": []}
        self.assertEqual(M.category_children(""), [])


class TheProbeSaysWhatCameBack(Answering):
    """Экран говорил «каталог не читается» и ничего больше. Чинить это можно
    было только гаданием, чего в этом проекте делать нельзя."""

    def test_it_reports_the_status_and_the_shape(self):
        self.body = {"data": SECTIONS}
        rows = M.category_tree_probe()
        self.assertEqual(rows[0]["status"], 200)
        self.assertIn("dict", rows[0]["shape"])

    def test_it_counts_what_looked_like_sections(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.category_tree_probe()[0]["found"], 2)

    def test_it_shows_the_first_row_as_it_came(self):
        self.body = {"data": SECTIONS}
        self.assertIn("telegram", M.category_tree_probe()[0]["first"])

    def test_a_bad_answer_is_reported_not_hidden(self):
        self.status = 500
        self.body = {"data": SECTIONS}
        rows = M.category_tree_probe()
        self.assertEqual(rows[0]["status"], 500)
        self.assertFalse(rows[0].get("found"))

    def test_every_address_is_tried_when_nothing_works(self):
        self.body = {"nothing": 1}
        self.assertEqual(len(M.category_tree_probe()),
                         len(M._CATALOGUE_TRIES))

    def test_the_command_exists_once(self):
        """Две команды с одним именем — не ошибка, а тишина."""
        import ast
        import pathlib
        names = []
        root = pathlib.Path(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))) / "handlers"
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "id", "") == "Command"):
                    names += [a.value for a in node.args
                              if isinstance(a, ast.Constant)]
        self.assertEqual(names.count("cat_debug"), 1, names)


class TheTopLevelArrivesFlat(Answering):
    """`/cat_debug` на живой витрине: 60 игр, ни у одной вложенных разделов.

        tanks-blitz — Tanks Blitz
        tiktok — TikTok
        …

    Считать их листьями нельзя: внутри «Telegram» живут «Звёзды»,
    «Премиум» и прочее, и следить надо за разделом, а не за игрой целиком —
    иначе позиция считается среди тысяч чужих товаров. Подразделы витрина
    отдаёт отдельным запросом.
    """

    FLAT = [{"id": 1, "slug": "tanks-blitz", "title": "Tanks Blitz"},
            {"id": 2, "slug": "telegram", "title": "Telegram"}]
    KIDS = [{"id": 512, "slug": "zvezdy", "title": "Звёзды"},
            {"id": 513, "slug": "premium", "title": "Премиум"}]

    def by_params(self):
        """Верхний уровень плоский; подразделы — только с параметром."""
        import requests
        outer = self

        class R:
            def __init__(self_inner, rows):
                self_inner.status_code = 200
                self_inner.content = b"x"
                self_inner.text = ""
                self_inner._rows = rows

            def json(self_inner):
                return {"data": self_inner._rows}

        def fake(url, params=None, **kw):
            outer.asked.append(url + str(params or ""))
            if params and params.get("parent") == "telegram":
                return R(outer.KIDS)
            if params:
                return R([])
            return R(outer.FLAT)

        requests.get = fake

    def test_the_children_are_fetched_separately(self):
        self.by_params()
        kids = M.fetch_category_children("telegram")
        self.assertEqual([k["slug"] for k in kids], ["zvezdy", "premium"])

    def test_the_screen_gets_them_through_category_children(self):
        self.by_params()
        rows = M.category_children("telegram")
        self.assertEqual([r["slug"] for r in rows], ["zvezdy", "premium"])

    def test_the_parent_itself_is_not_its_own_child(self):
        self.by_params()
        self.assertNotIn("telegram",
                         [k["slug"] for k in M.fetch_category_children("telegram")])

    def test_a_game_with_no_sections_says_so_rather_than_looping(self):
        """`/api/categories/<slug>` у части витрин отдаёт весь каталог
        заново. Принять его за подразделы значит показать список игр под
        видом разделов «Telegram»."""
        self.by_params()
        self.assertEqual(M.fetch_category_children("tanks-blitz"), [])

    def test_and_says_so_in_the_trace(self):
        self.by_params()
        trace: list = []
        M.fetch_category_children("tanks-blitz", None, trace)
        self.assertTrue(any("весь каталог" in t for t in trace), trace)

    def test_the_trace_records_which_address_answered(self):
        self.by_params()
        trace: list = []
        M.fetch_category_children("telegram", None, trace)
        self.assertTrue(any("2 подраздел" in t for t in trace), trace)

    def test_inline_children_are_still_preferred(self):
        """Лишний запрос там, где ответ уже на руках, — просто трата."""
        self.body = {"data": [{"id": 2, "slug": "telegram", "title": "Telegram",
                               "children": self.KIDS}]}
        rows = M.category_children("telegram")
        self.assertEqual([r["slug"] for r in rows], ["zvezdy", "premium"])
        self.assertEqual(len(self.asked), 1)


class TheNodeItself(Answering):
    def test_a_game_is_found_by_slug(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.category_node("telegram")["id"], 7)

    def test_so_is_a_section_inside_it(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.category_node("zvezdy")["title"], "Звёзды")

    def test_an_unknown_slug_is_empty(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.category_node("нет-такого"), {})


class TheSectionToAddressStepNeedsIt(Answering):
    """Найти раздел и не суметь превратить его в адрес — то же самое, что
    не найти. Все три дороги упираются в каталог."""

    def test_by_slug(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.slugs_for_section("zvezdy"), ["telegram", "zvezdy"])

    def test_by_number(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.slugs_for_section(512), ["telegram", "zvezdy"])

    def test_by_name(self):
        self.body = {"data": SECTIONS}
        self.assertEqual(M.slugs_for_section("Звёзды"), ["telegram", "zvezdy"])

    def test_an_empty_catalogue_makes_all_three_useless(self):
        self.body = {"data": []}
        for ref in ("zvezdy", 512, "Звёзды"):
            self.assertEqual(M.slugs_for_section(ref), [], ref)


if __name__ == "__main__":
    unittest.main()
