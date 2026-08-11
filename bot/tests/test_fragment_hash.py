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
        self.queries: list = []
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
                # Как в документации: Fragment читает всё из строки запроса.
                # Тело здесь остаётся пустым, и если что-то уехало туда —
                # для него этого аргумента не существует.
                query = dict(params or {})
                body = dict(data or {})
                outer.posts.append({**query, **body})
                outer.queries.append(query)
                if body:
                    outer.bodies.append(body)
                if query.get("hash") != outer.good_hash:
                    return Reply({"ok": False, "error": "Bad request"})
                if query.get("method") == "searchStarsRecipient":
                    if not query.get("query"):
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
        F._make_session = lambda cookies, proxy='': self.fake.session()
        F._page_session = lambda cookies, proxy='': self.fake.session()

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
    """Всё уходит в строку запроса — метод, хеш и аргументы.

    Так в документации клиента, который доводит покупку до конца. У нас было
    по-своему: метод и аргументы в теле. Поиск получателя при этом проходил,
    а покупка отвечала «Access denied» — и выдача стояла на этом днями.
    """

    def test_everything_goes_into_the_query_string(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        first = self.fake.queries[0]
        self.assertEqual(first.get("method"), "searchStarsRecipient", first)
        # Первым — «@ник»: так перечисляет написания документация.
        self.assertEqual(first.get("query"), "@durov", first)
        self.assertEqual(first.get("hash"), REAL_HASH, first)

    def test_nothing_is_sent_in_the_body(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        self.assertEqual(self.fake.bodies, [], self.fake.bodies)

    def test_the_search_carries_only_the_username(self):
        """Документ шлёт один `query`. Наш `quantity` был домыслом по форме
        на сайте."""
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        self.assertNotIn("quantity", self.fake.queries[0], self.fake.queries[0])


class TheRecipientIsTakenOnlyFromItsOwnField(unittest.TestCase):
    """`myself` — настоящее поле ответа, и оно булево.

    Рядом с `recipient` у нас стоял перебор «на всякий случай»: recipient,
    id, myself, value. У своего же аккаунта Fragment отвечает
    `{"myself": true, "recipient": "..."}` — стоило `recipient` не прийти, и
    в заявку ушло бы `recipient=True`. Ответ на такую заявку — «Access
    denied», то есть ровно то, что мы неделю искали в правах и транспорте.
    """

    def test_the_recipient_field_is_used(self):
        self.assertEqual(
            F._extract_recipient({"found": {"recipient": "R1"}}), "R1")

    def test_a_myself_flag_is_never_passed_off_as_a_recipient(self):
        self.assertEqual(F._extract_recipient({"found": {"myself": True}}), "")

    def test_myself_alongside_a_recipient_changes_nothing(self):
        self.assertEqual(
            F._extract_recipient({"found": {"myself": True,
                                            "recipient": "R1"}}), "R1")

    def test_nothing_found_means_nothing_returned(self):
        self.assertEqual(F._extract_recipient({"ok": False}), "")


class EveryWritingOfTheNickIsTried(Case):
    """Документация перебирает «@Username, @username, Username, username».

    Мы слали одно написание — то, что ввёл продавец. На своём аккаунте это
    проходило, а «получатель не найден» у покупателя пришлось бы объяснять
    вслепую.
    """

    def test_the_forms_follow_the_document(self):
        self.assertEqual(F._query_forms("Durov"),
                         ["@Durov", "@durov", "Durov", "durov"])

    def test_a_lowercase_nick_needs_only_two(self):
        self.assertEqual(F._query_forms("durov"), ["@durov", "durov"])

    def test_the_at_sign_is_not_doubled(self):
        self.assertEqual(F._query_forms("@durov"), ["@durov", "durov"])

    def test_an_empty_nick_yields_nothing_to_try(self):
        self.assertEqual(F._query_forms("  "), [])

    def test_the_next_writing_is_tried_when_the_first_finds_no_one(self):
        outer = self.fake
        sess = outer.session()
        real = sess.post

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if (q.get("method") == "searchStarsRecipient"
                    and q.get("query") != "durov"):
                outer.queries.append(q)
                return Reply({"ok": False, "error": "Unknown recipient"})
            return real(url, params=params, data=data, **kw)

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        asked = [q.get("query") for q in outer.queries
                 if q.get("method") == "searchStarsRecipient"]
        self.assertEqual(asked, ["@durov", "durov"], asked)

    def test_a_nick_found_at_once_costs_no_extra_requests(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash=REAL_HASH)
        asked = [q.get("query") for q in self.fake.queries
                 if q.get("method") == "searchStarsRecipient"]
        self.assertEqual(asked, ["@durov"], asked)


class TheSecondTonNodeIsTriedToo(unittest.TestCase):
    """Запасной узел есть в документации, у нас его не было.

    Транзакция к этому моменту уже подписана, но ещё не отправлена: один
    не ответивший узел проваливал заказ на ровном месте.
    """

    def setUp(self):
        self.urls: list[str] = []
        self._old = F.requests.post

    def tearDown(self):
        F.requests.post = self._old

    def _answer(self, results):
        def post(url, **kw):
            self.urls.append(url)
            return Reply(results[len(self.urls) - 1])

        F.requests.post = post

    def test_the_first_node_is_enough_when_it_answers(self):
        self._answer([{"ok": True}])
        self.assertTrue(F._send_boc("BOC"))
        self.assertEqual(self.urls, [F.TONCENTER_SEND])

    def test_the_second_is_tried_when_the_first_refuses(self):
        self._answer([{"ok": False}, {"ok": True}])
        self.assertTrue(F._send_boc("BOC"))
        self.assertEqual(self.urls,
                         [F.TONCENTER_SEND, F.TONCENTER_SEND_FALLBACK])

    def test_both_refusing_is_still_a_refusal(self):
        self._answer([{"ok": False}, {"ok": False}])
        self.assertFalse(F._send_boc("BOC"))


class AnswerOfConfirmReqIsNotThrownAway(Case):
    """Именно `confirmReq` засчитывает оплату.

    Его ответ выбрасывался: TON уходил, звёзды не начислялись, а бот
    рапортовал «✅ отправлены». Худшая из возможных поломок этого проекта —
    бодрый отчёт об успехе там, где покупатель остался без товара и без
    денег продавца.
    """

    def _refuse_confirm(self, error="Wrong boc"):
        outer = self.fake
        sess = outer.session()
        real = sess.post

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "initBuyStarsRequest":
                return Reply({"ok": True, "req_id": "REQ-1"})
            if q.get("method") == "getBuyStarsLink":
                return Reply({"transaction": {"messages": [
                    {"address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                     "amount": "1500000000", "payload": ""}]}})
            if q.get("method") == "confirmReq":
                return Reply({"ok": False, "error": error})
            return real(url, params=params, data=data, **kw)

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess

    def _buy(self):
        report: dict = {}
        saved = {name: getattr(F, name) for name in
                 ("_build_signed_boc", "_send_boc", "_get_seqno",
                  "_wait_seqno_advance", "_wallet_from_mnemonic")}
        # tonsdk в окружении тестов нет, да и подписывать здесь нечего:
        # проверяется, что делает бот с ответом Fragment, а не арифметика TON.
        addr = type("A", (), {"to_string": lambda self, *a: "EQ" + "A" * 46})()
        F._wallet_from_mnemonic = lambda m, v: type("W", (), {"address": addr})()
        F._build_signed_boc = lambda *a, **k: "BOC"
        F._send_boc = lambda boc: True
        F._get_seqno = lambda a: 5
        F._wait_seqno_advance = lambda a, n: True
        try:
            ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                       100, api_hash=REAL_HASH, report=report)
        finally:
            for name, value in saved.items():
                setattr(F, name, value)
        return ok, msg, report

    def test_a_refused_confirmation_is_not_reported_as_delivered(self):
        self._refuse_confirm()
        ok, msg, _r = self._buy()
        self.assertFalse(ok)
        self.assertNotIn("✅", msg)

    def test_the_seller_is_told_the_money_left(self):
        self._refuse_confirm()
        _ok, msg, _r = self._buy()
        self.assertIn("TON ушёл", msg)

    def test_the_reason_from_fragment_is_passed_on(self):
        self._refuse_confirm("Payment not found")
        _ok, msg, _r = self._buy()
        self.assertIn("Payment not found", msg)

    def test_the_payment_is_recorded_so_no_one_retries_it(self):
        """Повтор купил бы звёзды второй раз за деньги продавца."""
        self._refuse_confirm()
        _ok, _msg, report = self._buy()
        self.assertTrue(report.get("sent_onchain"))
        self.assertIn("confirm_error", report)

    def test_a_purchase_that_goes_through_still_says_so(self):
        _ok, _msg, report = None, None, None
        F._make_session = lambda cookies, proxy='': self.fake.session()
        outer = self.fake
        sess = outer.session()
        real = sess.post

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "initBuyStarsRequest":
                return Reply({"ok": True, "req_id": "REQ-1"})
            if q.get("method") == "getBuyStarsLink":
                return Reply({"transaction": {"messages": [
                    {"address": "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                     "amount": "1500000000", "payload": ""}]}})
            if q.get("method") == "confirmReq":
                return Reply({"ok": True})
            return real(url, params=params, data=data, **kw)

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        ok, msg, report = self._buy()
        self.assertTrue(ok, msg)
        self.assertIn("✅", msg)
        self.assertNotIn("confirm_error", report)


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
        F._make_session = lambda cookies, proxy='': sess

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


class TheHashAlwaysComesFromThePage(Case):
    """Раньше сохранённый хеш брался как есть, а на «Bad request» покупка
    один раз перечитывала страницу и повторяла запрос. Повтор был наш, не
    описанный, и держался на догадке, что отказ означает именно устаревший
    хеш. Хеш Fragment выдаёт сессии — значит его и надо читать каждый раз,
    а сохранённый годится только на случай, если на странице его нет.
    """

    def test_the_page_is_read_even_when_a_hash_is_stored(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash="stale-one")
        self.assertEqual(self.fake.gets,
                         ["https://fragment.com/stars/buy"], self.fake.gets)

    def test_the_page_hash_wins_over_the_stored_one(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash="stale-one")
        used = [p.get("hash") for p in self.fake.posts]
        self.assertEqual(set(used), {REAL_HASH}, used)

    def test_the_stored_one_is_used_when_the_page_has_none(self):
        """Иначе выдача встанет там, где ещё можно попробовать."""
        self.fake.page = "<html>" + SIGNED_IN + "пусто</html>"
        self.fake.good_hash = "stale-one"
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                         api_hash="stale-one")
        used = [p.get("hash") for p in self.fake.posts]
        self.assertEqual(set(used), {"stale-one"}, used)

    def test_the_page_is_read_once_not_per_method(self):
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100)
        self.assertEqual(len(self.fake.gets), 1, self.fake.gets)


class WhenTheSessionCannotBuy(Case):
    """Голое «Access denied» продавцу не говорит ничего.

    И «получателя нашли — значит сессия жива» тоже неправда: вычитанием
    кук проверено, что поиск проходит и вовсе без stel_token. Совет должен
    вести к тому единственному, что в опыте не работало, — к неподключённому
    кошельку, — и при этом не выдавать это за установленный факт: заявка не
    проходила ещё ни разу, сравнить «до и после» не с чем.
    """

    def test_it_is_explained_rather_than_repeated(self):
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            query = dict(params or {})
            outer.queries.append(query)
            if query.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                                   api_hash=REAL_HASH)
        self.assertFalse(ok)
        # Что делать дальше — иначе совет не совет. И это должен быть
        # оставшийся невыполненный пункт, а не уже проверенное.
        self.assertIn("Wallet Verified", msg)
        self.assertIn("my/profile", msg)

    def test_finding_the_recipient_is_not_passed_off_as_proof_of_access(self):
        """Поиск проходит и без stel_token — проверено вычитанием."""
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            query = dict(params or {})
            if query.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                    100, api_hash=REAL_HASH)
        self.assertNotIn("сессию он признаёт", msg)
        self.assertNotIn("сессия жива", msg)

    def test_a_disproved_guess_is_not_left_in_the_advice(self):
        """Кошелёк проверен на живой сессии: кука работала, отказ тот же.
        Совет «переподключите кошелёк» после этого — трата чужого времени."""
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            query = dict(params or {})
            if query.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                    100, api_hash=REAL_HASH)
        self.assertNotIn("Скорее всего", msg)
        self.assertIn("причиной не является", msg)

    def test_the_remaining_lead_is_not_called_the_cause(self):
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            query = dict(params or {})
            if query.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                    100, api_hash=REAL_HASH)
        self.assertIn("невыполненный пункт", msg)
        self.assertNotIn("причина в", msg.lower())

    def test_other_failures_keep_their_own_wording(self):
        outer = self.fake

        def post(url, params=None, data=None, **kw):
            body = dict(data or {})
            if body.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Quantity too small"})

        sess = outer.session()
        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100,
                                    api_hash=REAL_HASH)
        self.assertIn("Quantity too small", msg)
        self.assertNotIn("кошел", msg.lower())


class ComparingWallets(unittest.TestCase):
    """У одного адреса три записи: EQ…, UQ… и сырая 0:hex. Сравнение строк
    объявляло их разными кошельками — и бот уверенно сообщал «Fragment
    примет оплату только со своего», глядя на два написания одного и того
    же адреса."""

    EQ = "EQA24k42CMkz2G0SzJoSVjxkneLkcqY4V-4NvhXEtB_aX13S"
    RAW = "0:36e24e3608c933d86d12cc9a12563c649de2e472a63857ee0dbe15c4b41fda5f"

    def test_the_raw_form_is_the_same_wallet(self):
        self.assertTrue(F._same_wallet(self.EQ, self.RAW))
        self.assertTrue(F._same_wallet(self.RAW, self.EQ))

    def test_bounceable_and_not_are_the_same_wallet(self):
        """EQ… и UQ… различаются одним флагом, кошелёк за ними один."""
        import base64 as _b64
        raw = _b64.urlsafe_b64decode(self.EQ + "=" * (-len(self.EQ) % 4))
        uq = _b64.urlsafe_b64encode(bytes([0x51]) + raw[1:]).decode().rstrip("=")
        self.assertEqual(F.wallet_hash(uq), F.wallet_hash(self.EQ))

    def test_different_addresses_do_not(self):
        other = ("0:0000003608c933d86d12cc9a12563c649de2e472a63857ee0dbe15c4"
                 "b41fda5f")
        self.assertFalse(F._same_wallet(self.EQ, other))

    def test_rubbish_is_not_matched_to_anything(self):
        self.assertEqual(F.wallet_hash("не адрес"), "")
        self.assertFalse(F._same_wallet("не адрес", self.EQ))

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
        F._page_session = lambda cookies, proxy='': Sess()
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
        F._page_session = lambda cookies, proxy='': Sess()
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


class TheSessionProbe(Case):
    """Куки не приняты — а причин три, и две проверяются сами."""

    def _probe(self, cookies):
        import automation.fragment as FR

        class Sess:
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None),
                                     "__iter__": lambda self: iter(())})()

            @staticmethod
            def get(url, **kw):
                return Reply(None, 200, "<html>Connect TON</html>")

        old = FR.requests.Session
        FR.requests.Session = lambda: Sess()
        try:
            return FR.probe_session_sync(cookies)
        finally:
            FR.requests.Session = old

    def test_swapped_values_are_caught_by_their_length(self):
        """stel_ssid длиннее stel_token — почти всегда перепутанные поля."""
        got = "\n".join(self._probe({"stel_token": "x" * 39,
                                     "stel_ssid": "y" * 69,
                                     "stel_ton_token": "z" * 200}))
        self.assertIn("перепутаны местами", got)

    def test_a_normal_set_raises_no_alarm(self):
        got = "\n".join(self._probe({"stel_token": "x" * 120,
                                     "stel_ssid": "y" * 20,
                                     "stel_ton_token": "z" * 400}))
        self.assertNotIn("перепутаны местами", got)
        self.assertNotIn("необычная длина", got)

    def test_an_odd_length_is_flagged_on_its_own(self):
        got = "\n".join(self._probe({"stel_token": "x" * 5,
                                     "stel_ssid": "y" * 20,
                                     "stel_ton_token": "z" * 400}))
        self.assertIn("необычная длина", got)

    def test_a_missing_cookie_is_named(self):
        got = "\n".join(self._probe({"stel_token": "x" * 120}))
        self.assertIn("stel_ssid: нет", got)

    def test_no_secret_ever_reaches_the_report(self):
        secret = "SUPERSECRETVALUE"
        got = "\n".join(self._probe({"stel_token": secret * 8,
                                     "stel_ssid": "y" * 20,
                                     "stel_ton_token": "z" * 400}))
        self.assertNotIn(secret, got)

    def test_every_user_agent_is_tried(self):
        got = "\n".join(self._probe({"stel_token": "x" * 120}))
        for label, _ua in F._USER_AGENTS:
            self.assertIn(label, got)


class TextThatTelegramMustAccept(unittest.TestCase):
    """«Unsupported start tag "ник"» — Telegram отклонил сообщение целиком,
    приняв <ник> из подсказки заHTML-тег. Сообщение о неудаче не дошло
    вообще, а неудача осталась."""

    def test_no_message_carries_a_bare_angle_bracket(self):
        import ast
        import pathlib as _p
        import re as _re

        root = _p.Path(__file__).resolve().parent.parent
        placeholder = _re.compile(r"<(?:ник|номер|url|id|имя|username)[>»]")
        bad = []
        for path in (list((root / "handlers").glob("*.py"))
                     + list((root / "automation").glob("*.py"))
                     + [root / "tasks/manager.py"]):
            tree = ast.parse(path.read_text())
            # Докстроки и комментарии в чат не уходят — их пропускаем, иначе
            # проверка ловит объяснения вместо сообщений.
            docs = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                    first = (node.body or [None])[0]
                    if (isinstance(first, ast.Expr)
                            and isinstance(first.value, ast.Constant)
                            and isinstance(first.value.value, str)):
                        docs.add(id(first.value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in docs
                        and placeholder.search(node.value)):
                    bad.append(f"{path.name}:{node.lineno}")
        self.assertFalse(bad, "угловые скобки в тексте для чата: "
                              + ", ".join(bad))

    def test_the_access_denied_hint_is_safe(self):
        outer = FakeFragment()

        def post(url, params=None, data=None, **kw):
            body = dict(data or {})
            if body.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            return Reply({"ok": False, "error": "Access denied"})

        sess = outer.session()
        sess.post = post
        old = F._make_session
        F._make_session = lambda cookies, proxy='': sess
        try:
            _ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                        100, api_hash=REAL_HASH)
        finally:
            F._make_session = old
        self.assertNotIn("<", msg, msg)

class TheHashComesFromTheBuyPageFirst(unittest.TestCase):
    """Хеш со страницы витрины годился для поиска получателя, а на покупке
    шёл «Access denied». Документация рабочего клиента берёт его со страницы
    покупки — с неё и начинаем."""

    def test_the_buy_page_is_tried_before_the_showcase(self):
        from automation.fragment import _HASH_PAGES
        self.assertEqual(_HASH_PAGES[0], "https://fragment.com/stars/buy")

    def test_the_showcase_is_still_a_fallback(self):
        from automation.fragment import _HASH_PAGES
        self.assertIn("https://fragment.com/stars", _HASH_PAGES)


class TheDocumentIsTheSourceOfTruth(unittest.TestCase):
    """Документация клиента, который доводит покупку до конца. Где наша
    реализация расходилась с ней, расхождение убрано — своих вариантов мы
    здесь не сочиняем."""

    def test_the_hash_page_is_the_buy_page(self):
        self.assertEqual(F._HASH_PAGES[0], "https://fragment.com/stars/buy")

    def test_no_hash_is_hardcoded_any_more(self):
        """Зашитый чужой хеш — первая из пяти причин, по которым выдача
        не работала."""
        self.assertEqual(F.DEFAULT_HASH, "")

    def test_the_session_carries_only_a_user_agent(self):
        sess = F._make_session({"stel_token": "x"})
        self.assertEqual(set(sess.headers) & {"X-Requested-With", "Origin",
                                              "Referer"}, set())
        self.assertIn("User-Agent", sess.headers)

    def test_the_profile_page_is_checked_for_the_wallet(self):
        import inspect
        src = inspect.getsource(F.wallet_on_page_sync)
        self.assertIn("fragment.com/my/profile", src)

    def test_linked_scripts_are_searched_for_the_hash(self):
        """Документация ищет хеш в HTML и в подключённых JS."""
        self.assertEqual(
            F._script_urls('<script src="/js/a.js"></script>'),
            ["https://fragment.com/js/a.js"])

    def test_the_other_parameter_names_are_recognised(self):
        import re
        blob = '"csrf": "0123456789abcdef0123"'
        hits = [m.group(1) for p in F._HASH_PATTERNS
                for m in re.finditer(p, blob)]
        self.assertIn("0123456789abcdef0123", hits)


class TheBuyProbeShowsWhetherWeAreEvenAllowedToBuy(Case):
    """Проба перебирала форму запроса — и молчала о главном.

    Поиск получателя Fragment отдаёт и гостю, покупку — нет. Пока в отчёте
    не написано, признан ли вход и виден ли привязанный кошелёк, «Access
    denied» одинаково объясняется и транспортом, и тем, что этой сессии
    покупать нельзя. Перебирать транспорт при этом можно бесконечно — что
    мы и делали.
    """

    ADDR = "EQA24k42CMkz2G0SzJoSVjxkneLkcqY4V-4NvhXEtB_aX13S"
    COOKIES = {"stel_token": "t" * 120, "stel_ssid": "s" * 20,
               "stel_ton_token": "n" * 400}

    def _probe(self, page=None, deny=True, myself=False,
               deny_others=True, control="stranger", only_at=False):
        """deny — отказ на основном нике, deny_others — на контрольном.

        only_at — Fragment отвечает лишь на написание с «собакой».
        """
        import automation.fragment as FR

        outer = self.fake
        outer.page = page if page is not None else PAGE
        gets: list[str] = []

        class Sess:
            def __init__(self):
                self.headers: dict = {}
                self.cookies = type(
                    "C", (), {"update": staticmethod(lambda *a: None),
                              "__iter__": lambda self: iter(())})()
                self.who = ""

            def get(self, url, **kw):
                gets.append(url)
                return Reply(None, 200, outer.page)

            def post(self, url, params=None, data=None, **kw):
                q = dict(params or {})
                q.update(data or {})
                outer.queries.append(q)
                if q.get("method") == "searchStarsRecipient":
                    asked = q.get("query", "")
                    if only_at and not asked.startswith("@"):
                        return Reply({"ok": False, "error":
                                      "Please enter a username assigned "
                                      "to a user."})
                    self.who = asked.lstrip("@")
                    found = {"recipient": "R" + self.who}
                    if myself and self.who == "durov":
                        found["myself"] = True
                    return Reply({"ok": True, "found": found})
                refuse = deny_others if self.who == control else deny
                if refuse:
                    return Reply({"ok": False, "error": "Access denied"})
                return Reply({"ok": True, "req_id": "REQ-1"})

        old = FR.requests.Session
        FR.requests.Session = Sess
        try:
            return FR.probe_buy_sync(self.COOKIES, "durov", 50,
                                     control=control), gets
        finally:
            FR.requests.Session = old

    def test_it_says_whether_the_session_is_signed_in(self):
        got = "\n".join(self._probe()[0])
        self.assertIn("Вход:", got)
        self.assertIn("признан", got)

    def test_a_guest_page_is_not_reported_as_signed_in(self):
        page = ('<html>Connect TON<script>var x = "/api?hash=' + REAL_HASH
                + '";</script></html>')
        got = "\n".join(self._probe(page=page)[0])
        self.assertIn("отдана как гостю", got)

    def test_expired_cookies_come_with_what_to_do_about_them(self):
        """Куки Fragment живут недолго: за полчаса личный раздел сменился
        гостевой страницей с теми же куками. Пока это не сказано вслух,
        причину ищут в боте."""
        page = ('<html>Connect TON<script>var x = "/api?hash=' + REAL_HASH
                + '";</script></html>')
        got = "\n".join(self._probe(page=page)[0])
        self.assertIn("Снимите их заново", got)

    def test_a_live_session_is_not_told_to_re_take_the_cookies(self):
        got = "\n".join(self._probe()[0])
        self.assertNotIn("Снимите их заново", got)

    def test_it_says_whether_a_wallet_is_linked(self):
        got = "\n".join(self._probe(page=PAGE + self.ADDR)[0])
        self.assertIn("Кошелёк на странице Fragment: …", got)

    def test_a_missing_wallet_is_said_plainly(self):
        got = "\n".join(self._probe()[0])
        self.assertIn("Кошелёк на странице Fragment: не видно", got)

    def test_the_answer_to_the_request_is_printed_in_full(self):
        """«Access denied» бывает не единственным полем ответа, и соседние
        объясняют, чего не хватает."""
        got = "\n".join(self._probe()[0])
        self.assertIn("ответ заявки: HTTP 200", got)
        self.assertIn("Access denied", got)

    def test_the_recipient_we_actually_send_is_shown(self):
        got = "\n".join(self._probe()[0])
        self.assertIn("recipient длиной 6: «Rdurov…»", got)

    def test_the_details_are_printed_once_not_per_variant(self):
        got = "\n".join(self._probe()[0])
        self.assertEqual(got.count("ответ заявки: HTTP"), 1, got)

    def test_the_closed_transport_search_is_not_run_again(self):
        """Шесть форм запроса дали побайтово один ответ, а различие ответов
        по методам показало, что дело и не в правах. Гонять их каждый раз —
        держать в отчёте полтора десятка одинаковых строк и прятать за ними
        то, что ещё живо."""
        lines, _gets = self._probe()
        self.assertFalse([x for x in lines if "тело запроса" in x], lines)
        self.assertTrue(any("как в образце" in x for x in lines), lines)

    def test_a_working_variant_stops_the_search(self):
        lines, _gets = self._probe(deny=False)
        self.assertTrue(any(x.startswith("✅") for x in lines), lines)

    def test_cookie_names_and_lengths_are_shown(self):
        got = "\n".join(self._probe()[0])
        self.assertIn("stel_token (120)", got)

    def test_the_length_of_each_hash_is_shown(self):
        """У рабочего образца хеш ровно 18 знаков. Среди наших шаблонов есть
        «csrf» и «token» — под видом api-hash легко подобрать чужое, и поиск
        такое может стерпеть, а покупка нет. По хвосту этого не увидеть."""
        got = "\n".join(self._probe()[0])
        self.assertIn(f"({len(REAL_HASH)} знаков)", got)

    def test_a_self_purchase_is_named_as_such(self):
        """`myself: True` в ответе поиска — это не деталь: покупка себе и
        покупка чужому могут разрешаться по-разному."""
        got = "\n".join(self._probe(myself=True)[0])
        self.assertIn("myself: это ваш собственный аккаунт", got)


class TheControlRequestTellsTheTwoRefusalsApart(Case):
    """«Access denied» объясняли одинаково две разные причины.

    Сессии вообще нельзя покупать — или нельзя покупать себе. Отличить их
    рассуждением нельзя, а одной заявкой на чужой ник можно. Разница не
    отвлечённая: в работе получатель всегда покупатель, а не продавец, и во
    втором случае выдача исправна, а сломана только проверка.
    """

    _probe = TheBuyProbeShowsWhetherWeAreEvenAllowedToBuy._probe
    COOKIES = TheBuyProbeShowsWhetherWeAreEvenAllowedToBuy.COOKIES

    def test_the_control_nick_is_actually_asked_about(self):
        lines, _gets = self._probe()
        self.assertTrue(any("@stranger" in x for x in lines), lines)
        asked = [q.get("query") for q in self.fake.queries if q.get("query")]
        self.assertIn("@stranger", asked)

    def test_a_control_that_goes_through_clears_the_delivery(self):
        got = "\n".join(self._probe(deny_others=False)[0])
        self.assertIn("ЗАЯВКА ПРИНЯТА", got)
        self.assertIn("не даёт покупать только себе", got)

    def test_a_control_that_is_refused_too_blames_the_session(self):
        got = "\n".join(self._probe()[0])
        self.assertIn("покупать нельзя вообще", got)

    def test_it_is_not_run_when_the_purchase_already_worked(self):
        """Успех — конец разбора; лишняя заявка на чужой ник ни к чему."""
        lines, _gets = self._probe(deny=False)
        self.assertFalse(any("Контрольная заявка" in x for x in lines), lines)

    def test_the_same_nick_is_not_used_as_its_own_control(self):
        lines, _gets = self._probe(control="durov")
        self.assertFalse(any("Контрольная заявка" in x for x in lines), lines)

    def test_the_control_nick_is_tried_in_every_writing(self):
        """Перебор написаний был вписан в покупку, а в пробу — нет, и
        контрольная заявка упёрлась в «Please enter a username assigned to a
        user» на живом нике."""
        lines, _gets = self._probe(only_at=True)
        self.assertTrue(any("покупать нельзя вообще" in x for x in lines),
                        lines)

    def test_a_nick_nobody_can_find_is_not_left_without_advice(self):
        got = "\n".join(self._probe(control="ghost", only_at=True,
                                    deny_others=True)[0])
        self.assertIn("покупать нельзя вообще", got)


class AskingWhatOtherMethodsAnswer(Case):
    """Проверка, которой не хватало с самого начала.

    «Access denied» полторы недели читался как «этой сессии покупать
    нельзя» — и ни разу не был задан вопрос, что Fragment отвечает на имя
    метода, которого не существует. Если то же самое, вывод о правах не
    следует из ответа вообще, и вся линия рассуждений держалась на пустом.
    """

    def _ask(self, answers):
        """answers — что отвечать на каждый метод; ключ «*» на остальные."""
        outer = self.fake
        sess = outer.session()
        seen: list[str] = []

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            method = q.get("method", "")
            seen.append(method)
            if method == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R"}})
            return Reply(answers.get(method, answers.get("*", {"ok": True})))

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        return F._probe_methods(COOKIES, REAL_HASH, "durov", 50), seen

    def test_a_made_up_method_is_asked_about_too(self):
        _lines, seen = self._ask({})
        self.assertIn("thisMethodDoesNotExist", seen)

    def test_every_answer_is_shown_as_it_came(self):
        lines, _seen = self._ask({"initBuyStarsRequest":
                                  {"error": "Access denied"},
                                  "thisMethodDoesNotExist":
                                  {"error": "Unknown method"}})
        text = "\n".join(lines)
        self.assertIn("initBuyStarsRequest: Access denied", text)
        self.assertIn("thisMethodDoesNotExist: Unknown method", text)

    def test_an_accepted_answer_lists_its_fields(self):
        lines, _seen = self._ask({"getBuyStarsLink": {"ok": True,
                                                      "transaction": {}}})
        self.assertTrue(any("принято: ok, transaction" in x for x in lines),
                        lines)

    def test_the_dead_ids_are_never_real_ones(self):
        """Иначе проба подтвердила бы чужую оплату вместо диагностики."""
        import inspect
        src = inspect.getsource(F._probe_methods)
        self.assertIn('"id": "0"', src)

    def test_a_method_that_answers_no_json_is_still_reported(self):
        outer = self.fake
        sess = outer.session()

        def post(url, params=None, data=None, **kw):
            if (params or {}).get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R"}})
            return Reply(None, 403, "<html>nope</html>")

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        lines = F._probe_methods(COOKIES, REAL_HASH, "durov", 50)
        self.assertTrue(any("не JSON" in x for x in lines), lines)


class GoingOutThroughTheSellersOwnAddress(unittest.TestCase):
    """Прокси — проверка версии, что дело в адресе, с которого мы ходим.

    Fragment выдаёт сессию браузеру на конкретном адресе, а бот живёт в
    дата-центре. Сходится и то, что куки у бота «протухают» за полчаса, и
    то, что покупка отказывает с первой секунды при живой странице.

    Строка прокси несёт логин и пароль — те же чужие доступы, что и куки.
    В отчёт уходят только хост и порт.
    """

    PROXY = "http://user:s3cr3t@1.2.3.4:8080"

    def test_a_proxy_is_applied_to_the_api_session(self):
        s = F._make_session({"a": "1"}, self.PROXY)
        self.assertEqual(s.proxies.get("https"), self.PROXY)

    def test_a_proxy_is_applied_to_the_page_session_too(self):
        """Иначе страницу с хешем читаем с одного адреса, а покупаем с другого."""
        s = F._page_session({"a": "1"}, self.PROXY)
        self.assertEqual(s.proxies.get("https"), self.PROXY)

    def test_without_a_proxy_nothing_is_set(self):
        self.assertEqual(F._make_session({"a": "1"}).proxies, {})

    def test_the_label_hides_the_password(self):
        label = F.proxy_label(self.PROXY)
        self.assertNotIn("s3cr3t", label)
        self.assertNotIn("user", label)
        self.assertIn("1.2.3.4:8080", label)

    def test_no_proxy_is_said_plainly(self):
        self.assertEqual(F.proxy_label(""), "не задан")

    def test_socks_without_the_package_is_refused_in_russian(self):
        """Иначе продавец увидит «Missing dependencies for SOCKS support»
        внутри запроса — английский код ошибки на экране это отписка."""
        import builtins
        real = builtins.__import__

        def no_socks(name, *a, **kw):
            if name == "socks":
                raise ImportError("no socks")
            return real(name, *a, **kw)

        builtins.__import__ = no_socks
        try:
            said = F.proxy_problem("socks5://u:p@1.2.3.4:1080")
        finally:
            builtins.__import__ = real
        self.assertIn("PySocks", said)
        self.assertIn("http://", said)

    def test_an_http_proxy_is_never_refused_for_that(self):
        self.assertEqual(F.proxy_problem(self.PROXY), "")

    def test_no_proxy_has_no_problem_either(self):
        self.assertEqual(F.proxy_problem(""), "")

    def test_the_purchase_goes_through_it(self):
        seen: list[str] = []
        real = F._make_session
        F._make_session = lambda cookies, proxy="": seen.append(proxy) or real(
            cookies, proxy)
        try:
            F.buy_stars_sync({"c": "1"}, " ".join(["w"] * 24), "durov", 100,
                             proxy=self.PROXY)
        finally:
            F._make_session = real
        self.assertEqual(seen, [self.PROXY], seen)


class TellingWhichAddressWeGoOutFrom(unittest.TestCase):
    """«Прокси задан» ничего не значит: он мог и не примениться.

    Пока в отчёте не напечатан адрес, с которого нас видит интернет,
    проверить версию про дата-центр нечем — и очередной прогон уйдёт в
    спор о том, работал прокси или нет.
    """

    def setUp(self):
        self._old = F.requests.Session

    def tearDown(self):
        F.requests.Session = self._old

    def _answer(self, replies):
        calls = {"n": 0}

        class Sess:
            proxies: dict = {}
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None)})()

            def get(self, url, **kw):
                i = calls["n"]
                calls["n"] += 1
                if isinstance(replies[i], Exception):
                    raise replies[i]
                return Reply(None, 200, replies[i])

        F.requests.Session = Sess

    def test_the_address_is_reported(self):
        self._answer(["203.0.113.7"])
        self.assertEqual(F.outbound_ip(), "203.0.113.7")

    def test_a_dead_service_falls_through_to_the_next(self):
        self._answer([RuntimeError("down"), "203.0.113.7"])
        self.assertEqual(F.outbound_ip(), "203.0.113.7")

    def test_nothing_readable_is_said_plainly_not_guessed(self):
        self._answer([RuntimeError("down"), RuntimeError("down")])
        self.assertEqual(F.outbound_ip(), "адрес не узнать")

    def test_an_html_error_page_is_not_taken_for_an_address(self):
        self._answer(["<html>" + "x" * 500 + "</html>", "203.0.113.7"])
        self.assertEqual(F.outbound_ip(), "203.0.113.7")


