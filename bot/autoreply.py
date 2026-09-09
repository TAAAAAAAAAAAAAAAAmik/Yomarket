"""Автоответы: что отправить покупателю, когда и стоит ли вообще.

Раньше автоответ был один — на новый заказ, — а на сообщения покупателя бот
только присылал уведомление продавцу. Ночью или в отъезде покупатель сидел без
ответа. Здесь живёт вся логика: подбор ответа по ключевым словам, подстановка
данных заказа, пауза между ответами и лимит на заказ, чтобы автоответ не
превратился в спам.

Логика вынесена из обработчиков и из фонового цикла в одно место, потому что
её нужно проверять: экран «🧪 Проверка» гоняет ровно эти функции и показывает
продавцу, что именно бот ответит на конкретное сообщение.
"""
from __future__ import annotations

import re
import time
import uuid

# Подстановки в тексте ответа: {товар}, {покупатель}, … Ключ — то, что пишет
# продавец, значение — поле контекста заказа.
PLACEHOLDERS: dict[str, str] = {
    "товар": "title",
    "покупатель": "buyer",
    "цена": "price",
    "заказ": "order_id",
    "магазин": "shop",
    "время": "time",
    "количество": "quantity",
    # те же поля по-английски — привычка тех, кто настраивал ботов раньше
    "item": "title",
    "title": "title",
    "buyer": "buyer",
    "price": "price",
    "order": "order_id",
    "shop": "shop",
    "time": "time",
    "qty": "quantity",
}

# То, что показываем продавцу как подсказку. Английские синонимы работают, но
# в списке их нет — короткая подсказка полезнее полной.
HINT = "{товар} · {покупатель} · {цена} · {заказ} · {магазин} · {время}"

DEFAULTS: dict = {
    "enabled": False,
    "rules": [],                 # [{id, on, keywords: [...], text, hits}]
    "fallback": {
        "on": False,
        "text": "Здравствуйте! Сообщение получил, отвечу в ближайшее время.",
    },
    "cooldown_min": 30,          # не чаще одного автоответа в чат за N минут
    "max_per_order": 3,          # и не больше N автоответов на один заказ
    "quiet_only": False,         # отвечать только вне рабочего времени
    "from_hour": 22,             # начало «нерабочего» окна
    "to_hour": 9,                # конец окна
    "reply_to_complaints": False,
    "state": {},                 # {chat_id: {"last": ts, "count": n}}
    "log": [],                   # последние отправки, с результатом
}

_LOG_MAX = 30
_STATE_MAX = 200


def cfg(settings: dict) -> dict:
    """Настройки автоответов с проставленными значениями по умолчанию."""
    import copy
    c = settings.setdefault("autoreplies", {})
    for key, value in DEFAULTS.items():
        if key not in c:
            c[key] = copy.deepcopy(value)
    return c


# ─────────────────────────────── текст ───────────────────────────────

def render(text: str, ctx: dict | None = None) -> str:
    """Подставить данные заказа в шаблон.

    Неизвестное в фигурных скобках остаётся как есть: текст с «{» внутри — это
    опечатка продавца, а не повод отправить покупателю обрубок.
    """
    if not text:
        return ""
    ctx = ctx or {}

    def sub(m: re.Match) -> str:
        key = m.group(1).strip().lower()
        field = PLACEHOLDERS.get(key)
        if not field:
            return m.group(0)
        value = ctx.get(field)
        if value in (None, "", "—"):
            return m.group(0) if field not in ("quantity",) else ""
        return str(value)

    return re.sub(r"\{([^{}]{1,20})\}", sub, text)


def context(details: dict | None = None, order_id: str = "",
            shop: str = "", settings: dict | None = None) -> dict:
    """Контекст подстановки из того, что бот уже знает о заказе.

    {время} — часы продавца: покупателю уходило время сервера, то есть
    чужое."""
    import localtime as _lt
    d = details or {}
    return {
        "title": d.get("title") or "",
        "buyer": d.get("buyer") or "",
        "price": d.get("price") or "",
        "quantity": d.get("quantity") or "",
        "order_id": str(order_id or ""),
        "shop": shop or "",
        "time": _lt.now(settings).strftime("%H:%M"),
    }


