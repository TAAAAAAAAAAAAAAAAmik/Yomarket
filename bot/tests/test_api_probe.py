"""Finding the request the storefront makes to fill its listing.

The category page ships no offers — 81 KB of application shell — so the list
arrives over a separate call, and its address is compiled into the page's own
JavaScript. This is the tooling that finds it; the tests keep it honest,
because a probe that silently reports nothing is worse than no probe.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers.panel_items as PI              # noqa: E402

SHELL = (
    '<!DOCTYPE html><html><head><title>Юмаркет</title></head><body>'
    '<div id="__next"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps({"buildId": "abc123",
                  "props": {"pageProps": {"category": {"id": 42,
                                                       "slug": "virty"}}},
                  "query": {"slug": ["black-russia", "virty"]}},
                 ensure_ascii=False)
    + '</script><script src="/_next/static/chunks/main-1.js"></script>'
    '</body></html>')

BUNDLE = ('const API="https://api.yoo.market";'
          'fetch(API+"/v1/offers?category="+id);const P="/v1/offers";')

LISTING = json.dumps({"data": [
    {"id": 1, "title": "Лот A", "price": 230, "shop": {"name": "GigShop"},
     "rating": 5},
    {"id": 2, "title": "Лот B", "price": 239, "shop": {"name": "GadjiSeller"},
     "rating": 4.97},
    {"id": 3, "title": "Лот C", "price": 239.99, "shop": {"name": "Spike"},
     "rating": 4.84}]}, ensure_ascii=False)

URL = "https://yoomarket.net/categories/black-russia/virty?keyword=3.000.000"


class Reply:
    def __init__(self, text, code=200, ctype="text/html"):
        self.text = text
        self.status_code = code
        self.headers = {"content-type": ctype}

    def json(self):
        return json.loads(self.text)


class ApiProbe(unittest.TestCase):
    def setUp(self):
        import requests
        self._old = requests.get
        self.asked: list[str] = []

        def fake(url, **kw):
            self.asked.append(url)
            if url.endswith(".js"):
                return Reply(BUNDLE, 200, "application/javascript")
            if "/v1/offers" in url:
                return Reply(LISTING, 200, "application/json")
            if "_next/data" in url:
                return Reply('{"pageProps":{}}', 200, "application/json")
            if "/categories/" in url:
                return Reply(SHELL)
            return Reply("not found", 404)

        requests.get = fake

    def tearDown(self):
        import requests
        requests.get = self._old

    def run_(self, shop="Spike"):
        return PI._pos_api_sync(URL, shop)

    def test_it_reads_the_address_apart(self):
        out = self.run_()
        self.assertIn("['black-russia', 'virty']", out)
        self.assertIn("'keyword': '3.000.000'", out)

    def test_it_reports_the_build_id_for_the_data_route(self):
        out = self.run_()
        self.assertIn("buildId: abc123", out)
        self.assertTrue(any("_next/data/abc123" in u for u in self.asked))

    def test_it_pulls_the_endpoint_out_of_the_javascript(self):
        """The whole point: the address is in the bundle, not in the HTML."""
        out = self.run_()
        self.assertIn("https://api.yoo.market", out)
        self.assertIn("/v1/offers", out)

    def test_it_marks_the_response_that_has_our_shop(self):
        out = self.run_()
        hit = [l for l in out.split("\n") if "ЕСТЬ НАШ МАГАЗИН" in l]
        self.assertTrue(hit, f"the listing response was not flagged:\n{out}")
        self.assertIn("офферов: 3", hit[0])
        self.assertIn("api.yoo.market/v1/offers", hit[0])

    def test_the_page_parameters_are_carried_into_the_request(self):
        self.run_()
        offers = [u for u in self.asked if "/v1/offers" in u]
        self.assertTrue(offers)
        self.assertTrue(all("keyword=3.000.000" in u for u in offers),
                        f"the search was dropped: {offers}")

    def test_everything_it_does_is_a_read(self):
        import requests
        posted = []
        old_post = requests.post
        requests.post = lambda *a, **kw: posted.append(a)
        try:
            self.run_()
        finally:
            requests.post = old_post
        self.assertEqual(posted, [], "a diagnostic must never write")

    def test_a_dead_page_reports_instead_of_raising(self):
        import requests

        def boom(url, **kw):
            raise OSError("нет сети")

        requests.get = boom
        self.assertIn("не открылась", PI._pos_api_sync(URL, "Spike"))

    def test_a_shop_that_is_not_there_is_not_claimed(self):
        out = self.run_(shop="Кого-нет")
        self.assertNotIn("ЕСТЬ НАШ МАГАЗИН", out)
        self.assertIn("похоже на выдачу", out)


if __name__ == "__main__":
    unittest.main()
