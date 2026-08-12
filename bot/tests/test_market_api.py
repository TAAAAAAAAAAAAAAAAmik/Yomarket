"""Reading the listing from the API the storefront itself calls.

Found with /pos_api on the seller's own address:

    https://api.yoo.market/api/products?category=virty&keyword=3.000.000
    → {"data": [ …15 offers… ], "meta": {…}, "links": {…}}

Fifteen offers a page against a category of 638, so the paging is the feature,
not a detail — the seller's listing sits far below the first page, and a
position counted inside one page would be wrong in the direction that spends
money.
"""
from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import market as M          # noqa: E402

# Several cases cut the network on purpose; the warning that follows is the
# behaviour under test, not something to read in the output.
logging.getLogger("automation.market").setLevel(logging.CRITICAL)

PAGE_URL = ("https://yoomarket.net/categories/black-russia/virty"
            "?keyword=3.000.000")


TOTAL = 638          # what /api/categories says this section holds


def body(rows: list, **extra) -> dict:
    """A response shaped like the real one, and consistent with the section.

    `meta.total` matching the section's ads_count is what tells a correct query
    from one that returns a different catalogue, so a fixture without it makes
    the test measure the query search instead of what it means to.
    """
    out = {"data": rows, "meta": {"total": TOTAL}}
    out.update(extra)
    return out


def offer(n: int, shop: str, price: float = 239.0) -> dict:
    """One row shaped like the real response."""
    return {"id": 196000 + n, "title": f"💸3.000.000⚡ЛЮБОЙ СЕРВЕР лот {n}",
            "price": price, "shop": {"id": n, "name": shop},
            "rating": 4.8, "reviews_count": 100 + n}


