"""AutoStars in the scheduler, with Fragment and the chain stubbed out.

A paid order left silently waiting is the worst outcome this feature has: the
buyer is out of pocket and nobody knows. So what is asserted is that nothing
goes quiet — the buyer gets chased, the seller gets told, and a purchase that
cannot be paid for is refused before the buyer is promised anything.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage                                   # noqa: E402

# The order-confirm call is not what these tests are about, and it is stubbed
# with None — its warning is expected noise.
logging.getLogger("tasks.manager").setLevel(logging.CRITICAL)


class Harness:
    def __init__(self, test, *, settings, balance_ton=10.0, buy=(True, "ok"),
                 buy_report=None):
        self.test = test
        self.uid = 4242
        self.settings = settings
        self.balance_ton = balance_ton
        self.buy_result = buy
        # Что покупка успела записать в `report` до того, как сорвалась.
        # Через него она сообщает единственный факт, которого не видно по
        # (ok, текст): деньги уже ушли из кошелька.
        self.buy_report = buy_report or {}
        self.bought: list = []
        self.chat_sent: list = []
        self.notified: list = []
        self._undo = []

    def _patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def __enter__(self):
        from automation import fragment

        def balance(mnemonic, version="v4r2"):
            if self.balance_ton is None:
                return False, "TonCenter недоступен"
            return True, {"ton": self.balance_ton, "nano": 0, "address": "EQ..."}

        def buy(cookies, mnemonic, username, quantity, *a, **kw):
            self.bought.append((username, quantity))
            if isinstance(kw.get("report"), dict):
                kw["report"].update(self.buy_report)
            return self.buy_result

        self._patch(fragment, "get_wallet_balance_sync", balance)
        self._patch(fragment, "buy_stars_sync", buy)
        self._patch(storage, "get_fragment_creds",
                    lambda uid: {"cookies": {"c": "1"}, "mnemonic": " ".join(
                        ["word"] * 24), "wallet_version": "v4r2"})
        self._patch(storage, "get_settings", lambda uid: self.settings)
        self._patch(storage, "save_settings", lambda uid, s: None)
        return self

    def __exit__(self, *exc):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)
        return False

    def manager(self):
        from tasks.manager import TaskManager
        tm = TaskManager.__new__(TaskManager)

        async def send_chat(api, chat_id, text, *a, **kw):
            self.chat_sent.append((chat_id, text))
            return True, ""

        async def notify(user_id, text, *a, **kw):
            self.notified.append(text)

        tm._send_chat = send_chat
        tm._notify = notify
        return tm

    def ask(self, order_id, title, chat_id="c1", status="paid"):
        # Статус по умолчанию — оплаченный: эти проверки о том, узнаёт ли бот
        # заказ со звёздами, а не о том, дошли ли деньги. Отказ выдавать
        # неоплаченный заказ проверяется отдельно.
        tm = self.manager()
        return asyncio.run(tm._maybe_ask_stars_username(
            None, self.settings, order_id, title, chat_id, status))

    def reply(self, order_id, text, chat_id="c1"):
        tm = self.manager()
        return asyncio.run(tm._maybe_deliver_stars_reply(
            self.uid, None, self.settings, order_id, text, chat_id))

    def sweep(self, now=None):
        tm = self.manager()
        return asyncio.run(tm._stars_pending_sweep(
            self.uid, None, self.settings, time.time() if now is None else now))

    def balance_watch(self, now=None):
        tm = self.manager()
        return asyncio.run(tm._stars_balance_watch(
            self.uid, self.settings, time.time() if now is None else now))


def settings_for(**over) -> dict:
    p = {"enabled": True, "amount": 50, "keyword": "", "ask_username": True,
         "pending": {}, "delivered": [], "wallet_version": "v4r2",
         "low_balance_warn": True, "low_balance_deliveries": 2,
         "balance_checked_at": 0, "balance_low": False}
    p.update(over)
    return {"plugins": {"auto_stars": p}}


def plugin(s):
    return s["plugins"]["auto_stars"]


class Recognising(unittest.TestCase):
    def test_a_listing_that_says_stars_in_english_now_starts_the_flow(self):
        s = settings_for()
        with Harness(self, settings=s) as h:
            h.ask("1", "Telegram Stars 100 — автовыдача")
        self.assertIn("1", plugin(s)["pending"])
        self.assertEqual(plugin(s)["pending"]["1"]["quantity"], 100)
        self.assertTrue(h.chat_sent, "the buyer was never asked")

    def test_a_year_in_the_title_is_not_bought_as_a_quantity(self):
        """This would have charged the seller for 2024 stars."""
        s = settings_for()
        with Harness(self, settings=s) as h:
            h.ask("2", "Stars 2024 — 100 шт")
        self.assertEqual(plugin(s)["pending"]["2"]["quantity"], 100)

    def test_an_unrelated_order_is_left_alone(self):
        s = settings_for()
        with Harness(self, settings=s) as h:
            h.ask("3", "Аккаунт Steam 4.000.000")
        self.assertEqual(plugin(s)["pending"], {})
        self.assertEqual(h.chat_sent, [])


class NotSpendingBlind(unittest.TestCase):
    def setUp(self):
        self.s = settings_for(pending={"7": {"quantity": 500,
                                             "asked_at": time.time(),
                                             "chat_id": "c1"}})

    def test_a_wallet_that_cannot_cover_it_refuses_before_promising(self):
        with Harness(self, settings=self.s, balance_ton=0.5) as h:
            handled = h.reply("7", "@durov")
        self.assertTrue(handled)
        self.assertEqual(h.bought, [], "bought with too little on the wallet")
        said = " ".join(t for _c, t in h.chat_sent)
        self.assertNotIn("Отправляю", said,
                         "the buyer must not be told it is on its way")
        self.assertIn("7", plugin(self.s)["pending"], "the order must not be lost")
        self.assertTrue(any("TON" in n for n in h.notified),
                        "the seller was not told why")

    def test_enough_on_the_wallet_goes_through(self):
        with Harness(self, settings=self.s, balance_ton=10.0) as h:
            h.reply("7", "@durov")
        self.assertEqual(h.bought, [("durov", 500)])

    def test_an_unreadable_balance_does_not_block_the_delivery(self):
        """TonCenter being down is not a reason to refuse a paid order."""
        with Harness(self, settings=self.s, balance_ton=None) as h:
            h.reply("7", "@durov")
        self.assertEqual(h.bought, [("durov", 500)])


class NotLosingOrders(unittest.TestCase):
    def test_a_silent_buyer_is_reminded_once(self):
        now = time.time()
        s = settings_for(pending={"9": {"quantity": 100, "chat_id": "c1",
                                        "asked_at": now - 3600, "reminded": 0}})
        with Harness(self, settings=s) as h:
            h.sweep(now)
            h.sweep(now + 60)          # and not again on the next tick
        nudges = [t for _c, t in h.chat_sent if "Напоминаем" in t]
        self.assertEqual(len(nudges), 1, h.chat_sent)

    def test_a_fresh_order_is_not_nagged(self):
        now = time.time()
        s = settings_for(pending={"9": {"quantity": 100, "chat_id": "c1",
                                        "asked_at": now - 60, "reminded": 0}})
        with Harness(self, settings=s) as h:
            self.assertEqual(h.sweep(now), "")
            self.assertEqual(h.chat_sent, [])

    def test_an_order_stuck_for_hours_reaches_the_seller(self):
        now = time.time()
        s = settings_for(pending={"9": {"quantity": 100, "chat_id": "c1",
                                        "asked_at": now - 9 * 3600,
                                        "reminded": 1,
                                        "title": "100 звёзд"}})
        with Harness(self, settings=s) as h:
            note = h.sweep(now)
            again = h.sweep(now + 3600)
        self.assertIn("ЖДУТ USERNAME", note)
        self.assertIn("#9", note)
        self.assertEqual(again, "", "the seller must not be told every hour")

    def test_nothing_pending_is_silence(self):
        with Harness(self, settings=settings_for()) as h:
            self.assertEqual(h.sweep(), "")


class WatchingTheWallet(unittest.TestCase):
    def test_a_wallet_running_out_is_flagged_while_there_is_time(self):
        s = settings_for(amount=100)
        with Harness(self, settings=s, balance_ton=0.6) as h:
            note = h.balance_watch()
        self.assertIn("ЗАКАНЧИВАЕТСЯ TON", note)
        self.assertTrue(plugin(s)["balance_low"])

    def test_a_healthy_wallet_says_nothing(self):
        s = settings_for(amount=100)
        with Harness(self, settings=s, balance_ton=50.0) as h:
            self.assertEqual(h.balance_watch(), "")

    def test_the_warning_is_not_repeated_every_cycle(self):
        s = settings_for(amount=100)
        now = time.time()
        with Harness(self, settings=s, balance_ton=0.6) as h:
            first = h.balance_watch(now)
            s["plugins"]["auto_stars"]["balance_checked_at"] = 0
            second = h.balance_watch(now + 7 * 3600)
        self.assertIn("ЗАКАНЧИВАЕТСЯ", first)
        self.assertEqual(second, "")

    def test_topping_up_is_worth_saying_once(self):
        s = settings_for(amount=100, balance_low=True)
        with Harness(self, settings=s, balance_ton=50.0) as h:
            note = h.balance_watch()
        self.assertIn("ПОПОЛНЕН", note)
        self.assertFalse(plugin(s)["balance_low"])

    def test_the_wallet_is_not_polled_on_every_tick(self):
        now = time.time()
        s = settings_for(amount=100, balance_checked_at=now - 60)
        with Harness(self, settings=s, balance_ton=0.1) as h:
            self.assertEqual(h.balance_watch(now), "")

    def test_it_stays_out_of_the_way_when_switched_off(self):
        s = settings_for(amount=100, low_balance_warn=False)
        with Harness(self, settings=s, balance_ton=0.1) as h:
            self.assertEqual(h.balance_watch(), "")



class RetryingAfterAFailure(unittest.TestCase):
    """Покупатель присылает ник один раз. Если покупка сорвалась, ждать от
    него второго сообщения бессмысленно — а именно это и происходило:
    заказ возвращался в ожидание пустым и стоял до эскалации."""

    def _settings(self, **over):
        p = {"enabled": True, "ask_username": True, "amount": 50,
             "pending": {}, "delivered": [], "stuck": {}}
        p.update(over)
        return {"plugins": {"auto_stars": p},
                "known_orders": {"77": "paid"},
                "known_order_details": {"77": {"chat_id": "99"}}}

    def _manager(self):
        from tasks.manager import TaskManager
        tm = TaskManager.__new__(TaskManager)
        tm.notes = []
        tm.delivered = []

        async def _notify(uid, text, **kw):
            tm.notes.append(text)

        async def _deliver(user_id, api, settings, order_id, text, chat_id):
            tm.delivered.append((order_id, text))
            return True

        tm._notify = _notify
        tm._maybe_deliver_stars_reply = _deliver
        return tm

    def test_the_username_survives_a_failed_purchase(self):
        """Иначе повторять будет нечем."""
        import inspect
        from tasks import manager as M
        src = inspect.getsource(M.TaskManager._maybe_deliver_stars_reply)
        self.assertIn('"username": username', src,
                      "ник должен сохраняться в pending при неудаче")

    def test_a_known_username_is_retried_without_the_buyer(self):
        import asyncio
        tm = self._manager()
        now = time.time()
        s = self._settings(pending={"77": {"quantity": 50, "username": "NO0RD",
                                           "chat_id": "99", "tries": 1,
                                           "asked_at": now - 60,
                                           "retry_at": now - 1}})
        asyncio.run(tm._stars_pending_sweep(1, None, s, now))
        self.assertEqual(tm.delivered, [("77", "@NO0RD")], tm.delivered)

    def test_it_waits_out_the_pause_between_attempts(self):
        """Повтор через секунду упрётся в ту же причину."""
        import asyncio
        tm = self._manager()
        now = time.time()
        s = self._settings(pending={"77": {"quantity": 50, "username": "NO0RD",
                                           "chat_id": "99", "tries": 1,
                                           "asked_at": now - 60,
                                           "retry_at": now + 600}})
        asyncio.run(tm._stars_pending_sweep(1, None, s, now))
        self.assertEqual(tm.delivered, [])

    def test_an_order_still_waiting_for_a_username_is_not_retried(self):
        import asyncio
        tm = self._manager()
        now = time.time()
        s = self._settings(pending={"77": {"quantity": 50, "chat_id": "99",
                                           "asked_at": now - 60}})
        asyncio.run(tm._stars_pending_sweep(1, None, s, now))
        self.assertEqual(tm.delivered, [])

    def test_the_next_attempt_is_scheduled_after_this_one(self):
        import asyncio
        tm = self._manager()
        now = time.time()
        entry = {"quantity": 50, "username": "NO0RD", "chat_id": "99",
                 "tries": 1, "asked_at": now - 60, "retry_at": now - 1}
        s = self._settings(pending={"77": entry})
        asyncio.run(tm._stars_pending_sweep(1, None, s, now))
        self.assertGreater(float(entry["retry_at"]), now)


class APurchaseAlreadyPaidForIsNeverRepeated(unittest.TestCase):
    """Повтор после списания покупает звёзды второй раз за деньги продавца.

    Провал покупки до сих пор значил одно: «ничего не вышло, пробуем снова».
    Но `confirmReq` идёт уже после того, как TON ушёл из кошелька, и его
    отказ — это «заплатили и не получили». Сюда же попадает обрыв по
    таймауту: ожидание seqno длится до двух минут и рвётся уже после оплаты.
    """

    def _settings(self):
        return settings_for(pending={"7": {"quantity": 500, "chat_id": "c1",
                                           "asked_at": time.time()}})

    PAID = {"sent_onchain": True, "ton": 1.85,
            "confirm_error": "Payment not found"}

    def test_it_does_not_buy_again(self):
        s = self._settings()
        with Harness(self, settings=s, buy=(False, "не подтверждено"),
                     buy_report=self.PAID) as h:
            h.reply("7", "@durov")
            h.sweep(time.time() + 100000)
        self.assertEqual(len(h.bought), 1, h.bought)

    def test_the_order_does_not_go_back_into_the_queue(self):
        s = self._settings()
        with Harness(self, settings=s, buy=(False, "не подтверждено"),
                     buy_report=self.PAID) as h:
            h.reply("7", "@durov")
        self.assertNotIn("7", plugin(s)["pending"])
        self.assertIn("7", plugin(s)["stuck"])

    def test_the_seller_is_told_the_money_left_and_how_much(self):
        s = self._settings()
        with Harness(self, settings=s, buy=(False, "не подтверждено"),
                     buy_report=self.PAID) as h:
            h.reply("7", "@durov")
        said = " ".join(h.notified)
        self.assertIn("оплата ушла", said)
        self.assertIn("1.85", said)

    def test_the_buyer_is_not_promised_a_retry(self):
        """Шаблон «не получилось, пробуем ещё раз» здесь означал бы обещание
        второй покупки, которой не будет."""
        s = self._settings()
        with Harness(self, settings=s, buy=(False, "не подтверждено"),
                     buy_report=self.PAID) as h:
            h.reply("7", "@durov")
        self.assertEqual([t for _c, t in h.chat_sent
                          if "ещё раз" in t or "повтор" in t.lower()], [])

    def test_a_failure_before_any_payment_is_still_retried(self):
        """Иначе одна сетевая ошибка хоронила бы заказ."""
        s = self._settings()
        with Harness(self, settings=s, buy=(False, "сеть недоступна")) as h:
            h.reply("7", "@durov")
        self.assertIn("7", plugin(s)["pending"])
        self.assertNotIn("7", plugin(s).get("stuck", {}))

if __name__ == "__main__":
    unittest.main()