class OnlyTheFirstMessageIsPaid(Case):
    """Документация берёт ровно `messages[0]`.

    Мы платили по всем подряд, ожидая seqno между переводами. Это была наша
    предусмотрительность, а не описанное поведение: на лишнем сообщении она
    списала бы деньги продавца второй раз. Молчать о лишних сообщениях тоже
    нельзя — решать, доплачивать ли, не нам.
    """

    def _buy(self, messages):
        outer = self.fake
        sess = outer.session()
        sent: list[tuple] = []

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            if q.get("method") == "initBuyStarsRequest":
                return Reply({"ok": True, "req_id": "REQ-1"})
            if q.get("method") == "getBuyStarsLink":
                return Reply({"transaction": {"messages": messages}})
            return Reply({"ok": True})

        sess.post = post
        saved = {n: getattr(F, n) for n in
                 ("_build_signed_boc", "_send_boc", "_get_seqno",
                  "_wait_seqno_advance", "_wallet_from_mnemonic",
                  "_make_session")}
        addr = type("A", (), {"to_string": lambda self, *a: "EQ" + "A" * 46})()
        F._wallet_from_mnemonic = lambda m, v: type("W", (), {"address": addr})()
        F._make_session = lambda cookies, proxy='': sess
        F._build_signed_boc = lambda w, to, amount, p, s: sent.append(
            (to, amount)) or "BOC"
        F._send_boc = lambda boc: True
        F._get_seqno = lambda a: 5
        F._wait_seqno_advance = lambda a, n: True
        report: dict = {}
        try:
            ok, msg = F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov",
                                       100, api_hash=REAL_HASH, report=report)
        finally:
            for name, value in saved.items():
                setattr(F, name, value)
        return ok, msg, report, sent

    TWO = [{"address": "EQ" + "A" * 46, "amount": "1500000000", "payload": ""},
           {"address": "EQ" + "B" * 46, "amount": "900000000", "payload": ""}]

    def test_a_single_message_is_paid_as_before(self):
        ok, _msg, report, sent = self._buy(self.TWO[:1])
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(report["nano"], 1500000000)

    def test_a_second_message_is_not_paid(self):
        _ok, _msg, _report, sent = self._buy(self.TWO)
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(sent[0][1], 1500000000)

    def test_the_seller_is_told_there_were_more(self):
        _ok, msg, _report, _sent = self._buy(self.TWO)
        self.assertIn("ещё 1", msg)
        self.assertIn("оплачен только первый", msg)

    def test_a_single_message_says_nothing_extra(self):
        _ok, msg, _report, _sent = self._buy(self.TWO[:1])
        self.assertNotIn("оплачен только первый", msg)

    def test_the_spend_counts_only_what_was_paid(self):
        """Иначе «Прибыль» посчитает деньги, которые не уходили."""
        _ok, _msg, report, _sent = self._buy(self.TWO)
        self.assertEqual(report["nano"], 1500000000)


