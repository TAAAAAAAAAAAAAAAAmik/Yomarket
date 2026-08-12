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


class WhatTheSellersOwnCaseShowed(unittest.TestCase):
    """Товар «50 звезд», магазин Spike. `/pos_find` напечатал:

        слова поиска: звезд
        строк вернулось: 45
        нашли по: НЕ НАШЛИ

    Слово было одно — «звезд». Цифра «50» отбрасывалась отсевом «короче
    трёх букв», а в этом магазине названия ровно так и устроены: «50
    звезд», «100 звезд», «500 звезд», и различает их число. По слову
    «звезд» витрина отдала сорок пять чужих строк, нашей среди них не было.
    """

    def test_the_number_survives_the_filter(self):
        self.assertIn("50", M.search_words("50 звезд"))

    def test_and_it_is_tried_before_the_common_word(self):
        keys = M.search_keys("50 звезд")
        self.assertLess(keys.index("50"), keys.index("звезд"))

    def test_the_whole_title_is_tried_as_well(self):
        self.assertIn("50 звезд", M.search_keys("50 звезд"))

    def test_a_two_letter_word_is_still_dropped(self):
        """Отсев был не напрасен: «по», «за», «на» ищут всё подряд."""
        self.assertNotIn("на", M.search_words("на аккаунт"))

    def test_a_single_digit_alone_is_not_a_search_key(self):
        self.assertNotIn("5", M.search_words("5 аккаунтов"))


class TheDiagnosticAnswersTheNextQuestion(unittest.TestCase):
    """«Нас не было в выдаче» и «мы там под другим именем» — разные беды, и
    продавцу для них нужно разное."""

    def setUp(self):
        self._card = M.product_card
        M.product_card = lambda mid: {}
        import requests
        self._req = requests.get

    def tearDown(self):
        M.product_card = self._card
        import requests
        requests.get = self._req

    def fake(self, rows):
        import requests

        class R:
            status_code = 200

            def json(self_inner):
                return {"data": rows}

        requests.get = lambda *a, **kw: R()

    def test_it_lists_the_shops_it_saw(self):
        self.fake([row(1, "50 звезд", "Fgo shop"),
                   row(2, "50 звезд", "Пинкод-Маркет")])
        _got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertIn("Fgo shop", facts["shops"])
        self.assertIn("Пинкод-Маркет", facts["shops"])

    def test_it_says_our_shop_was_not_among_them(self):
        self.fake([row(1, "50 звезд", "Fgo shop")])
        _got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertFalse(facts["ours_seen"])

    def test_and_says_when_it_was(self):
        self.fake([row(1, "50 звезд", OURS)])
        got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertTrue(facts["ours_seen"])
        self.assertTrue(got)

    def test_the_screen_prints_both_the_verdict_and_the_names(self):
        import inspect
        from handlers import panel_items as PI
        src = inspect.getsource(PI.pos_find)
        self.assertIn("ours_seen", src)
        self.assertIn("магазины выдачи", src)


class TheSectionComesFromTheAdItself(unittest.TestCase):
    """Второй вывод `/pos_find` закрыл поиск по витрине как путь вообще:

        строк вернулось: 124
        магазинов в выдаче: 32, наш среди них: НЕТ

    Искать свой товар в чужой выдаче — заведомо слабый способ добраться до
    раздела. А раздел лежит в самом объявлении: Integration API кладёт его
    в `category_id`, и это поле бот выбрасывал, оставляя себе только номер
    и название.
    """

    TREE = [{"id": 7, "slug": "telegram", "title": "Telegram", "children": [
        {"id": 512, "slug": "zvezdy", "title": "Звёзды"}]}]

    def setUp(self):
        from handlers import selenium_settings as SS
        self.SS = SS
        self._tree = M.category_tree
        self._search = M.listing_urls_for
        M.category_tree = lambda: self.TREE
        M.listing_urls_for = lambda *a, **kw: ["ПОИСК-ПО-ВИТРИНЕ"]

    def tearDown(self):
        M.category_tree = self._tree
        M.listing_urls_for = self._search

    def test_the_address_is_built_from_the_ads_category_id(self):
        urls = self.SS._urls_for_ad({"id": "229402", "title": "50 звезд",
                                     "category_id": 512, "category": ""})
        self.assertIn("https://yoomarket.net/categories/telegram/zvezdy", urls)

    def test_and_the_storefront_search_is_not_used_at_all(self):
        urls = self.SS._urls_for_ad({"id": "229402", "title": "50 звезд",
                                     "category_id": 512, "category": ""})
        self.assertNotIn("ПОИСК-ПО-ВИТРИНЕ", urls)

    def test_the_section_alone_is_offered_as_well(self):
        """Товар может стоять в разделе без игры над ним."""
        urls = self.SS._urls_for_ad({"id": "1", "title": "x",
                                     "category_id": 512, "category": ""})
        self.assertIn("https://yoomarket.net/categories/zvezdy", urls)

    def test_the_name_rescues_an_id_from_another_space(self):
        """С номерами товаров пространства уже разошлись; с номерами
        разделов может быть так же, а название общее."""
        urls = self.SS._urls_for_ad({"id": "1", "title": "50 звезд",
                                     "category_id": 999999,
                                     "category": "Звёзды"})
        self.assertIn("https://yoomarket.net/categories/telegram/zvezdy", urls)

    def test_the_search_is_still_there_when_neither_works(self):
        urls = self.SS._urls_for_ad({"id": "1", "title": "50 звезд",
                                     "category_id": None, "category": ""})
        self.assertEqual(urls, ["ПОИСК-ПО-ВИТРИНЕ"])

    def test_the_ad_list_keeps_the_category(self):
        import inspect
        src = inspect.getsource(self.SS._my_ads)
        self.assertIn('"category_id"', src)


