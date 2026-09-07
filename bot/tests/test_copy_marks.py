"""Память копии: раздел, спрошенный один раз, за образцом и остаётся.

Раздела у товара в панели нет НИГДЕ — она задаёт его при создании и больше
не показывает (живой `/copy_debug` 07.09). Значит второй раз узнать его
неоткуда, и без памяти один и тот же вопрос повторялся бы при каждой копии
одного и того же товара.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                            # noqa: E402


class Bench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dir = storage._DATA_DIR
        storage._DATA_DIR = self.tmp.name
        storage._SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        storage._DATA_DIR = self._dir
        storage._SETTINGS_FILE = os.path.join(self._dir, "settings.json")
        self.tmp.cleanup()


class WhatWasAnsweredOnceIsKept(Bench):
    def test_nothing_is_remembered_until_it_is(self):
        self.assertEqual(storage.get_copy_marks(1, "250614"), {})

    def test_the_numbers_and_the_names_come_back(self):
        storage.remember_copy_marks(1, "250614",
                                    {"category": 613, "subcategory": 3},
                                    {"category": "Standoff 2"})
        got = storage.get_copy_marks(1, "250614")
        self.assertEqual(got["values"], {"category": 613, "subcategory": 3})
        self.assertEqual(got["labels"], {"category": "Standoff 2"})

    def test_it_is_kept_per_item_not_per_seller(self):
        """У каждого товара свой раздел. Общая память положила бы второй
        товар туда же, куда первый, — молча."""
        storage.remember_copy_marks(1, "111", {"category": 613})
        storage.remember_copy_marks(1, "222", {"category": 700})
        self.assertEqual(storage.get_copy_marks(1, "111")["values"]["category"],
                         613)
        self.assertEqual(storage.get_copy_marks(1, "222")["values"]["category"],
                         700)

    def test_and_per_seller_too(self):
        storage.remember_copy_marks(1, "111", {"category": 613})
        self.assertEqual(storage.get_copy_marks(2, "111"), {})

    def test_an_empty_answer_is_not_an_answer(self):
        """Пустое запоминать нельзя: оно перекрыло бы прежний верный ответ."""
        storage.remember_copy_marks(1, "111", {"category": 613})
        storage.remember_copy_marks(1, "111", {"category": None, "type": ""})
        self.assertEqual(storage.get_copy_marks(1, "111")["values"],
                         {"category": 613})

    def test_a_label_without_its_number_is_not_kept(self):
        """Надпись без номера отправить некуда, а на экране она врала бы."""
        storage.remember_copy_marks(1, "111", {"category": 613},
                                    {"category": "Standoff 2", "type": "Мгн."})
        self.assertEqual(storage.get_copy_marks(1, "111")["labels"],
                         {"category": "Standoff 2"})

    def test_the_seller_can_take_it_back(self):
        """Ошибиться разделом можно один раз: панель менять его не даёт."""
        storage.remember_copy_marks(1, "111", {"category": 613})
        self.assertTrue(storage.forget_copy_marks(1, "111"))
        self.assertEqual(storage.get_copy_marks(1, "111"), {})
        self.assertFalse(storage.forget_copy_marks(1, "111"))

    def test_it_does_not_grow_without_end(self):
        """Список растёт с каждым товаром, а лежит он в настройках продавца."""
        for i in range(storage._COPY_MARKS_KEEP + 20):
            storage.remember_copy_marks(1, str(i), {"category": i})
        kept = storage.get_settings(1)["copy_marks"]
        self.assertLessEqual(len(kept), storage._COPY_MARKS_KEEP)
        # Вытесняются старые, а не свежие: копируют обычно последнее.
        self.assertIn(str(storage._COPY_MARKS_KEEP + 19), kept)

    def test_it_survives_a_restart(self):
        storage.remember_copy_marks(1, "111", {"category": 613})
        storage._SETTINGS_CACHE = None
        self.assertEqual(storage.get_copy_marks(1, "111")["values"]["category"],
                         613)


class TheDefaultStockIsTheSellersOwnDecision(unittest.TestCase):
    """У товара с авто-выдачей остаток — это сами коды или аккаунты, и их
    получает покупатель. Заготовка, подставленная за продавца, ушла бы
    живому человеку вместо товара, поэтому по умолчанию список пуст."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.tmp.cleanup()

    def test_empty_until_the_seller_says_otherwise(self):
        self.assertEqual(storage.get_copy_stock(1), [])

    def test_what_was_given_comes_back_in_order(self):
        """Порядок — это порядок выдачи покупателям."""
        storage.set_copy_stock(1, ["KEY-1111", "KEY-2222", "KEY-3333"])
        self.assertEqual(storage.get_copy_stock(1),
                         ["KEY-1111", "KEY-2222", "KEY-3333"])

    def test_blank_lines_are_not_goods(self):
        """Пустая строка, уехавшая покупателю, — это оплаченная пустота."""
        n = storage.set_copy_stock(1, ["KEY-1", "", "   ", "KEY-2"])
        self.assertEqual(n, 2)
        self.assertEqual(storage.get_copy_stock(1), ["KEY-1", "KEY-2"])

    def test_it_can_be_taken_back(self):
        storage.set_copy_stock(1, ["KEY-1"])
        self.assertEqual(storage.set_copy_stock(1, []), 0)
        self.assertEqual(storage.get_copy_stock(1), [])

    def test_it_is_the_sellers_own(self):
        storage.set_copy_stock(1, ["KEY-1"])
        self.assertEqual(storage.get_copy_stock(2), [])

    def test_it_does_not_become_a_warehouse(self):
        storage.set_copy_stock(1, [f"K{i}" for i in range(500)])
        self.assertLessEqual(len(storage.get_copy_stock(1)),
                             storage._COPY_STOCK_MAX)


if __name__ == "__main__":
    unittest.main()
