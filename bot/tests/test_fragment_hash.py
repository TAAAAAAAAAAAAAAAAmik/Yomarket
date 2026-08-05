"""The api hash Fragment stamps on every request.

It is issued per session, and a hash from somebody else's session is answered
with «Bad request» — which is exactly what a hardcoded one produced. The seller
was never asked for it and could not have known, so the code has to find it.
"""
from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import fragment as F          # noqa: E402

logging.getLogger("automation.fragment").setLevel(logging.CRITICAL)

COOKIES = {"stel_token": "a", "stel_ssid": "b", "stel_ton_token": "c"}
REAL_HASH = "0123456789abcdef"
PAGE = ('<html><script>var x = "/api?hash=' + REAL_HASH + '";</script></html>')


class Reply:
    def __init__(self, payload, code=200, text=""):
        self._payload = payload
        self.status_code = code
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeFragment:
    """Answers only requests carrying this session's own hash."""

    def __init__(self, page=PAGE, good_hash=REAL_HASH):
        self.page = page
        self.page_code = 200
        self.good_hash = good_hash
        self.posts: list = []
        self.gets: list = []

    def session(self):
        outer = self

        class S:
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None)})()

            def get(self, url, **kw):
                outer.gets.append(url)
                return Reply(None, outer.page_code, outer.page)

            def post(self, url, params=None, **kw):
                p = dict(params or {})
                outer.posts.append(p)
                if p.get("hash") != outer.good_hash:
                    return Reply({"ok": False, "error": "Bad request"})
                if p.get("method") == "searchStarsRecipient":
                    return Reply({"ok": True, "found": {"recipient": "R1"}})
                return Reply({"ok": True})

        return S()


class Case(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFragment()
        self._old = F._make_session
        # The page is fetched with its own session — without the XHR header, or
        # Fragment answers as XHR and the scripts holding the hash are absent.
        self._old_page = F._page_session
        F._make_session = lambda cookies: self.fake.session()
        F._page_session = lambda cookies: self.fake.session()

    def tearDown(self):
        F._make_session = self._old
        F._page_session = self._old_page


class FindingIt(Case):
    def test_it_is_read_off_fragments_own_page(self):
        self.assertEqual(F.fetch_api_hash_sync(COOKIES), REAL_HASH)

    def test_a_page_without_one_yields_nothing_rather_than_a_guess(self):
        self.fake.page = "<html>ничего</html>"
        self.assertEqual(F.fetch_api_hash_sync(COOKIES), "")

    def test_other_shapes_the_page_might_use(self):
        for page, expect in (
                ('{"apiHash":"aabbccddeeff0011"}', "aabbccddeeff0011"),
                ("hash: 'ffeeddccbbaa9988'", "ffeeddccbbaa9988")):
            self.fake.page = page
            self.assertEqual(F.fetch_api_hash_sync(COOKIES), expect)


class CheckingTheSession(Case):
    def test_a_stale_hash_is_replaced_and_the_check_passes(self):
        """This is the failure the seller saw: «Bad request»."""
        ok, res = F.check_fragment_session_sync(COOKIES, "somebody-elses-hash")
        self.assertTrue(ok, res)
        self.assertIsInstance(res, dict, "the fresh hash must come back")
        self.assertEqual(res["api_hash"], REAL_HASH)

    def test_a_hash_that_already_works_is_left_alone(self):
        ok, res = F.check_fragment_session_sync(COOKIES, REAL_HASH)
        self.assertTrue(ok)
        self.assertIsInstance(res, str, "nothing to save — it already worked")
        self.assertEqual(self.fake.gets, [], "no need to open the page at all")

    def test_dead_cookies_are_not_blamed_on_the_hash(self):
        self.fake.good_hash = "\\x00nothing-matches"
        self.fake.page = "<html>no hash here</html>"
        ok, res = F.check_fragment_session_sync(COOKIES, "whatever")
        self.assertFalse(ok)
        self.assertIn("Bad request", str(res))
        self.assertIn("how", res, "must say what to do about it")


class WhenItCannotBeFound(Case):
    """The seller has a phone and no computer. F12 is not an instruction."""

    def _fail(self, page="<html>no hash here</html>"):
        self.fake.good_hash = "\\x00nothing-matches"
        self.fake.page = page
        ok, res = F.check_fragment_session_sync(COOKIES, "whatever")
        self.assertFalse(ok)
        self.assertIsInstance(res, dict, res)
        return res

    def test_it_never_sends_anyone_to_developer_tools(self):
        res = self._fail()
        said = f"{res.get('message')} {res.get('how')}"
        for word in ("F12", "Network", "devtools"):
            self.assertNotIn(word, said, said)

    def test_it_points_at_the_cookies_instead(self):
        self.assertIn("куки", self._fail()["how"].lower())

    def test_expired_cookies_are_named_as_the_reason(self):
        """A guest page is the usual cause — say so instead of guessing."""
        res = self._fail("<html>Log in to Fragment / Connect TON</html>")
        self.assertIn("гост", res["message"].lower(), res["message"])

    def test_it_shows_what_each_page_actually_answered(self):
        """«Не нашёл» with nothing behind it is a dead end."""
        report = self._fail()["report"]
        self.assertTrue(report, "no evidence collected")
        self.assertTrue(any("fragment.com" in line for line in report), report)
        self.assertTrue(any("200" in line for line in report), report)

    def test_an_unreachable_page_lands_in_the_report_too(self):
        self.fake.page_code = 502
        self.assertTrue(any("502" in line for line in self._fail()["report"]))

    def test_no_cookies_is_said_plainly(self):
        ok, res = F.check_fragment_session_sync({}, "")
        self.assertFalse(ok)
        self.assertIn("cookies", str(res))


class BuyingWithAStaleHash(Case):
    def test_the_purchase_recovers_instead_of_failing(self):
        """A delivery must not die on a hash the seller was never asked for."""
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash="stale-one")
        used = [p.get("hash") for p in self.fake.posts]
        self.assertIn(REAL_HASH, used, f"never retried with a fresh hash: {used}")

    def test_it_refreshes_once_and_not_on_every_call(self):
        self.fake.good_hash = "never-matches"
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash="stale-one")
        self.assertEqual(len(self.fake.gets), 1,
                         "the page must not be re-read for every method")

    def test_a_working_hash_costs_no_extra_requests(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        self.assertEqual(self.fake.gets, [])


if __name__ == "__main__":
    unittest.main()
