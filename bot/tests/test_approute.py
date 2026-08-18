"""Поставщик AppRoute: конверт ответа, отказы, каталог и ключ.

Поставщик выбран, и с него начинается автовыдача Robux. Проверяется в первую
очередь то, на чём этот проект уже обжигался.

**Главное здесь — конверт.** У AppRoute «получилось ли» отвечает поле `code`
внутри тела, а не статус HTTP: «нет в наличии» и «не хватает денег» приезжают
с HTTP 200. Клиент, который смотрит на статус, будет бодро докладывать об
успехе там, где ничего не произошло, — это самая дорогая поломка в этом
проекте, и её ловят первые же тесты.

**Второе — camelCase на проводе.** В SDK поля пишутся `item_id`, но его
транспорт переводит их в `itemId` перед отправкой. Мы идём без SDK, значит
имена наши собственные, и разойтись с сервером здесь так же легко, как
разошлись с Fragment.

**Третье — ключ наружу не выходит.** Он один даёт право тратить баланс
кабинета целиком.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import approute as A          # noqa: E402
from handlers import approute as H            # noqa: E402

# Заведомо не ключ: сканер секретов у GitHub видит в `sk_live_` + сплошной
# алфавит ключ Stripe и отклоняет пуш целиком. Дефисы ломают этот шаблон, а
# для теста важна только длина и узнаваемость.
KEY = "sk_live_test-not-a-real-key-0000-0000-0000"
CREDS = {"api_key": KEY, "region": "io"}


def envelope(code="OK", data=None, message="", trace="tr-1", errors=None):
    """Ответ в том виде, в каком его отдаёт поставщик."""
    body = {"status": "completed", "code": code, "message": message,
            "traceId": trace, "data": data}
    if errors:
        body["errors"] = errors
    return body


class Reply:
    def __init__(self, payload, code=200, headers=None):
        self.status_code = code
        self._payload = payload
        self.headers = headers or {}
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Fake:
    """Сервер поставщика: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self, *replies):
        self.replies = list(replies) or [Reply(envelope(data={}))]
        self.seen: list[dict] = []
        self.proxies: dict = {}

    def request(self, method, url, headers=None, params=None, json=None,
                timeout=None):
        self.seen.append({"method": method, "url": url,
                          "headers": dict(headers or {}), "params": params,
                          "body": json})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeSession:
    """Сессия-подставка.

    И рабочие вызовы, и проба ходят через `_session` — одну точку на весь
    модуль. Пока проба ходила мимо неё, напрямую через `requests.get`, тесты
    подменяли не то, что работает, а прогон уходил в настоящую сеть и
    растягивался на полминуты.
    """

    def __init__(self, answer, proxy=""):
        self.answer = answer
        self.proxy = proxy
        self.proxies: dict = {}
        self.calls: list[dict] = []
        self.posts: list[dict] = []
        if proxy:
            self.proxies.update({"http": proxy, "https": proxy})

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "proxies": dict(self.proxies)})
        return self.answer(url)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "proxies": dict(self.proxies), "body": json})
        self.posts.append({"url": url, "headers": dict(headers or {}),
                           "body": json})
        return self.answer(url)


def patch_session(case, answer):
    """Подменить сессию на время теста и вернуть список созданных."""
    made: list[FakeSession] = []
    real = A._session

    def factory(proxy=""):
        s = FakeSession(answer, proxy)
        made.append(s)
        return s

    A._session = factory
    case.addCleanup(lambda: setattr(A, "_session", real))
    return made


def client(fake, **over):
    c = A.ARClient(**{**{"api_key": KEY}, **over})
    c.session = fake
    return c


class TheEnvelopeDecidesNotTheHttpStatus(unittest.TestCase):
    """HTTP 200 — не доказательство. У этого поставщика отказ «нет в наличии»
    приезжает именно двухсотым, и клиент, читающий статус, отчитается об
    успехе там, где ничего не произошло."""

    def test_a_two_hundred_with_a_refusal_code_is_a_refusal(self):
        fake = Fake(Reply(envelope("OUT_OF_STOCK", message="no stock")))
        with self.assertRaises(A.ARError) as e:
            client(fake).call("GET", "/services")
        self.assertEqual(e.exception.code, "OUT_OF_STOCK")

    def test_the_payload_is_the_data_field_not_the_whole_body(self):
        fake = Fake(Reply(envelope(data={"items": [1, 2]})))
        self.assertEqual(client(fake).call("GET", "/services"),
                         {"items": [1, 2]})

    def test_accepted_counts_as_success(self):
        fake = Fake(Reply(envelope("ACCEPTED", data={"x": 1})))
        self.assertEqual(client(fake).call("GET", "/services"), {"x": 1})

    def test_a_repeat_of_the_same_order_counts_as_success(self):
        """`IDEMPOTENCY_REPLAY` — это «такой заказ уже был, отдаю прежний
        результат». Принять его за отказ значит потерять уже оплаченный
        заказ: ровно та беда, которую мы разбирали на звёздах."""
        fake = Fake(Reply(envelope("IDEMPOTENCY_REPLAY", data={"x": 1})))
        self.assertEqual(client(fake).call("GET", "/orders"), {"x": 1})

    def test_an_unknown_code_is_a_refusal_not_a_success(self):
        fake = Fake(Reply(envelope("SOMETHING_NEW", data={"x": 1})))
        with self.assertRaises(A.ARError):
            client(fake).call("GET", "/services")

    def test_a_body_without_an_envelope_is_reported_by_its_http_code(self):
        """Отвечал не поставщик, а что-то перед ним."""
        fake = Fake(Reply({"nginx": "502"}, code=502))
        with self.assertRaises(A.ARError) as e:
            client(fake, max_retries=0).call("GET", "/services")
        self.assertIn("вышестоящ", e.exception.why)

    def test_html_instead_of_json_does_not_crash_with_a_parser_error(self):
        fake = Fake(Reply(None, code=404))
        with self.assertRaises(A.ARError) as e:
            client(fake).call("GET", "/services")
        self.assertIn("base_url", e.exception.why)


class ARefusalIsExplainedInRussianAndCanBeTraced(unittest.TestCase):
    """Английский код на экране продавца — отписка. А `traceId` — то, по чему
    поставщик найдёт запрос у себя; без него разговор с поддержкой начинается
    с «пришлите время и что вы делали»."""

    def refusal(self, code, message="", errors=None):
        fake = Fake(Reply(envelope(code, message=message, errors=errors),
                          code=422))
        try:
            client(fake).call("GET", "/services")
        except A.ARError as e:
            return e
        self.fail("отказ не поднялся")

    def test_every_documented_code_has_a_russian_reason(self):
        for code in ("VALIDATION_ERROR", "UNAUTHORIZED", "FORBIDDEN",
                     "NOT_FOUND", "CONFLICT", "LIMIT_REACHED", "OUT_OF_STOCK",
                     "INSUFFICIENT_FUNDS", "UPSTREAM_ERROR", "INTERNAL_ERROR"):
            why = self.refusal(code).why
            self.assertTrue(any("а" <= ch.lower() <= "я" for ch in why),
                            f"{code} остался без перевода: {why}")

    def test_not_enough_money_says_so_in_plain_words(self):
        self.assertIn("не хватает средств", self.refusal("INSUFFICIENT_FUNDS").why)

    def test_a_rejected_key_names_both_things_it_can_mean(self):
        """«Ключ не принят» означает сразу три разные вещи: ключ не тот,
        кабинет не тот, наш адрес не в белом списке. Продавцу нечем их
        различить, если не назвать их вслух."""
        why = self.refusal("UNAUTHORIZED").why
        self.assertIn("кабинет", why)
        self.assertIn("белом списке", why)

    def test_the_trace_id_reaches_the_seller(self):
        self.assertIn("tr-1", self.refusal("INTERNAL_ERROR").explain())

    def test_the_field_errors_are_named(self):
        e = self.refusal("VALIDATION_ERROR", errors=[
            {"field": "quantity", "code": "OUT_OF_RANGE", "message": "too big"}])
        self.assertIn("quantity", e.explain())

    def test_the_suppliers_own_words_are_kept(self):
        """Их текст часто конкретнее кода."""
        self.assertIn("limit 3/day",
                      self.refusal("LIMIT_REACHED", message="limit 3/day").why)


