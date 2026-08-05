"""Экран «Цены»: цена каждого товара правится отдельно.

Расписание цен бьёт по всем товарам сразу, а торгуются по одному. Здесь
проверяется, что правится ровно выбранный товар, что показанная цена —
ответ маркетплейса, а не наша арифметика, и что откат ведёт к цене до
правок, а не к предыдущему шагу.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import prices as P          # noqa: E402


class FakeAPI:
    """Маркетплейс: цену хранит у себя и отдаёт её же в ответ."""

    def __init__(self, ads=None, fail=""):
        self.ads = {str(a["id"]): dict(a) for a in (ads or [
            {"id": "1", "title": "3.000.000 виртов", "price": 450},
            {"id": "2", "title": "100 звёзд Telegram", "price": 190},
            {"id": "3", "title": "Ключ Windows", "price": 90},
        ])}
        self.fail = fail
        self.updates: list[tuple[str, int]] = []

    async def get_ads(self, cursor=None):
        return {"data": list(self.ads.values()), "meta": {}}

    async def get_ad(self, ad_id):
        return dict(self.ads[str(ad_id)])

    async def update_price(self, ad_id, price, discount=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.updates.append((str(ad_id), int(price)))
        self.ads[str(ad_id)]["price"] = int(price)
        return {"ok": True}


class Screen:
    """Сообщение, которое экран правит."""

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
    def __init__(self):
        self.data: dict = {}
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def buttons(kb) -> list[str]:
    if kb is None:
        return []
    return [b.text for row in kb.inline_keyboard for b in row]


def callbacks(kb) -> list[str]:
    if kb is None:
        return []
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        P._cache.clear()
        P._page_of.clear()
        self.store: dict = {"price_undo": {}}
        P.get_settings = lambda uid: self.store
        P.save_settings = lambda uid, s: self.store.update(s)

    def run_(self, coro):
        return asyncio.run(coro)


class TheList(Base):
    def test_it_shows_every_price(self):
        cb = CB("prices:menu")
        self.run_(P.open_prices(cb, self.api, FSM()))
        text = cb.message.last
        for piece in ("450 ₽", "190 ₽", "90 ₽", "3.000.000 виртов"):
            self.assertIn(piece, text, text)

    def test_each_product_is_its_own_button(self):
        cb = CB("prices:menu")
        self.run_(P.open_prices(cb, self.api, FSM()))
        data = callbacks(cb.message.kbs[-1])
        for ad_id in ("1", "2", "3"):
            self.assertIn(f"prices:ad:{ad_id}", data, data)

    def test_a_long_shop_is_paged_not_truncated(self):
        self.api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                            for i in range(1, 21)])
        cb = CB("prices:menu")
        self.run_(P.open_prices(cb, self.api, FSM()))
        self.assertIn("товаров: 20", cb.message.last)
        self.assertIn("Ещё ▶️", buttons(cb.message.kbs[-1]))

    def test_the_second_page_shows_the_rest(self):
        self.api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                            for i in range(1, 21)])
        cb = CB("prices:l:2")
        self.run_(P.list_page(cb, self.api, FSM()))
        self.assertIn("Товар 17", cb.message.last, cb.message.last)

    def test_a_marketplace_failure_is_explained(self):
        class Dead(FakeAPI):
            async def get_ads(self, cursor=None):
                raise RuntimeError("502 Bad Gateway")

        cb = CB("prices:menu")
        self.run_(P.open_prices(cb, Dead(), FSM()))
        self.assertIn("502", cb.message.last)
        self.assertIn("prices:reload", callbacks(cb.message.kbs[-1]))


class OneProductAtATime(Base):
    def _step(self, ad_id, step):
        cb = CB(f"prices:p:{ad_id}:{step}")
        self.run_(P.step_price(cb, self.api))
        return cb

    def test_a_step_touches_only_that_product(self):
        self._step("2", -10)
        self.assertEqual(self.api.updates, [("2", 171)])
        self.assertEqual(self.api.ads["1"]["price"], 450, "сосед не тронут")
        self.assertEqual(self.api.ads["3"]["price"], 90, "сосед не тронут")

    def test_the_step_is_measured_from_the_current_price(self):
        self._step("1", 10)               # 450 → 495
        self._step("1", 10)               # 495 → 545, а не 540
        self.assertEqual([p for _i, p in self.api.updates], [495, 545])

    def test_the_card_shows_what_the_marketplace_answered(self):
        """Не нашу арифметику: округлил бы он — на экране была бы правда."""
        class Rounding(FakeAPI):
            async def update_price(self, ad_id, price, discount=None):
                await FakeAPI.update_price(self, ad_id, price)
                self.ads[str(ad_id)]["price"] = 500      # магазин округлил
                return {"ok": True}

        api = Rounding()
        cb = CB("prices:p:1:10")
        self.run_(P.step_price(cb, api))
        self.assertIn("500 ₽", cb.message.last, cb.message.last)

    def test_a_price_that_did_not_stick_is_not_called_done(self):
        """«✅ обновлено» при старой цене на сайте — худшее, что бот скажет."""
        class Ignoring(FakeAPI):
            async def update_price(self, ad_id, price, discount=None):
                self.updates.append((str(ad_id), int(price)))
                return {"ok": True}          # согласился и ничего не сделал

        api = Ignoring()
        cb = CB("prices:p:1:10")
        self.run_(P.step_price(cb, api))
        self.assertIn("не изменилась", cb.message.last, cb.message.last)
        self.assertNotIn("✅", cb.message.last, cb.message.last)

    def test_a_step_too_small_to_reach_a_rouble_says_so(self):
        api = FakeAPI([{"id": "9", "title": "Мелочь", "price": 3}])
        cb = CB("prices:p:9:5")           # 3 ₽ + 5% = 3 ₽
        self.run_(P.step_price(cb, api))
        self.assertEqual(api.updates, [], "цена не должна была меняться")
        self.assertTrue(any("не изменилась" in a for a in cb.alerts), cb.alerts)

    def test_a_rejected_change_is_reported_and_not_pretended(self):
        api = FakeAPI(fail="нет прав на это объявление")
        cb = CB("prices:p:1:-10")
        self.run_(P.step_price(cb, api))
        self.assertTrue(any("нет прав" in a for a in cb.alerts), cb.alerts)
        self.assertEqual(api.ads["1"]["price"], 450)

    def test_a_product_without_a_price_is_not_multiplied_by_zero(self):
        api = FakeAPI([{"id": "5", "title": "Без цены"}])
        cb = CB("prices:p:5:10")
        self.run_(P.step_price(cb, api))
        self.assertEqual(api.updates, [])
        self.assertTrue(any("не указана" in a for a in cb.alerts), cb.alerts)

    def test_the_price_never_falls_below_a_rouble(self):
        api = FakeAPI([{"id": "7", "title": "Дёшево", "price": 1}])
        cb = CB("prices:p:7:-10")
        self.run_(P.step_price(cb, api))
        for _i, p in api.updates:
            self.assertGreaterEqual(p, 1)


class TypingAnExactPrice(Base):
    def _send(self, text, ad_id="2"):
        st = FSM()
        st.data["ad_id"] = ad_id
        msg = Screen()
        msg.text = text
        msg.from_user = type("U", (), {"id": 1})()
        self.run_(P.exact_save(msg, st, self.api))
        return msg, st

    def test_a_plain_number_becomes_the_price(self):
        self._send("399")
        self.assertEqual(self.api.updates, [("2", 399)])

    def test_roubles_can_be_added_relative_to_the_current_price(self):
        self._send("+50")                 # 190 + 50
        self.assertEqual(self.api.updates, [("2", 240)])

    def test_a_percent_works_too(self):
        self._send("-15%")                # 190 − 15%
        self.assertEqual(self.api.updates, [("2", 162)])

    def test_nonsense_is_refused_without_losing_the_thread(self):
        _msg, st = self._send("дёшево")
        self.assertEqual(self.api.updates, [])
        self.assertIsNotNone(st.data.get("ad_id"),
                             "человек должен просто прислать число ещё раз")

    def test_a_free_product_is_not_accepted_silently(self):
        self._send("0")
        self.assertEqual(self.api.updates, [])

    def test_the_parser_on_its_own(self):
        for raw, current, expect in (("450", 100, 450), ("+50", 100, 150),
                                     ("-30", 100, 70), ("-15%", 200, 170),
                                     ("+10%", 200, 220), ("1 000", 0, 1000),
                                     ("99.6", 0, 100)):
            price, err = P._parse_price(raw, current)
            self.assertEqual((price, err), (expect, ""), raw)
        for raw in ("", "дорого", "0", "-200", "15%"):
            price, err = P._parse_price(raw, 100)
            self.assertTrue(err, f"{raw!r} прошло молча → {price}")


class GoingBack(Base):
    def test_undo_returns_the_price_from_before_the_edits(self):
        """Три шага подряд — и вернуться надо к 450, а не к предпоследнему."""
        for _ in range(3):
            self.run_(P.step_price(CB("prices:p:1:-5"), self.api))
        cb = CB("prices:undo:1")
        self.run_(P.undo_price(cb, self.api))
        self.assertEqual(self.api.ads["1"]["price"], 450)

    def test_the_button_names_the_price_it_returns_to(self):
        self.run_(P.step_price(CB("prices:p:1:-5"), self.api))
        cb = CB("prices:ad:1")
        self.run_(P.open_card(cb, self.api, FSM()))
        self.assertTrue(any("450" in b for b in buttons(cb.message.kbs[-1])),
                        buttons(cb.message.kbs[-1]))

    def test_nothing_to_undo_offers_no_button(self):
        cb = CB("prices:ad:1")
        self.run_(P.open_card(cb, self.api, FSM()))
        self.assertFalse(any("Вернуть" in b for b in buttons(cb.message.kbs[-1])))

    def test_after_undoing_there_is_nothing_left_to_undo(self):
        self.run_(P.step_price(CB("prices:p:1:-5"), self.api))
        self.run_(P.undo_price(CB("prices:undo:1"), self.api))
        cb = CB("prices:ad:1")
        self.run_(P.open_card(cb, self.api, FSM()))
        self.assertFalse(any("Вернуть" in b for b in buttons(cb.message.kbs[-1])))

    def test_undo_is_remembered_per_product(self):
        self.run_(P.step_price(CB("prices:p:1:-5"), self.api))
        self.run_(P.step_price(CB("prices:p:2:-5"), self.api))
        self.assertEqual(P._undo_price(1, "1"), 450)
        self.assertEqual(P._undo_price(1, "2"), 190)

    def test_back_returns_to_the_page_you_came_from(self):
        api = FakeAPI([{"id": str(i), "title": f"Товар {i}", "price": i * 10}
                       for i in range(1, 21)])
        self.run_(P.list_page(CB("prices:l:2"), api, FSM()))
        cb = CB("prices:ad:17")
        self.run_(P.open_card(cb, api, FSM()))
        self.assertIn("prices:l:2", callbacks(cb.message.kbs[-1]))


class TheButtonsTelegramWillAccept(Base):
    def test_every_callback_fits_in_64_bytes(self):
        cb = CB("prices:menu")
        self.run_(P.open_prices(cb, self.api, FSM()))
        card = CB("prices:ad:1")
        self.run_(P.open_card(card, self.api, FSM()))
        for kb in (cb.message.kbs[-1], card.message.kbs[-1]):
            for data in callbacks(kb):
                self.assertLessEqual(len(data.encode()), 64, data)


if __name__ == "__main__":
    unittest.main()
