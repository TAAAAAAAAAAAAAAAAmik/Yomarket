"""Поставщик ns.gifts — подписанный клиент NS API v2.

Истина по этому API — `docs/ns_api_reference.md`, присланная продавцом
документация. Своих вариантов здесь не сочиняем: с Fragment расхождения с
рабочим клиентом стоили дня, и каждое приходилось опровергать отдельным
прогоном.

Авторизация двухслойная: постоянный `api_secret` (base64, выдаётся
оператором один раз) плюс `token` с TTL два часа, который берётся по логину
и паролю. Каждый запрос подписывается HMAC-SHA256.

Что здесь легко сделать «по-своему» и сломать:

* **Строка запроса собирается как в документации** — `k=v&k2=v2`, без
  URL-кодирования и без сортировки. Подпись считается по этой же строке, и
  любое «улучшение» разойдётся с тем, что подписал сервер.
* **Тело — компактный JSON** (`separators=(",", ":")`): его sha256 входит в
  подпись, и лишний пробел делает её неверной.
* **Каждая подпись принимается один раз** (защита от повтора, 120 секунд).
  Повтор обязан подписываться заново со свежим `X-Timestamp` — иначе 401,
  который легко принять за «ключ неверный».

Секреты наружу не выходят: ни в логи, ни в отчёты диагностики.
Блокирующая — звать через executor.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ns.gifts"
TIMEOUT = 30

# Коды ошибок из документации — по-русски. Английский код на экране продавца
# в этом проекте считается отпиской.
_ERRORS = {
    400: "плохие параметры запроса",
    401: "подпись, время или токен не приняты",
    403: "доступ запрещён — возможно, наш IP не в белом списке у поставщика",
    404: "заказ или услуга не найдены",
    409: "конфликт: заказ уже оплачен или создан повторно",
    428: "нужен код двухфакторной проверки (TOTP)",
    500: "внутренняя ошибка поставщика — писать в их поддержку",
}


def explain_http(code: int) -> str:
    return _ERRORS.get(int(code or 0), f"HTTP {code}")


class NSError(Exception):
    """Отказ поставщика. `code` — HTTP, `why` — по-русски."""

    def __init__(self, code: int, why: str, body: str = ""):
        super().__init__(f"{code}: {why}")
        self.code, self.why, self.body = int(code or 0), why, body[:300]


def _sign(secret: str, method: str, path: str, query: str, body: bytes,
          ts: str, token: str | None) -> str:
    """Подпись запроса — формула из документации, шаг в шаг.

    Порядок частей: МЕТОД, ПУТЬ, СТРОКА ЗАПРОСА, ВРЕМЯ, [ТОКЕН], sha256
    тела. У `/get_token` слота под токен нет вовсе — это не пустая строка, а
    отсутствующая часть, и перепутать эти два случая означает неверную
    подпись на самом первом запросе.
    """
    parts = [method.upper(), path, query, ts]
    if token is not None:
        parts.append(token)
    parts.append(hashlib.sha256(body or b"").hexdigest())
    digest = hmac.new(base64.b64decode(secret),
                      "\n".join(parts).encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _query_of(params: dict | None) -> str:
    """`k=v&k2=v2` — ровно как в документации, без кодирования."""
    return "&".join(f"{k}={v}" for k, v in (params or {}).items())


def _body_of(json_body) -> bytes:
    if json_body is None:
        return b""
    return json.dumps(json_body, separators=(",", ":")).encode()


class NSClient:
    """Клиент на одну сессию. Токен берёт сам и обновляет при 401."""

    def __init__(self, user_id, api_secret: str, login: str, password: str,
                 base_url: str = BASE_URL, proxy: str = ""):
        self.user_id = str(user_id or "").strip()
        self.secret = str(api_secret or "").strip()
        self.login = str(login or "")
        self.password = str(password or "")
        self.base = (base_url or BASE_URL).rstrip("/")
        self.token: str = ""
        self._last_ts = 0
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _fresh_ts(self) -> str:
        """Время для подписи, всегда новое.

        Сервер принимает каждую подпись один раз (защита от повтора, 120
        секунд). Два запроса в одну и ту же секунду с одинаковыми полями
        дают одинаковую подпись — и второй отвергается как повтор. Заметнее
        всего это на повторе после 401: он идёт сразу за первым, и продавец
        получил бы «подпись не принята» там, где с ключом всё в порядке.
        """
        ts = int(time.time())
        if ts <= self._last_ts:
            time.sleep(1)
            ts = int(time.time())
            if ts <= self._last_ts:      # часы не сдвинулись — двигаем сами
                ts = self._last_ts + 1   # ±60 с от сервера это переживёт
        self._last_ts = ts
        return str(ts)

    # -- вход -------------------------------------------------------------
    def get_token(self) -> str:
        body = _body_of({"login": self.login, "password": self.password})
        ts = self._fresh_ts()
        path = "/api/v2/get_token"
        headers = {
            "X-User-Id": self.user_id,
            "X-Timestamp": ts,
            # Токена ещё нет — подпись по загрузочной формуле, без слота.
            "X-Signature": _sign(self.secret, "POST", path, "", body, ts, None),
            "Content-Type": "application/json",
        }
        r = self.session.post(self.base + path, headers=headers, data=body,
                              timeout=TIMEOUT)
        self._raise_for(r)
        self.token = str((r.json() or {}).get("token") or "")
        if not self.token:
            raise NSError(0, "поставщик не вернул токен", r.text)
        return self.token

    # -- запросы ----------------------------------------------------------
    def call(self, method: str, path: str, *, params: dict | None = None,
             json_body=None, _retried: bool = False):
        if not self.token:
            self.get_token()
        query, body = _query_of(params), _body_of(json_body)
        ts = self._fresh_ts()
        headers = {
            "X-User-Id": self.user_id,
            "X-Timestamp": ts,
            "X-Token": self.token,
            "X-Signature": _sign(self.secret, method, path, query, body, ts,
                                 self.token),
            "Content-Type": "application/json",
        }
        url = self.base + path + (f"?{query}" if query else "")
        r = self.session.request(method, url, headers=headers, data=body,
                                 timeout=TIMEOUT)
        if r.status_code == 401 and not _retried:
            # Токен живёт два часа и может истечь между запросами. Повтор
            # обязан подписываться заново: старая подпись уже потрачена,
            # сервер принимает каждую только один раз.
            self.get_token()
            return self.call(method, path, params=params, json_body=json_body,
                             _retried=True)
        self._raise_for(r)
        try:
            return r.json()
        except ValueError:
            raise NSError(r.status_code, "ответ не в формате JSON", r.text)

    def _raise_for(self, r) -> None:
        if r.status_code < 400:
            return
        why = explain_http(r.status_code)
        # Поставщик часто объясняет отказ в теле — это полезнее кода.
        detail = ""
        try:
            data = r.json()
            if isinstance(data, dict):
                detail = str(data.get("detail") or data.get("message")
                             or data.get("error") or "")[:150]
        except Exception:
            detail = (r.text or "")[:150]
        raise NSError(r.status_code, f"{why}{f' — {detail}' if detail else ''}",
                      r.text or "")


# ---------------------------------------------------------------------------
# Готовые вызовы. Блокирующие — звать через executor.
# ---------------------------------------------------------------------------

def _client(creds: dict) -> NSClient:
    return NSClient(creds.get("user_id"), creds.get("api_secret", ""),
                    creds.get("login", ""), creds.get("password", ""),
                    creds.get("base_url") or BASE_URL,
                    creds.get("proxy", ""))


def stock_sync(creds: dict) -> tuple[bool, object]:
    """Каталог поставщика → (успех, данные или причина по-русски)."""
    try:
        return True, _client(creds).call("GET", "/api/v2/stock")
    except NSError as e:
        return False, e.why
    except Exception as e:
        return False, f"не достучались: {str(e)[:120]}"


def balance_sync(creds: dict) -> tuple[bool, object]:
    """Баланс кабинета в USD → (успех, строка баланса или причина)."""
    try:
        data = _client(creds).call("GET", "/api/v2/check_balance")
        return True, str((data or {}).get("balance") or "")
    except NSError as e:
        return False, e.why
    except Exception as e:
        return False, f"не достучались: {str(e)[:120]}"


def find_services(stock: dict, needle: str) -> list[dict]:
    """Услуги, чьё название или категория содержат слово.

    Роблокса в документации нет ни в одном примере — есть ли он в каталоге
    вообще, знает только `/stock`. Поиск отдельной функцией, потому что
    ответ разбирают и экран, и диагностика.
    """
    want = str(needle or "").strip().lower()
    out: list[dict] = []
    for cat in (stock or {}).get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("category_name") or "")
        for svc in cat.get("services") or []:
            if not isinstance(svc, dict):
                continue
            haystack = f"{cat_name} {svc.get('service_name') or ''}".lower()
            if not want or want in haystack:
                out.append({**svc, "category_name": cat_name,
                            "category_id": cat.get("category_id"),
                            "fields": cat.get("fields") or []})
    return out
