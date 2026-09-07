"""Товары читаются ВСЕ, а не первая страница.

`/ads` отдаёт список курсором, и одна страница — это два-три десятка
объявлений. Все, кто спрашивал «мои товары», читали только первую: у
продавца с полусотней объявлений половина просто не существовала — ни в
списке, ни в шаблонной копии, ни в статистике, ни в автовозврате истёкших.
Снаружи это «бот загружает не все товары».
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from api.yoomarket import YooMarketAPI                     # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Paged(YooMarketAPI):
    """Маркетплейс, отдающий список страницами."""

    def __init__(self, pages, meta_key="next_cursor"):
        super().__init__("token")
        self.pages, self.meta_key = pages, meta_key
        self.asked: list = []

    async def get_ads(self, cursor=None):
        self.asked.append(cursor)
        rows, nxt = self.pages[cursor]
        body = {"data": rows}
        if nxt:
            body["meta"] = {self.meta_key: nxt}
        return body


class EveryPageIsRead(unittest.TestCase):
    def test_all_three_pages_come_back(self):
        api = Paged({
            None: ([{"id": 1}, {"id": 2}], "c2"),
            "c2": ([{"id": 3}, {"id": 4}], "c3"),
            "c3": ([{"id": 5}], None),
        })
        got = run(api.get_all_ads())
        self.assertEqual([a["id"] for a in got], [1, 2, 3, 4, 5])
        self.assertEqual(api.asked, [None, "c2", "c3"])

    def test_one_page_costs_one_request(self):
        """Лишний запрос на каждом экране — это лишняя секунда ожидания."""
        api = Paged({None: ([{"id": 1}], None)})
        self.assertEqual(len(run(api.get_all_ads())), 1)
        self.assertEqual(api.asked, [None])

    def test_the_cursor_may_live_in_links(self):
        """Курсор этот маркетплейс кладёт то в `meta`, то в `links`."""
        api = Paged({None: ([{"id": 1}], None)})

        async def two_pages(cursor=None):
            api.asked.append(cursor)
            if cursor is None:
                return {"data": [{"id": 1}], "links": {"next": "c2"}}
            return {"data": [{"id": 2}]}

        api.get_ads = two_pages
        self.assertEqual([a["id"] for a in run(api.get_all_ads())], [1, 2])

    def test_a_repeated_cursor_does_not_loop_forever(self):
        """Сервер, повторяющий свой же курсор, крутил бы этот цикл вечно —
        а продавец смотрел бы на «⏳ Загружаю товары…» до перезапуска."""
        api = Paged({None: ([{"id": 1}], "c1"), "c1": ([{"id": 2}], "c1")})
        got = run(api.get_all_ads())
        self.assertEqual([a["id"] for a in got], [1, 2])
        self.assertLessEqual(len(api.asked), 3)

    def test_the_same_item_on_two_pages_is_one_item(self):
        """Список меняется, пока мы его листаем: товар со сдвинувшейся
        страницы приходил бы дважды и считался бы за два."""
        api = Paged({None: ([{"id": 1}, {"id": 2}], "c2"),
                     "c2": ([{"id": 2}, {"id": 3}], None)})
        self.assertEqual([a["id"] for a in run(api.get_all_ads())], [1, 2, 3])

    def test_an_empty_page_ends_it(self):
        api = Paged({None: ([], "c2")})
        self.assertEqual(run(api.get_all_ads()), [])
        self.assertEqual(api.asked, [None])

    def test_the_page_limit_is_a_limit(self):
        """Бесконечный курсор с новыми значениями — тоже не повод висеть."""
        pages = {None: ([{"id": 0}], "c1")}
        for i in range(1, 200):
            pages[f"c{i}"] = ([{"id": i}], f"c{i + 1}")
        api = Paged(pages)
        run(api.get_all_ads(max_pages=5))
        self.assertEqual(len(api.asked), 5)

    def test_junk_rows_do_not_get_in(self):
        api = Paged({None: ([{"id": 1}, "мусор", None], None)})
        self.assertEqual(run(api.get_all_ads()), [{"id": 1}])


class TheCursorIsWhereThisMarketplacePutsIt(unittest.TestCase):
    """Живой ответ 08.09, дословно:

        "meta":  {"per_page": 100, "has_more": true}
        "links": {"next_cursor": "eyJpZCI6MTA5…", "prev_cursor": null}

    Курсор лежит в `links.next_cursor`. Мы искали `meta.next_cursor`,
    `meta.next` и `links.next` — ни одного из них тут нет, и листание не
    начиналось ВООБЩЕ. Продавец видел 15 объявлений из полусотни.
    """

    def real_shape(self, pages):
        api = YooMarketAPI("token")
        api.asked = []

        async def get_ads(cursor=None):
            api.asked.append(cursor)
            rows, nxt = pages[cursor]
            return {"data": rows,
                    "meta": {"per_page": 100, "has_more": bool(nxt)},
                    "links": {"next_cursor": nxt, "prev_cursor": None}}

        api.get_ads = get_ads
        return api

    def test_the_next_pages_are_read(self):
        api = self.real_shape({
            None: ([{"id": 250730}], "eyJpZCI6MTA5"),
            "eyJpZCI6MTA5": ([{"id": 250731}], None),
        })
        got = run(api.get_all_ads())
        self.assertEqual([a["id"] for a in got], [250730, 250731])

    def test_has_more_false_stops_it(self):
        """У последней страницы курсор остаётся от предыдущей: без этого
        стопа цикл сходил бы за ней ещё раз."""
        api = YooMarketAPI("token")
        api.asked = []

        async def get_ads(cursor=None):
            api.asked.append(cursor)
            return {"data": [{"id": 1}],
                    "meta": {"has_more": False},
                    "links": {"next_cursor": "старый"}}

        api.get_ads = get_ads
        run(api.get_all_ads())
        self.assertEqual(api.asked, [None])

    def test_a_full_url_is_not_a_cursor(self):
        """`links.next` у Laravel бывает адресом страницы. Отправленный как
        курсор, он вернул бы ту же страницу — и цикл бы закрутился."""
        api = YooMarketAPI("token")
        api.asked = []

        async def get_ads(cursor=None):
            api.asked.append(cursor)
            return {"data": [{"id": 1}],
                    "links": {"next": "https://api.yoo.market/ads?page=2"}}

        api.get_ads = get_ads
        run(api.get_all_ads())
        self.assertEqual(api.asked, [None])


class TheCategoryReferenceIsReadWholeToo(unittest.TestCase):
    """Тот же курсор — и у справочника разделов. «Категорий в справочнике:
    100» было ровно одной страницей: верхний уровень этого маркетплейса —
    сотни игр, и по имени находилась только первая сотня."""

    def test_all_pages_of_categories(self):
        api = YooMarketAPI("token")
        pages = {
            None: ([{"id": 1, "title": "Brawl Stars"}], "c2"),
            "c2": ([{"id": 77, "title": "Black Russia"}], None),
        }

        async def get(path, params=None):
            cursor = (params or {}).get("cursor")
            rows, nxt = pages[cursor]
            return {"data": rows,
                    "meta": {"per_page": 100, "has_more": bool(nxt)},
                    "links": {"next_cursor": nxt}}

        api._get = get
        got = run(api.get_categories())
        self.assertEqual([c["title"] for c in got],
                         ["Brawl Stars", "Black Russia"])


class TheExpiredRestoreSeesEveryPageToo(unittest.TestCase):
    """Истёкшее объявление со второй страницы не возвращалось никогда:
    проход просто его не видел."""

    def test_it_reads_all_pages(self):
        api = Paged({
            None: ([{"id": 1, "status": "active"}], "c2"),
            "c2": ([{"id": 2, "status": "expired"}], None),
        })
        report = run(api.restore_ads(dry_run=True, require_stock=False))
        self.assertEqual(report["total"], 2, report)


if __name__ == "__main__":
    unittest.main()