class AnAnswerWithoutTheEnvelopeIsExplainedNotJustNumbered(unittest.TestCase):
    """Живой отказ 17.08: «❌ Ключ не сработал · ❌ HTTP 200», и всё. Тело
    пришло, `traceId` в нём был, а поля `code` не было вовсе — то есть форма
    ответа не та, что в их SDK. Голая цифра на экране продавца — отписка:
    из неё не следует ни что случилось, ни что делать дальше."""

    def refusal(self, body, http=200):
        fake = Fake(Reply(body, code=http))
        try:
            client(fake, max_retries=0).call("GET", "/accounts")
        except A.ARError as e:
            return e
        self.fail("отказ не поднялся")

    def test_a_two_hundred_without_a_code_says_what_is_missing(self):
        e = self.refusal({"traceId": "25d2b713", "status": "completed"})
        self.assertIn("code", e.why)
        self.assertNotEqual(e.why.strip(), "HTTP 200")

    def test_it_lists_what_did_arrive(self):
        """По этому и видно, чем ответ отличается от описанного."""
        e = self.refusal({"traceId": "25d2b713", "result": {}})
        self.assertIn("traceId", e.why)
        self.assertIn("result", e.why)

    def test_the_suppliers_own_message_is_shown_if_there_is_one(self):
        e = self.refusal({"traceId": "t", "message": "ip not allowed"})
        self.assertIn("ip not allowed", e.why)

    def test_the_trace_id_still_reaches_the_seller(self):
        e = self.refusal({"traceId": "25d2b713"})
        self.assertIn("25d2b713", e.explain())

    def test_it_points_at_the_diagnostic(self):
        e = self.refusal({"traceId": "t"})
        self.assertIn("apr_debug", e.why)

    def test_a_known_http_code_keeps_its_own_wording(self):
        """401 объясняется по-своему и без рассуждений про конверт."""
        e = self.refusal({"traceId": "t"}, http=401)
        self.assertIn("ключ не принят", e.why)
        self.assertNotIn("apr_debug", e.why)


class TheLiveEnvelopeIsNotTheOneInTheirSdk(unittest.TestCase):
    """Снято пробой на настоящем ключе 17.08. На проводе приходит

        {data, errorCode, errors, status, statusCode, statusMessage, traceId}

    — ни `code`, ни `message`, которые описаны в их SDK. Одинаково на обоих
    доменах, то есть за ними один шлюз. Разбираем то, что видели: иначе бот
    не понимает ни одного настоящего ответа этого поставщика.

    Признак успеха здесь **выведен из имён полей, а не прочитан в
    документации**, поэтому сомнение решается в сторону отказа: принять
    отказ за успех значит отчитаться о выдаче, которой не было."""

    def answer(self, body, http=200):
        fake = Fake(Reply(body, code=http))
        return client(fake, max_retries=0)

    def live(self, **over):
        body = {"data": None, "errorCode": None, "errors": None,
                "status": True, "statusCode": 200, "statusMessage": "OK",
                "traceId": "4af69e7d"}
        body.update(over)
        return body

    def test_an_error_code_is_a_refusal_even_at_http_200(self):
        c = self.answer(self.live(errorCode="UNAUTHORIZED",
                                  statusCode=401, statusMessage="Invalid key"))
        with self.assertRaises(A.ARError) as e:
            c.call("GET", "/accounts")
        self.assertEqual(e.exception.code, "UNAUTHORIZED")

    def test_the_suppliers_own_sentence_reaches_the_seller(self):
        """`statusMessage` — то место, где сервер говорит словами. Первая
        проба напечатала только имена полей, и эта строка потерялась."""
        c = self.answer(self.live(errorCode="FORBIDDEN",
                                  statusMessage="IP 1.2.3.4 is not allowed"))
        with self.assertRaises(A.ARError) as e:
            c.call("GET", "/accounts")
        self.assertIn("1.2.3.4", e.exception.why)

    def test_an_unknown_error_code_still_says_something_useful(self):
        c = self.answer(self.live(errorCode="WHAT_IS_THIS",
                                  statusMessage="что-то пошло не так"))
        with self.assertRaises(A.ARError) as e:
            c.call("GET", "/accounts")
        self.assertIn("что-то пошло не так", e.exception.why)

    def test_the_data_comes_back_when_nothing_complains(self):
        c = self.answer(self.live(data={"ip": "1.2.3.4"}))
        self.assertEqual(c.call("GET", "/whoami"), {"ip": "1.2.3.4"})

    def test_an_empty_data_without_complaints_is_not_called_success(self):
        """`data: null` и никаких жалоб — это «непонятно», а не «получилось».
        У чтения каталога и баланса ответ всегда содержателен."""
        c = self.answer(self.live(data=None))
        with self.assertRaises(A.ARError):
            c.call("GET", "/accounts")

    def test_a_failing_status_flag_is_a_refusal(self):
        c = self.answer(self.live(status=False, data={"x": 1}))
        with self.assertRaises(A.ARError):
            c.call("GET", "/accounts")

    def test_a_non_success_status_code_inside_the_body_is_a_refusal(self):
        """Внутренний `statusCode` важнее внешнего HTTP: снаружи всегда 200."""
        c = self.answer(self.live(statusCode=403, data={"x": 1}))
        with self.assertRaises(A.ARError) as e:
            c.call("GET", "/accounts")
        self.assertIn("403", e.exception.why)

    def test_field_errors_alone_are_enough_to_refuse(self):
        c = self.answer(self.live(data={"x": 1},
                                  errors=[{"field": "q", "message": "плохо"}]))
        with self.assertRaises(A.ARError):
            c.call("GET", "/accounts")

    def test_the_trace_id_survives_the_other_envelope(self):
        c = self.answer(self.live(errorCode="UNAUTHORIZED"))
        with self.assertRaises(A.ARError) as e:
            c.call("GET", "/accounts")
        self.assertIn("4af69e7d", e.exception.explain())

    def test_the_sdk_envelope_still_works(self):
        """Их SDK не выдумка: где-то этот конверт может и прийти."""
        fake = Fake(Reply(envelope("OK", data={"items": []})))
        self.assertEqual(client(fake).call("GET", "/services"), {"items": []})


