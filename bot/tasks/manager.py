from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from datetime import datetime

_ACTIVE_STATUSES = {"active", "new", "work", "processing", "pending"}

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_token, get_settings, save_settings

logger = logging.getLogger(__name__)

# Selenium loop interval in seconds (check every 30 minutes)
_AUTO_LOOP_INTERVAL = 30 * 60

# Scheduled promotions are evaluated in the fast 60s loop, so a slot normally
# fires within a minute of its time. This catch-up window lets a slot still run
# if the bot was briefly down at that minute, without replaying long-stale slots
# (e.g. after the bot was off all morning) on the next start.
_BUMP_CATCHUP_SECONDS = 2 * 3600

# How many listings one restore pass will act on. Each candidate costs a stock
# check plus a publish, run one after another while the per-user lock is held —
# and the 60s orders loop needs that same lock. A shop with a few hundred ads
# down would otherwise stall order notifications for minutes. A backlog is
# drained over consecutive passes instead.
_RESTORE_MAX_PER_PASS = 40

# A status the marketplace refused with incorrect_status is skipped, but not
# forever: the ban is inferred from one ad's answer and applied to every ad in
# that state, so it is re-tested after a week in case it was transient or the
# marketplace changed its mind.
_RESTORE_BARRED_TTL = 7 * 86400

# Догоняющее автопринятие: заказ уже лежит оплаченным, а в работу не взят.
# Сколько таких брать за проход — чтобы после чистки хранилища магазин не
# получил залп из десятков действий разом; насколько старые догонять — старый
# оплаченный заказ мог быть выдан вручную, и на маркетплейсе, где «в работу»
# означает «выдал», нажатие по нему было бы ложным отчётом; сколько раз
# пробовать — отказ маркетплейса не лечится повтором каждую минуту.
from automation.fragment import BUY_TIMEOUT_SECS as _BUY_TIMEOUT_SECS

# Как часто проверять, жива ли сессия Fragment. Один лёгкий запрос страницы:
# чаще — бессмысленно (куки живут часами), реже — продавец узнаёт об
# истечении от покупателя.
_STARS_SESSION_EVERY = 3 * 3600

_CATCHUP_PER_PASS = 3
_CATCHUP_HOURS = 48
_CATCHUP_TRIES = 3


async def shop_balance(user_id: int, api,
                       why: list | None = None) -> tuple[float, str]:
    """The shop's balance → (amount, formatted) — «—» when it can't be read.

    The Integration API has none: /check answers identity only, so every
    caller reading it saw zero and reported it as fact. The panel holds the
    figure, and it is the one withdrawal acts on.

    `why` — куда положить причину, если сумму прочитать не удалось. Без неё
    экраны показывали голый прочерк: «Баланс сейчас: —» в итогах дня и ни
    слова о том, почему. Панель причину называет, её просто выбрасывали по
    дороге.
    """
    try:
        amount, text = await api.get_balance()
        if text not in ("—", None):
            return amount, text
    except Exception:
        pass
    from handlers.balance import _panel_balance
    err = ""
    try:
        shown, err = await _panel_balance(user_id)
    except Exception as e:
        shown, err = None, str(e)[:150]
    if shown is None:
        if why is not None:
            why.append(err or "панель не назвала причину")
        return 0.0, "—"
    try:
        # Разряды обязательны: «586226 ₽» на экране приходится пересчитывать
        # глазами по три цифры, и ошибиться разрядом здесь — это ошибиться
        # в сумме вывода.
        from orderfields import money as _fmt_money
        return float(shown), f"{_fmt_money(float(shown))} ₽"
    except (TypeError, ValueError):
        return 0.0, str(shown)


def _barred_map(ar: dict) -> dict:
    """{status: expiry} of statuses restore should skip.

    Accepts the older plain-list form, which carried no expiry, and dates it
    from now so an existing install starts re-testing instead of skipping those
    statuses forever.
    """
    raw = ar.get("barred_until")
    if isinstance(raw, dict):
        return {str(k).lower(): float(v or 0) for k, v in raw.items()}
    legacy = ar.get("barred_statuses") or []
    return {str(st).lower(): time.time() + _RESTORE_BARRED_TTL for st in legacy}


def _fmt_time(raw, settings: dict | None = None) -> str:
    """Время маркетплейса → время продавца.

    Раньше строка вида «2026-08-09T06:50:00+00:00» обрезалась до девятнадцати
    символов и разбиралась как местная: часовой пояс из ответа выбрасывался, и
    продавец видел время сервера. Разбор теперь общий с `_ts_of`, который
    смещение учитывает.
    """
    if not raw:
        return ""
    import localtime as _lt
    ts = _ts_of({"created_at": raw}) if not isinstance(raw, (int, float)) \
        else float(raw)
    if ts:
        return _lt.fmt(ts, settings)
    return str(raw)[:16]


def _is_newer(msg_id: str, last_id: str) -> bool:
    try:
        return int(msg_id) > int(last_id)
    except (ValueError, TypeError):
        return msg_id > last_id


# Один отправитель на магазин. Фоновый цикл шлёт уведомления сам, никого не
# спрашивая: сколько процессов запущено, столько копий и приходит продавцу.
# Команды при этом отвечают как обычно — апдейт Telegram достаётся только
# одному, — так что изнутри бота два процесса неотличимы от ошибки в коде.
# Аренда пишется в общее хранилище: владелец продлевает её на каждом проходе,
# остальные молчат. Чужая аренда старше этого срока считается брошенной —
# иначе упавший контейнер заткнул бы бота навсегда.
_SENDER_LEASE_TTL = 180.0


def _claim_sender(settings: dict, now: float | None = None,
                  instance: str = "") -> bool:
    """Взять право рассылки на этот проход → можно ли работать."""
    from handlers.start import INSTANCE_ID
    me = instance or INSTANCE_ID
    now = now if now is not None else time.time()
    lease = settings.get("_sender") or {}
    owner = str(lease.get("inst") or "")
    fresh = now - float(lease.get("ts") or 0) < _SENDER_LEASE_TTL
    if owner and owner != me and fresh:
        logger.info("sender lease held by %s — skipping tick", owner)
        return False
    settings["_sender"] = {"inst": me, "ts": now}
    return True


# Что этот процесс уже отправил — для /sent. Живёт в памяти и только своего
# процесса: если продавец видит дубль, а здесь одна запись, вторую копию
# прислал кто-то другой. Это и отличает две беды друг от друга.
_SENT_LOG: list[tuple[float, str]] = []
_SENT_LOG_MAX = 40


def _note_sent_notification(text: str) -> None:
    _SENT_LOG.append((time.time(), " ".join(str(text or "").split())[:70]))
    del _SENT_LOG[:-_SENT_LOG_MAX]


def _is_formatting_error(exc: Exception) -> bool:
    """Telegram отказался разбирать HTML — единственный случай, когда то же
    сообщение можно послать заново.

    Разрыв связи и таймаут сюда не входят: сообщение при этом обычно уже
    доставлено, и повтор превращается в дубль. Отличить их по факту
    доставки нельзя, поэтому повторяем только явный отказ разбора.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(mark in text for mark in (
        "can't parse entities", "cant parse entities", "unsupported start tag",
        "unclosed start tag", "can't find end tag", "entity", "bad request"))


def _msg_sort_key(msg: dict) -> tuple:
    """Ключ хронологического порядка: сначала номер, время — на ничью.

    Номер и есть определение «новее» во всём остальном коде (`_is_newer`,
    `_newest_id`), и порядок обязан считаться так же — иначе у бота два
    расходящихся представления о том, какое письмо позже.
    """
    mid = str(msg.get("id", ""))
    try:
        return (0, int(mid), _ts_of(msg))
    except (ValueError, TypeError):
        # Нечисловой номер — сравниваем строкой, но отдельной группой:
        # смешивать int и str в одном ключе нельзя.
        return (1, mid, _ts_of(msg))


def _msg_rows(data) -> list[dict]:
    """The messages out of whatever envelope the API used, oldest first.

    A chat outside an order does not have to answer in the same shape as one
    attached to an order, and a nested list arriving where a list was assumed
    raised inside the poll loop, where the error was only logged — so nothing
    ever arrived and nothing ever complained.

    Порядок задаётся здесь, потому что это единственное место, через которое
    проходят все читатели переписки. Юмаркет отдаёт письма ОТ НОВЫХ К СТАРЫМ;
    те, кто по списку ходил и показывал, получали разговор задом наперёд:
    уведомления в обратном порядке, а экран чата — начало переписки вместо
    свежих сообщений. Срезы `[-10:]` и `[-15:]` теперь значат то, что и
    читается — «самые свежие».
    """
    if isinstance(data, list):
        return sorted((m for m in data if isinstance(m, dict)), key=_msg_sort_key)
    if not isinstance(data, dict):
        return []
    for key in ("data", "items", "messages", "results"):
        v = data.get(key)
        if isinstance(v, list):
            return sorted((m for m in v if isinstance(m, dict)),
                          key=_msg_sort_key)
        if isinstance(v, dict):
            inner = _msg_rows(v)
            if inner:
                return inner
    return []


def _newest_id(rows: list[dict]) -> str:
    """The largest message id present.

    Taking rows[-1] assumed the API sorts oldest-first. If it sorts the other
    way round, that is the OLDEST message, "is there anything newer" is false
    forever, and no notification is ever sent.
    """
    best = ""
    for m in rows:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        if not best or _is_newer(mid, best):
            best = mid
    return best


# Кто отправил сообщение. Подстрока ищется только у отличительных слов: короткое
# «me» встречается внутри «customer».
_OWN_PARTS = ("shop", "seller", "store", "merchant", "продав", "магаз",
              "support", "админ")
_OWN_EXACT = frozenset({"me", "self", "own", "admin", "bot", "system"})


def _is_own_message(msg: dict, in_support_chat: bool = False) -> bool:
    """Сообщение написано магазином, а не собеседником.

    В чате с поддержкой «support» и «админ» — это как раз собеседник, а не мы;
    записав их в свои, бот считал бы такой чат всегда отвеченным.
    """
    if msg.get("is_mine") or msg.get("is_own"):
        return True
    sender = msg.get("sender_type") or msg.get("sender") or ""
    if isinstance(sender, dict):
        sender = (sender.get("type") or sender.get("role")
                  or sender.get("name") or "")
    sender = str(sender).lower()
    parts = (tuple(p for p in _OWN_PARTS if p not in ("support", "админ"))
             if in_support_chat else _OWN_PARTS)
    exact = _OWN_EXACT - {"admin"} if in_support_chat else _OWN_EXACT
    return sender in exact or any(k in sender for k in parts)


def _ts_of(msg: dict) -> float:
    """Время сообщения в секундах эпохи, 0 — если разобрать не вышло."""
    raw = (msg.get("created_at") or msg.get("date") or msg.get("time")
           or msg.get("timestamp"))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw) / 1000 if float(raw) > 1e11 else float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return 0.0
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


def _msg_text(msg: dict) -> str:
    """Текст сообщения, как бы поле ни называлось.

    Читались только `text` и `message`. Всё остальное превращалось в пустую
    цитату «— » в уведомлении, и продавцу приходило «СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ»
    без сообщения.
    """
    for key in ("text", "message", "body", "content", "comment", "value"):
        value = msg.get(key)
        if isinstance(value, dict):
            value = value.get("text") or value.get("value") or value.get("body")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fingerprint(text: str) -> str:
    """Отпечаток текста — чтобы узнать своё сообщение, вернувшееся из чата."""
    import hashlib
    norm = " ".join(str(text or "").lower().split())[:400]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


_SENT_PER_CHAT = 20
_SENT_CHATS = 100


def _note_sent_by_hand(settings: dict, chat_id: str, text: str) -> None:
    """Это написал продавец сам, из бота, а не автоответ.

    Маркетплейс возвращает такое письмо следующим проходом и ничем не
    помечает — бот присылал его продавцу как «СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ» с его
    же текстом. Отпечаток кладётся в общую книгу (чтобы своё узнавалось) и
    отдельно сюда — чтобы подпись была «вы ответили», а не тишина: автоответ
    продавец не писал, а это писал.
    """
    _note_sent_text(settings, chat_id, text)
    if not text:
        return
    book: dict = settings.setdefault("_hand_fp", {})
    row: list = book.setdefault(str(chat_id), [])
    row.insert(0, _fingerprint(text))
    del row[_SENT_PER_CHAT:]
    if len(book) > _SENT_CHATS:
        for key in list(book)[_SENT_CHATS:]:
            book.pop(key, None)


def _is_hand_text(settings: dict, chat_id: str, text: str) -> bool:
    if not text:
        return False
    row = (settings.get("_hand_fp") or {}).get(str(chat_id)) or []
    return _fingerprint(text) in row


def _note_sent_text(settings: dict, chat_id: str, text: str) -> None:
    """Запомнить, что это писали мы.

    API не помечает сообщения магазина ничем надёжным, и бот принимал
    собственный вопрос за ответ покупателя: из «отправьте ваш @username
    (например @durov)» он доставал «username» и шёл покупать звёзды
    несуществующему человеку. Отпечаток отправленного — то, по чему своё
    узнаётся точно.
    """
    if not text:
        return
    book: dict = settings.setdefault("_sent_fp", {})
    row: list = book.setdefault(str(chat_id), [])
    row.insert(0, _fingerprint(text))
    del row[_SENT_PER_CHAT:]
    if len(book) > _SENT_CHATS:
        for key in list(book)[_SENT_CHATS:]:
            book.pop(key, None)


def _is_our_text(settings: dict, chat_id: str, text: str) -> bool:
    if not text:
        return False
    row = (settings.get("_sent_fp") or {}).get(str(chat_id)) or []
    return _fingerprint(text) in row


def _msg_kind(msg: dict) -> str:
    """«system» — служебная запись чата, а не письмо покупателя."""
    for key in ("type", "kind", "message_type", "event"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


_SERVICE_KINDS = ("system", "service", "event", "status", "order", "notice",
                  "info", "bot")

# Сообщение, увиденное с опозданием, отвечать поздно, а уведомлять о нём
# поштучно — значит завалить продавца утренней перепиской в девять вечера.
_MSG_FRESH = 6 * 3600


def _is_service_message(msg: dict) -> bool:
    """Отметка маркетплейса о заказе — оплате, выдаче, смене статуса.

    Покупатель их не писал, и уведомлять о них как о его сообщении нельзя.
    """
    return any(k in _msg_kind(msg) for k in _SERVICE_KINDS)


def _newest_msg(rows: list[dict]) -> dict:
    """Самое свежее сообщение — по номеру, а не по месту в списке."""
    best: dict = {}
    best_id = ""
    for m in rows:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        if not best_id or _is_newer(mid, best_id):
            best_id, best = mid, m
    return best


_USERNAME_RE = re.compile(r"@?([a-zA-Z][a-zA-Z0-9_]{3,31})")

# Слова-заполнители из подсказок: это не ники, а место, куда ник вписывают.
# Бот доставал «username» из собственного вопроса и шёл покупать звёзды
# несуществующему человеку. Настоящие ники сюда не попадают — «durov» это
# живой аккаунт, и запрещать его нельзя.
_NOT_A_USERNAME = frozenset({
    "username", "юзернейм", "nickname", "example", "yourname",
})


def _extract_username(text: str) -> str:
    """Pull a Telegram @username out of free-form buyer text."""
    if not text:
        return ""
    # Prefer an explicit @mention
    for m in re.finditer(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})", text):
        if m.group(1).lower() not in _NOT_A_USERNAME:
            return m.group(1)
    stripped = text.strip()
    m = _USERNAME_RE.fullmatch(stripped)
    if m and m.group(1).lower() not in _NOT_A_USERNAME:
        return m.group(1)
    return ""


_COMPLAINT_KEYWORDS = (
    "жалоб", "жалуюсь", "проблем", "обман", "не работает", "не пришл",
    "не пришло", "верните", "верни деньги", "возврат", "скам", "scam",
    "развод", "кидал", "кинул", "арбитраж", "спор", "диспут", "dispute",
    "администрац", "поддержк", "модератор", "обратился в", "напишу в поддержку",
)
_COMPLAINT_STATUSES = (
    "dispute", "disputed", "complaint", "arbitration", "arbitrage",
    "problem", "conflict", "appeal",
)


def _is_complaint_text(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _COMPLAINT_KEYWORDS)


def _order_field(order: dict, *keys, default=None):
    """First non-empty value among keys (supports nested buyer.*)."""
    for k in keys:
        if "." in k:
            a, b = k.split(".", 1)
            v = (order.get(a) or {})
            v = v.get(b) if isinstance(v, dict) else None
        else:
            v = order.get(k)
        if v not in (None, "", "—"):
            return v
    return default


def _order_username(order: dict) -> str:
    """Buyer @username / contact if the API exposes it."""
    u = _order_field(order, "buyer_username", "username", "buyer.username",
                     "buyer.login", "contact", "buyer.contact")
    if not u:
        return ""
    u = str(u)
    return u if u.startswith("@") else f"@{u}"


# Списки статусов — общие с разбором заказа. Здесь не было «success», которым
# этот маркетплейс помечает выполненный заказ: выручка по таким заказам никуда
# не попадала.
from orderfields import (BACK as _BACK_STATUSES, DONE as _DONE_STATUSES,
                         WORK as _WORK_STATUSES, needs_work as _needs_work,
                         status_ru as _status_ru)


# Windowed order figures used to live here. They now come from stats_source,
# which reads the panel's ledger and falls back to this same local history, so
# the report and the «Статистика» screen cannot disagree about a day.


# Сколько чатов заказов опрашивать за проход. Каждый — отдельный запрос, но
# проход раз в минуту, так что два десятка вполне посильны.
_CHAT_POLL_LIMIT = 25
_CLOSED_QUIET_AFTER = 7 * 86400


def _stars_drop_if_closed(settings: dict, order_id: str) -> str:
    """Снять закрытый заказ с очереди «ждём @ник» → статус по-русски или «».

    Очередь AutoStars перехватывает каждое письмо покупателя в чате заказа,
    считая его ответом с ником. Для возвращённого заказа ждать нечего, а
    перехват остаётся: письма не доходят ни до уведомлений, ни до правил.
    """
    import orderfields as of
    status = str((settings.get("known_orders") or {}).get(str(order_id), ""))
    if not status or (status not in of.BACK and status not in of.DONE):
        return ""
    pending = ((settings.get("plugins") or {}).get("auto_stars") or {}
               ).get("pending") or {}
    if str(order_id) not in pending:
        return ""
    pending.pop(str(order_id), None)
    return of.status_ru(status)


def _order_age(det: dict) -> float | None:
    """Сколько секунд заказу. None — время неизвестно.

    Время создания на маркетплейсе достовернее, чем «когда бот его увидел»:
    после чистки хранилища (а без DATABASE_URL она случается при каждом
    редеплое) бот видит все старые заказы заново, и по `seen_at` им всем
    выходит сегодняшний возраст.
    """
    ts = _ts_of({"created_at": det.get("created")})
    if not ts:
        try:
            ts = float(det.get("seen_at") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    if not ts:
        return None
    return max(0.0, time.time() - ts)


def _stars_awaiting_delivery(settings: dict, order_id: str) -> bool:
    """Заказ стоит в очереди AutoStars — товар ещё не отправлен."""
    p = (settings.get("plugins") or {}).get("auto_stars") or {}
    if not p.get("enabled"):
        return False
    return str(order_id) in (p.get("pending") or {})


def _chats_to_poll(known_orders: dict, order_details: dict) -> list[str]:
    """Чаты каких заказов читать — самые свежие, а не первые попавшиеся.

    Раньше здесь стоял срез `[:15]` по словарю заказов. Словарь хранит порядок
    добавления, то есть срез брал пятнадцать САМЫХ СТАРЫХ заказов. Выполненные
    из него не исключались («success» в список закрытых не входил), поэтому у
    продавца с полутора десятками покупок все места занимали давно закрытые
    заказы, а свежие чаты бот не читал вообще — и автоответы молчали.
    """
    now = time.time()
    rows: list[tuple[float, str]] = []
    for oid, status in known_orders.items():
        det = order_details.get(oid) or {}
        seen = float(det.get("seen_at") or 0)
        closed = str(status) in _BACK_STATUSES
        # Возврат недельной давности писем уже не приносит.
        if closed and seen and now - seen > _CLOSED_QUIET_AFTER:
            continue
        # Чат, которого нет: маркетплейс отвечает 404 на каждом проходе. Он
        # занимал место в очереди из 25 чатов и висел предупреждением на
        # экране, притом что читать там нечего.
        if det.get("chat_gone"):
            continue
        rows.append((seen, str(oid)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [oid for _, oid in rows[:_CHAT_POLL_LIMIT]]


def _ar_context(details: dict | None, order_id: str, settings: dict) -> dict:
    """Данные заказа для подстановок вида {товар} в автоответе."""
    from autoreply import context
    return context(details, order_id, settings.get("shop_name", ""), settings)


def _ar_log(settings: dict, chat_id: str, text: str, ok: bool, err: str,
            rule: str) -> None:
    """Записать отправку автоответа в журнал, который видно в боте.

    Событийные автоответы (новый заказ, выполнен, возврат) отправлялись «в
    никуда»: провал попадал только в логи контейнера. Журнал у них общий с
    ответами на сообщения — продавцу неважно, какой механизм промолчал.
    """
    from autoreply import cfg, log
    log(cfg(settings), chat_id=chat_id, text=text, ok=ok, err=err, rule=rule)


def _today_stats(order_details: dict, known_orders: dict,
                 settings: dict | None = None) -> tuple[int, int]:
    """(orders today, revenue today ₽) from locally tracked order details.

    Kept for the new-purchase notification's running tally, which counts every
    order taken today, not only completed ones.
    """
    now = time.time()
    from stats_source import day_start as _day_start
    day_start = _day_start(now, settings)
    cnt = 0
    rev = 0
    for oid, det in order_details.items():
        seen = det.get("seen_at", 0)
        if seen and seen >= day_start:
            cnt += 1
            try:
                amount = float(str(det.get("price", 0)))
            except (ValueError, TypeError):
                continue          # цены нет — это не ноль рублей
            # Цена в записи — за штуку: карточка так её и показывает, «60 ₽
            # ×2». Выручка без множителя занижала день на каждой покупке
            # больше одной штуки.
            try:
                qty = int(float(str(det.get("quantity") or 1)))
            except (ValueError, TypeError):
                qty = 1
            rev += int(amount * max(qty, 1))
    return cnt, rev


def _parse_star_qty(title: str, default: int) -> int:
    """Extract a star count from an order title like '100 звёзд Telegram'."""
    if title:
        nums = re.findall(r"\d{2,6}", title.replace(" ", ""))
        if nums:
            try:
                val = int(nums[0])
                if 50 <= val <= 1_000_000:
                    return val
            except ValueError:
                pass
    return default


# Notifications share one layout: a title, a rule, the facts, a rule, context.
# Scanning a phone screen is easier when every alert puts the same thing in the
# same place.
_RULE = "━━━━━━━━━━━━━━"


def _esc(text) -> str:
    """Notifications are sent with HTML parsing, so anything typed by a buyer or
    by support — a '<' in a message, an angle bracket in a title — would make
    Telegram reject the send and the notification would never arrive."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _money(value) -> str:
    """1234567 -> '1 234 567' — a thin space every three digits."""
    try:
        return f"{int(float(str(value).replace(' ', '').replace(',', '.'))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _watch_name(w: dict) -> str:
    """What to call a watched listing in a notification."""
    title = str(w.get("title") or "").strip()
    if title:
        return title[:40]
    url = str(w.get("url") or "").split("://")[-1]
    return (url[:34] + "…") if len(url) > 35 else (url or "товар")


