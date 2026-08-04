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
# The star is excluded from the name rather than merely allowed to precede it:
# a one-character shop name used to come back as «★», because the lazy capture
# had to give its minimum of two characters to something.
_CARD_TAIL = re.compile(
    r"([^\n·•|★☆]{1,40}?)\s*[★☆]?\s*(\d(?:[.,]\d+)?)"
    r"\s*[·•]\s*([\d\s  ]+)\s*отзыв", re.I)
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


_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def get_page(url: str) -> tuple[bool, str]:
    """Blocking: fetch one storefront page. (True, html) or (False, error)."""
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if not url.startswith("http"):
        url = MARKET_URL + ("" if url.startswith("/") else "/") + url
    try:
        r = requests.get(url, timeout=(6, 20), verify=False,
                         headers=_BROWSER_HEADERS)
    except Exception as e:
        return False, f"страница не открылась: {str(e)[:100]}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    return True, r.text


# --- the listing as the storefront itself fetches it ------------------------
#
# The category page ships no offers: 81 KB of application shell, 1.5 KB of
# visible text, and the list drawn in the browser afterwards. It is fetched
# from here — found by reading the addresses compiled into the page's own
# JavaScript (/pos_api):
#
#   https://api.yoo.market/api/products?category=virty&keyword=3.000.000
#   → {"data": [ …15 offers… ], "meta": {…}, "links": {…}}
#
# Fifteen at a time, and a category holds hundreds, so the pages matter: the
# seller's own listing sits far below the first screen, which is the whole
# reason the feature exists.
API_URL = "https://api.yoo.market"
_API_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": MARKET_URL,
    "Referer": MARKET_URL + "/",
}
# 15 offers a page against a category of ~640, so a cap of twenty pages stopped
# exactly at 300 and reported "не нашёл" for a listing that was simply deeper.
# Forty-five covers the whole of such a category; the walk still stops the
# moment the shop is found, so this costs nothing on a listing near the top and
# only pays out when the seller really is buried.
API_MAX_PAGES = 45


def listing_path(url: str) -> tuple[list, dict]:
    """(category slugs, other filters) out of a storefront address.

    /categories/black-russia/virty?keyword=3.000.000
      → (["black-russia", "virty"], {"keyword": "3.000.000"})
    """
    from urllib.parse import parse_qsl, urlparse
    parts = urlparse(url if url.startswith("http") else MARKET_URL + url)
    segments = [p for p in parts.path.split("/") if p]
    filters = {k: v.strip() for k, v in
               parse_qsl(parts.query, keep_blank_values=True)
               if k not in ("page",)}
    slugs = []
    if "categories" in segments:
        slugs = segments[segments.index("categories") + 1:]
    return slugs, filters


def category_meta(slugs: list) -> dict:
    """What the marketplace says about this section of the catalogue.

    /api/categories/black-russia/virty answers with the game and, inside it,
    its sections. Two things are wanted from it: the id of the *section* — the
    listing is «вирты в Black Russia», not «вирты вообще» — and how many
    listings it holds, which is the only independent way to tell a correct
    query from one that quietly returns a different catalogue.
    """
    import requests
    if not slugs:
        return {}
    try:
        r = requests.get(f"{API_URL}/api/categories/" + "/".join(slugs),
                         headers=_API_HEADERS, timeout=(6, 20), verify=False)
        body = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.warning("category meta %s: %s", slugs, e)
        return {}
    if not isinstance(body, dict):
        return {}

    want = str(slugs[-1])
    found: dict = {}

    def walk(node):
        nonlocal found
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if str(node.get("slug") or "") == want and node.get("id") is not None:
                found = found or node
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(body)
    if found:
        return found
    # The root is only the answer when the address named it. Handing it back
    # for /categories/black-russia/akkaunty-s-virtami — which this API answers
    # with the game, ignoring the section — made the game's own ads_count
    # "confirm" a query for the whole game, and the position was then counted
    # in a list of 638 instead of 161.
    if len(slugs) == 1 and str(body.get("slug") or "") == want:
        return body
    return {}


