"""Reading the public storefront — the offers list a buyer actually sees.

Position is not in the seller API: it only returns the shop's own ads, with no
notion of who else is on the page or in what order. The place a listing holds
among competing offers exists only on the storefront, which is public, so it is
read from there.

The storefront is a Next.js app, so the offers usually arrive as JSON embedded
in the page (__NEXT_DATA__ or the RSC flight chunks) rather than as HTML rows.
Nothing here is guessed from a fixed selector: the page is searched for the
array that *looks* like a list of offers — several objects each carrying a price
and a title — which survives a redesign that would break any hardcoded path.
"""
from __future__ import annotations

import json as _json
import logging
import re

logger = logging.getLogger(__name__)

MARKET_URL = "https://yoomarket.net"

# A discounted card shows two prices — «239,99 ₽» next to a struck-through
# «490 ₽» — and the payload names them in whatever way the storefront likes, so
# the sale price is looked for first and the crossed-out one only as a fallback.
_PRICE_KEYS = ("price", "amount", "cost", "base_amount", "price_rub", "value",
               "current", "current_price", "final_price", "discount_price",
               "price_with_discount", "new_price", "sale_price", "min_price",
               "old_price", "base_price", "price_old")
_TITLE_KEYS = ("title", "name", "ad_title", "product_name", "label")
_SELLER_KEYS = ("shop", "seller", "store", "shop_name", "seller_name",
                "merchant", "user")
# Not a title and not a price, but only offers carry them — enough to recognise
# a card whose name sits in a nested node this code would not think to open.
_RATING_KEYS = ("rating", "reviews_count", "reviews", "rate", "stars",
                "review_count", "feedback_count")

# A price is a price, not a review counter. «1 620 отзывов» reduces to 1620 once
# the non-digits are stripped, and that used to look like a perfectly good
# number — the guard is the units, not the digits.
_NOT_PRICE = re.compile(
    r"отзыв|оцен|продаж|шт\.?|штук|рейтинг|review|sold|pcs", re.I)


def _num(value) -> float | None:
    """A price out of whatever shape it arrives in ({'amount': 149}, '149 ₽')."""
    if isinstance(value, dict):
        for k in _PRICE_KEYS:
            if k in value:
                got = _num(value[k])
                if got is not None:
                    return got
        return None
    if isinstance(value, bool):
        return None                     # True is 1.0 to float(), and not a price
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if _NOT_PRICE.search(value):
            return None
        digits = re.sub(r"[^\d.,]", "", value.replace(" ", "")
                        .replace(" ", "")).replace(",", ".")
        # «1.299.50» and other multi-separator forms are not a number we can
        # trust; guessing at the decimal point would invent a price.
        if digits.count(".") > 1:
            digits = digits.replace(".", "", digits.count(".") - 1)
        try:
            return float(digits) if digits.strip(".") else None
        except ValueError:
            return None
    return None


def _text(value) -> str:
    if isinstance(value, dict):
        for k in ("title", "name", "display", "label"):
            if isinstance(value.get(k), str):
                return value[k]
        return ""
    return str(value) if isinstance(value, (str, int, float)) else ""


def _looks_like_offer(node) -> bool:
    """A price plus one other thing only an offer would carry.

    Demanding a price *and* a title was too strict: the storefront keeps the
    name in a nested product node on some payloads, and the card is still
    unmistakably an offer because it has a seller and a rating. Requiring the
    price stays — a row without one is not something a buyer chooses between.
    """
    if not isinstance(node, dict):
        return False
    if not any(_num(node.get(k)) is not None for k in _PRICE_KEYS):
        return False
    has_title = any(isinstance(node.get(k), (str, dict)) and _text(node.get(k))
                    for k in _TITLE_KEYS)
    has_seller = bool(_seller_of(node))
    has_rating = any(node.get(k) is not None for k in _RATING_KEYS)
    return has_title or has_seller or has_rating


def _offer_lists(node, depth: int = 0, out: list | None = None) -> list:
    """Every array in the payload that reads as a list of offers."""
    if out is None:
        out = []
    if depth > 12:
        return out
    if isinstance(node, list):
        offers = [x for x in node if _looks_like_offer(x)]
        # Three is enough to be a listing rather than a coincidence
        if len(offers) >= 3:
            out.append(offers)
        for x in node:
            _offer_lists(x, depth + 1, out)
    elif isinstance(node, dict):
        for v in node.values():
            _offer_lists(v, depth + 1, out)
    return out


