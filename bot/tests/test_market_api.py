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

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import market as M          # noqa: E402

PAGE_URL = ("https://yoomarket.net/categories/black-russia/virty"
            "?keyword=3.000.000")


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
        self.calls: list[tuple[str, dict]] = []
        self.pages: dict[int, dict] = {}

        def fake(url, params=None, **kw):
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
        self.pages = {1: {"data": [offer(1, "GigShop")]}}
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, res)
        url, params = self.calls[0]
        self.assertEqual(url, "https://api.yoo.market/api/products")
        self.assertEqual(params, {"category": "virty", "keyword": "3.000.000"})

    def test_the_position_runs_across_pages(self):
        self.pages = {
            1: {"data": [offer(i, f"Продавец{i}") for i in range(15)],
                "links": {"next": "https://api.yoo.market/api/products?page=2"},
                "meta": {"total": 638}},
            2: {"data": [offer(20 + i, f"Другой{i}") for i in range(14)]
                        + [offer(99, "Spike", 239.99)],
                "meta": {"total": 638}},
        }
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok, res)
        spike = M.find_position(res["offers"], seller="Spike")
        self.assertEqual(spike["pos"], 30, "position restarted on page 2")
        self.assertEqual(spike["price"], 239.99)
        self.assertIn("из 638", res["note"])

    def test_it_stops_the_moment_we_are_found(self):
        self.pages = {1: {"data": [offer(1, "A"), offer(2, "Spike")]},
                      2: {"data": [offer(3, "B")]}}
        M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertEqual(len(self.calls), 1,
                         "found on page 1 — the rest is wasted money and time")

    def test_the_next_link_is_followed_when_given(self):
        self.pages = {
            1: {"data": [offer(1, "A")],
                "links": {"next": "https://api.yoo.market/api/products"
                                  "?category=virty&page=2"}},
            2: {"data": [offer(2, "Spike")]},
        }
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertIn("page=2", self.calls[1][0])
        self.assertEqual(M.find_position(res["offers"], seller="Spike")["pos"], 2)

    def test_without_a_next_link_it_falls_back_to_page_numbers(self):
        self.pages = {1: {"data": [offer(1, "A")]},
                      2: {"data": [offer(2, "Spike")]}}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertEqual(self.calls[1][1].get("page"), 2)
        self.assertEqual(self.calls[1][1].get("category"), "virty",
                         "the filters must survive onto page 2")

    def test_a_repeated_page_does_not_double_the_positions(self):
        same = {"data": [offer(1, "A"), offer(2, "B")]}
        self.pages = {n: same for n in range(1, 6)}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike")
        self.assertTrue(ok)
        self.assertEqual(len(res["offers"]), 2)

    def test_the_walk_is_bounded(self):
        self.pages = {n: {"data": [offer(n, f"Ш{n}")]} for n in range(1, 60)}
        ok, res = M.fetch_offers_api(PAGE_URL, shop="Spike", max_pages=5)
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 5)
        self.assertIsNone(M.find_position(res["offers"], seller="Spike"))

    def test_a_failure_midway_keeps_the_pages_already_read(self):
        import requests
        self.pages = {1: {"data": [offer(1, "A")]}}
        first = requests.get

        def flaky(url, params=None, **kw):
            if (params or {}).get("page"):
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
        self.pages = {1: {"result": {"items": [offer(i, f"Ш{i}")
                                               for i in range(4)]}}}
        ok, res = M.fetch_offers_api(PAGE_URL)
        self.assertTrue(ok, res)
        self.assertEqual(len(res["offers"]), 4)


class Preference(ApiCase):
    def test_fetch_listing_uses_the_api_and_never_touches_the_page(self):
        self.pages = {1: {"data": [offer(1, "A"), offer(2, "Spike")]}}
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
