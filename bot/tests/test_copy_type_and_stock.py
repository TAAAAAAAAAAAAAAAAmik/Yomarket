"""Тип выдачи и остатки — без ручного ввода.

Два места, где продавец ещё вписывал руками:

* **тип выдачи.** Он есть у объявления в Integration API (`type =
  auto-delivery`), и словарь у панели ТОТ ЖЕ: отчёт бота печатает «надпись
  (значение)», и у живого товара там стояло «Авто-выдача (auto-delivery)».
  Совпадение буквой, а не по смыслу, — значит брать можно;
* **остатки.** Позиции авто-выдачи бот придумать не может: это сам товар.
  Но список, присланный руками ОДИН раз, запоминается и кладётся дальше
  сам.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402
from handlers import panel_items as PI                     # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TheDeliveryTypeComesFromTheMarketplace(unittest.TestCase):
    """Живой ответ 08.09: у объявления `type = auto-delivery`, а панель тем
    же словом называет значение своего поля «Тип выдачи»."""

    def test_the_source_type_is_offered_to_the_form(self):
        from handlers import create_ad as C
        options = [{"label": "Мгновенная выдача", "value": "instant"},
                   {"label": "Авто-выдача", "value": "auto-delivery"},
                   {"label": "Ручная выдача", "value": "manual"}]
        got, how = C._pick_option(options, "auto-delivery", "", [], "", "")
        self.assertEqual(got["value"], "auto-delivery")
        self.assertEqual(how, "номер образца")

    def test_a_type_the_form_does_not_know_is_not_forced(self):
        """Словарь мог разойтись — тогда вопрос честнее подстановки."""
        from handlers import create_ad as C
        options = [{"label": "Ручная выдача", "value": "manual"}]
        got, _how = C._pick_option(options, "auto-delivery", "", [], "", "")
        self.assertIsNone(got)


class TheStockListIsRememberedAfterTheFirstTime(unittest.TestCase):
    """Позиции авто-выдачи бот не выдумывает — это сам товар. Но список,
    присланный руками один раз, дальше кладётся сам."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")
        import features
        self.features = features
        self._shown = features.ad_templates_shown
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def test_the_first_list_becomes_the_default(self):
        self.assertTrue(PI._remember_stock(7, ["KEY-1111", "KEY-2222"]))
        self.assertEqual(storage.get_copy_stock(7), ["KEY-1111", "KEY-2222"])

    def test_a_later_list_does_not_overwrite_it_silently(self):
        """Заготовка уходит живым покупателям. Подменить её тем, что
        продавец прислал одному товару, значит подменить молча."""
        PI._remember_stock(7, ["KEY-1111"])
        self.assertFalse(PI._remember_stock(7, ["ДРУГОЕ"]))
        self.assertEqual(storage.get_copy_stock(7), ["KEY-1111"])

    def test_it_is_not_remembered_for_whom_the_copy_is_closed(self):
        """Копия — только админам, и заготовка нужна ей одной."""
        self.features.ad_templates_shown = lambda uid: False
        self.assertFalse(PI._remember_stock(7, ["KEY-1111"]))
        self.assertEqual(storage.get_copy_stock(7), [])

    def test_a_broken_storage_does_not_eat_the_report(self):
        """Остатки уже добавлены. Исключение отсюда съело бы отчёт о них."""
        was = storage.set_copy_stock

        def boom(uid, rows):
            raise RuntimeError("диск")

        storage.set_copy_stock = boom
        try:
            self.assertFalse(PI._remember_stock(7, ["KEY-1111"]))
        finally:
            storage.set_copy_stock = was


class TheHandlerActuallyWiresItUp(unittest.TestCase):
    """Проверка самой проводки: `_remember_stock` можно вызвать правильно
    в тесте и забыть позвать в обработчике — снаружи это «прислал список,
    а он не запомнился»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")
        import features
        self.features = features
        self._shown = features.ad_templates_shown
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def send(self, text="KEY-1111\nKEY-2222\nKEY-3333"):
        class Sent:
            def __init__(s):
                s.texts: list = []

            async def edit_text(s, t, reply_markup=None, **kw):
                s.texts.append(t)
                return s

        class Msg:
            def __init__(s):
                s.text = text
                s.from_user = type("U", (), {"id": 7})()
                s.sent = Sent()

            async def answer(s, t, reply_markup=None, **kw):
                s.sent.texts.append(t)
                return s.sent

        class FSM:
            def __init__(s):
                s.data = {"item_id": "250730"}

            async def get_data(s):
                return dict(s.data)

            async def clear(s):
                s.data = {}

        class Api:
            added: list = []

            async def get_ad(s, ad_id):
                return {"data": {"type": "auto-delivery"}}

            async def add_ad_items(s, ad_id, items):
                Api.added.append(list(items))

        Api.added = []
        m = Msg()
        run(PI.item_stock_save(m, FSM(), Api()))
        return m.sent.texts[-1], Api.added

    def test_the_list_reaches_the_marketplace_and_the_settings(self):
        said, added = self.send()
        self.assertEqual(added, [["KEY-1111", "KEY-2222", "KEY-3333"]])
        self.assertEqual(storage.get_copy_stock(7),
                         ["KEY-1111", "KEY-2222", "KEY-3333"])

    def test_and_the_seller_is_told_it_was_remembered(self):
        """Эти строки уходят живым покупателям. Молчаливая заготовка
        однажды уедет вместо товара."""
        said, _added = self.send()
        self.assertIn("Запомнил этот список", said)
        self.assertIn("получит именно эти строки", said)

    def test_a_second_list_is_not_announced_as_remembered(self):
        """Заготовка уже есть, и её не подменяли — обещать обратное значит
        соврать на ровном месте."""
        storage.set_copy_stock(7, ["СТАРОЕ"])
        said, _added = self.send()
        self.assertNotIn("Запомнил этот список", said)
        self.assertEqual(storage.get_copy_stock(7), ["СТАРОЕ"])


if __name__ == "__main__":
    unittest.main()
