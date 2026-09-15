"""Заказ приходит без цены вообще — берём её из объявления и говорим откуда.

`/order_debug 1218314` напечатал ответ маркетплейса целиком:

    id = 1218314
    ad_id = 234852
    chat_id = 1158764
    customer: id, name
    fulfillment: item_id
    status = refunded
    created_at = 2026-08-15T09:15:44.000000Z

Ни в списке заказов, ни в карточке заказа денежных полей нет ни одного.
Не «прочитали не то поле» — их там нет. Отсюда «💰 — ₽» в каждом
уведомлении о покупке и «📊 Сегодня: 6 · 0 ₽» при шести проданных товарах.

Цена лежит в объявлении, номер которого в заказе есть. Дочитывание
объявления уже было — ради названия, — цена теперь берётся тем же запросом.

Но цена объявления и уплаченная сумма — разные вещи: продавец мог поменять
цену после продажи. Поэтому источник запоминается и подписывается на экране
(«по объявлению»), а не выдаётся за сумму заказа. Число без пометки здесь
было бы ровно тем враньём, ради которого написан весь этот проект.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import tasks.manager as M      # noqa: E402

# Ровно то, что маркетплейс прислал по #1218314 — без единого денежного поля.
BARE = {"id": "1218314", "ad_id": "234852", "chat_id": "1158764",
        "customer": {"id": 418361, "name": "Telman Гадыев"},
        "fulfillment": {"item_id": 801240},
        "status": "paid",
        "created_at": "2026-08-15T09:15:44.000000Z"}

AD = {"id": 234852, "title": "50 зч", "status": "publish",
      "price": {"amount": 60, "base_amount": 60, "currency": "RUB"}}


_KEEP = object()


class Api:
    def __init__(self, ad=_KEEP, order=None):
        self.ad = AD if ad is _KEEP else ad
        self.order = BARE if order is None else order
        self.ads_read: list[str] = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def get_orders(self, cursor=None):
        return {"data": [self.order]}

    async def get_order(self, oid):
        return {"data": self.order}

    async def get_ad(self, aid):
        self.ads_read.append(str(aid))
        if self.ad is None:
            raise RuntimeError("объявление удалено")
        return self.ad

    async def work_order(self, oid):
        return {"ok": True}

    async def get_messages(self, cid):
        return {"data": []}

    async def send_message(self, cid, text):
        return {"ok": True}


def run(api=None, *, settings=None, passes=1):
    api = api or Api()
    real_api, real_save = M.YooMarketAPI, M.save_settings
    M.YooMarketAPI = lambda token=None: api
    M.save_settings = lambda uid, s: None
    tm = M.TaskManager.__new__(M.TaskManager)
    tm.notes: list[str] = []

    async def notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = notify
    s = {"known_orders": {}, "known_order_details": {},
         "orders_initialized": True,
         "auto_accept": {"enabled": False},
         "notify_orders": {"enabled": True},
         "auto_reply": {"enabled": False},
         "plugins": {"auto_stars": {"enabled": False}}}
    s.update(settings or {})
    try:
        for _ in range(passes):
            asyncio.run(tm._process_orders(1, "tok", s))
    finally:
        M.YooMarketAPI, M.save_settings = real_api, real_save
    return s, api, tm.notes


class ThePriceComesFromTheListing(unittest.TestCase):
    def test_the_order_gets_a_price_at_all(self):
        s, _api, _n = run()
        self.assertEqual(s["known_order_details"]["1218314"]["price"], 60.0)

    def test_the_notification_stops_showing_a_dash(self):
        _s, _api, notes = run()
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertIn("60 ₽", card)
        self.assertNotIn("— ₽", card)

    def test_the_object_shaped_price_is_read_through(self):
        """{"amount": 60, "currency": "RUB"} — не число, и как скаляр оно
        превращалось в ноль."""
        s, _api, _n = run()
        self.assertNotEqual(s["known_order_details"]["1218314"]["price"], 0)

    def test_the_listing_is_read_once_not_every_pass(self):
        _s, api, _n = run(passes=3)
        self.assertEqual(len(api.ads_read), 1, api.ads_read)

    def test_a_price_in_the_order_itself_wins(self):
        """Дочитывание — про нехватку, а не про недоверие."""
        api = Api(order=dict(BARE, title="50 зч",
                             price={"amount": 129, "currency": "RUB"}))
        s, _api, _n = run(api)
        self.assertEqual(s["known_order_details"]["1218314"]["price"], 129.0)
        self.assertEqual(api.ads_read, [], "объявление читать было незачем")


class TheSourceIsSaidOutLoud(unittest.TestCase):
    """Цена объявления — не уплаченная сумма. Число без пометки утверждало
    бы то, чего маркетплейс не присылал."""

    def test_the_notification_says_where_the_number_is_from(self):
        _s, _api, notes = run()
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertIn("по объявлению", card)

    def test_it_is_remembered_with_the_number(self):
        s, _api, _n = run()
        self.assertEqual(s["known_order_details"]["1218314"]["price_src"], "ad")

    def test_a_price_from_the_order_carries_no_such_note(self):
        api = Api(order=dict(BARE, title="50 зч",
                             price={"amount": 129, "currency": "RUB"}))
        _s, _api2, notes = run(api)
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertNotIn("по объявлению", card)

    def test_the_order_card_says_it_too(self):
        import handlers.orders as O
        d = O._merge(BARE, {"1218314": {"price": 60.0, "price_src": "ad"}})
        self.assertEqual(d["price_src"], "ad")


class WhenTheListingCannotBeRead(unittest.TestCase):
    def test_a_missing_listing_does_not_break_the_notification(self):
        _s, _api, notes = run(Api(ad=None))
        self.assertTrue([n for n in notes if "НОВАЯ ПОКУПКА" in n], notes)

    def test_and_the_price_stays_honestly_unknown(self):
        """Ноль здесь читался бы как бесплатный заказ."""
        s, _api, _n = run(Api(ad=None))
        self.assertEqual(s["known_order_details"]["1218314"]["price"], "—")


class OrdersSeenBeforeTheFixAreNotLeftWithoutAPrice(unittest.TestCase):
    """Заказ, дочитанный до правки, помечен «дочитан» — и остался бы без
    цены навсегда, а вместе с ним и выручка за день. Продавец увидел бы
    «Сегодня: 6 · 0 ₽» ещё сутки после выката и решил, что не починилось."""

    OLD = {"1218314": {"title": "50 зч", "buyer": "Telman Гадыев",
                       "price": "—", "enriched": True,
                       "seen_at": time.time() - 3600, "chat_id": "1158764"}}

    def test_the_price_is_fetched_on_the_next_pass(self):
        s, _api, _n = run(settings={
            "known_orders": {"1218314": "paid"},
            "known_order_details": {k: dict(v) for k, v in self.OLD.items()}})
        self.assertEqual(s["known_order_details"]["1218314"]["price"], 60.0)

    def test_the_listing_is_asked_once_even_if_it_has_no_price(self):
        """Объявление удалено — цены не будет никогда. Спрашивать о ней
        каждую минуту значит долбить витрину впустую."""
        api = Api(ad={"id": 234852, "title": "50 зч"})
        s, api, _n = run(api, settings={
            "known_orders": {"1218314": "paid"},
            "known_order_details": {k: dict(v) for k, v in self.OLD.items()}},
            passes=4)
        self.assertEqual(len(api.ads_read), 1, api.ads_read)
        self.assertTrue(s["known_order_details"]["1218314"]["price_tried"])

    def test_an_order_that_already_has_a_price_is_not_re_read(self):
        api = Api()
        _s, api, _n = run(api, settings={
            "known_orders": {"1218314": "paid"},
            "known_order_details": {"1218314": dict(self.OLD["1218314"],
                                                    price=60.0)}})
        self.assertEqual(api.ads_read, [])


class TheDiagnosticShowsWhatTheBotStored(unittest.TestCase):
    """Отчёт печатал цену из ответа маркетплейса — а её там нет и не будет.
    Восстановленная из объявления лежит в записи бота, и без неё отчёт
    читается как «всё ещё сломано»."""

    def setUp(self):
        import handlers.orders as O
        self.O = O
        self._get = O.get_settings
        self.settings = {"auto_accept": {"enabled": True},
                         "known_order_details": {
                             "1218314": {"price": 60.0, "price_src": "ad",
                                         "seen_at": time.time()}},
                         "plugins": {"auto_stars": {"enabled": False}}}
        O.get_settings = lambda uid: self.settings

    def tearDown(self):
        self.O.get_settings = self._get

    def report(self, api=None):
        api = api or Api()
        said: list[str] = []

        class Status:
            async def edit_text(self, t, **kw):
                said.append(t)

        class Msg:
            from_user = type("U", (), {"id": 1})()
            text = "/order_debug 1218314"

            async def answer(self, t, **kw):
                said.append(t)
                return Status()

        asyncio.run(self.O.order_debug(Msg(), api))
        return "\n\n".join(said)

    def test_the_stored_price_is_in_the_report(self):
        line = next((l for l in self.report().splitlines()
                     if "у бота записано" in l), "")
        self.assertIn("60 ₽", line)

    def test_it_says_the_number_came_from_the_listing(self):
        self.assertIn("по объявлению", self.report())

    def test_the_listing_price_is_shown_too(self):
        """Чтобы было видно: цену взять есть откуда, вопрос только в том,
        дошла ли она до записи."""
        self.assertIn("в объявлении 234852", self.report())

    def test_a_missing_stored_price_says_whether_the_listing_was_asked(self):
        self.settings["known_order_details"]["1218314"] = {"price": "—"}
        got = self.report()
        self.assertIn("ещё не спрашивали", got)

    def test_and_says_when_it_already_was(self):
        self.settings["known_order_details"]["1218314"] = {"price": "—",
                                                           "price_tried": True}
        self.assertIn("уже спрашивали", self.report())


class TheDailyTallyCountsQuantity(unittest.TestCase):
    """Цена в записи — за штуку: карточка так её и показывает, «60 ₽ ×2».
    Выручка без множителя занижала день на каждой покупке больше одной."""

    def tally(self, details):
        return M._today_stats(details, {}, {})

    def test_two_items_count_twice(self):
        cnt, rev = self.tally({"1": {"seen_at": time.time(), "price": 60,
                                     "quantity": 2}})
        self.assertEqual((cnt, rev), (1, 120))

    def test_one_item_counts_once(self):
        _cnt, rev = self.tally({"1": {"seen_at": time.time(), "price": 60}})
        self.assertEqual(rev, 60)

    def test_an_unknown_price_is_not_counted_as_zero_roubles(self):
        cnt, rev = self.tally({"1": {"seen_at": time.time(), "price": "—"}})
        self.assertEqual((cnt, rev), (1, 0))

    def test_a_broken_quantity_does_not_lose_the_order(self):
        _cnt, rev = self.tally({"1": {"seen_at": time.time(), "price": 60,
                                      "quantity": "две"}})
        self.assertEqual(rev, 60)


if __name__ == "__main__":
    unittest.main()
