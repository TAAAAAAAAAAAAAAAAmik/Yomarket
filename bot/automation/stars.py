"""Разбор заказа на звёзды: тот ли это заказ и сколько звёзд.

Вынесено отдельно от планировщика, потому что оба ответа стоят денег.
Ошибка в первом означает заказ, который молча никогда не выдастся; ошибка
во втором — покупку четырёх тысяч звёзд из-за того, что в названии
случайно оказался год.

Здесь только чистые функции над названием заказа — ни сети, ни настроек, —
поэтому их можно гонять напрямую (`bot/tests/test_stars.py`).
"""
from __future__ import annotations

import re

# Как товар со звёздами называет сам себя. Раньше настройка была одним
# «звёзд», matched literally: «звезды» without the ё, «Stars» and «⭐» all went
# проходили мимо, выдача не начиналась, и причина не говорилась нигде.
STAR_WORDS = ("звёзд", "звезд", "star", "⭐", "🌟", "✨", "звёздочк", "звездочк")

# Числа, которые никогда не бывают количеством звёзд, как их ни напиши.
_NOT_A_QUANTITY = re.compile(
    r"(?:^|[^\d])(?:19|20)\d{2}(?:$|[^\d])"      # a year: «Stars 2024»
)
# Слова, рядом с которыми число означает уже совсем другое.
_UNIT_AFTER = re.compile(
    r"^\s*(?:₽|руб|р\b|rub|%|лвл|lvl|ур|сек|мин|час|дн|шт\.?\s*/|:|/)", re.I)

MIN_QUANTITY = 50            # Fragment refuses anything smaller
MAX_QUANTITY = 1_000_000


def is_stars_order(title: str, keyword: str = "") -> bool:
    """Whether an order title is for Telegram Stars.

    `keyword` is the seller's own override; when they have not set one, any of
    the usual spellings counts. A listing named «💫100 ЗВЁЗД💫» and one named
    «Telegram Stars 100» are the same business, and only one of them used to
    trigger.
    """
    low = str(title or "").lower()
    if not low:
        return False
    if keyword:
        kw = str(keyword).strip().lower()
        # Своё слово продавца — это требование, а не подсказка: выполняем дословно.
        if kw and kw not in ("звёзд", "звезд"):
            return kw in low
    return any(w in low for w in STAR_WORDS)


def _candidates(title: str) -> list:
    """(number, distance to the nearest star word) for every number in a title.

    Distance is what decides: «⭐ 100» and «100 звёзд» both put the count next
    to the word, while a year, a level or a price sits somewhere else in the
    name.
    """
    low = str(title or "").lower()
    spots = [m.start() for w in STAR_WORDS for m in re.finditer(re.escape(w), low)]
    out = []
    for m in re.finditer(r"\d[\d\s  .,]*", low):
        raw = m.group(0)
        # «1 000», «1.000», «1,000» — одно число; разделитель в конце — уже нет
        # part of it.
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue
        tail = low[m.end():m.end() + 6]
        if _UNIT_AFTER.match(tail):
            continue                          # a price, a level, a duration
        around = low[max(0, m.start() - 1):m.end() + 1]
        if _NOT_A_QUANTITY.search(f" {around} "):
            continue
        try:
            value = int(digits)
        except ValueError:
            continue
        if not MIN_QUANTITY <= value <= MAX_QUANTITY:
            continue
        near = min((abs(m.start() - s) for s in spots), default=10_000)
        out.append((value, near, m.start()))
    return out


# Во сколько обходится покупка на Fragment — с точностью, достаточной для
# предупреждения. Настоящая цена складывается на оплате и ходит за курсом
# TON, поэтому здесь намеренно грубая оценка: она годится сказать «это не
# пройдёт» ДО того, как покупатель останется ждать, и не годится решать,
# сколько платить.
TON_PER_STAR = 0.0042
# Каждый перевод к тому же немного сгорает на комиссии.
TON_FEE_ALLOWANCE = 0.05


def ton_needed(quantity: int) -> float:
    """Примерно во сколько TON обойдётся покупка такого числа звёзд."""
    try:
        return round(int(quantity) * TON_PER_STAR + TON_FEE_ALLOWANCE, 3)
    except (TypeError, ValueError):
        return TON_FEE_ALLOWANCE


def deliveries_left(balance_ton: float, quantity: int) -> int:
    """На сколько ещё таких заказов хватит кошелька."""
    per = ton_needed(quantity)
    if per <= 0:
        return 0
    try:
        return max(0, int(float(balance_ton) // per))
    except (TypeError, ValueError):
        return 0


def star_quantity(title: str, default: int = MIN_QUANTITY) -> int:
    """Сколько звёзд продаётся в этом товаре.

    «Берём первое число в разумных пределах» хватало, чтобы купить 2024
    звезды по названию «Stars 2024 — 100 шт». Это деньги продавца, и до
    самого счёта ошибку не показало бы ничто.
    """
    rows = _candidates(title)
    if not rows:
        return default
    # Побеждает ближайшее к слову; при равенстве — то, что раньше в названии.
    rows.sort(key=lambda r: (r[1], r[2]))
    return rows[0][0]
