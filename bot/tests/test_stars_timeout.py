"""Оборванное ожидание покупки — это неизвестность, а не отказ.

Покупка звёзд идёт в отдельном потоке, а менеджер ждёт её с таймаутом.
Ожидание оборвалось — поток на этом не останавливается: он доживает свою
цепочку и может отправить деньги через секунду после того, как менеджер уже
решил «не вышло» и поставил заказ в очередь на повтор. Через двадцать минут
повтор покупает звёзды **второй раз, за те же деньги продавца**.

Отчёт о тратах от этого не спасает: менеджер читает его сразу после обрыва,
а поток пишет в него позже. Дважды списанные деньги продавец увидит только в
истории кошелька.

Сам таймаут был при этом не запасом, а рабочим режимом: 180 секунд против
цепочки, где одна только страница с хешем, три написания ника, заявка,
ссылка, seqno, отправка, ожидание подтверждения и подтверждение дают заведомо
больше. Обрыв приходился ровно на то место, где деньги уже могли уйти.

Отдельно: у `TimeoutError` пустой `str()`, и продавцу приходило «ошибка: »
без ошибки.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                   # noqa: E402
from automation import fragment                  # noqa: E402
import tasks.manager as M                        # noqa: E402


# Набор проверяет сам плагин, а он от того, показан ли он продавцу, не
# меняется. Без этого спрятанный плагин унёс бы с собой и свои проверки:
# вернув его однажды, мы узнали бы о поломке от продавца.
#
# Правка ОТКАТЫВАЕТСЯ: подменённый на импорте флаг остался бы подменённым
# на весь прогон, и проверки «плагин спрятан» падали бы через раз — в
# зависимости от того, какой набор загрузился раньше.
import features                                            # noqa: E402

_STARS_WAS = features.STARS_HIDDEN


def setUpModule():
    global _STARS_WAS
    _STARS_WAS = features.STARS_HIDDEN
    features.STARS_HIDDEN = False


def tearDownModule():
    features.STARS_HIDDEN = _STARS_WAS


logging.getLogger("tasks.manager").setLevel(logging.CRITICAL)


class Bench(unittest.TestCase):
    """Покупка, которая не успевает ответить в отведённое время."""

    UID = 4242
    OID = "7"

    def setUp(self):
        self.bought: list = []
        self.chat: list = []
        self.notes: list = []
        self.settings = {"plugins": {"auto_stars": {
            "enabled": True, "amount": 50, "ask_username": True,
            "pending": {self.OID: {"quantity": 100, "chat_id": "c1",
                                   "asked_at": time.time()}},
            "delivered": [], "wallet_version": "v4r2"}}}

        self._undo: list = []

        def patch(module, name, value):
            self._undo.append((module, name, getattr(module, name)))
            setattr(module, name, value)

        self.patch = patch
        patch(fragment, "get_wallet_balance_sync",
              lambda m, v="v4r2": (True, {"ton": 10.0, "nano": 0,
                                          "address": "EQ..."}))
        patch(storage, "get_fragment_creds",
              lambda uid: {"cookies": {"c": "1"},
                           "mnemonic": " ".join(["word"] * 24),
                           "wallet_version": "v4r2"})
        patch(storage, "get_settings", lambda uid: self.settings)
        patch(storage, "save_settings", lambda uid, s: None)

    def tearDown(self):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def plugin(self):
        return self.settings["plugins"]["auto_stars"]

    def deliver(self, *, hangs=False, report=None, late_report=None,
                result=(False, "отказ"), timeout=0.05):
        """Прогон выдачи.

        `hangs` — поток не успевает в отведённое время. `report` пишется до
        зависания (так и бывает: отметка о списании ставится сразу после
        отправки перевода, а зависает уже ожидание сети), `late_report` —
        после, и это та самая гонка: менеджер прочитал отчёт раньше, чем
        поток успел в него написать.
        """
        outer = self

        def buy(cookies, mnemonic, username, quantity, *a, **kw):
            outer.bought.append((username, quantity))
            box = kw.get("report")
            if isinstance(box, dict) and report:
                box.update(report)
            if hangs:
                # Поток живёт своей жизнью и после обрыва ожидания. Спим
                # заметно дольше таймаута — так же ведёт себя настоящая
                # покупка, у которой впереди ещё две минуты ожидания сети.
                time.sleep(timeout * 6)
            if isinstance(box, dict) and late_report:
                box.update(late_report)
            return result

        self.patch(fragment, "buy_stars_sync", buy)

        tm = M.TaskManager.__new__(M.TaskManager)
        tm._STARS_BUY_TIMEOUT = timeout

        async def send_chat(api, chat_id, text, *a, **kw):
            self.chat.append(text)
            return True, ""

        async def notify(uid, text, *a, **kw):
            self.notes.append(text)

        tm._send_chat = send_chat
        tm._notify = notify

        class Api:
            async def get_order(self, oid):
                return {"data": {"id": oid, "status": "paid"}}

            async def confirm_order(self, oid):
                return {"ok": True}

        return asyncio.run(tm._maybe_deliver_stars_reply(
            self.UID, Api(), self.settings, self.OID, "@durov", "c1"))


class AnInterruptedPurchaseIsNotRetried(Bench):
    def test_the_order_does_not_go_back_into_the_queue(self):
        """Здесь и покупались звёзды второй раз."""
        self.deliver(hangs=True)
        self.assertNotIn(self.OID, self.plugin()["pending"])

    def test_even_when_the_money_leaves_after_we_gave_up(self):
        """Гонка в чистом виде: менеджер прочитал отчёт о тратах, а поток
        написал в него позже. Узнать о списании было нельзя — значит и
        повторять нельзя тем более."""
        self.deliver(hangs=True, late_report={"sent_onchain": True,
                                              "ton": 1.5})
        self.assertNotIn(self.OID, self.plugin()["pending"])
        self.assertIn(self.OID, self.plugin().get("stuck", {}))

    def test_it_is_handed_to_the_seller_instead(self):
        self.deliver(hangs=True)
        self.assertIn(self.OID, self.plugin().get("stuck", {}))

    def test_and_marked_as_an_unknown_outcome_not_a_failure(self):
        self.deliver(hangs=True)
        self.assertTrue(self.plugin()["stuck"][self.OID].get("unknown"))

    def test_the_seller_is_told_the_money_may_have_left(self):
        self.deliver(hangs=True)
        said = "\n".join(self.notes)
        self.assertIn("неизвестно", said)
        self.assertIn("повтор не делаю", said.lower())

    def test_the_reason_is_not_an_empty_string(self):
        """У TimeoutError пустой str(), и приходило «ошибка: » без ошибки."""
        self.deliver(hangs=True)
        said = "\n".join(self.notes)
        self.assertNotIn("ошибка: \n", said)
        self.assertIn("мин", said)

    def test_a_wordless_exception_still_names_itself(self):
        """Не только у таймаута пустой текст. Пустая причина на экране
        продавца — то же самое, что её отсутствие."""
        def boom(*a, **kw):
            raise RuntimeError("")

        self.patch(fragment, "buy_stars_sync", boom)
        tm = M.TaskManager.__new__(M.TaskManager)

        async def send_chat(api, chat_id, text, *a, **kw):
            return True, ""

        async def notify(uid, text, *a, **kw):
            self.notes.append(text)

        tm._send_chat, tm._notify = send_chat, notify

        class Api:
            async def get_order(self, oid):
                return {"data": {"id": oid, "status": "paid"}}

        asyncio.run(tm._maybe_deliver_stars_reply(
            self.UID, Api(), self.settings, self.OID, "@durov", "c1"))
        said = "\n".join(self.notes)
        self.assertIn("RuntimeError", said)

    def test_the_username_is_still_recorded_for_manual_delivery(self):
        self.deliver(hangs=True)
        self.assertEqual(self.plugin()["stuck"][self.OID]["username"], "durov")
        self.assertEqual(self.plugin()["stuck"][self.OID]["quantity"], 100)


class AnOrdinaryRefusalStillGetsASecondChance(Bench):
    """Осторожность не должна съесть полезное поведение: обычный отказ
    Fragment чаще всего временный, и повтор здесь по-прежнему нужен."""

    def test_a_refusal_that_answered_in_time_is_queued_again(self):
        self.deliver(result=(False, "Fragment: временная ошибка"))
        self.assertIn(self.OID, self.plugin()["pending"])
        self.assertEqual(self.plugin()["pending"][self.OID]["tries"], 1)

    def test_it_is_not_marked_unknown(self):
        self.deliver(result=(False, "Fragment: временная ошибка"))
        self.assertEqual(self.plugin().get("stuck", {}), {})

    def test_a_successful_purchase_is_unaffected(self):
        self.deliver(result=(True, "✅ 100⭐ отправлены"))
        self.assertIn(self.OID, self.plugin()["delivered"])
        self.assertEqual(self.plugin().get("stuck", {}), {})


class MoneyAlreadyGoneStillWinsOverEverything(Bench):
    """Если покупка успела сообщить, что деньги ушли, — это сильнее любого
    таймаута: заказ уходит продавцу с суммой, а не с «неизвестно»."""

    def test_a_paid_but_unconfirmed_purchase_is_reported_as_paid(self):
        self.deliver(hangs=True, report={"sent_onchain": True, "ton": 1.5})
        stuck = self.plugin()["stuck"][self.OID]
        self.assertTrue(stuck.get("paid"))
        self.assertEqual(stuck["ton"], 1.5)

    def test_and_never_retried(self):
        self.deliver(hangs=True, report={"sent_onchain": True, "ton": 1.5})
        self.assertNotIn(self.OID, self.plugin()["pending"])


class TheSessionIsWatchedOnASchedule(Bench):
    """Куки Fragment живут часами, и до сих пор об истечении узнавали в
    единственный неподходящий момент: заказ оплачен, ник прислан, звёзды не
    уходят. Проверка идёт по расписанию, а не при заказе."""

    def watch(self, alive, why="", *, now=None):
        self.patch(fragment, "session_alive_sync",
                   lambda cookies, proxy="": (alive, why))
        tm = M.TaskManager.__new__(M.TaskManager)

        async def notify(uid, text, *a, **kw):
            self.notes.append(text)

        tm._notify = notify
        return asyncio.run(tm._stars_session_watch(
            self.UID, self.settings, time.time() if now is None else now))

    def test_a_live_session_says_nothing(self):
        self.assertEqual(self.watch(True, "вижу личный раздел"), "")

    def test_an_expired_one_warns_before_the_next_order(self):
        got = self.watch(False, "страница отдана гостю")
        self.assertIn("СЕССИЯ FRAGMENT ИСТЕКЛА", got)
        self.assertIn("Данные Fragment", got)

    def test_and_says_what_will_happen_if_nothing_is_done(self):
        self.assertIn("не сработает", self.watch(False, "гость"))

    def test_it_does_not_repeat_every_hour(self):
        self.watch(False, "гость")
        self.plugin()["session_checked_at"] = 0
        self.assertEqual(self.watch(False, "гость"), "")

    def test_coming_back_to_life_is_said_once(self):
        self.watch(False, "гость")
        self.plugin()["session_checked_at"] = 0
        got = self.watch(True, "вижу личный раздел")
        self.assertIn("СНОВА ЖИВА", got)
        self.plugin()["session_checked_at"] = 0
        self.assertEqual(self.watch(True, "вижу личный раздел"), "")

    def test_it_is_not_checked_more_often_than_the_interval(self):
        """Лишний запрос к Fragment каждую минуту — способ потерять сессию,
        а не проверить её."""
        calls: list = []

        def probe(cookies, proxy=""):
            calls.append(1)
            return True, "жива"

        self.patch(fragment, "session_alive_sync", probe)
        tm = M.TaskManager.__new__(M.TaskManager)

        async def notify(uid, text, *a, **kw):
            pass

        tm._notify = notify
        now = time.time()
        for _ in range(5):
            asyncio.run(tm._stars_session_watch(self.UID, self.settings, now))
        self.assertEqual(len(calls), 1, calls)

    def test_the_periodic_tick_actually_calls_it(self):
        """Проверка проводки, а не расчёта. Функция может считать верно, а на
        экран её никто не зовёт — в этом проекте так выходило трижды."""
        called: list = []

        async def watch(uid, settings, now):
            called.append(uid)
            return ""

        async def nothing(*a, **kw):
            return ""

        tm = M.TaskManager.__new__(M.TaskManager)
        tm._stars_session_watch = watch
        tm._stars_pending_sweep = nothing
        tm._stars_balance_watch = nothing
        tm._panel_bump = nothing

        async def notify(uid, text, *a, **kw):
            self.notes.append(text)

        tm._notify = notify

        class Api:
            async def start(self):
                pass

            async def close(self):
                pass

            def __getattr__(self, name):
                async def anything(*a, **kw):
                    return {}
                return anything

        self.patch(M, "YooMarketAPI", lambda token=None: Api())
        self.patch(M, "get_settings", lambda uid: self.settings)
        self.patch(M, "save_settings", lambda uid, s: None)
        self.patch(M, "_claim_sender", lambda s, now: True)
        try:
            asyncio.run(tm._tick_auto_locked(self.UID, "tok"))
        except Exception as e:                     # noqa: BLE001
            self.fail(f"проход расписания упал: {e}")
        self.assertEqual(called, [self.UID])

    def test_a_switched_off_plugin_is_not_probed(self):
        self.plugin()["enabled"] = False
        calls: list = []
        self.patch(fragment, "session_alive_sync",
                   lambda c, proxy="": calls.append(1) or (True, ""))
        tm = M.TaskManager.__new__(M.TaskManager)
        asyncio.run(tm._stars_session_watch(self.UID, self.settings,
                                            time.time()))
        self.assertEqual(calls, [])


class TheTimeoutFitsTheChainItWaitsFor(unittest.TestCase):
    """Таймаут меньше самой цепочки — не запас, а гарантированный обрыв в
    середине оплаты."""

    def test_it_covers_the_documented_steps(self):
        from automation.fragment import SEQNO_MAX_WAIT_SECS
        # Страница 30 + три написания ника по 30 + заявка 30 + ссылка 30 +
        # seqno 15 + отправка 20 + подтверждение 30 = 245 без ожидания сети.
        floor = 245 + SEQNO_MAX_WAIT_SECS
        self.assertGreaterEqual(M.TaskManager._STARS_BUY_TIMEOUT, floor)


if __name__ == "__main__":
    unittest.main()
