"""Чтение публичной витрины — той самой выдачи, которую видит покупатель.

Позиции в API продавца нет: оно отдаёт только собственные объявления
магазина и ничего не знает о том, кто ещё стоит на странице и в каком
порядке. Место товара среди чужих предложений существует только на витрине,
а витрина публичная — оттуда и читаем.

Витрина написана на Next.js, поэтому предложения обычно приезжают JSON'ом
внутри страницы (__NEXT_DATA__ или куски RSC), а не строками HTML. Ничего
здесь не угадывается по жёсткому селектору: в странице ищется массив,
который ВЫГЛЯДИТ как список предложений — несколько объектов, у каждого
цена и название. Такой поиск переживает переделку вёрстки, которая сломала
бы любой зашитый путь.
"""
from __future__ import annotations

import json as _json
import logging
import re

logger = logging.getLogger(__name__)

MARKET_URL = "https://yoomarket.net"

# У карточки со скидкой две цены — «239,99 ₽» рядом с зачёркнутой «490 ₽», —
# и называет их ответ витрины как ему вздумается. Поэтому сперва ищется цена
# со скидкой, а зачёркнутая — только если первой не нашлось.
_PRICE_KEYS = ("price", "amount", "cost", "base_amount", "price_rub", "value",
               "current", "current_price", "final_price", "discount_price",
               "price_with_discount", "new_price", "sale_price", "min_price",
               "old_price", "base_price", "price_old")
_TITLE_KEYS = ("title", "name", "ad_title", "product_name", "label")
_SELLER_KEYS = ("shop", "seller", "store", "shop_name", "seller_name",
                "merchant", "user")
# Не название и не цена, но встречается только у предложений: этого хватает,
# чтобы узнать карточку, у которой имя лежит во вложенном узле, открывать
# который этот код бы и не догадался.
_RATING_KEYS = ("rating", "reviews_count", "reviews", "rate", "stars",
                "review_count", "feedback_count")

# Цена — это цена, а не счётчик отзывов. «1 620 отзывов» после снятия
# нецифр превращается в 1620, и это выглядело совершенно приличным числом.
# Отличают их единицы измерения, а не цифры.
_NOT_PRICE = re.compile(
    r"отзыв|оцен|продаж|шт\.?|штук|рейтинг|review|sold|pcs", re.I)


def _num(value) -> float | None:
    """Цена из чего угодно, в каком бы виде она ни пришла: {'amount': 149}, «149 ₽»."""
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
        # «1.299.50» и прочие формы с несколькими разделителями — не то число,
        # которому можно верить: догадка о том, где здесь десятичная точка,
        # означала бы выдуманную цену.
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
    """Цена плюс ещё что-нибудь, что бывает только у предложения.

    Требовать цену И название оказалось слишком строго: на части ответов
    витрина держит имя во вложенном узле товара, а карточка всё равно
    несомненно предложение — у неё есть продавец и рейтинг. Требование цены
    остаётся: строка без неё — не то, между чем выбирает покупатель.
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
    """Все массивы в ответе, которые читаются как список предложений."""
    if out is None:
        out = []
    if depth > 12:
        return out
    if isinstance(node, list):
        offers = [x for x in node if _looks_like_offer(x)]
        # Трёх достаточно, чтобы это была выдача, а не совпадение
        if len(offers) >= 3:
            out.append(offers)
        for x in node:
            _offer_lists(x, depth + 1, out)
    elif isinstance(node, dict):
        for v in node.values():
            _offer_lists(v, depth + 1, out)
    return out


def _json_blobs(html: str) -> list:
    """Разобранные куски JSON, вложенные в страницу Next.js."""
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
    # Куски RSC: self.__next_f.push([1,"…фрагмент json…"])
    chunks = re.findall(r'__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
    if chunks:
        joined = "".join(chunks)
        try:
            joined = _json.loads(f'"{joined}"')       # unescape as one string
        except Exception:
            joined = joined.replace('\\"', '"').replace("\\n", "\n")
        # Достаём сбалансированные объекты и массивы, достаточно большие,
        # чтобы вместить выдачу
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
    """Сбалансированный литерал JSON, начинающийся с `start`, либо пустая строка."""
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


# --- чтение отрисованной страницы, когда данных в HTML нет ------------------
#
# Последняя надежда, и намеренно узкая. Карточка в выдаче кончается продавцом
# и его рейтингом — «GadjiSeller ★ 4.97 · 1 620 отзывов», — и этот хвост куда
# более надёжная примета, чем любое имя класса, которое переделка вёрстки
# переименует. Всё между двумя хвостами — одна карточка, а первая цена в ней
# и есть та, которую платит покупатель.

_STRIP_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
# «GadjiSeller 4.97 · 1 620 отзывов» — имя, рейтинг, число отзывов.
# Звёздочка исключена из имени, а не просто разрешена перед ним: односимвольное
# название магазина возвращалось как «★», потому что ленивому захвату надо было
# отдать свои минимальные два знака хоть чему-нибудь.
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


# Насколько далеко назад от рейтинга может тянуться карточка. Ограничено
# намеренно: забрав всё от предыдущей карточки, на первой мы проглотили бы
# шапку страницы, а вместе с ней и её цены.
_CARD_WINDOW = 400
# Цена со скидкой и её зачёркнутый оригинал стоят вплотную — «239,99 ₽ +12
# 490 ₽». Две цены, разошедшиеся дальше этого, — не пара, а цена этой
# карточки и что-то, что страница напечатала выше.
_PAIR_GAP = 45


def _card_price(chunk: str) -> float | None:
    """Цена этой карточки из всего, что в её окне помечено ценой.

    Брать последнюю цену — значит найти зачёркнутую старую на карточке со
    скидкой; брать предпоследнюю — значит найти баннер на карточке без скидки.
    Ни то, ни другое не правило. Правило в том, что две цены одной карточки
    напечатаны рядом, а покупатель платит первую из них.
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
    """Предложения, вытащенные из отрисованной разметки. [] — если не вышло."""
    text = _visible_text(html)
    tails = list(_CARD_TAIL.finditer(text))
    if len(tails) < 3:
        return []
    rows, prev_end = [], 0
    for t in tails:
        chunk = text[max(prev_end, t.start() - _CARD_WINDOW):t.start()]
        prev_end = t.end()
        price = _card_price(chunk)
        # Название — самая длинная строка карточки: имя всегда перевешивает
        # значки вокруг него («-88%», «Black Russia / Вирты»).
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
    """Блокирующая: скачать одну страницу витрины → (True, html) либо (False, ошибка)."""
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


