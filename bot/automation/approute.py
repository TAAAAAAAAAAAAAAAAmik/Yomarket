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

# Поставщик сам разделил отказы по видам — это то, чего не хватало у
# ns.gifts. Переводим: английский код на экране продавца здесь считается
# отпиской.
_CODES = {
    "VALIDATION_ERROR": "запрос не прошёл проверку",
    "UNAUTHORIZED": "ключ не принят. Проверьте две вещи: тот ли кабинет "
                    "(io/ru) и стоит ли наш IP в белом списке — его "
                    "показывает кнопка «🪪 Наш IP у поставщика»",
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
    401: "ключ не принят — проверьте кабинет (io/ru) и белый список IP",
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


class ARClient:
    """Клиент на одну сессию. Авторизация — один заголовок."""

    def __init__(self, api_key: str, base_url: str = BASE_URL,
                 proxy: str = "", max_retries: int = 2):
        self.api_key = str(api_key or "").strip()
        self.base = (base_url or BASE_URL).rstrip("/")
        self.max_retries = max(0, int(max_retries or 0))
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

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

        # Конверт из SDK: успех решает `code`.
        code = str(body.get("code") or "")
        if code:
            if code in SUCCESS_CODES:
                return body.get("data")
            raise ARError(code, explain_code(code, body.get("message")),
                          r.status_code, trace, errors)

        # Живой конверт, снятый 17.08 пробой `/apr_debug`: полей `code` и
        # `message` в нём нет вовсе, зато есть `errorCode`, `statusCode` и
        # `statusMessage`. Форма разошлась с их же SDK, и разбираем мы её
        # по тому, что видели, а не по тому, что было написано.
        if _looks_like_live_envelope(body):
            problem = str(body.get("errorCode") or "").strip()
            said = str(body.get("statusMessage") or "").strip()
            status_code = body.get("statusCode")
            if _live_envelope_is_ok(body):
                return body.get("data")
            why = explain_code(problem, said) if problem else (
                said or "поставщик отказал, не назвав причины")
            if status_code and str(status_code) not in ("200", "0"):
                why += f" (код {status_code})"
            raise ARError(problem, why, r.status_code, trace, errors)

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
    out: list[dict] = []
    for region, base in BASE_URLS.items():
        for path in ("/whoami", "/accounts"):
            row = {"region": region, "path": path, "http": 0, "json": False,
                   "keys": [], "code": "", "trace": "", "said": "",
                   "excerpt": "", "error": "", "fields": {}, "data": ""}
            try:
                r = requests.get(
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
