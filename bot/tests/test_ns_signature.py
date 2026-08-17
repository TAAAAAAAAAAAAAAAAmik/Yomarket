"""Подпись запросов к ns.gifts — по документации, шаг в шаг.

Здесь нечего «улучшить». Строка запроса собирается без кодирования, тело —
компактный JSON, порядок частей строго заданный, а у `/get_token` слота под
токен нет вовсе. Любое отступление даёт 401, неотличимый от «ключ неверный»,
и искать причину пришлось бы вслепую — ровно так мы потеряли день на
Fragment, где наша форма запроса разошлась с рабочим клиентом.

Отдельно проверяется, что секреты не попадают наружу: `api_secret` — это
доступ к деньгам на балансе поставщика.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import nsgifts as N          # noqa: E402

SECRET = base64.b64encode(b"secret-bytes-for-tests").decode()
CREDS = {"user_id": 1234, "api_secret": SECRET, "login": "log",
         "password": "pass"}


def expected(method, path, query, body, ts, token):
    parts = [method.upper(), path, query, ts]
    if token is not None:
        parts.append(token)
    parts.append(hashlib.sha256(body or b"").hexdigest())
    return base64.b64encode(hmac.new(
        base64.b64decode(SECRET), "\n".join(parts).encode(),
        hashlib.sha256).digest()).decode()


class TheFormulaIsTheDocumentsFormula(unittest.TestCase):
    def test_a_signed_request_matches_the_document(self):
        got = N._sign(SECRET, "GET", "/api/v2/stock", "", b"", "1700000000",
                      "TOK")
        self.assertEqual(got, expected("GET", "/api/v2/stock", "", b"",
                                       "1700000000", "TOK"))

    def test_the_bootstrap_has_no_token_slot_at_all(self):
        """Не пустая строка вместо токена, а отсутствующая часть. Перепутать
        эти два случая — значит сломать самый первый запрос."""
        without = N._sign(SECRET, "POST", "/api/v2/get_token", "", b"{}",
                          "1700000000", None)
        empty = N._sign(SECRET, "POST", "/api/v2/get_token", "", b"{}",
                        "1700000000", "")
        self.assertNotEqual(without, empty)
        self.assertEqual(without, expected("POST", "/api/v2/get_token", "",
                                           b"{}", "1700000000", None))

    def test_the_body_hash_is_of_the_exact_bytes(self):
        a = N._sign(SECRET, "POST", "/p", "", b'{"a":1}', "1", "T")
        b = N._sign(SECRET, "POST", "/p", "", b'{"a": 1}', "1", "T")
        self.assertNotEqual(a, b, "лишний пробел в теле меняет подпись")

    def test_the_method_is_upper_cased(self):
        self.assertEqual(N._sign(SECRET, "get", "/p", "", b"", "1", "T"),
                         N._sign(SECRET, "GET", "/p", "", b"", "1", "T"))


class TheQueryAndBodyAreBuiltAsInTheDocument(unittest.TestCase):
    def test_the_query_is_not_url_encoded(self):
        """Подпись считается по этой же строке. «Улучшение» разойдётся с
        тем, что подписал сервер."""
        self.assertEqual(N._query_of({"a": "b c", "d": 2}), "a=b c&d=2")

    def test_the_order_is_kept_as_given(self):
        self.assertEqual(N._query_of({"b": 1, "a": 2}), "b=1&a=2")

    def test_no_params_means_an_empty_string(self):
        self.assertEqual(N._query_of(None), "")
        self.assertEqual(N._query_of({}), "")

    def test_the_body_is_compact_json(self):
        self.assertEqual(N._body_of({"a": 1, "b": "x"}), b'{"a":1,"b":"x"}')

    def test_no_body_means_no_bytes(self):
        self.assertEqual(N._body_of(None), b"")


class Fake:
    """Сервер поставщика: считает подписи так же, как документация."""

    def __init__(self, answers=None, token="TOK-1"):
        self.answers = answers or {}
        self.token = token
        self.seen: list[dict] = []
        self.tokens_issued = 0

    def request(self, method, url, headers=None, data=None, timeout=None):
        path = url.split("api.ns.gifts", 1)[-1]
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        self.seen.append({"method": method, "path": path, "query": query,
                          "headers": dict(headers or {}), "body": data})
        if path == "/api/v2/get_token":
            self.tokens_issued += 1
            return Reply(200, {"user_id": 1234, "token": self.token,
                               "expires_in": 7200})
        answer = self.answers.get(path, (200, {"ok": True}))
        return Reply(*answer)

    def post(self, url, headers=None, data=None, timeout=None):
        return self.request("POST", url, headers=headers, data=data)


class Reply:
    def __init__(self, code, payload, text=""):
        self.status_code = code
        self._payload = payload
        self.text = text or json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def client(fake, **over):
    c = N.NSClient(**{**CREDS, **over})
    c.session = fake
    return c


class NoSleeping(unittest.TestCase):
    """Свежее время добывается ожиданием секунды — в тестах ждать нечего."""

    def setUp(self):
        self._sleep = N.time.sleep
        N.time.sleep = lambda s: None

    def tearDown(self):
        N.time.sleep = self._sleep


class TheServerWouldAcceptWhatWeSend(NoSleeping):
    def test_the_signature_we_send_is_the_one_the_document_expects(self):
        fake = Fake()
        client(fake).call("GET", "/api/v2/stock")
        sent = fake.seen[-1]
        h = sent["headers"]
        self.assertEqual(
            h["X-Signature"],
            expected("GET", "/api/v2/stock", "", b"", h["X-Timestamp"],
                     "TOK-1"))

    def test_the_user_id_travels_as_a_header(self):
        fake = Fake()
        client(fake).call("GET", "/api/v2/stock")
        self.assertEqual(fake.seen[-1]["headers"]["X-User-Id"], "1234")

    def test_the_token_is_fetched_before_the_first_call(self):
        fake = Fake()
        client(fake).call("GET", "/api/v2/stock")
        self.assertEqual(fake.seen[0]["path"], "/api/v2/get_token")

    def test_and_not_fetched_again_for_the_second_call(self):
        fake = Fake()
        c = client(fake)
        c.call("GET", "/api/v2/stock")
        c.call("GET", "/api/v2/check_balance")
        self.assertEqual(fake.tokens_issued, 1)

    def test_the_secret_itself_never_leaves(self):
        """Он даёт доступ к деньгам на балансе поставщика."""
        fake = Fake()
        client(fake).call("GET", "/api/v2/stock")
        blob = json.dumps(fake.seen, default=str)
        self.assertNotIn(SECRET, blob)


class AnExpiredTokenIsRenewedOnceNotForever(NoSleeping):
    """Токен живёт два часа. Но повтор обязан подписываться заново: сервер
    принимает каждую подпись один раз, и старая уже потрачена."""

    class Expiring(Fake):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def request(self, method, url, headers=None, data=None, timeout=None):
            if "/get_token" not in url:
                self.calls += 1
                if self.calls == 1:
                    self.seen.append({"method": method, "path": "/api/v2/stock",
                                      "query": "", "headers": dict(headers),
                                      "body": data})
                    return Reply(401, {"detail": "token expired"})
            return super().request(method, url, headers=headers, data=data)

    def test_it_logs_in_again_and_succeeds(self):
        fake = self.Expiring()
        got = client(fake).call("GET", "/api/v2/stock")
        self.assertEqual(got, {"ok": True})
        self.assertEqual(fake.tokens_issued, 2)

    def test_the_retry_carries_a_fresh_signature(self):
        fake = self.Expiring()
        client(fake).call("GET", "/api/v2/stock")
        sigs = [s["headers"]["X-Signature"] for s in fake.seen
                if s["path"] == "/api/v2/stock"]
        self.assertEqual(len(set(sigs)), len(sigs), "подпись отправлена дважды")

    def test_it_does_not_loop_forever_on_401(self):
        class Always(Fake):
            def request(self, method, url, headers=None, data=None, timeout=None):
                if "/get_token" in url:
                    return super().request(method, url, headers=headers,
                                           data=data)
                return Reply(401, {"detail": "nope"})

        fake = Always()
        with self.assertRaises(N.NSError) as e:
            client(fake).call("GET", "/api/v2/stock")
        self.assertEqual(e.exception.code, 401)
        self.assertLessEqual(fake.tokens_issued, 2)


class RefusalsAreSaidInRussian(NoSleeping):
    """Английский код ошибки на экране продавца — это отписка."""

    def refusal(self, code, payload=None):
        fake = Fake({"/api/v2/stock": (code, payload or {"detail": "no"})})
        with self.assertRaises(N.NSError) as e:
            client(fake).call("GET", "/api/v2/stock")
        return e.exception

    def test_a_whitelist_refusal_names_the_whitelist(self):
        """403 у этого поставщика — чаще всего IP не в белом списке, а бот
        живёт на Railway, где адрес меняется при каждом деплое."""
        self.assertIn("бел", self.refusal(403).why)

    def test_a_two_factor_refusal_names_the_code(self):
        self.assertIn("двухфактор", self.refusal(428).why)

    def test_a_conflict_says_the_order_was_already_paid(self):
        self.assertIn("уже оплачен", self.refusal(409).why)

    def test_the_suppliers_own_words_are_kept_too(self):
        why = self.refusal(400, {"detail": "service_id is required"}).why
        self.assertIn("service_id is required", why)

    def test_an_unknown_code_still_comes_through(self):
        self.assertIn("418", N.explain_http(418))


class ReadingTheCatalogue(unittest.TestCase):
    STOCK = {"categories": [
        {"category_name": "Roblox", "category_id": 42,
         "fields": [{"key": "account", "type": "string", "required": True}],
         "services": [{"service_id": 900, "service_name": "Roblox | 100 Robux",
                       "price": 1.1, "currency": "USD", "in_stock": 50}]},
        {"category_name": "Apple Gift Card USA", "category_id": 17,
         "fields": [{"key": "quantity", "type": "int"}],
         "services": [{"service_id": 20, "service_name": "Apple | 2 USD",
                       "price": 1.9, "currency": "USD", "in_stock": 73}]},
    ]}

    def test_a_service_is_found_by_word(self):
        got = N.find_services(self.STOCK, "robux")
        self.assertEqual([s["service_id"] for s in got], [900])

    def test_the_category_name_counts_as_well(self):
        got = N.find_services(self.STOCK, "roblox")
        self.assertEqual([s["service_id"] for s in got], [900])

    def test_the_fields_schema_travels_with_the_service(self):
        """Без схемы полей заказ не составить, а лежит она у категории."""
        got = N.find_services(self.STOCK, "robux")[0]
        self.assertEqual(got["fields"][0]["key"], "account")

    def test_an_empty_needle_returns_everything(self):
        self.assertEqual(len(N.find_services(self.STOCK, "")), 2)

    def test_a_word_that_is_not_there_returns_nothing(self):
        self.assertEqual(N.find_services(self.STOCK, "minecraft"), [])

    def test_a_broken_answer_does_not_raise(self):
        for junk in ({}, {"categories": None}, {"categories": ["x"]}, None):
            self.assertEqual(N.find_services(junk, "robux"), [])


class TheReadyMadeCallsNeverRaise(NoSleeping):
    """Фоновый цикл не должен падать из-за поставщика."""

    def test_stock_returns_a_reason_instead_of_an_exception(self):
        fake = Fake({"/api/v2/stock": (403, {"detail": "ip"})})
        real = N._client
        N._client = lambda creds: client(fake)
        try:
            ok, why = N.stock_sync(CREDS)
        finally:
            N._client = real
        self.assertFalse(ok)
        self.assertIn("бел", str(why))

    def test_balance_comes_back_as_a_string(self):
        fake = Fake({"/api/v2/check_balance": (200, {"balance": "305.2403"})})
        real = N._client
        N._client = lambda creds: client(fake)
        try:
            ok, value = N.balance_sync(CREDS)
        finally:
            N._client = real
        self.assertTrue(ok)
        self.assertEqual(value, "305.2403")


if __name__ == "__main__":
    unittest.main()