# --- выдача так, как её берёт сама витрина ---------------------------------
#
# Страница раздела не везёт ни одного предложения: 81 КБ каркаса приложения,
# 1,5 КБ видимого текста, а список рисуется в браузере потом. Берётся он
# отсюда — адрес найден чтением того, что вкомпилировано в JavaScript самой
# страницы (/pos_api):
#
#   https://api.yoo.market/api/products?category=virty&keyword=3.000.000
#   → {"data": [ …15 предложений… ], "meta": {…}, "links": {…}}
#
# По пятнадцать за раз, а в разделе их сотни, — значит страницы важны:
# собственный товар продавца лежит далеко за первым экраном, ради чего вся
# эта функция и существует.
API_URL = "https://api.yoo.market"
_API_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": MARKET_URL,
    "Referer": MARKET_URL + "/",
}
# Пятнадцать предложений на странице против раздела примерно в 640: потолок
# в двадцать страниц останавливался ровно на трёхсотом и отвечал «не нашёл»
# про товар, который просто лежал глубже. Сорок пять покрывают такой раздел
# целиком, а обход всё равно прекращается сразу, как найден магазин, — то есть
# на товаре из верхушки это не стоит ничего и срабатывает только тогда, когда
# продавец действительно закопан.
API_MAX_PAGES = 45