# ─────────────────────────────── подбор ───────────────────────────────

def _norm(text: str) -> str:
    """Ключевые слова ищутся по «мягкому» тексту: без регистра, ё=е и знаков.

    Покупатель пишет «Здравствуйте!!! где ключ?» — правило со словом «где ключ»
    должно сработать.
    """
    low = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я@#+]+", " ", low).strip()


def _best_match(rules: list, haystack: str, weak: bool
                ) -> tuple[int, dict, str] | None:
    best: tuple[int, dict, str] | None = None
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("on", True):
            continue
        if bool(rule.get("weak")) != weak:
            continue
        if not (rule.get("text") or "").strip():
            continue
        for kw in rule.get("keywords") or []:
            needle = _norm(str(kw))
            if not needle or needle not in haystack:
                continue
            if best is None or len(needle) > best[0]:
                best = (len(needle), rule, str(kw))
    return best


def pick(conf: dict, text: str) -> tuple[dict | None, str]:
    """Правило для входящего сообщения и совпавшее слово.

    Два уровня. Сначала обычные правила, среди них выигрывает самое длинное
    совпадение: иначе правило «ключ» перехватывало бы сообщения, ради которых
    заведено «ключ не подошёл».

    Приветствия помечены как «слабые» и разбираются вторым заходом. Иначе
    «Здравствуйте, а где ключ?» уходило бы в приветствие — слово «здравствуйте»
    просто длиннее, чем «ключ», а ответить надо про ключ.
    """
    haystack = _norm(text)
    if not haystack:
        return None, ""
    rules = conf.get("rules") or []
    best = _best_match(rules, haystack, weak=False) or \
        _best_match(rules, haystack, weak=True)
    if best:
        return best[1], best[2]
    fb = conf.get("fallback") or {}
    if fb.get("on") and (fb.get("text") or "").strip():
        return {"id": "fallback", "text": fb.get("text"), "on": True,
                "keywords": []}, ""
    return None, ""


# ─────────────────────────── когда можно отвечать ───────────────────────────

def in_quiet_window(conf: dict, now: float | None = None,
                    settings: dict | None = None) -> bool:
    """Сейчас «нерабочее» время (окно может переходить через полночь).

    Часы — продавца, а не сервера. Контейнер живёт по UTC, и окно
    «22:00—09:00» у продавца из UTC+5 работало с 03:00 до 14:00: бот молчал
    весь его вечер и отвечал среди рабочего дня.
    """
    import localtime as _lt
    if now is not None:
        hour = _lt.to_local(now, settings).hour
    else:
        hour = _lt.hour(settings)
    start = int(conf.get("from_hour", 22) or 0)
    end = int(conf.get("to_hour", 9) or 0)
    if start == end:
        return True                      # окно на все сутки
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # 22:00 → 09:00


def gate(conf: dict, chat_id: str, now: float | None = None,
         settings: dict | None = None) -> tuple[bool, str]:
    """Можно ли сейчас отправить автоответ в этот чат → (можно, причина).

    Причина возвращается всегда, даже когда «можно»: её показывает экран
    проверки, чтобы продавец видел, почему бот промолчал бы, а не гадал.
    """
    now = now if now is not None else time.time()
    if not conf.get("enabled"):
        return False, "автоответы выключены"
    if conf.get("quiet_only") and not in_quiet_window(conf, now, settings):
        return False, (f"сейчас рабочее время — отвечаю только "
                       f"с {int(conf.get('from_hour', 22)):02d}:00 "
                       f"до {int(conf.get('to_hour', 9)):02d}:00")

    st = (conf.get("state") or {}).get(str(chat_id)) or {}
    cooldown = float(conf.get("cooldown_min", 30) or 0) * 60
    last = float(st.get("last", 0) or 0)
    if cooldown and last and now - last < cooldown:
        left = int((cooldown - (now - last)) / 60) + 1
        return False, f"пауза между ответами — ещё {left} мин"

    cap = int(conf.get("max_per_order", 3) or 0)
    if cap and int(st.get("count", 0) or 0) >= cap:
        return False, f"по этому заказу уже {cap} автоответ(а) — дальше вручную"
    return True, "можно отвечать"


