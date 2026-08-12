"""Свой товар на витрине узнаётся по магазину, а не только по номеру.

Продавец: «бот не может найти сам объявления в ленте». Слежение за
позицией начинается с того, что бот ищет свой товар на витрине — оттуда
берётся раздел, по которому потом считается место. Этот первый шаг
сверял **только номер**: номер объявления из Integration API против
номера строки на витрине.

Пространства номеров разные. Когда они не совпадают, поиск возвращал
нашу же строку и выбрасывал её, а продавец читал «Не нашёл этот товар в
поиске витрины» про товар, который там стоит.

Обиднее всего, что шагом ниже, в `find_position`, магазин и название
учитывались с самого начала: первый шаг был строже второго, и падал
именно он.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import market as M     # noqa: E402


def row(rid, title, shop):
    return {"id": rid, "title": title, "slug": f"lot-{rid}",
            "shop": {"name": shop}}


OURS = "Spike"
TITLE = "🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫"


class OurRowWhenTheIdsDoNotLineUp(unittest.TestCase):
    """Ровно случай продавца: строка на витрине наша, номер другой."""

    ROWS = [row(90001, TITLE, "Другой магазин"),
            row(90002, TITLE, OURS),
            row(90003, TITLE, "Третий")]

    def test_the_shop_name_finds_it(self):
        got = M.pick_ours(self.ROWS, TITLE, OURS)
        self.assertEqual(got["id"], 90002)

    def test_it_is_not_the_first_row_of_the_search(self):
        """Взять первую было бы «нашли» с чужим разделом."""
        got = M.pick_ours(self.ROWS, TITLE, OURS)
        self.assertNotEqual(got["id"], 90001)

    def test_without_the_shop_name_identical_titles_are_not_resolved(self):
        """Названия в выдаче почти одинаковые — это копии одного товара у
        разных продавцов. Угадывать тут нечего."""
        self.assertEqual(M.pick_ours(self.ROWS, TITLE, ""), {})


class SeveralOfOurOwnOnOnePage(unittest.TestCase):
    ROWS = [row(1, "100 звёзд", OURS), row(2, "500 звёзд", OURS)]

    def test_the_title_picks_the_right_one(self):
        self.assertEqual(M.pick_ours(self.ROWS, "500 звёзд", OURS)["id"], 2)

    def test_a_partial_title_still_picks(self):
        self.assertEqual(M.pick_ours(self.ROWS, "500", OURS)["id"], 2)

    def test_no_title_match_is_a_refusal_not_a_coin_toss(self):
        """Не тот раздел — это потом «мы там не стоим» и час поисков."""
        self.assertEqual(M.pick_ours(self.ROWS, "1000 звёзд", OURS), {})

    def test_a_single_row_of_ours_needs_no_title(self):
        self.assertEqual(M.pick_ours([row(7, "что угодно", OURS)], "", OURS)["id"], 7)


class WhenTheShopIsUnknown(unittest.TestCase):
    def test_a_unique_exact_title_is_enough(self):
        rows = [row(1, "Аккаунт Steam", "Чужой"), row(2, "Другое", "Чужой")]
        self.assertEqual(M.pick_ours(rows, "Аккаунт Steam", "")["id"], 1)

    def test_two_rows_with_the_same_title_are_not(self):
        rows = [row(1, "Аккаунт Steam", "А"), row(2, "Аккаунт Steam", "Б")]
        self.assertEqual(M.pick_ours(rows, "Аккаунт Steam", ""), {})

    def test_nothing_found_is_not_a_crash(self):
        self.assertEqual(M.pick_ours([], TITLE, OURS), {})


class TheSearchReportsWhatItDid(unittest.TestCase):
    """Факты словарём, а не прозой: диагностика не должна разбирать
    собственный отчёт."""

    def setUp(self):
        self._card = M.product_card
        self._req = None
        M.product_card = lambda mid: {}

    def tearDown(self):
        M.product_card = self._card
        if self._req is not None:
            sys.modules["requests"].get = self._req

    def fake_search(self, rows):
        import requests
        self._req = requests.get

        class R:
            status_code = 200

            def json(self_inner):
                return {"data": rows}

        requests.get = lambda *a, **kw: R()

    def test_it_names_the_words_it_tried(self):
        self.fake_search([])
        _got, facts = M.search_own_listing(1, "Аккаунт Steam", OURS)
        self.assertIn("Аккаунт", facts["keys"])

    def test_it_says_how_it_matched(self):
        self.fake_search([row(90002, TITLE, OURS)])
        got, facts = M.search_own_listing(11111, TITLE, OURS)
        self.assertTrue(got)
        self.assertEqual(facts["by"], "магазин и название")

    def test_a_matching_id_is_still_the_first_answer(self):
        self.fake_search([row(11111, TITLE, "Кто угодно")])
        got, facts = M.search_own_listing(11111, TITLE, OURS)
        self.assertEqual(facts["by"], "номер")
        self.assertEqual(got["id"], 11111)

    def test_a_miss_lists_the_ids_and_shops_it_did_see(self):
        """По ним и видно, что номера из разных пространств."""
        self.fake_search([row(90001, "Чужое", "Чужой")])
        got, facts = M.search_own_listing(11111, TITLE, OURS)
        self.assertEqual(got, {})
        self.assertIn("90001", facts["ids"])
        self.assertIn("Чужой", facts["sellers"])

    def test_a_miss_says_so_rather_than_leaving_it_blank(self):
        self.fake_search([])
        _got, facts = M.search_own_listing(11111, TITLE, OURS)
        self.assertEqual(facts["by"], "")
        self.assertEqual(facts["rows"], 0)


class TheFlowPassesTheShopName(unittest.TestCase):
    def test_the_address_builder_takes_a_seller(self):
        import inspect
        self.assertIn("seller",
                      inspect.signature(M.listing_urls_for).parameters)

    def test_and_hands_it_to_the_search(self):
        import inspect
        self.assertIn("find_own_listing(market_id, title,\n",
                      inspect.getsource(M.listing_urls_for))

    def test_the_screen_gives_the_shop_name_to_the_builder(self):
        import inspect
        from handlers import selenium_settings as SS
        src = inspect.getsource(SS._find_listing_page)
        self.assertIn('ad["title"], shop', src)

    def test_the_diagnostic_exists_and_is_unique(self):
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
        self.assertEqual(names.count("pos_find"), 1, names)


if __name__ == "__main__":
    unittest.main()