def listing_path(url: str) -> tuple[list, dict]:
    """(куски разделов, прочие фильтры) из адреса витрины.

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
    # Корень — верный ответ только тогда, когда его назвал сам адрес. Отдав его
    # для /categories/black-russia/akkaunty-s-virtami — на что это API отвечает
    # ИГРОЙ, пропуская раздел, — мы получали `ads_count` самой игры, которым
    # «подтверждался» запрос на всю игру, и позиция считалась в списке из 638
    # вместо 161.
    if len(slugs) == 1 and str(body.get("slug") or "") == want:
        return body
    return {}


def product_card(market_id: str | int) -> dict:
    """Один товар так, как его отдаёт витрина, — по его номеру на маркетплейсе."""
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
    """The searchable words of a title, most distinctive first.

    Titles are written to catch the eye — «💖Аккаунт 💖Баланс: 4.000.000 ₽⚡» —
    and the decorations are not searchable. What is left are the words a buyer
    would actually type.

    **Числа идут первыми и не отбрасываются за короткость.** Отсев «короче
    трёх букв» съедал ровно то, что отличает один лот от другого: у товара
    «50 звезд» оставалось одно слово «звезд», по которому витрина отдала
    сорок пять чужих строк, а нашей среди них не было. Продавец получил
    «не нашёл на витрине» на товар, который там стоит. В этом магазине
    названия так и устроены — «50 звезд», «100 звезд», «500 звезд», — и
    различает их число.
    """
    words = re.findall(r"[\w.]+", str(title or ""), re.UNICODE)
    seen, out = set(), []
    for w in words:
        w = w.strip(".")
        low = w.lower()
        if not w or low in seen:
            continue
        digits = any(ch.isdigit() for ch in w)
        if len(w) < 3 and not (digits and len(w) >= 2):
            continue
        seen.add(low)
        out.append(w)
    return sorted(out, key=lambda w: (not any(ch.isdigit() for ch in w),
                                      -len(w)))


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
    # И пара слов — на случай, если поиск согласен на несколько: когда согласен,
    # он сужает выдачу, и наш товар всплывает раньше.
    if len(words) > 1:
        ordered = [w for w in re.findall(r"[\w.]+", str(title), re.UNICODE)
                   if w.strip(".") in words[:2]]
        pair = " ".join(dict.fromkeys(ordered))[:60]
        if pair and pair not in keys:
            keys.append(pair)
    # Последним — всё название без украшений. Отдельные слова бывают общими
    # до бесполезности («звезд» вернуло сорок пять чужих строк), а название
    # целиком продавец и покупатель видят одинаково.
    whole = " ".join(re.findall(r"[\w.]+", str(title), re.UNICODE))[:60].strip()
    if whole and whole not in keys:
        keys.append(whole)
    return keys


def search_key(title: str) -> str:
    """Первое, что имеет смысл искать. Оставлено ради сообщений и тестов."""
    keys = search_keys(title)
    return keys[0] if keys else ""


def _row_title(row: dict) -> str:
    return _text(row.get("title") or row.get("name") or "")


def pick_ours(rows: list, title: str = "", seller: str = "") -> dict:
    """Наша строка среди найденных поиском — по магазину и названию.

    Сверялся только номер. А номер объявления в Integration API и номер
    строки на витрине — **разные пространства**: когда они не совпадают,
    поиск находил нашу же строку и выбрасывал её, а продавец получал
    «Не нашёл этот товар в поиске витрины» на товар, который там стоит.

    Ниже по течению, в `find_position`, магазин и название учитывались
    давно. То есть первый шаг был строже второго — и падал именно он.

    Порядок тот же, что и там, и по той же причине: список выдачи — это
    копии одного товара у разных продавцов, названия почти одинаковые, и
    сверка по названию первой привела бы к чужой строке. Магазин отделяет
    нашу, название разбирает ничью внутри неё.
    """
    if not rows:
        return {}
    want_t, want_s = _norm(title), _norm(seller)
    if want_s:
        mine = [r for r in rows if want_s in _norm(_seller_of(r))]
        if mine:
            if want_t:
                for pick in ([r for r in mine if _norm(_row_title(r)) == want_t],
                             [r for r in mine if want_t in _norm(_row_title(r))]):
                    if pick:
                        return pick[0]
            if len(mine) == 1:
                return mine[0]
            # Несколько наших строк, и ни одна не по названию — какая из них
            # та самая, неизвестно. Взять первую значит выбрать не тот раздел
            # и потом объяснять, почему «мы там не стоим».
            return {}
    if want_t:
        exact = [r for r in rows if _norm(_row_title(r)) == want_t]
        if len(exact) == 1:
            return exact[0]
    return {}


def search_own_listing(market_id: str | int, title: str = "",
                       seller: str = "") -> tuple[dict, dict]:
    """(строка, факты) — наша строка на витрине и как её искали.

    Факты отдельно, чтобы диагностика печатала их, а не разбирала прозу:
    какие слова пробовали, сколько строк вернулось, чем в итоге совпало.
    """
    import requests
    facts = {"keys": [], "rows": 0, "by": "", "ids": [], "sellers": []}
    card = product_card(market_id)
    if card:
        facts["by"] = "карточка по номеру"
        return card, facts

    want = str(market_id)
    seen: list[dict] = []
    by_word: list[dict] = []          # строки, пришедшие не по одному числу
    for key in search_keys(title):
        facts["keys"].append(key)
        # По несколько страниц на слово: то, что уцелело от разукрашенного
        # названия, не всегда различает — «Быстрая выдача» подходит половине раздела.
        for page in range(1, 4):
            try:
                r = requests.get(f"{API_URL}/api/products",
                                 params={"keyword": key, "page": page},
                                 headers=_API_HEADERS, timeout=(6, 20),
                                 verify=False)
                body = r.json() if r.status_code == 200 else {}
            except Exception as e:
                logger.info("search for own listing %s: %s", market_id, e)
                facts["error"] = str(e)[:80]
                break
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                break                       # this key is exhausted, try another
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("id")) == want:
                    facts["by"] = "номер"
                    facts["rows"] += 1
                    return row, facts
                seen.append(row)
                # По какому слову пришла строка — не мелочь. Ключ «50»
                # приводит танковую голду и всё, где есть полсотни чего
                # угодно; голосовать за раздел такие строки не должны.
                if not key.strip().isdigit():
                    by_word.append(row)
    facts["rows"] = len(seen)
    facts["ids"] = [str(r.get("id")) for r in seen[:8]]
    facts["sellers"] = [_seller_of(r) for r in seen[:8]]
    # Главный вопрос при промахе: нас в этой выдаче не было вовсе — или мы
    # там были под другим именем? Без списка магазинов это неразличимо, а
    # действия продавца в двух случаях разные.
    facts["shops"] = sorted({_seller_of(r) for r in seen if _seller_of(r)})
    facts["ours_seen"] = bool(seller) and any(
        _norm(seller) in _norm(s) for s in facts["shops"])
    got = pick_ours(seen, title, seller)
    if got:
        facts["by"] = "магазин и название"
        return got, facts
    # Почему соседи не проголосовали — вопрос, который встанет следующим.
    # Причин ровно две, и они требуют разного: подходящих строк не нашлось
    # совсем, или нашлись, но раздела в них нет. Различать это по нулю
    # голосов невозможно.
    same, how = neighbours_of(by_word, title)
    facts["neighbours"] = len(same)
    facts["matched_by"] = how
    facts["by_word_rows"] = len(by_word)
    facts["with_section"] = sum(section_votes(same).values())
    facts["row_keys"] = sorted(seen[0].keys())[:14] if seen else []
    facts["row_section"] = section_ref_of(seen[0]) if seen else None
    # Сырое значение поля — последняя инстанция, когда «раздела нет», а поле
    # есть. Шесть кругов ушло на то, чего не печатали.
    facts["row_category_raw"] = repr((seen[0].get("category")
                                      if seen else None))[:120]
    if same:
        facts["neighbour_section_raw"] = repr(same[0].get("category"))[:120]
    facts["section"], facts["section_votes"] = section_of_neighbours(by_word,
                                                                     title)
    return got, facts


def _title_has(row_title: str, words: list) -> bool:
    """Есть ли в чужом названии все наши слова.

    Числа сверяются целиком, слова — подстрокой. Иначе «500 звёздочек»
    содержит «50» куском и притворяется нашим товаром, а «50 звезд» и
    «500 звезд» — разные лоты в одном разделе.
    """
    low = _norm(row_title)
    nums = set(re.findall(r"\d+", str(row_title or "")))
    for w in words:
        if w.isdigit():
            if w not in nums:
                return False
        elif w not in low:
            return False
    return True


def neighbours_of(rows: list, title: str) -> tuple[list, str]:
    """Строки того же товара у других магазинов. (строки, чем совпало)

    Три сита подряд, от строгого к широкому: названия у соседей украшены
    по-своему, и требовать точного совпадения — значит не найти никого.
    """
    want = _norm(title)
    if not want or not rows:
        return [], ""
    same = [r for r in rows if _norm(_row_title(r)) == want]
    if same:
        return same, "точное название"
    words = [_norm(w) for w in search_words(title) if _norm(w)]
    if words:
        same = [r for r in rows if _title_has(_row_title(r), words)]
        if same:
            return same, "все слова названия"
        # Корни слов: «звезд» против «звёздочек», «аккаунт» против
        # «аккаунты». Число при этом обязано совпасть — оно и отличает
        # «50 звезд» от «500 звезд».
        nums = {w for w in words if w.isdigit()}
        stems = [w[:4] for w in words if not w.isdigit() and len(w) >= 4]
        if nums and stems:
            # Число целиком, а не куском: «500 звёздочек» содержит «50»
            # подстрокой и притворилось бы нашим товаром.
            same = [r for r in rows
                    if nums <= set(re.findall(r"\d+", _row_title(r)))
                    and any(st in _norm(_row_title(r)) for st in stems)]
            if same:
                return same, "число и корень слова"
        # И, наконец, по одним корням слов, без числа. Мы ищем **раздел**, а
        # не свой лот: сосед может продавать звёзды пачками на выбор и не
        # писать в названии никакого числа, а стоять при этом там же, где
        # мы. Число тут только мешало.
        if stems:
            same = [r for r in rows
                    if all(st in _norm(_row_title(r)) for st in stems)]
            if same:
                return same, "корни слов без числа"
    return [], ""


def section_of_neighbours(rows: list, title: str) -> tuple[object, int]:
    """Раздел витрины по ЧУЖИМ строкам того же товара. (номер, голосов)

    Своей строки в выдаче может не быть вовсе: у «50 звезд» витрина отдала
    124 строки от тридцати магазинов, и нашего среди них нет — конкурентов
    сотни, а листается три страницы. Искать себя дальше бессмысленно.

    Но раздел-то нам и нужен, а не своя строка. Соседи, продающие ровно
    тот же товар, стоят в **том же разделе** — и их строки витрина отдала
    сотнями. Берём раздел, за который их больше всего.

    Догадкой это не остаётся: адрес потом открывается и в нём ищется наша
    строка. Не нашлась — бот так и скажет, а не запишет нас в чужой раздел.
    """
    same, _how = neighbours_of(rows, title)
    votes = section_votes(same)
    if not votes:
        return None, 0
    best = max(votes.items(), key=lambda kv: kv[1])
    return best[0], best[1]


def section_ref_of(row: dict):
    """Раздел строки витрины — номером, slug'ом или названием, как отдан.

    Числовой ключ проверяется первым. Дальше годится что угодно, чем витрина
    назвала раздел: строка-slug, или объект без номера — `{"slug": …,
    "title": …}`. Раньше требовался номер, и раздел, лежавший на виду,
    молча пропадал; из-за этого «раздел по соседям» держался на нуле, сколько
    бы соседей ни находилось.
    """
    cid = _category_id_of(row)
    if cid not in (None, ""):
        return cid
    node = row.get("category") or row.get("subcategory")
    if isinstance(node, str) and node.strip():
        return node.strip()
    if isinstance(node, dict):
        for key in ("slug", "title", "name"):
            got = node.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
    return None


def section_votes(rows: list) -> dict:
    """{раздел: сколько строк за него} — только строки с разделом."""
    votes: dict = {}
    for r in rows:
        ref = section_ref_of(r)
        if ref not in (None, ""):
            votes[str(ref)] = votes.get(str(ref), 0) + 1
    return votes


def find_own_listing(market_id: str | int, title: str = "",
                     seller: str = "") -> dict:
    """Наш товар так, как он выглядит на витрине, — найденный поиском.

    `/api/products/{id}` этот маркетплейс не отвечает, поэтому один номер не
    даёт ничего. А поиск работает — это тот же адрес, которым пользуются
    страницы выдачи, — так что название находит строку, а строка несёт в себе
    раздел, из которого собирается адрес.
    """
    return search_own_listing(market_id, title, seller)[0]


def _slug_chain(node, depth: int = 0) -> list:
    """Куски разделов, найденные в ответе, — от внешнего к внутреннему.

    Карточка называет свой раздел и, внутри или рядом, игру, которой он
    принадлежит. Какой ключ что держит — не закреплено, поэтому дерево
    обходится, а куски собираются в том порядке, в каком они вложены.
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