def _promo_price(settings: dict) -> int:
    from handlers.selenium_settings import promo_price
    return promo_price(settings)


def _card(title: str, body: list[str], footer: str = "") -> str:
    # Empty strings are deliberate spacing, so only None is dropped — filtering
    # on truthiness collapsed the blank lines that separate the blocks.
    parts = [title, _RULE, *[b for b in body if b is not None]]
    if footer:
        parts += [_RULE, footer]
    return "\n".join(parts)


# Сколько выдач помним. Журнал нужен для «Прибыли» и «Накопленных», но это
# настройки, а не база данных: расти без предела им нельзя.
_STARS_LOG_LIMIT = 300

# Тексты покупателю. Продавец может заменить любой; пустая строка означает
# «оставить как есть». Подстановки — только те, что здесь перечислены:
# обещать в подсказке больше, чем подставляется, хуже, чем не обещать.
STARS_TEXTS = {
    "ask": ("⭐ Для выдачи звёзд отправьте, пожалуйста, ваш ник в Telegram — "
            "одним сообщением, например: durov\nЗвёзды придут автоматически."),
    "remind": ("⭐ Напоминаем: пришлите ваш ник в Telegram одним сообщением — "
               "и звёзды придут автоматически. Если ника нет, его можно "
               "задать в настройках Telegram."),
    "sending": "⏳ Отправляю {qty}⭐ на @{username}…",
    "done": "✅ Готово! {qty}⭐ отправлены на @{username}. Спасибо за заказ!",
    "failed": ("⚠️ Не удалось отправить звёзды автоматически. "
               "Продавец скоро выдаст их вручную."),
}


def stars_text(settings: dict, key: str, **fields) -> str:
    """Текст покупателю: свой, если задан, иначе стандартный.

    Подстановка защищена: продавец пишет текст руками, и {опечатка} в нём не
    должна ронять выдачу — сообщение уйдёт как есть.
    """
    p = (settings.get("plugins") or {}).get("auto_stars") or {}
    custom = str(((p.get("texts") or {}).get(key) or "")).strip()
    template = custom or STARS_TEXTS.get(key, "")
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return template


def stars_notify_on(settings: dict, key: str) -> bool:
    """Слать ли продавцу уведомление этого рода."""
    p = (settings.get("plugins") or {}).get("auto_stars") or {}
    return bool((p.get("notify") or {}).get(key, True))


def _log_delivery(settings: dict, order_id: str, qty: int, username: str,
                  ton: float) -> None:
    """Записать выдачу: что, кому, когда и во сколько обошлась.

    Раньше оставался только список номеров заказов — по нему нельзя ответить
    ни «сколько звёзд выдано», ни «сколько на этом заработано».
    """
    p = settings.setdefault("plugins", {}).setdefault("auto_stars", {})
    entry = {"order_id": str(order_id), "qty": int(qty or 0),
             "username": str(username or "").lstrip("@"),
             "ts": time.time(), "ton": round(float(ton or 0.0), 6)}
    # Выручку берём из заказа: цена уже прочитана при разборе, и это
    # единственное честное «сколько получили» — курс тут ни при чём.
    det = (settings.get("known_order_details") or {}).get(str(order_id)) or {}
    from orderfields import order_price
    revenue = order_price(det) if isinstance(det, dict) else None
    if revenue is not None:
        entry["revenue"] = float(revenue)
    log = p.setdefault("log", [])
    log.append(entry)
    if len(log) > _STARS_LOG_LIMIT:
        del log[:len(log) - _STARS_LOG_LIMIT]


def _stars_failure_hint(result) -> str:
    """Что делать с этой конкретной неудачей — одной строкой.

    «Bad request» и «получатель не найден» лечатся совершенно по-разному, а
    выглядят одинаково: голый текст ошибки, из которого продавцу непонятно
    ничего. Ни одна подсказка не должна упираться в F12 — телефона это не
    касается.
    """
    said = str(result or "").lower()
    if "bad request" in said or "api-hash" in said or "api hash" in said:
        return ("🔑 Похоже, истекли куки Fragment — сессия отдаётся как гостю. "
                "Обновите их: Плагины → AutoStars → 🔑 Данные Fragment "
                "→ «🧪 Проверить вход» покажет, что именно отвалилось.\n\n")
    if "не найден" in said or "not found" in said or "assigned to a user" in said:
        return ("👤 Fragment не нашёл такой ник. Уточните его у покупателя — "
                "возможно, аккаунт скрыт или ник написан с ошибкой.\n\n")
    if "ton" in said and ("баланс" in said or "недостат" in said):
        return "💎 Пополните TON-кошелёк — на покупку не хватило.\n\n"
    return ""


def _order_notify_kb(order_id: str, chat_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Quick actions straight from the notification (no need to open the order).
    #
    # Кнопки «В работу» здесь нет намеренно. Что делает POST /orders/{id}/work
    # на этом маркетплейсе, до конца не выяснено: на тестовом заказе покупатель
    # в ту же минуту увидел «магазин сообщил, что выполнил заказ». Пока это не
    # подтверждено, нажимать такое одним тапом из уведомления нельзя — это
    # заявление перед покупателем и площадкой. Автопринятие остаётся: оно
    # включается осознанно и показывает настоящий статус после действия.
    builder.button(text="✅ Подтвердить", callback_data=f"order:{order_id}:confirm")
    builder.button(text="↩️ Возврат", callback_data=f"order:{order_id}:refundask")
    builder.button(text="💬 Чат", callback_data=f"chat:{chat_id}:")
    builder.button(text="🔍 Детали", callback_data=f"order:{order_id}:view")
    builder.adjust(2, 2)
    return builder.as_markup()


def _balance_notify_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Вывести средства", callback_data="balance:withdraw")
    builder.button(text="💰 Баланс", callback_data="menu:balance")
    builder.button(text="📊 Статистика", callback_data="menu:stats")
    builder.adjust(1, 2)
    return builder.as_markup()


def _watched_notify_kb(chat_id: str) -> InlineKeyboardMarkup:
    """For a support/moderation message: the marketplace API refuses a reply
    (no active order), so answering goes through the panel chat token in-bot,
    with a link to the panel as a fallback."""
    digits = "".join(ch for ch in str(chat_id) if ch.isdigit()) or str(chat_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"sreply:{chat_id}")
    builder.button(text="📜 История", callback_data=f"wchat_hist:{chat_id}")
    builder.button(text="🌐 В панели",
                   url=f"https://panel.yoomarket.net/chats/{digits}")
    builder.adjust(2, 1)
    return builder.as_markup()


def _missed_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Открыть чаты", callback_data="chats:list")
    builder.adjust(1)
    return builder.as_markup()


def _message_notify_kb(chat_id: str, order_id: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Ответить", callback_data=f"reply_init:{chat_id}")
    builder.button(text="💬 Открыть чат", callback_data=f"chat:{chat_id}:")
    if order_id:
        # Most replies are followed by acting on the order, so keep those here
        builder.button(text="✅ Подтвердить",
                       callback_data=f"order:{order_id}:confirm")
        builder.adjust(2, 1)
    else:
        builder.adjust(2)
    return builder.as_markup()


def _shop_label(user_id: int) -> str:
    """The shop the bot's token belongs to, with the active account name.

    Naming both sides is what turns "разные магазины" from a verdict into
    something the seller can act on: adding an account is not the same as
    switching to it, and that is the usual reason the two drift apart.
    """
    from storage import get_active_account, get_shop_name
    shop = get_shop_name(user_id) or "магазин без имени"
    account = get_active_account(user_id)
    return f"{shop}" + (f" · аккаунт «{account}»" if account else "")


async def panel_republish(user_id: int, rows: list[dict]
                          ) -> tuple[list[dict], list[dict]]:
    """Retry refused publishes through the panel. Returns (done, still_failed).

    The Integration API answers incorrect_status for listings sitting in
    «unpublish» — the state it is supposed to undo — so publishing over it is a
    dead end for them. The panel runs the same Nova action a human clicks,
    which is already how promotion and withdrawal work here, so the same route
    is the fallback rather than an invented endpoint.

    Only listings the API refused with incorrect_status come here: a network
    failure or a real validation error should not silently drive a second
    attempt by another road.

    Module level, not a method: the manual «Запустить сейчас» button needs it
    too, and it uses nothing from the task manager.
    """
    from storage import get_panel_creds
    creds = get_panel_creds(user_id)
    if not creds or not creds.get("cookies"):
        return [], [{**r, "panel": "unreached",
                     "reason": f"{r.get('reason', '')} · нет входа в панель"}
                    for r in rows]

    from automation.panel import (panel_find_listing_sync,
                                  panel_publish_item_sync)
    loop = asyncio.get_event_loop()

    # The two systems number the same listing differently, and not every
    # listing lives in the panel's `items` resource — an id from the API
    # answered 404 there. Find each one by title across the panel's own lists,
    # then act on the resource that actually holds it.
    found, trace = await loop.run_in_executor(
        None, panel_find_listing_sync, creds["cookies"],
        [r.get("title") for r in rows])

    # Nothing matched at all, while the panel clearly holds listings: that is
    # not a naming quirk but the panel being logged into a different shop than
    # the API token belongs to. Said plainly, because no amount of matching can
    # fix an account mismatch — and it is the same cause behind the panel
    # answering "нет прав" to promotion.
    # Deliberately not concluding "different shops" from an empty result: this
    # panel keeps listings in more than one resource, so failing to find them
    # means the search missed, not that the shop is wrong. That inference cost
    # several rounds chasing a configuration that was correct all along.
    mismatch = False
    done, failed = [], []
    for row in rows:
        hit = found.get(_title_key(row.get("title")))
        if not hit:
            note = (f" · ⚠️ панель и токен — разные магазины ({_shop_label(user_id)})"
                    if mismatch else
                    f" · в панели не нашёл этот товар (искал: {trace})")
            # Not found is a verdict on where the listing is, not on its
            # status — marked so, so it cannot bar anything.
            failed.append({**row, "panel": "not_found",
                           "reason": f"{row.get('reason', '')}{note}"})
            continue
        res_name, panel_id = hit
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_publish_item_sync,
                                     creds["cookies"], panel_id,
                                     user_id, True, res_name),
                timeout=60)
        except Exception as e:
            ok, msg = False, str(e)[:120]
        if ok:
            done.append(row)
        else:
            # The panel's own markup is stripped: the reason is escaped
            # downstream, so its tags would reach the seller as literal <code>.
            plain = re.sub(r"<[^>]+>", "", str(msg)).replace("\n", " ")
            failed.append({**row, "panel": "refused",
                           "reason": f"{row.get('reason', '')} · панель "
                                     f"{res_name}#{panel_id}: {plain[:300]}"})
    return done, failed


def _title_key(title) -> str:
    """A title reduced to what both systems agree on: letters and digits.

    Titles carry emoji, pipes and spacing that differ between the API and the
    panel, so comparing them raw finds nothing.
    """
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", str(title or "")).lower()


def _match_panel_id(title, by_title: dict) -> str:
    """The panel id for a listing title: exact first, then a containment match
    for the truncation and decoration the two lists apply differently."""
    key = _title_key(title)
    if not key:
        return ""
    if key in by_title:
        return by_title[key]
    for other, pid in by_title.items():
        if key.startswith(other[:24]) or other.startswith(key[:24]):
            return pid
    return ""


# Состояния записи журнала Robux, означающие «выдача начата и не доведена».
# По ним `_robux_resume` находит то, что оборвалось посередине: контейнер
# перезапускается при каждом выкате, а статус оплаченного заказа больше не
# меняется — сам такой заказ не подхватится никогда.
_ROBUX_UNFINISHED = ("собираемся покупать", "покупаем", "куплен, отправляем")


