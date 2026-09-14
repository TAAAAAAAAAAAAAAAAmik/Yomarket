"""Тесты движка выдачи. Гоняются на поддельных площадке и поставщике.

Зачем они здесь: пока пишется адаптер новой площадки, эти тесты отвечают на
вопрос «движок цел?». Если они зелёные, а выдача не работает — дело в
адаптере, и искать надо там.

Имя каждого теста — предложение о поведении, а не о функции. Проверяется
СЛЕДСТВИЕ: сколько раз списаны деньги и что ушло покупателю, а не как
выглядит текст.

    python3 -m pytest tests/ -q          # либо
    python3 -m unittest discover tests -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "code"))

import delivery                                              # noqa: E402
from catalog import Card, Denomination                       # noqa: E402
from delivery import DeliveryEngine                          # noqa: E402
from marketplace import Order                                # noqa: E402
from store import (JsonStore, STATE_DONE, STATE_SEND_FAILED,  # noqa: E402
                   STATE_WAIT_CODE)

CARD = Card(slug="robux", title="Roblox", emoji="🎮",
            keywords=("robux", "роблокс"), measure="робуксов",
            activation="Активируйте код на roblox.com/redeem.")


class FakeMarket:
    """Площадка-пустышка. Ровно пять методов протокола."""
    name = "fake"

    def __init__(self, orders=None, send_ok=True, send_why=""):
        self.orders = {o.id: o for o in (orders or [])}
        self.sent: list[tuple[str, str]] = []
        self.send_ok, self.send_why = send_ok, send_why

    async def paid_orders(self):
        return [o for o in self.orders.values() if self.is_paid(o.status)]

    async def get_order(self, order_id):
        return self.orders.get(str(order_id))

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self.send_ok, self.send_why

    def is_paid(self, status):
        return str(status).lower() in ("paid", "оплачен")

    def order_url(self, order_id):
        return f"https://example.test/{order_id}"


class FakeSupplier:
    """Поставщик-пустышка. Считает, сколько раз у него покупали."""

    def __init__(self, *, in_stock=5, status="SUCCESS", codes=("CODE-1",),
                 place_fails=False, by_ref_status="SUCCESS",
                 by_ref_codes=None):
        self.purchases: list[str] = []       # ссылки, по которым покупали
        self.in_stock = in_stock
        self.status, self.codes = status, list(codes)
        self.place_fails = place_fails
        self.by_ref_status = by_ref_status
        self.by_ref_codes = list(by_ref_codes or [])
        self.by_ref_calls = 0

    def item(self, service_id, item_id):
        return {"inStock": self.in_stock, "price": 1.23}

    def place(self, denomination_id, reference, quantity=1):
        if self.place_fails:
            return {"ok": False, "why": "не хватает денег на счёте",
                    "status": "", "codes": []}
        # Идемпотентность поставщика: та же ссылка — тот же результат,
        # второго списания нет.
        if reference in self.purchases:
            return {"ok": True, "why": "", "status": "SUCCESS",
                    "codes": list(self.codes)}
        self.purchases.append(reference)
        return {"ok": True, "why": "", "status": self.status,
                "codes": list(self.codes)}

    def by_reference(self, reference):
        self.by_ref_calls += 1
        return {"ok": True, "why": "", "found": True,
                "status": self.by_ref_status,
                "codes": list(self.by_ref_codes)}


def catalog_of(card, region):
    return [Denomination(service_id="svc", item_id="den-1000", value=1000,
                         title="1000 Robux", price=1.2, in_stock=5,
                         region=region or "GL")]


class Base(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = JsonStore(os.path.join(self.tmp, "state.json"))
        self.store.conf("robux")["enabled"] = True
        self.notes: list[str] = []
        # Ожидание кода в тестах не должно занимать две минуты.
        self._steps = delivery.POLL_STEPS
        delivery.POLL_STEPS = (0, 0, 0)

    def tearDown(self):
        delivery.POLL_STEPS = self._steps

    async def notify(self, text):
        self.notes.append(text)

    def engine(self, market, supplier):
        return DeliveryEngine(market, supplier, self.store, [CARD],
                              self.notify, catalog_of, reference_prefix="pk")

    @staticmethod
    def order(oid="777", status="paid", title="Roblox 1000 Robux",
              desc="Регион кода: GL", chat="chat-1"):
        return Order(id=oid, title=title, status=status, chat_id=chat,
                     description=desc)


class HappyPath(Base):
    """Обычная выдача: заказ оплачен, код куплен и отправлен."""

    async def test_a_paid_order_gets_a_code_in_the_buyers_chat(self):
        market, sup = FakeMarket(), FakeSupplier()
        res = await self.engine(market, sup).on_paid_order(self.order())
        self.assertTrue(res.ok)
        self.assertEqual(res.state, STATE_DONE)
        self.assertEqual(len(sup.purchases), 1)
        self.assertEqual(len(market.sent), 1)
        self.assertIn("CODE-1", market.sent[0][1])
        self.assertIn("chat-1", market.sent[0][0])

    async def test_the_order_is_marked_delivered_only_after_the_code_is_sent(self):
        market, sup = FakeMarket(), FakeSupplier()
        await self.engine(market, sup).on_paid_order(self.order())
        self.assertIn("777", self.store.conf("robux")["delivered"])

    async def test_a_greeting_is_sent_before_the_code_and_only_once(self):
        self.store.conf("robux")["greeting"] = "Здравствуйте! Готовлю код."
        market, sup = FakeMarket(), FakeSupplier()
        await self.engine(market, sup).on_paid_order(self.order())
        self.assertEqual(len(market.sent), 2)
        self.assertIn("Готовлю код", market.sent[0][1])
        self.assertIn("CODE-1", market.sent[1][1])


class NoMoneySpentTwice(Base):
    """Самое дорогое: второе списание за один заказ."""

    async def test_an_order_already_delivered_is_not_bought_again(self):
        market, sup = FakeMarket(), FakeSupplier()
        eng = self.engine(market, sup)
        await eng.on_paid_order(self.order())
        await eng.on_paid_order(self.order())
        self.assertEqual(len(sup.purchases), 1)
        self.assertEqual(len(market.sent), 1)

    async def test_an_unpaid_order_is_never_bought(self):
        market, sup = FakeMarket(), FakeSupplier()
        res = await self.engine(market, sup).on_paid_order(
            self.order(status="created"))
        self.assertFalse(res.ok)
        self.assertEqual(sup.purchases, [])
        self.assertTrue(any("не оплачен" in n for n in self.notes))

    async def test_the_reference_stays_the_same_when_delivery_is_retried(self):
        """Ссылка — вся защита от двойного списания. Пересчёт = вторая покупка."""
        market = FakeMarket([self.order()])
        sup = FakeSupplier(status="IN_PROGRESS", codes=())
        eng = self.engine(market, sup)
        await eng.on_paid_order(self.order())          # купили, кода нет
        first = list(sup.purchases)
        sup.by_ref_codes = ["CODE-LATE"]
        await eng.resume_unfinished()                  # довели
        self.assertEqual(sup.purchases, first,
                         "при возобновлении ссылка изменилась — это второе "
                         "списание")

    async def test_a_lost_journal_entry_still_produces_the_same_reference(self):
        """Последний рубеж: запись журнала вытеснена, а покупка уже была.

        Журнал обрезается по длине. Если запись незаконченной выдачи ушла
        из него, повтор заведёт НОВУЮ — и ссылку посчитает заново. Считалась
        бы она от времени или от случайного числа, поставщик не узнал бы
        заказ и списал бы деньги второй раз. Поэтому ссылка считается
        только из номера заказа и вида товара.
        """
        market = FakeMarket([self.order()])
        sup = FakeSupplier(status="IN_PROGRESS", codes=())
        eng = self.engine(market, sup)
        await eng.on_paid_order(self.order())
        self.assertEqual(len(sup.purchases), 1)

        # Журнал вытеснен: следа покупки у нас не осталось.
        self.store.conf("robux")["log"] = []
        self.store.save()

        sup.status, sup.codes = "SUCCESS", ["CODE-1"]
        res = await eng.on_paid_order(self.order())
        self.assertTrue(res.ok)
        self.assertEqual(len(sup.purchases), 1,
                         "ссылка посчиталась заново — это второе списание")

    async def test_only_the_first_matching_card_takes_the_order(self):
        """«Apple или Xbox» признали бы обе — и купили бы два кода."""
        other = Card(slug="steam", title="Steam", keywords=("robux",))
        self.store.conf("steam")["enabled"] = True
        market, sup = FakeMarket(), FakeSupplier()
        eng = DeliveryEngine(market, sup, self.store, [CARD, other],
                             self.notify, catalog_of)
        await eng.on_paid_order(self.order())
        self.assertEqual(len(sup.purchases), 1)


class SurvivesRestart(Base):
    """Обрыв между покупкой и отправкой — обычное дело при выкате."""

    async def test_a_bought_code_that_was_not_sent_is_resent_not_rebought(self):
        market = FakeMarket([self.order()], send_ok=False,
                            send_why="чат закрыт маркетплейсом")
        sup = FakeSupplier()
        eng = self.engine(market, sup)
        res = await eng.on_paid_order(self.order())
        self.assertFalse(res.ok)
        self.assertEqual(res.state, STATE_SEND_FAILED)
        self.assertEqual(res.codes, ("CODE-1",))

        # Чат починился — доводим.
        market.send_ok, market.send_why = True, ""
        out = await eng.resume_unfinished()
        self.assertEqual(len(sup.purchases), 1, "купили второй раз")
        self.assertTrue(out and out[-1].ok)
        self.assertIn("777", self.store.conf("robux")["delivered"])

    async def test_the_bought_code_is_written_down_before_it_is_sent(self):
        """Чат может быть закрыт — купленный код не должен остаться никому."""
        market = FakeMarket([self.order()], send_ok=False, send_why="закрыт")
        await self.engine(market, FakeSupplier()).on_paid_order(self.order())
        entry = self.store.conf("robux")["log"][0]
        self.assertEqual(entry["codes"], ["CODE-1"])
        self.assertEqual(entry["state"], STATE_SEND_FAILED)

    async def test_a_fresh_store_reads_back_what_was_written(self):
        market, sup = FakeMarket(), FakeSupplier()
        await self.engine(market, sup).on_paid_order(self.order())
        again = JsonStore(self.store.path)
        self.assertIn("777", again.conf("robux")["delivered"])


class SupplierAnswers(Base):
    """Ответы поставщика, каждый из которых уже стоил денег."""

    async def test_in_progress_is_waited_out_not_treated_as_failure(self):
        market = FakeMarket([self.order()])
        sup = FakeSupplier(status="IN_PROGRESS", codes=(),
                           by_ref_codes=["CODE-LATE"])
        res = await self.engine(market, sup).on_paid_order(self.order())
        self.assertTrue(res.ok)
        self.assertIn("CODE-LATE", market.sent[0][1])
        self.assertEqual(len(sup.purchases), 1)

    async def test_a_terminal_status_without_codes_does_not_buy_again(self):
        market = FakeMarket([self.order()])
        sup = FakeSupplier(status="IN_PROGRESS", codes=(),
                           by_ref_status="CANCELLED", by_ref_codes=[])
        eng = self.engine(market, sup)
        await eng.on_paid_order(self.order())
        await eng.resume_unfinished()
        self.assertEqual(len(sup.purchases), 1)

    async def test_a_refusal_is_reported_with_its_reason(self):
        market, sup = FakeMarket(), FakeSupplier(place_fails=True)
        res = await self.engine(market, sup).on_paid_order(self.order())
        self.assertFalse(res.ok)
        self.assertTrue(any("не хватает денег" in n for n in self.notes))
        self.assertEqual(market.sent, [])

    async def test_a_nominal_that_ran_out_before_the_purchase_is_not_bought(self):
        market, sup = FakeMarket(), FakeSupplier(in_stock=0)
        res = await self.engine(market, sup).on_paid_order(self.order())
        self.assertFalse(res.ok)
        self.assertEqual(sup.purchases, [])
        self.assertTrue(any("кончился у поставщика" in n for n in self.notes))


class ReadsTheOrderRight(Base):
    """Номинал и регион: ошибка здесь — купленный не тот товар."""

    async def test_an_unknown_nominal_is_refused_not_guessed(self):
        market, sup = FakeMarket(), FakeSupplier()
        res = await self.engine(market, sup).on_paid_order(
            self.order(title="Roblox 777 Robux"))
        self.assertFalse(res.ok)
        self.assertEqual(sup.purchases, [])
        self.assertTrue(any("Подбирать похожий бот не станет" in n
                            for n in self.notes))

    async def test_the_description_beats_the_plugin_setting_for_region(self):
        self.store.conf("robux")["region"] = "RU"
        market, sup = FakeMarket(), FakeSupplier()
        await self.engine(market, sup).on_paid_order(
            self.order(desc="Регион кода: US"))
        self.assertEqual(self.store.conf("robux")["log"][0]["region"], "US")

    async def test_an_order_without_a_region_anywhere_is_refused(self):
        market, sup = FakeMarket(), FakeSupplier()
        res = await self.engine(market, sup).on_paid_order(self.order(desc=""))
        self.assertFalse(res.ok)
        self.assertEqual(sup.purchases, [])
        self.assertTrue(any("Регион кода" in n for n in self.notes))


class ByHand(Base):
    """Ручная выдача идёт тем же путём, а не своим."""

    async def test_manual_delivery_uses_the_same_path_and_marks_delivered(self):
        market = FakeMarket([self.order()])
        sup = FakeSupplier()
        res = await self.engine(market, sup).deliver_by_hand("robux", "777")
        self.assertTrue(res.ok)
        self.assertIn("777", self.store.conf("robux")["delivered"])

    async def test_manual_delivery_of_an_unknown_order_says_so(self):
        res = await self.engine(FakeMarket(), FakeSupplier()).deliver_by_hand(
            "robux", "нет-такого")
        self.assertFalse(res.ok)
        self.assertIn("не найден", res.why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
