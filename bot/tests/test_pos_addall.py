"""«Не нашёл на витрине: 14» — и четырнадцать названий без единой причины.

Продавец нажал «Добавить все сразу» и получил список из четырнадцати
товаров, про которые бот не смог сказать ничего, кроме того, что не смог.
А исходов четыре, и делать по ним надо разное:

* **раздел витрины не определился** — лечится выбором раздела кнопками;
* **раздел большой, до нас не дочитали** — нужен раздел поуже;
* **раздел прочитан весь, товара в нём нет** — не тот раздел либо товар
  снят с публикации;
* **страница не прочиталась** — витрина не ответила, дело во времени.

В общем списке они выглядели одинаково. Различить их бот мог: и адреса
кандидатов, и признак «дочитано до конца» у него на руках — их просто не
показывали.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.selenium_settings as SS     # noqa: E402


class Screen:
    def __init__(self):
        self.text = ""
        self.message = self
        self.data = "pos:addall"

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text = text

    async def answer(self, *a, **kw):
        pass

    from_user = type("U", (), {"id": 501})()


class BulkAdd(unittest.TestCase):
    ADS = [{"id": "1", "title": "50 звезд", "category_id": None, "category": ""},
           {"id": "2", "title": "100 ЗВЁЗД", "category_id": None, "category": ""},
           {"id": "3", "title": "Аккаунт Steam", "category_id": None,
            "category": ""}]

    def setUp(self):
        self.saved: dict = {}
        self._get, self._save = SS.get_settings, SS.save_settings
        self._ads, self._find = SS._my_ads, SS._find_listing_page
        self._bind = SS._bind_panel_item
        SS.get_settings = lambda uid: self.saved
        SS.save_settings = lambda uid, s: self.saved.update(s)

        async def ads(uid, reload=False):
            return list(self.ADS)

        async def bind(uid, idx, title):
            return None

        SS._my_ads = ads
        SS._bind_panel_item = bind

    def tearDown(self):
        SS.get_settings, SS.save_settings = self._get, self._save
        SS._my_ads, SS._find_listing_page = self._ads, self._find
        SS._bind_panel_item = self._bind

    def run_with(self, outcomes):
        """outcomes: {title: (адрес, кандидаты, причина промаха)}"""

        async def find(uid, ad):
            got = outcomes.get(ad["title"], ("", [], "no-section"))
            url, urls, why = got
            return url, 1, 10, urls, why

        SS._find_listing_page = find
        cb = Screen()
        asyncio.run(SS.pos_add_all(cb))
        return cb.text


class TheCausesAreToldApart(BulkAdd):
    def test_a_missing_section_is_named_as_such(self):
        text = self.run_with({})
        self.assertIn("Не понял раздел витрины", text)

    def test_a_found_section_without_us_is_named_differently(self):
        text = self.run_with({t["title"]: ("", ["https://x/y"], "not-here")
                              for t in self.ADS})
        self.assertIn("товара в нём нет", text)
        self.assertNotIn("Не понял раздел витрины", text)

    def test_both_causes_at_once_are_counted_separately(self):
        text = self.run_with({"50 звезд": ("", ["https://x/y"], "not-here")})
        self.assertIn("товара в нём нет — 1", text)
        self.assertIn("Не понял раздел витрины — 2", text)

    def test_the_advice_differs_by_cause(self):
        """Совет «выберите раздел кнопками» бесполезен тому, у кого раздел
        уже найден."""
        text = self.run_with({t["title"]: ("", ["https://x/y"], "not-here")
                              for t in self.ADS})
        self.assertIn("снят с публикации", text)

    def test_what_did_get_added_is_still_counted(self):
        text = self.run_with({"50 звезд": ("https://x/zvezdy", ["u"], "")})
        self.assertIn("Под наблюдением новых: <b>1</b>", text)

    def test_nothing_missed_means_no_reason_blocks(self):
        text = self.run_with({t["title"]: ("https://x/y", ["u"], "")
                              for t in self.ADS})
        self.assertNotIn("Не нашёл на витрине", text)
        self.assertNotIn("Не понял раздел", text)

    def test_the_titles_are_still_listed(self):
        """Без названий продавцу непонятно, о каких товарах речь."""
        text = self.run_with({})
        self.assertIn("50 звезд", text)


class ADeepListIsNotAWrongSection(BulkAdd):
    """Список раздела бот читает не бесконечно. «Дочитали и вас там нет» и
    «не дочитали» — разные вещи: первое значит не тот раздел, второе — что
    товар просто глубже. Совет в них разный."""

    def test_an_unfinished_read_says_so(self):
        text = self.run_with({t["title"]: ("", ["u"], "partial")
                              for t in self.ADS})
        self.assertIn("до тебя не дочитал", text)

    def test_it_does_not_claim_the_item_is_absent(self):
        text = self.run_with({t["title"]: ("", ["u"], "partial")
                              for t in self.ADS})
        self.assertNotIn("товара в нём нет", text)

    def test_an_unreadable_page_is_its_own_case(self):
        text = self.run_with({t["title"]: ("", ["u"], "unread")
                              for t in self.ADS})
        self.assertIn("не ответила", text)


class WhereTheReasonIsActuallyDecided(unittest.TestCase):
    """Сводка права ровно настолько, насколько прав тот, кто её кормит.
    Здесь проверяется сам `_find_listing_page`, а не пересказ его ответа."""

    def setUp(self):
        from automation import market as M
        self.M = M
        self._urls = SS._urls_for_ad
        self._meta, self._fetch = M.category_meta, M.fetch_listing
        import storage as _S
        self._S, self._shop = _S, _S.get_shop_name
        _S.get_shop_name = lambda uid: "Spike"
        SS._urls_for_ad = lambda ad, shop="": ["https://yoomarket.net/categories/g/s"]
        M.category_meta = lambda slugs: {"slug": slugs[-1] if slugs else ""}

    def tearDown(self):
        SS._urls_for_ad = self._urls
        self._S.get_shop_name = self._shop
        self.M.category_meta, self.M.fetch_listing = self._meta, self._fetch

    def run_it(self, complete):
        offers = [{"pos": 1, "id": "9", "title": "Чужое", "price": 10,
                   "seller": "Кто-то"}]
        self.M.fetch_listing = lambda url, shop="", category_id=None: (
            True, {"offers": offers, "note": "", "total": 500,
                   "complete": complete})
        return asyncio.run(SS._find_listing_page(
            1, {"id": "1", "title": "50 звезд", "category_id": None,
                "category": ""}))

    def test_an_exhausted_list_without_us_means_the_wrong_section(self):
        self.assertEqual(self.run_it(True)[4], "not-here")

    def test_a_list_cut_short_says_so_instead(self):
        """Иначе «товара в разделе нет» — про товар, до которого просто не
        дошли, а это разные советы продавцу."""
        self.assertEqual(self.run_it(False)[4], "partial")

    def test_a_hit_carries_no_reason_at_all(self):
        offers = [{"pos": 7, "id": "1", "title": "50 звезд", "price": 10,
                   "seller": "Spike"}]
        self.M.fetch_listing = lambda url, shop="", category_id=None: (
            True, {"offers": offers, "note": "", "complete": True})
        chosen, pos, _seen, _urls, why = asyncio.run(SS._find_listing_page(
            1, {"id": "1", "title": "50 звезд", "category_id": None,
                "category": ""}))
        self.assertTrue(chosen)
        self.assertEqual(pos, 7)
        self.assertEqual(why, "")


class TheWatchThatSucceedsIsWiredUp(BulkAdd):
    def test_it_is_saved_with_its_place_and_id(self):
        self.run_with({"50 звезд": ("https://x/zvezdy", ["u"], "")})
        ws = (self.saved.get("promo_position") or {}).get("watches") or []
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["market_id"], "1")
        self.assertEqual(ws[0]["last_pos"], 1)


if __name__ == "__main__":
    unittest.main()