class OneSessionForEverything(Case):
    """Последнее расхождение с документацией — и самое незаметное.

    Там одна `requests.Session()`: ею читается страница покупки, из неё
    берётся хеш, ею же уходят все запросы. У нас страницу читала вторая
    сессия — со своим User-Agent и своей банкой кук. Куки, которые Fragment
    ставит при открытии страницы, оставались в ней, а хеш, выданный ей,
    уходил в запрос от первой. Хеш Fragment выдаёт сессии.
    """

    def test_the_purchase_reads_the_page_with_the_session_that_buys(self):
        """Иначе хеш принадлежит одной сессии, а запрос идёт от другой."""
        outer = self.fake
        sess = outer.session()
        used: list[str] = []
        real_get, real_post = sess.get, sess.post

        def get(url, **kw):
            used.append("get")
            return real_get(url, **kw)

        def post(url, **kw):
            used.append("post")
            return real_post(url, **kw)

        sess.get, sess.post = get, post
        F._make_session = lambda cookies, proxy='': sess
        # Отдельную «страничную» сессию ломаем: если покупка полезет за
        # хешем через неё, тест это увидит.
        F._page_session = lambda cookies, proxy='': None
        F.buy_stars_sync(COOKIES, " ".join(["w"] * 24), "durov", 100)
        self.assertEqual(used[0], "get", used)
        self.assertIn("post", used)

    def test_no_second_session_is_created_for_the_page(self):
        import inspect
        src = inspect.getsource(F.buy_stars_sync)
        self.assertNotIn("_page_session", src)
        self.assertNotIn("fetch_api_hash_sync", src)

    def test_the_page_may_be_read_with_any_session_when_asked(self):
        """Диагностике нужны обе: и своя сессия, и общая."""
        got = F.collect_api_hashes_sync(COOKIES, session=self.fake.session())
        self.assertEqual(got, [REAL_HASH])

    def test_the_probe_runs_the_whole_chain_in_one_session(self):
        outer = self.fake
        sess = outer.session()
        sess.cookies = type("C", (), {
            "update": staticmethod(lambda *a: None),
            "__iter__": lambda self: iter(
                [type("K", (), {"name": "stel_ssid"})(),
                 type("K", (), {"name": "stel_dt"})()])})()
        F._make_session = lambda cookies, proxy='': sess
        got = "\n".join(F._probe_single_session(COOKIES, "durov", 50))
        self.assertIn("куки после неё: stel_dt, stel_ssid", got)
        # Кука, которой у нас не было, — и есть возможное недостающее звено.
        self.assertIn("Fragment поставил сам: stel_dt", got)
        self.assertIn(f"({len(REAL_HASH)} знаков)", got)

    def test_a_page_without_a_hash_stops_there_rather_than_guessing(self):
        self.fake.page = "<html>пусто</html>"
        got = "\n".join(F._probe_single_session(COOKIES, "durov", 50))
        self.assertIn("дальше идти не с чем", got)


