"""Оплата подписки переводом внутри Bybit — чтение поступлений.

Продавец переводит USDT на UID владельца обычным внутренним переводом
Bybit: мгновенно и без комиссии. Бот читает поступления и сам засчитывает
оплату.

**Истина здесь — документация Bybit** (`bybit-exchange/docs`, ветка master,
снято 31.08). Своих вариантов не сочиняем: SDK уже один раз соврал.

* **`GET /v5/asset/deposit/query-internal-record`** — переводы внутри
  Bybit, не в блокчейне. Параметры: `txID`, `startTime`, `endTime`, `coin`,
  `cursor`, `limit` (1..50). Между `startTime` и `endTime` не больше 30 дней.
* **UID отправителя приходит в `fromMemberId`** — на нём держится всё
  сопоставление. **В TypeScript-SDK `bybit-api` этого поля нет**: тип
  `InternalDepositRecordV5` перечисляет id, type, coin, amount, status,
  address, createdTime, txID — и всё. Построив по SDK, мы бы заключили,
  что отправителя не видно и автоподтверждение невозможно. Это неправда.
* **`status`: 1 — в обработке, 2 — успех, 3 — отказ.** Засчитывается
  только двойка. Единица — не «почти успех», а «ещё неизвестно».
* **`txID`** — ключ идемпотентности: по нему одно поступление не
  засчитывается дважды после перезапуска.
* **`address`** — замазанные почта или телефон отправителя, не адрес
  кошелька. Показывать продавцу нечего, и мы не показываем.
* **Лимит запросов — 300 в секунду.** Опрос раз в полминуты не заметят;
  подгонять темп, как у Юмаркета, здесь не нужно.

Подпись (те же документы, раздел «Authentication»):

* заголовки `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP` (мс), `X-BAPI-SIGN`,
  `X-BAPI-RECV-WINDOW`;
* для GET подписывается строка `timestamp + api_key + recv_window +
  queryString`, HMAC-SHA256, hex в нижнем регистре;
* время запроса обязано лежать в `[server_time - recv_window;
  server_time + 1000)`. Отсюда отказ `10002` при ушедших часах контейнера —
  и это НЕ «неверный ключ», лечится он совсем другим.

Ключ нужен **только на чтение**. Ключ с правом вывода в чужих руках — это
чужие деньги, а нам достаточно видеть поступления.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
RECV_WINDOW = "20000"          # запас на часы контейнера, но не бесконечный
TIMEOUT = 20
DAY_MS = 86_400_000
MAX_WINDOW_MS = 30 * DAY_MS    # документация: больше 30 дней за раз нельзя

# Успех перевода — только двойка. Единица означает «ещё неизвестно», и
# засчитать её значит выдать подписку за перевод, который может не дойти.
STATUS_DONE = 2
STATUS_PENDING = 1
STATUS_FAILED = 3


class BybitError(Exception):
    """Отказ Bybit, уже переведённый на русский.

    `code` сохраняется рядом: по нему решают, что делать, а не по тексту —
    правило «не управляйте логикой по тексту собственного отчёта».
    """

    def __init__(self, message: str, code: int = 0, fixable: bool = True):
        super().__init__(message)
        self.code = int(code or 0)
        self.fixable = bool(fixable)


# Отказы, которые продавец или владелец может исправить сам, и те, что
# лечатся ожиданием. Текст пишется владельцу, значит — на «ты».
_ERRORS: dict[int, str] = {
    10001: "Bybit не принял параметры запроса.",
    10002: ("Часы сервера разошлись с Bybit. Это не про ключ — ключ может "
            "быть верным; проверь время на машине, где живёт бот."),
    10003: ("Bybit не принял ключ. Три обычные причины: ключ от другого "
            "кабинета, ключ отозван, либо он с testnet, а мы ходим на "
            "основной адрес."),
    10004: ("Подпись не сошлась. Обычно это лишний пробел или перенос "
            "строки в секрете — вставь его заново, целиком."),
    10005: ("У ключа нет прав на чтение активов. Включи в кабинете Bybit "
            "право читать кошелёк — вывод средств не нужен, и включать его "
            "не надо."),
    10006: "Bybit просит сбавить темп. Подожду и повторю.",
    10007: "Bybit не признал пользователя ключа.",
    10010: ("Bybit не пустил с нашего адреса: у ключа задан белый список "
            "IP. Либо убери его, либо впиши туда адрес, с которого ходит "
            "бот."),
    10016: "Bybit сейчас отвечает ошибкой на своей стороне. Повторю позже.",
}

# Что лечится ожиданием, а не руками. `explain` вторым значением отдаёт
# «можно ли починить», чтобы совет не расходился с делом.
_TRANSIENT = frozenset({10006, 10016})


def explain(code: int, message: str = "") -> tuple[str, bool]:
    """Отказ по-русски и «можно ли исправить руками».

    Английский код на экране — отписка: `retMsg` у Bybit английский, и
    показывать его вместо объяснения значит перекладывать разбор на
    того, кто платит нам за то, чтобы не разбираться.
    """
    code = int(code or 0)
    known = _ERRORS.get(code)
    if known:
        return known, code not in _TRANSIENT
    tail = f" (код {code}: {message})" if message else f" (код {code})"
    return "Bybit отказал, и это не знакомый нам отказ." + tail, True


def _session() -> requests.Session:
    """Одна точка на весь модуль — и для рабочих вызовов, и для проверки
    ключа. Проба, ходящая мимо, проверяет не то, чем пользуется бот."""
    return requests.Session()


def _payload(timestamp: str, api_key: str, query: str,
             recv_window: str = RECV_WINDOW) -> str:
    """Строка, которую подписывают: `timestamp + api_key + recv_window +
    queryString` — без разделителей, ровно в этом порядке.

    Вынесено отдельно не ради красоты: секрета из примеров документации
    нам не дали, сверить готовый хеш не с чем — а вот саму строку сверить
    можно, и ошибаются именно в ней.
    """
    return f"{timestamp}{api_key}{recv_window}{query}"


def _sign(secret: str, timestamp: str, api_key: str, query: str) -> str:
    """HMAC-SHA256 от `_payload`, hex в нижнем регистре.

    Строка запроса подписывается ровно та, что уйдёт в адрес: собери её
    дважды разными способами — и порядок параметров разойдётся, а подпись
    не сойдётся. Поэтому собирается один раз и передаётся сюда готовой.
    """
    return hmac.new(str(secret).encode("utf-8"),
                    _payload(timestamp, api_key, query).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _get(api_key: str, api_secret: str, path: str, params: dict,
         base_url: str = BASE_URL, session=None) -> dict:
    """Подписанный GET. Отдаёт `result`, иначе бросает `BybitError`."""
    api_key = str(api_key or "").strip()
    api_secret = str(api_secret or "").strip()
    if not api_key or not api_secret:
        raise BybitError("Ключ Bybit не задан.", 0)

    # `doseq=False` и сортировки нет намеренно: подписывается ровно эта
    # строка, и любая её пересборка ниже сломала бы подпись.
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "")})
    ts = str(int(time.time() * 1000))
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": _sign(api_secret, ts, api_key, query),
        "Accept": "application/json",
    }
    url = f"{(base_url or BASE_URL).rstrip('/')}{path}" + (f"?{query}" if query else "")
    s = session or _session()
    try:
        r = s.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise BybitError(f"Bybit не ответил: {e}", 0, fixable=False) from e

    # HTTP 200 — не ответ на вопрос «получилось ли»: у Bybit всё
    # существенное лежит в конверте, как и у AppRoute.
    try:
        body = r.json()
    except ValueError:
        raise BybitError(
            f"Bybit ответил не по-нашему (HTTP {r.status_code}).", 0)
    code = int(body.get("retCode") or 0)
    if code != 0:
        why, fixable = explain(code, str(body.get("retMsg") or ""))
        raise BybitError(why, code, fixable=fixable)
    return body.get("result") or {}


def internal_deposits(api_key: str, api_secret: str, *,
                      since_ms: int | None = None, coin: str = "",
                      limit: int = 50, base_url: str = BASE_URL,
                      session=None) -> list[dict]:
    """Поступления внутри Bybit, свежие первыми.

    `since_ms` обрезается тридцатью днями — не потому, что нам столько
    надо, а потому что Bybit с большим окном откажет. Молча сдвинуть
    границу и вернуть «ничего не пришло» было бы враньём.
    """
    now = int(time.time() * 1000)
    start = int(since_ms or (now - DAY_MS))
    start = max(start, now - MAX_WINDOW_MS)
    result = _get(api_key, api_secret, "/v5/asset/deposit/query-internal-record",
                  {"startTime": start, "endTime": now,
                   "coin": (coin or "").strip().upper(),
                   "limit": max(1, min(50, int(limit or 50)))},
                  base_url=base_url, session=session)
    rows = result.get("rows")
    return list(rows) if isinstance(rows, list) else []


def check_access(api_key: str, api_secret: str, *, base_url: str = BASE_URL,
                 session=None) -> tuple[bool, str]:
    """Годится ли ключ. Проверяется вызовом, а не видом ключа.

    Отдаёт (годится, что сказать). Именно тем же вызовом, каким потом
    читаются поступления: проверка другим методом отвечала бы на другой
    вопрос — у ключей Bybit права раздаются по разделам.
    """
    try:
        rows = internal_deposits(api_key, api_secret, limit=1,
                                 base_url=base_url, session=session)
    except BybitError as e:
        return False, str(e)
    return True, (f"Ключ принят, поступления читаются "
                  f"(за сутки записей: {len(rows)}).")


def parse_uid(text: str) -> str:
    """UID Bybit из того, что ввёл человек, либо пустая строка.

    UID — это только цифры. Люди присылают его со словом «UID», с
    пробелами внутри и скопированным вместе с невидимыми знаками; принимать
    надо всё это, а не отвечать «неверный формат» на верный UID.
    """
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    # Восемь знаков — нынешняя длина UID, но она растёт со временем, и
    # верхнюю границу мы не выдумываем: отвергнуть настоящий UID дороже,
    # чем принять опечатку — она всё равно не сойдётся с поступлением.
    return digits if 5 <= len(digits) <= 20 else ""