def product_card(market_id: str | int) -> dict:
    """One listing as the storefront serves it, by its marketplace id."""
    import requests
    try:
        r = requests.get(f"{API_URL}/api/products/{market_id}",
                         headers=_API_HEADERS, timeout=(6, 20), verify=False)
        body = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.info("product card %s: %s", market_id, e)
        return {}
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    return body if isinstance(body, dict) else {}


def search_words(title: str) -> list:
    """The searchable words of a title, longest first.

    Titles are written to catch the eye — «💖Аккаунт 💖Баланс: 4.000.000 ₽⚡» —
    and the decorations are not searchable. What is left are the words a buyer
    would actually type.
    """
    words = re.findall(r"[\w.]+", str(title or ""), re.UNICODE)
    seen, out = set(), []
    for w in words:
        w = w.strip(".")
        low = w.lower()
        if len(w) < 3 or low in seen:
            continue
        seen.add(low)
        out.append(w)
    return sorted(out, key=len, reverse=True)


def search_keys(title: str) -> list:
    """Things to try in the marketplace's search box, likeliest first.

    One word at a time rather than a phrase. Joining the two longest words gave
    «Аккаунт 4.000.000» for a title that reads «💖Аккаунт 💖Баланс: 4.000.000 ₽»
    — the words are not adjacent, so a search that matches phrases finds
    nothing at all. A single distinctive word cannot have that problem.
    """
    words = search_words(title)
    if not words:
        return []
    keys = words[:3]
    # And the pair, in case the search is happy with several words: when it is,
    # it narrows the results and our listing surfaces sooner.
    if len(words) > 1:
        ordered = [w for w in re.findall(r"[\w.]+", str(title), re.UNICODE)
                   if w.strip(".") in words[:2]]
        pair = " ".join(dict.fromkeys(ordered))[:60]
        if pair and pair not in keys:
            keys.append(pair)
    return keys


def search_key(title: str) -> str:
    """The first thing worth searching for — kept for messages and tests."""
    keys = search_keys(title)
    return keys[0] if keys else ""


def find_own_listing(market_id: str | int, title: str = "") -> dict:
    """Our listing as it appears on the storefront, found by searching for it.

    `/api/products/{id}` is not something this marketplace answers, so the id
    alone gets us nowhere. The search does work — it is the same endpoint the
    listing pages use — so the title finds the row and the row carries the
    category the address is built from.
    """
    import requests
    card = product_card(market_id)
    if card:
        return card
    want = str(market_id)
    for key in search_keys(title):
        # A few pages per key: the words that survive a decorated title are not
        # always distinctive — «Быстрая выдача» matches half a category.
        for page in range(1, 4):
            try:
                r = requests.get(f"{API_URL}/api/products",
                                 params={"keyword": key, "page": page},
                                 headers=_API_HEADERS, timeout=(6, 20),
                                 verify=False)
                body = r.json() if r.status_code == 200 else {}
            except Exception as e:
                logger.info("search for own listing %s: %s", market_id, e)
                return {}
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                break                       # this key is exhausted, try another
            for row in rows:
                if isinstance(row, dict) and str(row.get("id")) == want:
                    return row
    return {}


def _slug_chain(node, depth: int = 0) -> list:
    """Category slugs found in a payload, outermost first.

    A card names its section and, inside or beside it, the game it belongs to.
    Which key holds which is not fixed, so the tree is walked and the slugs are
    collected in the order they nest.
    """
    out: list = []
    if depth > 6 or not isinstance(node, (dict, list)):
        return out
    if isinstance(node, list):
        for x in node:
            out.extend(_slug_chain(x, depth + 1))
        return out
    slug = node.get("slug")
    if isinstance(slug, str) and slug and not slug.startswith("http"):
        out.append(slug)
    for key in ("parent", "category", "categories", "section", "game",
                "breadcrumbs", "path", "ancestors"):
        if key in node:
            out.extend(_slug_chain(node[key], depth + 1))
    return out


