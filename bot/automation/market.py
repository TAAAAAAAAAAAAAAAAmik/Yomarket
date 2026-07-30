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

_PRICE_KEYS = ("price", "amount", "cost", "base_amount", "price_rub", "value")
_TITLE_KEYS = ("title", "name", "ad_title", "product_name", "label")
_SELLER_KEYS = ("shop", "seller", "store", "shop_name", "seller_name",
                "merchant", "user")


def _num(value) -> float | None:
    """A price out of whatever shape it arrives in ({'amount': 149}, '149 ₽')."""
    if isinstance(value, dict):
        for k in _PRICE_KEYS:
            if k in value:
                return _num(value[k])
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d.,]", "", value).replace(",", ".")
        try:
            return float(digits) if digits else None
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
    if not isinstance(node, dict):
        return False
    has_price = any(_num(node.get(k)) is not None for k in _PRICE_KEYS)
    has_title = any(isinstance(node.get(k), (str, dict)) and _text(node.get(k))
                    for k in _TITLE_KEYS)
    return has_price and has_title


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
    lists = []
    for blob in _json_blobs(html):
        lists.extend(_offer_lists(blob))
    if not lists:
        return False, (f"на странице не нашёл список предложений "
                       f"({len(html)}б). Пришлите адрес страницы, где видно "
                       f"именно список офферов.")

    # The longest list is the page's own listing; shorter ones are usually
    # "similar" or "recommended" blocks.
    best = max(lists, key=len)
    return True, {"offers": _normalize(best), "note": f"{len(html)}б, "
                  f"списков-кандидатов: {len(lists)}"}


def find_position(offers: list[dict], *, ad_id: str = "",
                  title: str = "", seller: str = "") -> dict | None:
    """Our own row among the offers, matched by id, then title, then shop.

    Id first because it cannot be ambiguous; the title is the practical
    fallback, since the storefront does not always carry the seller ad id.
    """
    if ad_id:
        for row in offers:
            if row["id"] and str(row["id"]) == str(ad_id):
                return row
    def _norm(s: str) -> str:
        return re.sub(r"[^\w]+", "", str(s or "")).lower()

    if title:
        want = _norm(title)
        for row in offers:
            if want and _norm(row["title"]) == want:
                return row
        for row in offers:                      # decorated titles differ a bit
            if want and want in _norm(row["title"]):
                return row
    if seller:
        want = _norm(seller)
        for row in offers:
            if want and want in _norm(row["seller"]):
                return row
    return None


def cheapest(offers: list[dict]) -> float | None:
    prices = [o["price"] for o in offers if o["price"] is not None]
    return min(prices) if prices else None