class TheProbePrintsValuesNotJustFieldNames(unittest.TestCase):
    """Первая проба напечатала «поля тела: data, errorCode, …» — и на этом
    остановилась. Ответ на вопрос «что не так» лежал в значении
    `statusMessage`, которое она не показала. Перечень имён без значений —
    диагностика, которая говорит «форма не та» и молчит о причине."""

    def setUp(self):
        patch_session(self, lambda url: Reply({
            "data": None, "errorCode": "UNAUTHORIZED", "errors": None,
            "status": False, "statusCode": 401,
            "statusMessage": "API key is not allowed from IP 1.2.3.4",
            "traceId": "4af69e7d"}))

    def test_the_message_value_is_in_the_report(self):
        rows = A.probe_sync(CREDS)
        self.assertIn("statusMessage", rows[0]["fields"])
        self.assertIn("1.2.3.4", rows[0]["fields"]["statusMessage"])

    def test_the_error_code_value_is_in_the_report(self):
        self.assertEqual(A.probe_sync(CREDS)[0]["fields"]["errorCode"],
                         "UNAUTHORIZED")

    def test_the_inner_status_code_is_shown_too(self):
        """Снаружи всегда 200 — значимо именно внутреннее число."""
        self.assertEqual(A.probe_sync(CREDS)[0]["fields"]["statusCode"], "401")

    def test_it_says_whether_there_was_any_data_at_all(self):
        self.assertIn("null", A.probe_sync(CREDS)[0]["data"])

    def test_data_contents_are_described_not_dumped(self):
        patch_session(self, lambda url: Reply({
            "data": {"ip": "1.2.3.4", "account": "x"}, "traceId": "t",
            "statusCode": 200}))
        self.assertIn("ip", A.probe_sync(CREDS)[0]["data"])

    def test_the_key_is_cut_out_of_values_too(self):
        """Поставщик вполне может вернуть ключ в тексте своей же ошибки."""
        patch_session(self, lambda url: Reply({
            "statusMessage": f"key {KEY} rejected", "errorCode": "UNAUTHORIZED",
            "traceId": "t"}))
        blob = json.dumps(A.probe_sync(CREDS), ensure_ascii=False)
        self.assertNotIn(KEY, blob)


class EveryRequestGoesTheSameWayIncludingTheDiagnostics(unittest.TestCase):
    """Белый список у поставщика привязан к адресу, с которого мы приходим.
    Значит проба обязана идти тем же маршрутом, что и рабочие вызовы: первая
    версия ходила напрямую, а каталог и баланс — через прокси продавца, и
    отчёт показывал бы адрес, с которого поставщик нас вообще не видит.
    Диагностика, врущая про собственный маршрут, хуже её отсутствия."""

    PROXY = "http://user:pass@1.2.3.4:8080"

    def test_the_probe_uses_the_sellers_proxy(self):
        made = patch_session(self, lambda url: Reply(envelope("OK", data={})))
        A.probe_sync({**CREDS, "proxy": self.PROXY})
        self.assertTrue(made, "сессия не создавалась вовсе")
        self.assertTrue(all(s.proxy == self.PROXY for s in made))

    def test_the_working_calls_use_it_too(self):
        made = patch_session(self, lambda url: Reply(envelope("OK", data={})))
        A._client({**CREDS, "proxy": self.PROXY})
        self.assertEqual(made[-1].proxy, self.PROXY)

    def test_without_a_proxy_nothing_is_routed_anywhere(self):
        made = patch_session(self, lambda url: Reply(envelope("OK", data={})))
        A._client(dict(CREDS))
        self.assertEqual(made[-1].proxies, {})

    def test_the_session_carries_the_proxy_to_requests(self):
        """Проверка самой сборки сессии, без подмены."""
        s = A._session(self.PROXY)
        self.assertEqual(s.proxies.get("https"), self.PROXY)


class TheProxyPasswordIsNeverShown(unittest.TestCase):
    """В строке прокси логин и пароль от платного адреса — чужой доступ
    ровно так же, как ключ."""

    PROXY = "socks5://vasya:secret-pass@5.6.7.8:1080"

    def test_the_label_keeps_only_host_and_port(self):
        label = A.proxy_label({"proxy": self.PROXY})
        self.assertIn("5.6.7.8", label)
        self.assertNotIn("secret-pass", label)
        self.assertNotIn("vasya", label)

    def test_no_proxy_says_so_plainly(self):
        self.assertEqual(A.proxy_label({}), "не задан")

    def test_a_socks_proxy_without_the_package_is_flagged_before_the_request(self):
        """Иначе продавец получит английское «Missing dependencies for SOCKS
        support» вместо ответа — то есть отписку."""
        import automation.fragment as F
        real = F.proxy_problem
        F.proxy_problem = lambda url: ("для socks5 нужен пакет PySocks"
                                       if str(url).startswith("socks") else "")
        self.addCleanup(lambda: setattr(F, "proxy_problem", real))
        self.assertIn("PySocks", A.proxy_problem({"proxy": self.PROXY}))
        self.assertEqual(A.proxy_problem({"proxy": "http://1.2.3.4:8080"}), "")

    def test_the_stored_proxy_is_encrypted_like_the_key(self):
        import storage
        self.assertIn("proxy", storage._AR_SECRET_FIELDS)


class WhetherAProxyFitsTheAllowlistIsCheckedNotAdvised(unittest.TestCase):
    """«Подойдёт ли мой прокси» — вопрос проверяемый, и отвечать на него
    советом значит перекладывать проверку на продавца. Белому списку нужен
    постоянный адрес; ротационный прокси меняет его от запроса к запросу, и
    вписывать в кабинет нечего."""

    def ips(self, *answers):
        import automation.fragment as F
        seq = list(answers)
        real = F.outbound_ip
        F.outbound_ip = lambda proxy="": (seq.pop(0) if seq else "5.5.5.5")
        self.addCleanup(lambda: setattr(F, "outbound_ip", real))

    def test_one_address_every_time_is_called_suitable(self):
        self.ips("1.2.3.4", "1.2.3.4", "1.2.3.4", "9.9.9.9")
        got = A.proxy_check_sync({"proxy": "http://p:1"})
        self.assertTrue(got["stable"])
        self.assertEqual(got["ip"], "1.2.3.4")

    def test_a_changing_address_is_called_unsuitable(self):
        """Ровно то, чем плох дешёвый ротационный прокси."""
        self.ips("1.2.3.4", "1.2.3.5", "1.2.3.6", "9.9.9.9")
        got = A.proxy_check_sync({"proxy": "http://p:1"})
        self.assertFalse(got["stable"])
        self.assertEqual(len(got["seen"]), 3)

    def test_a_proxy_that_changes_nothing_is_caught(self):
        """«Прокси задан» и «запросы идут через прокси» — разные
        утверждения. Совпадение с прямым адресом означает второе неверным."""
        self.ips("7.7.7.7", "7.7.7.7", "7.7.7.7", "7.7.7.7")
        got = A.proxy_check_sync({"proxy": "http://p:1"})
        self.assertTrue(got["same_as_direct"])

    def test_a_dead_proxy_leaves_no_address(self):
        self.ips("адрес не узнать", "адрес не узнать", "адрес не узнать",
                 "9.9.9.9")
        got = A.proxy_check_sync({"proxy": "http://p:1"})
        self.assertEqual(got["ip"], "")
        self.assertFalse(got["stable"])

    def test_without_a_proxy_it_reports_the_direct_address(self):
        self.ips("9.9.9.9")
        got = A.proxy_check_sync({})
        self.assertFalse(got["proxy"])
        self.assertEqual(got["ip"], "9.9.9.9")

    def test_the_verdict_names_the_address_to_whitelist(self):
        self.ips("1.2.3.4", "1.2.3.4", "1.2.3.4", "9.9.9.9")
        said = asyncio.run(H._proxy_verdict({"proxy": "http://p:1"}))
        self.assertIn("1.2.3.4", said)
        self.assertIn("белый список", said)

    def test_the_verdict_refuses_a_rotating_proxy_plainly(self):
        self.ips("1.2.3.4", "1.2.3.5", "1.2.3.6", "9.9.9.9")
        said = asyncio.run(H._proxy_verdict({"proxy": "http://p:1"}))
        self.assertIn("не годится", said)
        self.assertNotIn("✅", said)

    def test_the_verdict_does_not_pass_a_sample_off_as_proof(self):
        """Прокси может менять адрес раз в час — три запроса этого не
        покажут. Догадка, поданная как факт, — то же враньё."""
        self.ips("1.2.3.4", "1.2.3.4", "1.2.3.4", "9.9.9.9")
        said = asyncio.run(H._proxy_verdict({"proxy": "http://p:1"}))
        self.assertIn("выборка", said)

    def test_the_check_is_reachable_from_the_screen(self):
        """Проверка, до которой не дойти кнопкой, продавцу не существует."""
        import inspect
        self.assertIn("apr:proxy_test", inspect.getsource(H))