def category_tree() -> list:
    """The catalogue as the storefront's own menu gets it."""
    import requests
    try:
        r = requests.get(f"{API_URL}/api/categories", headers=_API_HEADERS,
                         timeout=(6, 20), verify=False)
        body = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.info("category tree: %s", e)
        return []
    rows = body.get("data") if isinstance(body, dict) else body
    return rows if isinstance(rows, list) else []


def _find_by_id(nodes, want, trail=()) -> list:
    """The slugs leading down to a category id, outermost first."""
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        here = trail + ((str(node.get("slug") or ""),) if node.get("slug")
                        else ())
        if str(node.get("id")) == str(want):
            return [s for s in here if s]
        for key in ("children", "subcategories", "items", "categories"):
            got = _find_by_id(node.get(key), want, here)
            if got:
                return got
    return []


def category_children(parent_slug: str = "") -> list:
    """Top-level sections, or the sections inside one game.

    [{"id":…, "slug":…, "title":…, "has_children": bool}] — enough to offer the
    catalogue as buttons, which is the one way of naming a section that cannot
    be got wrong by either side.
    """
    nodes = category_tree()
    if parent_slug:
        found = None

        def walk(items):
            nonlocal found
            for n in items if isinstance(items, list) else []:
                if not isinstance(n, dict):
                    continue
                if str(n.get("slug") or "") == parent_slug:
                    found = n
                    return
                for key in ("children", "subcategories", "items", "categories"):
                    if n.get(key):
                        walk(n[key])
                        if found:
                            return

        walk(nodes)
        if not found:
            return []
        nodes = next((found[k] for k in ("children", "subcategories", "items",
                                         "categories") if found.get(k)), [])
    out = []
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict) or not n.get("slug"):
            continue
        kids = next((n[k] for k in ("children", "subcategories", "items",
                                    "categories") if n.get(k)), [])
        out.append({"id": n.get("id"), "slug": str(n["slug"]),
                    "title": str(n.get("title") or n.get("name") or n["slug"]),
                    "has_children": bool(kids),
                    "ads_count": n.get("ads_count")})
    return out


def category_slugs_for(cat_id) -> list:
    """Where a category sits in the catalogue: ['black-russia', 'virty'].

    A listing row names its category, but not always the whole path to it — the
    one we saw carried the game and nothing else, which builds an address for
    the whole game rather than the section, and our own listing is not in the
    first hundreds of that.
    """
    if cat_id in (None, ""):
        return []
    return _find_by_id(category_tree(), cat_id)


def _category_id_of(card: dict):
    """The category a listing row says it belongs to."""
    for key in ("category_id", "categoryId", "subcategory_id"):
        if card.get(key) not in (None, ""):
            return card[key]
    node = card.get("category") or card.get("subcategory")
    if isinstance(node, dict):
        # The deepest id in there — the section, not the game above it
        deepest = node.get("id")
        for key in ("children", "subcategory", "section"):
            inner = node.get(key)
            if isinstance(inner, dict) and inner.get("id") is not None:
                deepest = inner["id"]
        return deepest
    return None


def listing_urls_for(market_id: str | int, card: dict | None = None,
                     title: str = "") -> list:
    """Storefront addresses a listing of ours could live at, likeliest first.

    Saves the seller from opening the marketplace, finding their own item and
    copying the address for every listing they want watched — the same work
    fifteen times over, and the step most easily got wrong.

    Candidates rather than one answer, because the card names its section and
    its game without saying which is which: a card points at its parent, so the
    chain usually arrives inside-out, while a breadcrumb arrives the right way
    round. Guessing would be a coin toss; the caller settles it by asking each
    address whether our listing is actually in it.
    """
    card = card if card is not None else find_own_listing(market_id, title)
    if not card:
        return []
    out = []
    # First and best: the catalogue itself, looked up by the category id the
    # row carries. A row does not have to name the whole path — the one we saw
    # gave the game and nothing else, and an address for the whole game puts
    # our listing hundreds of places down a list it does not belong in.
    path = category_slugs_for(_category_id_of(card))
    if path:
        out.append(f"{MARKET_URL}/categories/" + "/".join(path[-2:]))

    own = str(card.get("slug") or "")
    slugs = [s for s in dict.fromkeys(_slug_chain(card)) if s != own]
    pair = slugs[:2]
    if len(pair) == 2:
        # The row names its section and its game without saying which is which
        out.append(f"{MARKET_URL}/categories/" + "/".join(reversed(pair)))
        out.append(f"{MARKET_URL}/categories/" + "/".join(pair))
    for one in slugs[:2]:
        out.append(f"{MARKET_URL}/categories/{one}")
    return list(dict.fromkeys(out))