def _json_blobs(html: str) -> list:
    """Decoded JSON payloads embedded in a Next.js page."""
    blobs = []
    for m in re.finditer(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S):
        try:
            blobs.append(_json.loads(m.group(1)))
        except Exception:
            pass
    for m in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S):
        try:
            blobs.append(_json.loads(m.group(1)))
        except Exception:
            pass
    # RSC flight chunks: self.__next_f.push([1,"…json fragment…"])
    chunks = re.findall(r'__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
    if chunks:
        joined = "".join(chunks)
        try:
            joined = _json.loads(f'"{joined}"')       # unescape as one string
        except Exception:
            joined = joined.replace('\\"', '"').replace("\\n", "\n")
        # Pull out balanced JSON objects/arrays big enough to hold a listing
        for m in re.finditer(r'[\[{]"?\w', joined):
            start = m.start()
            frag = _balanced(joined, start)
            if frag and len(frag) > 200:
                try:
                    blobs.append(_json.loads(frag))
                except Exception:
                    pass
    return blobs


def _balanced(text: str, start: int, limit: int = 400_000) -> str:
    """The balanced JSON literal beginning at `start`, or ''."""
    opening = text[start]
    closing = {"{": "}", "[": "]"}.get(opening)
    if not closing:
        return ""
    depth, in_str, esc = 0, False, False
    for i in range(start, min(len(text), start + limit)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _seller_of(node) -> str:
    for k in _SELLER_KEYS:
        v = node.get(k)
        got = _text(v)
        if got:
            return got
    return ""


# --- reading the rendered page, when the payload is not in the HTML ---------
#
# The last resort, and deliberately a narrow one. A card on the offers list ends
# with the seller and their rating — «GadjiSeller ★ 4.97 · 1 620 отзывов» — and
# that tail is a far steadier landmark than any class name, which a redesign
# renames. Everything between two tails is one card, and the first price in it
# is what the buyer pays.

_STRIP_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
# «GadjiSeller 4.97 · 1 620 отзывов» — name, rating, review count.
_CARD_TAIL = re.compile(
    r"([^\n·•|]{2,40}?)\s*★?\s*(\d(?:[.,]\d+)?)\s*[·•]\s*([\d\s  ]+)\s*отзыв",
    re.I)
_PRICE_TEXT = re.compile(r"(\d[\d\s  ]*(?:[.,]\d{1,2})?)\s*₽")


def _visible_text(html: str) -> str:
    import html as _htmlmod
    text = _STRIP_TAGS.sub(" ", html)
    text = _TAG.sub("\n", text)
    text = _htmlmod.unescape(text)
    return re.sub(r"[ \t ]+", " ", text)


# How far back from the rating a card can reasonably reach. Bounded on purpose:
# taking everything since the previous card would swallow the page header on
# the first one, and its prices with it.
_CARD_WINDOW = 400
# A sale price and its struck-through original sit together — «239,99 ₽ +12
# 490 ₽». Two prices further apart than this are not a pair, they are this
# card's price and something the page happened to print above it.
_PAIR_GAP = 45


def _card_price(chunk: str) -> float | None:
    """This card's price out of everything priced in its window.

    Taking the last price finds the struck-through original on a discounted
    card; taking the second-to-last finds a banner on a card with no discount.
    Neither is a rule — the rule is that the two prices of one card are printed
    next to each other, and the buyer pays the first of them.
    """
    hits = [(m.start(), m.end(), _num(m.group(1)))
            for m in _PRICE_TEXT.finditer(chunk)]
    hits = [h for h in hits if h[2]]
    if not hits:
        return None
    if len(hits) >= 2 and hits[-1][0] - hits[-2][1] <= _PAIR_GAP:
        return hits[-2][2]
    return hits[-1][2]


def _offers_from_text(html: str) -> list[dict]:
    """Offers recovered from the rendered markup. [] when it does not fit."""
    text = _visible_text(html)
    tails = list(_CARD_TAIL.finditer(text))
    if len(tails) < 3:
        return []
    rows, prev_end = [], 0
    for t in tails:
        chunk = text[max(prev_end, t.start() - _CARD_WINDOW):t.start()]
        prev_end = t.end()
        price = _card_price(chunk)
        # The title is the longest line of the card — the name always outweighs
        # the badges («-88%», «Black Russia / Вирты») around it.
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]
        title = max(lines, key=len) if lines else ""
        rows.append({
            "price": price,
            "title": title[:80],
            "seller": t.group(1).strip()[:40],
            "id": "",
        })
    return rows


def _normalize(offers: list) -> list[dict]:
    rows = []
    for i, o in enumerate(offers, 1):
        price = None
        for k in _PRICE_KEYS:
            price = _num(o.get(k))
            if price is not None:
                break
        title = ""
        for k in _TITLE_KEYS:
            title = _text(o.get(k))
            if title:
                break
        rows.append({
            "pos": i,
            "id": str(o.get("id") or o.get("ad_id") or ""),
            "title": title[:80],
            "price": price,
            "seller": _seller_of(o)[:40],
        })
    return rows


def fetch_offers_sync(url: str) -> tuple[bool, object]:
    """Blocking: the offers on a storefront page, in the order shown.

    Returns (True, {"offers": [{pos,id,title,price,seller}], "note": str}) or
    (False, error). Read-only and unauthenticated — this is the public page.
    """
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if not url.startswith("http"):
        url = MARKET_URL + ("" if url.startswith("/") else "/") + url
    try:
        r = requests.get(url, timeout=(6, 20), verify=False, headers={
            "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
    except Exception as e:
        return False, f"страница не открылась: {str(e)[:100]}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"

    html = r.text
    blobs = _json_blobs(html)
    lists = []
    for blob in blobs:
        lists.extend(_offer_lists(blob))
    if lists:
        # The longest list is the page's own listing; shorter ones are usually
        # "similar" or "recommended" blocks.
        best = max(lists, key=len)
        return True, {"offers": _normalize(best),
                      "note": f"json, {len(html)}б, кандидатов: {len(lists)}"}

    # No usable payload — read what the page actually shows instead.
    rows = _offers_from_text(html)
    if rows:
        return True, {"offers": _normalize(rows),
                      "note": f"разметка, {len(html)}б, карточек: {len(rows)}"}

    # Say which of the two ways failed and how far it got: "не нашёл список"
    # alone gave nothing to act on, and the page cannot be looked at from here.
    return False, (
        f"на странице не нашёл список предложений. "
        f"HTML {len(html)}б, JSON-блоков: {len(blobs)}, "
        f"списков в них: 0, карточек в разметке: 0. "
        f"Пришлите вывод /pos_raw для этого адреса — покажет, где лежат данные.")


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", str(s or "")).lower()


def find_position(offers: list[dict], *, ad_id: str = "",
                  title: str = "", seller: str = "") -> dict | None:
    """Our own row among the offers: by id, then by shop, then by title.

    The order matters and is not the obvious one. An offers list is every
    seller's copy of the *same* product, so the titles are near-identical —
    matching on the title first lands on whoever happens to be at the top,
    which reads as "you are 1st" no matter where the listing really sits. The
    shop name is what separates our row from the others; the title is only a
    tie-breaker within it, and a last resort when the page carries no seller.
    """
    if ad_id:
        for row in offers:
            if row["id"] and str(row["id"]) == str(ad_id):
                return row

    if seller:
        want = _norm(seller)
        mine = [r for r in offers if want and want in _norm(r["seller"])]
        if len(mine) == 1:
            return mine[0]
        if mine:
            # Several listings from the same shop on one page — the title picks
            # out which one is being watched.
            if title:
                exact = [r for r in mine if _norm(r["title"]) == _norm(title)]
                if exact:
                    return exact[0]
                loose = [r for r in mine if _norm(title) in _norm(r["title"])]
                if loose:
                    return loose[0]
            return mine[0]

    if title:
        want = _norm(title)
        for row in offers:
            if want and _norm(row["title"]) == want:
                return row
        for row in offers:                      # decorated titles differ a bit
            if want and want in _norm(row["title"]):
                return row
    return None


def cheapest(offers: list[dict]) -> float | None:
    prices = [o["price"] for o in offers if o["price"] is not None]
    return min(prices) if prices else None