class Reply:
    def __init__(self, payload, code=200):
        self._payload = payload
        self.status_code = code
        self.headers = {"content-type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class ApiCase(unittest.TestCase):
    def setUp(self):
        import requests
        self._old = requests.get
        self.calls: list[tuple[str, dict]] = []       # /api/products only
        self.meta_calls: list[str] = []
        self.pages: dict[int, dict] = {}
        # What /api/categories/<slugs> answers. None = no metadata at all,
        # which is how a listing that is only a search behaves.
        self.category = {"id": 77, "slug": "black-russia",
                         "title": "Black Russia", "ads_count": 638,
                         "children": [{"id": 512, "slug": "virty",
                                       "title": "Вирты", "ads_count": 638}]}

        def fake(url, params=None, **kw):
            if "/api/categories" in url:
                self.meta_calls.append(url)
                return Reply(self.category or {})
            self.calls.append((url, dict(params or {})))
            n = int((params or {}).get("page", 0) or 0)
            if not n:
                # links.next carries the page inside the URL
                import re
                m = re.search(r"page=(\d+)", url)
                n = int(m.group(1)) if m else 1
            body = self.pages.get(n)
            if body is None:
                return Reply({"data": []})
            return Reply(body)

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get = self._old


class TheQuery(unittest.TestCase):
    def test_the_deepest_slug_is_the_category(self):
        """/categories/black-russia/virty asks for category=virty.

        The game above it is not what the listing is filtered by — asking for
        black-russia would return a different, much larger listing and every
        position in it would be wrong.
        """
        self.assertEqual(M.listing_query(PAGE_URL),
                         {"category": "virty", "keyword": "3.000.000"})

    def test_a_category_without_a_subcategory(self):
        self.assertEqual(
            M.listing_query("https://yoomarket.net/categories/black-russia"),
            {"category": "black-russia"})

    def test_a_plain_search_has_no_category(self):
        self.assertEqual(
            M.listing_query("https://yoomarket.net/products?keyword=вирты"),
            {"keyword": "вирты"})

    def test_a_page_number_in_the_address_is_not_carried_over(self):
        self.assertNotIn("page", M.listing_query(PAGE_URL + "&page=4"))

    def test_other_filters_are_kept(self):
        got = M.listing_query(PAGE_URL + "&sort=price&online=1")
        self.assertEqual(got["sort"], "price")
        self.assertEqual(got["online"], "1")


class Fetching(ApiCase):
    def test_the_first_page_is_asked_for_with_the_pages_own_filters(self):
        self.pages = {1: body([offer(1, "GigShop")])}
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, res)
        url, params = self.calls[0]
        self.assertEqual(url, "https://api.yoo.market/api/products")
        self.assertEqual(params.get("keyword"), "3.000.000",
                         "the search must reach the API")
        # The section, not the game and not «вирты вообще»: /api/categories
        # gave «Вирты в Black Russia» the id 512.
        self.assertEqual(params.get("category_id"), 512)
        self.assertIn("запрос:", res["note"])

    def test_the_position_runs_across_pages(self):
        self.pages = {
            1: body([offer(i, f"Продавец{i}") for i in range(15)],
                    links={"next": "https://api.yoo.market/api/products?page=2"}),
            2: body([offer(20 + i, f"Другой{i}") for i in range(14)]
                    + [offer(99, "Spike", 239.99)]),
        }
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok, res)
        spike = M.find_position(res["offers"], seller="Spike")
        self.assertEqual(spike["pos"], 30, "position restarted on page 2")
        self.assertEqual(spike["price"], 239.99)
        self.assertIn("из 638", res["note"])

    def test_it_stops_the_moment_we_are_found(self):
        self.pages = {1: body([offer(1, "A"), offer(2, "Spike")]),
                      2: body([offer(3, "B")])}
        M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertEqual(len(self.calls), 1,
                         "found on page 1 — the rest is wasted money and time")

    def test_the_next_link_is_followed_when_given(self):
        self.pages = {
            1: body([offer(1, "A")],
                    links={"next": "https://api.yoo.market/api/products"
                                   "?category=virty&page=2"}),
            2: body([offer(2, "Spike")]),
        }
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertIn("page=2", self.calls[1][0])
        self.assertEqual(M.find_position(res["offers"], seller="Spike")["pos"], 2)

    def test_without_a_next_link_it_falls_back_to_page_numbers(self):
        self.pages = {1: body([offer(1, "A")]),
                      2: body([offer(2, "Spike")])}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertEqual(self.calls[1][1].get("page"), 2)
        self.assertEqual(self.calls[1][1].get("keyword"), "3.000.000",
                         "the filters must survive onto page 2")

    def test_a_repeated_page_does_not_double_the_positions(self):
        same = body([offer(1, "A"), offer(2, "B")])
        self.pages = {n: same for n in range(1, 6)}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertEqual(len(res["offers"]), 2)

    def test_the_walk_is_bounded(self):
        self.pages = {n: body([offer(n, f"Ш{n}")]) for n in range(1, 60)}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike", max_pages=5)
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 5)
        self.assertIsNone(M.find_position(res["offers"], seller="Spike"))

    def test_a_failure_midway_keeps_the_pages_already_read(self):
        import requests
        self.pages = {1: body([offer(1, "A")])}
        first = requests.get

        def flaky(url, params=None, **kw):
            # Page one is what the query search asks for; the break comes after
            if int((params or {}).get("page", 1) or 1) > 1 or "page=2" in url:
                raise OSError("сеть отвалилась")
            return first(url, params=params, **kw)

        requests.get = flaky
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok, "one good page is still an answer")
        self.assertEqual(len(res["offers"]), 1)

    def test_a_dead_api_is_reported_not_swallowed(self):
        import requests
        requests.get = lambda *a, **kw: (_ for _ in ()).throw(OSError("нет сети"))
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertFalse(ok)
        self.assertIn("API витрины", res)

    def test_an_unexpected_shape_still_yields_the_offers(self):
        """If `data` ever stops being the key, the generic search takes over."""
        self.category = None
        self.pages = {1: {"result": {"items": [offer(i, f"Ш{i}")
                                               for i in range(4)]}}}
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, res)
        self.assertEqual(len(res["offers"]), 4)