class TheSectionComesFromTheNeighbours(unittest.TestCase):
    """Третий вывод `/pos_find` не оставил от прежних путей ничего:

        раздел из API: id=5221 название=—
          по номеру → НЕТ в каталоге витрины
          по названию → НЕТ
        строк вернулось: 124
        магазинов в выдаче: 30, наш среди них: НЕТ

    Своей строки в выдаче нет и не будет: конкурентов сотни, листается три
    страницы. Но нам нужна не своя строка, а **раздел** — а соседи,
    продающие ровно тот же товар, стоят в том же разделе, и их строк витрина
    отдала сто двадцать четыре.

    Догадкой это не остаётся: адрес потом открывается и в нём ищется наша
    строка. Не нашлась — бот так и скажет.
    """

    def rows(self, *pairs):
        out = []
        for i, (title, cid) in enumerate(pairs):
            r = row(1000 + i, title, f"Магазин {i}")
            r["category_id"] = cid
            out.append(r)
        return out

    def test_the_section_most_neighbours_agree_on_wins(self):
        rows = self.rows(("50 звезд", 512), ("50 звезд", 512),
                         ("50 звезд", 999))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_it_says_how_many_agreed(self):
        rows = self.rows(("50 звезд", 512), ("50 звезд", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[1], 2)

    def test_a_different_product_does_not_vote(self):
        """«100 звезд» — другой раздел не обязан, но и голосовать не должен."""
        rows = self.rows(("50 звезд", 512), ("100 звезд", 777),
                         ("100 звезд", 777))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_a_decorated_neighbour_still_counts(self):
        """Точных совпадений может не быть вовсе — тогда годятся строки, где
        есть все наши слова."""
        rows = self.rows(("⚡50 звезд Telegram⚡", 512),
                         ("Аккаунт Steam", 999))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_nothing_relevant_is_an_honest_nothing(self):
        rows = self.rows(("Аккаунт Steam", 999))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд"), (None, 0))

    def test_a_yo_in_the_neighbours_title_is_the_same_word(self):
        """«50 звезд» у нас, «50 звёзд» у соседей — из-за этой буквы раздел
        набрал ноль голосов при ста двадцати четырёх подходящих строках."""
        rows = self.rows(("50 звёзд", 512), ("50 звёзд", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_a_word_stem_is_enough_when_endings_differ(self):
        rows = self.rows(("50 звёздочек Telegram", 512),
                         ("50 звёздочек Telegram", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_a_different_number_is_a_different_product(self):
        """«500 звезд» стоит, может быть, там же — но это не наш товар, и
        голосовать за раздел он не должен."""
        rows = self.rows(("500 звёздочек", 777), ("50 звёздочек", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")


class TheWholeFeedIsNotEvidence(unittest.TestCase):
    """Запас «взять самый частый раздел по всей выдаче» просуществовал один
    деплой и был снят.

    По ключу «50» витрина приводит танковую голду и всё, где есть полсотни
    чего угодно. Большинство в такой выдаче оказалось за `tanks-blitz`, и
    бот уверенно пошёл искать «50 звезд» среди танков: прочитал 675
    предложений и ответил «в этом разделе товара нет».

    Ответ честный, но повод был выдуманный. Такой вывод хуже отсутствия
    вывода: он тратит время и уводит от настоящей причины.
    """

    def rows(self, *pairs):
        return [{"id": i, "title": t, "category_id": c, "shop": {"name": "x"}}
                for i, (t, c) in enumerate(pairs)]

    def test_a_feed_of_strangers_gives_nothing(self):
        rows = self.rows(("50 голды", 900), ("50 боёв", 900), ("50 премок", 900))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд"), (None, 0))

    def test_a_scattered_feed_gives_nothing_either(self):
        rows = self.rows(("а", 1), ("б", 2), ("в", 3), ("г", 4))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд"), (None, 0))

    def test_only_rows_about_our_product_vote(self):
        rows = self.rows(("50 голды", 900), ("50 голды", 900),
                         ("50 голды", 900), ("звёзды Telegram", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")


class RowsFoundByTheBareNumberDoNotVote(unittest.TestCase):
    """Ключ «50» приводит танковую голду и всё, где есть полсотни чего
    угодно. Именно эти строки и составили большинство, отправившее бота
    искать «50 звезд» среди танков.

    Голосовать за раздел должны строки, найденные по СЛОВУ. Число как
    отдельный ключ полезно для поиска своего лота и вредно для вывода о
    разделе.
    """

    def setUp(self):
        import requests
        self._get, self._card = requests.get, M.product_card
        M.product_card = lambda mid: {}

        by_key = {
            # По одному числу приходит всё, где есть полсотни чего угодно —
            # в том числе чужие «звёзды» из другого раздела.
            "50": [{"id": 1, "title": "50 звёзд Warface", "category_id": 900,
                    "shop": {"name": "Танки"}}] * 5,
            "звезд": [{"id": 2, "title": "Звёзды Telegram", "category_id": 512,
                       "shop": {"name": "Сосед"}}],
        }

        class R:
            def __init__(self_inner, rows):
                self_inner.rows, self_inner.status_code = rows, 200

            def json(self_inner):
                return {"data": self_inner.rows}

        def fake(url, params=None, **kw):
            key = (params or {}).get("keyword", "")
            page = int((params or {}).get("page", 1))
            return R(by_key.get(key, []) if page == 1 else [])

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get, M.product_card = self._get, self._card

    def test_the_section_comes_from_the_word_not_the_number(self):
        _got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertEqual(facts["section"], "512")

    def test_the_tank_section_does_not_win_by_headcount(self):
        _got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertNotEqual(facts["section"], "900")

    def test_the_numeric_rows_are_still_searched_for_our_own_lot(self):
        """Отсев касается только голосования: своё объявление по числу
        находить по-прежнему можно и нужно."""
        _got, facts = M.search_own_listing(229402, "50 звезд", OURS)
        self.assertGreater(facts["rows"], facts["by_word_rows"])


class ANeighbourNeedNotRepeatOurNumber(unittest.TestCase):
    """Мы ищем **раздел**, а не свой лот. Сосед может продавать звёзды
    пачками на выбор и не писать в названии числа — а стоять там же, где
    мы. Требование числа отсекало ровно таких соседей."""

    def rows(self, *pairs):
        return [{"id": i, "title": t, "category_id": c, "shop": {"name": "x"}}
                for i, (t, c) in enumerate(pairs)]

    def test_a_numberless_neighbour_still_names_the_section(self):
        rows = self.rows(("Звёзды Telegram", 512), ("Звёзды Telegram", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_it_is_reported_as_the_looser_match(self):
        rows = self.rows(("Звёзды Telegram", 512))
        self.assertEqual(M.neighbours_of(rows, "50 звезд")[1],
                         "корни слов без числа")

    def test_an_exact_match_still_wins_over_it(self):
        rows = self.rows(("Звёзды Telegram", 777), ("50 звёзд", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")

    def test_rows_without_a_section_do_not_vote(self):
        rows = self.rows(("50 звезд", None), ("50 звезд", 512))
        self.assertEqual(M.section_of_neighbours(rows, "50 звезд")[0], "512")


class TheAddressIsBuiltFromTheNeighboursSection(unittest.TestCase):
    TREE = [{"id": 7, "slug": "telegram", "title": "Telegram", "children": [
        {"id": 512, "slug": "zvezdy", "title": "Звёзды"}]}]

    def setUp(self):
        self._tree, self._search = M.category_tree, M.search_own_listing
        M.category_tree = lambda: self.TREE

    def tearDown(self):
        M.category_tree, M.search_own_listing = self._tree, self._search

    def test_our_row_missing_but_the_section_known(self):
        M.search_own_listing = lambda mid, t="", s="": ({}, {"section": "512"})
        urls = M.listing_urls_for(229402, None, "50 звезд", "Spike")
        self.assertIn("https://yoomarket.net/categories/telegram/zvezdy", urls)

    def test_end_to_end_from_the_real_search(self):
        """Сквозь настоящий `search_own_listing`: ровно случай продавца —
        своей строки в выдаче нет, чужие с тем же товаром есть."""
        import requests
        old_get, old_card = requests.get, M.product_card
        M.product_card = lambda mid: {}
        neighbours = [{"id": 22424, "title": "50 звезд", "category_id": 512,
                       "shop": {"name": "Empathy"}},
                      {"id": 86474, "title": "50 звезд", "category_id": 512,
                       "shop": {"name": "Fgo shop"}}]

        class R:
            status_code = 200

            def json(self_inner):
                return {"data": neighbours}

        requests.get = lambda *a, **kw: R()
        try:
            urls = M.listing_urls_for(229402, None, "50 звезд", "Spike")
        finally:
            requests.get, M.product_card = old_get, old_card
        self.assertIn("https://yoomarket.net/categories/telegram/zvezdy", urls)

    def test_neither_the_row_nor_the_section_gives_nothing(self):
        """Пусто — честный ответ. Дальше продавцу предлагают каталог."""
        M.search_own_listing = lambda mid, t="", s="": ({}, {"section": None})
        self.assertEqual(M.listing_urls_for(229402, None, "50 звезд"), [])


class TheReferenceFillsTheMissingCategoryName(unittest.TestCase):
    """API кладёт рядом с объявлением только номер раздела, и номер этот из
    своего пространства: 5221 в каталоге витрины нет. Название общее — но
    его надо дочитать из справочника /categories."""

    def test_the_ad_list_resolves_names_from_the_reference(self):
        import inspect
        from handlers import selenium_settings as SS
        src = inspect.getsource(SS._my_ads)
        self.assertIn("find_categories", src)
        # И результат справочника доходит до объявления, а не остаётся
        # прочитанным впустую.
        self.assertIn("_from_reference(a, names)", src)

    def test_a_resolved_name_reaches_the_ad(self):
        from handlers import selenium_settings as SS
        self.assertEqual(
            SS._from_reference({"category_id": 5221}, {5221: "Звёзды"}),
            "Звёзды")

    def test_an_unresolved_id_is_an_empty_name_not_the_number(self):
        from handlers import selenium_settings as SS
        self.assertEqual(SS._from_reference({"category_id": 5221}, {}), "")


class TheCatalogueLookupByName(unittest.TestCase):
    TREE = [{"id": 7, "slug": "telegram", "title": "Telegram", "children": [
        {"id": 512, "slug": "zvezdy", "title": "Звёзды"}]},
        {"id": 8, "slug": "steam", "title": "Steam"}]

    def setUp(self):
        self._tree = M.category_tree
        M.category_tree = lambda: self.TREE

    def tearDown(self):
        M.category_tree = self._tree

    def test_a_nested_section_gives_the_whole_chain(self):
        self.assertEqual(M.category_slugs_by_title("Звёзды"),
                         ["telegram", "zvezdy"])

    def test_case_does_not_matter(self):
        self.assertEqual(M.category_slugs_by_title("звёзды"),
                         ["telegram", "zvezdy"])

    def test_a_top_level_section_works_too(self):
        self.assertEqual(M.category_slugs_by_title("Steam"), ["steam"])

    def test_an_unknown_name_gives_nothing_rather_than_a_guess(self):
        self.assertEqual(M.category_slugs_by_title("Чего нет"), [])

    def test_an_empty_name_is_not_a_match_for_everything(self):
        self.assertEqual(M.category_slugs_by_title(""), [])


class TheFlowPassesTheShopName(unittest.TestCase):
    def test_the_address_builder_takes_a_seller(self):
        import inspect
        self.assertIn("seller",
                      inspect.signature(M.listing_urls_for).parameters)

    def test_and_hands_it_to_the_search(self):
        import inspect
        self.assertIn("search_own_listing(market_id, title, seller)",
                      inspect.getsource(M.listing_urls_for))

    def test_the_screen_gives_the_shop_name_to_the_builder(self):
        import inspect
        from handlers import selenium_settings as SS
        self.assertIn('ad["title"], shop', inspect.getsource(SS._urls_for_ad))

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
