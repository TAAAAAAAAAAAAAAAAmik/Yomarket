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
SIGNED_IN = '<a href="/logout">Log out</a>'          # признак вошедшего
PAGE = ('<html>' + SIGNED_IN + '<script>var x = "/api?hash=' + REAL_HASH
        + '";</script></html>')


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
        self.bodies: list = []
        self.gets: list = []

    def session(self):
        outer = self

        class S:
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None)})()

            def get(self, url, **kw):
                outer.gets.append(url)
                return Reply(None, outer.page_code, outer.page)

            def post(self, url, params=None, data=None, **kw):
                # Как настоящий Fragment: хеш он берёт из строки запроса, а
                # метод и аргументы — только из тела. Аргумент, уехавший в
                # строку запроса, для него не существует.
                body = dict(data or {})
                outer.posts.append({**dict(params or {}), **body})
                outer.bodies.append(body)
                if (params or {}).get("hash") != outer.good_hash:
                    return Reply({"ok": False, "error": "Bad request"})
                if body.get("method") == "searchStarsRecipient":
                    if not body.get("query"):
                        return Reply({"ok": False, "error":
                                      "Please enter a username assigned "
                                      "to a user."})
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
        self.fake.page = "<html>" + SIGNED_IN + "ничего</html>"
        self.assertEqual(F.fetch_api_hash_sync(COOKIES), "")

    def test_other_shapes_the_page_might_use(self):
        for page, expect in (
                (SIGNED_IN + '{"apiHash":"aabbccddeeff0011"}',
                 "aabbccddeeff0011"),
                (SIGNED_IN + "hash: 'ffeeddccbbaa9988'",
                 "ffeeddccbbaa9988")):
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
        self.fake.page = "<html>" + SIGNED_IN + "no hash here</html>"
        ok, res = F.check_fragment_session_sync(COOKIES, "whatever")
        self.assertFalse(ok)
        self.assertIn("Bad request", str(res))
        self.assertIn("how", res, "must say what to do about it")


class HowTheRequestIsSent(Case):
    """Хеш — в строке запроса, аргументы — в теле.

    Пока всё уходило в строку запроса, Fragment видел хеш и не видел `query`:
    на живой ник он отвечал «Please enter a username assigned to a user», и
    покупка умирала на первом же шаге — поиске получателя.
    """

    def test_arguments_go_into_the_body(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        first = self.fake.bodies[0]
        self.assertEqual(first.get("query"), "durov", first)
        self.assertEqual(first.get("method"), "searchStarsRecipient", first)

    def test_the_hash_stays_in_the_query_string(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        self.assertNotIn("hash", self.fake.bodies[0], self.fake.bodies[0])

    def test_the_recipient_search_carries_the_quantity(self):
        """Форма Fragment шлёт его вместе с ником."""
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        self.assertEqual(str(self.fake.bodies[0].get("quantity")), "100")

    def test_the_check_reaches_fragment_the_same_way(self):
        ok, res = F.check_fragment_session_sync(COOKIES, REAL_HASH)
        self.assertTrue(ok, res)
        self.assertEqual(self.fake.bodies[0].get("query"), "durov",
                         self.fake.bodies[0])


class AnAnswerOnTheMerits(Case):
    """Ответ по существу — не повод показывать продавцу «⚠️»."""

    def _picky(self):
        outer = self.fake

        class Picky(type(outer)):
            pass

        # Fragment принял запрос, но пробный ник ему не понравился.
        def post(url, params=None, data=None, **kw):
            body = dict(data or {})
            outer.posts.append(body)
            outer.bodies.append(body)
            if (params or {}).get("hash") != outer.good_hash:
                return Reply({"ok": False, "error": "Bad request"})
            return Reply({"ok": False, "error":
                          "Please enter a username assigned to a user."})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies: sess

    def test_a_live_session_is_not_called_broken(self):
        self._picky()
        ok, res = F.check_fragment_session_sync(COOKIES, REAL_HASH)
        self.assertTrue(ok, res)
        self.assertIn("работает", str(res))

    def test_but_a_guest_page_still_fails_the_check(self):
        self._picky()
        self.fake.page = "<html>Log in / Connect TON</html>"
        ok, res = F.check_fragment_session_sync(COOKIES, "stale")
        self.assertFalse(ok, res)
        self.assertIn("куки", str(res).lower())


class WhenItCannotBeFound(Case):
    """The seller has a phone and no computer. F12 is not an instruction."""

    def _fail(self, page=None):
        page = page if page is not None else "<html>" + SIGNED_IN + "нет хеша</html>"
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


class WhenTheSessionCannotBuy(Case):
    """«Access denied» на покупке — не то же, что мёртвые куки.

    Получателя Fragment нашёл, значит сессия жива. Покупку разрешает не вход
    через Telegram, а привязанный TON-кошелёк, и голое «Access denied»
    продавцу об этом не говорит ничего.
    """

    def test_it_is_explained_rather_than_repeated(self):
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            body = dict(data or {})
            outer.bodies.append(body)
            if body.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies: sess
        ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                                   api_hash=REAL_HASH)
        self.assertFalse(ok)
        self.assertIn("кошел", msg.lower(), msg)
        self.assertIn("Проверить вход", msg)

    def test_other_failures_keep_their_own_wording(self):
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            body = dict(data or {})
            if body.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Quantity too small"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies: sess
        _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                                    api_hash=REAL_HASH)
        self.assertIn("Quantity too small", msg)
        self.assertNotIn("кошел", msg.lower())


