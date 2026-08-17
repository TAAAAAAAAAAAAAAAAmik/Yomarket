"""Поставщик AppRoute — клиент публичного API (только чтение).

Истина по этому API — исходники официального SDK и `openapi.yaml`
(`github.com/AppRoute-FZCO/AppRoute-Public-API-SDK`), разобранные в
`docs/approute_api_notes.md`. Как и с Fragment, своих вариантов здесь не
сочиняем: расхождение с рабочим клиентом стоило дня.

Три вещи, которые легко сделать «по-своему» и получить молчаливую поломку:

* **HTTP 200 — не ответ на вопрос «получилось ли».** У AppRoute всё
  завёрнуто в конверт `{status, code, message, traceId, data}`, и успехом
  считаются только коды `OK`, `ACCEPTED`, `IDEMPOTENCY_REPLAY`. «Нет в
  наличии» и «не хватает денег» приезжают с кодом внутри конверта. Клиент,
  который смотрит на статус HTTP, будет бодро докладывать об успехе там,
  где ничего не произошло, — это самая дорогая поломка в этом проекте.
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

# Кабинеты разные, и ключ от одного в другом не работает. Продавец выбирает
# регион кнопкой — иначе «ключ не принят» означает что угодно.
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
    "UNAUTHORIZED": "ключ не принят — проверьте, что он из того же кабинета "
                    "(approute.io и approute.ru — разные)",
    "FORBIDDEN": "ключу не хватает прав — они включаются в кабинете",
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
    401: "ключ не принят",
    403: "ключу не хватает прав",
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
        code = str(body.get("code") or "")
        trace = str(body.get("traceId") or "")
        if code in SUCCESS_CODES:
            return body.get("data")
        if not code:
            # Конверта нет — значит отвечали не мы и не их приложение.
            raise ARError("", _HTTP.get(r.status_code, f"HTTP {r.status_code}"),
                          r.status_code, trace)
        raise ARError(code, explain_code(code, body.get("message")),
                      r.status_code, trace, body.get("errors") or [])


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