class TheAllowlistIsTheFirstThingToCheck(unittest.TestCase):
    """У AppRoute белый список IP — это написано на экране выдачи ключа, а не
    в SDK. Бот живёт на Railway, где адрес меняется при каждом выкате, и
    «ключ не сработал» после деплоя обычно означает именно это."""

    def test_a_rejected_key_names_the_allowlist_not_just_the_dashboard(self):
        fake = Fake(Reply(envelope("UNAUTHORIZED"), code=401))
        try:
            client(fake, max_retries=0).call("GET", "/accounts")
        except A.ARError as e:
            self.assertIn("белом списке", e.why)

    def test_a_rejected_key_names_the_expiry_too(self):
        """Продавец сказал, что в кабинете выдают ещё и временный ключ на 48
        часов, живущий по другим правилам. Назвать одну причину из трёх —
        значит отправить человека проверять не то."""
        fake = Fake(Reply(envelope("UNAUTHORIZED"), code=401))
        try:
            client(fake, max_retries=0).call("GET", "/accounts")
        except A.ARError as e:
            self.assertIn("48 часов", e.why)
            self.assertIn("белом списке", e.why)
            self.assertIn("кабинет", e.why)

    def test_forbidden_says_the_same(self):
        fake = Fake(Reply(envelope("FORBIDDEN"), code=403))
        try:
            client(fake, max_retries=0).call("GET", "/accounts")
        except A.ARError as e:
            self.assertIn("белом списке", e.why)

    def test_whoami_only_reads(self):
        fake = Fake(Reply(envelope(data={"ip": "1.2.3.4"})))
        real = A._client
        A._client = lambda creds: client(fake)
        try:
            ok, data = A.whoami_sync(CREDS)
        finally:
            A._client = real
        self.assertTrue(ok)
        self.assertEqual(fake.seen[-1]["method"], "GET")
        self.assertTrue(fake.seen[-1]["url"].endswith("/whoami"))


class TheRawProbeAsksBothDashboards(unittest.TestCase):
    """Разбирать непонятное по догадкам в этом проекте уже стоило дней. Проба
    печатает факты: статус, поля тела и его начало — по обоим адресам, потому
    что кабинет approute.ru предлагает проверять ключ запросом на
    approute.io, и какой из них наш — вопрос к серверу, а не к нам."""

    def setUp(self):
        self.made = patch_session(
            self, lambda url: Reply(envelope("OK", data={"ip": "1.2.3.4"})))

    @property
    def calls(self):
        return [c for s in self.made for c in s.calls]

    def test_both_dashboards_are_asked(self):
        A.probe_sync(CREDS)
        hosts = {c["url"].split("/api/")[0] for c in self.calls}
        self.assertEqual(hosts, {"https://approute.io", "https://approute.ru"})

    def test_it_asks_who_we_are_and_what_the_balance_is(self):
        A.probe_sync(CREDS)
        paths = {c["url"].rsplit("/", 1)[-1] for c in self.calls}
        self.assertEqual(paths, {"whoami", "accounts"})

    def test_the_rows_carry_the_facts_not_a_verdict(self):
        rows = A.probe_sync(CREDS)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["http"], 200)
            self.assertIn("traceId", row["keys"])
            self.assertEqual(row["code"], "OK")

    def test_a_body_that_is_not_json_is_quoted_as_it_came(self):
        patch_session(self, lambda url: Reply(None, code=502))
        rows = A.probe_sync(CREDS)
        self.assertTrue(all(not r["json"] for r in rows))

    def test_a_dead_host_does_not_stop_the_other_one(self):
        """Половина фактов лучше, чем трассировка вместо отчёта."""
        def half(url):
            if "approute.ru" in url:
                raise A.requests.ConnectionError("no route to host")
            return Reply(envelope("OK", data={}))

        patch_session(self, half)
        rows = A.probe_sync(CREDS)
        self.assertTrue(any(r["error"] for r in rows))
        self.assertTrue(any(r["http"] == 200 for r in rows))

    def test_the_key_never_appears_in_the_probe_output(self):
        patch_session(self, lambda url: Reply(None, code=418))
        blob = json.dumps(A.probe_sync(CREDS), ensure_ascii=False)
        self.assertNotIn(KEY, blob)

    def test_the_key_is_cut_out_even_if_the_server_echoes_it(self):
        """Поставщик вполне может вернуть его в тексте ошибки."""
        patch_session(self, lambda url: Reply(None, code=400))
        real_redact = A._redact
        self.assertIn("вырезан", real_redact(f"bad key {KEY}", KEY))
        self.assertNotIn(KEY, real_redact(f"bad key {KEY}", KEY))


class TheKeyTravelsAsAHeaderAndNowhereElse(unittest.TestCase):
    def test_the_header_is_the_one_the_api_expects(self):
        fake = Fake()
        client(fake).call("GET", "/accounts")
        self.assertEqual(fake.seen[-1]["headers"]["X-API-Key"], KEY)

    def test_the_key_is_not_in_the_url_or_the_query(self):
        """В адрес он попадал бы в чужие логи по дороге."""
        fake = Fake()
        client(fake).call("GET", "/accounts")
        sent = fake.seen[-1]
        self.assertNotIn(KEY, sent["url"])
        self.assertNotIn(KEY, json.dumps(sent["params"]))

    def test_a_refusal_never_repeats_the_key(self):
        fake = Fake(Reply(envelope("UNAUTHORIZED", message="bad key"),
                          code=401))
        ok, why = A.balance_sync({"api_key": KEY, "region": "io"})
        self.assertIn(str(why), str(why))          # причина есть
        self.assertNotIn(KEY, str(why))

    def test_the_dashboard_choice_picks_the_base_url(self):
        self.assertIn("approute.io", A.base_url_of({"region": "io"}))
        self.assertIn("approute.ru", A.base_url_of({"region": "ru"}))

    def test_an_unknown_region_falls_back_to_the_default_one(self):
        self.assertEqual(A.base_url_of({"region": "zz"}), A.BASE_URL)

    def test_an_explicit_base_url_wins(self):
        self.assertEqual(A.base_url_of({"base_url": "https://x/api/v1/"}),
                         "https://x/api/v1")


class TooManyRequestsIsWaitedOutNotReportedAsFailure(unittest.TestCase):
    def setUp(self):
        self.slept: list[float] = []
        self._sleep = A.time.sleep
        A.time.sleep = self.slept.append

    def tearDown(self):
        A.time.sleep = self._sleep

    def test_the_second_try_succeeds(self):
        fake = Fake(Reply(envelope(), code=429),
                    Reply(envelope(data={"items": []})))
        self.assertEqual(client(fake).call("GET", "/services"), {"items": []})
        self.assertEqual(len(fake.seen), 2)

    def test_the_wait_is_the_one_the_supplier_asked_for(self):
        """Своя оценка была бы догадкой поверх его же ответа."""
        fake = Fake(Reply(envelope(), code=429, headers={"Retry-After": "7"}),
                    Reply(envelope(data={})))
        client(fake).call("GET", "/services")
        self.assertEqual(self.slept, [7.0])

    def test_a_permanent_refusal_is_not_retried(self):
        fake = Fake(Reply(envelope("UNAUTHORIZED"), code=401))
        with self.assertRaises(A.ARError):
            client(fake).call("GET", "/services")
        self.assertEqual(len(fake.seen), 1)