class ComparingWallets(unittest.TestCase):
    def test_the_two_ways_of_writing_one_address_match(self):
        eq = "EQAbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj"
        uq = "UQAbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj"
        self.assertTrue(F._same_wallet(eq, uq))

    def test_different_addresses_do_not(self):
        self.assertFalse(F._same_wallet(
            "EQAbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj",
            "EQZzZzZzGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj"))

    def test_a_missing_address_is_never_a_match(self):
        self.assertFalse(F._same_wallet("", "EQAbCd"))
        self.assertFalse(F._same_wallet("EQAbCd", ""))

    def test_the_address_is_read_off_the_page(self):
        addr = "EQAbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIj"

        class Sess:
            @staticmethod
            def get(url, **kw):
                return Reply(None, 200, f'<div data-address="{addr}"></div>')

        old = F._page_session
        F._page_session = lambda cookies: Sess()
        try:
            self.assertEqual(F.wallet_on_page_sync(COOKIES), addr)
        finally:
            F._page_session = old

    def test_a_page_without_a_wallet_yields_nothing(self):
        class Sess:
            @staticmethod
            def get(url, **kw):
                return Reply(None, 200, "<div>Connect TON</div>")

        old = F._page_session
        F._page_session = lambda cookies: Sess()
        try:
            self.assertEqual(F.wallet_on_page_sync(COOKIES), "")
        finally:
            F._page_session = old


class TryingEveryCandidate(Case):
    """В разметке лежит не один «hash», и подойти может не первый."""

    TWO = (SIGNED_IN + '<script>var a = {"hash":"aaaaaaaaaaaaaaaa"};'
           ' var b = "/api?hash=bbbbbbbbbbbbbbbb";</script>')

    def test_the_second_candidate_is_tried_when_the_first_fails(self):
        self.fake.page = self.TWO
        self.fake.good_hash = "bbbbbbbbbbbbbbbb"
        ok, res = F.check_fragment_session_sync(COOKIES, "stale")
        self.assertTrue(ok, res)
        self.assertEqual(res["api_hash"], "bbbbbbbbbbbbbbbb")

    def test_all_candidates_are_collected(self):
        self.fake.page = self.TWO
        got = F.collect_api_hashes_sync(COOKIES)
        self.assertIn("aaaaaaaaaaaaaaaa", got)
        self.assertIn("bbbbbbbbbbbbbbbb", got)

    def test_a_hash_that_was_read_but_refused_is_not_called_unreadable(self):
        """Прежний текст говорил «прочитать не удалось», хотя отчёт рядом
        сообщал «хеш найден» — и уводил от настоящей причины."""
        self.fake.page = self.TWO
        self.fake.good_hash = "\x00nothing-matches"
        ok, res = F.check_fragment_session_sync(COOKIES, "stale")
        self.assertFalse(ok)
        self.assertNotIn("прочитать не удалось", res["message"])
        self.assertIn("не принял ни один", res["message"])

    def test_with_no_candidates_at_all_it_still_says_so(self):
        self.fake.page = "<html>" + SIGNED_IN + "пусто</html>"
        self.fake.good_hash = "\x00nothing-matches"
        ok, res = F.check_fragment_session_sync(COOKIES, "stale")
        self.assertFalse(ok)
        self.assertIn("прочитать не удалось", res["message"])

    def test_the_stored_hash_is_not_retried_as_a_candidate(self):
        self.fake.page = self.TWO
        self.fake.good_hash = "\x00nothing-matches"
        F.check_fragment_session_sync(COOKIES, "aaaaaaaaaaaaaaaa")
        used = [p.get("hash") for p in self.fake.posts]
        self.assertEqual(used.count("aaaaaaaaaaaaaaaa"), 1, used)


class ReadingTheWalletOffThePage(Case):
    ADDR = "EQA24k42CMkz2G0SzJoSVjxkneLkcqY4V-4NvhXEtB_aX13S"

    def test_an_address_ending_in_a_dash_is_still_found(self):
        """У адреса на конце бывает «-» или «_», и \\b там не срабатывает."""
        self.fake.page = f'<div data-address="{self.ADDR}"></div>'
        self.assertEqual(F.wallet_on_page_sync(COOKIES), self.ADDR)

    def test_the_raw_form_is_understood_too(self):
        raw = "0:" + "ab" * 32
        self.fake.page = f'<span>{raw}</span>'
        self.assertEqual(F.wallet_on_page_sync(COOKIES), raw)

    def test_a_page_with_no_wallet_says_nothing_rather_than_guessing(self):
        self.fake.page = "<div>Connect TON</div>"
        self.assertEqual(F.wallet_on_page_sync(COOKIES), "")


class WhatThePageActuallyShows(unittest.TestCase):
    """Признаки страницы — фактами. Прежний детектор считал входом строку
    «ton-auth», хотя это кнопка «Connect TON», то есть признак обратного:
    бот докладывал «вход есть» на гостевой странице."""

    def test_the_connect_button_is_not_mistaken_for_being_signed_in(self):
        guest = '<a class="ton-auth-link">Connect TON</a>'
        self.assertTrue(F._looks_logged_out(guest))

    def test_a_logout_link_is_what_proves_it(self):
        self.assertFalse(F._looks_logged_out('<a href="/logout">Log out</a>'))

    def test_the_signals_are_listed_one_by_one(self):
        got = F.page_signals('<title>Stars</title>'
                             '<a class="ton-auth">Connect TON</a>')
        joined = " | ".join(got)
        self.assertIn("Stars", joined)
        self.assertIn("кнопка «Connect TON»: есть", joined)
        self.assertIn("ссылка выхода: нет", joined)

    def test_an_empty_page_is_not_called_signed_in(self):
        self.assertTrue(F._looks_logged_out(""))

if __name__ == "__main__":
    unittest.main()