class TheRightCatalogue(unittest.TestCase):
    """Choosing the query that means «вирты в Black Russia», not «вирты».

    This is the failure that looked like success: category=virty answers 200
    with hundreds of offers — virtual currency across every game — and the
    position taken out of that list is a number from a different catalogue.
    Our own listing was nowhere in the first three hundred of it.
    """

    SECTION = {"id": 77, "slug": "black-russia", "ads_count": 638,
               "children": [{"id": 512, "slug": "virty", "ads_count": 638}]}

    def setUp(self):
        import requests
        self._old = requests.get
        self.asked: list[dict] = []
        # What each query shape returns: the wrong ones answer perfectly well,
        # they just answer about something else.
        self.totals = {"category=virty": 4021,
                       "category=black-russia": 638,
                       "category_id=512": 638}

        def fake(url, params=None, **kw):
            if "/api/categories" in url:
                return Reply(self.SECTION)
            p = dict(params or {})
            self.asked.append(p)
            key = next((f"{k}={v}" for k, v in p.items()
                        if k in ("category", "category_id")), "")
            total = self.totals.get(key, 4021)
            return Reply({"data": [offer(1, "Кто-то")], "meta": {"total": total}})

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get = self._old

    def test_the_section_id_is_taken_from_the_catalogue(self):
        """/api/categories answers about the game; the section is inside it."""
        meta = M.category_meta(["black-russia", "virty"])
        self.assertEqual(meta["id"], 512, "took the game, not the section")
        self.assertEqual(meta["ads_count"], 638)

    def test_the_game_is_not_offered_up_as_the_section(self):
        """/api/categories/<game>/<section> answers with the game, section
        ignored — this API really does that.

        Handing that root back as "the section" let the game's own ads_count
        confirm a query for the whole game: 638 offers instead of 161, and a
        position counted in a list our listing is not really in.
        """
        import requests
        old = requests.get
        requests.get = lambda url, **kw: Reply(
            {"id": 77, "slug": "black-russia", "ads_count": 638})
        try:
            got = M.category_meta(["black-russia", "akkaunty-s-virtami"])
            root = M.category_meta(["black-russia"])
        finally:
            requests.get = old
        self.assertEqual(got, {}, "the game was passed off as the section")
        self.assertEqual(root.get("id"), 77,
                         "an address naming only the game still resolves")

    def test_the_query_matching_the_sections_own_count_is_chosen(self):
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, res)
        chosen = self.asked[-1]
        self.assertEqual(chosen.get("category_id"), 512)
        self.assertIn("запрос:", res["note"])

    def test_a_query_returning_another_catalogue_is_rejected(self):
        """Even though it answers 200 with a full page of offers."""
        M.fetch_offers_api(PAGE_URL)
        wrong = [p for p in self.asked if p.get("category") == "virty"]
        chosen = self.asked[-1]
        self.assertNotEqual(chosen.get("category"), "virty",
                            f"settled for the mixed listing: {wrong}")

    def test_when_nothing_matches_it_says_so_instead_of_pretending(self):
        self.totals = {}                       # every shape answers 4021
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, "an answer is still returned")
        self.assertIn("⚠️", res["note"])
        self.assertIn("638", res["note"])

    def test_without_a_count_to_check_against_it_does_not_thrash(self):
        self.SECTION = {"slug": "virty"}       # no ads_count
        ok, res = M.fetch_offers_api(PAGE_URL, max_pages=1)
        self.assertTrue(ok)
        self.assertEqual(len(self.asked), 1,
                         "nothing to verify against — trying every shape would "
                         "be requests spent on a question that cannot be "
                         "answered")


