"""Прайс-лист: цены всех товаров на одном экране, только просмотр.

Менять цену бот не умеет намеренно. Через панель это невозможно — у позиции
нет такого поля, — а через API не проверено ни разу, и правка, которая может
не примениться, хуже её отсутствия: продавец считает, что цена новая, а
торгует по старой. Здесь же проверяется, что путей изменения не осталось:
кнопка, ведущая в тупик, — та же ложь, только молча.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import prices as P          # noqa: E402

BOT = pathlib.Path(__file__).resolve().parent.parent


class FakeAPI:
    def __init__(self, ads=None):
        self.ads = ads if ads is not None else [
            {"id": "1", "title": "3.000.000 виртов",
             "price": {"amount": 450, "currency": "RUB"}},
            {"id": "2", "title": "100 звёзд Telegram", "price": 190},
            {"id": "3", "title": "Ключ Windows", "price": 90},
        ]

    async def get_ads(self, cursor=None):
        return {"data": list(self.ads), "meta": {}}


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


class CB:
    def __init__(self, data, uid=1):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": uid})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    async def clear(self):
        pass


def buttons(kb) -> list[str]:
    return [] if kb is None else [b.text for row in kb.inline_keyboard
                                  for b in row]


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        P._cache.clear()
        P._page_of.clear()

    def open(self, uid=1):
        cb = CB("prices:menu", uid)
        asyncio.run(P.open_prices(cb, self.api, FSM()))
        return cb


class TheList(Base):
    def test_it_shows_every_price(self):
        text = self.open().message.last
        for piece in ("450 ₽", "190 ₽", "90 ₽", "3.000.000 виртов"):
            self.assertIn(piece, text, text)

    def test_the_marketplaces_object_shape_is_understood(self):
        """Цена приходит как {"amount": 450, …}, а не числом."""
        self.assertIn("450 ₽", self.open().message.last)

    def test_the_range_is_summed_up_at_the_top(self):
        text = self.open().message.last
        self.assertIn("от 90 до 450 ₽", text, text)

    def test_a_long_shop_is_paged(self):
        self.api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                            for i in range(1, 26)])
        cb = self.open()
        self.assertIn("товаров: 25", cb.message.last)
        self.assertIn("Ещё ▶️", buttons(cb.message.kbs[-1]))

    def test_the_second_page_shows_the_rest(self):
        self.api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                            for i in range(1, 26)])
        cb = CB("prices:l:2")
        asyncio.run(P.list_page(cb, self.api))
        self.assertIn("Товар 21", cb.message.last, cb.message.last)

    def test_an_empty_shop_says_so(self):
        self.api = FakeAPI([])
        self.assertIn("Товаров нет", self.open().message.last)

    def test_a_missing_price_is_named_not_shown_as_zero(self):
        self.api = FakeAPI([{"id": "9", "title": "Без цены"}])
        self.assertIn("цена не указана", self.open().message.last)

    def test_a_marketplace_failure_is_explained(self):
        class Dead(FakeAPI):
            async def get_ads(self, cursor=None):
                raise RuntimeError("502 Bad Gateway")

        self.api = Dead()
        cb = self.open()
        self.assertIn("502", cb.message.last)
        self.assertIn("prices:reload", callbacks(cb.message.kbs[-1]))

    def test_reload_keeps_the_page_you_are_on(self):
        self.api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                            for i in range(1, 26)])
        asyncio.run(P.list_page(CB("prices:l:2"), self.api))
        cb = CB("prices:reload")
        asyncio.run(P.reload_prices(cb, self.api))
        self.assertIn("Товар 21", cb.message.last, cb.message.last)

    def test_titles_cannot_break_the_message(self):
        """'<' в названии — и Telegram отклонит весь экран."""
        self.api = FakeAPI([{"id": "1", "title": "<b>хитрый</b>", "price": 10}])
        self.assertIn("&lt;b&gt;", self.open().message.last)


class ItIsReadOnly(Base):
    def test_the_screen_offers_no_way_to_change_a_price(self):
        for label in buttons(self.open().message.kbs[-1]):
            for word in ("%", "Изменить", "Цена", "Вернуть"):
                self.assertNotIn(word, label, label)

    def test_and_says_where_prices_are_changed(self):
        self.assertIn("на сайте", self.open().message.last)


class NothingIsLeftBehind(unittest.TestCase):
    """Убрать функцию — значит убрать и все ходы к ней."""

    def _sources(self):
        for path in (BOT / "handlers").glob("*.py"):
            yield path, path.read_text()
        for name in ("api/yoomarket.py", "tasks/manager.py", "storage.py",
                     "keyboards/main.py", "main.py"):
            path = BOT / name
            yield path, path.read_text()

    def test_no_handler_writes_a_price(self):
        for path, text in self._sources():
            self.assertNotIn("update_price", text, path.name)

    def test_no_button_promises_a_price_change(self):
        for path, text in self._sources():
            for dead in ("ad_price:", "pitem_price:", "prices:p:",
                         "prices:set:", "prices:undo:"):
                self.assertNotIn(dead, text, f"{path.name}: {dead}")

    def test_the_night_discount_schedule_is_gone(self):
        for path, text in self._sources():
            for dead in ("price_schedule", "pricesched", "price_sched"):
                self.assertNotIn(dead, text, f"{path.name}: {dead}")

    def test_the_price_is_still_shown(self):
        """Убрано изменение, а не сама цена."""
        from handlers import ads as A
        self.assertEqual(A._price_text({"price": {"amount": 129}}), "129")


if __name__ == "__main__":
    unittest.main()