class EveryHashIsTriedOnThePurchaseToo(Case):
    """Дыра в прежней пробе: перебор шёл от поиска.

    Хеш, на котором поиск не проходит, отбрасывался вместе с заявкой —
    значит второй кандидат ни разу не пробовался на покупке. У Fragment
    хеш вполне может быть свой на каждый раздел: поиску годится один,
    покупке другой, а «Access denied» тогда означает просто «не тот хеш
    для этого метода». Сайт с этого же аккаунта звёзды выдаёт — значит
    дело именно в нашем запросе.
    """

    GOOD, BUY = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def _try(self, *, buy_hash=None, scripts=None):
        """buy_hash — тот, которым заявка проходит. None — не проходит ни один."""
        page = (SIGNED_IN + '<script src="/js/a.js"></script>'
                + f'<script>var a = "/api?hash={self.GOOD}";</script>')
        scripts = scripts if scripts is not None else {}
        sess = self.fake.session()
        asked: list[str] = []

        def get(url, **kw):
            if url.endswith("/stars/buy"):
                return Reply(None, 200, page)
            return Reply(None, 200, scripts.get(url, ""))

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "searchStarsRecipient":
                if q.get("hash") != self.GOOD:
                    return Reply({"ok": False, "error": "Bad request"})
                return Reply({"ok": True, "found": {"recipient": "R1"}})
            asked.append(q.get("hash", ""))
            if buy_hash and q.get("hash") == buy_hash:
                return Reply({"ok": True, "req_id": "REQ-1"})
            return Reply({"ok": False, "error": "Access denied"})

        sess.get, sess.post = get, post
        F._make_session = lambda cookies, proxy="": sess
        return F._probe_every_hash(COOKIES, "durov", 50), asked

    def test_a_hash_that_fails_the_search_is_still_tried_on_the_purchase(self):
        _lines, asked = self._try(
            scripts={"https://fragment.com/js/a.js":
                     f'{{"hash":"{self.BUY}"}}'})
        self.assertIn(self.BUY, asked, asked)

    def test_a_working_buy_hash_is_named_outright(self):
        lines, _asked = self._try(
            buy_hash=self.BUY,
            scripts={"https://fragment.com/js/a.js":
                     f'{{"hash":"{self.BUY}"}}'})
        self.assertTrue(any("ЗАЯВКА ПРИНЯТА" in x for x in lines), lines)
        self.assertTrue(any(self.BUY[-6:] in x for x in lines), lines)

    def test_scripts_are_searched_even_when_the_page_has_a_hash(self):
        """Раньше скрипты читались только при пустой странице — а на
        странице лежал как раз тот хеш, что для покупки не годится."""
        _lines, asked = self._try(
            scripts={"https://fragment.com/js/a.js":
                     f'{{"hash":"{self.BUY}"}}'})
        self.assertGreater(len(asked), 1, asked)

    def test_the_recipient_is_taken_with_whichever_hash_gives_it(self):
        lines, _asked = self._try()
        self.assertFalse(any("получателя не нашёл" in x for x in lines), lines)

    def test_with_no_recipient_at_all_it_says_so(self):
        sess = self.fake.session()
        sess.get = lambda url, **kw: Reply(
            None, 200, SIGNED_IN + f'<script>var a="/api?hash={self.GOOD}";</script>')
        sess.post = lambda *a, **kw: Reply({"ok": False, "error": "nope"})
        F._make_session = lambda cookies, proxy="": sess
        got = F._probe_every_hash(COOKIES, "durov", 50)
        self.assertIn("получателя не нашёл ни один", got[0])