def _looks_like_category(node) -> bool:
    """Узел похож на раздел каталога: есть slug либо пара «номер + название»."""
    if not isinstance(node, dict):
        return False
    if isinstance(node.get("slug"), str) and node["slug"].strip():
        return True
    has_name = any(isinstance(node.get(k), str) and node[k].strip()
                   for k in ("title", "name"))
    return has_name and node.get("id") is not None


def _category_lists(node, depth: int = 0) -> list:
    """Все массивы в ответе, похожие на список разделов, крупнейший первым.

    Ответ разбирался одной строчкой — `body["data"]`. Если каталог лежит
    под другим ключом или на уровень глубже, получался пустой список, и
    экран говорил «каталог витрины сейчас не читается». Так же ищутся
    предложения на странице (`_offer_lists`) — там это давно себя оправдало.
    """
    out: list = []
    if depth > 6:
        return out
    if isinstance(node, list):
        rows = [x for x in node if _looks_like_category(x)]
        if len(rows) >= 2:
            out.append(rows)
        for x in node:
            out.extend(_category_lists(x, depth + 1))
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_category_lists(v, depth + 1))
    return out


# Куда ходить за каталогом. Первый адрес — тот, что был; остальные пробуются,
# только если он не ответил ничем годным, и стоят по одному запросу.
_CATALOGUE_TRIES = (
    ("/api/categories", None),
    ("/api/categories", {"per_page": "200"}),
    ("/api/categories/tree", None),
    ("/api/catalog", None),
)