def listing_query(url: str) -> dict:
    """The API query a storefront address stands for.

    Kept for the simple case and for the tests: the section slug plus whatever
    filters the address carried. `listing_queries` builds the fuller set.
    """
    slugs, filters = listing_path(url)
    query = dict(filters)
    if slugs:
        query["category"] = slugs[-1]
    return query


def listing_queries(url: str, meta: dict | None = None) -> list[dict]:
    """Every plausible way to ask the API for this listing, best guess first.

    There is no documentation to consult, and the obvious query is wrong in a
    way that looks right: `category=virty` returns virtual currency across every
    game, not the Black Russia section the address names. Hundreds of other
    sellers' offers come back, our own listing is nowhere in the first pages,
    and the position that would be reported is a number from a different
    catalogue. So the candidates are tried against the section's own
    `ads_count` instead of being trusted.
    """
    slugs, filters = listing_path(url)
    if not slugs:
        return [dict(filters)] if filters else []
    meta = meta or {}
    cat_id = meta.get("id")
    out: list[dict] = []

    def add(extra: dict):
        cand = {**filters, **extra}
        if cand not in out:
            out.append(cand)

    if cat_id is not None:
        add({"category_id": cat_id})
        add({"category": cat_id})
    if len(slugs) > 1:
        add({"category": slugs[0], "subcategory": slugs[-1]})
        add({"category": "/".join(slugs)})
    add({"category": slugs[-1]})
    return out


def _api_get(url: str, params: dict | None) -> tuple[dict | None, str]:
    """One JSON GET against the storefront API. (body, error)."""
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    try:
        r = requests.get(url, params=params or None, headers=_API_HEADERS,
                         timeout=(6, 20), verify=False)
    except Exception as e:
        return None, f"API витрины не ответил: {str(e)[:100]}"
    if r.status_code != 200:
        return None, f"API витрины: HTTP {r.status_code}"
    try:
        return r.json(), ""
    except Exception:
        return None, "API витрины ответил не JSON"


def _batch_of(body) -> list:
    """The offers in one API response, whatever it calls them."""
    batch = body.get("data") if isinstance(body, dict) else body
    if isinstance(batch, list):
        return batch
    found = _offer_lists(body)
    return max(found, key=len) if found else []


def _pick_query(candidates: list, expected) -> tuple[dict, dict | None, str, str]:
    """Choose the query that really stands for this section of the catalogue.

    Returns (query, its first page, note, error). A 200 proves nothing here:
    `category=virty` answers with hundreds of offers — virtual currency across
    every game — and the position taken from that list would be a number out of
    a different catalogue. The section's own `ads_count` is the check that
    cannot be fooled, so each candidate is judged by whether it returns that
    many. The response is kept, not thrown away and re-fetched.
    """
    fallback_query, fallback_body, err = None, None, ""
    for cand in candidates:
        body, e = _api_get(f"{API_URL}/api/products", {**cand, "page": 1})
        if body is None:
            err = err or e
            continue
        if fallback_body is None:
            fallback_query, fallback_body = cand, body
        if not expected:
            return cand, body, "", ""       # nothing to check against
        got = (body.get("meta") or {}).get("total") if isinstance(body, dict) else None
        try:
            if got is not None and int(got) == int(expected):
                return cand, body, f", запрос: {sorted(cand)}", ""
        except (TypeError, ValueError):
            pass
    if fallback_body is None:
        return {}, None, "", err or "API витрины не ответил"
    return (fallback_query, fallback_body,
            f", ⚠️ ни один запрос не дал {expected} объявлений — "
            f"список может быть не тем", "")