# Что маркетплейс отвечает на попытку написать в чат — и что это значит
# продавцу. Голый английский код в интерфейсе означает «разбирайся сам», а
# половина этих ошибок вообще не про бота: чат закрыт, и вручную туда тоже
# не написать. Третий элемент — можно ли ответить руками.
_SEND_ERRORS: tuple[tuple[str, str, bool], ...] = (
    ("no_active_orders_in_chat",
     "маркетплейс закрыл чат — по заказу нет активных сделок. "
     "Написать туда нельзя ни боту, ни вручную", False),
    ("resource_not_found",
     "чата больше нет — заказ удалён или скрыт маркетплейсом", False),
    ("chat_is_closed", "чат заказа закрыт маркетплейсом", False),
    ("unauthenticated",
     "токен интеграции больше не действует — обновите его в «Настройках»", True),
    ("unauthorized",
     "токен интеграции больше не действует — обновите его в «Настройках»", True),
    ("too_many_requests",
     "маркетплейс попросил сбавить темп — попробую позже", True),
    # Действие над заказом, а не отправка: то же место разбора ошибок,
    # потому что и попадает оно на тот же экран продавца.
    ("incorrect_status",
     "маркетплейс не принял действие: заказ сейчас в другом статусе", False),
    ("order_not_found",
     "маркетплейс не нашёл этот заказ — возможно, он уже закрыт", False),
    ("access_denied",
     "маркетплейс не разрешил это действие по этому заказу", False),
)


def explain_error(err: str) -> tuple[str, bool]:
    """Ошибка отправки по-русски → (объяснение, можно ли исправить вручную).

    Второе значение важнее первого: советовать «ответьте вручную» там, где
    маркетплейс закрыл чат, — это отправить продавца биться в стену.
    """
    low = (err or "").lower()
    for needle, text, actionable in _SEND_ERRORS:
        if needle in low:
            return text, actionable
    return ((err or "неизвестная ошибка")[:120], True)


