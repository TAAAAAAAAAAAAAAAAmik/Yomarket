"""Выдача Robux в фоновом цикле, с поставщиком и чатом на подставках.

Проверяется не текст сообщений, а следствия: сколько раз позвали поставщика,
что именно купили, в каком порядке легли записи и когда заказ считается
выданным. Порядок здесь и есть предмет проверки — он из
`docs/robux_delivery.md`, и каждая его ступень закрывает свой способ
потерять деньги:

* намерение пишется **до** вызова поставщика — иначе обрыв связи оставляет
  покупку, о которой никто не знает;
* код пишется **до** отправки покупателю — иначе закрытый чат оставляет
  купленный код никому;
* «выдано» ставится **после** подтверждённой отправки — иначе бот однажды
  доложит о выдаче, которой покупатель не видел.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                   # noqa: E402

logging.getLogger("tasks.manager").setLevel(logging.CRITICAL)

CATALOG = {"items": [{
    "id": "svc-GL", "name": "Roblox Wallet Code | GL", "type": "voucher",
    "fields": None,
    "items": [
        {"id": "gl-1000", "name": "1000 Robux", "price": 11.22,
         "currency": "USD", "inStock": 994},
        {"id": "gl-800", "name": "800 Robux", "price": 9.078,
         "currency": "USD", "inStock": 2329},
    ]}]}


def settings_with(**over):
    plugin = {"enabled": True, "region": "GL", "keyword": "", "note": "",
              "delivered": [], "log": []}
    plugin.update(over)
    return {"plugins": {"auto_roblox": plugin}}


class Harness:
    """Поставщик, чат и хранилище — на подставках; считаем вызовы."""

    def __init__(self, test, *, settings, catalog=CATALOG,
                 dry=(True, {}), buy=None, send=(True, "")):
        self.test = test
        self.uid = 4242
        self.settings = settings
        self.catalog = catalog
        self.dry_result = dry
        self.buy_result = buy if buy is not None else (
            True, {"result": {"vouchers": [{"pin": "AAAA-1111"}]}})
        self.send_result = send
        self.orders: list = []          # (denomination, qty, ref, check_only)
        self.chat_sent: list = []
        self.notified: list = []
        self.saves: list = []           # снимок состояния на каждом save
        self._undo: list = []

    def _patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def __enter__(self):
        from automation import approute
        from tasks import manager as M

        def services_sync(creds):
            return (True, self.catalog) if self.catalog is not None else (
                False, "каталог не прочитан")

        def order_sync(creds, denomination_id, quantity=1, reference="",
                       check_only=False):
            self.orders.append((denomination_id, quantity, reference, check_only))
            return self.dry_result if check_only else self.buy_result

        self._patch(approute, "services_sync", services_sync)
        self._patch(approute, "order_sync", order_sync)
        self._patch(storage, "get_ar_creds", lambda uid: {"api_key": "k"})
        self._patch(M, "get_ar_creds", lambda uid: {"api_key": "k"})

        def save_settings(uid, s):
            p = s["plugins"]["auto_roblox"]
            self.saves.append({
                "delivered": list(p.get("delivered") or []),
                "log": [dict(e) for e in (p.get("log") or [])],
            })

        self._patch(M, "save_settings", save_settings)
        self._patch(storage, "save_settings", save_settings)
        return self

    def __exit__(self, *exc):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def run(self, title="1000 Robux", status="work", order_id="777"):
        from tasks.manager import TaskManager
        mgr = TaskManager.__new__(TaskManager)

        async def send_chat(api, chat_id, text, settings=None):
            self.chat_sent.append((chat_id, text))
            return self.send_result

        async def notify(uid, text):
            self.notified.append(text)

        mgr._send_chat = send_chat
        mgr._notify = notify
        asyncio.run(mgr._maybe_deliver_robux(
            self.uid, object(), self.settings, order_id, title, "chat-1", status))
        return self

    @property
    def log(self):
        return self.settings["plugins"]["auto_roblox"]["log"]

    @property
    def delivered(self):
        return self.settings["plugins"]["auto_roblox"]["delivered"]


class APaidOrderIsDeliveredWithoutAskingTheBuyerAnything(unittest.TestCase):
    """У Robux нет шага «спросить ник»: выдаётся код. Скопированный со
    звёзд вопрос покупателю задерживал бы выдачу без всякой причины."""

    def test_the_right_denomination_is_bought_once(self):
        with Harness(self, settings=settings_with()) as h:
            h.run(title="1000 Robux")
            real = [o for o in h.orders if not o[3]]
            self.assertEqual(len(real), 1, "покупок должно быть ровно одна")
            self.assertEqual(real[0][0], "gl-1000")

    def test_a_dry_run_goes_first_with_the_same_body(self):
        """Форма тела подтверждена ответами сервера, но не живой покупкой.
        Ошибись мы в ней — отказ придёт на сухом прогоне, а не на деньгах."""
        with Harness(self, settings=settings_with()) as h:
            h.run()
            self.assertTrue(h.orders[0][3], "первым должен идти сухой прогон")
            self.assertFalse(h.orders[1][3])
            self.assertEqual(h.orders[0][0], h.orders[1][0])
            self.assertEqual(h.orders[0][2], h.orders[1][2])   # та же ссылка

    def test_the_code_reaches_the_buyer(self):
        with Harness(self, settings=settings_with()) as h:
            h.run()
            self.assertEqual(len(h.chat_sent), 1)
            self.assertIn("AAAA-1111", h.chat_sent[0][1])

    def test_only_then_is_the_order_marked_delivered(self):
        with Harness(self, settings=settings_with()) as h:
            h.run()
            self.assertIn("777", h.delivered)

    def test_a_second_pass_does_not_buy_again(self):
        with Harness(self, settings=settings_with()) as h:
            h.run()
            h.run()
            self.assertEqual(len([o for o in h.orders if not o[3]]), 1)


class NothingHappensQuietly(unittest.TestCase):
    """«Ничего не произошло» без причины — самая частая поломка проекта.
    Здесь она стоила бы покупателю ожидания оплаченного заказа."""

    def test_an_unpaid_order_is_not_delivered(self):
        """«Создан» деньгами не является — панель прямо предупреждает."""
        with Harness(self, settings=settings_with()) as h:
            h.run(status="created")
            self.assertEqual(h.orders, [])

    def test_a_missing_denomination_tells_the_seller_and_buys_nothing(self):
        with Harness(self, settings=settings_with()) as h:
            h.run(title="500 Robux")
            self.assertEqual(h.orders, [])
            self.assertTrue(h.notified)
            self.assertIn("500", h.notified[0])

    def test_a_missing_key_is_named_not_swallowed(self):
        with Harness(self, settings=settings_with()) as h:
            # Выдача берёт ключ из storage — там и подменяем. Правка в
            # модуле менеджера прошла бы мимо: импорт там локальный.
            storage.get_ar_creds = lambda uid: {}
            h.run()
            self.assertEqual(h.orders, [])
            self.assertIn("ключ", h.notified[0].lower())

    def test_a_failed_dry_run_stops_before_the_money(self):
        with Harness(self, settings=settings_with(),
                     dry=(False, "Validation error")) as h:
            h.run()
            self.assertEqual([o for o in h.orders if not o[3]], [])
            self.assertTrue(h.notified)

    def test_a_refused_purchase_is_reported(self):
        with Harness(self, settings=settings_with(),
                     buy=(False, "нет в наличии")) as h:
            h.run()
            self.assertNotIn("777", h.delivered)
            self.assertTrue(h.notified)


class MoneyIsNeverLostSilently(unittest.TestCase):
    """Три места, где купленный код мог бы пропасть незаметно."""

    def test_an_answer_without_a_code_is_not_a_delivery(self):
        """Двухсотый без vouchers — отказ. Деньги при этом могли уйти, и
        продавец обязан узнать об этом, а не увидеть тишину."""
        with Harness(self, settings=settings_with(),
                     buy=(True, {"result": {"vouchers": []}})) as h:
            h.run()
            self.assertNotIn("777", h.delivered)
            self.assertEqual(h.chat_sent, [])
            self.assertTrue(h.notified)
            self.assertIn("без кода", h.notified[0].lower())

    def test_a_closed_chat_leaves_the_code_with_the_seller(self):
        """Чат на этом маркетплейсе закрывается легко. Купленный код не
        должен остаться никому."""
        with Harness(self, settings=settings_with(),
                     send=(False, "no_active_orders_in_chat")) as h:
            h.run()
            self.assertNotIn("777", h.delivered, "выдачи не было")
            self.assertTrue(h.notified)
            self.assertIn("AAAA-1111", h.notified[0])

    def test_the_intent_is_written_before_the_supplier_is_called(self):
        """Обрыв связи не должен оставлять покупку, о которой никто не знает."""
        with Harness(self, settings=settings_with()) as h:
            h.run()
            # Первый снимок сделан до первого обращения к поставщику.
            self.assertTrue(h.saves)
            first = h.saves[0]["log"]
            self.assertTrue(first)
            self.assertEqual(first[0]["order"], "777")
            self.assertEqual(first[0]["codes"], [])

    def test_the_code_is_written_before_it_is_sent(self):
        """Иначе отправка в закрытый чат теряет его окончательно."""
        with Harness(self, settings=settings_with(),
                     send=(False, "закрыт")) as h:
            h.run()
            saved = [s for s in h.saves
                     if s["log"] and s["log"][0].get("codes")]
            self.assertTrue(saved, "код не попал в журнал до отправки")

    def test_a_repeat_after_a_break_reuses_the_same_reference(self):
        """На ту же ссылку поставщик отвечает IDEMPOTENCY_REPLAY — прежним
        результатом. Другая ссылка означала бы вторую покупку."""
        with Harness(self, settings=settings_with(),
                     send=(False, "закрыт")) as h:
            h.run()
            first_ref = h.orders[0][2]
            h.orders.clear()
            h.send_result = (True, "")
            h.run()
            self.assertEqual(h.orders[0][2], first_ref)


if __name__ == "__main__":
    unittest.main()