class TaskManager:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}
        self._auto_tasks: dict[int, asyncio.Task] = {}
        # Per-user lock: the orders loop (60s) and the auto-tasks loop (30min)
        # both load→mutate→save the whole settings blob. Without serializing
        # them, whichever saves last silently clobbers the other's changes
        # (e.g. AutoStars pending / known_orders vs bump_schedule.last_runs).
        self._locks: dict[int, asyncio.Lock] = {}
        # Каталог AppRoute на пару минут: он отдаётся ОДНИМ ответом на 1263
        # услуги, а читался заново под каждый заказ. Два оплаченных заказа
        # подряд — два тяжёлых чтения по 90 секунд таймаута, и всё это время
        # опрос заказов этого продавца стоит. Срок нарочно короткий: остаток
        # у номинала меняется, и решает всё равно сухой прогон перед покупкой.
        self._ar_catalog: dict[int, tuple[float, object]] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def start_for_user(self, user_id: int) -> None:
        if user_id in self._tasks and not self._tasks[user_id].done():
            self._tasks[user_id].cancel()
        self._tasks[user_id] = asyncio.create_task(self._user_loop(user_id))
        # Also start the auto-features loop
        if user_id in self._auto_tasks and not self._auto_tasks[user_id].done():
            self._auto_tasks[user_id].cancel()
        self._auto_tasks[user_id] = asyncio.create_task(self._auto_loop(user_id))

    def stop_for_user(self, user_id: int) -> None:
        if user_id in self._tasks:
            self._tasks[user_id].cancel()
            del self._tasks[user_id]
        if user_id in self._auto_tasks:
            self._auto_tasks[user_id].cancel()
            del self._auto_tasks[user_id]

    async def start_all(self) -> None:
        from storage import get_all_users
        for uid in get_all_users():
            self.start_for_user(uid)

    async def _user_loop(self, user_id: int) -> None:
        while True:
            try:
                await self._tick(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Task error for user %s: %s", user_id, e)
            await asyncio.sleep(60)

    async def _tick(self, user_id: int) -> None:
        token = get_token(user_id)
        if not token:
            return
        async with self._lock(user_id):
            settings = get_settings(user_id)
            if not _claim_sender(settings):
                return
            await self._process_orders(user_id, token, settings)
            await self._check_reminders(user_id, settings)
            await self._maybe_bump_schedule(user_id, settings)

    async def _maybe_bump_schedule(self, user_id: int, settings: dict) -> None:
        """Fire scheduled promotions at their configured times.

        Runs in the fast (60s) loop. In the auto loop this was evaluated every
        30 minutes against a 35-minute window — two periods so close that any
        drift in that loop (a slow panel call, a restart) pushed the tick past
        the window and the slot was silently dropped for the whole day. Here a
        slot fires within a minute of its time, and a catch-up window covers
        brief downtime instead of losing the run.
        """
        bs = settings.get("bump_schedule", {})
        if not (bs.get("enabled") and bs.get("times")):
            return

        # Сутки и часы — продавца: слот «10:00» должен срабатывать в его
        # десять утра, а дневной лимит трат — обнуляться в его полночь.
        import localtime as _lt
        now_dt = _lt.now(settings)
        today_str = now_dt.strftime("%Y-%m-%d")
        last_runs: dict = bs.setdefault("last_runs", {})
        dirty = False

        if bs.get("spent_day") != today_str:
            bs["spent_day"] = today_str
            bs["spent_today"] = 0.0
            dirty = True

        # Run-markers are keyed "{date}_{slot}"; drop previous days' so the
        # settings blob doesn't grow without bound.
        stale = [k for k in last_runs if not k.startswith(f"{today_str}_")]
        if stale:
            for k in stale:
                last_runs.pop(k, None)
            dirty = True

        due: list[str] = []
        for slot in bs["times"]:
            try:
                sh, sm = map(int, str(slot).split(":"))
                slot_dt = now_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
            except (ValueError, AttributeError):
                continue
            if last_runs.get(f"{today_str}_{slot}"):
                continue
            due_secs = (now_dt - slot_dt).total_seconds()
            if 0 <= due_secs <= _BUMP_CATCHUP_SECONDS:
                due.append(slot)

        if not due:
            if dirty:
                save_settings(user_id, settings)
            return

        price_per_bump = float(bs.get("price_per_bump", 0) or 0)
        ceiling = float(bs.get("daily_ceiling", 0) or 0)
        spent_today = float(bs.get("spent_today", 0) or 0)

        # Ceiling already reached: mark the due slots handled so this stops
        # being re-checked every minute, and say so once.
        if ceiling > 0 and price_per_bump > 0 and spent_today >= ceiling:
            for slot in due:
                last_runs[f"{today_str}_{slot}"] = now_dt.isoformat()
            save_settings(user_id, settings)
            await self._notify(
                user_id,
                f"⛔ Продвижение ({', '.join(due)}) пропущено: достигнут потолок "
                f"{ceiling:.0f} ₽/день (потрачено {spent_today:.0f} ₽)",
            )
            return

        # One run promotes every listing, so slots that came due together
        # (after downtime) collapse into a single pass — never paying twice.
        try:
            count, msg = await self._panel_bump(user_id)
        except Exception as e:
            logger.warning("Scheduled promotion error for user %s: %s", user_id, e)
            return  # slots stay unmarked, so the next tick retries

        for slot in due:
            last_runs[f"{today_str}_{slot}"] = now_dt.isoformat()
        bs["bumps_total"] = int(bs.get("bumps_total", 0) or 0) + (count or 0)
        bs["last_bump_at"] = now_dt.isoformat()
        if price_per_bump > 0 and count:
            spent_today += count * price_per_bump
            bs["spent_today"] = spent_today
            bs["spent_total"] = float(bs.get("spent_total", 0) or 0) + count * price_per_bump
            cap = f" (потрачено {spent_today:.0f}"
            cap += f"/{ceiling:.0f} ₽)" if ceiling > 0 else " ₽)"
            msg += cap
        settings["bump_schedule"] = bs
        save_settings(user_id, settings)
        await self._notify(user_id, f"⬆️ Продвижение ({', '.join(due)}): {msg}")

    async def _process_orders(self, user_id: int, token: str, settings: dict) -> None:
        api = YooMarketAPI(token)
        await api.start()
        try:
            data = await api.get_orders()
            orders = data.get("data") or data.get("items") or []

            known: dict = settings.get("known_orders", {})
            ar = settings.get("auto_reply", {})
            ae = settings.get("auto_events", {})
            rules = settings.get("auto_rules", [])
            responders_map = settings.get("responders", {})
            order_details: dict = settings.setdefault("known_order_details", {})

            blacklist: list = settings.get("blacklist", [])
            reminded: list = settings.setdefault("reminded_orders", [])

            # First-run baseline: on the very first pass (fresh DB / new account)
            # record existing orders SILENTLY — otherwise every old order would
            # be treated as "new" and spam a notification. Only orders that
            # appear AFTER initialization trigger alerts/auto-actions.
            initialized = settings.get("orders_initialized", False)

            # Listings that just sold, for the instant put-back-on-sale below.
            sold_now = False
            sold_ads: set[str] = set()

            # Заказы, взятые в работу «догоняющим» проходом (см. ниже).
            caught_up: list[str] = []

            # По каким заказам выдача Robux в этом проходе уже вызывалась.
            # Остальные из ручной очереди дочитываются поимённо ниже.
            robux_tried: set[str] = set()

            # Сколько заказов дочитать за проход. Каждый — отдельный запрос,
            # поэтому на первом проходе полная витрина разбирается за
            # несколько кругов, а дальше дочитываются только новые.
            detail_budget = 8

            for order in orders:
                oid = str(order.get("id", ""))
                if not oid:
                    continue

                # Разбор общий с экраном заказов: угаданные имена полей
                # оставляли здесь «—» вместо товара, покупателя и суммы, а на
                # этих значениях стоит и статистика, и подписи чатов.
                from orderfields import describe as _describe
                d = _describe(order)
                prev_det = order_details.get(oid, {})
                # Заказы, дочитанные до того, как цена стала браться из
                # объявления, помечены «дочитан» — и остались бы без цены
                # навсегда, а вместе с ними и выручка за день. Одна
                # повторная попытка на заказ: `price_tried` ставится и при
                # неудаче, иначе заказ с удалённым объявлением дёргал бы
                # витрину каждый проход.
                needs_price = (d["price"] is None
                               and not prev_det.get("price_tried")
                               and prev_det.get("price") in (None, "", "—"))
                # Список отдаёт заказ без товара и покупателя — дочитываем
                # карточку. Один раз на заказ: дальше берём из сохранённого.
                if (not d["title"] or not d["buyer"] or needs_price):
                    if prev_det.get("enriched") and not needs_price:
                        d["title"] = d["title"] or prev_det.get("title") or ""
                        d["buyer"] = d["buyer"] or prev_det.get("buyer") or ""
                        d["username"] = d["username"] or prev_det.get("username") or ""
                        if d["price"] is None:
                            d["price"] = prev_det.get("price")
                            d["price_src"] = prev_det.get("price_src", "")
                    elif detail_budget > 0:
                        detail_budget -= 1
                        d = await self._enrich_order(api, oid, d, order)
                status = d["status"]
                prev_status = known.get(oid)
                title = d["title"] or "—"
                buyer = d["buyer"] or "—"
                price = d["price"] if d["price"] is not None else "—"
                time_raw = d["created"]
                # Номер чата ищется по всем полям, где он бывает, и берётся из
                # уже дочитанного, если в строке списка его нет. Подставлять
                # номер заказа «на всякий случай» — это resource_not_found на
                # каждом втором чате.
                from orderfields import order_chat_id as _chat_of
                chat_id = (_chat_of(order) or str(d.get("chat_id") or "")
                           or str(prev_det.get("chat_id") or "") or oid)
                username = d["username"] or _order_username(order)
                quantity = d["quantity"]
                category = _order_field(order, "category", "category_name", "ad_category")

                work_at = prev_det.get("work_at")
                if (status in _WORK_STATUSES
                        and prev_status not in _WORK_STATUSES):
                    work_at = time.time()
                # Спорный статус — это проблема сам по себе, ещё до того, как
                # покупатель что-то написал.
                disputed = any(cs in str(status).lower()
                               for cs in _COMPLAINT_STATUSES)
                order_details[oid] = {
                    "title": title,
                    "buyer": buyer,
                    "price": price,
                    # Откуда взята цена: пусто — из заказа, «ad» — из
                    # объявления, потому что в заказе её не было вовсе.
                    "price_src": d.get("price_src")
                    or prev_det.get("price_src", ""),
                    # Объявление за ценой спрашивали — второй раз не пойдём.
                    "price_tried": bool(d.get("price_tried")
                                        or prev_det.get("price_tried")),
                    "chat_id": chat_id,
                    "username": username,
                    "quantity": quantity,
                    "category": category,
                    "seen_at": prev_det.get("seen_at") or time.time(),
                    # Когда заказ создан на маркетплейсе. До этого статистика
                    # знала только `seen_at` — когда бот его впервые увидел,
                    # — и на первом же проходе вся история магазина получала
                    # сегодняшнюю дату: «Сегодня» показывало выручку за все
                    # месяцы сразу. С панелью это не всплывало, потому что
                    # цифры берутся оттуда; без панели — ровно этот обман.
                    "created": time_raw or prev_det.get("created"),
                    "work_at": work_at,
                    "status": status,
                    # Дочитанное не перечитывается на каждом проходе.
                    "enriched": bool(d.get("enriched") or prev_det.get("enriched")),
                    # Метки подсветки чата переживают пересборку записи — иначе
                    # красный чат гас на следующем же проходе опроса.
                    "waiting": prev_det.get("waiting", 0),
                    "problem": (time.time() if disputed
                                else prev_det.get("problem", 0)),
                    # Почему заказ не взят в работу — тоже переживает
                    # пересборку. Иначе счётчик попыток обнулялся каждый
                    # проход, и «не долбиться в отказ» превращалось в
                    # долбёжку раз в минуту.
                    "work_tries": prev_det.get("work_tries", 0),
                    "work_error": prev_det.get("work_error", ""),
                    "work_error_status": prev_det.get("work_error_status", ""),
                    "work_skip": prev_det.get("work_skip", ""),
                    "work_result": prev_det.get("work_result", ""),
                }

                # If order moved to a terminal/changed status, clear its reminder record
                if prev_status is not None and prev_status != status:
                    if oid in reminded:
                        reminded.remove(oid)

                # Complaint/dispute status → high-priority alert
                cn = settings.get("complaint_notify", {"enabled": True})
                if (initialized and cn.get("enabled", True) and prev_status != status
                        and any(cs in str(status).lower() for cs in _COMPLAINT_STATUSES)):
                    seen = cn.setdefault("seen", [])
                    mark = f"status:{oid}:{status}"
                    if mark not in seen:
                        seen.append(mark)
                        settings["complaint_notify"] = cn
                        who = _esc(f"{buyer}" + (f" ({username})" if username else ""))
                        await self._notify(
                            user_id,
                            _card("🚨 <b>СПОР ПО ЗАКАЗУ</b>",
                                  [f"📦 <b>{_esc(title)}</b>",
                                   f"💰 <b>{_money(price)} ₽</b>   📊 {_status_ru(status)}",
                                   "",
                                   f"👤 {who}",
                                   f"🧾 <code>#{oid}</code>",
                                   "",
                                   "🔺 <b>Требуется вмешательство</b>"]),
                            reply_markup=_order_notify_kb(oid, chat_id),
                        )

                is_blacklisted = buyer in blacklist

                if prev_status is None and not initialized:
                    # baseline pass: record silently, no notify / no auto-actions
                    known[oid] = status
                    continue

                if prev_status is None:
                    # A sale is the moment the listing may have gone off sale —
                    # note it so it can be put back up right now instead of
                    # waiting out the restore interval.
                    sold_now = True
                    sold_ad = _order_field(order, "ad_id", "product_id",
                                           "item_id", "listing_id",
                                           "ad.id", "product.id", "item.id")
                    if sold_ad:
                        sold_ads.add(str(sold_ad))

                    # Auto-accept: press "начать заказ" so the order does not
                    # sit unaccepted (an unaccepted order the buyer can cancel).
                    accepted, real = await self._maybe_accept_order(
                        user_id, api, settings, oid, status, order_details)
                    if accepted:
                        known[oid] = status = real or "work"
                    if not is_blacklisted and settings.get(
                            "notify_orders", {}).get("enabled", True):
                        time_str = _fmt_time(time_raw, settings)
                        cnt_today, rev_today = _today_stats(order_details, known, settings)
                        qty_part = f"  ×{quantity}" if quantity else ""
                        who_line = f"👤 <b>{_esc(buyer)}</b>" + (
                            f"  {_esc(username)}" if username else "")
                        body = [
                            f"📦 <b>{_esc(title)}</b>{qty_part}",
                            f"💰 <b>{_money(price)} ₽</b>"
                            + (" <i>по объявлению</i>"
                               if d.get("price_src") == "ad" else "")
                            + (f"   🏷 {_esc(category)}" if category else ""),
                            "",
                            who_line,
                            f"🕐 {time_str}   🧾 <code>#{oid}</code>"
                            if time_str else f"🧾 <code>#{oid}</code>",
                        ]
                        if accepted:
                            # Что маркетплейс сделал с заказом на самом деле,
                            # а не как называется кнопка. Если «Брать в
                            # работу» переводит заказ сразу в «выполнен» —
                            # продавец обязан это увидеть в ту же минуту, а не
                            # узнать от покупателя.
                            got = order_details[oid].get("work_result") or ""
                            body.append(
                                f"▶️ <i>взят в работу автоматически</i> → "
                                f"{_status_ru(got)}" if got else
                                "▶️ <i>взят в работу автоматически</i>")
                        await self._notify(
                            user_id,
                            _card("🛒 <b>НОВАЯ ПОКУПКА</b>", body,
                                  f"📊 Сегодня: <b>{cnt_today}</b> · "
                                  f"<b>{_money(rev_today)} ₽</b>"),
                            reply_markup=_order_notify_kb(oid, chat_id),
                        )
                    if ar.get("enabled"):
                        msg = self._pick_message(
                            title, ar.get("message", "Спасибо за заказ!"),
                            rules, responders_map,
                            _ar_context(order_details.get(oid), oid, settings))
                        ok, err = await self._send_chat(api, chat_id, msg, settings)
                        _ar_log(settings, chat_id, msg, ok, err, "новый заказ")
                    # AutoStars: ask the buyer for their @username in chat
                    await self._maybe_ask_stars_username(
                        api, settings, oid, title, chat_id, status)
                    # AutoRoblox: спрашивать нечего — выдаём код сразу
                    robux_tried.add(oid)
                    await self._maybe_deliver_robux(
                        user_id, api, settings, oid, title, chat_id, status)

                elif prev_status != status and status in _DONE_STATUSES:
                    ev = ae.get("on_confirmed", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(
                            title, ev.get("message", "Заказ подтверждён!"),
                            rules, responders_map,
                            _ar_context(order_details.get(oid), oid, settings))
                        ok, err = await self._send_chat(api, chat_id, msg, settings)
                        _ar_log(settings, chat_id, msg, ok, err, "заказ выполнен")
                    cnt_today, rev_today = _today_stats(order_details, known, settings)
                    buyer_line = _esc(f"👤 {buyer}" + (f" ({username})" if username else ""))
                    await self._notify(
                        user_id,
                        _card("✅ <b>ЗАКАЗ ВЫПОЛНЕН</b>",
                              [f"📦 <b>{_esc(title)}</b>",
                               f"💰 <b>{_money(price)} ₽</b>",
                               "",
                               buyer_line,
                               f"🧾 <code>#{oid}</code>"],
                              f"📊 Сегодня выполнено на "
                              f"<b>{_money(rev_today)} ₽</b>"),
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                elif prev_status != status and status in _BACK_STATUSES:
                    ev = ae.get("on_refunded", {})
                    if ev.get("enabled"):
                        msg = self._pick_message(
                            title, ev.get("message", "Возврат оформлен."),
                            rules, responders_map,
                            _ar_context(order_details.get(oid), oid, settings))
                        ok, err = await self._send_chat(api, chat_id, msg, settings)
                        _ar_log(settings, chat_id, msg, ok, err, "возврат")
                    buyer_line = _esc(f"👤 {buyer}" + (f" ({username})" if username else ""))
                    await self._notify(
                        user_id,
                        _card("↩️ <b>ВОЗВРАТ</b>",
                              [f"📦 <b>{_esc(title)}</b>",
                               f"💰 <b>{_money(price)} ₽</b>",
                               "",
                               buyer_line,
                               f"🧾 <code>#{oid}</code>   📊 {_status_ru(status)}"]),
                        reply_markup=_order_notify_kb(oid, chat_id),
                    )

                # Оплата могла прийти позже создания заказа: заказ увидели
                # неоплаченным, выдачу не начинали, а теперь деньги дошли.
                tried_now = False
                if prev_status is not None and prev_status != status:
                    from orderfields import is_paid as _is_paid
                    if _is_paid(status) and not _is_paid(prev_status):
                        tried_now = True
                        # Взять в работу — тоже здесь. Автопринятие жило в
                        # ветке «заказ увиден впервые», а увиден он бывает
                        # неоплаченным: деньги приходили следующим проходом, и
                        # второго шанса у автопринятия не было. Для магазина,
                        # где оплата догоняет заказ, функция просто не
                        # срабатывала — при включённом тумблере.
                        got, real = await self._maybe_accept_order(
                            user_id, api, settings, oid, status, order_details)
                        if got:
                            status = real or "work"
                        await self._maybe_ask_stars_username(
                            api, settings, oid, title, chat_id, status)
                        robux_tried.add(oid)
                        await self._maybe_deliver_robux(
                            user_id, api, settings, oid, title, chat_id, status)

                # Заказ лежит оплаченным, а в работу не взят. Автопринятие
                # знало ровно два момента: заказ увиден впервые и пришла
                # оплата. Оба можно пропустить — и пропускались:
                #
                # * первый проход после чистого хранилища записывает все
                #   заказы молча, а без DATABASE_URL хранилище на Railway
                #   стирается при каждом редеплое;
                # * тумблер могли включить уже после покупки.
                #
                # Дальше заказ не трогал никто: статус не менялся, значит не
                # было и повода. Продавец видел в панели оплаченный заказ с
                # ненажатой кнопкой — при включённом автопринятии.
                if (not tried_now and prev_status is not None
                        and _needs_work(status)):
                    status = await self._catch_up_work(
                        user_id, api, settings, oid, status, order_details,
                        title, caught_up)

                known[oid] = status

            settings["reminded_orders"] = reminded
            settings["orders_initialized"] = True  # baseline established

            settings["known_orders"] = known
            settings["known_order_ids"] = list(known.keys())
            settings["known_order_details"] = order_details

            await self._robux_forced_sweep(user_id, api, settings, robux_tried)
            # После ручной очереди: сначала то, о чём продавец попросил
            # явно, потом то, что оборвалось само.
            await self._robux_resume(user_id, api, settings, robux_tried)

            await self._check_messages(user_id, api, settings)
            await self._check_watched_chats(user_id, api, settings)
            await self._auto_confirm(user_id, api, settings)
            await self._auto_refund(user_id, api, settings)
            if sold_ads or sold_now:
                await self._restore_after_sale(user_id, api, settings,
                                               sold_ads, sold_now)

        finally:
            # Сохраняем при любом исходе. Раньше save_settings стоял последней
            # строкой try: стоило упасть чему-нибудь после рассылки — проверке
            # чатов, автоподтверждению, возврату товара в продажу, — и всё уже
            # разосланное не записывалось. Следующий проход считал заказы и
            # письма новыми и уведомлял по второму разу.
            try:
                save_settings(user_id, settings)
            except Exception as e:
                logger.error("Save settings failed (user %s): %s", user_id, e)
            await api.close()

    async def _check_watched_chats(self, user_id: int, api: YooMarketAPI,
                                   settings: dict) -> None:
        """Poll chats that belong to no order — support, moderation.

        The API cannot list chats, only read one by id, so these are the ids the
        seller added by hand. Without this, support messages never reach the bot
        at all: order polling has nothing to find them by.
        """
        watched: dict = settings.get("watched_chats") or {}
        if not watched:
            return
        # Support and moderation are chat traffic too — the same switch covers
        # them, but their position is still tracked so nothing is re-sent when
        # notifications are turned back on.
        announce = settings.get("notify_messages", {}).get("enabled", True)

        for chat_id, info in list(watched.items()):
            try:
                data = await api.get_messages(chat_id)
                messages = _msg_rows(data)
                if not messages:
                    continue

                newest_id = _newest_id(messages)
                last_known = info.get("last_msg")

                # Тот же признак, что и у чатов заказов: последним написал
                # покупатель — чат ждёт ответа. Считается всегда, чтобы метка
                # гасла, когда продавец ответил из панели.
                newest = _newest_msg(messages)
                if newest and _is_own_message(newest, in_support_chat=True):
                    info["waiting"] = 0
                    info["problem"] = 0
                elif newest and not info.get("waiting"):
                    info["waiting"] = _ts_of(newest) or time.time()

                if last_known is None:
                    info["last_msg"] = newest_id      # baseline, stay quiet
                    continue
                if not _is_newer(newest_id, last_known):
                    continue

                label = info.get("label") or f"Чат #{chat_id}"
                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known):
                        continue
                    if _is_own_message(msg, in_support_chat=True):
                        continue
                    if _is_complaint_text(msg.get("text") or msg.get("message") or ""):
                        info["problem"] = time.time()

                    if not announce:
                        continue
                    text = (msg.get("text") or msg.get("message") or "")[:400]
                    time_str = _fmt_time(msg.get("created_at")
                                         or msg.get("date"), settings)
                    await self._notify(
                        user_id,
                        _card(f"🛟 <b>{_esc(label).upper()}</b>",
                              [f"🕐 {time_str}" if time_str else "",
                               "",
                               f"<blockquote>{_esc(text)}</blockquote>"],
                              f"💬 <code>#{chat_id}</code>"),
                        reply_markup=_watched_notify_kb(str(chat_id)),
                    )
                info["last_msg"] = newest_id
            except Exception as e:
                logger.warning("watched chat %s: %s", chat_id, e)

        settings["watched_chats"] = watched

    async def _check_messages(self, user_id: int, api: YooMarketAPI, settings: dict) -> None:
        known_orders: dict = settings.get("known_orders", {})
        known_messages: dict = settings.setdefault("known_messages", {})
        order_details: dict = settings.get("known_order_details", {})

        active = _chats_to_poll(known_orders, order_details)
        # Постоянный покупатель ведёт ВСЕ свои заказы в одном чате: у второго
        # и третьего заказа тот же chat_id. Опрос шёл по заказам, поэтому один
        # и тот же чат читался столько раз, сколько у покупателя заказов, — и
        # каждое его письмо приходило продавцу столько же раз. У случайного
        # покупателя заказ один, и там всё выглядело исправно.
        by_chat: dict[str, list[str]] = {}
        for oid in active:
            cid = str((order_details.get(oid) or {}).get("chat_id") or oid)
            by_chat.setdefault(cid, []).append(oid)

        # Пульс опроса: по нему видно, читает ли бот чаты вообще. Без этого
        # «автоответы не работают» невозможно отличить от «до чатов не дошло».
        poll = {"ts": 0.0, "chats": len(by_chat), "orders": len(known_orders),
                "new_msgs": 0, "error": ""}
        # Сообщения, которые бот увидел с опозданием (чат раньше не читался).
        # Они собираются здесь и уходят одной сводкой, а не пачкой уведомлений.
        missed: list[tuple[str, str, str]] = []

        for chat_id, chat_orders in by_chat.items():
            # `active` отсортирован от свежих к старым, значит первый — самый
            # свежий заказ этого покупателя. Письмо почти наверняка про него:
            # к нему и привязываем товар, ник и очередь выдачи звёзд.
            order_id = chat_orders[0]
            try:
                # Messages live under the ORDER'S CHAT, not the order:
                # GET /chats/{chat_id}/messages. Querying by order id returned
                # nothing whenever the two differ, so buyer messages never
                # arrived. The id was already stored when the order was seen.
                details = order_details.get(order_id, {})

                data = await api.get_messages(chat_id)
                # Чат прочитался — прошлые сбои больше не в счёт. Отметку
                # снимаем здесь, а не в конце разбора: ниже несколько
                # законных `continue`, и до конца доходит не каждый проход.
                if order_id in order_details:
                    order_details[order_id].pop("chat_misses", None)
                messages = _msg_rows(data)
                if not messages:
                    continue

                newest_id = _newest_id(messages)
                # Отметка «дочитано» теперь на ЧАТ, а не на заказ: у одного
                # чата их может быть несколько, и каждая отмечала прочитанное
                # отдельно. Старые отметки по заказам сворачиваем в одну и
                # берём самую дальнюю — так ничего не объявится по второму
                # разу при переходе на новый ключ.
                mark_key = f"c{chat_id}"
                last_known_id = known_messages.get(mark_key)
                for oid in chat_orders:
                    old = known_messages.pop(oid, None)
                    if old is None:
                        continue
                    if last_known_id is None or _is_newer(old, last_known_id):
                        last_known_id = old
                if last_known_id is not None:
                    # Записываем сразу: ниже несколько законных `continue`, а
                    # старые ключи уже сняты — иначе свёртка потерялась бы, и
                    # чат на следующем проходе выглядел бы непрочитанным.
                    known_messages[mark_key] = last_known_id

                # Кто написал последним — этим и подсвечивается чат в списке.
                # Считается на каждом проходе, даже когда новых сообщений нет:
                # продавец мог ответить из панели или приложения, и тогда метка
                # «покупатель ждёт» должна погаснуть сама.
                # Считаем только настоящие письма: служебные отметки о заказе
                # и пустые строки покупатель не писал, и ждать ответа на них
                # незачем.
                real = [m for m in messages
                        if not _is_service_message(m) and _msg_text(m)
                        and not _is_our_text(settings, chat_id, _msg_text(m))]
                newest = _newest_msg(real)
                if order_id in order_details:
                    det = order_details[order_id]
                    if newest and _is_own_message(newest):
                        det["waiting"] = 0
                        det["problem"] = 0
                    elif newest:
                        det.setdefault("waiting", 0)
                        if not det["waiting"]:
                            det["waiting"] = (_ts_of(newest) or time.time())

                if last_known_id is None:
                    known_messages[mark_key] = newest_id
                    continue

                # Отметка «дочитано до» выше всего, что есть в чате, — значит
                # она не отсюда: номера сообщений на маркетплейсе сквозные, и
                # проход по чужому чату (когда номер чата ещё не был известен
                # и подставлялся номер заказа) записывал сюда чужой номер.
                # Дальше «есть ли что новее» отвечает «нет» — навсегда, и чат
                # глохнет молча. Сбрасываем и читаем заново от свежих.
                if newest_id and _is_newer(last_known_id, newest_id):
                    logger.info("chat %s: watermark %s above chat max %s — reset",
                                chat_id, last_known_id, newest_id)
                    last_known_id = ""

                if not _is_newer(newest_id, last_known_id):
                    continue

                title = details.get("title", f"Заказ #{order_id}")
                buyer_name = details.get("buyer", "Покупатель")
                d_username = details.get("username", "")
                d_price = details.get("price", "")
                who = _esc(f"{buyer_name}" + (f" ({d_username})" if d_username else ""))
                order_line = _esc(f"📦 {title}" + (f"  •  💰 {d_price} ₽" if d_price and d_price != "—" else ""))

                # Пачка писем за один проход отвечается один раз, и последним
                # письмом: «привет» и следом «где ключ» — вопрос во втором.
                # Раньше отвечало то, которое API вернул первым, а он отдаёт
                # от новых к старым — то есть выбор был случайностью формата.
                answer_to: tuple[str, bool] | None = None
                answerable = 0

                for msg in messages:
                    msg_id = str(msg.get("id", ""))
                    if not _is_newer(msg_id, last_known_id):
                        continue
                    poll["new_msgs"] += 1
                    # Skip what the shop itself sent; anything else counts as
                    # the buyer. Requiring a known buyer value meant an
                    # unfamiliar wording silently dropped every message.
                    if _is_own_message(msg):
                        continue
                    # Отметка маркетплейса о заказе — не письмо покупателя.
                    if _is_service_message(msg):
                        continue
                    raw_text = _msg_text(msg)
                    if not raw_text:
                        # Пустая строка — это не сообщение. Раньше о таких
                        # приходило «СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ» с пустой цитатой,
                        # хотя покупатель ничего не писал.
                        continue
                    if _is_our_text(settings, chat_id, raw_text):
                        # Наше же сообщение, вернувшееся из чата. Бот отвечал
                        # сам себе и доставал «@username» из собственного
                        # вопроса — с этого начинался круг покупок в пустоту.
                        # Написанное продавцом вручную всё же показываем: он
                        # ждёт подтверждения, что ответ дошёл. Но подписью
                        # «вы ответили», а не «сообщение от покупателя» — с
                        # его же текстом внутри. Автоответы остаются немыми:
                        # их продавец не писал, и эхо ему ни к чему.
                        if (_is_hand_text(settings, chat_id, raw_text)
                                and settings.get("notify_messages", {})
                                        .get("enabled", True)):
                            await self._notify(
                                user_id,
                                _card("✍️ <b>ВЫ ОТВЕТИЛИ</b>",
                                      [f"<blockquote>{_esc(raw_text[:200])}"
                                       f"</blockquote>",
                                       "",
                                       "<i>Доставлено покупателю.</i>"],
                                      f"{order_line}\n🧾 <code>#{order_id}</code>"),
                                reply_markup=_message_notify_kb(chat_id, order_id))
                        continue
                    sender = msg.get("sender_type") or msg.get("sender")
                    if isinstance(sender, str) and sender.lower() not in (
                            "", "buyer", "client", "customer", "user"):
                        logger.info("chat %s: unknown sender %r", chat_id, sender)

                    # Жалоба красит чат независимо от того, когда её увидели.
                    # Вчерашнее «верните деньги» — сегодняшняя проблема, и
                    # прятать её в сводку пропущенных нельзя.
                    is_complaint = _is_complaint_text(raw_text)
                    if is_complaint and order_id in order_details:
                        order_details[order_id]["problem"] = time.time()

                    # Разбор дневного завала не должен превращаться в поток
                    # уведомлений: то, что пришло часы назад, отвечать поздно —
                    # чат просто помечается ждущим, и в конце уходит одна сводка.
                    age = time.time() - (_ts_of(msg) or time.time())
                    stale = age > _MSG_FRESH
                    if stale:
                        missed.append((order_id, chat_id, raw_text))
                        # Записываем и это: пропуск по давности — тоже причина
                        # молчания, а без записи экран «Почему молчит» уверенно
                        # отвечает «не молчал», хотя покупателю не ответили.
                        import autoreply as _ar
                        conf = _ar.cfg(settings)
                        if conf.get("enabled"):
                            conf["last_skip"] = {
                                "ts": time.time(), "chat": str(chat_id),
                                "text": (raw_text or "")[:80],
                                "why": (f"сообщению {age / 3600:.0f} ч — "
                                        f"отвечать поздно, чат помечен "
                                        f"ждущим")}
                        continue

                    time_str = _fmt_time(msg.get("created_at")
                                         or msg.get("date"), settings)
                    time_part = f"  •  🕐 {time_str}" if time_str else ""
                    # raw_text stays intact for the rules below; only the copy
                    # that goes into an HTML message is escaped
                    msg_text = _esc(raw_text[:200])

                    # AutoStars: buyer replied with their @username → deliver
                    handled = await self._maybe_deliver_stars_reply(
                        user_id, api, settings, order_id, raw_text, chat_id)
                    if handled:
                        continue

                    # Жалоба уже отмечена выше — здесь только уведомление.
                    alerted = False
                    cn = settings.get("complaint_notify", {"enabled": True})
                    if cn.get("enabled", True) and is_complaint:
                        seen = cn.setdefault("seen", [])
                        mark = f"{order_id}:{msg_id}"
                        if mark not in seen:
                            seen.append(mark)
                            if len(seen) > 500:
                                del seen[:250]
                            settings["complaint_notify"] = cn
                            alerted = True
                            await self._notify(
                                user_id,
                                _card("🚨 <b>ЖАЛОБА КЛИЕНТА</b>",
                                      [f"👤 <b>{who}</b>{time_part}",
                                       "",
                                       f"<blockquote>{msg_text}</blockquote>",
                                       "",
                                       "🔺 <b>Ответьте как можно быстрее</b>"],
                                      f"{order_line}\n🧾 <code>#{order_id}</code>"),
                                reply_markup=_message_notify_kb(chat_id, order_id),
                            )

                    # A complaint is an alert, not chat traffic, and is sent
                    # above regardless — this switch only silences ordinary
                    # buyer messages.
                    if not alerted and settings.get(
                            "notify_messages", {}).get("enabled", True):
                        await self._notify(
                            user_id,
                            _card("💬 <b>СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ</b>",
                                  [f"👤 <b>{who}</b>{time_part}",
                                   "",
                                   f"<blockquote>{msg_text}</blockquote>"],
                                  f"{order_line}\n🧾 <code>#{order_id}</code>"),
                            reply_markup=_message_notify_kb(chat_id, order_id),
                        )

                    # Уведомление ушло по каждому письму — их продавцу нужны
                    # все. Ответ покупателю один, и на последнее письмо:
                    # список идёт по возрастанию, так что побеждает свежее.
                    answerable += 1
                    answer_to = (raw_text, is_complaint)

                # Уведомить продавца — половина дела; покупателю нужен ответ.
                # Отправляется после уведомлений, чтобы продавец видел и
                # вопрос, и то, что бот на него ответил.
                if answer_to:
                    raw_text, is_complaint = answer_to
                    await self._auto_answer(
                        user_id, api, settings, chat_id=chat_id,
                        order_id=order_id, text=raw_text, details=details,
                        is_complaint=is_complaint, batch=answerable)

                known_messages[mark_key] = newest_id

            except Exception as e:
                logger.warning("Message check for order %s: %s", order_id, e)
                import autoreply as _ar
                why, _fixable = _ar.explain_error(str(e))
                poll["error"] = f"#{order_id}: {why[:80]}"
                # Чат, которого нет, не появится сам. Три промаха подряд — и
                # заказ выпадает из опроса: иначе он вечно занимает одно из
                # 25 мест и держит на экране предупреждение, по которому
                # нечего делать.
                if "resource_not_found" in str(e).lower() and order_id in order_details:
                    det = order_details[order_id]
                    misses = int(det.get("chat_misses", 0) or 0) + 1
                    det["chat_misses"] = misses
                    if misses >= 3:
                        det["chat_gone"] = time.time()
                        poll["error"] = (f"#{order_id}: чата нет — "
                                         f"больше не опрашиваю")

        settings["known_messages"] = known_messages
        poll["ts"] = time.time()
        poll["missed"] = len(missed)
        settings["_chat_poll"] = poll

        if missed and settings.get("notify_messages", {}).get("enabled", True):
            chats = {c for _o, c, _t in missed}
            body = [f"Пока бот не следил за этими чатами, покупатели написали "
                    f"<b>{len(missed)}</b> раз(а) в <b>{len(chats)}</b> чат(ах)."]
            for _oid, _cid, text in missed[:5]:
                body.append(f"• <i>{_esc(text[:70])}</i>")
            if len(missed) > 5:
                body.append(f"…и ещё {len(missed) - 5}")
            body += ["", "Чаты помечены как ждущие ответа — они в начале списка."]
            await self._notify(
                user_id,
                _card("📥 <b>ПРОПУЩЕННЫЕ СООБЩЕНИЯ</b>", body),
                reply_markup=_missed_kb())

    async def _check_reminders(self, user_id: int, settings: dict) -> None:
        rem = settings.get("reminders", {})
        if not rem.get("enabled"):
            return

        threshold_secs = rem.get("hours", 24) * 3600
        now = time.time()
        known_orders: dict = settings.get("known_orders", {})
        order_details: dict = settings.get("known_order_details", {})
        reminded: list = settings.setdefault("reminded_orders", [])
        reminded_set = set(reminded)

        changed = False
        for oid, status in known_orders.items():
            # Списки статусов — общие, из `orderfields`. Свой, написанный
            # здесь, не знал «success» — статуса, которым этот маркетплейс
            # помечает выполненный заказ. Продавцу приходило «Ждёт
            # подтверждения уже 48 ч» про заказ, у которого в том же
            # сообщении стояло «Статус: ✅ Выполнен», и рядом кнопки
            # «Подтвердить» и «Возврат».
            if status in _DONE_STATUSES or status in _BACK_STATUSES:
                continue
            if oid in reminded_set:
                continue
            det = order_details.get(oid, {})
            seen_at = det.get("seen_at", now)
            if (now - seen_at) < threshold_secs:
                continue

            hours_waiting = int((now - seen_at) / 3600)
            title = det.get("title", f"Заказ #{oid}")
            buyer = det.get("buyer", "—")
            price = det.get("price", "—")
            chat_id = det.get("chat_id", oid)
            uname = det.get("username", "")
            who = _esc(f"{buyer}" + (f" ({uname})" if uname else ""))

            await self._notify(
                user_id,
                f"⏰ <b>Напоминание о заказе</b>\n\n"
                f"🧾 Заказ <code>#{oid}</code>\n"
                f"📦 {_esc(title)}\n"
                f"👤 {who}  •  💰 {_esc(price)} ₽\n"
                f"📊 Статус: {_status_ru(status)}\n\n"
                f"⏳ Ждёт подтверждения уже <b>{hours_waiting} ч</b>",
                reply_markup=_order_notify_kb(oid, chat_id),
            )
            reminded_set.add(oid)
            changed = True

        if changed:
            settings["reminded_orders"] = list(reminded_set)
            from storage import save_settings
            save_settings(user_id, settings)

    async def _maybe_accept_order(self, user_id: int, api: YooMarketAPI,
                                  settings: dict, oid: str, status: str,
                                  order_details: dict) -> tuple[bool, str]:
        """Взять заказ в работу → (взяли, настоящий статус после действия).

        Три вещи, без которых это не работало или работало во вред.

        Только оплаченный. В списке разрешённых статусов стояли «new»,
        «created», «pending» — то есть неоплаченные. Панель на такой заказ
        прямо предупреждает «не выдавайте товар», а бот брал его в работу.

        Статус не назначается от себя, а перечитывается: что делает
        `POST /orders/{id}/work` на этом маркетплейсе, из кода не видно.

        И если перечитанный статус оказался «выполнен» — значит «взять в
        работу» здесь означает «отчитаться о выдаче». Это не догадка, а
        наблюдение, поэтому оно записывается, продавцу говорят один раз, и
        дальше заказы, ждущие выдачи, автопринятие не трогает.
        """
        from orderfields import describe as _describe, is_paid as _is_paid
        aa = settings.get("auto_accept", {})
        if not aa.get("enabled") or not _is_paid(status):
            return False, ""
        if aa.get("means_fulfilled") and _stars_awaiting_delivery(settings, oid):
            logger.info("auto-accept skipped for %s: awaiting delivery", oid)
            order_details.setdefault(str(oid), {})["work_skip"] = (
                "ждёт ник для выдачи звёзд, а «в работу» здесь означает «выдал»")
            return False, ""
        try:
            await api.work_order(oid)
        except Exception as e:
            # Отказ записывается туда, где его увидит продавец: в карточке
            # заказа на вопрос «почему не взят» до этого отвечал лог на
            # сервере, то есть никто.
            logger.warning("Auto-accept order %s: %s", oid, e)
            det = order_details.setdefault(str(oid), {})
            det["work_error"] = str(e)[:200]
            det["work_tries"] = int(det.get("work_tries", 0) or 0) + 1
            # Каким заказ был в этот момент на самом деле. «incorrect_status»
            # один и тот же в двух совершенно разных случаях: маркетплейс не
            # пускает в работу оплаченный заказ — тогда функция здесь
            # невозможна, — или заказ успел уйти из оплаченных между чтением
            # списка и нажатием, и всё в порядке. Различить их можно только
            # статусом, а он на отказе не приходит.
            try:
                fresh = await api.get_order(oid)
                node = (fresh.get("data") if isinstance(fresh, dict)
                        and isinstance(fresh.get("data"), dict) else fresh)
                det["work_error_status"] = _describe(
                    node if isinstance(node, dict) else {})["status"]
            except Exception as err:
                logger.info("Auto-accept re-read after refusal %s: %s", oid, err)
            return False, ""

        real = ""
        try:
            fresh = await api.get_order(oid)
            node = (fresh.get("data") if isinstance(fresh, dict)
                    and isinstance(fresh.get("data"), dict) else fresh)
            real = _describe(node if isinstance(node, dict) else {})["status"]
        except Exception as e:
            logger.warning("Auto-accept re-read %s: %s", oid, e)

        det = order_details.setdefault(str(oid), {})
        det["work_at"] = time.time()
        det["work_result"] = real
        det.pop("work_error", None)
        det.pop("work_skip", None)
        if real and real in _DONE_STATUSES and not aa.get("means_fulfilled"):
            aa["means_fulfilled"] = True
            settings["auto_accept"] = aa
            await self._notify(
                user_id,
                _card("⚠️ <b>«В РАБОТУ» = ОТЧЁТ О ВЫДАЧЕ</b>",
                      [f"Заказ <code>#{_esc(str(oid))}</code> после «взять в "
                       f"работу» сразу стал <b>{_status_ru(real)}</b>.",
                       "",
                       "Значит на этом маркетплейсе это не «начал заниматься», "
                       "а заявление покупателю, что товар выдан.",
                       "",
                       "Заказы, ждущие выдачи звёзд, автопринятие больше не "
                       "трогает. Остальные берёт как раньше."]))
        return True, real

    async def _catch_up_work(self, user_id: int, api: YooMarketAPI,
                             settings: dict, oid: str, status: str,
                             order_details: dict, title: str,
                             caught: list) -> str:
        """Взять в работу заказ, который уже лежит оплаченным.

        Продавец прислал снимок панели: заказ «Оплачен», кнопка «В работу»
        не нажата, автопринятие включено. Автоответ покупателю по этому же
        заказу ушёл — значит бот его видел. Причина в том, что автопринятие
        знало только два момента: заказ увиден впервые и пришла оплата. Оба
        можно пропустить, и тогда заказ не трогал уже никто.

        Предохранители здесь не украшение. «Взять в работу» на этом
        маркетплейсе может означать «товар выдан» (см. `means_fulfilled`), а
        догонять приходится пачку сразу — например всё, что открыто на
        момент чистки хранилища. Поэтому: не больше нескольких за проход, не
        старше суток-двух, и отказ маркетплейса не повторяется бесконечно.

        Возвращает статус заказа — новый, если взяли, прежний, если нет.
        Каждое «не взяли» объясняется в `work_skip`: карточка заказа читает
        оттуда, чтобы вопрос «почему не взят» не выяснялся перепиской.
        """
        aa = settings.get("auto_accept", {}) or {}
        if not aa.get("enabled"):
            return status
        det = order_details.setdefault(str(oid), {})
        if int(det.get("work_tries", 0) or 0) >= _CATCHUP_TRIES:
            return status                     # причина уже лежит в work_error
        if det.get("work_at"):
            # Нажимали и не помогло. Второй раз здесь — это круг: заказ
            # остаётся оплаченным, догонялка видит его снова, и так каждую
            # минуту до бесконечности. Код 200 доказательством не считается,
            # но и поводом жать повторно — тоже.
            det["work_skip"] = ("бот уже нажимал «в работу», а заказ так и "
                                "остался оплаченным — повторно не жму, "
                                "нажмите в панели")
            return status
        if len(caught) >= _CATCHUP_PER_PASS:
            det["work_skip"] = (f"за проход бот берёт не больше "
                                f"{_CATCHUP_PER_PASS} зависших заказов — "
                                f"дойдёт очередь на следующем")
            return status
        age = _order_age(det)
        if age is None:
            det["work_skip"] = ("время заказа неизвестно — такие бот сам в "
                                "работу не берёт, нажмите вручную")
            return status
        hours = float(aa.get("catchup_hours") or _CATCHUP_HOURS)
        if age > hours * 3600:
            det["work_skip"] = (f"заказ старше {int(hours)} ч — бот не берёт "
                                f"в работу задним числом, нажмите вручную")
            return status

        got, real = await self._maybe_accept_order(
            user_id, api, settings, oid, status, order_details)
        if not got:
            return status
        caught.append(str(oid))

        lay = f"{int(age // 3600)} ч" if age >= 3600 else "меньше часа"
        await self._notify(
            user_id,
            _card("✅ <b>ВЗЯЛ В РАБОТУ</b>",
                  [f"📦 <b>{_esc(title)}</b>" if title
                   else "📦 <i>товар без названия</i>",
                   f"🧾 <code>#{_esc(str(oid))}</code>",
                   "",
                   f"Заказ лежал оплаченным {lay} и в работу взят не был — "
                   f"ни при появлении, ни при оплате. Взял сейчас.",
                   "",
                   f"📊 Стал: {_status_ru(real)}" if real else
                   "📊 Маркетплейс ответил, но перечитать статус не удалось — "
                   "проверьте в панели."]))
        return real or "work"

    async def _auto_refund(self, user_id: int, api: YooMarketAPI,
                           settings: dict) -> None:
        """Вернуть деньги за заказы, зависшие дольше заданного срока.

        Единственная автоматика в боте, которая **отдаёт** деньги, поэтому
        осторожностей здесь больше, чем в остальных.

        Главная опасность — вернуть деньги за уже выданный товар: тогда
        продавец теряет и товар, и деньги, а узнаёт об этом из отчёта.
        Бот достоверно знает о выдаче только своей: заказ, который AutoStars
        всё ещё ждёт (покупатель не прислал ник), товара точно не получил.
        Отсюда два режима, и по умолчанию стоит осторожный:

        * `scope="stars"` — только такие заказы;
        * `scope="any"` — любой застрявший в работе, **включая выданные
          вручную**. Включается осознанно, экран об этом предупреждает.

        Остальные предохранители: суточный потолок возвратов, отметка уже
        возвращённых, и перечитывание статуса после — код 200 доказательством
        не считается, а «вернул» при невернувшемся заказе здесь стоит дороже
        всего.
        """
        ar = settings.get("auto_refund", {})
        if not ar.get("enabled"):
            return

        import localtime as _lt
        today = _lt.today_str(settings)
        if ar.get("day") != today:
            ar["day"], ar["count"] = today, 0
        cap = int(ar.get("max_per_day", 3) or 0)
        if cap and int(ar.get("count", 0) or 0) >= cap:
            return

        threshold = float(ar.get("hours", 48) or 48) * 3600
        scope = str(ar.get("scope") or "stars")
        now = time.time()
        known: dict = settings.get("known_orders", {})
        details: dict = settings.get("known_order_details", {})
        done: list = ar.setdefault("done", [])

        for oid, status in list(known.items()):
            if cap and int(ar.get("count", 0) or 0) >= cap:
                break
            if str(oid) in done:
                continue
            # Закрытый или уже возвращённый заказ трогать нечем и незачем.
            if status in _DONE_STATUSES or status in _BACK_STATUSES:
                continue
            if status not in _WORK_STATUSES:
                continue
            det = details.get(oid, {})
            started = det.get("work_at") or det.get("seen_at")
            if not started or (now - started) < threshold:
                continue
            waiting_stars = _stars_awaiting_delivery(settings, oid)
            if scope != "any" and not waiting_stars:
                # В осторожном режиме про этот заказ бот не знает, выдан ли
                # товар, — а значит возвращать за него деньги не вправе.
                continue

            title = det.get("title", f"Заказ #{oid}")
            hours = int((now - started) / 3600)
            try:
                await api.refund_order(oid)
            except Exception as e:
                await self._notify(
                    user_id,
                    _card("⚠️ <b>АВТОВОЗВРАТ НЕ ПРОШЁЛ</b>",
                          [f"📦 {_esc(title)} <code>#{_esc(str(oid))}</code>",
                           f"<i>{_esc(str(e)[:150])}</i>",
                           "",
                           "Заказ остался как был — верните вручную, если "
                           "нужно."]))
                done.append(str(oid))      # второй раз не долбимся
                continue

            # HTTP 200 — не доказательство. Перечитываем статус: «вернул» при
            # невернувшемся заказе здесь дороже любой другой неправды.
            confirmed = ""
            try:
                fresh = await api.get_order(oid)
                from orderfields import order_status
                got = order_status(fresh if isinstance(fresh, dict) else {})
                if got:
                    known[oid] = got
                    confirmed = got
            except Exception:
                pass

            done.append(str(oid))
            ar["count"] = int(ar.get("count", 0) or 0) + 1
            why = ("покупатель не прислал ник для выдачи звёзд"
                   if waiting_stars else "заказ висит в работе")
            if confirmed and confirmed in _BACK_STATUSES:
                head, tail = "↩️ <b>АВТОВОЗВРАТ</b>", "Маркетплейс подтвердил возврат."
            elif confirmed:
                head = "⚠️ <b>АВТОВОЗВРАТ — СТАТУС НЕ ИЗМЕНИЛСЯ</b>"
                tail = (f"Маркетплейс всё ещё показывает «{_status_ru(confirmed)}». "
                        f"Проверьте заказ вручную.")
            else:
                head = "↩️ <b>АВТОВОЗВРАТ ОТПРАВЛЕН</b>"
                tail = "Статус перечитать не удалось — проверьте заказ сами."
            await self._notify(
                user_id,
                _card(head,
                      [f"📦 {_esc(title)} <code>#{_esc(str(oid))}</code>",
                       f"⏳ {why}, {hours} ч",
                       "",
                       tail]))

        settings["auto_refund"] = ar
        settings["known_orders"] = known
        save_settings(user_id, settings)

    async def _auto_confirm(self, user_id: int, api: YooMarketAPI, settings: dict) -> None:
        ac = settings.get("auto_confirm", {})
        if not ac.get("enabled"):
            return
        threshold_secs = ac.get("hours", 24) * 3600
        now = time.time()
        known_orders: dict = settings.get("known_orders", {})
        order_details: dict = settings.get("known_order_details", {})
        # Отметки «предупредил, что товар не выдан» живут ровно столько,
        # сколько заказ стоит в очереди. Возвращённый заказ подтверждения так
        # и не дождётся, и без уборки его отметка осталась бы в настройках
        # навсегда.
        for oid in list(ac.get("held") or {}):
            if not _stars_awaiting_delivery(settings, oid):
                ac["held"].pop(oid, None)
        for oid, status in list(known_orders.items()):
            if status not in _WORK_STATUSES:
                continue
            det = order_details.get(oid, {})
            work_at = det.get("work_at")
            if not work_at or (now - work_at) < threshold_secs:
                continue
            # «Выполнил заказ» — это заявление перед покупателем и площадкой.
            # Заказ, который ещё стоит в очереди на выдачу звёзд, товара не
            # получил, и подтверждать его — рапортовать о невыданном: выдача
            # падает, деньги уходят продавцу, покупатель идёт в арбитраж.
            if _stars_awaiting_delivery(settings, oid):
                held = ac.setdefault("held", {})
                if str(oid) not in held:
                    held[str(oid)] = now
                    await self._notify(
                        user_id,
                        _card("⏸ <b>НЕ ПОДТВЕРЖДАЮ — ТОВАР НЕ ВЫДАН</b>",
                              [f"📦 {_esc(det.get('title', f'Заказ #{oid}'))} "
                               f"<code>#{_esc(str(oid))}</code>",
                               "",
                               "Заказ ждёт выдачи звёзд. Подтвержу, когда "
                               "товар уйдёт покупателю, — или выдайте "
                               "вручную: Плагины → AutoStars."]))
                continue
            ac.get("held", {}).pop(str(oid), None)
            try:
                await api.confirm_order(oid)
                known_orders[oid] = "confirmed"
                title = det.get("title", f"Заказ #{oid}")
                # Раньше здесь было просто «Авто-подтверждение», и отличить
                # его от подтверждения из AutoStars или от ручного нажатия
                # было нельзя — источник записи в чате оставался загадкой.
                hours = int(ac.get("hours", 24) or 24)
                await self._notify(
                    user_id,
                    f"✅ <b>Авто-подтверждение</b>\n\n📦 {title} #{oid}\n"
                    f"<i>Причина: заказ в работе больше {hours} ч.</i>")
            except Exception as e:
                logger.warning("Auto-confirm order %s: %s", oid, e)
        settings["known_orders"] = known_orders

    # ------------------------------------------------------------------
    # AutoStars — Telegram Stars auto-delivery via Fragment
    # ------------------------------------------------------------------

    async def _maybe_deliver_robux(
        self, user_id: int, api: YooMarketAPI, settings: dict,
        order_id: str, title: str, chat_id: str, status: str = "",
    ) -> None:
        """Выдать код Robux по оплаченному заказу.

        Порядок шагов — из `docs/robux_delivery.md`, и он не переставляется:
        намерение пишется до вызова поставщика, код — до отправки
        покупателю, отметка «выдано» — только после подтверждённой отправки.
        Каждое из трёх закрывает свой способ потерять деньги.

        В отличие от звёзд, спрашивать покупателя не о чем: выдаётся код,
        ник не нужен. Поэтому выдача идёт сразу по факту оплаты.
        """
        from automation.robux import (codes_from_result, is_robux_order,
                                      match_denomination, order_reference,
                                      robux_quantity)
        from orderfields import is_paid
        from storage import get_ar_creds

        p = settings.get("plugins", {}).get("auto_roblox", {})
        # Ручная выдача идёт этим же путём, а не своим. Второй путь к деньгам
        # пришлось бы снабдить тем же порядком записей (намерение → код →
        # отметка), и однажды он бы с этим разъехался. Продавец ставит заказ
        # в очередь, покупает по-прежнему фоновый цикл.
        forced: list = p.setdefault("force", [])
        by_hand = order_id in forced
        if not by_hand:
            if not p.get("enabled"):
                return
            if not is_robux_order(title, p.get("keyword") or ""):
                return
        # «Создан» деньгами не является: панель на таком заказе прямо
        # предупреждает «не выдавайте товар». Это правило не обходится и
        # вручную: продавец видит статус в боте, а деньги — на маркетплейсе.
        if not is_paid(status):
            if by_hand:
                forced.remove(order_id)
                # Без записи снятие живёт до конца прохода: настройки
                # читаются заново каждую минуту, заказ возвращался в очередь
                # сам собой, и продавец получал одно и то же уведомление
                # раз в минуту, пока покупатель не заплатит.
                save_settings(user_id, settings)
                await self._robux_stop(
                    user_id, settings, order_id, 0,
                    f"заказ не оплачен (статус «{status}»). Выдавать по "
                    f"неоплаченному нельзя — маркетплейс сам об этом "
                    f"предупреждает", record=False)
            return
        delivered: list = p.setdefault("delivered", [])
        if order_id in delivered:
            # Ручную очередь при этом надо освободить: заказ мог быть выдан
            # уже после того, как его туда поставили, и запись осталась бы
            # висеть, а бот молча заходил бы за ней каждый проход.
            if by_hand:
                forced.remove(order_id)
                save_settings(user_id, settings)
                await self._notify(user_id, _card(
                    "🎮 <b>ROBUX УЖЕ ВЫДАНЫ</b>",
                    [f"Заказ #{_esc(order_id)} выдан раньше — "
                     f"второй раз бот покупать не станет.",
                     "Код лежит в «📜 Журнал выдач»."]))
            return
        log: list = p.setdefault("log", [])
        # Заказ уже в журнале со ссылкой — значит вызов поставщика мог уйти.
        # Повторять его можно только той же ссылкой (см. ниже), а заводить
        # вторую запись нельзя: по ней потом не понять, что покупали.
        already = next((e for e in log if e.get("order") == order_id), None)

        # Снимаем метку сразу: что бы дальше ни случилось, второй раз по
        # ней заходить нельзя. Причину продавец узнает из уведомления.
        if by_hand:
            forced.remove(order_id)
            save_settings(user_id, settings)

        qty = robux_quantity(title)
        region = str(p.get("region") or "GL").upper()

        creds = get_ar_creds(user_id)
        if not creds or not creds.get("api_key"):
            await self._robux_stop(user_id, settings, order_id, qty,
                                   "ключ AppRoute не задан — Плагины → "
                                   "AutoRoblox → 🔑 Поставщик AppRoute")
            return

        loop = asyncio.get_event_loop()
        from automation.approute import order_sync
        ok, catalog = await self._robux_catalog(user_id, creds)
        if not ok:
            await self._robux_stop(user_id, settings, order_id, qty,
                                   f"каталог поставщика не прочитан: {catalog}")
            return

        row, why = match_denomination(catalog, qty, region)
        if not row:
            await self._robux_stop(user_id, settings, order_id, qty, why)
            return

        # Ссылка при повторе берётся из записи, а не считается заново.
        # Считалась она из номера заказа и количества, а количество читается
        # из названия — правка названия на витрине давала другую ссылку, и
        # повтор уходил бы к поставщику как НОВАЯ покупка: `IDEMPOTENCY_REPLAY`
        # не сработал бы, и мы купили бы второй код за свои деньги. То же и с
        # номиналом: покупать надо ровно то, о чём записано намерение.
        reference = str((already or {}).get("reference") or "") \
            or order_reference(order_id, qty)
        denomination = str((already or {}).get("denomination") or "") \
            or row["denomination_id"]
        entry = already or {"order": order_id, "robux": qty,
                            "denomination": denomination,
                            "reference": reference, "region": region,
                            "price": row.get("price"), "at": time.time(),
                            # Чат запоминается: возобновление оборванной
                            # выдачи идёт из журнала, а карточки заказа под
                            # рукой у него нет.
                            "chat": str(chat_id or ""),
                            "codes": [], "state": "собираемся покупать"}
        if entry not in log:
            log.insert(0, entry)
            del log[40:]
        save_settings(user_id, settings)      # намерение — до вызова

        # Сухой прогон тем же телом. Форма выяснена ответами сервера, но
        # последнее звено живым вызовом не подтверждалось: если она всё же
        # неверна, отказ придёт здесь и денег не потратит.
        try:
            dry_ok, dry = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: order_sync(creds, denomination,
                                             1, reference, True)),
                timeout=90)
        except Exception as e:
            dry_ok, dry = False, str(e)[:200]
        if not dry_ok:
            entry["state"] = "сухой прогон отказал"
            entry["why"] = str(dry)[:300]
            save_settings(user_id, settings)
            await self._robux_stop(user_id, settings, order_id, qty,
                                   f"проверка заказа не прошла: {dry}",
                                   record=False)
            return

        await self._robux_finish(user_id, api, settings, entry, creds,
                                 chat_id)

    async def _robux_catalog(self, user_id: int, creds: dict) -> tuple[bool, object]:
        """Каталог поставщика — свой на продавца, живёт две минуты.

        Читался он под каждый заказ заново, а это один ответ на 1263 услуги:
        три оплаченных заказа в одном проходе — три таких чтения подряд, и
        всё это время опрос заказов стоит.

        Срок короткий намеренно. По каталогу берётся остаток (`inStock`), а
        он меняется; но решает не он, а сухой прогон прямо перед покупкой —
        устаревший остаток обернётся честным отказом, а не выдачей того,
        чего нет.
        """
        from automation.approute import services_sync

        # Заводится лениво: менеджер создаётся и в обход `__init__` (так его
        # строят тесты), и полагаться на атрибут из конструктора нельзя.
        cache = getattr(self, "_ar_catalog", None)
        if cache is None:
            cache = {}
            self._ar_catalog = cache
        fresh = cache.get(user_id)
        if fresh and (time.time() - fresh[0]) < 120:
            return True, fresh[1]
        loop = asyncio.get_event_loop()
        try:
            ok, catalog = await asyncio.wait_for(
                loop.run_in_executor(None, services_sync, creds), timeout=90)
        except Exception as e:
            ok, catalog = False, str(e)[:200]
        if ok:
            cache[user_id] = (time.time(), catalog)
        return ok, catalog

    async def _robux_finish(self, user_id: int, api: YooMarketAPI,
                            settings: dict, entry: dict, creds: dict,
                            chat_id: str, dry_first: bool = False) -> None:
        """Купить (если ещё не купили), отправить код, отметить выданным.

        **Единственный путь к деньгам.** Сюда приходят все три входа: новый
        оплаченный заказ, ручная очередь и возобновление оборванной выдачи.
        Второй такой путь пришлось бы снабдить тем же порядком записей —
        намерение до вызова, код до отправки, отметка после — и однажды он
        бы с ним разъехался.

        Покупка не повторяется, если код уже куплен: он лежит в записи, и
        достаточно доотправить. А если покупка была оборвана, повтор идёт
        **той же ссылкой** — на неё поставщик отвечает `IDEMPOTENCY_REPLAY`,
        то есть прежним результатом, а не вторым списанием.
        """
        from automation.approute import order_sync
        from automation.robux import codes_from_result

        order_id = str(entry.get("order") or "")
        qty = int(entry.get("robux") or 0)
        region = str(entry.get("region") or "")
        denomination = str(entry.get("denomination") or "")
        reference = str(entry.get("reference") or "")
        p = settings.setdefault("plugins", {}).setdefault("auto_roblox", {})
        loop = asyncio.get_event_loop()

        codes = [str(c) for c in (entry.get("codes") or []) if c]
        if not codes:
            if dry_first:
                # Запись осталась в состоянии «собираемся покупать» — значит
                # сухой прогон в тот раз не успел пройти, и пропускать его
                # нельзя: он единственное, что стоит между неверной формой
                # тела и потраченными деньгами.
                try:
                    dry_ok, dry = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, lambda: order_sync(creds, denomination, 1,
                                                     reference, True)),
                        timeout=90)
                except Exception as e:
                    dry_ok, dry = False, str(e)[:200]
                if not dry_ok:
                    entry["state"] = "сухой прогон отказал"
                    entry["why"] = str(dry)[:300]
                    save_settings(user_id, settings)
                    await self._robux_stop(user_id, settings, order_id, qty,
                                           f"проверка заказа не прошла: {dry}",
                                           record=False)
                    return

            entry["state"] = "покупаем"
            save_settings(user_id, settings)
            try:
                bought, data = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: order_sync(creds, denomination, 1,
                                                 reference, False)),
                    timeout=120)
            except Exception as e:
                bought, data = False, str(e)[:200]
            if not bought:
                entry["state"] = "поставщик отказал"
                entry["why"] = str(data)[:300]
                save_settings(user_id, settings)
                await self._robux_stop(user_id, settings, order_id, qty,
                                       f"поставщик отказал: {data}",
                                       record=False)
                return

            codes = codes_from_result(data)
            if not codes:
                # Двухсотый без кода — это не выдача. Деньги при этом могли
                # уйти, поэтому молчать нельзя: продавец смотрит кабинет.
                entry["state"] = "ответ без кода"
                save_settings(user_id, settings)
                await self._notify(user_id, _card(
                    "🎮 <b>ROBUX: ОТВЕТ БЕЗ КОДА</b>",
                    [f"Заказ #{_esc(order_id)}, {qty} Robux.",
                     "Поставщик не отказал, но кода не прислал.",
                     f"Ссылка покупки: <code>{_esc(reference)}</code>.",
                     "",
                     "Деньги могли списаться. Посмотрите кабинет по этой "
                     "ссылке и выдайте код вручную, если он там есть."]))
                return

            # Код записывается ДО отправки: чат может быть закрыт, и тогда
            # купленный код не должен остаться никому.
            entry["codes"] = codes
            entry["state"] = "куплен, отправляем"
            save_settings(user_id, settings)

        note = str(p.get("note") or "").strip()
        text = "\n".join(
            [f"Ваш код на {qty} Robux:"]
            + [f"{c}" for c in codes]
            + ["", "Активировать: roblox.com → Пополнить → Использовать код."]
            + ([f"Регион кода: {region}."] if region else [])
            + ([note] if note else []))
        sent, err = await self._send_chat(api, chat_id, text, settings)
        if not sent:
            entry["state"] = "куплен, отправить не смогли"
            entry["why"] = str(err)[:200]
            save_settings(user_id, settings)
            await self._notify(user_id, _card(
                "🎮 <b>ROBUX КУПЛЕН, НО НЕ ОТПРАВЛЕН</b>",
                [f"Заказ #{_esc(order_id)}, {qty} Robux.",
                 f"Причина: {_esc(str(err))}.",
                 "",
                 "Код: " + ", ".join(_esc(c) for c in codes),
                 "",
                 "Передайте его покупателю сами — второй раз бот покупать "
                 "не станет."]))
            return

        # Только теперь. Отметка по факту покупки однажды доложила бы о
        # выдаче, которой покупатель не видел.
        entry["state"] = "выдан"
        delivered: list = p.setdefault("delivered", [])
        if order_id not in delivered:
            delivered.append(order_id)
        # Новые записи в конце, значит лишнее режется с начала. `del [200:]`
        # срезал ровно то, что только что добавили: после двухсотой выдачи
        # отметка не сохранялась вовсе, заказ снова считался невыданным — и
        # следующий проход покупал второй код за наши деньги.
        del delivered[:-200]
        save_settings(user_id, settings)
        logger.info("AutoRoblox: order %s delivered (%s Robux)", order_id, qty)

    async def _robux_forced_sweep(self, user_id: int, api: YooMarketAPI,
                                  settings: dict, tried: set) -> None:
        """Ручная очередь Robux: заказы, до которых обычный проход не доходит.

        Выдача вызывалась в двух местах — когда заказ увиден впервые и когда
        пришла оплата. Оба — про **перемену**. А в очередь продавец ставит
        обычно давно оплаченный заказ, с которым ничего не происходит:
        статус не менялся, значит по нему не срабатывало ничего. Кнопка
        «🚀 Выдать вручную» при этом отвечала «Куплю на ближайшем проходе»
        и обещала отчёт «и если получится, и если нет» — а не делала ровно
        ничего и молчала.

        Заказ дочитывается поимённо, а не ищется в списке: список отдаёт
        свежие заказы, и «нет в списке» не значит «нет такого заказа».
        Не отдал маркетплейс — так и говорим, и очередь освобождаем: висеть
        в ней вечно значит молчать вечно.
        """
        p = settings.get("plugins", {}).get("auto_roblox", {})
        waiting = [o for o in list(p.get("force") or []) if o not in tried]
        if not waiting:
            return
        from orderfields import describe as _describe
        from orderfields import order_chat_id as _chat_of

        for oid in waiting:
            raw, err = {}, ""
            try:
                raw = await api.get_order(oid)
            except Exception as e:
                raw, err = {}, str(e)[:200]
            node = (raw.get("data") if isinstance(raw, dict)
                    and isinstance(raw.get("data"), dict) else raw)
            node = node if isinstance(node, dict) else {}
            if not node:
                queue: list = p.setdefault("force", [])
                if oid in queue:
                    queue.remove(oid)
                save_settings(user_id, settings)
                # Из очереди заказ снимается в обоих случаях — и когда
                # номер неверен, и когда оборвалась связь. Различить их
                # здесь нечем, а оставлять запись «на всякий случай»
                # значит молчать о ней каждый следующий проход.
                await self._robux_stop(
                    user_id, settings, oid, 0,
                    "маркетплейс не отдал заказ с таким номером"
                    + (f" ({err})" if err else "")
                    + ". Проверьте номер; если это был сбой связи — "
                    "поставьте заказ в очередь снова",
                    record=False)
                continue
            d = _describe(node)
            # `describe` номера чата не возвращает вовсе — его достаёт
            # `order_chat_id`. Подставлять номер заказа приходится, когда
            # чата в карточке нет: тогда отправка ответит ошибкой, и код
            # останется у продавца в журнале, а не пропадёт.
            chat_id = _chat_of(node) or oid
            await self._maybe_deliver_robux(
                user_id, api, settings, oid, d.get("title") or "—",
                chat_id, d.get("status") or "")

    async def _robux_resume(self, user_id: int, api: YooMarketAPI,
                            settings: dict, tried: set) -> None:
        """Довести до конца выдачи, оборванные посередине.

        Выдача срабатывает на **перемену**: заказ увиден впервые или пришла
        оплата. А обрыв случается в середине — и чаще всего не от сбоя, а от
        обычного выката: контейнер перезапускается, задача умирает между
        «покупаем» и «выдан». После этого статус заказа больше не меняется
        никогда, значит по нему уже ничего не сработает: деньги потрачены,
        покупатель без кода, продавец без единого слова.

        Повтор безопасен ровно потому, что ссылка та же: у поставщика он
        отвечает `IDEMPOTENCY_REPLAY` — прежним результатом, а не вторым
        списанием. А если код уже куплен и лежит в записи, покупки не будет
        вовсе: останется доотправить.
        """
        from storage import get_ar_creds

        p = settings.get("plugins", {}).get("auto_roblox", {})
        log = list(p.get("log") or [])
        delivered = p.get("delivered") or []
        stuck = [e for e in log
                 if isinstance(e, dict)
                 and str(e.get("state") or "") in _ROBUX_UNFINISHED
                 and str(e.get("order") or "")
                 and str(e.get("order")) not in tried
                 and str(e.get("order")) not in delivered]
        if not stuck:
            return

        creds = get_ar_creds(user_id)
        if not creds or not creds.get("api_key"):
            # Без ключа доводить нечем. Но и молчать нельзя: записи с
            # купленными кодами — это чужие деньги, лежащие без движения.
            for entry in stuck:
                if entry.get("codes"):
                    await self._robux_stop(
                        user_id, settings, str(entry.get("order")),
                        int(entry.get("robux") or 0),
                        "выдача осталась незаконченной, а ключ AppRoute не "
                        "задан. Код уже куплен и лежит в «📜 Журнал выдач» — "
                        "передайте его покупателю", record=False)
            return

        from orderfields import order_chat_id as _chat_of

        for entry in stuck:
            oid = str(entry.get("order"))
            tried.add(oid)
            chat_id = str(entry.get("chat") or "")
            if not chat_id:
                # Записи, сделанные до того, как чат стал запоминаться.
                # Дочитываем заказ поимённо: «нет в списке» не значит
                # «нет такого заказа».
                try:
                    raw = await api.get_order(oid)
                except Exception:
                    raw = {}
                node = (raw.get("data") if isinstance(raw, dict)
                        and isinstance(raw.get("data"), dict) else raw)
                chat_id = _chat_of(node if isinstance(node, dict) else {}) or oid
                entry["chat"] = chat_id
            logger.info("AutoRoblox: доводим оборванную выдачу по заказу %s "
                        "(состояние «%s»)", oid, entry.get("state"))
            await self._robux_finish(
                user_id, api, settings, entry, creds, chat_id,
                # «Собираемся покупать» означает, что сухой прогон в тот раз
                # не успел пройти, — значит он нужен снова.
                dry_first=str(entry.get("state")) == "собираемся покупать")

    async def _robux_stop(self, user_id: int, settings: dict, order_id: str,
                          qty: int, why: str, record: bool = True) -> None:
        """Сказать продавцу, почему выдачи не будет.

        «Ничего не произошло» без причины — самая частая поломка этого
        проекта. Здесь она стоила бы покупателю ожидания оплаченного заказа.
        """
        if record:
            p = settings.setdefault("plugins", {}).setdefault("auto_roblox", {})
            log: list = p.setdefault("log", [])
            log.insert(0, {"order": order_id, "robux": qty, "codes": [],
                           "state": "не выдан", "why": str(why)[:300],
                           "at": time.time()})
            del log[40:]
            save_settings(user_id, settings)
        await self._notify(user_id, _card(
            "🎮 <b>ROBUX НЕ ВЫДАНЫ</b>",
            [f"Заказ #{_esc(order_id)}" + (f", {qty} Robux" if qty else "") + ".",
             f"Причина: {_esc(str(why))}.",
             "",
             "Покупатель ждёт оплаченный заказ — выдайте вручную."]))

    async def _maybe_ask_stars_username(
        self, api: YooMarketAPI, settings: dict,
        order_id: str, title: str, chat_id: str, status: str = "",
    ) -> None:
        from automation.stars import is_stars_order, star_quantity
        from orderfields import is_paid

        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled") or not p.get("ask_username", True):
            return
        if not is_stars_order(title, p.get("keyword") or ""):
            return
        # Заказ «Создан (не оплачен)» — деньги ещё не пришли. Панель на такой
        # прямо предупреждает «не выдавайте товар», а бот начинал выдачу с
        # первого же появления заказа, независимо от статуса. Спросим ник,
        # когда оплата дойдёт: статус меняется — проход это увидит.
        if not is_paid(status):
            logger.info("AutoStars: order %s not paid yet (%s)", order_id, status)
            return
        pending: dict = p.setdefault("pending", {})
        delivered: list = p.setdefault("delivered", [])
        if order_id in pending or order_id in delivered:
            return
        qty = star_quantity(title, p.get("amount", 50))
        pending[order_id] = {"quantity": qty, "asked_at": time.time(),
                             "title": title, "chat_id": chat_id,
                             "reminded": 0}
        await self._send_chat(api, chat_id,
                              stars_text(settings, "ask", qty=qty), settings)
        logger.info("AutoStars: asked username for order %s (qty=%s)", order_id, qty)

    async def _maybe_deliver_stars_reply(
        self, user_id: int, api: YooMarketAPI, settings: dict,
        order_id: str, buyer_text: str, chat_id: str,
    ) -> bool:
        """If this order is awaiting a username, try to deliver. Returns True
        if the message was consumed by the stars flow."""
        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled"):
            return False
        pending: dict = p.get("pending", {})
        entry = pending.get(order_id)
        if not entry:
            return False

        # Возвращённый заказ из очереди не выходил сам. А пока он в ней стоит,
        # ЛЮБОЕ письмо покупателя в этом чате считается ответом с ником: оно
        # не доходит ни до уведомления, ни до правил автоответа. Продавец
        # видит только тишину — ни ошибки, ни записи в журнале.
        closed = _stars_drop_if_closed(settings, order_id)
        if closed:
            await self._notify(
                user_id,
                _card("⭐ <b>ЗАКАЗ ЗАКРЫТ — СНЯТ С ОЧЕРЕДИ</b>",
                      [f"Заказ #{_esc(order_id)}: <b>{_esc(closed)}</b>.",
                       "Звёзд он больше не ждёт, и письма покупателя в этом "
                       "чате снова идут обычным путём."]))
            return False

        username = _extract_username(buyer_text)
        if not username:
            ok, err = await self._send_chat(
                api, chat_id,
                "Не разобрал ник. Пришлите его одним сообщением: латиница, "
                "минимум 5 символов, например durov", settings,
            )
            if not ok:
                # Раньше исход этой отправки не смотрели вовсе: покупатель не
                # получал ни звёзд, ни просьбы прислать ник, а продавец —
                # ни строчки. Сообщение при этом считалось разобранным.
                import autoreply as _ar
                why, _fixable = _ar.explain_error(err)
                await self._notify(
                    user_id,
                    _card("⭐ <b>НЕ СПРОСИЛ НИК</b>",
                          [f"Заказ #{_esc(order_id)}: покупатель написал, но "
                           f"переспросить не вышло.",
                           f"❌ {_esc(why)}"]))
            return True

        qty = int(entry.get("quantity", p.get("amount", 50)))

        # Последняя проверка оплаты — прямо перед тем, как тратить деньги.
        # Между вопросом и ответом покупателя заказ могли отменить или он мог
        # так и остаться неоплаченным; звёзды после этого уже не вернуть.
        from orderfields import describe as _describe, is_paid
        try:
            fresh = await api.get_order(order_id)
            node = (fresh.get("data") if isinstance(fresh, dict)
                    and isinstance(fresh.get("data"), dict) else fresh)
            status_now = _describe(node if isinstance(node, dict) else {})["status"]
        except Exception as e:
            logger.warning("AutoStars: order %s status check: %s", order_id, e)
            status_now = ""
        if status_now and not is_paid(status_now):
            await self._send_chat(
                api, chat_id,
                "⏳ Заказ ещё не оплачен. Как только оплата пройдёт, "
                "звёзды придут автоматически.", settings)
            await self._notify(
                user_id,
                _card("⭐ <b>ЗВЁЗДЫ НЕ ВЫДАНЫ</b>",
                      [f"Заказ #{_esc(order_id)}, @{_esc(username)}, {qty}⭐",
                       "",
                       f"Статус заказа: <b>{_status_ru(status_now)}</b>.",
                       "Выдача не запускалась — деньги за заказ не поступили."]))
            return True

        # Deliver via Fragment in a thread
        from automation.fragment import buy_stars_sync
        from storage import get_fragment_creds
        creds = get_fragment_creds(user_id)
        if not creds or not creds.get("cookies") or not creds.get("mnemonic"):
            await self._notify(
                user_id,
                f"⚠️ <b>AutoStars</b>: заказ #{order_id} — покупатель прислал "
                f"@{username}, но данные Fragment не настроены.\n"
                "Плагины → AutoStars → ⚙️ Настройки → 🔑 Данные Fragment",
            )
            return True

        # Check the wallet can cover it before the buyer is left waiting. A
        # purchase that dies halfway is worse than one that never started: the
        # buyer has already been told it is on its way.
        from automation.stars import deliveries_left, ton_needed
        need = ton_needed(qty)
        loop = asyncio.get_event_loop()
        try:
            from automation.fragment import get_wallet_balance_sync
            ok_bal, bal = await asyncio.wait_for(
                loop.run_in_executor(None, get_wallet_balance_sync,
                                     creds["mnemonic"],
                                     creds.get("wallet_version", "v4r2")),
                timeout=45)
        except Exception as e:
            ok_bal, bal = False, str(e)[:80]
        if ok_bal and isinstance(bal, dict) and bal.get("ton", 0) < need:
            await self._send_chat(
                api, chat_id,
                "⚠️ Не получается выдать звёзды прямо сейчас. "
                "Продавец уже уведомлён и выдаст их вручную.", settings)
            await self._notify(
                user_id,
                _card("⭐ <b>ЗВЁЗДЫ НЕ ВЫДАНЫ</b>",
                      [f"Заказ #{_esc(order_id)}, @{_esc(username)}, {qty}⭐",
                       "",
                       f"На кошельке <b>{bal['ton']:.3f} TON</b>, "
                       f"нужно ≈ <b>{need:.3f} TON</b>.",
                       "Пополните кошелёк и выдайте вручную: "
                       "Плагины → AutoStars → 🚀 Ручная выдача"]))
            pending[order_id] = {**entry, "quantity": qty,
                                 "asked_at": time.time()}
            return True

        await self._send_chat(
            api, chat_id,
            stars_text(settings, "sending", qty=qty, username=username),
            settings)

        # Сколько TON реально ушло — считает сам кошелёк. Иначе «прибыль»
        # пришлось бы прикидывать по курсу из головы.
        spend: dict = {}
        timed_out = False
        try:
            ok, result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, functools.partial(
                        buy_stars_sync,
                        creds["cookies"], creds["mnemonic"], username, qty,
                        creds.get("wallet_version", "v4r2"),
                        # Пусто — значит бот подберёт хеш сам. Прописывать сюда
                        # чужой хеш нельзя: Fragment отвечает «Bad request».
                        creds.get("api_hash", ""),
                        report=spend,
                        proxy=creds.get("proxy", "")),
                ),
                timeout=self._STARS_BUY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            timed_out = True
            ok, result = False, (
                f"покупка не завершилась за "
                f"{int(self._STARS_BUY_TIMEOUT / 60)} мин")
        except Exception as e:
            # У TimeoutError пустой str(), и продавцу приходило «ошибка: » без
            # ошибки. Имя класса некрасиво, но это хотя бы факт.
            ok, result = False, f"ошибка: {str(e)[:100] or type(e).__name__}"

        # Update plugin state
        pending.pop(order_id, None)
        if ok:
            p.setdefault("delivered", []).append(order_id)
            _log_delivery(settings, order_id, qty, username,
                          float(spend.get("ton") or 0.0))
            await self._send_chat(
                api, chat_id,
                stars_text(settings, "done", qty=qty, username=username),
                settings)
            # try to confirm the order automatically
            try:
                await api.confirm_order(order_id)
            except Exception as e:
                logger.warning("AutoStars confirm order %s: %s", order_id, e)
            if stars_notify_on(settings, "done"):
                await self._notify(
                    user_id,
                    f"⭐ <b>AutoStars</b>: выдано {qty}⭐ на @{username} "
                    f"(заказ #{order_id})\n{result}",
                )
        else:
            # Три попытки — и хватит. Раньше заказ возвращался в ожидание без
            # счёта, и каждая новая попытка приносила продавцу ещё одно
            # одинаковое «не удалось»: в ленте их было с десяток подряд.
            tries = int(entry.get("tries", 0) or 0) + 1
            # Деньги уже ушли из кошелька — повторять нельзя ни при каких
            # обстоятельствах: вторая попытка купит звёзды ещё раз, за те же
            # деньги продавца. Сюда попадает и обрыв по таймауту: ожидание
            # seqno длится до двух минут, и оборваться может уже после оплаты.
            if spend.get("sent_onchain"):
                p.setdefault("stuck", {})[order_id] = {
                    "username": username, "quantity": qty,
                    "reason": str(result)[:200], "paid": True,
                    "ton": float(spend.get("ton") or 0.0), "ts": time.time()}
                await self._notify(
                    user_id,
                    f"⚠️ <b>AutoStars</b>: заказ #{order_id}, @{username}, "
                    f"{qty}⭐ — <b>оплата ушла, выдача не подтверждена</b>.\n"
                    f"Списано {float(spend.get('ton') or 0.0):.4f} TON.\n"
                    f"{result}\n\n"
                    "Повтор не делаю: он купил бы звёзды второй раз за ваши "
                    "деньги. Проверьте на fragment.com, начислены ли звёзды, "
                    "и выдайте вручную только если их нет: "
                    "Плагины → AutoStars → 🚀 Ручная выдача",
                )
                return True
            if timed_out:
                # Таймаут — это неизвестность, а не отказ, и обходиться с ним
                # как с отказом нельзя. Ожидание оборвалось на нашей стороне,
                # а поток покупки продолжает работать: он может отправить
                # деньги через секунду после того, как мы решили «не вышло».
                # Отчёт о тратах мы к этому моменту уже прочитали, и повтор
                # через двадцать минут купил бы звёзды второй раз — за те же
                # деньги продавца. Поэтому заказ уходит продавцу, а не в
                # очередь на повтор.
                p.setdefault("stuck", {})[order_id] = {
                    "username": username, "quantity": qty,
                    "reason": str(result)[:200], "unknown": True,
                    "ts": time.time()}
                await self._notify(
                    user_id,
                    f"⚠️ <b>AutoStars</b>: заказ #{order_id}, @{username}, "
                    f"{qty}⭐ — <b>чем кончилась покупка, неизвестно</b>.\n"
                    f"{result}.\n\n"
                    "Деньги могли уйти уже после того, как ожидание "
                    "оборвалось, поэтому повтор не делаю: он купил бы звёзды "
                    "второй раз. Проверьте на fragment.com, начислены ли "
                    "звёзды, и выдайте вручную только если их нет: "
                    "Плагины → AutoStars → 🚀 Ручная выдача",
                )
                return True
            if tries < self._STARS_MAX_TRIES:
                # Ник сохраняем: покупатель прислал его один раз и второй раз
                # не пришлёт. Без этого заказ возвращался в ожидание пустым и
                # ждал сообщения, которого не будет, — автовыдача после первой
                # же неудачи молча умирала.
                pending[order_id] = {**entry, "quantity": qty,
                                     "username": username,
                                     "asked_at": time.time(), "tries": tries,
                                     "retry_at": time.time() + self._STARS_RETRY_AFTER}
                await self._send_chat(
                    api, chat_id,
                    stars_text(settings, "failed", qty=qty, username=username),
                    settings)
            else:
                p.setdefault("stuck", {})[order_id] = {
                    "username": username, "quantity": qty,
                    "reason": str(result)[:200], "ts": time.time()}
            # Уведомление — только о первой неудаче и о том, что попытки
            # кончились. Промежуточные продавцу ничего не добавляют.
            if (stars_notify_on(settings, "failed")
                    and (tries == 1 or tries >= self._STARS_MAX_TRIES)):
                tail = ("\n\n<b>Попытки исчерпаны</b> — заказ ждёт вас."
                        if tries >= self._STARS_MAX_TRIES else "")
                await self._notify(
                    user_id,
                    f"❌ <b>AutoStars</b>: заказ #{order_id}, @{username}, {qty}⭐ — "
                    f"не удалось (попытка {tries}).\n{result}{tail}\n\n"
                    + _stars_failure_hint(result)
                    + "Выдайте вручную: Плагины → AutoStars → 🚀 Ручная выдача",
                )
        return True

    # Через сколько повторить покупку, которая не удалась. Не сразу: отказ
    # чаще всего временный, но повтор через секунду упрётся в ту же причину.
    _STARS_RETRY_AFTER = 20 * 60

    # Сколько ждать покупку целиком. Число одно на всех, кто её зовёт, и
    # живёт рядом с самой цепочкой — см. `fragment.BUY_TIMEOUT_SECS`.
    # Прежние 180 с обрывали покупку в середине, ровно там, где деньги уже
    # могли уйти.
    _STARS_BUY_TIMEOUT = _BUY_TIMEOUT_SECS

    # Сколько раз пытаться выдать звёзды по одному заказу, прежде чем оставить
    # его продавцу. Без счёта каждая неудача возвращала заказ в очередь, и
    # неудачи шли подряд одинаковыми уведомлениями.
    _STARS_MAX_TRIES = 3

    # How long a buyer is given to send their username before being nudged, and
    # how long before the seller is told the order is stuck. A paid order left
    # silently waiting is the worst outcome here: the buyer is out of pocket
    # and nobody knows.
    _STARS_REMIND_AFTER = 1800          # 30 minutes
    _STARS_ESCALATE_AFTER = 6 * 3600

    async def _stars_pending_sweep(self, user_id: int, api: YooMarketAPI,
                                   settings: dict, now: float) -> str:
        """Chase the orders that asked for a username and never got one."""
        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled"):
            return ""
        pending: dict = p.get("pending") or {}
        if not pending:
            return ""
        stuck = []
        dropped: list[str] = []
        for order_id, entry in list(pending.items()):
            if not isinstance(entry, dict):
                continue

            # Закрытый заказ снимается сам, не дожидаясь письма покупателя:
            # иначе очередь копит возвраты, и каждый из них глушит свой чат.
            was = _stars_drop_if_closed(settings, order_id)
            if was:
                dropped.append(f"• #{_esc(str(order_id))} — {_esc(was)}")
                continue

            # Ник известен — ждать нечего, надо повторять покупку. Раньше
            # заказ с уже полученным ником стоял в очереди «ждут username»
            # до эскалации: повтор случался только если покупатель напишет
            # ещё раз, а зачем бы ему.
            known = str(entry.get("username") or "").lstrip("@")
            if known and now >= float(entry.get("retry_at") or 0):
                entry["retry_at"] = now + self._STARS_RETRY_AFTER
                await self._maybe_deliver_stars_reply(
                    user_id, api, settings, order_id, f"@{known}",
                    str(entry.get("chat_id") or ""))
                continue

            waited = now - float(entry.get("asked_at") or 0)
            chat_id = entry.get("chat_id")
            reminded = int(entry.get("reminded") or 0)
            if waited >= self._STARS_REMIND_AFTER and not reminded and chat_id:
                # Ни «@», ни слова «username»: это сообщение возвращается из
                # чата, и раньше бот доставал из него «username» как ответ
                # покупателя. Отпечаток теперь тоже ставится — вторая страховка.
                await self._send_chat(
                    api, chat_id, stars_text(settings, "remind"), settings)
                entry["reminded"] = 1
                entry["reminded_at"] = now
            if waited >= self._STARS_ESCALATE_AFTER and reminded < 2:
                entry["reminded"] = 2
                stuck.append((order_id, entry, waited))
        if dropped and not stuck:
            return _card("⭐ <b>СНЯТЫ С ОЧЕРЕДИ</b>", dropped[:8],
                         "Заказы закрыты — звёзд они больше не ждут, и чаты "
                         "снова читаются обычным путём.")
        if not stuck:
            return ""
        lines = []
        for order_id, entry, waited in stuck[:8]:
            lines.append(f"• #{_esc(str(order_id))} — {entry.get('quantity', '?')}⭐, "
                         f"ждёт {waited / 3600:.0f} ч: "
                         f"{_esc(str(entry.get('title') or ''))[:40]}")
        return _card("⭐ <b>ЗВЁЗДЫ: ЖДУТ USERNAME</b>", lines,
                     "Покупатели не прислали @username. Напишите им или "
                     "выдайте вручную: Плагины → AutoStars")

    async def _stars_session_watch(self, user_id: int, settings: dict,
                                   now: float) -> str:
        """Сказать про истёкшую сессию Fragment до заказа, а не во время.

        Куки Fragment живут недолго — это записано в журнале приёмки как
        отдельная находка: в один и тот же день проба видела личный раздел, а
        через полчаса гостевую страницу с теми же куками. Без этой проверки
        продавец узнавал об истечении в единственный момент, когда поздно:
        покупатель прислал ник, заказ оплачен, звёзды не уходят.

        Молчание — тоже ответ, но только одно: пока сессия жива, сообщений
        нет. Предупреждение приходит один раз на истечение, и один раз —
        когда куки переснимут.
        """
        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled"):
            return ""
        if (now - float(p.get("session_checked_at") or 0)) < _STARS_SESSION_EVERY:
            return ""
        from storage import get_fragment_creds
        creds = get_fragment_creds(user_id)
        if not creds or not creds.get("cookies"):
            return ""
        p["session_checked_at"] = now
        loop = asyncio.get_event_loop()
        try:
            from automation.fragment import session_alive_sync
            alive, why = await asyncio.wait_for(
                loop.run_in_executor(None, session_alive_sync,
                                     creds["cookies"], creds.get("proxy", "")),
                timeout=40)
        except Exception as e:
            logger.info("AutoStars session for %s: %s", user_id, e)
            return ""
        was_dead = bool(p.get("session_dead"))
        p["session_dead"] = not alive
        if alive:
            if was_dead:
                return _card("⭐ <b>СЕССИЯ FRAGMENT СНОВА ЖИВА</b>",
                             [f"Проверил: {_esc(why)}.",
                              "Автовыдача звёзд работает."])
            return ""
        if was_dead:
            return ""                  # уже сказали; повторять каждый час — шум
        return _card("⭐ <b>СЕССИЯ FRAGMENT ИСТЕКЛА</b>",
                     [f"Проверил: {_esc(why)}.",
                      "",
                      "Автовыдача звёзд сейчас не сработает: покупатель "
                      "пришлёт ник, а купить будет нечем.",
                      "",
                      "Переснимите куки: Плагины → AutoStars → ⚙️ Настройки "
                      "→ 🔑 Данные Fragment."])

    async def _stars_balance_watch(self, user_id: int, settings: dict,
                                   now: float) -> str:
        """Warn while there is still time to top up, not at the checkout.

        Running out mid-delivery leaves a buyer waiting on an order they have
        already paid for, so the wallet is looked at on a schedule rather than
        only when an order arrives.
        """
        from automation.stars import deliveries_left, ton_needed

        p = settings.get("plugins", {}).get("auto_stars", {})
        if not p.get("enabled") or not p.get("low_balance_warn", True):
            return ""
        if not stars_notify_on(settings, "low_balance"):
            return ""
        if (now - float(p.get("balance_checked_at") or 0)) < 6 * 3600:
            return ""
        from storage import get_fragment_creds
        creds = get_fragment_creds(user_id)
        if not creds or not creds.get("mnemonic"):
            return ""
        p["balance_checked_at"] = now
        loop = asyncio.get_event_loop()
        try:
            from automation.fragment import get_wallet_balance_sync
            ok, bal = await asyncio.wait_for(
                loop.run_in_executor(None, get_wallet_balance_sync,
                                     creds["mnemonic"],
                                     creds.get("wallet_version", "v4r2")),
                timeout=45)
        except Exception as e:
            logger.info("AutoStars balance for %s: %s", user_id, e)
            return ""
        if not ok or not isinstance(bal, dict):
            return ""
        qty = int(p.get("amount", 50) or 50)
        left = deliveries_left(bal.get("ton", 0), qty)
        floor = int(p.get("low_balance_deliveries", 2) or 2)
        was_low = bool(p.get("balance_low"))
        p["balance_low"] = left <= floor
        if left > floor:
            if was_low:
                return _card("⭐ <b>КОШЕЛЁК ПОПОЛНЕН</b>",
                             [f"{bal['ton']:.3f} TON — хватит примерно на "
                              f"<b>{left}</b> выдач по {qty}⭐"])
            return ""
        if was_low:
            return ""                   # already said so; do not repeat hourly
        return _card("⭐ <b>ЗАКАНЧИВАЕТСЯ TON</b>",
                     [f"На кошельке <b>{bal['ton']:.3f} TON</b>",
                      f"Хватит примерно на <b>{left}</b> выдач по {qty}⭐ "
                      f"(≈ {ton_needed(qty):.3f} TON за выдачу)",
                      "",
                      "Пополните кошелёк, иначе выдача остановится на "
                      "оплаченном заказе."])

    def _pick_message(self, title: str, default: str, rules: list[dict],
                      responders: dict | None = None,
                      ctx: dict | None = None) -> str:
        """Текст автоответа для товара с таким заголовком.

        Побеждает самое длинное совпадение, а не первое по порядку словаря:
        автоответчик для «Steam» перехватывал заказы «Steam Deck», хотя для них
        был заведён свой, — просто потому, что его добавили раньше.
        """
        from autoreply import render
        title_lower = (title or "").lower()
        best: tuple[int, str] | None = None
        for game_name, message in (responders or {}).items():
            key = str(game_name).lower()
            if key and key in title_lower and (best is None or len(key) > best[0]):
                best = (len(key), str(message))
        for rule in rules or []:
            kw = str(rule.get("keyword", "")).lower()
            if kw and kw in title_lower and (best is None or len(kw) > best[0]):
                best = (len(kw), str(rule.get("message", default)))
        return render(best[1] if best else default, ctx)

    async def _enrich_order(self, api: YooMarketAPI, oid: str, d: dict,
                            row: dict | None = None) -> dict:
        """Дочитать заказ, если список отдал одни номера.

        GET /orders отдаёт скупую строку — номер, статус, ссылку на
        объявление, — и экраны показывали «Заказ #1136046» вместо товара и
        покупателя. Карточка заказа знает больше, а название товара при
        необходимости берётся из самого объявления. Дочитывается один раз на
        заказ: результат оседает в известных деталях.

        Ответ приходит завёрнутым в `{"data": …}`, и номер объявления искали
        в самой обёртке — то есть не находили никогда. Дочитывание
        объявления не срабатывало ни разу, а выглядело это как «маркетплейс
        не прислал название»: в уведомлении стоял прочерк. Разворачиваем
        один раз в начале и дальше работаем с содержимым.
        """
        from orderfields import ad_title, describe, order_ad_id

        full: dict = {}
        try:
            full = await api.get_order(oid)
        except Exception as e:
            logger.info("order %s detail: %s", oid, e)
        node = (full.get("data") if isinstance(full, dict)
                and isinstance(full.get("data"), dict) else full)
        node = node if isinstance(node, dict) else {}
        if node:
            deeper = describe(node)
            for key in ("title", "buyer", "username", "quantity"):
                if not d.get(key):
                    d[key] = deeper.get(key)
            # Номер чата есть в карточке заказа, но не в строке списка — ради
            # него дочитывание и нужно в первую очередь.
            from orderfields import order_chat_id
            d["chat_id"] = order_chat_id(node) or d.get("chat_id") or ""
            if d.get("price") in (None, "") and deeper.get("price") is not None:
                d["price"] = deeper["price"]

        # Цена — оттуда же, откуда название. На этом магазине заказ приходит
        # вообще без денежных полей: ни в списке, ни в карточке. Продавец
        # видел «💰 — ₽» в каждом уведомлении и «Сегодня: 6 · 0 ₽» при шести
        # покупках — проверено `/order_debug 1218314`, в ответе только id,
        # ad_id, chat_id, покупатель, статус и время.
        #
        # Цена объявления — не то же самое, что уплаченная: продавец мог
        # поменять её после продажи, да и скидку маркетплейс сюда не
        # передаёт. Поэтому источник запоминается и подписывается на экране,
        # а не выдаётся за сумму заказа.
        if not d.get("title") or d.get("price") is None:
            # Номер объявления есть и в строке списка — если карточка не
            # прочиталась, дочитывать всё равно есть по чему.
            ad_id = order_ad_id(node) or order_ad_id(row or {}) or ""
            if ad_id:
                d["price_tried"] = True
                try:
                    ad = await api.get_ad(ad_id)
                    d["title"] = d.get("title") or ad_title(ad)
                    if d.get("price") is None:
                        from orderfields import ad_price
                        got = ad_price(ad)
                        if got is not None:
                            d["price"], d["price_src"] = got, "ad"
                except Exception as e:
                    logger.info("ad %s for order %s: %s", ad_id, oid, e)
        d["enriched"] = True
        return d

    async def _send_chat(self, api: YooMarketAPI, chat_id: str, text: str,
                         settings: dict | None = None) -> tuple[bool, str]:
        """Отправить сообщение в чат заказа → (получилось, причина).

        Раньше провал уходил только в логи контейнера: покупатель не получал
        ничего, а продавец был уверен, что автоответ работает. Результат
        возвращается, и вызывающий его показывает.

        Отправленное запоминается отпечатком: следующим проходом оно вернётся
        из чата, и без этого бот принимает своё сообщение за письмо покупателя.
        """
        if not (text or "").strip():
            return False, "пустой текст"
        if settings is not None:
            # Отметка ставится до отправки: сообщение может уйти и вернуться
            # быстрее, чем сюда дойдёт ответ API.
            _note_sent_text(settings, chat_id, text)
        try:
            await api.send_message(chat_id, text)
            return True, ""
        except Exception as e:
            logger.warning("Auto chat send failed (chat %s): %s", chat_id, e)
            return False, str(e)[:150] or type(e).__name__

    async def _auto_answer(self, user_id: int, api: YooMarketAPI, settings: dict,
                           *, chat_id: str, order_id: str, text: str,
                           details: dict, is_complaint: bool,
                           batch: int = 1) -> None:
        """Ответить покупателю на его сообщение, если есть чем и можно.

        Бот присылал продавцу уведомление и молчал в чате — ночью покупатель
        ждал до утра. Здесь тот же подбор правил, что показывает экран
        «🧪 Проверка», так что настроенное и отправленное совпадают.
        """
        import autoreply as ar

        conf = ar.cfg(settings)

        def skip(why: str) -> None:
            """Записать, почему промолчали. «Ничего не произошло» — худший из
            возможных ответов на «автоответы не работают»."""
            conf["last_skip"] = {"ts": time.time(), "chat": str(chat_id),
                                 "text": (text or "")[:80], "why": why}
            logger.info("autoreply skipped (chat %s): %s", chat_id, why)

        if not conf.get("enabled"):
            return skip("автоответы выключены")
        if is_complaint and not conf.get("reply_to_complaints"):
            # на жалобу шаблон отвечать не должен — нужен человек
            return skip("это жалоба, а ответ на жалобы выключен")

        rule, matched = ar.pick(conf, text)
        if not rule:
            return skip("ни одно правило не совпало, запасной ответ выключен")
        allowed, why = ar.gate(conf, chat_id, settings=settings)
        if not allowed:
            return skip(why)

        if batch > 1:
            # Остальные письма пачки остались без ответа — намеренно, но
            # молчать об этом нельзя: экран «Почему молчит» иначе покажет
            # только последнее и создаст впечатление, что их и не было.
            conf["last_skip"] = {
                "ts": time.time(), "chat": str(chat_id),
                "text": (text or "")[:80],
                "why": (f"писем за один проход: {batch} — ответил на "
                        f"последнее, оно и несёт вопрос")}

        body = ar.render(rule.get("text", ""),
                         ar.context(details, order_id,
                                    settings.get("shop_name", ""), settings))
        ok, err = await self._send_chat(api, chat_id, body, settings)
        ar.log(conf, chat_id=chat_id, text=body, ok=ok, err=err,
               rule=matched or ("запасной" if rule.get("id") == "fallback" else ""))
        if ok:
            ar.note_sent(conf, chat_id)
            if rule.get("id") != "fallback":
                rule["hits"] = int(rule.get("hits", 0) or 0) + 1
        else:
            # Молчаливый провал — худший исход: продавец считает, что покупателю
            # ответили. Сообщаем, но не чаще раза в час, чтобы упавшее API не
            # превратилось в поток уведомлений.
            why, fixable = ar.explain_error(err)
            skip(f"отправка не прошла: {why}")
            now = time.time()
            if now - float(conf.get("last_fail_notice", 0) or 0) > 3600:
                conf["last_fail_notice"] = now
                # «Ответьте вручную» уместно, только когда вручную и правда
                # можно. На закрытом чате этот совет отправлял продавца
                # биться в стену — маркетплейс не примет и его сообщение.
                tail = ("Покупателю ничего не отправлено — ответьте вручную."
                        if fixable else
                        "Покупателю ничего не отправлено, и написать туда "
                        "уже нельзя — чат закрыт на стороне маркетплейса.")
                await self._notify(
                    user_id,
                    _card("⚠️ <b>АВТООТВЕТ НЕ ДОШЁЛ</b>",
                          [f"💬 Чат <code>{_esc(chat_id)}</code>",
                           f"❌ {_esc(why)}",
                           "",
                           tail]),
                    reply_markup=_message_notify_kb(chat_id, order_id)
                    if fixable else None)

    async def _panel_bump(self, user_id: int, api: YooMarketAPI | None = None,
                          ) -> tuple[int, str]:
        """Promote all listings through the panel.

        Runs only from schedules the owner switched on themselves, so
        confirm=True is passed here; the tariff they picked decides what is
        bought, and the daily spend ceiling caps how many listings a run pays
        for.
        """
        from storage import get_panel_creds
        from automation.panel import panel_bump_all_sync
        from handlers.selenium_settings import (promo_limit, promo_only_ids,
                                                promo_params, promo_price)

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return 0, "нужен вход в панель — откройте «Панель продавца»"

        settings = get_settings(user_id)
        params = promo_params(settings)
        if not params:
            return 0, ("не выбран тариф «Премиум» — откройте "
                       "«Объявления» → «Премиум продвижение» → «Тариф»")

        # Пак, к которому привязано расписание, мог исчезнуть или опустеть.
        # Пустой список товаров означает здесь «поднимать все», так что
        # молчаливый пропуск этой проверки — оплата поднятия всего магазина
        # вместо трёх товаров.
        from handlers.selenium_settings import promo_pack_problem
        problem = promo_pack_problem(settings)
        if problem:
            return 0, problem

        # Not gated on the shop balance: «Премиум» is paid by СБП/card/crypto,
        # not from it, so the balance says nothing about whether this can run.
        caps = [c for c in (promo_limit(settings),) if c]
        loop = asyncio.get_event_loop()
        try:
            count, msg = await asyncio.wait_for(
                loop.run_in_executor(
                    None, panel_bump_all_sync, creds["cookies"], user_id, True,
                    params, min(caps) if caps else 0,
                    promo_only_ids(settings)),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return 0, "панель не ответила вовремя"
        spent = count * promo_price(settings)
        if spent:
            msg += f" · к оплате {spent} ₽"
        return count, msg

    async def _promote_one(self, user_id: int, item_id: str,
                           settings: dict) -> tuple[bool, str]:
        """Pay for one «Премиум» promotion of one listing.

        The position trigger fires for a single listing, so it must pay for
        that one. Routing it through the shop-wide bump would charge for every
        selected listing because one of them slipped — the difference between
        one tariff and thirty.
        """
        from automation.panel import panel_bump_item_sync
        from handlers.selenium_settings import promo_params
        from storage import get_panel_creds

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return False, "нужен вход в панель"
        params = promo_params(settings)
        if not params:
            return False, "не выбран тариф «Премиум»"
        if not item_id:
            return False, "товар не привязан к наблюдению"
        loop = asyncio.get_event_loop()
        try:
            ok, msg = await asyncio.wait_for(
                loop.run_in_executor(None, panel_bump_item_sync,
                                     creds["cookies"], str(item_id), user_id,
                                     True, params),
                timeout=120,
            )
        except asyncio.TimeoutError:
            return False, "панель не ответила вовремя"
        except Exception as e:
            return False, str(e)[:120]
        return bool(ok), str(msg)

    async def _check_reviews(self, user_id: int, settings: dict) -> str:
        """Новые отзывы — из панели.

        В Integration API отзывов нет вовсе: прежний код перебирал /reviews,
        /feedback и /ratings, получал 404 и возвращал пустой список. Тумблер
        включался, и не происходило ничего — ни отзывов, ни объяснения.
        """
        from storage import get_panel_creds
        from automation.panel import panel_reviews_sync

        rm = settings.setdefault("reviews_monitor", {})
        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            # Молча возвращать пусто — это и была прежняя болезнь. Говорим
            # один раз, а не на каждом проходе.
            if not rm.get("warned_no_panel"):
                rm["warned_no_panel"] = True
                return ("⭐ Отзывы читаются из панели, а вход в неё не "
                        "выполнен: Настройки → 🌐 Панель продавца.")
            return ""
        rm.pop("warned_no_panel", None)

        loop = asyncio.get_event_loop()
        try:
            ok, got = await asyncio.wait_for(
                loop.run_in_executor(None, functools.partial(
                    panel_reviews_sync, creds["cookies"],
                    rm.get("resource", ""))),
                timeout=60)
        except Exception as e:
            logger.warning("Reviews: %s", e)
            return ""
        if not ok or not isinstance(got, dict):
            if not rm.get("warned_not_found"):
                rm["warned_not_found"] = True
                return (f"⭐ Отзывы в панели найти не удалось. "
                        f"<i>{_esc(str(got)[:200])}</i>")
            return ""
        rm.pop("warned_not_found", None)
        rm["resource"] = got.get("resource", "")

        reviews = [r for r in got.get("reviews", []) if r.get("id") is not None]
        known = {str(x) for x in (rm.get("known_review_ids") or [])}
        fresh = [r for r in reviews if str(r["id"]) not in known]
        # Первый проход только запоминает: иначе продавец получил бы пачку
        # уведомлений обо всех отзывах за всю историю магазина.
        rm["known_review_ids"] = [str(r["id"]) for r in reviews][:500]
        if not known or not fresh:
            return ""

        for rev in fresh[:5]:
            rating = rev.get("rating")
            stars = ("⭐" * int(rating)) if isinstance(rating, (int, float)) \
                and 1 <= rating <= 5 else "—"
            body = [f"👤 {_esc(rev.get('author') or 'Покупатель')}  {stars}"]
            if rev.get("title"):
                body.append(f"📦 {_esc(rev['title'])}")
            # «Без текста» сказано вслух: отзыв с одной оценкой — обычное
            # дело, а уведомление, оборванное на нике, выглядит как сбой.
            body += ["", f"<i>«{_esc(rev['text'][:300])}»</i>"] \
                if rev.get("text") else ["", "<i>без текста</i>"]
            await self._notify(user_id, _card("⭐ <b>НОВЫЙ ОТЗЫВ</b>", body))
        if len(fresh) > 5:
            return f"⭐ Ещё {len(fresh) - 5} новых отзывов"
        return ""

    async def _check_position(self, user_id: int, settings: dict, now: float,
                              api: YooMarketAPI | None = None) -> str:
        """Walk the watched listings; act on the ones that slipped.

        Returns a notification, or '' to stay quiet. One watch per listing:
        a shop does not have "a position", each listing has its own, on its own
        page. Promotion costs money, so a slip only *pays* when the seller
        switched that on — and even then the cooldown and the daily cap in
        automation/position.py stand between a bad day and an empty card.
        """
        from automation.market import fetch_listing
        from automation.position import (budget_left, evaluate, is_due,
                                         note_position_after, note_promotion,
                                         watches)
        from storage import get_shop_name

        pp = settings.setdefault("promo_position", {})
        # Часовой пояс продавца — в pp: функции лимитов настройки целиком не
        # получают, а сутки им считать надо по его часам.
        import localtime as _lt
        pp["_tz_min"] = _lt.offset_minutes(settings)
        ws = watches(pp)
        if not ws:
            return ""
        shop = get_shop_name(user_id) or ""
        loop = asyncio.get_event_loop()
        blocks: list[str] = []
        checked = 0

        for w in ws:
            if not is_due(w, pp, now):
                continue
            url = (w.get("url") or "").strip()
            if not url:
                continue
            checked += 1
            try:
                ok, res = await asyncio.wait_for(
                    loop.run_in_executor(None, fetch_listing, url, shop,
                                         w.get("category_id")),
                    timeout=120)
            except Exception as e:
                logger.warning("position check for %s: %s", user_id, e)
                ok, res = False, str(e)[:120]
            if not ok:
                # A page that will not load says nothing about the position, so
                # nothing is claimed and nothing is paid. Repeated failures are
                # reported once — silence would let a dead watch look healthy.
                w["last_check"] = now
                w["fails"] = int(w.get("fails") or 0) + 1
                if w["fails"] == 3:
                    blocks.append(
                        f"⚠️ <b>{_esc(_watch_name(w))}</b>\n"
                        f"Страница не читается 3 проверки подряд: "
                        f"{_esc(str(res))[:120]}")
                continue
            w["fails"] = 0

            verdict = evaluate(w, res["offers"], shop=shop, pp=pp, now=now,
                               price=_promo_price(settings))
            # Чем кончилось недавнее поднятие — видно только на следующей
            # проверке. Иначе отчёт умеет сказать «потрачено», но не «помогло».
            if verdict.found:
                note_position_after(pp, str(w.get("item_id") or ""),
                                    int(verdict.pos or 0), now)
            lines = list(verdict.lines)
            if verdict.promote:
                ok_paid, msg = await self._promote_one(
                    user_id, str(w.get("item_id") or ""), settings)
                if ok_paid:
                    price = _promo_price(settings)
                    # Only a promotion that went through is recorded: a refusal
                    # is not a purchase, and charging the day's budget for it
                    # would stop the next real one.
                    note_promotion(w, now, price, pp)
                    w.pop("last_fail", None)
                    left = budget_left(pp, now)
                    lines.append("⭐ Поднял" + (f" · {price} ₽" if price else "")
                                 + (f" (осталось на сегодня {left:.0f} ₽)"
                                    if left >= 0 else "")
                                 + f": {_esc(str(msg))[:160]}")
                else:
                    # A listing that stays down retries every hour, and a
                    # misconfiguration — no tariff, nothing bound — fails the
                    # same way every time. Say it once, and again only when the
                    # answer changes.
                    reason = str(msg)[:160]
                    if w.get("last_fail") != reason:
                        w["last_fail"] = reason
                        lines.append(f"⚠️ Не поднял: {_esc(reason)}")
            if lines:
                head = f"<b>{_esc(_watch_name(w))}</b>"
                if verdict.found:
                    head += f" — {verdict.pos} место"
                blocks.append(head + "\n" + "\n".join(lines))

        if not blocks:
            return ""
        # An over-long send fails whole — including the part that says money was
        # spent — so the message is kept well inside Telegram's 4096 as the
        # watch list grows. Whole blocks are dropped, never a cut mid-tag.
        kept, used = [], 0
        for block in blocks:
            if used + len(block) > 2800 and kept:
                kept.append(f"…и ещё {len(blocks) - len(kept)} — /pos_debug")
                break
            kept.append(block)
            used += len(block)
        return _card("📍 <b>ПОЗИЦИЯ НА ВИТРИНЕ</b>", kept,
                     f"🏪 {_esc(shop)}   ·   проверено: {checked}")

    async def _daily_report_text(self, user_id: int, api: YooMarketAPI,
                                 settings: dict) -> str:
        """The end-of-day summary, as today's numbers rather than lifetime ones.

        The figures come from the panel's own ledger, with the bot's order
        history as the fallback: built from local tracking alone, the report
        counted only what the poller happened to witness, so a restart or a
        sale made before the bot came up simply vanished from the day.

        The balance line used to read an undefined name — the exception was
        caught and every report went out saying «—». It is passed in now.
        """
        from stats_source import LOCAL, day_start, events_for, summarize

        events, source, panel_err = await events_for(user_id, settings, force=True)
        d0 = day_start(None, settings)
        st = summarize(events, d0)

        spent_today = st["spend"]
        if not spent_today and source == LOCAL:
            bs = settings.get("bump_schedule", {})
            import localtime as _lt
            if bs.get("spent_day") == _lt.today_str(settings):
                spent_today = float(bs.get("spent_today", 0) or 0)

        bal_why: list[str] = []
        try:
            _bal, balance_str = await shop_balance(user_id, api, bal_why)
        except Exception as e:
            logger.warning("Daily report balance for %s: %s", user_id, e)
            balance_str, bal_why = "—", [str(e)[:150]]

        net = st["revenue"] - spent_today
        body = [
            f"🛒 Заказов за день: <b>{st['orders']}</b>",
            # `summarize` называет это поле `refunds`. Опечатка «refunded»
            # роняла отчёт на KeyError, а исключение ловилось строкой выше и
            # уходило в лог контейнера — то есть итоги дня не приходили
            # никогда и молча. Пункт B5 чеклиста ровно про этот случай.
            f"✅ Продаж: <b>{st['sales']}</b>"
            + (f"   ↩️ Возвраты: {st['refunds']}" if st['refunds'] else ""),
            "",
            f"💵 Выручка: <b>{_money(st['revenue'])} ₽</b>",
        ]
        if spent_today:
            body.append(f"⬆️ Продвижение: −{_money(spent_today)} ₽")
            body.append(f"🟰 Чистыми: <b>{_money(net)} ₽</b>")
        if st["payout_count"]:
            body.append(f"💸 Выведено: <b>{_money(st['payouts'])} ₽</b>")
        body.append("")
        body.append(f"💰 Баланс сейчас: <b>{balance_str}</b>")
        # Прочерк без причины — это «ничего не произошло» без объяснения.
        # Причину панель называет, её просто теряли по дороге.
        if balance_str == "—" and bal_why:
            import html as _hb
            body.append(f"<i>панель: {_hb.escape(bal_why[0][:150])}</i>")
        if not st["orders"]:
            body.append("")
            body.append("<i>Сегодня продаж не было.</i>")
        if source == LOCAL:
            body.append("")
            body.append("<i>Считано по истории бота — панель не ответила"
                        + (f" ({_esc(panel_err)})" if panel_err else "") + ".</i>")

        return _card("📊 <b>ИТОГИ ДНЯ</b>", body,
                     f"🗓 {__import__('localtime').now(settings).strftime('%d.%m.%Y')}")

    async def _panel_republish(self, user_id: int, rows: list[dict]
                               ) -> tuple[list[dict], list[dict]]:
        return await panel_republish(user_id, rows)

    async def _restore_after_sale(self, user_id: int, api: YooMarketAPI,
                                  settings: dict, sold_ads: set, sold_now: bool
                                  ) -> None:
        """Put a listing back on sale the moment it sells.

        The scheduled pass runs on an interval — an hour by default — so a
        listing that sold out sat off-sale for up to that long. A sale is the
        one event that reliably means "this may have gone down", so it drives
        the restore directly, in the 60s orders loop.

        Two paths. When the order says which listing it was, that listing alone
        is republished: one detail fetch and one publish, no full sweep, and no
        throttle needed. When the order carries no listing id, a normal restore
        pass is used instead — that costs a full listing fetch, so it is capped
        to once every few minutes rather than running every time an order lands
        during a rush.
        """
        ar = settings.setdefault("auto_restore", {})
        if not (ar.get("enabled") and ar.get("instant", True)):
            return

        now = time.time()
        from handlers.panel_items import _deleted_ids
        skip = _deleted_ids(user_id) | {
            aid for aid, f in (ar.get("failures") or {}).items()
            if float(f.get("until", 0) or 0) > now}

        back: list[str] = []
        for aid in sorted(sold_ads - set(skip)):
            try:
                ad = await api.get_ad(aid)
                inner = ad.get("data") or ad
                state = api._ad_state(inner)
                if state not in api._DOWN:
                    continue                       # still live, nothing to do
                if ar.get("require_stock", True):
                    has, _note = await api.ad_stock(aid, inner)
                    if not has:
                        continue                   # sold out — nothing to sell
                title = str(inner.get("title") or inner.get("name") or f"#{aid}")
                try:
                    await api.restore_ad(aid)
                except Exception as e:
                    # Same dead end as the scheduled pass: the API won't publish
                    # out of «unpublish». Take the panel route instead.
                    if "incorrect_status" not in str(e):
                        raise
                    done, _still = await self._panel_republish(
                        user_id, [{"id": str(aid), "title": title, "reason": str(e)}])
                    if not done:
                        continue
                back.append(title)
            except Exception as e:
                logger.info("Instant restore of %s: %s", aid, e)

        if back:
            ar["restored_total"] = int(ar.get("restored_total", 0) or 0) + len(back)
            await self._notify(
                user_id,
                _card("♻️ <b>СНОВА В ПРОДАЖЕ</b>",
                      [f"• {_esc(t)[:44]}" for t in back[:8]]
                      + ["", "<i>Вернул сразу после продажи.</i>"]))
            return

        # No listing id in the order — fall back to a normal pass, rate-limited.
        if sold_now and now - float(ar.get("last_instant_run", 0) or 0) >= 300:
            ar["last_instant_run"] = now
            note = await self._auto_restore(user_id, api, settings, now)
            if note:
                await self._notify(user_id, note)

    async def _auto_restore(self, user_id: int, api: YooMarketAPI,
                            settings: dict, now: float) -> str:
        """One scheduled restore pass. Returns a notification, or '' to stay quiet.

        Quiet matters here: this runs on a timer, and "нечего восстанавливать"
        every hour is noise that trains the seller to ignore the channel. Only
        real events speak — ads that went back up, and refusals not already
        reported.
        """
        ar = settings.setdefault("auto_restore", {})
        failures: dict = ar.setdefault("failures", {})

        # An ad the marketplace refused is not retried immediately: the reason
        # rarely changes within the hour, and a schedule would otherwise repeat
        # the same rejected call forever. The wait doubles, up to a day.
        held = {aid for aid, f in failures.items()
                if float(f.get("until", 0) or 0) > now}

        from handlers.panel_items import _deleted_ids
        skip = set(held) | _deleted_ids(user_id)

        # Statuses barred by a past incorrect_status, minus the ones whose ban
        # has aged out and is due for a re-test.
        barred_until: dict = _barred_map(ar)
        active_barred = [st for st, until in barred_until.items() if until > now]

        try:
            rep = await api.restore_ads(
                require_stock=bool(ar.get("require_stock", True)),
                skip_ids=skip,
                limit=_RESTORE_MAX_PER_PASS,
                skip_statuses=active_barred)
        except Exception as e:
            logger.warning("Auto-restore for %s: %s", user_id, e)
            return f"🔄 Авто-восстановление не отработало: {_esc(str(e)[:120])}"

        ar["last_restore_run"] = now

        # Anything the API refused because of the listing's state gets a second
        # try through the panel, which can make that transition when the API
        # cannot. Recovered listings move into `restored` and are reported as
        # normal successes — the route taken is an implementation detail.
        via_panel: list[dict] = []
        retryable = [r for r in rep["failed"]
                     if "incorrect_status" in str(r.get("reason", ""))]
        if retryable:
            via_panel, still = await self._panel_republish(user_id, retryable)
            rep["restored"] += via_panel
            rep["failed"] = [r for r in rep["failed"] if r not in retryable] + still

        for row in rep["restored"]:
            failures.pop(str(row["id"]), None)      # it worked; forget the past
        ar["restored_total"] = int(ar.get("restored_total", 0)) + len(rep["restored"])

        # Learn which statuses the marketplace itself refuses to publish, so
        # the next pass skips them instead of re-asking every hour. This is the
        # answer coming from the marketplace, not a guess about its states.
        # Each ban carries an expiry: it is inferred from a single ad and
        # applied to every ad in that state, so it must be able to heal.
        #
        # A status the panel just published from is not barred, and any
        # standing ban on it is lifted. The API refusing `unpublish` is the
        # normal case the panel fallback exists to handle — barring it would
        # switch off restore for the one state it is built around, and every
        # later pass would skip in silence.
        # A pass that never got a verdict out of the panel — no login, or the
        # listing not located there — says nothing about any status. Bans
        # standing from such a pass were never evidence, so they are dropped
        # rather than holding restore back for a week after the cause is fixed.
        if any(r.get("panel") in ("unreached", "not_found")
               for r in rep["failed"]):
            barred_until.clear()
        recovered = {str(r.get("status") or "").lower() for r in via_panel}
        for st in recovered:
            barred_until.pop(st, None)
        for row in rep["failed"]:
            reason = str(row.get("reason", ""))
            if "incorrect_status" not in reason:
                continue
            # Only the panel actually refusing the action is evidence about
            # the status. Matching on the reason text was tried and let «в
            # панели не нашёл этот товар» through, which barred `unpublish` —
            # the one state restore exists for — over a lookup problem.
            if row.get("panel") != "refused":
                continue
            st = str(row.get("status") or "").lower()
            if st and st not in recovered:
                barred_until[st] = now + _RESTORE_BARRED_TTL
        ar["barred_until"] = barred_until
        ar.pop("barred_statuses", None)          # superseded by the dated form

        fresh_failures = []
        for row in rep["failed"]:
            aid = str(row["id"])
            prev = failures.get(aid) or {}
            tries = int(prev.get("tries", 0)) + 1
            # 1h, 2h, 4h ... capped at a day
            wait = min(3600 * (2 ** (tries - 1)), 86400)
            failures[aid] = {"tries": tries, "until": now + wait,
                             "reason": row["reason"], "title": row["title"]}
            if prev.get("reason") != row["reason"]:
                fresh_failures.append(row)          # only new news is reported

        # Keep the memory from growing without bound
        for aid in [a for a, f in failures.items()
                    if float(f.get("until", 0) or 0) < now - 7 * 86400]:
            failures.pop(aid, None)
        ar["failures"] = failures

        # A status in neither list is the signal that the marketplace has a
        # state this code doesn't know about — exactly how «unpublish» hid for
        # so long. Reporting it only when something *else* happened defeats the
        # purpose, so a status not seen before counts as news in its own right.
        # Each distinct status is announced once, not every hour.
        # Taken down by hand: the marketplace will not publish these back, so
        # they are named once and then left alone. Reporting them every pass
        # would be an hourly error about something only the seller can undo.
        told_manual = set(str(x) for x in (ar.get("told_manual") or []))
        manual_new = [r for r in (rep.get("manual") or [])
                      if str(r["id"]) not in told_manual]
        if manual_new:
            ar["told_manual"] = sorted(told_manual
                                       | {str(r["id"]) for r in manual_new})

        seen_unknown = set(ar.get("seen_unknown") or [])
        new_unknown = sorted({str(r["status"]) for r in rep.get("unknown") or []}
                             - seen_unknown)
        if new_unknown:
            ar["seen_unknown"] = sorted(seen_unknown | set(new_unknown))

        # Listings left over because the pass is capped; picked up next round
        # rather than after another full interval.
        backlog = max(0, int(rep.get("candidates", 0)) - _RESTORE_MAX_PER_PASS)
        if backlog:
            ar["last_restore_run"] = 0

        summary = (f"поднято {len(rep['restored'])}, "
                   f"без остатков {len(rep['no_stock'])}, "
                   f"отказов {len(rep['failed'])}")
        ar["last_result"] = summary

        if (not rep["restored"] and not fresh_failures and not new_unknown
                and not manual_new):
            return ""                                # nothing happened, say nothing

        body: list[str] = []
        if rep["restored"]:
            body.append(f"✅ Снова в продаже: <b>{len(rep['restored'])}</b>")
            for row in rep["restored"][:8]:
                body.append(f"   • {_esc(row['title'])[:40]}")
            if via_panel:
                body.append(f"   <i>из них через панель: {len(via_panel)}</i>")
            body.append("")
            body.append("<i>Опубликованное уходит на модерацию — "
                        "статус сменится после проверки.</i>")
        if rep["no_stock"]:
            body.append("")
            body.append(f"📦 Без остатков: <b>{len(rep['no_stock'])}</b> "
                        f"— их публиковать нечем")
            for row in rep["no_stock"][:5]:
                body.append(f"   • {_esc(row['title'])[:34]} — {_esc(row.get('note'))}")
        if manual_new:
            body.append("")
            body.append(f"✋ <b>Сняты вручную: {len(manual_new)}</b> — "
                        f"Юмаркет возвращает в продажу только истёкшие, "
                        f"эти нужно вернуть самому на сайте:")
            for row in manual_new[:6]:
                body.append(f"   • {_esc(row['title'])[:40]}")

        if manual_new:
            body.append("")
            body.append(f"✋ <b>Сняты вручную: {len(manual_new)}</b>")
            for row in manual_new[:6]:
                body.append(f"   • {_esc(row['title'])[:40]}")
            body.append("<i>Юмаркет возвращает в продажу только истёкшие "
                        "объявления. Снятые вручную нужно вернуть самому — "
                        "бот их больше не трогает.</i>")

        if new_unknown:
            affected = [r for r in rep["unknown"] if str(r["status"]) in new_unknown]
            body.append("")
            body.append("❔ <b>Незнакомый статус</b> — не трогал: "
                        + ", ".join(_esc(x) for x in new_unknown[:6]))
            for row in affected[:4]:
                body.append(f"   • {_esc(row['title'])[:38]}")
            body.append("<i>Напишите нам этот статус, если такие товары "
                        "должны возвращаться в продажу.</i>")
        if fresh_failures:
            body.append("")
            body.append(f"⛔ Маркетплейс отказал: <b>{len(fresh_failures)}</b>")
            for row in fresh_failures[:5]:
                # The status is the whole point of an incorrect_status refusal
                # — without it the seller can't tell what to change.
                st = _esc(str(row.get("status") or "?"))
                body.append(f"   • {_esc(row['title'])[:26]}  <code>{st}</code>\n"
                            f"     {_esc(row['reason'])[:400]}")
            body.append("<i>Повтор будет позже — с нарастающей паузой.</i>")

        if backlog:
            body.append("")
            body.append(f"⏳ Ещё <b>{backlog}</b> в очереди — продолжу "
                        f"в следующий заход.")

        return _card("🔄 <b>АВТО-ВОССТАНОВЛЕНИЕ</b>", body,
                     f"📦 Всего объявлений: {rep['total']}")

    async def _check_panel_session(self, user_id: int, settings: dict, now: float) -> None:
        """Warn once when the stored panel session stops working.

        Checked at most every 6 hours, and the warning is sent only on the
        transition from working to dead, so a logged-out user is not nagged.
        """
        from storage import get_panel_creds

        creds = get_panel_creds(user_id)
        if not creds or not creds.get("cookies"):
            return

        state = settings.setdefault("panel_session", {})
        if now - state.get("last_check", 0) < 6 * 3600:
            return
        state["last_check"] = now

        from automation.panel import panel_check_session_sync
        loop = asyncio.get_event_loop()
        ok, _detail = await asyncio.wait_for(
            loop.run_in_executor(None, panel_check_session_sync, creds["cookies"]),
            timeout=30,
        )
        was_ok = state.get("ok", True)
        state["ok"] = ok

        if not ok and was_ok:
            b = InlineKeyboardBuilder()
            b.button(text="📧 Войти по коду", callback_data="panel:sms_start")
            b.adjust(1)
            await self._notify(
                user_id,
                "⚠️ <b>Сессия панели истекла</b>\n\n"
                "Создание товаров и управление объявлениями сейчас недоступны. "
                "Войдите заново — код придёт на почту.",
                reply_markup=b.as_markup(),
            )

    async def _notify(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
            _note_sent_notification(text)
            return
        except Exception as e:
            logger.warning("Notify failed (user %s): %s", user_id, e)
            # Повторять можно только то, что Telegram ТОЧНО не принял.
            # Раньше повтор шёл на любую ошибку, включая таймаут: сообщение
            # при этом уже доставлено, и продавец получал его дважды — второй
            # раз без разметки. Разрыв связи не значит «не дошло».
            if not _is_formatting_error(e):
                return
        # A notification is worth more unformatted than not at all: if the HTML
        # was rejected, strip the tags and send it as plain text.
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await self.bot.send_message(user_id, plain, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Notify plain fallback failed (user %s): %s", user_id, e)

    # ------------------------------------------------------------------
    # Auto-features loop (separate from the orders loop)
    # ------------------------------------------------------------------

    async def _auto_loop(self, user_id: int) -> None:
        """Background loop for the auto features (bump / restore / withdraw)."""
        # Initial delay so it doesn't run immediately on startup
        await asyncio.sleep(60)
        while True:
            try:
                await self._tick_auto(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Auto-task error for user %s: %s", user_id, e)
            await asyncio.sleep(_AUTO_LOOP_INTERVAL)

    async def _tick_auto(self, user_id: int) -> None:
        """Run auto-bump / auto-restore / auto-withdraw via the Integration API."""
        token = get_token(user_id)
        if not token:
            return
        async with self._lock(user_id):
            await self._tick_auto_locked(user_id, token)

    async def _tick_auto_locked(self, user_id: int, token: str) -> None:
        settings = get_settings(user_id)
        now = time.time()
        # Второй цикл тоже рассылает — и тоже только у владельца аренды.
        if not _claim_sender(settings, now):
            return
        messages = []

        api = YooMarketAPI(token)
        await api.start()
        try:
            # --- Auto-bump ---
            ab = settings.get("auto_bump", {})
            if ab.get("enabled"):
                interval_hours = ab.get("interval_hours", 24)
                last_run = ab.get("last_bump_run", 0)
                if (now - last_run) / 3600 >= interval_hours:
                    # Bumping runs against the panel, not the Integration API:
                    # the API has no such method, the panel exposes it as a
                    # Nova action.
                    logger.info("Auto-bump for user %s via panel", user_id)
                    count, msg = await self._panel_bump(user_id, api)
                    settings["auto_bump"]["last_bump_run"] = now
                    messages.append(f"⬆️ Авто-поднятие: {msg}")

            # --- AutoStars: stuck orders and a wallet that is running out ---
            if settings.get("plugins", {}).get("auto_stars", {}).get("enabled"):
                for note in (await self._stars_pending_sweep(
                                 user_id, api, settings, now),
                             await self._stars_balance_watch(
                                 user_id, settings, now),
                             await self._stars_session_watch(
                                 user_id, settings, now)):
                    if note:
                        messages.append(note)

            # --- Position watch ---
            if settings.get("promo_position", {}).get("enabled"):
                note = await self._check_position(user_id, settings, now, api)
                if note:
                    messages.append(note)

            # --- Auto-restore ---
            ar = settings.get("auto_restore", {})
            if ar.get("enabled"):
                interval = float(ar.get("interval_hours", 1) or 1)
                if (now - float(ar.get("last_restore_run", 0) or 0)) / 3600 >= interval:
                    logger.info("Auto-restore for user %s via API", user_id)
                    note = await self._auto_restore(user_id, api, settings, now)
                    if note:
                        messages.append(note)

            # --- Auto-withdraw ---
            # --- Panel session health ---
            # Panel operations (product creation, item management) run on
            # cookies that expire silently; without this the user only finds
            # out when an action fails mid-use.
            try:
                await self._check_panel_session(user_id, settings, now)
            except Exception as e:
                logger.warning("Panel session check failed for %s: %s", user_id, e)

            # --- Balance notify ---
            bn = settings.get("balance_notify", {})
            if bn.get("enabled"):
                threshold = float(bn.get("threshold", 1000) or 0)
                last_bal = float(bn.get("last_notified_balance", 0.0) or 0)
                try:
                    balance, balance_str = await shop_balance(user_id, api)
                    # An unreadable balance must not be recorded as 0: that would
                    # re-arm the alert and fire a false "crossed the threshold"
                    # the moment a real number came back.
                    if balance_str != "—":
                        settings["balance_notify"]["last_notified_balance"] = balance
                        # Edge-triggered: fire once as the balance crosses up to
                        # the threshold, re-arm only after it drops back below.
                        if balance >= threshold > last_bal:
                            await self._notify(
                                user_id,
                                _card("🔔 <b>БАЛАНС ДОСТИГ ПОРОГА</b>",
                                      [f"💰 На счету: <b>{balance_str}</b>",
                                       f"🎯 Порог: {_money(threshold)} ₽",
                                       "",
                                       "Можно выводить средства."]),
                                reply_markup=_balance_notify_kb(),
                            )
                except Exception as e:
                    logger.warning("Balance notify error for user %s: %s", user_id, e)

            # --- Daily report ---
            dr = settings.get("daily_report", {})
            if dr.get("enabled"):
                report_hour = dr.get("hour", 20)
                import localtime as _lt
                today_str = _lt.today_str(settings)
                last_day = dr.get("last_report_day", "")
                if last_day != today_str and _lt.hour(settings) >= report_hour:
                    try:
                        await self._notify(
                            user_id,
                            await self._daily_report_text(user_id, api, settings),
                            reply_markup=_balance_notify_kb())
                        # Marked only once it actually went out. Marking first
                        # meant a panel timeout or a failed send burned the day:
                        # the report was recorded as delivered and never
                        # retried, which is «статистика не приходит» exactly.
                        settings["daily_report"]["last_report_day"] = today_str
                    except Exception as e:
                        logger.warning("Daily report error for user %s: %s", user_id, e)
                        # Отчёт, который не пришёл, продавцу невидим: он
                        # решит, что за день не было продаж. Так и жила
                        # опечатка `refunded` — сводка падала на KeyError,
                        # исключение уходило в лог контейнера, и «итоги дня»
                        # не приходили молча. Раз в день говорим об этом.
                        if settings["daily_report"].get("last_fail_day") != today_str:
                            settings["daily_report"]["last_fail_day"] = today_str
                            try:
                                import html as _h
                                await self._notify(
                                    user_id,
                                    "⚠️ <b>Итоги дня не собрались</b>\n\n"
                                    f"Причина: <code>{_h.escape(str(e)[:150])}"
                                    "</code>\n\n"
                                    "Цифры за день целы — они в разделе "
                                    "«Статистика». Сломалась только сводка.")
                            except Exception:
                                pass

            # --- Reviews monitor ---
            rm = settings.get("reviews_monitor", {})
            if rm.get("enabled"):
                try:
                    note = await self._check_reviews(user_id, settings)
                    if note:
                        messages.append(note)
                except Exception as e:
                    logger.warning("Reviews monitor error for user %s: %s",
                                   user_id, e)

            # --- Bump scheduler ---
            # Moved to the fast 60s loop (_maybe_bump_schedule) so slots fire at
            # their exact time instead of drifting with this 30-min loop.

        except Exception as e:
            logger.error("Auto-tasks error for user %s: %s", user_id, e)
            messages.append(f"❌ Ошибка авто-задач: {e}")
        finally:
            await api.close()

        # Always persist: balance_notify / daily_report / reviews_monitor update
        # their dedupe state (last_notified_balance, last_report_day,
        # known_review_ids) WITHOUT appending to `messages`. Saving only when
        # `messages` was non-empty lost that state every cycle → repeated spam.
        save_settings(user_id, settings)

        if messages:
            await self._notify(
                user_id,
                "🤖 <b>Авто-задачи</b>\n\n" + "\n".join(messages),
            )