class TheCatalogueIsReadAsItCame(unittest.TestCase):
    """Имена полей — из `openapi.yaml`, а не из наших привычек: на проводе
    camelCase (`categoryName`, `itemId`), потому что snake_case в SDK — это
    его собственный перевод перед отправкой."""

    CATALOG = {"items": [
        {"id": "svc-1", "name": "Roblox Gift Card", "type": "voucher",
         "categoryName": "Games", "subcategoryName": "Roblox",
         "countryCode": "US",
         "items": [
             {"id": "it-1", "name": "800 Robux", "nominal": 10, "price": 8.4,
              "currency": "USDT", "available": True, "stock": 25},
             {"id": "it-2", "name": "1700 Robux", "nominal": 20, "price": 16.9,
              "currency": "USDT", "available": False, "stock": 0},
         ],
         "fields": [{"key": "nickname", "type": "text", "required": True},
                    {"key": "region", "type": "select"}]},
        {"id": "svc-2", "name": "Steam Wallet", "type": "direct_topup",
         "categoryName": "Wallets", "items": [], "fields": []},
    ], "hasNext": False}

    def test_searching_by_name_finds_the_product(self):
        got = A.find_products(self.CATALOG, "roblox")
        self.assertEqual([p["id"] for p in got], ["svc-1"])

    def test_searching_by_category_finds_it_too(self):
        self.assertEqual(len(A.find_products(self.CATALOG, "wallets")), 1)

    def test_an_empty_word_returns_everything(self):
        self.assertEqual(len(A.find_products(self.CATALOG, "")), 2)

    def test_the_search_is_case_insensitive(self):
        self.assertEqual(len(A.find_products(self.CATALOG, "ROBLOX")), 1)

    def test_a_truncated_catalogue_is_recognised(self):
        """«Роблокса нет» и «нам прислали не весь список» — разные ответы."""
        self.assertFalse(A.truncated(self.CATALOG))
        self.assertTrue(A.truncated({"items": [], "hasNext": True}))

    def test_balances_are_read_from_their_field_names(self):
        lines = A.balance_lines({"items": [
            {"currency": "USDT", "balance": 120.5, "available": 100.0,
             "overdraftLimit": 0}]})
        self.assertEqual(len(lines), 1)
        self.assertIn("USDT", lines[0])
        self.assertIn("120.5", lines[0])
        self.assertIn("100.0", lines[0])

    def test_no_accounts_means_no_lines(self):
        self.assertEqual(A.balance_lines({"items": []}), [])
        self.assertEqual(A.balance_lines(None), [])


class NothingHereBuysAnything(unittest.TestCase):
    """Диагностика, которая тратит деньги, — не диагностика.

    Запрет писался под условие: «пока не видно, под каким `itemId` лежит
    Roblox и каких полей требует заказ, выдача была бы гаданием на чужих
    деньгах». 18.08 условие выполнено — живой `/services` на настоящем ключе
    отдал 1263 услуги, среди них 26 с Roblox: все типа `voucher`, `fields`
    пустые, `itemId` известны.

    Поэтому запрет **сужен, а не снят**: покупки по-прежнему нет. Разрешён
    ровно один вызов `POST /orders` — сухой прогон формы тела. Что он
    остаётся безопасным, проверяет `TheOrderProbeNeverBuysAnything`:
    `checkOnly` в каждом теле и несуществующий товар, пока продавец не
    назвал свой.
    """

    def test_the_client_has_no_order_call(self):
        import inspect
        src = inspect.getsource(A)
        self.assertNotIn('"POST"', src.replace('json_body: dict | None = None', ''))

    def test_the_only_order_call_is_the_dry_run_probe(self):
        """Место с `/orders` ровно одно, и оно сухое.

        Появится второе — значит кто-то начал писать выдачу, не сверив форму
        тела живым вызовом. Ровно этого запрет и не пускает.
        """
        import inspect
        src = inspect.getsource(A)
        self.assertEqual(src.count('"/orders"'), 1,
                         "вызовов /orders стало больше одного")
        self.assertIn("checkOnly", src)
        self.assertNotIn("create_order", src)

    def test_the_screens_never_place_a_real_order(self):
        """Экранам покупка недоступна: они зовут пробу, а не заказ."""
        import inspect
        src = inspect.getsource(H)
        for forbidden in ("create_order", "orders_type", '"/orders"'):
            self.assertNotIn(forbidden, src)

    def test_the_ready_made_calls_are_reads_only(self):
        for name in ("balance_sync", "services_sync"):
            fake = Fake()
            fn = getattr(A, name)
            real = A._client
            A._client = lambda creds, f=fake: client(f)
            try:
                fn(CREDS)
            finally:
                A._client = real
            self.assertEqual(fake.seen[-1]["method"], "GET",
                             f"{name} сходил не за чтением")


class Msg:
    def __init__(self, text):
        self.text = text
        self.said: list[str] = []
        self.deleted = False
        self.from_user = type("U", (), {"id": 1})()

    async def answer(self, text, reply_markup=None, **kw):
        self.said.append(text)
        return Status(self.said)

    async def delete(self):
        self.deleted = True


class Status:
    def __init__(self, sink):
        self.sink = sink

    async def edit_text(self, text, **kw):
        self.sink.append(text)


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


class CB:
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self):
        self.data: dict = {}
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def buttons(kb) -> list[str]:
    return [] if kb is None else [b.text for row in kb.inline_keyboard
                                  for b in row]


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


class TheKeyIsReadFromWhateverWasPasted(unittest.TestCase):
    def test_a_bare_key_needs_no_label(self):
        """Ключ один, угадывать нечего — требовать подписи значит требовать
        аккуратности там, где она ничего не даёт."""
        self.assertEqual(H.parse_creds(KEY)["api_key"], KEY)

    def test_a_labelled_key_works_too(self):
        self.assertEqual(H.parse_creds(f"api_key: {KEY}")["api_key"], KEY)

    def test_the_russian_label_is_understood(self):
        self.assertEqual(H.parse_creds(f"ключ = {KEY}")["api_key"], KEY)

    def test_the_dashboard_can_be_named_in_russian(self):
        self.assertEqual(H.parse_creds("регион: российский")["region"], "ru")

    def test_the_international_dashboard_is_the_default_reading(self):
        self.assertEqual(H.parse_creds("region: io")["region"], "io")

    def test_a_key_of_an_unexpected_shape_is_still_accepted(self):
        """В README у поставщика примеры вида `sk_live_…`, а настоящий ключ
        из кабинета оказался другой формы (`wli-…`). Проверка «похоже ли на
        ключ» отвергала бы ровно то, что поставщик и выдал."""
        real = "wli-lq1Tm941fe613nnbhE6Re-примерно-такой-формы"
        self.assertEqual(H.parse_creds(real)["api_key"], real)

    def test_a_sentence_is_not_mistaken_for_a_key(self):
        self.assertEqual(H.parse_creds("я не понял что присылать"), {})