class ChangingOneFieldOfTheRequestAtATime(Case):
    """Метод существует, на куки не смотрит — остаётся сам запрос.

    Fragment отвечает «Invalid method» на выдуманное имя и «Session
    expired» соседним методам на мёртвой сессии, а `initBuyStarsRequest`
    и там и там говорит «Access denied». Значит проверять надо поля. А
    если ответ одинаков даже на пустой запрос — до полей он не доходит, и
    это тоже ответ, только другой.
    """

    def _shapes(self, answer=None, by_args=None):
        outer = self.fake
        sess = outer.session()
        sent: list[dict] = []

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "searchStarsRecipient":
                return Reply({"ok": True, "found": {"recipient": "R" * 64}})
            sent.append(q)
            if by_args:
                for key, reply in by_args.items():
                    if key in q:
                        return Reply(reply)
            return Reply(answer or {"ok": False, "error": "Access denied"})

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        return F._probe_init_shapes(COOKIES, REAL_HASH, "durov", 50), sent

    def test_an_empty_request_is_among_the_shapes(self):
        _lines, sent = self._shapes()
        bare = [q for q in sent if set(q) == {"method", "hash"}]
        self.assertTrue(bare, sent)

    def test_one_answer_for_everything_is_named_as_such(self):
        lines, _sent = self._shapes()
        self.assertTrue(any("до разбора полей Fragment не доходит" in x
                            for x in lines), lines)

    def test_a_differing_answer_is_not_called_the_same(self):
        lines, _sent = self._shapes(
            by_args={"show_sender": {"ok": False, "error": "Bad request"}})
        self.assertFalse([x for x in lines if "не доходит" in x], lines)

    def test_a_shape_that_works_stops_the_search_and_says_which(self):
        lines, _sent = self._shapes(
            by_args={"show_sender": {"ok": True, "req_id": "R1"}})
        self.assertTrue(any("ПРИНЯТО" in x and "show_sender" in x
                            for x in lines), lines)

    def test_without_a_recipient_there_is_nothing_to_compare(self):
        outer = self.fake
        sess = outer.session()
        sess.post = lambda *a, **kw: Reply({"ok": False, "error": "nope"})
        F._make_session = lambda cookies, proxy='': sess
        got = F._probe_init_shapes(COOKIES, REAL_HASH, "durov", 50)
        self.assertIn("сравнивать не с чем", got[0])