class FindingOurOwn(unittest.TestCase):
    """Locating our listing on the storefront without being handed its page.

    /api/products/{id} is not something this marketplace answers, so the id
    alone gets us nowhere; the search does work, and the row it returns carries
    the category the address is built from.
    """

    def setUp(self):
        import requests
        self._old = requests.get
        self.asked: list[dict] = []
        self.pages = {1: [{"id": 1, "title": "Чужое"}],
                      2: [{"id": 196439, "title": "🏮АВТОВЫДАЧА🏮 100 ЗВЁЗД",
                           "category": {"slug": "zvezdy",
                                        "parent": {"slug": "telegram"}}}]}

        def fake(url, params=None, **kw):
            if f"/api/products/" in url and not url.endswith("/products"):
                return Reply({}, 404)          # no card endpoint here
            p = dict(params or {})
            self.asked.append(p)
            return Reply({"data": self.pages.get(int(p.get("page", 1)), [])})

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get = self._old

    def test_a_decorated_title_still_yields_something_searchable(self):
        self.assertIn("АВТОВЫДАЧА",
                      M.search_keys("🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫"))
        self.assertEqual(M.search_keys("⭐⭐⭐"), [])

    def test_the_number_in_the_title_is_tried_first(self):
        """Названия здесь устроены как «50 звезд», «100 звезд», «500 звезд»:
        различает их число. Отсев «короче трёх букв» его выбрасывал, у товара
        «50 звезд» оставалось одно слово «звезд», и витрина отдавала сорок
        пять чужих строк без нашей."""
        self.assertEqual(M.search_key("50 звезд"), "50")
        self.assertIn("звезд", M.search_keys("50 звезд"))

    def test_the_whole_title_is_the_last_resort(self):
        """Отдельные слова бывают общими до бесполезности."""
        self.assertEqual(M.search_keys("50 звезд")[-1], "50 звезд")

    def test_words_are_searched_one_at_a_time_not_as_a_phrase(self):
        """«💖Аккаунт 💖Баланс: 4.000.000 ₽» has «Аккаунт» and «4.000.000»
        apart, so a phrase built from them matches nothing at all."""
        keys = M.search_keys("💖Аккаунт 💖Баланс: 4.000.000 ₽⚡Уровень 3-")
        self.assertEqual(keys[0], "4.000.000", "longest word goes first")
        self.assertIn("Аккаунт", keys)
        self.assertTrue(all(" " not in k for k in keys[:3]),
                        f"the first tries must be single words: {keys}")

    def test_every_key_is_tried_before_giving_up(self):
        self.pages = {1: [{"id": 5, "title": "Чужое"}]}
        M.find_own_listing(196439, "💖Аккаунт 💖Баланс: 4.000.000 ₽")
        used = [p.get("keyword") for p in self.asked]
        self.assertIn("4.000.000", used)
        self.assertIn("Аккаунт", used)

    def test_our_row_is_found_past_the_first_page_of_results(self):
        row = M.find_own_listing(196439, "🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫")
        self.assertEqual(str(row.get("id")), "196439")
        self.assertGreater(len(self.asked), 1, "gave up after one page")

    def test_the_search_uses_the_words_out_of_the_title(self):
        M.find_own_listing(196439, "🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫")
        # Число — первым: оно и различает «100 ЗВЁЗД» от «500 ЗВЁЗД».
        # Дальше слов не понадобилось — строка нашлась сразу; что при
        # промахе перебираются все, проверяет соседний тест.
        self.assertEqual(self.asked[0].get("keyword"), "100")

    def test_the_address_is_built_from_the_row_we_found(self):
        urls = M.listing_urls_for(196439, title="🏮АВТОВЫДАЧА🏮 💫100 ЗВЁЗД💫")
        self.assertIn("https://yoomarket.net/categories/telegram/zvezdy", urls)
        self.assertIn("https://yoomarket.net/categories/zvezdy/telegram", urls)

    def test_the_section_comes_from_the_catalogue_when_the_row_names_only_the_game(self):
        """A row does not have to carry the whole path.

        One really did give «black-russia» and nothing else, which builds an
        address for the entire game — and our listing is hundreds of places
        down a list it does not belong in.
        """
        import requests
        tree = [{"id": 77, "slug": "black-russia", "children": [
            {"id": 512, "slug": "akkaunty-s-virtami"},
            {"id": 513, "slug": "virty"}]}]
        row = {"id": 196439, "title": "Аккаунт", "category_id": 512,
               "category": {"slug": "black-russia"}}
        old = requests.get

        def fake(url, params=None, **kw):
            if url.endswith("/api/categories"):
                return Reply({"data": tree})
            if "/api/products/" in url:
                return Reply({}, 404)
            return Reply({"data": [row]})

        requests.get = fake
        try:
            urls = M.listing_urls_for(196439, title="Аккаунт")
        finally:
            requests.get = old
        self.assertEqual(
            urls[0],
            "https://yoomarket.net/categories/black-russia/akkaunty-s-virtami",
            f"the section was not recovered: {urls}")

    def test_the_catalogue_path_is_found_by_id_at_any_depth(self):
        tree = [{"id": 1, "slug": "games", "children": [
            {"id": 77, "slug": "black-russia",
             "subcategories": [{"id": 512, "slug": "akkaunty-s-virtami"}]}]}]
        self.assertEqual(M._find_by_id(tree, 512),
                         ["games", "black-russia", "akkaunty-s-virtami"])
        self.assertEqual(M._find_by_id(tree, 999), [])

    def test_the_deepest_id_on_the_row_is_the_section(self):
        self.assertEqual(M._category_id_of({"category_id": 512}), 512)
        self.assertEqual(
            M._category_id_of({"category": {"id": 77,
                                            "section": {"id": 512}}}), 512)
        self.assertIsNone(M._category_id_of({"title": "нет категории"}))

    def test_a_listing_that_is_nowhere_is_reported_as_nothing(self):
        self.assertEqual(M.find_own_listing(999999, "Нет такого товара"), {})
        self.assertEqual(M.listing_urls_for(999999, title="Нет такого"), [])

    def test_a_title_with_nothing_searchable_is_not_a_blind_search(self):
        self.assertEqual(M.find_own_listing(1, "⭐⭐⭐"), {})
        self.assertEqual(self.asked, [], "must not query with an empty keyword")


