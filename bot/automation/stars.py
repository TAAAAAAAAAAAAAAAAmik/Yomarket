"""Reading a stars order: is this one, and how many.

Kept apart from the scheduler because both answers spend money. Getting the
first one wrong means an order silently never delivers; getting the second one
wrong means buying somebody four thousand stars because the title happened to
mention a year.

Pure functions over the order title — no network, no settings — so they can be
exercised directly (`bot/tests/test_stars.py`).
"""
from __future__ import annotations

import re

# What a stars listing calls itself. The setting used to be a single word,
# «звёзд», matched literally: «звезды» without the ё, «Stars» and «⭐» all went
# past without the delivery ever starting, and nothing said why.
STAR_WORDS = ("звёзд", "звезд", "star", "⭐", "🌟", "✨", "звёздочк", "звездочк")

# Numbers that are never a quantity of stars, however they are written.
_NOT_A_QUANTITY = re.compile(
    r"(?:^|[^\d])(?:19|20)\d{2}(?:$|[^\d])"      # a year: «Stars 2024»
)
# Things a number can be attached to that make it something else entirely.
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
        # A custom keyword is a demand, not a hint — respect it exactly.
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
        # «1 000», «1.000», «1,000» are one number; a trailing separator is not
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


# What a purchase costs on Fragment, near enough to warn on. The real price is
# set at checkout and moves with the TON rate, so this is deliberately a rough
# figure used only to say «this will not go through» *before* a buyer is left
# waiting — never to decide what to pay.
TON_PER_STAR = 0.0042
# Every transfer also burns a little on fees.
TON_FEE_ALLOWANCE = 0.05


def ton_needed(quantity: int) -> float:
    """Roughly what buying this many stars will cost, in TON."""
    try:
        return round(int(quantity) * TON_PER_STAR + TON_FEE_ALLOWANCE, 3)
    except (TypeError, ValueError):
        return TON_FEE_ALLOWANCE


def deliveries_left(balance_ton: float, quantity: int) -> int:
    """How many more orders of this size the wallet can cover."""
    per = ton_needed(quantity)
    if per <= 0:
        return 0
    try:
        return max(0, int(float(balance_ton) // per))
    except (TypeError, ValueError):
        return 0


def star_quantity(title: str, default: int = MIN_QUANTITY) -> int:
    """How many stars a listing is for.

    Taking the first number in range was enough to buy 2024 stars for a listing
    named «Stars 2024 — 100 шт»: it is the seller's money, and nothing in the
    order would have shown the mistake until the bill.
    """
    rows = _candidates(title)
    if not rows:
        return default
    # Nearest the word wins; ties go to whichever comes first in the title.
    rows.sort(key=lambda r: (r[1], r[2]))
    return rows[0][0]
