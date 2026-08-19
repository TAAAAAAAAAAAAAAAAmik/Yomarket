"""Поставщик AppRoute — клиент публичного API (только чтение).

Истина по этому API — исходники официального SDK и `openapi.yaml`
(`github.com/AppRoute-FZCO/AppRoute-Public-API-SDK`), разобранные в
`docs/approute_api_notes.md`. Как и с Fragment, своих вариантов здесь не
сочиняем: расхождение с рабочим клиентом стоило дня.

Три вещи, которые легко сделать «по-своему» и получить молчаливую поломку:

* **HTTP 200 — не ответ на вопрос «получилось ли».** Всё завёрнуто в
  конверт, и снаружи ответ всегда двухсотый. Клиент, который смотрит на
  статус HTTP, будет бодро докладывать об успехе там, где ничего не
  произошло, — это самая дорогая поломка в этом проекте.
* **Конвертов два, и настоящий — не тот, что в SDK.** SDK описывает
  `{status, code, message, traceId, data}` с успехом по трём кодам (`OK`,
  `ACCEPTED`, `IDEMPOTENCY_REPLAY`). Живой ответ, снятый пробой на настоящем
  ключе 17.08, выглядит иначе:
  `{data, errorCode, errors, status, statusCode, statusMessage, traceId}` —
  ни `code`, ни `message` там нет вовсе, а значимый код лежит в `statusCode`
  **внутри** тела. Одинаково на обоих доменах. Понимаем оба; признак успеха
  во втором **выведен из имён полей**, а не прочитан в документации, поэтому
  сомнение решается в сторону отказа.
* **На проводе camelCase.** В SDK поля пишутся `item_id`, `reference_id`,
  `client_time` — но это удобство Python: его транспорт переводит ключи в
  `itemId`, `referenceId`, `clientTime` перед отправкой. Мы идём без SDK,
  значит camelCase пишем сами. Ответы читаются как пришли, теми же именами,
  что в `openapi.yaml`.
* **`traceId` печатается в каждом отказе.** По нему поставщик находит
  запрос у себя. Отказ без него — повод для переписки на день.

Покупок здесь нет ни одной, и это не забывчивость: пока не видно, есть ли у
поставщика Roblox, под каким `itemId` он лежит и каких полей требует, писать
выдачу значит гадать. За этим следит отдельный тест.

Блокирующая — звать через executor.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

# Кабинета два. Их README говорит брать ключ в «своём» кабинете, но экран
# выдачи ключа в approute.ru предлагает проверять его запросом на
# approute.io — то есть общий адрес не исключён. Мы не гадаем: регион
# переключается кнопкой, а `/apr_debug` спрашивает оба и печатает ответы.
BASE_URLS = {
    "io": "https://approute.io/api/v1",
    "ru": "https://approute.ru/api/v1",
}
BASE_URL = BASE_URLS["io"]
TIMEOUT = 30

# Коды конверта, при которых запрос считается выполненным. Ровно те три, что
# в транспорте SDK: `IDEMPOTENCY_REPLAY` — это «такой reference уже был,
# отдаю прежний результат», то есть успех, а не отказ.
SUCCESS_CODES = ("OK", "ACCEPTED", "IDEMPOTENCY_REPLAY")

# Числовой enum конверта — из документации поставщика (19.08,
# `docs/approute_api_reference.md`). Не путать с HTTP: снаружи ответ почти
# всегда двухсотый, а получилось ли — сказано здесь. Семёрки в enum нет;
# неизвестный код считается отказом.
AR_STATUS_CODES = {
    0: "OK", 1: "ACCEPTED", 2: "IDEMPOTENCY_REPLAY", 3: "VALIDATION_ERROR",
    4: "UNAUTHORIZED", 5: "FORBIDDEN", 6: "NOT_FOUND", 8: "LIMIT_REACHED",
    9: "OUT_OF_STOCK", 10: "INSUFFICIENT_FUNDS", 11: "UPSTREAM_ERROR",
}
# Успех — три кода, а не один. `ACCEPTED` это «заказ принят, код будет
# позже», `IDEMPOTENCY_REPLAY` — «такой заказ уже был, отдаю прежний
# результат». Принять любой из них за отказ значит потерять оплаченный
# заказ или купить второй раз.
AR_SUCCESS_CODES = (0, 1, 2)


def _status_code(body: dict):
    """Числовой код конверта или None, если его нет вовсе.

    `True`/`False` отсекаются нарочно: в Python это целые, и булев `status`
    из чужого ответа притворился бы кодом 0, то есть успехом.
    """
    raw = (body or {}).get("statusCode")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

# Поставщик сам разделил отказы по видам — это то, чего не хватало у
# ns.gifts. Переводим: английский код на экране продавца здесь считается
# отпиской.
_CODES = {
    "VALIDATION_ERROR": "запрос не прошёл проверку",
    # Три причины, а не одна: продавец сказал, что в кабинете выдают ещё и
    # временный ключ на 48 часов, живущий по другим правилам. Назвать одну
    # из трёх значит отправить человека проверять не то.
    "UNAUTHORIZED": "ключ не принят. Причин обычно три: истёк срок "
                    "(временный ключ живёт 48 часов), наш IP не в белом "
                    "списке кабинета, либо ключ из другого кабинета. "
                    "Что именно — покажет «🔎 Что отвечает сервер»",
    "FORBIDDEN": "ключу не хватает прав, либо наш IP не в белом списке "
                 "кабинета — посмотрите «🪪 Наш IP у поставщика»",
    "NOT_FOUND": "товар или заказ не найден",
    "CONFLICT": "конфликт: такой заказ уже создан",
    "LIMIT_REACHED": "упёрлись в лимит кабинета",
    "OUT_OF_STOCK": "нет в наличии",
    "INSUFFICIENT_FUNDS": "не хватает средств на балансе у поставщика",
    "UPSTREAM_ERROR": "сбой у вышестоящего поставщика — не в кабинете и не у нас",
    "INTERNAL_ERROR": "внутренняя ошибка AppRoute — писать в их поддержку",
}

# Если конверта нет вовсе (упал балансировщик, отдалась HTML-страница).
_HTTP = {
    401: "ключ не принят — проверьте срок ключа, белый список IP и кабинет",
    403: "ключу не хватает прав или наш IP не в белом списке кабинета",
    404: "адрес не найден — возможно, не тот base_url",
    429: "слишком часто: поставщик просит подождать",
    500: "внутренняя ошибка AppRoute",
    502: "сбой у вышестоящего поставщика",
    503: "поставщик временно недоступен",
    504: "поставщик не ответил вовремя",
}

# Их же список повторяемых: транспорт SDK повторяет ровно эти.
RETRYABLE = (429, 500, 502, 503, 504)


class ARError(Exception):
    """Отказ поставщика. `why` — по-русски, `trace_id` — для их поддержки."""

    def __init__(self, code: str, why: str, http: int = 0, trace_id: str = "",
                 fields: list | None = None):
        super().__init__(f"{code or http}: {why}")
        self.code = str(code or "")
        self.why = why
        self.http = int(http or 0)
        self.trace_id = str(trace_id or "")
        self.fields = fields or []

    def explain(self) -> str:
        """Причина одной строкой — то, что увидит продавец."""
        out = self.why
        for f in self.fields[:3]:
            if isinstance(f, dict) and f.get("field"):
                out += f"\n• поле {f.get('field')}: {f.get('message') or f.get('code')}"
        if self.trace_id:
            # По нему поставщик находит запрос у себя. Без него разговор с
            # поддержкой начинается с «пришлите время и что вы делали».
            out += f"\nНомер обращения: {self.trace_id}"
        return out


def explain_code(code: str, message: str = "") -> str:
    """Код конверта → причина по-русски. Текст поставщика добавляем, если он
    не пустой: он часто конкретнее кода."""
    why = _CODES.get(str(code or "").upper(), "")
    text = str(message or "").strip()
    if not why:
        why = text or f"поставщик отказал: {code or 'без кода'}"
        text = ""
    return f"{why} — {text}" if text and text.lower() not in why.lower() else why


def base_url_of(creds: dict) -> str:
    """Адрес кабинета. Явный `base_url` побеждает выбор региона."""
    explicit = str((creds or {}).get("base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    region = str((creds or {}).get("region") or "io").strip().lower()
    return BASE_URLS.get(region, BASE_URL)


def _session(proxy: str = "") -> requests.Session:
    """Сессия для запросов к поставщику — одна точка на весь модуль.

    Через неё ходят и рабочие вызовы, и диагностика: иначе проба показывала
    бы адрес, с которого поставщик нас вообще не видит. Заодно это
    единственное место подмены для тестов — с прямым `requests.get` прогон
    уходил в настоящую сеть.
    """
    s = requests.Session()
    url = (proxy or "").strip()
    if url:
        s.proxies.update({"http": url, "https": url})
    return s


class ARClient:
    """Клиент на одну сессию. Авторизация — один заголовок."""

    def __init__(self, api_key: str, base_url: str = BASE_URL,
                 proxy: str = "", max_retries: int = 2):
        self.api_key = str(api_key or "").strip()
        self.base = (base_url or BASE_URL).rstrip("/")
        self.max_retries = max(0, int(max_retries or 0))
        self.session = _session(proxy)

    def call(self, method: str, path: str, *, params: dict | None = None,
             json_body: dict | None = None):
        """Запрос и разбор конверта. Возвращает `data`, иначе бросает ARError."""
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = self.base + path
        for attempt in range(self.max_retries + 1):
            r = self.session.request(method, url, headers=headers,
                                     params=params or None, json=json_body,
                                     timeout=TIMEOUT)
            if r.status_code in RETRYABLE and attempt < self.max_retries:
                # Поставщик сам говорит, сколько ждать. Своя оценка была бы
                # догадкой поверх его же ответа.
                delay = _retry_after(r) or (1.0 * (2 ** attempt))
                time.sleep(min(delay, 10.0))
                continue
            return self._unwrap(r)
        # Сюда попасть нельзя: последняя попытка всегда возвращает разбор.
        raise ARError("", "поставщик не ответил после повторов")   # pragma: no cover

    def _unwrap(self, r):
        """Конверт → данные. Успех решает `code`, а не статус HTTP."""
        try:
            body = r.json()
        except ValueError:
            raise ARError("", f"{_HTTP.get(r.status_code, 'ответ не в формате JSON')} "
                              f"(HTTP {r.status_code})", r.status_code)
        if not isinstance(body, dict):
            raise ARError("", f"неожиданный ответ (HTTP {r.status_code})",
                          r.status_code)
        trace = str(body.get("traceId") or "")
        errors = body.get("errors") or []

        # Решает `statusCode` — числовой код ВНУТРИ тела. Документация
        # поставщика прямо предупреждает, что он не совпадает с HTTP:
        # снаружи почти всегда 200. Раньше здесь стояла догадка, выведенная
        # из имён полей, и она читала как отказ два успеха сразу:
        # `1` ACCEPTED («заказ принят, код будет позже») и `2`
        # IDEMPOTENCY_REPLAY («такой referenceId уже был, отдаю прежний
        # результат»). Второе — это вторая покупка за свои деньги, ровно то,
        # от чего строилась вся защита от обрыва связи.
        status_code = _status_code(body)
        if status_code is not None:
            # Сверх плана: успешный код вместе с жалобами — `errors` или
            # непустым `errorCode` — это ответ, противоречащий сам себе.
            # Таких мы не видели, и противоречие решается в сторону «денег
            # не потратили»: ошибиться в эту сторону дешевле, а выдача всё
            # равно проверит, есть ли код.
            complained = str(body.get("errorCode") or "").strip()
            if status_code in AR_SUCCESS_CODES and (errors or complained):
                said = str(body.get("statusMessage") or "").strip()
                raise ARError(complained or AR_STATUS_CODES.get(status_code, ""),
                              explain_code(complained, said) if complained else
                              "поставщик ответил успехом, но с жалобами по "
                              "полям — считаем это отказом",
                              r.status_code, trace, errors)
            if status_code in AR_SUCCESS_CODES:
                # `data` при успехе бывает пустым: у ACCEPTED внутри `result`
                # ещё ничего нет. Требовать непустоту здесь значит объявить
                # отказом принятый заказ; есть ли код — решает выдача.
                return body.get("data")
            name = AR_STATUS_CODES.get(status_code, "")
            said = str(body.get("statusMessage") or "").strip()
            raise ARError(name or str(status_code),
                          explain_code(name, said) if name else
                          (said or f"поставщик отказал (код {status_code})"),
                          r.status_code, trace, errors)

        # Конверт из SDK. Проверяется ПОСЛЕ живого: SDK местами устарел, и
        # при расхождении верх берёт то, что приходит на самом деле.
        code = str(body.get("code") or "")
        if code:
            if code in SUCCESS_CODES:
                return body.get("data")
            raise ARError(code, explain_code(code, body.get("message")),
                          r.status_code, trace, errors)

        # Ни того конверта, ни другого — значит отвечали не они.
        raise ARError("", _no_envelope(body, r.status_code),
                      r.status_code, trace)


# Поля живого конверта AppRoute — снято пробой на настоящем ключе 17.08.
# Их SDK описывает другой набор (`code`, `message`), и полагаться на него
# одного значит не понимать половину ответов.
LIVE_ENVELOPE_FIELDS = ("statusCode", "statusMessage", "errorCode")


def _looks_like_live_envelope(body: dict) -> bool:
    return any(k in body for k in LIVE_ENVELOPE_FIELDS)


def _live_envelope_is_ok(body: dict) -> bool:
    """Успех ли это — по тому конверту, который приходит на самом деле.

    Признак успеха здесь **выведен из имён полей, а не прочитан в
    документации**: `errorCode` пуст, `status`/`statusCode` не возражают, и
    `data` действительно есть. Сомнение решается в сторону отказа — принять
    отказ за успех значит отчитаться о выдаче, которой не было, а это самая
    дорогая поломка в этом проекте. Обратная ошибка дешевле: продавец увидит
    причину и `traceId`.
    """
    if str(body.get("errorCode") or "").strip():
        return False
    if body.get("errors"):
        return False
    status = body.get("status")
    if isinstance(status, bool) and not status:
        return False
    if isinstance(status, str) and status.strip().lower() in (
            "error", "fail", "failed", "false", "ошибка"):
        return False
    status_code = body.get("statusCode")
    if status_code is not None and str(status_code).strip() not in ("200", "201", "0", ""):
        return False
    # Пустой `data` при отсутствии жалоб — не успех, а «непонятно»: у чтения
    # каталога и баланса ответ всегда содержателен.
    return body.get("data") is not None


def _no_envelope(body: dict, http: int) -> str:
    """Ответ без поля `code` — что о нём можно сказать честно.

    Догадка не подаётся как факт: мы не знаем, отказ это или успех в другой
    форме, и так и пишем. Зато перечисляем, что в теле было, — по этому
    поставщик и мы поймём, чем его ответ отличается от описанного в SDK.
    """
    known = _HTTP.get(http)
    if known:
        return known
    said = str(body.get("message") or body.get("error") or
               body.get("detail") or "").strip()
    keys = ", ".join(sorted(str(k) for k in body)[:12]) or "пусто"
    out = (f"поставщик ответил HTTP {http}, но поля <code>code</code> в теле "
           f"нет — а по нему и определяется, получилось ли.")
    if said:
        out += f"\nОн написал: {said}"
    out += (f"\nЧто было в ответе: {keys}"
            f"\nЭто не тот конверт, что описан в их SDK. Покажите этот "
            f"отчёт поставщику или посмотрите <code>/apr_debug</code>.")
    return out


def _retry_after(r) -> float:
    try:
        return float((r.headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Готовые вызовы. Блокирующие — звать через executor.
# ---------------------------------------------------------------------------

def proxy_problem(creds: dict) -> str:
    """Что мешает пользоваться заданным прокси. Пусто — ничего.

    Разбор общий с Fragment: там socks5 без PySocks уже показывал продавцу
    «Missing dependencies for SOCKS support» вместо ответа, и второй раз
    писать то же самое незачем.
    """
    from automation.fragment import proxy_problem as check
    return check(str((creds or {}).get("proxy") or ""))


def proxy_label(creds: dict) -> str:
    """Прокси для показа: только хост и порт, без логина и пароля."""
    from automation.fragment import proxy_label as label
    return label(str((creds or {}).get("proxy") or ""))


def outbound_ip(creds: dict) -> str:
    """С какого адреса нас видит интернет — с прокси или без него.

    Это тот адрес, который надо вписать в белый список кабинета. Спрашивается
    у стороннего сервиса, а не у AppRoute: `/whoami` доступен только когда
    ключ уже принят, а узнать адрес нужно как раз до этого.
    """
    from automation.fragment import outbound_ip as ip
    return ip(str((creds or {}).get("proxy") or ""))


def proxy_check_sync(creds: dict, times: int = 3) -> dict:
    """Годится ли этот прокси для белого списка. Только чтение.

    Белому списку нужен **постоянный** адрес. Ротационные прокси меняют его
    на каждый запрос, и вписывать в кабинет нечего. Проверяется это
    единственным честным способом: спросить свой адрес несколько раз подряд
    и посмотреть, один ли он.

    Заодно сверяется адрес без прокси. Если они совпали — прокси не
    применяется на самом деле (не тот формат строки, отвергнутая
    авторизация, тихий обход), и тогда «прокси задан» ничего не значит.

    Ответ — dict, а не готовый текст: разбирать собственную прозу вместо
    структурных данных в этом проекте уже приводило к ошибкам.
    """
    from automation.fragment import outbound_ip as ip

    proxy = str((creds or {}).get("proxy") or "").strip()
    out = {"proxy": bool(proxy), "seen": [], "stable": False, "ip": "",
           "direct": "", "same_as_direct": False, "problem": proxy_problem(creds)}
    if not proxy:
        out["direct"] = ip("")
        out["ip"] = out["direct"]
        out["seen"] = [out["direct"]]
        out["stable"] = True
        return out

    seen = []
    for _ in range(max(1, int(times or 1))):
        seen.append(ip(proxy))
    out["seen"] = seen
    good = [a for a in seen if a and a != "адрес не узнать"]
    out["ip"] = good[0] if good else ""
    out["stable"] = bool(good) and len(set(good)) == 1 and len(good) == len(seen)
    out["direct"] = ip("")
    out["same_as_direct"] = bool(out["ip"]) and out["ip"] == out["direct"]
    return out


def _client(creds: dict) -> ARClient:
    return ARClient(str((creds or {}).get("api_key") or ""),
                    base_url_of(creds), str((creds or {}).get("proxy") or ""))


def _guarded(fn) -> tuple[bool, object]:
    """(успех, данные) или (False, причина по-русски). Наружу не летит ничего
    английского и ничего секретного."""
    try:
        return True, fn()
    except ARError as e:
        return False, e.explain()
    except requests.Timeout:
        return False, "поставщик не ответил вовремя"
    except requests.RequestException as e:
        # В текст исключения requests кладёт URL, но не заголовки: ключ туда
        # не попадает. Обрезаем на всякий случай.
        return False, f"не достучались: {str(e)[:120]}"
    except Exception as e:                              # pragma: no cover
        return False, f"не достучались: {str(e)[:120]}"


def balance_sync(creds: dict) -> tuple[bool, object]:
    """Баланс кабинета → (успех, список счетов или причина).

    Он же проверка ключа: чтение, денег не тратит.
    """
    return _guarded(lambda: (_client(creds).call("GET", "/accounts") or {}))


def services_sync(creds: dict) -> tuple[bool, object]:
    """Каталог поставщика → (успех, ответ `/services` или причина).

    С него начинается всё остальное: есть ли у AppRoute Roblox, под каким
    `itemId` он лежит, почём и каких полей требует заказ — знает только
    живой ответ. В SDK и OpenAPI списка товаров нет.
    """
    return _guarded(lambda: (_client(creds).call("GET", "/services") or {}))


def whoami_sync(creds: dict) -> tuple[bool, object]:
    """`GET /whoami` → (успех, что ответил поставщик).

    Этого вызова нет ни в SDK, ни в `openapi.yaml` — он найден на экране
    выдачи ключа в кабинете, где им предлагают проверить, **какой IP видит
    поставщик**. У AppRoute белый список адресов, а бот живёт на Railway, где
    адрес меняется при каждом деплое: это первое, что надо проверять, когда
    ключ «не сработал».
    """
    return _guarded(lambda: (_client(creds).call("GET", "/whoami") or {}))


REFERENCE_MAX = 40


def cut_reference(reference: str) -> str:
    """Ссылка в том виде, в каком её увидит поставщик.

    Требование схемы — 1..40 символов. Обрезать надо **одинаково** при
    покупке и при поиске: разойдутся на один символ — заказ не найдётся, и
    после обрыва связи мы решим, что покупки не было. Поэтому обрезание
    живёт в одной функции, а не в двух местах по памяти.
    """
    return str(reference or "")[:REFERENCE_MAX]


def live_order_body(denomination_id: str, quantity: int = 1,
                    reference: str = "") -> dict:
    """Тело заказа в той форме, которую поставщик принимает на самом деле.

    Выяснена сухим прогоном 18.08: ни SDK (`itemId` наверху), ни
    `openapi.yaml` (`clientTime` + `reference`) не угадали. Сервер требует
    `ordersType` и позиции **списком**, а внутри позиции — `denominationId`,
    то есть `id` номинала из `/services`, а не услуги:

        {"ordersType": "shop",
         "orders": [{"denominationId": "…", "quantity": 1}]}

    **Ссылка кладётся наверх, а не внутрь позиции.** Это исправление, и оно
    денежное. Официальная модель запроса (`OrderCreateRequest` в SDK) выглядит
    так:

        orders_type, reference_id, reference, check_only, orders[]

    а внутри позиции (`OrderItemInput`) полей ровно шесть — `denomination_id`,
    `item_id`, `quantity`, `fields`, `amount_currency_code`, `is_long_order`, —
    и `reference` среди них нет. Мы клали его туда, и на этом держалась вся
    защита от двойной покупки: повтор после обрыва связи безопасен ровно
    потому, что поставщик узнаёт ссылку и отвечает `IDEMPOTENCY_REPLAY`.
    Ссылка не на своём месте — значит он её не видел, значит повтор был бы
    **второй покупкой**, а не повтором.

    **Имя поля — `referenceId`.** Прежняя запись «берётся `reference`,
    потому что оно возвращается в строке заказа» перепутала запрос с
    ответом: `reference` есть только в *ответе* `GET /orders`, как эхо
    нашего значения, а в схеме запроса такого поля нет вовсе. Поставщик
    нашего `reference` не видел — значит идемпотентности не было, и повтор
    после обрыва связи был бы **второй покупкой**, а не повтором.

    Что схема примет на самом деле, проверяется `/apr_order_probe`: он
    спрашивает поставщика тремя телами с заведомо несуществующим номиналом —
    денег не тратит ни одно.
    """
    body: dict = {
        "ordersType": "shop",
        "orders": [{"denominationId": str(denomination_id or ""),
                    "quantity": int(quantity or 1)}],
    }
    if reference:
        # 1..40 символов, уникален в пределах кабинета. Повтор с тем же
        # значением возвращает первый результат и statusCode=2.
        body["referenceId"] = cut_reference(reference)
    return body


def order_sync(creds: dict, denomination_id: str, quantity: int = 1,
               reference: str = "") -> tuple[bool, object]:
    """Купить номинал → (успех, `data` поставщика или причина по-русски).

    **Единственное место в проекте, которое тратит чужие деньги.** Отсюда
    два правила, оба написаны прошлыми потерями:

    * `reference` придумывается вызывающим ДО обращения сюда и при повторе
      обязан быть тем же. Тогда обрыв связи лечится повтором: на ту же
      ссылку поставщик отвечает `IDEMPOTENCY_REPLAY` — «такой заказ уже
      был, отдаю прежний результат», то есть успехом. Принять этот ответ за
      отказ значит купить второй раз.
    * **Сухого прогона здесь нет и быть не может.** `checkOnly` разрешён
      только при `ordersType=dtu`, а Robux — `voucher`, то есть shop. Что
      сервер сделает с этим полем в теле магазинного заказа, не сказано
      нигде, и оба исхода плохи: либо отказ по схеме на каждой покупке,
      либо поле игнорируется — и тогда «сухой прогон» и есть покупка, то
      есть мы покупаем дважды подряд. Замена — `item_sync` перед вызовом.

    Успех здесь означает ровно «поставщик не отказал». Есть ли в ответе
    код — решает вызывающий: пустой `vouchers` при HTTP 200 это отказ, а не
    выдача.
    """
    body = live_order_body(denomination_id, quantity, reference)
    return _guarded(lambda: _client(creds).call("POST", "/orders",
                                                json_body=body))


def item_sync(creds: dict, service_id: str, item_id: str) -> tuple[bool, object]:
    """Свежие цена и остаток одного номинала — замена сухому прогону.

    Прогон для магазина не разрешён, а покупать вслепую нельзя: остаток
    мог кончиться с прошлого чтения каталога, а цена — измениться. Этот
    запрос отвечает на оба вопроса и стоит дёшево: 120 в минуту против 2 у
    полного `/services`.

    Ответ совпадает с элементом `items[]` каталога: `id`, `price`,
    `currency`, `inStock`, `isLongOrder`, `minQtyToLongOrder`.
    """
    return _guarded(lambda: _client(creds).call(
        "GET", f"/services/{str(service_id or '')}/items/{str(item_id or '')}")
        or {})


def codes_of(data) -> list[str]:
    """Коды из ответа — тем же чтением, что и у выдачи.

    Ответ сухого прогона, в котором лежат коды, сам по себе доказывает, что
    прогон не сухой: коды выдаются только за деньги.
    """
    from automation.robux import codes_from_result
    return codes_from_result(data)


def order_by_reference_sync(creds: dict, reference: str) -> tuple[bool, object]:
    """Чем кончился заказ с этой ссылкой — на случай обрыва связи.

    Ссылку мы придумали до вызова, поэтому спросить о судьбе покупки можно
    и тогда, когда ответа на неё не пришло вовсе. Без этого единственным
    выходом был бы повтор вслепую.

    **`unhide=true` обязателен.** Без него коды приходят замазанными —
    `****9012`, — и отправка такого «кода» покупателю была бы отчётом о
    выдаче, которой не было. Фильтр при этом тоже обязателен: `unhide` без
    `referenceId` отвергается с HTTP 422.

    У запроса есть побочное действие: поставщик помечает коды полученными и
    запоминает время первой выдачи. Поэтому звать его «просто посмотреть»
    из диагностики нельзя — только на пути настоящей выдачи.
    """
    return _guarded(lambda: _client(creds).call(
        "GET", "/orders",
        params={"referenceId": cut_reference(reference), "unhide": "true"}))


TERMINAL_STATUSES = ("SUCCESS", "PARTIALLY_COMPLETED", "CANCELLED")


def _facts(data, ok: bool, why: str = "") -> dict:
    """Общий разбор ответа про заказ — фактами, а не прозой.

    Разбирать собственный текст вместо структурных данных в этом проекте
    уже приводило к ошибкам, поэтому наружу отсюда идёт dict.
    """
    from automation.robux import codes_from_result
    out = {"ok": bool(ok), "why": str(why or ""), "status": "",
           "order_id": "", "codes": [], "trace_id": ""}
    if isinstance(data, dict):
        out["status"] = str(data.get("status") or "").upper()
        out["order_id"] = str(data.get("orderId") or data.get("id") or "")
        out["codes"] = codes_from_result(data)
    return out


def order_place_sync(creds: dict, denomination_id: str, quantity: int = 1,
                     reference: str = "") -> dict:
    """Купить. Возвращает факты, а не текст.

    `{"ok", "why", "status", "order_id", "codes", "trace_id"}`.

    Пустые `codes` при `ok` — не поломка: покупка законно отвечает
    `IN_PROGRESS` (заказ принят, код будет позже), и тогда его надо
    дождаться опросом, а не объявлять выдачу несостоявшейся.
    """
    ok, data = order_sync(creds, denomination_id, quantity, reference)
    return _facts(data if ok else None, ok, "" if ok else str(data))


def order_codes_sync(creds: dict, reference: str) -> dict:
    """Чем кончился заказ с этой ссылкой — и коды, если они уже есть.

    `{"ok", "why", "found", "status", "codes"}`. Ходит с `unhide=true`,
    иначе коды приходят замазанными (см. `order_by_reference_sync`).
    """
    ok, data = order_by_reference_sync(creds, reference)
    out = _facts(data if ok else None, ok, "" if ok else str(data))
    rows = []
    if isinstance(data, dict):
        page = data.get("page")
        rows = (page.get("items") if isinstance(page, dict) else None) \
            or data.get("items") or []
    out["found"] = bool(rows)
    if rows and not out["status"]:
        first = rows[0] if isinstance(rows[0], dict) else {}
        out["status"] = str(first.get("status") or "").upper()
    return out


def probe_sync(creds: dict) -> list[dict]:
    """Сырые ответы обоих кабинетов — факты, а не наша их трактовка.

    17.08 «Проверить ключ» вернуло HTTP 200 с `traceId`, но **без поля
    `code`**, то есть ответ не той формы, что описана в SDK. Гадать, что это
    было, здесь не принято: проба ходит по обоим адресам, печатает статус,
    ключи тела и его начало — и тогда видно, что происходит на самом деле.

    Только чтение. Ключ в отчёт не попадает: если поставщик вернёт его сам,
    он вырезается.
    """
    key = str((creds or {}).get("api_key") or "")
    # Проба обязана идти тем же путём, что и рабочие вызовы. Первая версия
    # ходила напрямую через `requests.get`, а покупки и каталог — через
    # прокси продавца: тогда отчёт показывал бы адрес, с которого поставщик
    # нас вообще не видит. Диагностика, врущая про свой же маршрут, хуже её
    # отсутствия.
    session = _session(str((creds or {}).get("proxy") or ""))
    out: list[dict] = []
    for region, base in BASE_URLS.items():
        for path in ("/whoami", "/accounts"):
            row = {"region": region, "path": path, "http": 0, "json": False,
                   "keys": [], "code": "", "trace": "", "said": "",
                   "excerpt": "", "error": "", "fields": {}, "data": ""}
            try:
                r = session.get(
                    base + path,
                    headers={"X-API-Key": key, "Accept": "application/json"},
                    timeout=TIMEOUT)
                row["http"] = r.status_code
                try:
                    body = r.json()
                    row["json"] = True
                    if isinstance(body, dict):
                        row["keys"] = sorted(str(k) for k in body)[:12]
                        row["code"] = str(body.get("code") or "")
                        row["trace"] = str(body.get("traceId") or "")
                        row["said"] = str(body.get("message") or
                                          body.get("error") or
                                          body.get("detail") or "")[:200]
                        # Значения, а не только имена полей. Первая проба
                        # напечатала список ключей — и `statusMessage`, то
                        # самое место, где сервер словами говорит, что не
                        # так, осталась непрочитанной. Перечень имён без
                        # значений отвечает «форма не та» и молчит о причине.
                        for name in ("status", "statusCode", "statusMessage",
                                     "errorCode", "message", "error"):
                            if name in body and body.get(name) not in (None, ""):
                                row["fields"][name] = _redact(
                                    str(body.get(name)), key)[:200]
                        if body.get("errors"):
                            row["fields"]["errors"] = _redact(
                                str(body.get("errors")), key)[:200]
                        row["data"] = _describe_data(body.get("data"), key)
                except ValueError:
                    row["excerpt"] = _redact(r.text, key)[:200]
            except requests.RequestException as e:
                row["error"] = _redact(str(e), key)[:150]
            out.append(row)
    return out


# Две формы тела `POST /orders`. SDK (`orders.create`) собирает одну, схема
# `PurchaseRequest` в `openapi.yaml` требует другую и лишние поля запрещает
# (`additionalProperties: false`). Обе описаны в `docs/approute_api_notes.md`;
# какую поставщик принимает на самом деле, документация не говорит, а ошибка
# здесь стоит оплаченного заказа, ушедшего в никуда.
# Формы, которые проба спрашивает у сервера. Плоские тела из SDK и из
# `openapi.yaml` здесь больше не нужны: 18.08 живой прогон отверг обе
# («orders: Field required», «itemId: Extra inputs are not permitted»), это
# записано в `docs/approute_api_notes.md`. Осталось выяснить единственное
# невыясненное — **куда кладётся ссылка**, а от неё зависит, будет ли повтор
# повтором или второй покупкой.
ORDER_SHAPES = ("reference-сверху", "referenceId-сверху", "reference-внутри")

# Товар, которого заведомо нет. Проба спрашивает про него нарочно: отказ
# «такого товара нет» доказывает, что форму тела разобрали, а отказ про
# проверку запроса — что не разобрали. Купить при этом нечего ни в одном
# случае.
PROBE_ITEM_ID = "approute-probe-no-such-item"

# Отказы, по которым видно, что тело **разобрали**: поставщик дошёл до
# товара и правил кабинета, то есть спор SDK против схемы решён в пользу
# этой формы.
_SHAPE_ACCEPTED = ("NOT_FOUND", "OUT_OF_STOCK", "CONFLICT",
                   "INSUFFICIENT_FUNDS", "LIMIT_REACHED", "UPSTREAM_ERROR")
# Отказ, по которому видно, что тело **не разобрали**.
_SHAPE_REJECTED = ("VALIDATION_ERROR",)
# Ключ не принят — про форму тела это не говорит ничего.
_SHAPE_UNKNOWN = ("UNAUTHORIZED", "FORBIDDEN")


def order_body(shape: str, item_id: str, reference: str, quantity: int = 1,
               service_id: str = "", check_only: bool = True) -> dict:
    """Тело `POST /orders` с ссылкой в одном из трёх мест. camelCase.

    Форма самого тела больше не спорная: `ordersType` + позиции списком с
    `denominationId` — это подтвердил и живой прогон 18.08, и официальная
    модель `OrderItemInput`. Спорным осталось место ссылки, и оно денежное:
    на ней держится защита от двойной покупки. Не там — поставщик её не
    увидит, и повтор после обрыва связи станет второй покупкой.

    По модели `OrderCreateRequest` ссылка лежит **наверху** и зовётся
    `reference` либо `referenceId`; внутри позиции такого поля нет вовсе.
    Проба спрашивает все три варианта, включая наш прежний (внутри), — чтобы
    ответ дал сервер, а не мы.

    **Денег не тратит:** `checkOnly` плюс заведомо несуществующий номинал.
    """
    ref = str(reference or "")
    position: dict = {"denominationId": str(item_id or ""),
                      "quantity": int(quantity or 1)}
    body: dict = {"ordersType": "shop", "orders": [position]}
    if str(shape) == "referenceId-сверху":
        body["referenceId"] = ref
    elif str(shape) == "reference-внутри":
        position["reference"] = ref
    else:
        body["reference"] = ref
    if service_id:
        position["serviceId"] = str(service_id)
    if check_only:
        body["checkOnly"] = True
    return body


def _field_complaints(body: dict) -> list[str]:
    """Жалобы поставщика по конкретным полям — «поле: что с ним не так».

    Ради этого проба и писалась. На отказ по схеме AppRoute отвечает списком
    `errors` вида `{field, code, message}`, и в нём прямым текстом сказано,
    чего не хватает (`Field required`) и что лишнее (`Extra inputs are not
    permitted`). Повторы он присылает по нескольку раз — сворачиваем,
    сохраняя порядок: он идёт от корня к вложенным полям.
    """
    out: list[str] = []
    for item in (body.get("errors") or []):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        said = str(item.get("message") or item.get("code") or "").strip()
        if not field and not said:
            continue
        line = f"{field}: {said}" if field and said else (field or said)
        if line not in out:
            out.append(line)
    return out


def _shape_reading(code: str, http: int, said: str) -> str:
    """Что этот отказ говорит о форме тела — **чтение, а не факт**.

    Решается по коду отказа, а не по словам поставщика: разбор чужой прозы
    здесь уже приводил к неверным выводам. Чего код не покрывает — так и
    называется непонятным, а не досочиняется.
    """
    up = str(code or "").upper()
    if up in _SHAPE_REJECTED:
        return "форму тела, похоже, НЕ приняли — отказ про проверку запроса"
    if up in _SHAPE_ACCEPTED:
        return ("форму тела, похоже, приняли: поставщик дошёл до товара "
                "и отказал уже по делу")
    if up in _SHAPE_UNKNOWN:
        return ("про форму тела не говорит ничего: не принят ключ. "
                "Сначала ключ и белый список IP, потом эта проба")
    if not up and http and int(http) >= 500:
        return "сбой на их стороне — о форме тела не говорит ничего"
    return "непонятно: такого кода отказа мы ещё не видели"


def order_shape_probe_sync(creds: dict, item_id: str = "",
                           service_id: str = "",
                           quantity: int = 1) -> list[dict]:
    """Какую форму тела `POST /orders` принимает поставщик — обе пробуются.

    Это тот самый невыясненный пункт, из-за которого автовыдача не написана:
    SDK и `openapi.yaml` описывают тело по-разному, и **до первой покупки**
    надо знать, кто из них прав. Гадать здесь нельзя — ровно так мы потеряли
    день на Fragment.

    **Денег проба не тратит.** Две защиты сразу: `checkOnly: true` (сухой
    прогон по схеме) и заведомо несуществующий товар, если продавец не
    назвал свой. Настоящий `itemId` можно передать, но тогда защита остаётся
    одна — та, которую мы ещё не видели в работе.

    Возвращает по строке на форму: что отправили, что ответили и как это
    читается. Ключ в отчёт не попадает.
    """
    key = str((creds or {}).get("api_key") or "")
    item = str(item_id or "").strip() or PROBE_ITEM_ID
    invented = not str(item_id or "").strip()
    session = _session(str((creds or {}).get("proxy") or ""))
    base = base_url_of(creds)
    out: list[dict] = []
    for shape in ORDER_SHAPES:
        # Ссылка придумывается ДО вызова: по ней потом спрашивают, чем
        # кончилось, если связь оборвётся.
        reference = f"probe-{shape}-{int(time.time())}"
        body = order_body(shape, item, reference, quantity, service_id)
        row = {"shape": shape, "sent": sorted(body), "reference": reference,
               "item": item, "invented_item": invented, "http": 0,
               "envelope": "нет", "code": "", "trace": "", "fields": {},
               "data": "", "error": "", "reading": "", "complaints": []}
        try:
            r = session.post(
                base + "/orders",
                headers={"X-API-Key": key, "Accept": "application/json",
                         "Content-Type": "application/json"},
                json=body, timeout=TIMEOUT)
            row["http"] = r.status_code
            try:
                got = r.json()
            except ValueError:
                row["error"] = _redact(r.text, key)[:200]
                row["reading"] = "ответ не в формате JSON — отвечали не они"
                out.append(row)
                continue
            if not isinstance(got, dict):
                row["reading"] = "ответ не объект — о форме тела не говорит ничего"
                out.append(row)
                continue
            row["trace"] = str(got.get("traceId") or "")
            # Значения, а не имена полей: перечень имён отвечает «форма не
            # та» и молчит о причине.
            for name in ("status", "statusCode", "statusMessage", "errorCode",
                         "code", "message", "error"):
                if got.get(name) not in (None, ""):
                    row["fields"][name] = _redact(str(got.get(name)), key)[:200]
            if got.get("errors"):
                row["fields"]["errors"] = _redact(str(got.get("errors")), key)[:300]
            row["data"] = _describe_data(got.get("data"), key)
            sdk_code = str(got.get("code") or "")
            if sdk_code:
                row["envelope"] = "SDK"
                row["code"] = sdk_code
            elif _looks_like_live_envelope(got):
                row["envelope"] = "живой"
                row["code"] = str(got.get("errorCode") or "")
            # Разбор жалоб по полям идёт первым. Живая проба 18.08 вернула
            # `errorCode: null`, а причину сложила в `errors` — и чтение,
            # смотревшее только на код, честно сказало «непонятно» там, где
            # сервер прямым текстом перечислил недостающие поля. Здесь
            # лежит ответ на весь вопрос: какие поля он ждёт на самом деле.
            row["complaints"] = _field_complaints(got)
            if row["complaints"]:
                row["reading"] = ("форму тела НЕ приняли — сервер назвал поля: "
                                  + "; ".join(row["complaints"][:6]))
            elif row["envelope"] != "нет" and not row["code"]:
                # Ни кода отказа, ни жалоб — а заказ мы просили сухим
                # прогоном. Успех это или нет, по именам полей не решить.
                row["reading"] = ("отказа не назвали. Считать это согласием "
                                  "нельзя: сухой прогон мог и пройти, и быть "
                                  "не понят — смотрите data и statusCode")
            else:
                row["reading"] = _shape_reading(
                    row["code"], r.status_code,
                    str(row["fields"].get("statusMessage") or ""))
        except requests.RequestException as e:
            row["error"] = _redact(str(e), key)[:150]
            row["reading"] = "не достучались — о форме тела не говорит ничего"
        out.append(row)
    return out

def _describe_data(data, key: str) -> str:
    """Что лежит в `data` — коротко и без секретов.

    «Есть ли данные» и «что именно» — разные ответы: пустой `data` при
    отсутствии жалоб означает не успех, а «непонятно».
    """
    if data is None:
        return "null (пусто)"
    if isinstance(data, dict):
        if not data:
            return "{} (пустой объект)"
        return "поля: " + _redact(", ".join(sorted(str(k) for k in data)), key)[:200]
    if isinstance(data, list):
        return f"список из {len(data)}"
    return _redact(str(data), key)[:200]


def _redact(text: str, key: str) -> str:
    """Ключ не показывается даже в сыром отчёте — это право тратить баланс."""
    text = str(text or "")
    return text.replace(key, "…ключ вырезан…") if key else text


def balance_lines(data) -> list[str]:
    """Счета в вид «USDT: 12.5 (доступно 10.0)». Валюта только USDT."""
    out: list[str] = []
    for acc in (data or {}).get("items") or []:
        if not isinstance(acc, dict):
            continue
        cur = acc.get("currency") or "?"
        line = f"{cur}: {acc.get('balance')}"
        if acc.get("available") is not None and acc.get("available") != acc.get("balance"):
            line += f" (доступно {acc.get('available')})"
        limit = acc.get("overdraftLimit")
        if limit:
            line += f", овердрафт {limit}"
        out.append(line)
    return out


def find_products(catalog, needle: str) -> list[dict]:
    """Товары, у которых слово встречается в названии, категории или стране.

    Ищем по товару целиком, а не по номиналу: у AppRoute Roblox — это один
    `Product` с несколькими `items` (номиналами), и разорвать их значит
    потерять то самое, ради чего каталог и читается — цену за номинал.
    """
    want = str(needle or "").strip().lower()
    out: list[dict] = []
    for product in (catalog or {}).get("items") or []:
        if not isinstance(product, dict):
            continue
        haystack = " ".join(str(product.get(k) or "") for k in (
            "name", "categoryName", "subcategoryName", "countryCode", "id")).lower()
        if not want or want in haystack:
            out.append(product)
    return out


def truncated(catalog) -> bool:
    """Сказал ли поставщик, что список не весь.

    Постраничного чтения `/services` нет ни в SDK, ни в OpenAPI, поэтому
    дочитать нечем — но промолчать об этом нельзя: «Роблокса нет» и «нам
    прислали не весь список» это разные ответы.
    """
    return bool((catalog or {}).get("hasNext"))