class Completeness(ApiCase):
    """Whether the whole listing was seen changes what "не нашёл" means."""

    def test_running_out_of_pages_is_not_a_complete_read(self):
        self.pages = {n: body([offer(n, f"Ш{n}")]) for n in range(1, 60)}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike", max_pages=4)
        self.assertTrue(ok)
        self.assertFalse(res["complete"])
        self.assertEqual(res["total"], 638)
        self.assertIn("из 638", res["note"])

    def test_reaching_the_end_of_the_listing_is(self):
        self.category = {"id": 7, "slug": "virty", "ads_count": 2}
        self.pages = {1: {"data": [offer(1, "A")], "meta": {"total": 2}},
                      2: {"data": [offer(2, "B")], "meta": {"total": 2}}}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike", max_pages=9)
        self.assertTrue(res["complete"], "the listing ran out — we saw it all")

    def test_an_empty_page_ends_the_listing(self):
        self.category = None
        self.pages = {1: {"data": [offer(1, "A")]}}      # page 2 comes back []
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike", max_pages=9)
        self.assertTrue(res["complete"])

    def test_the_default_depth_covers_a_whole_category(self):
        """A category of ~640 at fifteen a page needs more than twenty pages.

        Twenty stopped at exactly 300 and called a deeper listing "не найден".
        """
        self.assertGreaterEqual(M.API_MAX_PAGES * 15, 640)


class Cheapest(unittest.TestCase):
    def test_a_zero_is_not_the_cheapest_offer(self):
        """One priceless row among three hundred read as «дешевле всех: 0 ₽».

        That number made the undercut guard meaningless — every real price is
        more than nothing, so the guard would block every promotion.
        """
        rows = [{"price": 0.0}, {"price": 239.0}, {"price": 1449.0}]
        self.assertEqual(M.cheapest(rows), 239.0)

    def test_all_zero_is_no_price_at_all(self):
        self.assertIsNone(M.cheapest([{"price": 0.0}, {"price": None}]))


class TheSectionIdWins(ApiCase):
    """When the catalogue named the section, its id is what gets asked for.

    This API answers /api/categories/<game>/<section> with the *game* — so a
    section inferred from an address can quietly widen to the whole game. That
    is what happened on a real shop: 675 offers read out of a section holding
    161, and our listing nowhere in them.
    """

    def test_a_known_section_id_is_used_as_is(self):
        self.pages = {1: body([offer(1, "A")])}
        ok, res = M.fetch_offers_api(PAGE_URL, category_id=512)
        self.assertTrue(ok, res)
        self.assertEqual(self.calls[0][1].get("category_id"), 512)

    def test_a_known_id_needs_no_catalogue_lookup_at_all(self):
        self.pages = {1: body([offer(1, "A")])}
        M.fetch_offers_api(PAGE_URL, max_pages=1, category_id=512)
        self.assertEqual(self.meta_calls, [],
                         "the section is already known — nothing to look up")
        self.assertEqual(len(self.calls), 1,
                         "and no query shapes to try — one request, one answer")

    def test_the_pages_own_filters_still_travel_with_it(self):
        self.pages = {1: body([offer(1, "A")])}
        M.fetch_offers_api(PAGE_URL, category_id=512)
        self.assertEqual(self.calls[0][1].get("keyword"), "3.000.000")

    def test_fetch_listing_passes_it_through(self):
        self.pages = {1: body([offer(1, "A"), offer(2, "Spike")])}
        ok, _res = M.fetch_listing(PAGE_URL, "Spike", 512)
        self.assertTrue(ok)
        self.assertEqual(self.calls[0][1].get("category_id"), 512)


class Preference(ApiCase):
    def test_fetch_listing_uses_the_api_and_never_touches_the_page(self):
        self.pages = {1: body([offer(1, "A"), offer(2, "Spike")])}
        ok, res = M.fetch_listing(PAGE_URL, "Spike")
        self.assertTrue(ok)
        self.assertIn("api", res["note"])
        self.assertTrue(all("api.yoo.market" in u for u, _ in self.calls),
                        f"the shell page was fetched needlessly: {self.calls}")

    def test_when_the_api_fails_the_page_is_tried_and_both_reasons_survive(self):
        import requests
        requests.get = lambda *a, **kw: (_ for _ in ()).throw(OSError("нет"))
        ok, res = M.fetch_listing(PAGE_URL, "Spike")
        self.assertFalse(ok)
        self.assertIn("API витрины", res)
        self.assertIn("страница:", res)


if __name__ == "__main__":
    unittest.main()