def category_tree_probe() -> list[dict]:
    """Что каждый адрес каталога ответил на самом деле. Только чтение.

    Продавец: «разделы не выходят». Экран говорил «каталог не читается» и
    ничего больше — а без ответа сервера чинить это можно только гаданием,
    чего в этом проекте делать нельзя.
    """
    import requests
    out = []
    for path, params in _CATALOGUE_TRIES:
        row = {"path": path + ("?" + "&".join(f"{k}={v}" for k, v in
                                              (params or {}).items())
                               if params else "")}
        try:
            r = requests.get(f"{API_URL}{path}", params=params,
                             headers=_API_HEADERS, timeout=(6, 20),
                             verify=False)
            row["status"] = r.status_code
            row["bytes"] = len(r.content or b"")
            try:
                body = r.json()
            except Exception:
                row["shape"] = "не JSON"
                row["sample"] = (r.text or "")[:120]
                out.append(row)
                continue
            row["shape"] = (f"dict{sorted(body)[:8]}" if isinstance(body, dict)
                            else f"{type(body).__name__}[{len(body)}]"
                            if isinstance(body, list) else type(body).__name__)
            # Разделы засчитываются только у нормального ответа: тело
            # ошибки тоже бывает похоже на список, и «нашлось 2» на пятисотке
            # увело бы в сторону.
            lists = _category_lists(body) if r.status_code == 200 else []
            row["found"] = len(lists[0]) if lists else 0
            if lists:
                row["first"] = _json.dumps(lists[0][0], ensure_ascii=False)[:200]
        except Exception as e:
            row["status"] = "—"
            row["shape"] = f"ошибка: {str(e)[:70]}"
        out.append(row)
        if row.get("found"):
            break
    return out


def category_tree() -> list:
    """The catalogue as the storefront's own menu gets it.

    Разбор терпимый нарочно: раньше бралось ровно `body["data"]`, и стоило
    каталогу приехать под другим ключом — экран выбора раздела показывал
    «каталог витрины сейчас не читается», а автоматика теряла оба пути к
    разделу. Одна строчка разбора ломала обе дороги сразу.
    """
    import requests
    for path, params in _CATALOGUE_TRIES:
        try:
            r = requests.get(f"{API_URL}{path}", params=params,
                             headers=_API_HEADERS, timeout=(6, 20),
                             verify=False)
            if r.status_code != 200:
                continue
            body = r.json()
        except Exception as e:
            logger.info("category tree %s: %s", path, e)
            continue
        rows = body.get("data") if isinstance(body, dict) else body
        if isinstance(rows, list) and any(_looks_like_category(x) for x in rows):
            return rows
        lists = _category_lists(body)
        if lists:
            return max(lists, key=len)
    return []


def _find_by_slug(nodes, want, trail=()) -> list:
    """Цепочка slug'ов до раздела с таким slug, снаружи внутрь."""
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        slug = str(node.get("slug") or "")
        here = list(trail) + ([slug] if slug else [])
        if slug and slug == str(want):
            return here
        for key in ("children", "subcategories", "items", "categories"):
            got = _find_by_slug(node.get(key), want, here)
            if got:
                return got
    return []


def slugs_for_section(ref) -> list:
    """Цепочка slug'ов раздела по чему угодно: номеру, slug'у, названию.

    Строка витрины кладёт раздел в поле `category`, и это **не число**:
    `_category_id_of` искал только числовые ключи и возвращал пусто у всех
    тринадцати найденных соседей — «из них с разделом: 0» при полном
    списке полей `category, id, images, price, rating, shop, slug, …`.
    Раздел был на виду и не читался.
    """
    if ref in (None, ""):
        return []
    text = str(ref).strip()
    if text.isdigit():
        return _find_by_id(category_tree(), text)
    return _find_by_slug(category_tree(), text) or category_slugs_by_title(text)


def _find_by_id(nodes, want, trail=()) -> list:
    """Куски, ведущие вниз к номеру раздела, — от внешнего к внутреннему."""
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


def category_node(slug: str) -> dict:
    """Раздел каталога по slug'у: {"id", "slug", "title"} или пусто."""
    want = str(slug or "")
    if not want:
        return {}

    def walk(items):
        for n in items if isinstance(items, list) else []:
            if not isinstance(n, dict):
                continue
            if str(n.get("slug") or "") == want:
                return {"id": n.get("id"), "slug": want,
                        "title": str(n.get("title") or n.get("name") or want)}
            for key in ("children", "subcategories", "items", "categories"):
                if n.get(key):
                    got = walk(n[key])
                    if got:
                        return got
        return {}

    return walk(category_tree())


def _child_tries(slug: str, cid) -> tuple:
    """Чем можно спросить подразделы. Порядок — от вероятного к запасному."""
    tries = [("/api/categories", {"parent": slug}),
             ("/api/categories", {"parent_slug": slug}),
             (f"/api/categories/{slug}", None),
             (f"/api/categories/{slug}/children", None)]
    if cid not in (None, ""):
        tries.insert(2, ("/api/categories", {"parent_id": str(cid)}))
    return tuple(tries)


def fetch_category_children(parent_slug: str, cid=None,
                            trace: list | None = None) -> list:
    """Подразделы одной игры, спрошенные у витрины отдельным запросом.

    Верхний уровень каталога приезжает плоским: шестьдесят игр, ни у одной
    вложенных разделов. Считать их листьями нельзя — внутри «Telegram»
    живут «Звёзды», «Премиум» и прочее, и следить надо за разделом, а не за
    игрой целиком.

    Адрес не документирован, поэтому пробуется несколько; годным считается
    ответ, где нашлись разделы с чужими slug'ами — сама игра в своих детях
    не считается.
    """
    import requests
    # Адрес вида /api/categories/<slug> у части витрин отдаёт весь каталог
    # заново. Принять его за подразделы значит предложить продавцу список
    # игр под видом разделов «Telegram» — поэтому ответ, целиком лежащий на
    # верхнем уровне, отвергается.
    top = {str(n.get("slug") or "") for n in category_tree()
           if isinstance(n, dict)}
    for path, params in _child_tries(parent_slug, cid):
        label = path + (f"?{list(params)[0]}" if params else "")
        try:
            r = requests.get(f"{API_URL}{path}", params=params,
                             headers=_API_HEADERS, timeout=(6, 20),
                             verify=False)
            if r.status_code != 200:
                if trace is not None:
                    trace.append(f"{label} → {r.status_code}")
                continue
            body = r.json()
        except Exception as e:
            if trace is not None:
                trace.append(f"{label} → {str(e)[:50]}")
            continue
        for rows in _category_lists(body):
            kids = [x for x in rows
                    if str(x.get("slug") or "") not in ("", parent_slug)]
            slugs = {str(x.get("slug") or "") for x in kids}
            if kids and top and slugs <= top:
                if trace is not None:
                    trace.append(f"{label} → это снова весь каталог")
                continue
            if kids:
                if trace is not None:
                    trace.append(f"{label} → {len(kids)} подразделов")
                return kids
        if trace is not None:
            trace.append(f"{label} → разделов нет")
    return []