class TheMessageWithTheKeyDoesNotLinger(unittest.TestCase):
    def setUp(self):
        self.saved: dict = {}
        self._save, self._get = H.save_ar_creds, H.get_ar_creds
        H.save_ar_creds = lambda uid, c: self.saved.update(c)
        H.get_ar_creds = lambda uid: dict(self.saved)

    def tearDown(self):
        H.save_ar_creds, H.get_ar_creds = self._save, self._get

    def login(self, text):
        msg = Msg(text)
        asyncio.run(H.apr_login(msg))
        return msg

    def test_it_is_deleted_after_being_read(self):
        self.assertTrue(self.login(f"/apr_login {KEY}").deleted)

    def test_it_is_deleted_even_when_nothing_parsed(self):
        self.assertTrue(self.login("/apr_login что сюда писать").deleted)

    def test_the_answer_never_repeats_the_key(self):
        msg = self.login(f"/apr_login {KEY}")
        self.assertNotIn(KEY, "\n".join(msg.said))

    def test_an_empty_command_explains_where_the_key_lives(self):
        said = "\n".join(self.login("/apr_login").said)
        self.assertIn("approute.io/dashboard", said)
        self.assertIn("approute.ru/dashboard", said)

    def test_a_saved_key_points_at_the_next_step(self):
        self.assertIn("/apr_stock", "\n".join(self.login(f"/apr_login {KEY}").said))


class TheAccessIsEnteredByButtons(unittest.TestCase):
    """Продавцу неоткуда узнать про команду: раздел он открывает кнопками,
    значит и ключ вводится там же."""

    def setUp(self):
        self.saved: dict = {}
        self._save, self._get = H.save_ar_creds, H.get_ar_creds
        self._del = H.delete_ar_creds
        H.save_ar_creds = lambda uid, c: self.saved.update(c)
        H.get_ar_creds = lambda uid: dict(self.saved)
        H.delete_ar_creds = lambda uid: self.saved.clear()
        # Ввод ключа сам спрашивает поставщика, поэтому сеть подменяется на
        # весь класс: без этого тесты ввода уходили бы в настоящий API и
        # ждали таймаута. Откат — в tearDown: подмена в общем модуле без
        # отката однажды уронила тринадцать чужих тестов.
        self._balance, self._whoami = A.balance_sync, A.whoami_sync
        self.asked: list[str] = []
        A.balance_sync = lambda creds: (
            self.asked.append("balance"),
            (True, {"items": [{"currency": "USD", "balance": 0.0,
                               "available": 0.0}]}))[1]
        A.whoami_sync = lambda creds: (
            self.asked.append("whoami"),
            (True, {"clientIp": "138.124.113.101", "allowlist": [],
                    "allowlistMatches": True}))[1]

    def tearDown(self):
        H.save_ar_creds, H.get_ar_creds = self._save, self._get
        H.delete_ar_creds = self._del
        A.balance_sync, A.whoami_sync = self._balance, self._whoami

    def screen(self):
        cb = CB("apr:creds")
        asyncio.run(H.apr_creds_screen(cb, FSM()))
        return cb

    def test_the_roblox_plugin_leads_here(self):
        """Проверка проводки: экран может быть хорош, а попасть в него
        неоткуда."""
        from handlers import plugins as P
        self.assertIn("apr:creds",
                      callbacks(P._roblox_keyboard({"plugins": {"auto_roblox": {}}})))

    def test_the_previous_supplier_is_still_reachable_for_price_comparison(self):
        """Выбрали AppRoute — но сравнивать закупочную цену не с чем, если
        второй кабинет виден только по памяти.

        Из раздела Robux ns.gifts убран: поставщик выбран, и сравнение цены
        не то, ради чего открывают экран выдачи. Вход переехал в меню
        плагинов — совсем убрать его нельзя, иначе экран остаётся
        достижимым только командой, а про команду продавцу неоткуда узнать.
        """
        from handlers import plugins as P
        self.assertNotIn("ns:creds",
                         callbacks(P._roblox_keyboard({"plugins": {"auto_roblox": {}}})))
        self.assertIn("ns:creds", callbacks(P._plugins_menu_keyboard()))

    def test_reading_the_catalogue_is_offered_only_once_the_key_is_there(self):
        cb = self.screen()
        self.assertNotIn("apr:stock", callbacks(cb.message.kbs[-1]))
        self.saved["api_key"] = KEY
        self.assertIn("apr:stock", callbacks(self.screen().message.kbs[-1]))

    def test_the_key_is_shown_as_a_length_not_a_value(self):
        self.saved["api_key"] = KEY
        cb = self.screen()
        self.assertNotIn(KEY, cb.message.last)
        self.assertIn("символов", cb.message.last)

    def test_encryption_is_promised_only_when_it_is_real(self):
        """Обещать шифрование без `SECRET_KEY` — то же враньё, что «Пак
        поднят · Поднято: 0». На Railway ключ до сих пор не задан, и узнавать
        об этом продавец должен здесь, а не из /version."""
        self.saved["api_key"] = KEY
        real = H.encryption_on
        try:
            H.encryption_on = lambda: True
            self.assertIn("зашифрован", self.screen().message.last)
            H.encryption_on = lambda: False
            said = self.screen().message.last
            self.assertIn("в открытом виде", said)
            self.assertIn("SECRET_KEY", said)
            self.assertNotIn("хранится зашифрованным", said)
        finally:
            H.encryption_on = real

    def test_the_dashboard_is_named_on_the_screen(self):
        self.saved.update({"api_key": KEY, "region": "ru"})
        self.assertIn("approute.ru", self.screen().message.last)

    def test_switching_the_dashboard_is_saved(self):
        self.saved["region"] = "io"
        asyncio.run(H.apr_region(CB("apr:region:ru")))
        self.assertEqual(self.saved["region"], "ru")

    def test_a_typed_key_is_saved_and_the_message_deleted(self):
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        self.assertEqual(self.saved["api_key"], KEY)
        self.assertTrue(msg.deleted)

    def test_and_the_answer_does_not_echo_it(self):
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        self.assertNotIn(KEY, "\n".join(msg.said))

    def test_an_empty_value_changes_nothing(self):
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        asyncio.run(H.apr_key_input(Msg("   "), fsm))
        self.assertNotIn("api_key", self.saved)

    def test_saving_the_key_checks_it_without_a_second_tap(self):
        """«Ключ сохранён» — про наше хранилище, а не про поставщика.

        Раньше вход отвечал только этим, и принял ли ключ поставщик,
        продавец узнавал, лишь нажав отдельную кнопку. Зелёная галочка там,
        где ничего не проверено, — то же самое «Пак поднят · Поднято: 0».
        """
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        self.assertIn("balance", self.asked)
        self.assertIn("принят", "\n".join(msg.said))

    def test_the_login_also_names_the_address_the_supplier_sees(self):
        """У них белый список IP: следующий вопрос после отказа — всегда
        «а с какого адреса нас видно». Отвечаем сразу."""
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        said = "\n".join(msg.said)
        self.assertIn("whoami", self.asked)
        self.assertIn("138.124.113.101", said)

    def test_an_empty_allowlist_is_called_empty_not_a_reason_to_add_us(self):
        """Пустой список означает «пускаем отовсюду». Советовать вписывать
        адрес туда, где проверки нет, — совет о несуществующем."""
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        said = "\n".join(msg.said)
        self.assertIn("пуст", said)
        self.assertNotIn("нет</b> в вашем белом списке", said)

    def test_a_missing_allowlist_entry_is_stated_plainly(self):
        A.whoami_sync = lambda creds: (True, {
            "clientIp": "1.2.3.4", "allowlist": ["9.9.9.9"],
            "allowlistMatches": False})
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        said = "\n".join(msg.said)
        self.assertIn("нет", said)
        self.assertIn("1.2.3.4", said)

    def test_a_refused_key_says_so_instead_of_reporting_it_saved(self):
        A.balance_sync = lambda creds: (False, "ключ не принят")
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        said = "\n".join(msg.said)
        self.assertIn("не сработал", said)
        self.assertNotIn("Ключ принят", said)

    def test_the_verdict_still_does_not_echo_the_key(self):
        fsm = FSM()
        asyncio.run(H.apr_set_key(CB("apr:set"), fsm))
        msg = Msg(KEY)
        asyncio.run(H.apr_key_input(msg, fsm))
        self.assertNotIn(KEY, "\n".join(msg.said))

    def test_deleting_clears_it(self):
        self.saved["api_key"] = KEY
        asyncio.run(H.apr_delete(CB("apr:del")))
        self.assertEqual(self.saved, {})

    def test_the_check_button_only_reads(self):
        self.saved["api_key"] = KEY
        real = A.balance_sync
        A.balance_sync = lambda creds: (True, {"items": [
            {"currency": "USDT", "balance": 42.0, "available": 42.0}]})
        try:
            cb = CB("apr:check")
            asyncio.run(H.apr_check(cb))
        finally:
            A.balance_sync = real
        self.assertIn("42.0", cb.message.last)

    def test_a_refused_check_says_why_and_does_not_claim_success(self):
        self.saved["api_key"] = KEY
        real = A.balance_sync
        A.balance_sync = lambda creds: (False, "ключ не принят — проверьте, "
                                               "что он из того же кабинета")
        try:
            cb = CB("apr:check")
            asyncio.run(H.apr_check(cb))
        finally:
            A.balance_sync = real
        self.assertIn("не принят", cb.message.last)
        self.assertNotIn("Ключ принят", cb.message.last)


