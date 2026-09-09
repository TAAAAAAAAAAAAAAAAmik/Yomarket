"""Публикация товара: маркетплейс первым, панель запасной.

Живой отказ 08.09: панель отвечает на «Опубликовать» кодом 200 и текстом
«Извините! У вас нет прав для выполнения этого действия», а флага публикации
среди её полей нет вовсе. Товар при этом создан, остатки в нём есть — и
остаётся черновиком.

У маркетплейса для этого есть свой документированный путь —
`POST /ads/{id}/publish`, тот самый, которым возвращаются истёкшие
объявления. Дорога, которая отказывает, не должна быть первой.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as PANEL                      # noqa: E402
from handlers import panel_items as P                      # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Api:
    def __init__(self, state_after="moderate", fail=None, before="draft",
                 answer=None):
        self.state_after, self.fail, self.before = state_after, fail, before
        self.answer = answer
        self.published: list = []

    async def publish_ad(self, ad_id):
        if self.fail:
            raise RuntimeError(self.fail)
        self.published.append(str(ad_id))
        return self.answer if self.answer is not None else {
            "data": {"status": self.state_after}}

    async def get_ad(self, ad_id):
        state = self.state_after if self.published else self.before
        return {"data": {"id": ad_id, "status": state}}


class Bench(unittest.TestCase):
    def setUp(self):
        self.panel_calls: list = []
        self._panel = PANEL.panel_publish_item_sync
        self.panel_answer = (False, "нет прав")

        def fake_panel(cookies, item_id, uid=None, public=True,
                       resource="items"):
            self.panel_calls.append(str(item_id))
            return self.panel_answer

        PANEL.panel_publish_item_sync = fake_panel

    def tearDown(self):
        PANEL.panel_publish_item_sync = self._panel

    def publish(self, api, cookies="c=1"):
        return run(P.publish_item_sync_first(api, cookies, "250713", 7))


class TheMarketplaceGoesFirst(Bench):
    def test_it_publishes_and_the_panel_is_not_touched(self):
        api = Api()
        ok, note = self.publish(api)
        self.assertTrue(ok, note)
        self.assertEqual(api.published, ["250713"])
        self.assertEqual(self.panel_calls, [],
                         "панель тревожить незачем — уже опубликован")

    def test_the_status_is_re_read_not_assumed(self):
        """HTTP 200 у этого маркетплейса приходит и на отказ. В отчёт идёт
        то, что показывает статус, а не то, что мы отправили."""
        api = Api(state_after="draft")
        ok, note = self.publish(api)
        self.assertFalse(ok, note)
        self.assertIn("статус", note)

    def test_the_panel_is_the_fallback(self):
        api = Api(fail="HTTP 403")
        self.panel_answer = (True, "через поле «public»")
        ok, note = self.publish(api)
        self.assertTrue(ok, note)
        self.assertEqual(self.panel_calls, ["250713"])
        self.assertIn("панель", note)

    def test_both_refusals_are_named(self):
        """Одна причина из двух — половина правды, и по ней продавец пойдёт
        чинить не то. Живой случай: панель говорит «нет прав», а вопрос в
        том, что ответил маркетплейс."""
        api = Api(fail="empty_images")
        ok, note = self.publish(api)
        self.assertFalse(ok)
        self.assertIn("empty_images", note)
        self.assertIn("нет прав", note)

    def test_without_a_marketplace_token_the_panel_still_tries(self):
        self.panel_answer = (True, "действие «Опубликовать»")
        ok, _note = self.publish(None)
        self.assertTrue(ok)
        self.assertEqual(self.panel_calls, ["250713"])

    def test_with_neither_it_says_so_instead_of_lying(self):
        ok, note = self.publish(None, cookies="")
        self.assertFalse(ok)
        self.assertTrue(note)

    def test_a_status_the_marketplace_never_publishes_is_named(self):
        """«Статус остался unpublish» — это факт, а не причина, и делать с
        ним продавцу нечего. Про этот статус известно ровно то, что через
        API маркетплейс его не публикует, а на сайте кнопка работает."""
        api = Api(state_after="unpublish", before="unpublish")
        ok, note = self.publish(api)
        self.assertFalse(ok)
        self.assertIn("на сайте", note)
        self.assertIn("unpublish", note)

    def test_the_marketplace_answer_is_shown_not_swallowed(self):
        """Тело ответа выбрасывалось, и разбор упирался в «принял, а статус
        не изменился» — без единого слова о том, что он сказал."""
        api = Api(state_after="unpublish", before="unpublish",
                  answer={"message": "incorrect_status"})
        _ok, note = self.publish(api)
        self.assertIn("incorrect_status", note)

    def test_an_already_published_item_is_not_published_twice(self):
        """Повторная публикация живого товара — лишний запрос и лишний
        отказ: маркетплейс отвечает на такое `incorrect_status`."""
        api = Api(before="active")
        ok, note = self.publish(api)
        self.assertTrue(ok, note)
        self.assertEqual(api.published, [])
        self.assertEqual(self.panel_calls, [])

    def test_a_dead_panel_does_not_eat_the_answer(self):
        """Товар уже создан. Исключение отсюда съело бы отчёт о нём."""
        def boom(*a, **kw):
            raise RuntimeError("сеть")

        PANEL.panel_publish_item_sync = boom
        ok, note = self.publish(Api(fail="HTTP 500"))
        self.assertFalse(ok)
        self.assertIn("сеть", note)


if __name__ == "__main__":
    unittest.main()
