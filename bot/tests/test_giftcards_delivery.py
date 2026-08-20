"""Выдача гифт-карт: один путь к деньгам на все карты реестра.

Проверяется не текст сообщений, а следствия: сколько раз позвали
поставщика, что именно купили, в каком порядке легли записи и когда заказ
считается выданным.

Порядок здесь и есть предмет проверки, и он тот же, что у Robux:

* намерение пишется **до** вызова поставщика — иначе обрыв связи оставляет
  покупку, о которой никто не знает;
* код пишется **до** отправки покупателю — иначе закрытый чат оставляет
  купленный код никому;
* «выдано» ставится **после** подтверждённой отправки — иначе бот однажды
  доложит о выдаче, которой покупатель не видел.

Отдельно проверяется то, чего у Robux не было: **карт много**, и они не
должны путать друг другу журналы, заказы и тексты активации.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                              # noqa: E402
from automation import giftcards as gc                      # noqa: E402

logging.getLogger("tasks.manager").setLevel(logging.CRITICAL)

# Каталог настоящий: строки списаны с живого ответа `GET /services` 20.08.
CATALOG = {"items": [
    {"id": "svc-apple-tr", "name": "Apple Gift Card | TR", "type": "voucher",
     "countryCode": "TR", "subcategoryName": "Apple Gift Cards", "fields": None,
     "items": [
         {"id": "tr-10", "name": "TRY 10 App Store & iTunes code",
          "price": 0.2301, "currency": "USD", "inStock": 130},
         {"id": "tr-25", "name": "TRY 25 App Store & iTunes code",
          "price": 0.5455, "currency": "USD", "inStock": 220},
     ]},
    # Apple и PlayStation в США пересекаются по номиналам ($2, $3, $4) —
    # это настоящее пересечение из живого каталога, и на нём проверяется,
    # что один заказ не купит два кода.
    {"id": "svc-apple-us", "name": "Apple Gift Card | US", "type": "voucher",
     "countryCode": "US", "subcategoryName": "Apple Gift Cards", "fields": None,
     "items": [
         {"id": "us-a2", "name": "$2 Apple Gift Card",
          "price": 1.9094, "currency": "USD", "inStock": 11334},
         {"id": "us-a3", "name": "$3 Apple Gift Card",
          "price": 2.8642, "currency": "USD", "inStock": 5421},
     ]},
    {"id": "svc-psn-us", "name": "PlayStation®Store Wallet | US",
     "type": "voucher", "countryCode": "US",
     "subcategoryName": "PlayStation Gift Cards", "fields": None,
     "items": [
         {"id": "us-p2", "name": "$2 PlayStation®Store Wallet gift card",
          "price": 1.818, "currency": "USD", "inStock": 143},
         {"id": "us-p10", "name": "$10 PlayStation®Store Wallet gift card",
          "price": 9.37, "currency": "USD", "inStock": 40},
     ]},
]}


class Harness:
    """Поставщик, чат, маркетплейс и хранилище — на подставках."""

    def __init__(self, test, *, cards=("apple",), region="TR",
                 catalog=CATALOG, item=None, buy=None, send=(True, ""),
                 description="Регион кода: TR"):
        self.test = test
        self.uid = 4242
        self.settings: dict = {}
        for slug in cards:
            conf = gc.card_conf(self.settings, slug)
            conf["enabled"] = True
            conf["region"] = region
        self.catalog = catalog
        self.description = description
        self.item_result = item if item is not None else (
            True, {"id": "tr-10", "price": 0.2301, "currency": "USD",
                   "inStock": 130})
        self.buy_result = buy if buy is not None else {
            "ok": True, "why": "", "status": "SUCCESS", "order_id": "o1",
            "codes": ["AAAA-1111"]}
        self.lookup_results: list = []
        self.send_result = send
        self.orders: list = []
        self.items: list = []
        self.lookups: list = []
        self.chat_sent: list = []
        self.notified: list = []
        self.saves: list = []
        self._undo: list = []

    def _patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def __enter__(self):
        from automation import approute
        from tasks import manager as M

        def services_sync(creds):
            return ((True, self.catalog) if self.catalog is not None
                    else (False, "каталог не прочитан"))

        def order_place_sync(creds, denomination_id, quantity=1, reference=""):
            self.orders.append((denomination_id, quantity, reference))
            return dict(self.buy_result)

        def item_sync(creds, service_id, item_id):
            self.items.append((service_id, item_id))
            return self.item_result

        def order_codes_sync(creds, reference):
            self.lookups.append(reference)
            if self.lookup_results:
                return dict(self.lookup_results.pop(0))
            return {"ok": True, "why": "", "found": False, "status": "",
                    "codes": []}

        self._patch(approute, "services_sync", services_sync)
        self._patch(approute, "order_place_sync", order_place_sync)
        self._patch(approute, "item_sync", item_sync)
        self._patch(approute, "order_codes_sync", order_codes_sync)
        # Опрос «принятого» заказа ждёт десятками секунд — в тестах важен
        # порядок вызовов, а не их темп.
        self._patch(M.TaskManager, "_GIFT_POLL_STEPS", (0, 0, 0))
        self._patch(storage, "get_ar_creds", lambda uid: {"api_key": "k"})
        self._patch(M, "get_ar_creds", lambda uid: {"api_key": "k"})

        def save_settings(uid, s):
            snap = {}
            for slug, conf in (s.get("plugins", {})
                               .get("gift_cards", {}) or {}).items():
                snap[slug] = {
                    "delivered": list(conf.get("delivered") or []),
                    "force": list(conf.get("force") or []),
                    "log": [dict(e) for e in (conf.get("log") or [])],
                }
            self.saves.append(snap)

        self._patch(M, "save_settings", save_settings)
        self._patch(storage, "save_settings", save_settings)
        return self

    def __exit__(self, *exc):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def _manager(self):
        from tasks.manager import TaskManager
        mgr = TaskManager.__new__(TaskManager)

        async def send_chat(api, chat_id, text, settings=None):
            self.chat_sent.append((chat_id, text))
            return self.send_result

        async def notify(uid, text):
            self.notified.append(text)

        mgr._send_chat = send_chat
        mgr._notify = notify
        return mgr

    def _api(self):
        description = self.description

        class Api:
            async def get_order(_self, oid):
                return {"data": {"id": oid, "ad_id": "ad-1"}}

            async def get_ad(_self, ad_id):
                return {"data": {"content": description}}

        return Api()

    def run(self, title="Apple Gift Card TRY 10", status="work",
            order_id="777"):
        asyncio.run(self._manager()._maybe_deliver_gifts(
            self.uid, self._api(), self.settings, order_id, title,
            "chat-1", status))
        return self

    def resume(self):
        asyncio.run(self._manager()._gift_resume(
            self.uid, self._api(), self.settings, set()))
        return self

    def conf(self, slug="apple"):
        return gc.card_conf(self.settings, slug)

    def log(self, slug="apple"):
        return self.conf(slug)["log"]

    def delivered(self, slug="apple"):
        return self.conf(slug)["delivered"]


class APaidOrderBuysExactlyOneCode(unittest.TestCase):
    """Выдача идёт сразу по факту оплаты: спрашивать покупателя нечего,
    выдаётся код, ник не нужен."""

    def test_the_right_denomination_is_bought_once(self):
        with Harness(self) as h:
            h.run(title="Apple Gift Card TRY 10")
            self.assertEqual(len(h.orders), 1, "покупок должно быть ровно одна")
            self.assertEqual(h.orders[0][0], "tr-10")

    def test_the_nominal_is_reread_before_the_purchase(self):
        """Сухого прогона для магазина не существует: `checkOnly` разрешён
        только при `ordersType=dtu`. Его место занимает чтение одного
        номинала — оно же ловит кончившийся остаток до траты денег."""
        with Harness(self) as h:
            h.run()
            self.assertEqual(h.items, [("svc-apple-tr", "tr-10")])

    def test_a_zero_stock_nominal_is_not_bought(self):
        with Harness(self, item=(True, {"id": "tr-10", "inStock": 0})) as h:
            h.run()
            self.assertEqual(h.orders, [], "покупки быть не должно")
            self.assertTrue(any("кончил" in t for t in h.notified))

    def test_an_unpaid_order_is_not_delivered(self):
        with Harness(self) as h:
            h.run(status="new")
            self.assertEqual(h.orders, [])

    def test_an_already_delivered_order_is_not_bought_again(self):
        with Harness(self) as h:
            h.conf()["delivered"].append("777")
            h.run(order_id="777")
            self.assertEqual(h.orders, [])


class TheOrderOfRecords(unittest.TestCase):
    """Три записи, и каждая закрывает свой способ потерять деньги."""

    def test_the_intent_is_written_before_the_supplier_is_called(self):
        with Harness(self) as h:
            h.run()
            first = h.saves[0].get("apple", {})
            self.assertTrue(first["log"], "намерение должно лечь до покупки")
            self.assertEqual(first["log"][0]["state"], "собираемся покупать")

    def test_the_code_is_stored_before_it_is_sent(self):
        """Чат может быть закрыт. Купленный код не должен остаться никому."""
        with Harness(self, send=(False, "чат закрыт")) as h:
            h.run()
            self.assertEqual(h.log()[0]["codes"], ["AAAA-1111"])
            self.assertNotIn("777", h.delivered())
            self.assertTrue(any("НЕ ОТПРАВЛЕН" in t for t in h.notified))

    def test_delivered_is_marked_only_after_the_send_succeeded(self):
        with Harness(self) as h:
            h.run()
            self.assertIn("777", h.delivered())
            self.assertEqual(h.log()[0]["state"], "выдан")


class WhenTheSupplierTakesItsTime(unittest.TestCase):
    """`IN_PROGRESS` — законный ответ: заказ принят, код будет позже.

    У гифт-карт «долгих» номиналов заметно больше, чем у Robux, поэтому
    объявить такой ответ выдачей без кода значит регулярно терять
    оплаченные заказы, которые на самом деле уже куплены.
    """

    def test_in_progress_is_polled_until_the_code_arrives(self):
        with Harness(self, buy={"ok": True, "why": "", "status": "IN_PROGRESS",
                                "order_id": "o1", "codes": []}) as h:
            h.lookup_results = [
                {"ok": True, "found": True, "status": "IN_PROGRESS", "codes": []},
                {"ok": True, "found": True, "status": "SUCCESS",
                 "codes": ["BBBB-2222"]},
            ]
            h.run()
            self.assertEqual(len(h.orders), 1, "покупка одна, дальше опрос")
            self.assertIn("BBBB-2222", h.chat_sent[0][1])
            self.assertIn("777", h.delivered())

    def test_a_terminal_status_without_codes_is_a_refusal_not_a_retry(self):
        """`PARTIALLY_COMPLETED` — конец, а не «ещё подождём». Провалиться
        отсюда в покупку значит купить ещё раз то, что уже закончено."""
        with Harness(self, buy={"ok": True, "why": "", "status": "IN_PROGRESS",
                                "order_id": "o1", "codes": []}) as h:
            h.lookup_results = [
                {"ok": True, "found": True, "status": "PARTIALLY_COMPLETED",
                 "codes": []},
            ]
            h.run()
            self.assertEqual(len(h.orders), 1, "второй покупки быть не должно")
            self.assertEqual(h.chat_sent, [])
            # Продавцу назван статус, которым поставщик закончил заказ:
            # «кода нет» без «почему» — это отписка.
            self.assertTrue(any("PARTIALLY_COMPLETED" in t for t in h.notified),
                            h.notified)


class WhenDeliveryWasInterrupted(unittest.TestCase):
    """Обрыв случается чаще всего не от сбоя, а от обычного выката."""

    def test_a_bought_code_is_only_resent_never_rebought(self):
        with Harness(self) as h:
            h.conf()["log"].insert(0, {
                "order": "555", "card": "apple", "nominal": "TRY 10",
                "denomination": "tr-10", "service_id": "svc-apple-tr",
                "reference": "gc-apple-555", "region": "TR",
                "chat": "chat-9", "codes": ["OLD-CODE"],
                "state": "куплен, отправляем"})
            h.resume()
            self.assertEqual(h.orders, [], "второй покупки быть не должно")
            self.assertIn("OLD-CODE", h.chat_sent[0][1])
            self.assertIn("555", h.delivered())

    def test_an_interrupted_purchase_repeats_with_the_same_reference(self):
        """Та же ссылка — единственная защита от второго списания: на неё
        поставщик отвечает `IDEMPOTENCY_REPLAY`, а не покупкой."""
        with Harness(self) as h:
            h.conf()["log"].insert(0, {
                "order": "555", "card": "apple", "nominal": "TRY 10",
                "denomination": "tr-10", "service_id": "svc-apple-tr",
                "reference": "gc-apple-555", "region": "TR",
                "chat": "chat-9", "codes": [], "state": "покупаем"})
            h.resume()
            self.assertEqual(len(h.orders), 1)
            self.assertEqual(h.orders[0][2], "gc-apple-555")


class ManyCardsDoNotMixThemselvesUp(unittest.TestCase):
    """Карт много, и это единственное, чего не было у Robux.

    Перепутанный журнал или дважды выданный заказ здесь стоили бы ровно
    столько же, сколько чужой код в чате покупателя.
    """

    def test_only_the_first_card_that_recognises_the_order_takes_it(self):
        """Название вида «Apple или PlayStation» признали бы обе карты, и
        без остановки бот купил бы два кода на один оплаченный заказ.

        Регион США выбран не случайно: там у обеих карт есть номинал `$2`
        (это настоящее пересечение живого каталога). Возьми мы регион, где
        совпадения нет, вторую покупку остановила бы нехватка номинала, а
        не защита — и тест прошёл бы, ничего не проверив.
        """
        with Harness(self, cards=("apple", "psn"), region="US",
                     description="Регион кода: US",
                     item=(True, {"id": "us-a2", "price": 1.91,
                                  "currency": "USD", "inStock": 100})) as h:
            h.run(title="Apple или PlayStation $2")
            self.assertEqual(len(h.orders), 1, "куплен должен быть один код")

    def test_a_card_that_is_off_does_not_deliver(self):
        with Harness(self) as h:
            h.conf()["enabled"] = False
            h.run()
            self.assertEqual(h.orders, [])

    def test_each_card_keeps_its_own_journal(self):
        with Harness(self, cards=("apple", "psn")) as h:
            h.run(title="Apple Gift Card TRY 10")
            self.assertTrue(h.log("apple"))
            self.assertEqual(h.log("psn"), [])

    def test_the_activation_text_of_the_card_is_sent_to_the_buyer(self):
        """Единственное, что не может быть одинаковым: одна инструкция на
        всех означала бы, что бот уверенно отправляет неверный адрес."""
        with Harness(self) as h:
            h.run()
            self.assertIn("appstore.com/redeem", h.chat_sent[0][1])


class WhereTheRegionComesFrom(unittest.TestCase):
    """У Юмаркета поля региона нет: описание товара — единственное место,
    где он переживает создание объявления."""

    def test_the_region_is_read_from_the_ad_description(self):
        with Harness(self, region="", description="Регион кода: TR") as h:
            h.run()
            self.assertEqual(len(h.orders), 1)
            self.assertEqual(h.log()[0]["region"], "TR")

    def test_without_a_region_nothing_is_bought(self):
        """Купить код не того региона значит выдать негодный: покупатель
        его не активирует вовсе."""
        with Harness(self, region="", description="Просто описание") as h:
            h.run()
            self.assertEqual(h.orders, [])
            self.assertTrue(any("регион" in t.lower() for t in h.notified))


class WhatNeverReachesTheBuyer(unittest.TestCase):
    """Замазанный код приходит успешным ответом, поле на месте, и заметить
    подмену со стороны нечем."""

    def test_a_masked_code_never_reaches_the_buyer(self):
        with Harness(self, buy={"ok": True, "why": "", "status": "SUCCESS",
                                "order_id": "o1", "codes": []}) as h:
            h.run()
            self.assertEqual(h.chat_sent, [])
            self.assertTrue(any("БЕЗ КОДА" in t for t in h.notified))


if __name__ == "__main__":
    unittest.main()
