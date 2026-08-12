"""«Не нашёл на витрине: 14» — и четырнадцать названий без единой причины.

Продавец нажал «Добавить все сразу» и получил список из четырнадцати
товаров, про которые бот не смог сказать ничего, кроме того, что не смог.
А причин ровно две, и делать по ним надо разное:

* **раздел витрины не определился** — лечится выбором раздела кнопками;
* **раздел нашёлся, а товара в нём нет** — значит товар снят с публикации
  или стоит в другом разделе.

В общем списке они выглядели одинаково. Различить их бот мог: `урлы`
кандидатов у него на руках — их просто не показывали.
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
        """outcomes: {title: (адрес, кандидаты)}"""

        async def find(uid, ad):
            url, urls = outcomes.get(ad["title"], ("", []))
            return url, 1, 10, urls

        SS._find_listing_page = find
        cb = Screen()
        asyncio.run(SS.pos_add_all(cb))
        return cb.text


class TheTwoCausesAreToldApart(BulkAdd):
    def test_a_missing_section_is_named_as_such(self):
        text = self.run_with({})
        self.assertIn("Не понял раздел витрины", text)

    def test_a_found_section_without_us_is_named_differently(self):
        text = self.run_with({t["title"]: ("", ["https://x/y"])
                              for t in self.ADS})
        self.assertIn("товара в нём нет", text)
        self.assertNotIn("Не понял раздел витрины", text)

    def test_both_causes_at_once_are_counted_separately(self):
        text = self.run_with({"50 звезд": ("", ["https://x/y"])})
        self.assertIn("товара в нём нет — 1", text)
        self.assertIn("Не понял раздел витрины — 2", text)

    def test_the_advice_differs_by_cause(self):
        """Совет «выберите раздел кнопками» бесполезен тому, у кого раздел
        уже найден."""
        text = self.run_with({t["title"]: ("", ["https://x/y"])
                              for t in self.ADS})
        self.assertIn("снятый с публикации", text)

    def test_what_did_get_added_is_still_counted(self):
        text = self.run_with({"50 звезд": ("https://x/zvezdy", ["u"])})
        self.assertIn("Под наблюдением новых: <b>1</b>", text)

    def test_nothing_missed_means_no_reason_blocks(self):
        text = self.run_with({t["title"]: ("https://x/y", ["u"])
                              for t in self.ADS})
        self.assertNotIn("Не нашёл на витрине", text)
        self.assertNotIn("Не понял раздел", text)

    def test_the_titles_are_still_listed(self):
        """Без названий продавцу непонятно, о каких товарах речь."""
        text = self.run_with({})
        self.assertIn("50 звезд", text)


class TheWatchThatSucceedsIsWiredUp(BulkAdd):
    def test_it_is_saved_with_its_place_and_id(self):
        self.run_with({"50 звезд": ("https://x/zvezdy", ["u"])})
        ws = (self.saved.get("promo_position") or {}).get("watches") or []
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["market_id"], "1")
        self.assertEqual(ws[0]["last_pos"], 1)


if __name__ == "__main__":
    unittest.main()