def fetch_offers_api(url: str, shop: str = "",
                     max_pages: int = API_MAX_PAGES,
                     category_id=None) -> tuple[bool, object]:
    """Blocking: the listing straight from the API the storefront calls.

    Walks the pages until the shop is found and stops the moment it is, so a
    routine check on a listing near the top costs one request. Returns
    (True, {"offers": …, "note": …, "total": …, "complete": …}) or
    (False, error).
    """
    slugs, filters = listing_path(url)
    if category_id not in (None, ""):
        # The section was named outright — by the catalogue, not inferred from
        # an address the API reads loosely. Nothing to search for.
        meta_cat = {"id": category_id}
        expected = None
        candidates = [{**filters, "category_id": category_id}]
    else:
        meta_cat = category_meta(slugs) if slugs else {}
        expected = meta_cat.get("ads_count")
        candidates = listing_queries(url, meta_cat)
    if not candidates:
        return False, "не понял адрес — нужна страница категории или поиска"

    query, body, picked_note, err = _pick_query(candidates, expected)
    if body is None:
        return False, err

    rows: list = []
    pages = 0
    total = None
    seen: set = set()
    # Did the listing run out, or did we merely stop looking? Only the first
    # makes «не нашёл» a statement about the listing rather than about us.
    exhausted = False
    while body is not None and pages < max(1, max_pages):
        batch = _batch_of(body)
        if not batch:
            exhausted = True
            break
        sig = _signature(batch)
        if sig in seen:
            exhausted = True                # the same page came back
            break
        seen.add(sig)
        rows.extend(batch)
        pages += 1

        meta = body.get("meta") if isinstance(body, dict) else None
        if isinstance(meta, dict) and total is None:
            total = meta.get("total")
        if shop and find_position(_normalize(rows), seller=shop):
            break
        if pages >= max(1, max_pages):
            break
        # Laravel hands the next page over in `links.next`; falling back on
        # ?page=N keeps this working if that ever stops being included.
        links = body.get("links") if isinstance(body, dict) else None
        nxt = (links or {}).get("next") if isinstance(links, dict) else None
        if nxt:
            body, e = _api_get(nxt, None)
        else:
            body, e = _api_get(f"{API_URL}/api/products",
                               {**query, "page": pages + 1})
        if body is None:
            break                           # keep what the earlier pages gave

    if not rows:
        return False, "API витрины вернул пустой список"
    if total is None and expected:
        total = expected
    note = f"api, офферов: {len(rows)}"
    if total:
        note += f" из {total}"
    note += f", страниц: {pages}{picked_note}"
    if total:
        try:
            exhausted = exhausted or len(rows) >= int(total)
        except (TypeError, ValueError):
            pass
    return True, {"offers": _normalize(rows), "note": note,
                  "total": total, "complete": exhausted}


def with_page(url: str, n: int) -> str:
    """The same listing, page n — keeping every filter already in the address.

    The seller's own address carries a search: …/virty?keyword=3.000.000. Naive
    string concatenation would either drop that or produce a second '?', and
    either way the page that comes back is a different listing than the one
    being watched.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    parts = urlparse(url if url.startswith("http") else MARKET_URL + url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page"]
    query.append(("page", str(n)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _offers_in(html: str) -> tuple[list, str]:
    """(raw offer rows, how they were found) for one page's HTML."""
    lists = []
    for blob in _json_blobs(html):
        lists.extend(_offer_lists(blob))
    if lists:
        # The longest list is the page's own listing; shorter ones are usually
        # "similar" or "recommended" blocks.
        return max(lists, key=len), "json"
    return _offers_from_text(html), "разметка"