class CheckingOneNickOnDemand(Case):
    """«Ник не находится» и «покупка запрещена» — разные беды.

    По прогонам на собственном аккаунте их не различить: на себе поиск
    проходит всегда. Отдельная кнопка позволяет спросить Fragment про любой
    живой ник за секунду — и увидеть, на каком именно шаге он говорит нет.
    """

    def _check(self, *, found=True, myself=False, init_ok=False,
               nick="durov", search_error="No Telegram users found."):
        outer = self.fake
        sess = outer.session()
        asked: list[str] = []

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "searchStarsRecipient":
                asked.append(q.get("query", ""))
                if not found:
                    return Reply({"ok": False, "error": search_error})
                got = {"recipient": "R" * 64}
                if myself:
                    got["myself"] = True
                return Reply({"ok": True, "found": got})
            if init_ok:
                return Reply({"ok": True, "req_id": "REQ-1"})
            return Reply({"ok": False, "error": "Access denied"})

        sess.post = post
        F._make_session = lambda cookies, proxy='': sess
        return F.probe_recipient_sync(COOKIES, nick, 50, REAL_HASH), asked

    def test_a_found_nick_says_so_and_which_writing_worked(self):
        lines, _asked = self._check()
        self.assertIn("найден", lines[0])
        self.assertIn("@durov", lines[0])

    def test_every_writing_is_tried_before_giving_up(self):
        _lines, asked = self._check(found=False, nick="Durov")
        self.assertEqual(asked, ["@Durov", "@durov", "Durov", "durov"], asked)

    def test_a_missing_nick_carries_fragments_own_words(self):
        got = "\n".join(self._check(found=False)[0])
        self.assertIn("No Telegram users found.", got)

    def test_a_missing_nick_is_not_explained_by_guesswork(self):
        """Fragment одинаково отвечает и на несуществующий ник, и на того,
        кому звёзды слать нельзя. Выбирать за него мы не станем."""
        got = "\n".join(self._check(found=False)[0])
        self.assertIn("Fragment не уточняет", got)

    def test_ones_own_account_is_flagged(self):
        got = "\n".join(self._check(myself=True)[0])
        self.assertIn("ваш собственный аккаунт", got)

    def test_someone_elses_account_is_not(self):
        got = "\n".join(self._check(myself=False)[0])
        self.assertNotIn("собственный аккаунт", got)

    def test_the_request_is_made_and_its_answer_shown(self):
        got = "\n".join(self._check()[0])
        self.assertIn("Access denied", got)

    def test_an_accepted_request_says_no_money_moved(self):
        got = "\n".join(self._check(init_ok=True)[0])
        self.assertIn("Заявка на 50⭐ принята", got)
        self.assertIn("не списано", got)

    def test_without_cookies_it_says_that_and_asks_nothing(self):
        got = F.probe_recipient_sync({}, "durov")
        self.assertIn("Куки Fragment не заданы", got[0])

    def test_an_unreadable_hash_is_blamed_on_the_cookies(self):
        self.fake.page = "<html>пусто</html>"
        got = "\n".join(F.probe_recipient_sync(COOKIES, "durov"))
        self.assertIn("истекли куки", got)