def category_children(parent_slug: str = "") -> list:
    """Разделы верхнего уровня либо разделы внутри одной игры.

    [{"id":…, "slug":…, "title":…, "has_children": bool}] — этого хватает,
    чтобы предложить каталог кнопками, а кнопка — единственный способ назвать
    раздел, в котором не может ошибиться ни одна сторона.
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
        if not nodes:
            # Верхний уровень витрина отдаёт без вложений: у всех шестидесяти
            # игр детей внутри нет. Значит подразделы — «Звёзды», «Премиум» —
            # надо спрашивать отдельно, а не считать игру листом. Иначе
            # продавец выбирает игру целиком и следит за позицией среди
            # тысяч чужих товаров, которые к его лоту отношения не имеют.
            nodes = fetch_category_children(parent_slug, found.get("id"))
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
    """Где раздел стоит в каталоге: ['black-russia', 'virty'].

    Строка товара называет свой раздел, но не всегда весь путь к нему: та,
    что мы видели, несла только игру, — а из этого собирается адрес всей игры,
    а не раздела, и нашего товара в первых сотнях такой выдачи нет.
    """
    if cat_id in (None, ""):
        return []
    return _find_by_id(category_tree(), cat_id)


def category_slugs_by_title(name: str) -> list:
    """Раздел витрины по его названию: «Звёзды» → ['telegram', 'zvezdy'].

    Запасной путь к разделу, когда номера не сходятся. Номера товаров у
    Integration API и у витрины из разных пространств — это уже стоило
    продавцу «не нашёл на витрине» на товар, который там стоит. С номерами
    разделов может быть так же, а вот название раздела у обоих одно: это
    таксономия маркетплейса, а не строка конкретного магазина.
    """
    want = _norm(name)
    if not want:
        return []
    best: list = []

    def walk(nodes, trail):
        nonlocal best
        for n in nodes if isinstance(nodes, list) else []:
            if not isinstance(n, dict):
                continue
            here = trail + ([str(n["slug"])] if n.get("slug") else [])
            title = _norm(str(n.get("title") or n.get("name") or ""))
            # Точное совпадение побеждает; раздел глубже — точнее раздела
            # выше, поэтому длинная цепочка вытесняет короткую.
            if title and title == want and (not best or len(here) > len(best)):
                best = here
            for key in ("children", "subcategories", "items", "categories"):
                if n.get(key):
                    walk(n[key], here)

    walk(category_tree(), [])
    return best


def _category_id_of(card: dict):
    """Раздел, к которому строка товара себя относит."""
    for key in ("category_id", "categoryId", "subcategory_id"):
        if card.get(key) not in (None, ""):
            return card[key]
    node = card.get("category") or card.get("subcategory")
    if isinstance(node, dict):
        # Самый глубокий номер оттуда — это раздел, а не игра над ним
        deepest = node.get("id")
        for key in ("children", "subcategory", "section"):
            inner = node.get(key)
            if isinstance(inner, dict) and inner.get("id") is not None:
                deepest = inner["id"]
        return deepest
    return None


def listing_urls_for(market_id: str | int, card: dict | None = None,
                     title: str = "", seller: str = "") -> list:
    """Адреса витрины, где мог бы жить наш товар, — вероятные первыми.

    Избавляет продавца от необходимости открывать маркетплейс, находить там
    свой товар и копировать адрес — и так для каждого товара, за которым он
    хочет следить: пятнадцать раз одна и та же работа, и тот шаг, в котором
    легче всего ошибиться.

    Отдаются кандидаты, а не один ответ, потому что карточка называет свой
    раздел и свою игру, не говоря, что из них что: карточка указывает на
    родителя, поэтому цепочка обычно приходит вывернутой наизнанку, а
    «хлебные крошки» — правильной стороной. Угадывание здесь — подбрасывание
    монеты; вызывающий решает спор, спрашивая у каждого адреса, есть ли в нём
    на самом деле наш товар.
    """
    # Имя магазина — чтобы узнать свою строку, когда номер витрины не совпал
    # с номером объявления. Без него поиск находил нашу строку и выбрасывал.
    facts: dict = {}
    if card is None:
        card, facts = search_own_listing(market_id, title, seller)
    if not card:
        # Своей строки в выдаче нет — конкурентов сотни, а листается три
        # страницы. Но раздел нам и нужен, а не своя строка: соседи с тем
        # же товаром стоят в том же разделе.
        path = slugs_for_section(facts.get("section"))
        if path:
            out = [f"{MARKET_URL}/categories/" + "/".join(path[-2:])]
            if len(path) > 1:
                out.append(f"{MARKET_URL}/categories/{path[-1]}")
            return out
        return []
    out = []
    # Первое и лучшее: сам каталог, спрошенный по номеру раздела, который несёт
    # строка. Строка не обязана называть весь путь — та, что мы видели, дала
    # только игру, а адрес всей игры кладёт наш товар на сотни мест вниз по
    # списку, к которому он не относится.
    path = slugs_for_section(section_ref_of(card))
    if path:
        out.append(f"{MARKET_URL}/categories/" + "/".join(path[-2:]))

    own = str(card.get("slug") or "")
    slugs = [s for s in dict.fromkeys(_slug_chain(card)) if s != own]
    pair = slugs[:2]
    if len(pair) == 2:
        # Строка называет свой раздел и свою игру, не говоря, что из них что
        out.append(f"{MARKET_URL}/categories/" + "/".join(reversed(pair)))
        out.append(f"{MARKET_URL}/categories/" + "/".join(pair))
    for one in slugs[:2]:
        out.append(f"{MARKET_URL}/categories/{one}")
    return list(dict.fromkeys(out))


def listing_query(url: str) -> dict:
    """Запрос к API, которому соответствует адрес витрины.

    Оставлен ради простого случая и ради тестов: кусок раздела плюс те
    фильтры, что несёт адрес. Полный набор строит `listing_queries`.
    """
    slugs, filters = listing_path(url)
    query = dict(filters)
    if slugs:
        query["category"] = slugs[-1]
    return query


def listing_queries(url: str, meta: dict | None = None) -> list[dict]:
    """Все правдоподобные способы спросить у API про эту выдачу, лучший первым.

    Документации, в которую можно заглянуть, нет, а очевидный запрос неверен
    так, что выглядит верным: `category=virty` возвращает виртуальную валюту
    по ВСЕМ играм, а не раздел Black Russia, названный в адресе. Приходят сотни
    чужих предложений, нашего товара нет и на первых страницах, а позиция,
    которую мы бы сообщили, — это номер из другого каталога. Поэтому кандидаты
    не принимаются на веру, а проверяются по `ads_count` самого раздела.
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
    """Один GET за JSON к API витрины → (тело, ошибка)."""
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
    """Предложения из одного ответа API — как бы он их ни называл."""
    batch = body.get("data") if isinstance(body, dict) else body
    if isinstance(batch, list):
        return batch
    found = _offer_lists(body)
    return max(found, key=len) if found else []