class TheCatalogueReportShowsWhatAnOrderWillNeed(unittest.TestCase):
    """Это и есть тот живой ответ, ради которого всё написано: есть ли у
    AppRoute Roblox, под каким `itemId` и почём. Печатается как пришло —
    угадывание имён полей в этом проекте уже стоило дня."""

    def setUp(self):
        self._get = H.get_ar_creds
        H.get_ar_creds = lambda uid: dict(CREDS)
        self._services = A.services_sync

    def tearDown(self):
        H.get_ar_creds = self._get
        A.services_sync = self._services

    def report(self, text="/apr_stock roblox", answer=None):
        A.services_sync = lambda creds: (answer or
                                         (True, TheCatalogueIsReadAsItCame.CATALOG))
        msg = Msg(text)
        asyncio.run(H.apr_stock(msg))
        return "\n".join(msg.said)

    def test_the_service_id_is_shown(self):
        self.assertIn("serviceId=svc-1", self.report())

    def test_the_item_id_of_each_nominal_is_shown(self):
        """Заказ делается по `itemId`, а не по названию номинала."""
        got = self.report()
        self.assertIn("itemId=it-1", got)
        self.assertIn("itemId=it-2", got)

    def test_the_price_is_shown_next_to_the_nominal(self):
        """Ради этого каталог и читается: сравнить закупку с ns.gifts."""
        got = self.report()
        self.assertIn("800 Robux", got)
        self.assertIn("8.4", got)

    def test_a_sold_out_nominal_is_marked_as_such(self):
        got = self.report()
        self.assertIn("⛔", got)

    def test_the_order_fields_are_printed_as_they_came(self):
        got = self.report()
        self.assertIn("nickname", got)
        self.assertIn("region", got)

    def test_a_required_field_is_marked(self):
        self.assertIn("nickname (text)*", self.report())

    def test_the_kind_of_product_is_said_in_russian(self):
        self.assertIn("ваучер", self.report())

    def test_nothing_found_says_so_and_suggests_another_word(self):
        got = self.report("/apr_stock minecraft")
        self.assertIn("ничего не нашлось", got)
        self.assertIn("другое слово", got)

    def test_a_partial_catalogue_is_admitted_not_passed_off_as_whole(self):
        """«Роблокса нет» и «нам прислали не весь список» — разные ответы, и
        второй нельзя выдать за первый."""
        got = self.report(answer=(True, {"items": [], "hasNext": True}))
        self.assertIn("не весь", got)

    def test_a_refusal_is_shown_instead_of_a_catalogue(self):
        got = self.report(answer=(False, "ключу не хватает прав — они "
                                         "включаются в кабинете"))
        self.assertIn("не хватает прав", got)

    def test_a_missing_key_stops_it_before_the_network(self):
        H.get_ar_creds = lambda uid: {}
        called: list = []
        A.services_sync = lambda creds: called.append(1) or (True, {})
        msg = Msg("/apr_stock")
        asyncio.run(H.apr_stock(msg))
        self.assertEqual(called, [])
        self.assertIn("не задан", "\n".join(msg.said))


