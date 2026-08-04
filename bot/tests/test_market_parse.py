"""Pulling the offers list out of a storefront page.

The storefront is a Next.js app and its markup is not a contract, so nothing
here asserts on a CSS selector. What is asserted is the property the parser is
built on: the page carries the offers as JSON somewhere, and the array that
reads like a list of offers is the one to take.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import market as M          # noqa: E402


def next_data_page(payload: dict) -> str:
    return (
        "<!DOCTYPE html><html><head><title>Юмаркет</title></head><body>"
        "<div id=__next></div>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False) +
        "</script></body></html>"
    )


def flight_page(payload: dict) -> str:
    """The RSC form: JSON escaped inside self.__next_f.push([1,"…"]) chunks."""
    raw = json.dumps(payload, ensure_ascii=False)
    escaped = json.dumps(raw)[1:-1]              # escape, drop the outer quotes
    half = len(escaped) // 2
    return (
        "<!DOCTYPE html><html><body>"
        f'<script>self.__next_f.push([1,"{escaped[:half]}"])</script>'
        f'<script>self.__next_f.push([1,"{escaped[half:]}"])</script>'
        "</body></html>"
    )


OFFERS = [
    {"id": 1, "title": "Аккаунт Steam", "price": 100, "shop": {"name": "Конкурент А"}},
    {"id": 2, "title": "Аккаунт Steam", "price": 120, "shop": {"name": "Конкурент Б"}},
    {"id": 220075, "title": "Аккаунт Steam", "price": 130, "shop": {"name": "Спайк"}},
]


class OfferListDetection(unittest.TestCase):
    def test_finds_offers_in_next_data(self):
        html = next_data_page({"props": {"pageProps": {"offers": OFFERS}}})
        lists = []
        for blob in M._json_blobs(html):
            lists.extend(M._offer_lists(blob))
        self.assertTrue(lists)
        rows = M._normalize(max(lists, key=len))
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[2]["seller"], "Спайк")
        self.assertEqual(rows[2]["price"], 130.0)
        self.assertEqual(rows[2]["id"], "220075")

    def test_finds_offers_in_flight_chunks(self):
        html = flight_page({"data": {"items": OFFERS}})
        lists = []
        for blob in M._json_blobs(html):
            lists.extend(M._offer_lists(blob))
        self.assertTrue(lists, "RSC chunks should still yield the offers")
        rows = M._normalize(max(lists, key=len))
        self.assertEqual(len(rows), 3)

    def test_prefers_the_longest_list_over_a_recommendations_block(self):
        html = next_data_page({"props": {"pageProps": {
            "similar": OFFERS[:3],
            "offers": OFFERS + [
                {"id": 9, "title": "Аккаунт Steam", "price": 150,
                 "shop": {"name": "Г"}},
                {"id": 10, "title": "Аккаунт Steam", "price": 160,
                 "shop": {"name": "Д"}},
            ]}}})
        lists = []
        for blob in M._json_blobs(html):
            lists.extend(M._offer_lists(blob))
        self.assertEqual(len(max(lists, key=len)), 5)

    def test_ignores_arrays_that_are_not_offers(self):
        html = next_data_page({"props": {"pageProps": {
            "breadcrumbs": [{"title": "Игры"}, {"title": "Steam"}, {"title": "Аккаунты"}],
            "prices": [10, 20, 30],
        }}})
        lists = []
        for blob in M._json_blobs(html):
            lists.extend(M._offer_lists(blob))
        self.assertEqual(lists, [], "titles without prices are not offers")

    def test_a_page_with_nothing_parseable_is_reported_not_guessed(self):
        self.assertEqual(M._json_blobs("<html><body>ничего</body></html>"), [])


class PriceShapes(unittest.TestCase):
    def test_reads_the_shapes_this_api_actually_returns(self):
        self.assertEqual(M._num(149), 149.0)
        self.assertEqual(M._num("149 ₽"), 149.0)
        self.assertEqual(M._num("1 299,50 ₽"), 1299.50)
        self.assertEqual(M._num({"amount": 149, "currency": "RUB"}), 149.0)
        self.assertEqual(M._num({"base_amount": 99}), 99.0)
        self.assertIsNone(M._num(None))
        self.assertIsNone(M._num("бесплатно"))

    def test_seller_out_of_the_shapes_a_shop_arrives_in(self):
        self.assertEqual(M._seller_of({"shop": {"name": "Спайк"}}), "Спайк")
        self.assertEqual(M._seller_of({"seller": "Спайк"}), "Спайк")
        self.assertEqual(M._seller_of({"store": {"title": "Спайк"}}), "Спайк")
        self.assertEqual(M._seller_of({"nothing": 1}), "")

    def test_cheapest_ignores_rows_without_a_price(self):
        rows = [{"price": None}, {"price": 120.0}, {"price": 90.0}]
        self.assertEqual(M.cheapest(rows), 90.0)
        self.assertIsNone(M.cheapest([{"price": None}]))
        self.assertIsNone(M.cheapest([]))


# Modelled on the page the seller sent: a category listing of Black Russia
# currency — cards with a discount, a seller and a rating.
#
# Their shop, Spike, appeared third *on the screenshot*, but only because they
# had scrolled down to it. The real listing is long and their card sits well
# below the fold, so the fixture puts Spike deep in the list on purpose: a
# parser that latches onto the top of the page passes a four-card fixture and
# fails on the real one.
CARD = """
<div class="card"><a href="/o/{id}"><h3>{title}</h3>
{badge}<span class="cat">Black Russia / Вирты</span>
<span class="price">{price}&nbsp;₽</span><span class="bonus">+11.95</span>
{old}<div class="foot"><span class="shop">{shop}</span>
<span class="rating">★ {rating} · {reviews} отзывов</span></div></a></div>
"""

REAL_CARDS = [
    dict(id=1, title="АВТОВЫДАЧА 3.000.000 Все серверы", badge="",
         price="230", old="", shop="GigShop", rating="5", reviews="11"),
    dict(id=2, title="АВТОВЫДАЧА 3.000.000 + БОНУС Все серверы БЫСТРО",
         badge='<span class="sale">-88%</span>', price="239",
         old='<s class="old">2&nbsp;112&nbsp;₽</s>', shop="GadjiSeller",
         rating="4.97", reviews="1 620"),
    dict(id=3, title="3.000.000р Бонусы Любой сервер Быстро",
         badge='<span class="sale">-51%</span>', price="239,99",
         old='<s class="old">490&nbsp;₽</s>', shop="Spike",
         rating="4.84", reviews="1 242"),
    dict(id=4, title="АВТОВЫДАЧА 3.000.000 БОНУСЫ ВСЕ СЕРВЕРА",
         badge="", price="239", old="", shop="BlackMarket",
         rating="4.89", reviews="168"),
]


# Where Spike really sits: far enough down that only a parser reading the whole
# list finds it.
SPIKE_POS = 17
FILLER_BEFORE = SPIKE_POS - 3          # Spike is 3rd among REAL_CARDS
FILLER_AFTER = 9


def _filler(n: int, start: int) -> str:
    return "".join(
        CARD.format(id=900 + start + i, title=f"АВТОВЫДАЧА вирты лот {i}",
                    badge="", price=str(220 + i), old="",
                    shop=f"Продавец{start + i}", rating="4.8",
                    reviews=str(100 + i))
        for i in range(n))


def rendered_page(long: bool = False) -> str:
    """The listing as markup only — no payload embedded anywhere.

    `long=True` buries Spike where it actually is, past the fold.
    """
    if not long:
        return ("<!DOCTYPE html><html><head><title>Black Russia</title>"
                "<script>window.x=1</script></head><body><main>"
                + "".join(CARD.format(**c) for c in REAL_CARDS)
                + "</main></body></html>")
    return ("<!DOCTYPE html><html><head><title>Black Russia</title></head>"
            "<body><main>"
            + _filler(FILLER_BEFORE, 1)
            + "".join(CARD.format(**c) for c in REAL_CARDS)
            + _filler(FILLER_AFTER, 500)
            + "</main></body></html>")


class TheRealListing(unittest.TestCase):
    def test_markup_only_page_still_yields_the_right_order(self):
        rows = M._normalize(M._offers_from_text(rendered_page()))
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["seller"] for r in rows],
                         ["GigShop", "GadjiSeller", "Spike", "BlackMarket"])
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3, 4])

    def test_a_shop_below_the_fold_is_found_where_it_actually_is(self):
        """The whole point of the feature: the position nobody scrolled to."""
        rows = M._normalize(M._offers_from_text(rendered_page(long=True)))
        self.assertEqual(len(rows), FILLER_BEFORE + 4 + FILLER_AFTER)
        spike = find_position_(rows, seller="Spike")
        self.assertEqual(spike["pos"], SPIKE_POS)
        self.assertEqual(spike["price"], 239.99,
                         "found some other card that far down")

    def test_a_long_list_does_not_collapse_to_the_top_of_the_page(self):
        rows = M._normalize(M._offers_from_text(rendered_page(long=True)))
        self.assertNotEqual(find_position_(rows, seller="Spike")["pos"], 1)
        self.assertEqual(rows[0]["seller"], "Продавец1")

    def test_the_sale_price_is_read_not_the_struck_through_one(self):
        rows = M._normalize(M._offers_from_text(rendered_page()))
        spike = find_position_(rows, seller="Spike")
        self.assertEqual(spike["price"], 239.99, "took the crossed-out 490")

    def test_a_review_count_is_never_taken_for_a_price(self):
        rows = M._normalize(M._offers_from_text(rendered_page()))
        self.assertNotIn(1620.0, [r["price"] for r in rows])
        self.assertIsNone(M._num("1 620 отзывов"))
        self.assertIsNone(M._num("4.97 · 168 отзывов"))

    def test_the_same_listing_as_json_reads_the_same_way(self):
        offers = [{"id": c["id"], "title": c["title"],
                   "price": {"current": c["price"].replace(",", "."),
                             "old_price": 490},
                   "shop": {"name": c["shop"]},
                   "rating": c["rating"]} for c in REAL_CARDS]
        html = next_data_page({"props": {"pageProps": {"offers": offers}}})
        lists = []
        for blob in M._json_blobs(html):
            lists.extend(M._offer_lists(blob))
        rows = M._normalize(max(lists, key=len))
        self.assertEqual(find_position_(rows, seller="Spike")["pos"], 3)
        self.assertEqual(find_position_(rows, seller="Spike")["price"], 239.99)

    def test_a_card_whose_name_is_nested_is_still_an_offer(self):
        """Price + seller + rating is an offer even with no title field."""
        node = {"price": 239, "shop": {"name": "Spike"}, "rating": 4.84}
        self.assertTrue(M._looks_like_offer(node))
        self.assertFalse(M._looks_like_offer({"shop": {"name": "Spike"}}))

    def test_too_few_cards_is_not_a_listing(self):
        one = ("<html><body>" + CARD.format(**REAL_CARDS[0])
               + "</body></html>")
        self.assertEqual(M._offers_from_text(one), [])

    def test_a_page_header_full_of_prices_does_not_shift_the_first_card(self):
        """The real page has a header, banners and a «похожие» block.

        The first card's window used to reach back to the top of the document
        and pick up whatever price it found there.
        """
        noise = "<header>" + "<span>от 99&nbsp;₽</span>" * 20 + "</header>"
        page = ("<html><body>" + noise
                + "".join(CARD.format(**c) for c in REAL_CARDS)
                + "<aside>Похожие: <span>1&nbsp;₽</span></aside></body></html>")
        rows = M._normalize(M._offers_from_text(page))
        self.assertEqual([r["seller"] for r in rows],
                         ["GigShop", "GadjiSeller", "Spike", "BlackMarket"])
        self.assertEqual(rows[0]["price"], 230.0, "took a price from the header")
        self.assertEqual(find_position_(rows, seller="Spike")["price"], 239.99)

    def test_a_seller_with_no_reviews_does_not_shift_the_ones_below(self):
        """A new seller has no rating, so their card has no tail to find.

        They are missed — nothing can be done about that from the markup — but
        the cards below must keep their own prices rather than inherit theirs.
        """
        no_rating = ('<div class="card"><h3>Новый продавец</h3>'
                     '<span class="price">100&nbsp;₽</span>'
                     '<span class="shop">Новичок</span></div>')
        page = ("<html><body>" + CARD.format(**REAL_CARDS[0]) + no_rating
                + "".join(CARD.format(**c) for c in REAL_CARDS[1:])
                + "</body></html>")
        rows = M._normalize(M._offers_from_text(page))
        spike = find_position_(rows, seller="Spike")
        self.assertEqual(spike["price"], 239.99)


def find_position_(rows, **kw):
    from automation.market import find_position
    return find_position(rows, **kw)


class Balanced(unittest.TestCase):
    def test_extracts_one_complete_object(self):
        text = 'prefix{"a":{"b":[1,2]},"c":"}"}suffix'
        self.assertEqual(M._balanced(text, text.index("{")),
                         '{"a":{"b":[1,2]},"c":"}"}')

    def test_unterminated_object_yields_nothing(self):
        self.assertEqual(M._balanced('{"a":1', 0), "")

    def test_escaped_quote_inside_a_string_does_not_end_it(self):
        text = r'{"a":"he said \"hi\"","b":2}'
        self.assertEqual(M._balanced(text, 0), text)


if __name__ == "__main__":
    unittest.main()