_ALL_TOTAL: dict = {"ts": 0.0, "value": None}


def unfiltered_total(max_age: float = 600.0):
    """Сколько предложений витрина отдаёт БЕЗ фильтра по разделу.

    Проверка, которую нельзя обмануть и для которой ничего не нужно знать
    о каталоге: если запрос с разделом возвращает столько же, сколько
    запрос вообще без раздела, — раздел как фильтр не сработал.

    Ровно это и происходило. «Звезды» и «Аккаунты с виртами» — разные
    разделы, а бот в обоих прочитал по 675 предложений: сорок пять страниц
    по пятнадцать, то есть свой же предел на общей ленте. Позиция,
    посчитанная в такой ленте, — число из чужого списка, а «товара в
    разделе нет» — заявление о разделе, которого никто не читал.
    """
    import time as _t
    now = _t.time()
    if _ALL_TOTAL["value"] is not None and now - _ALL_TOTAL["ts"] < max_age:
        return _ALL_TOTAL["value"]
    body, _e = _api_get(f"{API_URL}/api/products", {"page": 1})
    total = (body.get("meta") or {}).get("total") if isinstance(body, dict) else None
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    _ALL_TOTAL.update(ts=now, value=total)
    return total


def _total_of(body) -> int | None:
    got = (body.get("meta") or {}).get("total") if isinstance(body, dict) else None
    try:
        return int(got) if got is not None else None
    except (TypeError, ValueError):
        return None


def _pick_query(candidates: list, expected) -> tuple[dict, dict | None, str, str]:
    """Выбрать запрос, который действительно означает этот раздел каталога.

    Отдаёт (запрос, его первую страницу, пояснение, ошибку). HTTP 200 здесь не
    доказывает ничего: `category=virty` отвечает сотнями предложений —
    виртуальной валютой по всем играм, — и позиция, взятая из такого списка,
    была бы номером из другого каталога. Проверка, которую не обмануть, — это
    `ads_count` самого раздела: каждый кандидат оценивается по тому, столько
    ли он вернул. Ответ при этом сохраняется, а не выбрасывается ради
    повторного запроса.
    """
    fallback_query, fallback_body, err = None, None, ""
    all_total = unfiltered_total()
    ignored = 0
    for cand in candidates:
        body, e = _api_get(f"{API_URL}/api/products", {**cand, "page": 1})
        if body is None:
            err = err or e
            continue
        got = _total_of(body)
        # Совпало со счётчиком раздела из каталога — это доказательство, и
        # оно сильнее всего остального. Проверяется первым.
        if expected and got is not None and got == int(expected):
            return cand, body, f", запрос: {sorted(cand)}", ""
        # Столько же, сколько без всякого раздела, — значит раздел витрина
        # не приняла. Такой ответ не годится ни как выбор, ни как запасной:
        # именно он и превращался в «просмотрено 675 предложений» из общей
        # ленты и в «товара в разделе нет» про раздел, который не читали.
        if all_total is not None and got is not None and got == all_total:
            ignored += 1
            continue
        if fallback_body is None:
            fallback_query, fallback_body = cand, body
        if not expected:
            return cand, body, f", запрос: {sorted(cand)}", ""
    if fallback_body is None:
        if ignored:
            return {}, None, "", (
                "витрина не приняла раздел как фильтр: на запрос с разделом "
                f"отвечает тем же, что и без него ({all_total} предложений). "
                "Позиция в такой ленте была бы числом из чужого списка.")
        return {}, None, "", err or "API витрины не ответил"
    return (fallback_query, fallback_body,
            f", ⚠️ ни один запрос не дал {expected} объявлений — "
            f"список может быть не тем", "")