class TheKeyIsStoredEncrypted(unittest.TestCase):
    """Ключ — право тратить баланс кабинета целиком, ровно как seed-фраза
    TON. В базу он ложится зашифрованным, если задан SECRET_KEY."""

    def setUp(self):
        import shutil
        import tempfile
        self.dir = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DATA_DIR", "SECRET_KEY",
                                                    "DATABASE_URL", "FRAGMENT_KEY")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["SECRET_KEY"] = "тестовый-ключ"
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("FRAGMENT_KEY", None)
        self._rm = shutil.rmtree

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._rm(self.dir, ignore_errors=True)
        import importlib

        import storage
        importlib.reload(storage)

    def load(self):
        import importlib

        import storage
        return importlib.reload(storage)

    def test_the_key_comes_back_the_same(self):
        s = self.load()
        s.save_ar_creds(1, {"api_key": KEY, "region": "ru"})
        got = s.get_ar_creds(1)
        self.assertEqual(got["api_key"], KEY)
        self.assertEqual(got["region"], "ru")

    def test_it_is_not_readable_in_the_file(self):
        s = self.load()
        s.save_ar_creds(1, {"api_key": KEY})
        with open(os.path.join(self.dir, "approute_creds.json"),
                  encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(KEY, raw)

    def test_saving_twice_does_not_wrap_it_twice(self):
        s = self.load()
        s.save_ar_creds(1, {"api_key": KEY})
        s.save_ar_creds(1, {"region": "io"})
        self.assertEqual(s.get_ar_creds(1)["api_key"], KEY)

    def test_deleting_leaves_nothing(self):
        s = self.load()
        s.save_ar_creds(1, {"api_key": KEY})
        s.delete_ar_creds(1)
        self.assertEqual(s.get_ar_creds(1), {})

    def test_nothing_stored_means_nothing_returned(self):
        self.assertEqual(self.load().get_ar_creds(999), {})


class TheCommandsAreWiredIn(unittest.TestCase):
    def test_the_router_is_included_in_main(self):
        import inspect

        import main as M
        self.assertIn("dp.include_router(approute.router)",
                      inspect.getsource(M.main))

    def test_the_commands_do_not_collide_with_the_reply_rules(self):
        """Префикс `ar:` уже занят автоответами. Две кнопки с одним
        callback_data — не ошибка, а тишина: победит роутер, подключённый
        раньше."""
        import inspect
        src = inspect.getsource(H)
        self.assertNotIn('"ar:', src)


class TheOrderProbeNeverBuysAnything(unittest.TestCase):
    """Проба формы тела заказа не должна стоить денег.

    Она бьёт в `POST /orders` — тот самый вызов, который тратит баланс
    кабинета. Пока не выяснено, какую форму тела поставщик принимает, писать
    выдачу нельзя; но и выяснять это покупкой нельзя тем более.
    """

    def rows(self, answer=None, **kw):
        answer = answer or (lambda url: Reply(envelope("NOT_FOUND")))
        made = patch_session(self, answer)
        rows = A.order_shape_probe_sync(CREDS, **kw)
        return rows, made

    def test_without_an_item_it_asks_about_one_that_cannot_exist(self):
        rows, made = self.rows()
        for row in rows:
            self.assertEqual(row["item"], A.PROBE_ITEM_ID)
            self.assertTrue(row["invented_item"])
        for call in made[0].posts:
            self.assertEqual(call["body"]["itemId"], A.PROBE_ITEM_ID)

    def test_every_body_asks_for_a_dry_run(self):
        """`checkOnly` — вторая защита. Обе формы обязаны её нести."""
        rows, made = self.rows()
        self.assertTrue(made[0].posts)
        for call in made[0].posts:
            self.assertIs(call["body"].get("checkOnly"), True)

    def test_a_seller_supplied_item_is_marked_as_such(self):
        """С настоящим товаром защита остаётся одна — и отчёт это скажет."""
        rows, made = self.rows(item_id="ITEM-7")
        for row in rows:
            self.assertEqual(row["item"], "ITEM-7")
            self.assertFalse(row["invented_item"])

    def test_it_goes_through_the_sellers_proxy_like_the_real_calls(self):
        """Проба мимо прокси показала бы адрес, с которого нас не видят."""
        made = patch_session(self, lambda url: Reply(envelope("NOT_FOUND")))
        A.order_shape_probe_sync({"api_key": KEY, "proxy": "http://p:1"})
        self.assertEqual(made[0].proxy, "http://p:1")


class TheTwoBodyShapesGoOutExactlyAsDocumented(unittest.TestCase):
    """SDK и `openapi.yaml` расходятся — поэтому шлём ровно обе, как описаны.

    Придумать третью, «разумную» форму значит снова гадать: так мы потеряли
    день на Fragment, где своя версия запроса проходила поиск и падала на
    покупке.
    """

    def bodies(self):
        made = patch_session(self, lambda url: Reply(envelope("NOT_FOUND")))
        A.order_shape_probe_sync(CREDS)
        return {c["body"].get("ordersType") and "sdk" or "openapi": c["body"]
                for c in made[0].posts}

    def test_the_sdk_shape_carries_orders_type_and_reference_id(self):
        body = self.bodies()["sdk"]
        self.assertEqual(body["ordersType"], "shop")
        self.assertIn("referenceId", body)
        self.assertNotIn("reference", body)
        self.assertNotIn("clientTime", body)

    def test_the_openapi_shape_carries_client_time_and_reference(self):
        body = self.bodies()["openapi"]
        self.assertIn("reference", body)
        self.assertIn("clientTime", body)
        self.assertNotIn("ordersType", body)
        self.assertNotIn("referenceId", body)

    def test_names_go_camel_case_because_we_ride_without_the_sdk(self):
        """`item_id` осталось бы непонятым: переводит их транспорт SDK."""
        for body in self.bodies().values():
            self.assertIn("itemId", body)
            self.assertNotIn("item_id", body)
            self.assertNotIn("reference_id", body)
            self.assertNotIn("client_time", body)

    def test_the_reference_is_invented_before_the_call(self):
        """По ней спрашивают, чем кончилось, если связь оборвалась."""
        made = patch_session(self, lambda url: Reply(envelope("NOT_FOUND")))
        rows = A.order_shape_probe_sync(CREDS)
        refs = [r["reference"] for r in rows]
        self.assertTrue(all(refs))
        self.assertEqual(len(set(refs)), len(refs))
        for call, ref in zip(made[0].posts, refs):
            sent = call["body"].get("referenceId") or call["body"].get("reference")
            self.assertEqual(sent, ref)


class TheReadingOfARefusalIsNotPassedOffAsAFact(unittest.TestCase):
    """Что отказ говорит о форме тела — вывод, и он обязан так и называться.

    Правило проекта: догадка не подаётся как факт. Здесь цена ошибки — выдача,
    написанная под форму, которую поставщик на самом деле не принимает.
    """

    def read(self, code, http=200):
        return A._shape_reading(code, http, "")

    def test_a_validation_error_reads_as_the_shape_being_rejected(self):
        self.assertIn("НЕ приняли", self.read("VALIDATION_ERROR"))

    def test_reaching_the_item_reads_as_the_shape_being_accepted(self):
        for code in ("NOT_FOUND", "OUT_OF_STOCK", "INSUFFICIENT_FUNDS"):
            self.assertIn("приняли", self.read(code))
            self.assertNotIn("НЕ приняли", self.read(code))

    def test_a_rejected_key_says_nothing_about_the_shape(self):
        """Иначе продавец пойдёт чинить тело запроса вместо белого списка."""
        for code in ("UNAUTHORIZED", "FORBIDDEN"):
            said = self.read(code)
            self.assertIn("ключ", said)
            self.assertNotIn("приняли", said)

    def test_an_unseen_code_is_called_unclear_rather_than_guessed(self):
        said = self.read("SOMETHING_NEW")
        self.assertIn("непонятно", said.lower())

    def test_an_envelope_without_a_refusal_is_not_reported_as_agreement(self):
        """Сухой прогон без жалоб — не доказательство, что форму поняли.

        Принять молчание за согласие значит написать выдачу под форму,
        которую поставщик, может быть, просто не разобрал.
        """
        patch_session(self, lambda url: Reply(
            {"status": "ok", "statusCode": 200, "data": None, "traceId": "t"}))
        rows = A.order_shape_probe_sync(CREDS)
        for row in rows:
            self.assertIn("нельзя", row["reading"])
            self.assertNotIn("приняли", row["reading"])

    def test_a_validation_refusal_is_read_from_errors_not_only_the_code(self):
        """Живая проба 18.08: `errorCode` пуст, а причина — в `errors`.

        Первая версия смотрела только на код и отвечала «непонятно» там, где
        сервер прямым текстом перечислил недостающие поля. Именно этот
        список и был ответом на вопрос, ради которого писалась проба.
        """
        patch_session(self, lambda url: Reply({
            "status": "CANCELLED", "statusCode": 3,
            "statusMessage": "Validation error", "errorCode": None,
            "traceId": "t-9", "data": None,
            "errors": [
                {"field": "orders", "code": "INVALID_VALUE",
                 "message": "Field required"},
                {"field": "itemId", "code": "INVALID_VALUE",
                 "message": "Extra inputs are not permitted"},
                {"field": "orders", "code": "INVALID_VALUE",
                 "message": "Field required"},
            ]}, code=422))
        rows = A.order_shape_probe_sync(CREDS)
        for row in rows:
            self.assertIn("НЕ приняли", row["reading"])
            self.assertNotIn("непонятно", row["reading"].lower())
            # Повтор свёрнут, порядок — от корня к вложенным.
            self.assertEqual(row["complaints"],
                             ["orders: Field required",
                              "itemId: Extra inputs are not permitted"])

    def test_the_named_fields_reach_the_report(self):
        """Отчёт без имён полей отвечает «форму не ту» и молчит о том, какая
        нужна, — а сервер это уже сказал."""
        import inspect
        self.assertIn("complaints", inspect.getsource(H._order_probe_report))

    def test_a_refusal_prints_the_trace_id_for_their_support(self):
        patch_session(self, lambda url: Reply(
            {"errorCode": "OUT_OF_STOCK", "statusMessage": "нет",
             "statusCode": 409, "traceId": "trace-77"}))
        rows = A.order_shape_probe_sync(CREDS)
        for row in rows:
            self.assertEqual(row["trace"], "trace-77")
            self.assertEqual(row["fields"].get("statusMessage"), "нет")

    def test_the_key_never_reaches_the_report(self):
        patch_session(self, lambda url: Reply(
            {"errorCode": "VALIDATION_ERROR",
             "statusMessage": f"ключ {KEY} не подошёл", "traceId": "t"}))
        rows = A.order_shape_probe_sync(CREDS)
        self.assertNotIn(KEY, json.dumps(rows, ensure_ascii=False))

if __name__ == "__main__":
    unittest.main()
