"""Цепочка разделов маркетплейса — единственный источник раздела для копии.

Живой ответ панели 07.09 по товару 250614: раздела у товара в панели нет
НИГДЕ — ни в форме правки, ни на карточке, ни в строке списка. Панель
задаёт его один раз при создании и больше не показывает.

Значит взять его можно только у маркетплейса. И взять надо ЦЕПОЧКУ, а не
имя листа: товар лежит в «Аккаунтах», а панель раскладывает товары по играм
(«Standoff 2»), то есть нужное слово стоит на среднем уровне дерева.
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


class TheChainIsBuiltByClimbingWhenTheMarketplaceNamesTheParent(
        unittest.TestCase):
    """Подъём стоит по запросу на уровень. Обход же сверху упирается в
    уровень, где сотни строк, и бюджет кончается раньше, чем находится
    лист, — то есть цепочка не собирается вовсе."""

    def api(self, tree: dict) -> YooMarketAPI:
        api = YooMarketAPI("token")
        self.asked: list = []

        async def fake_get(path, **kw):
            self.asked.append(path)
            cid = int(path.rsplit("/", 1)[-1])
            if cid not in tree:
                raise RuntimeError("нет такого раздела")
            return {"data": tree[cid]}

        api._get = fake_get
        return api

    TREE = {
        5221: {"id": 5221, "name": "Аккаунты", "parent_id": 613},
        613: {"id": 613, "name": "Standoff 2", "parent_id": 1},
        1: {"id": 1, "name": "Игры", "parent_id": None},
    }

    def test_the_whole_chain_comes_back_top_down(self):
        got = run(self.api(self.TREE).category_path(5221))
        self.assertEqual(got, ["Игры", "Standoff 2", "Аккаунты"])

    def test_it_costs_one_request_per_level(self):
        api = self.api(self.TREE)
        run(api.category_path(5221))
        self.assertEqual(len(self.asked), 3, self.asked)

    def test_a_loop_in_the_tree_does_not_hang_it(self):
        run(self.api({1: {"id": 1, "name": "Круг", "parent_id": 1}})
            .category_path(1))

    def test_a_nested_parent_object_is_understood_too(self):
        tree = {7: {"id": 7, "name": "Аккаунты", "parent": {"id": 8}},
                8: {"id": 8, "name": "Standoff 2"}}
        self.assertEqual(run(self.api(tree).category_path(7)),
                         ["Standoff 2", "Аккаунты"])

    def test_a_marketplace_that_hides_the_parent_falls_back_to_walking(self):
        """Родителя в ответе может не быть — тогда остаётся обход дерева.
        Молчаливый пустой ответ здесь означал бы «раздел не найден» на
        ровном месте."""
        api = YooMarketAPI("token")

        async def no_single(path, **kw):
            raise RuntimeError("404")

        async def levels(max_pages=2, parent_id=None):
            if parent_id is None:
                return [{"id": 1, "name": "Игры", "is_leaf": False}]
            if parent_id == 1:
                return [{"id": 613, "name": "Standoff 2", "is_leaf": False}]
            if parent_id == 613:
                return [{"id": 5221, "name": "Аккаунты", "is_leaf": True}]
            return []

        api._get = no_single
        api.get_categories = levels
        self.assertEqual(run(api.category_path(5221)),
                         ["Игры", "Standoff 2", "Аккаунты"])

    def test_a_number_that_is_not_a_number_is_not_a_crash(self):
        self.assertEqual(run(self.api(self.TREE).category_path("—")), [])


if __name__ == "__main__":
    unittest.main()