class ReadingHowTheSiteCallsItsOwnApi(Case):
    """Прежний разбор искал имена методов по кавычкам в первых шести
    скриптах — и не нашёл даже `searchStarsRecipient`, который у нас
    работает. Отрицательный результат тогда не значил ничего, а выглядел
    как «метода больше нет»: два прогона ушли на объяснение пустоты.
    """

    def _read(self, page, scripts=None):
        scripts = scripts or {}
        outer = self.fake

        class Sess:
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None)})()

            @staticmethod
            def get(url, **kw):
                if url.endswith("/stars/buy"):
                    return Reply(None, 200, page)
                return Reply(None, 200, scripts.get(url, ""))

        old = F._page_session
        F._page_session = lambda cookies, proxy='': Sess()
        try:
            return F.probe_page_api_sync(COOKIES)
        finally:
            F._page_session = old
        del outer

    def test_a_method_name_is_found_without_quotes_around_it(self):
        """У Fragment имя может стоять в data-атрибуте, а не в кавычках JS."""
        page = '<html data-method=searchStarsRecipient>x</html>'
        got = "\n".join(self._read(page))
        self.assertIn("StarsRecipient", got)

    def test_the_surrounding_code_is_shown_not_just_the_name(self):
        """Нужны имена соседних параметров — по ним видно, чего мы не шлём."""
        page = '<html>ajax("initBuyStars", {mode: "gift", quantity: 50})</html>'
        got = "\n".join(self._read(page))
        self.assertIn("mode", got)

    def test_it_reads_more_than_six_scripts(self):
        urls = [f"https://fragment.com/js/f{i}.js" for i in range(10)]
        page = "".join(f'<script src="/js/f{i}.js"></script>'
                       for i in range(10))
        scripts = {u: "" for u in urls}
        scripts[urls[9]] = 'call("initBuyStarsRequest", p)'
        got = "\n".join(self._read(page, scripts))
        self.assertIn("initBuy", got)

    def test_finding_nothing_now_means_something(self):
        got = "\n".join(self._read("<html>пусто</html>"))
        self.assertIn("Ни одной из искомых строк", got)

    def test_a_script_that_will_not_load_is_named_not_skipped(self):
        class Sess:
            headers: dict = {}
            cookies = type("C", (), {"update": staticmethod(lambda *a: None)})()

            @staticmethod
            def get(url, **kw):
                if url.endswith("/stars/buy"):
                    return Reply(None, 200,
                                 '<script src="/js/dead.js"></script>')
                raise RuntimeError("timeout")

        old = F._page_session
        F._page_session = lambda cookies, proxy='': Sess()
        try:
            got = "\n".join(F.probe_page_api_sync(COOKIES))
        finally:
            F._page_session = old
        self.assertIn("dead.js", got)

    def test_the_report_does_not_run_away_with_the_whole_file(self):
        page = "<html>" + ("initBuy " * 500) + "</html>"
        got = self._read(page)
        self.assertLess(len("\n".join(got)), 3000, got)