def fetch_offers_api(url: str, shop: str = "",
                     max_pages: int = API_MAX_PAGES,
                     category_id=None) -> tuple[bool, object]:
    """Блокирующая: выдача прямо из того API, которое зовёт витрина.

    Идёт по страницам, пока не найдёт магазин, и останавливается сразу, как
    нашла: обычная проверка товара из верхушки стоит одного запроса. Отдаёт
    (True, {"offers": …, "note": …, "total": …, "complete": …}) либо
    (False, ошибка).
    """
    slugs, filters = listing_path(url)
    if category_id not in (None, ""):
        # Раздел назван прямо — каталогом, а не выведен из адреса, который
        # этот API читает вольно. Но «назван» не значит «принят»: как
        # именно витрина ждёт номер раздела, нигде не написано, и один
        # угаданный ключ молча превращался в общую ленту. Поэтому имён
        # несколько, а годность каждого проверяется.
        meta_cat = category_meta(slugs) if slugs else {}
        expected = meta_cat.get("ads_count")
        candidates = [{**filters, "category_id": category_id},
                      {**filters, "category": category_id},
                      {**filters, "subcategory_id": category_id},
                      {**filters, "categories[]": category_id}]
        if slugs:
            candidates.append({**filters, "category": slugs[-1]})
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
    # Выдача кончилась — или это мы перестали смотреть? Только первое делает
    # «не нашёл» утверждением о выдаче, а не о нас самих.
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
        # Laravel отдаёт следующую страницу в `links.next`; запасной ?page=N
        # оставлен на случай, если её однажды перестанут присылать.
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
    """Та же выдача, страница n, — с сохранением всех фильтров из адреса.

    В собственном адресе продавца бывает поиск: …/virty?keyword=3.000.000.
    Наивная склейка строк либо потеряет его, либо поставит второй «?», и в
    обоих случаях вернётся не та выдача, за которой следят.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    parts = urlparse(url if url.startswith("http") else MARKET_URL + url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page"]
    query.append(("page", str(n)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _offers_in(html: str) -> tuple[list, str]:
    """(сырые строки предложений, каким способом нашлись) для HTML одной страницы."""
    lists = []
    for blob in _json_blobs(html):
        lists.extend(_offer_lists(blob))
    if lists:
        # Самый длинный список — это выдача самой страницы; те, что короче,
        # обычно блоки «похожие» и «рекомендуем».
        return max(lists, key=len), "json"
    return _offers_from_text(html), "разметка"


def _signature(rows: list) -> tuple:
    """Столько страницы, чтобы отличить её от предыдущей."""
    out = []
    for o in rows[:6]:
        if isinstance(o, dict):
            out.append((_seller_of(o)[:20], str(_num(o.get("price")))))
    return tuple(out)


def fetch_offers_sync(url: str, *, max_pages: int = 1,
                      want_seller: str = "") -> tuple[bool, object]:
    """Блокирующая: предложения на витрине, в том порядке, в каком они показаны.

    Отдаёт (True, {"offers": [{pos,id,title,price,seller}], "note": str}) либо
    (False, ошибка). Только чтение и без входа — страница публичная.

    При `max_pages` > 1 читаются и следующие страницы, а позиции идут сквозной
    нумерацией. Здесь и проходит разница между настоящим ответом и
    успокоительным: продавцу приходилось прокручивать за первый экран, чтобы
    найти свою карточку, — значит выдача, прочитанная на одну страницу, либо
    его не найдёт, либо, что хуже, назовёт место, посчитанное внутри куска.
    Чтение прекращается, как только найден `want_seller`, поэтому обычная
    проверка по-прежнему стоит одного запроса.
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
            # Параметр страницы ничего не изменил — вернулась та же выдача.
            # Идти дальше значило бы посчитать первую страницу дважды и завысить
            # каждую позицию под ней.
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



# Сколько страниц выдачи прочесть, прежде чем сдаться в поисках себя.
# Собственный адрес продавца — это поиск внутри раздела, и его карточка
# лежит заметно за первым экраном, поэтому одна страница — не ответ. Но
# каждая страница стоит запроса, и обход прекращается сразу, как найден
# магазин.
MAX_LIST_PAGES = 6


def fetch_listing(url: str, shop: str = "", category_id=None):
    """Предложения по адресу витрины — любым способом, каким их удастся взять.

    Сперва API: на этом маркетплейсе выдача существует только там, сама
    страница — пустой каркас. Разбор разметки остаётся вторым путём: держать
    его ничего не стоит, он покрыт тестами и он же верный ответ для страницы,
    которая рисует список на сервере.
    """
    ok, res = fetch_offers_api(url, shop, category_id=category_id)
    if ok:
        return ok, res
    api_error = res
    ok2, res2 = fetch_offers_sync(url, max_pages=MAX_LIST_PAGES if shop else 1,
                                  want_seller=shop)
    if ok2:
        return ok2, res2
    # Не вышло ни то, ни другое: говорим об этом одной строкой, а не валим
    # вину на тот способ, что случайно оказался последним.
    return False, f"{api_error}; страница: {res2}"


def _norm(s: str) -> str:
    """Строка для сравнения: без знаков, в нижнем регистре, «ё» как «е».

    «ё» стоила отдельного круга. Наш товар называется «50 звезд», соседи по
    разделу пишут «50 звёзд» — для сравнения это были разные слова, и
    раздел по соседям набрал ноль голосов при ста двадцати четырёх
    подходящих строках на экране.
    """
    return re.sub(r"[^\w]+", "", str(s or "")).lower().replace("ё", "е")


def find_position(offers: list[dict], *, ad_id: str = "",
                  title: str = "", seller: str = "") -> dict | None:
    """Наша строка среди предложений: по номеру, потом по магазину, потом по названию.

    Порядок важен и он не тот, который кажется очевидным. Выдача — это копии
    ОДНОГО товара у разных продавцов, поэтому названия почти одинаковы: поиск
    по названию первым попадает в того, кто случайно оказался наверху, и это
    читается как «вы первый» независимо от того, где товар на самом деле.
    Отделяет нашу строку от чужих название магазина; название товара — лишь
    способ выбрать между своими же строками и последняя надежда, когда на
    странице продавец не указан вовсе.
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
            # Несколько товаров одного магазина на странице — по названию
            # выбирается тот, за которым следят.
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