def note_sent(conf: dict, chat_id: str, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    state = conf.setdefault("state", {})
    st = state.setdefault(str(chat_id), {})
    st["last"] = now
    st["count"] = int(st.get("count", 0) or 0) + 1
    # Хранилище настроек — не архив: держим только свежие чаты.
    if len(state) > _STATE_MAX:
        oldest = sorted(state.items(), key=lambda kv: float(kv[1].get("last", 0) or 0))
        for key, _ in oldest[: len(state) - _STATE_MAX]:
            state.pop(key, None)


def log(conf: dict, *, chat_id: str, text: str, ok: bool, err: str = "",
        rule: str = "", now: float | None = None) -> None:
    """Записать отправку — вместе с провалом.

    Провал автоответа раньше уходил только в логи контейнера: покупатель не
    получал ничего, продавец об этом не знал. Журнал видно в боте.
    """
    entries = conf.setdefault("log", [])
    entries.insert(0, {
        "ts": now if now is not None else time.time(),
        "chat": str(chat_id), "text": text[:200], "ok": bool(ok),
        "err": str(err)[:120], "rule": rule[:40],
    })
    del entries[_LOG_MAX:]


# ─────────────────────────────── правила ───────────────────────────────

def new_rule(keywords: list[str], text: str, weak: bool = False) -> dict:
    """`weak` — правило-приветствие: срабатывает, только если ничего другого
    не совпало."""
    return {"id": uuid.uuid4().hex[:8], "on": True, "weak": bool(weak),
            "keywords": [k for k in keywords if k], "text": text, "hits": 0}


def find_rule(conf: dict, rule_id: str) -> dict | None:
    for rule in conf.get("rules") or []:
        if isinstance(rule, dict) and rule.get("id") == rule_id:
            return rule
    return None


def parse_keywords(raw: str) -> list[str]:
    """«ключ, где ключ; не пришёл» → список. Пустые и дубликаты отбрасываются."""
    parts = [p.strip() for p in re.split(r"[,;\n]+", raw or "")]
    out: list[str] = []
    for p in parts:
        if p and p.lower() not in [o.lower() for o in out]:
            out.append(p)
    return out


# Готовые наборы: продавец получает работающие автоответы в одно нажатие,
# вместо того чтобы придумывать формулировки с нуля.
TEMPLATES: dict[str, dict] = {
    "digital": {
        "name": "🔑 Цифровые товары",
        "about": "приветствие, «где ключ», «не пришёл», «сколько ждать», гарантия",
        "rules": [
            # Приветствие — слабое: «Здравствуйте, а где ключ?» должно уйти в
            # ответ про ключ, а не в «здравствуйте».
            (["здравствуйте", "привет", "добрый день", "добрый вечер", "доброе утро"],
             "Здравствуйте! Я на связи. Заказ «{товар}» вижу, отвечу по существу "
             "в ближайшие минуты.", True),
            (["где ключ", "как получить", "где товар", "где код", "не вижу"],
             "Товар приходит в этот чат сразу после оплаты. Проверьте, "
             "пожалуйста, сообщения выше — если там пусто, напишите, "
             "и я выдам вручную."),
            (["не пришёл", "не пришел", "не работает", "не подошёл", "не подошел",
              "ошибка"],
             "Извините за неудобство. Опишите, пожалуйста, что именно "
             "происходит, — заменю или верну деньги."),
            (["сколько ждать", "когда", "долго", "как быстро"],
             "Обычно выдача занимает несколько минут. Если задержка больше — "
             "напишите, разберусь сразу."),
            (["гарантия", "возврат", "вернуть деньги"],
             "Гарантия действует: если товар не подошёл или не работает — "
             "заменю или верну деньги."),
        ],
    },
    "night": {
        "name": "🌙 Ночной ответ",
        "about": "один ответ на любое сообщение вне рабочего времени",
        "rules": [],
        "fallback": "Здравствуйте! Сейчас нерабочее время, отвечу утром. "
                    "Заказ «{товар}» уже вижу.",
        "quiet_only": True,
    },
    "busy": {
        "name": "⏳ Занят",
        "about": "короткое «получил, скоро отвечу» на всё подряд",
        "rules": [],
        "fallback": "Здравствуйте! Сообщение получил, скоро отвечу.",
    },
}


def apply_template(conf: dict, key: str) -> tuple[int, str]:
    """Добавить набор. Уже существующие ключевые слова не дублируются."""
    tpl = TEMPLATES.get(key)
    if not tpl:
        return 0, "набор не найден"
    known = {_norm(k) for r in (conf.get("rules") or [])
             for k in (r.get("keywords") or [])}
    added = 0
    for entry in tpl.get("rules", []):
        keywords, text = entry[0], entry[1]
        weak = bool(entry[2]) if len(entry) > 2 else False
        fresh = [k for k in keywords if _norm(k) not in known]
        if not fresh:
            continue
        conf.setdefault("rules", []).append(new_rule(fresh, text, weak))
        known.update(_norm(k) for k in fresh)
        added += 1
    if tpl.get("fallback"):
        conf["fallback"] = {"on": True, "text": tpl["fallback"]}
    if tpl.get("quiet_only"):
        conf["quiet_only"] = True
    conf["enabled"] = True
    what = f"добавлено правил: {added}" if added else "новых правил не было"
    if tpl.get("fallback"):
        what += ", запасной ответ включён"
    return added, what