class WhatFragmentSaysAboutTheAccount(Case):
    """Документация читает с `/my/profile` пять вещей, мы — три.

    Отметки Identity Verified и Wallet Verified не смотрели ни разу, хотя
    именно они решают, что аккаунту разрешено. Живая сессия, работающие
    куки, подключённый кошелёк — и всё равно «Access denied»: значит
    смотреть надо туда, где Fragment говорит о правах словами.
    """

    PROFILE = ('<html><a href="/logout">out</a> @seller '
               'Identity Verified · Wallet Verified '
               'EQA24k42CMkz2G0SzJoSVjxkneLkcqY4V-4NvhXEtB_aX13S</html>')

    def _facts(self, page):
        self.fake.page = page
        return "\n".join(F.profile_facts_sync(COOKIES))

    def test_the_verification_marks_are_read(self):
        got = self._facts(self.PROFILE)
        self.assertIn("Identity Verified: есть", got)
        self.assertIn("Wallet Verified: есть", got)

    def test_a_missing_mark_is_said_plainly(self):
        got = self._facts('<html><a href="/logout">out</a> @seller</html>')
        self.assertIn("Identity Verified: не встречается", got)

    def test_the_wallet_is_shown_by_its_tail_only(self):
        got = self._facts(self.PROFILE)
        self.assertIn("кошелёк: …_aX13S", got)
        self.assertNotIn("EQA24k42", got)

    def test_a_guest_profile_is_not_mined_for_facts(self):
        """Иначе отчёт полон «не встречается» — и все они ни о чём."""
        got = self._facts("<html>Connect TON</html>")
        self.assertIn("отдана как гостю", got)
        self.assertNotIn("Identity Verified:", got)

    def test_the_page_that_is_read_is_the_one_from_the_document(self):
        self._facts(self.PROFILE)
        self.assertIn("https://fragment.com/my/profile", self.fake.gets)

    def test_a_verified_identity_with_an_unverified_wallet_is_flagged(self):
        """Обе фразы Fragment пишет одинаково: раз одну видно, вторую тоже
        было бы видно. Значит это не шум разметки, а разные состояния."""
        page = ('<html><a href="/logout">out</a> @seller Identity Verified '
                'EQA24k42CMkz2G0SzJoSVjxkneLkcqY4V-4NvhXEtB_aX13S</html>')
        got = self._facts(page)
        self.assertIn("Личность проверена, а кошелёк — нет", got)

    def test_both_verified_raises_nothing(self):
        self.assertNotIn("Личность проверена", self._facts(self.PROFILE))

    def test_neither_verified_raises_nothing_either(self):
        """Тогда непонятно, читаются ли отметки вообще, — и молчание честнее."""
        page = '<html><a href="/logout">out</a> @seller</html>'
        self.assertNotIn("Личность проверена", self._facts(page))

    def test_the_lead_is_not_dressed_up_as_the_answer(self):
        page = ('<html><a href="/logout">out</a> @seller Identity Verified'
                '</html>')
        self.assertIn("фактом это станет только после", self._facts(page))


class TheControlNickIsHuntedDownNotGivenUpOn(Case):
    """@durov Fragment не находит — «Please enter a username assigned to a
    user». Почему, неизвестно; проба из-за этого два прогона подряд не
    доходила до сути. Перебор запасных ников дешевле догадок о причине.
    """

    def _control(self, known=()):
        outer = self.fake
        sess = outer.session()
        asked: list[str] = []

        def post(url, params=None, data=None, **kw):
            q = dict(params or {})
            if q.get("method") == "searchStarsRecipient":
                nick = q.get("query", "").lstrip("@")
                asked.append(nick)
                if nick in known:
                    return Reply({"ok": True, "found": {"recipient": "R"}})
                return Reply({"ok": False, "error":
                              "Please enter a username assigned to a user."})
            return Reply({"ok": False, "error": "Access denied"})

        sess.post = post
        old = F.requests.Session
        F.requests.Session = lambda: sess
        try:
            return F._probe_control(COOKIES, "durov", 50, REAL_HASH,
                                    "NO0RD"), asked
        finally:
            F.requests.Session = old

    def test_a_spare_nick_is_tried_when_the_given_one_is_not_found(self):
        lines, asked = self._control(known={"telegram"})
        self.assertIn("telegram", asked)
        self.assertTrue(any("взял @telegram" in x for x in lines), lines)

    def test_finding_one_gets_the_answer_we_came_for(self):
        lines, _asked = self._control(known={"telegram"})
        self.assertTrue(any("покупать нельзя вообще" in x for x in lines),
                        lines)

    def test_our_own_nick_is_never_used_as_the_control(self):
        """На себе проверять нечего — это и есть основной случай."""
        _lines, asked = self._control(known={"NO0RD"})
        self.assertNotIn("NO0RD", asked)

    def test_when_nobody_is_found_it_says_who_was_tried(self):
        lines, _asked = self._control(known=())
        text = "\n".join(lines)
        self.assertIn("Ни один ник не нашёлся", text)
        self.assertIn("@telegram", text)
        self.assertIn("свой второй", text)


class WhichCookieActuallyDoesAnything(Case):
    """«Connect TON» на странице ничего не доказывает.

    Это либо «кошелёк не подключён», либо «разметка окна лежит там всегда» —
    по HTML не различить, а на похожем признаке (`ton-auth`) мы уже один раз
    ошиблись и объявили гостя вошедшим. Зато различить можно опытом: убрать
    куку и посмотреть, изменилось ли хоть что-нибудь.
    """

    COOKIES = {"stel_token": "t" * 120, "stel_ssid": "s" * 20,
               "stel_ton_token": "n" * 400}

    def _roles(self, *, ton_token_matters):
        import automation.fragment as FR

        outer = self.fake
        seen: list[dict] = []

        def page_session(cookies):
            sess = outer.session()
            has_ton = "stel_ton_token" in (cookies or {})
            body = PAGE + ("<div>My assets</div>" if has_ton
                           or not ton_token_matters else "")
            sess.get = lambda url, **kw: Reply(None, 200, body)
            return sess

        class Sess:
            def __init__(self):
                self.headers: dict = {}
                self.cookies = type("C", (), {
                    "update": staticmethod(lambda *a: seen.append(dict(a[0])))
                })()

            def post(self, url, params=None, data=None, **kw):
                q = dict(params or {})
                if q.get("method") == "searchStarsRecipient":
                    return Reply({"ok": True, "found": {"recipient": "R1"}})
                return Reply({"ok": False, "error": "Access denied"})

        old_page, old_sess = FR._page_session, FR.requests.Session
        FR._page_session = page_session
        FR.requests.Session = Sess
        try:
            return FR._probe_cookie_roles(self.COOKIES, "durov", 50), seen
        finally:
            FR._page_session, FR.requests.Session = old_page, old_sess

    def test_every_cookie_is_left_out_in_turn(self):
        lines, _seen = self._roles(ton_token_matters=True)
        text = "\n".join(lines)
        for name in self.COOKIES:
            self.assertIn(f"без {name}", text)
        self.assertIn("без кук вовсе", text)

    def test_a_cookie_that_changes_nothing_is_named(self):
        lines, _seen = self._roles(ton_token_matters=False)
        self.assertTrue(any("не работает" in x for x in lines), lines)

    def test_a_cookie_that_does_change_things_is_not_accused(self):
        lines, _seen = self._roles(ton_token_matters=True)
        self.assertFalse([x for x in lines if "не работает" in x], lines)

    def test_the_request_really_goes_without_that_cookie(self):
        """Иначе опыт ничего не проверяет: убрали из подписи, а шлём то же."""
        _lines, seen = self._roles(ton_token_matters=True)
        self.assertTrue(any("stel_ton_token" not in s for s in seen), seen)

    def test_no_cookie_value_ever_reaches_the_report(self):
        """Это доступ к чужому аккаунту и кошельку — в отчёт уходит длина."""
        secret = "SUPERSECRETVALUE"
        import automation.fragment as FR

        outer = self.fake
        outer.page = PAGE

        class Sess:
            def __init__(self):
                self.headers: dict = {}
                self.cookies = type(
                    "C", (), {"update": staticmethod(lambda *a: None),
                              "__iter__": lambda self: iter(())})()

            def get(self, url, **kw):
                return Reply(None, 200, outer.page)

            def post(self, url, params=None, data=None, **kw):
                q = dict(params or {})
                if q.get("method") == "searchStarsRecipient":
                    return Reply({"ok": True, "found": {"recipient": "R1"}})
                return Reply({"ok": False, "error": "Access denied"})

        old = FR.requests.Session
        FR.requests.Session = Sess
        try:
            got = "\n".join(FR.probe_buy_sync(
                {"stel_token": secret * 8, "stel_ssid": "s" * 20,
                 "stel_ton_token": "n" * 400}, "durov", 50))
        finally:
            FR.requests.Session = old
        self.assertNotIn(secret, got)


if __name__ == "__main__":
    unittest.main()