def _signature(rows: list) -> tuple:
    """Enough of a page to tell it apart from the one before it."""
    out = []
    for o in rows[:6]:
        if isinstance(o, dict):
            out.append((_seller_of(o)[:20], str(_num(o.get("price")))))
    return tuple(out)


def fetch_offers_sync(url: str, *, max_pages: int = 1,
                      want_seller: str = "") -> tuple[bool, object]:
    """Blocking: the offers on a storefront listing, in the order shown.

    Returns (True, {"offers": [{pos,id,title,price,seller}], "note": str}) or
    (False, error). Read-only and unauthenticated — this is the public page.

    With `max_pages` > 1 the following pages are read too and the positions run
    straight through them. That is the difference between a real answer and a
    reassuring one: the seller had to scroll past the first screen to find their
    own card, so a listing read one page deep would either miss them or — worse
    — report a place counted within a fragment. Reading stops as soon as
    `want_seller` is found, so the usual check is still a single request.
    """
    ok, first = get_page(url)
    if not ok:
        return False, first
    html = first
    rows, how = _offers_in(html)
    if not rows:
        # Say which of the two ways failed and how far it got: "не нашёл
        # список" alone gave nothing to act on, and the page cannot be looked
        # at from here.
        return False, (
            f"на странице не нашёл список предложений. "
            f"HTML {len(html)}б, JSON-блоков: {len(_json_blobs(html))}, "
            f"списков в них: 0, карточек в разметке: 0. "
            f"Пришлите вывод /pos_raw для этого адреса — покажет, где данные.")

    all_rows = list(rows)
    seen = {_signature(rows)}
    pages_read = 1
    per_page = len(rows)
    while (max_pages > 1 and pages_read < max_pages
           and want_seller
           and not find_position(_normalize(all_rows), seller=want_seller)):
        ok, more_html = get_page(with_page(url, pages_read + 1))
        if not ok:
            break
        more, _how = _offers_in(more_html)
        if not more:
            break
        sig = _signature(more)
        if sig in seen:
            # The page parameter did nothing — the same listing came back.
            # Continuing would count the first page twice and inflate every
            # position below it.
            break
        seen.add(sig)
        all_rows.extend(more)
        pages_read += 1
        if len(more) < per_page:
            break                       # a short page is the last one

    note = f"{how}, {len(html)}б, карточек: {len(all_rows)}"
    if pages_read > 1:
        note += f", страниц: {pages_read}"
    return True, {"offers": _normalize(all_rows), "note": note}



# How many pages of a listing to read before giving up on finding ourselves.
# The seller's own address is a search inside a category and their card sits
# well past the first screen, so one page is not an answer — but each page is a
# request, so the walk stops the moment the shop is found.
MAX_LIST_PAGES = 6


def fetch_listing(url: str, shop: str = "", category_id=None):
    """The offers behind a storefront address, however they can be had.

    The API first, because on this marketplace it is the only place the listing
    exists — the page itself is an empty shell. Parsing the markup stays as the
    second route: it costs nothing to keep, it is covered by tests, and it is
    the right answer for a page that does render its list server-side.
    """
    ok, res = fetch_offers_api(url, shop, category_id=category_id)
    if ok:
        return ok, res
    api_error = res
    ok2, res2 = fetch_offers_sync(url, max_pages=MAX_LIST_PAGES if shop else 1,
                                  want_seller=shop)
    if ok2:
        return ok2, res2
    # Both failed: say so in one line rather than blaming whichever ran last.
    return False, f"{api_error}; страница: {res2}"


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
    """The lowest price actually being asked.

    Zero is excluded deliberately: it is what a row without a real price
    reduces to, and one of them among three hundred offers was enough to
    report «дешевле всех: 0 ₽» — a number that made the undercut guard
    meaningless and told the seller nothing.
    """
    prices = [o["price"] for o in offers
              if o["price"] is not None and o["price"] > 0]
    return min(prices) if prices else None
