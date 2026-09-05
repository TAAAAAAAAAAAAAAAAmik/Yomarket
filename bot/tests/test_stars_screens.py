"""Четыре экрана AutoStars, которые раньше отвечали «в следующем обновлении».

Кнопка-заглушка хуже отсутствующей: продавец на неё рассчитывает. Здесь
проверяется, что каждая из четырёх делает обещанное — и делает по данным, а
не по прикидке.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import plugins as P          # noqa: E402
from tasks import manager as M             # noqa: E402


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
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self):
        self.data: dict = {}

    async def set_state(self, s):
        pass

    async def clear(self):
        pass

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def buttons(kb) -> list[str]:
    return [] if kb is None else [b.text for row in kb.inline_keyboard
                                  for b in row]


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


def base_settings(**stars) -> dict:
    p = {"enabled": True, "amount": 50, "pending": {}, "delivered": [],
         "stuck": {}, "log": [], "notify": {}, "texts": {}}
    p.update(stars)
    return {"plugins": {"auto_stars": p}, "known_order_details": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.store = base_settings()
        P.get_settings = lambda uid: self.store
        P.save_settings = lambda uid, s: None

    def run_(self, coro):
        return asyncio.run(coro)


class NothingSaysComingSoon(Base):
    """Ни один из четырёх экранов больше не отговаривается обновлением."""

    def test_all_four_answer_with_a_screen(self):
        for handler in (P.stars_accumulated, P.stars_profit, P.stars_notifs):
            cb = CB("x")
            self.run_(handler(cb))
            self.assertTrue(cb.message.texts, handler.__name__)
            for alert in cb.alerts:
                self.assertNotIn("следующем обновлении", alert, handler.__name__)
        cb = CB("x")
        self.run_(P.stars_replies(cb, FSM()))
        self.assertTrue(cb.message.texts)


class Accumulated(Base):
    def test_an_empty_queue_says_so(self):
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("Пусто", cb.message.last)

    def test_orders_waiting_for_a_username_are_listed(self):
        self.store = base_settings(pending={
            "77": {"quantity": 100, "asked_at": time.time() - 7200}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("#77", cb.message.last)
        self.assertIn("100⭐", cb.message.last)
        self.assertIn("Ждут ник", cb.message.last)

    def test_a_failed_order_can_be_handed_out_from_here(self):
        """У сорвавшегося есть ник — значит выдать можно прямо сейчас."""
        self.store = base_settings(stuck={
            "88": {"username": "vasya", "quantity": 50,
                   "reason": "Bad request", "ts": time.time()}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("Сорвались", cb.message.last)
        self.assertIn("plugins:stars:retry:88", callbacks(cb.message.kbs[-1]))

    def test_the_reason_it_failed_is_shown(self):
        self.store = base_settings(stuck={
            "88": {"username": "v", "quantity": 50,
                   "reason": "нет прав", "ts": time.time()}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertIn("нет прав", cb.message.last)

    def test_waiting_orders_get_no_hand_out_button(self):
        """Ника нет — выдавать некому, кнопка вела бы в никуда."""
        self.store = base_settings(pending={"77": {"quantity": 100}})
        cb = CB("x")
        self.run_(P.stars_accumulated(cb))
        self.assertFalse([c for c in callbacks(cb.message.kbs[-1])
                          if "retry" in c])


class RetryingAFailedOrder(Base):
    def setUp(self):
        super().setUp()
        self.store = base_settings(stuck={
            "88": {"username": "vasya", "quantity": 50,
                   "reason": "Bad request", "ts": time.time()}})
        P.get_fragment_creds = lambda uid: {"cookies": {"a": "b"},
                                            "mnemonic": "w " * 24}
        P.save_settings = lambda uid, s: self.store.update(s)
        import automation.fragment as FR
        self.fr = FR
        self._buy = FR.buy_stars_sync

    def tearDown(self):
        self.fr.buy_stars_sync = self._buy

    def _buy_returns(self, ok, msg, ton=0.08):
        # **kwargs, а не перечень: вызов покупки пополнялся и будет
        # пополняться (прокси доехал сюда позже прочих путей), а подделка,
        # падающая на новом аргументе, превращается в «покупка не удалась» —
        # то есть проверяет не то, что написано в имени теста.
        def fake(cookies, mnemonic, username, qty, wallet_version="v4r2",
                 api_hash="", wait_confirm=True, report=None, **kw):
            if report is not None and ok:
                report["ton"] = ton
            return ok, msg
        self.fr.buy_stars_sync = fake

    def test_a_successful_retry_clears_the_order(self):
        self._buy_returns(True, "готово")
        self.run_(P.stars_retry(CB("plugins:stars:retry:88")))
        p = self.store["plugins"]["auto_stars"]
        self.assertNotIn("88", p["stuck"])
        self.assertIn("88", p["delivered"])

    def test_and_records_what_it_cost(self):
        self._buy_returns(True, "готово", ton=0.0812)
        self.run_(P.stars_retry(CB("plugins:stars:retry:88")))
        log = self.store["plugins"]["auto_stars"]["log"]
        self.assertEqual(len(log), 1, log)
        self.assertAlmostEqual(log[0]["ton"], 0.0812, places=5)
        self.assertEqual(log[0]["qty"], 50)

    def test_a_failed_retry_keeps_the_order_in_the_list(self):
        """Неудачная попытка — не повод потерять заказ."""
        self._buy_returns(False, "снова Bad request")
        cb = CB("plugins:stars:retry:88")
        self.run_(P.stars_retry(cb))
        self.assertIn("88", self.store["plugins"]["auto_stars"]["stuck"])
        self.assertIn("Bad request", cb.message.last)

    def test_missing_credentials_are_named_before_anything_is_tried(self):
        P.get_fragment_creds = lambda uid: {}
        cb = CB("plugins:stars:retry:88")
        self.run_(P.stars_retry(cb))
        self.assertTrue(any("Fragment" in a for a in cb.alerts), cb.alerts)

    def test_an_order_that_is_already_gone_is_not_bought_twice(self):
        self._buy_returns(True, "готово")
        cb = CB("plugins:stars:retry:404")
        self.run_(P.stars_retry(cb))
        self.assertFalse(self.store["plugins"]["auto_stars"]["log"])


class Profit(Base):
    def test_with_no_deliveries_it_does_not_invent_numbers(self):
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("нечего считать", cb.message.last)

    def test_it_counts_stars_roubles_and_ton(self):
        now = time.time()
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "revenue": 139.0,
             "ts": now - 100},
            {"order_id": "2", "qty": 50, "ton": 0.08, "revenue": 79.0,
             "ts": now - 200},
        ])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        text = cb.message.last
        self.assertIn("150⭐", text, text)
        self.assertIn("218 ₽", text, text)
        self.assertIn("0.2400 TON", text, text)

    def test_yesterday_is_not_counted_as_today(self):
        now = time.time()
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "ts": now - 200},
            {"order_id": "2", "qty": 500, "ton": 0.80, "ts": now - 3 * 86400},
        ])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        day = cb.message.last.split("За 30 дней")[0]
        self.assertIn("100⭐", day, day)
        self.assertNotIn("600⭐", day, day)

    def test_it_does_not_pretend_to_know_the_ton_rate(self):
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "revenue": 139.0,
             "ts": time.time()}])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("курс TON он не знает", cb.message.last)

    def test_an_order_without_a_known_price_does_not_break_the_sum(self):
        self.store = base_settings(log=[
            {"order_id": "1", "qty": 100, "ton": 0.16, "ts": time.time()},
            {"order_id": "2", "qty": 50, "ton": 0.08, "revenue": 79.0,
             "ts": time.time()}])
        cb = CB("x")
        self.run_(P.stars_profit(cb))
        self.assertIn("79 ₽", cb.message.last)


class Notifications(Base):
    def test_every_kind_can_be_switched_off(self):
        cb = CB("x")
        self.run_(P.stars_notifs(cb))
        data = callbacks(cb.message.kbs[-1])
        for key in ("done", "failed", "low_balance"):
            self.assertIn(f"plugins:stars:ntog:{key}", data)

    def test_toggling_sticks(self):
        saved = {}
        P.save_settings = lambda uid, s: saved.update(s)
        self.run_(P.stars_notif_toggle(CB("plugins:stars:ntog:done")))
        self.assertIs(self.store["plugins"]["auto_stars"]["notify"]["done"],
                      False)
        self.assertTrue(saved)

    def test_the_manager_obeys_the_switch(self):
        self.assertTrue(M.stars_notify_on(self.store, "done"))
        self.store["plugins"]["auto_stars"]["notify"]["done"] = False
        self.assertFalse(M.stars_notify_on(self.store, "done"))

    def test_an_unset_switch_means_on(self):
        self.assertTrue(M.stars_notify_on({"plugins": {"auto_stars": {}}},
                                          "failed"))


class Replies(Base):
    def test_the_standard_texts_are_shown(self):
        cb = CB("x")
        self.run_(P.stars_replies(cb, FSM()))
        self.assertIn("Запрос ника", cb.message.last)
        self.assertIn("стандартный", cb.message.last)

    def test_a_custom_text_replaces_the_standard_one(self):
        saved = {}
        P.save_settings = lambda uid, s: saved.update(s)
        st = FSM()
        st.data["reply_key"] = "done"
        msg = Screen()
        msg.text = "Спасибо! {qty}⭐ уже у @{username}"
        msg.from_user = type("U", (), {"id": 1})()
        self.run_(P.stars_reply_save(msg, st))
        self.assertEqual(
            M.stars_text(self.store, "done", qty=100, username="vasya"),
            "Спасибо! 100⭐ уже у @vasya")

    def test_a_dash_restores_the_standard(self):
        self.store["plugins"]["auto_stars"]["texts"]["done"] = "своё"
        st = FSM()
        st.data["reply_key"] = "done"
        msg = Screen()
        msg.text = "-"
        msg.from_user = type("U", (), {"id": 1})()
        self.run_(P.stars_reply_save(msg, st))
        self.assertIn("Готово", M.stars_text(self.store, "done", qty=1,
                                             username="x"))

    def test_a_typo_in_a_placeholder_does_not_kill_the_delivery(self):
        """Текст пишет человек — {опечатка} не должна ронять выдачу."""
        self.store["plugins"]["auto_stars"]["texts"]["done"] = "Готово {колво}⭐"
        out = M.stars_text(self.store, "done", qty=100, username="v")
        self.assertEqual(out, "Готово {колво}⭐")

    def test_the_standard_ask_still_carries_no_at_sign(self):
        """Иначе бот вычитает «@username» из своего же вопроса как ответ."""
        self.assertNotIn("@", M.stars_text({}, "ask"))


class ARepeatIsNotOfferedWhenItCouldCostMoney(Base):
    """«🔁 Повторить» под оборванной покупкой — кнопка, покупающая звёзды
    второй раз за деньги продавца.

    Исходов у выдачи было два — вышло и не вышло, — и оборванное ожидание
    попадало во второй вместе с обычным отказом Fragment. Между тем поток
    покупки после обрыва продолжает работать: он может отправить деньги уже
    после того, как экран сказал «не удалось». Третий исход — «неизвестно» —
    заведён ровно для этого.
    """

    def setUp(self):
        super().setUp()
        self.store = base_settings(stuck={
            "88": {"username": "vasya", "quantity": 50,
                   "reason": "Bad request", "ts": time.time()}})
        P.get_fragment_creds = lambda uid: {"cookies": {"a": "b"},
                                            "mnemonic": "w " * 24}
        P.save_settings = lambda uid, s: self.store.update(s)
        import automation.fragment as FR
        self.fr = FR
        self._buy = FR.buy_stars_sync

    def tearDown(self):
        self.fr.buy_stars_sync = self._buy

    def _hangs(self):
        def fake(*a, **kw):
            raise asyncio.TimeoutError()
        self.fr.buy_stars_sync = fake

    def _paid_but_unconfirmed(self):
        def fake(*a, **kw):
            if isinstance(kw.get("report"), dict):
                kw["report"].update({"sent_onchain": True, "ton": 0.081})
            return False, "Fragment не засчитал оплату"
        self.fr.buy_stars_sync = fake

    def _retry(self):
        cb = CB("plugins:stars:retry:88")
        self.run_(P.stars_retry(cb))
        return cb

    def test_an_interrupted_retry_is_not_called_a_failure(self):
        self._hangs()
        cb = self._retry()
        self.assertIn("неизвестно", cb.message.last)
        self.assertNotIn("Не вышло", cb.message.last)

    def test_and_says_not_to_press_again_before_checking(self):
        self._hangs()
        self.assertIn("fragment.com", self._retry().message.last)

    def test_the_order_keeps_the_mark_after_the_screen_is_gone(self):
        """Через час продавец увидит только список, а не этот экран."""
        self._hangs()
        self._retry()
        self.assertTrue(
            self.store["plugins"]["auto_stars"]["stuck"]["88"].get("unknown"))

    def test_money_that_already_left_is_named_with_the_amount(self):
        self._paid_but_unconfirmed()
        cb = self._retry()
        self.assertIn("0.0810", cb.message.last)
        self.assertIn("неизвестно", cb.message.last)

    def test_an_ordinary_refusal_is_still_an_ordinary_refusal(self):
        """Осторожность не должна съесть обычный путь: отказ Fragment,
        случившийся вовремя, деньгами не пахнет и повтора не запрещает."""
        def fake(*a, **kw):
            return False, "снова Bad request"
        self.fr.buy_stars_sync = fake
        cb = self._retry()
        self.assertIn("Не вышло", cb.message.last)
        self.assertFalse(
            self.store["plugins"]["auto_stars"]["stuck"]["88"].get("unknown"))


class TheManualHandOutScreenTooo(Base):
    """Второй ручной путь — «🚀 Ручная выдача». Там кнопка «Повторить»
    стояла прямо под сообщением о неудаче."""

    def setUp(self):
        super().setUp()
        import automation.fragment as FR
        self.fr = FR
        self._buy = FR.buy_stars_sync
        P.get_fragment_creds = lambda uid: {"cookies": {"a": "b"},
                                            "mnemonic": "w " * 24}

    def tearDown(self):
        self.fr.buy_stars_sync = self._buy

    def deliver(self, fake):
        self.fr.buy_stars_sync = fake
        return self.run_(P.deliver_stars(1, "vasya", 50))

    def test_an_interrupted_purchase_forbids_a_repeat(self):
        def hangs(*a, **kw):
            raise asyncio.TimeoutError()
        ok, msg, no_repeat = self.deliver(hangs)
        self.assertFalse(ok)
        self.assertTrue(no_repeat)
        self.assertIn("неизвестно", msg)

    def test_a_purchase_that_already_paid_forbids_it_too(self):
        def paid(*a, **kw):
            if isinstance(kw.get("report"), dict):
                kw["report"].update({"sent_onchain": True, "ton": 0.081})
            return False, "не засчитал"
        _ok, msg, no_repeat = self.deliver(paid)
        self.assertTrue(no_repeat)
        self.assertIn("0.0810", msg)

    def test_an_ordinary_refusal_still_allows_one(self):
        _ok, _msg, no_repeat = self.deliver(lambda *a, **kw: (False, "отказ"))
        self.assertFalse(no_repeat)

    def test_a_wordless_error_still_names_itself(self):
        def boom(*a, **kw):
            raise RuntimeError("")
        _ok, msg, _n = self.deliver(boom)
        self.assertIn("RuntimeError", msg)

    def test_success_is_success(self):
        ok, _msg, no_repeat = self.deliver(lambda *a, **kw: (True, "готово"))
        self.assertTrue(ok)
        self.assertFalse(no_repeat)

    def screen_after(self, fake) -> Screen:
        """Настоящий экран ручной выдачи, а не разбор её исходников."""
        self.fr.buy_stars_sync = fake
        out = Screen()

        class Msg:
            text = "50"
            from_user = type("U", (), {"id": 1})()

            async def answer(self, text, reply_markup=None, **kw):
                return await out.answer(text, reply_markup, **kw)

        fsm = FSM()
        fsm.data["buyer"] = "vasya"
        self.run_(P.stars_manual_amount_input(Msg(), fsm))
        return out

    def test_the_screen_hides_the_repeat_button_after_an_interrupted_buy(self):
        """Проверка проводки: значение может считаться верно, а экран всё
        равно нарисует кнопку."""
        def hangs(*a, **kw):
            raise asyncio.TimeoutError()

        out = self.screen_after(hangs)
        self.assertIn("неизвестно", out.last)
        self.assertNotIn("🔁 Повторить", buttons(out.kbs[-1]))

    def test_but_keeps_it_after_an_ordinary_refusal(self):
        out = self.screen_after(lambda *a, **kw: (False, "отказ"))
        self.assertIn("🔁 Повторить", buttons(out.kbs[-1]))


if __name__ == "__main__":
    unittest.main()
