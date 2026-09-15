"""Экран, говорящий о доступе, сначала смотрит, есть ли он.

Живая жалоба 01.09: «после того как активировалась бесплатная подписка,
всё равно после команды старт висит кнопка получить доступ, а доступа к
функциям бота нету» — и следом: «та же проблема после того как оплатил».

Причина одна на всё семейство: `/start` ветвился только по токену. Взявший
пробу или заплативший снова видел витрину с «🚀 Получить доступ» — то есть
предложение купить то, что у него уже есть, и ни слова о том, что делать
дальше. Того единственного шага, который ему оставался — подключить
магазин, — на экране не было вовсе.

Того же класса нашлось ещё два:

* экран «Получить доступ» продавал доступ имеющему и предлагал пробы,
  которые поверх подписки не выдаются;
* отказ на пробе звучал «Сейчас пробный период не выдаётся» — притом что
  период включён, просто поверх оплаты он не даётся. Человек читает это
  как поломку.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.start as S                                 # noqa: E402
import logs                                                # noqa: E402
import storage                                             # noqa: E402


def run(coro):
    return asyncio.run(coro)


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class Msg:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.out: list = []
        self.bot = None

    async def answer(self, text, reply_markup=None, **kw):
        self.out.append((text, reply_markup))


class Screen:
    def __init__(self):
        self.text = ""
        self.markup = None

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text, self.markup = text, reply_markup


class St:
    async def clear(self):
        return None

    async def set_state(self, x):
        return None

    async def update_data(self, **kw):
        return None


class Bench(unittest.TestCase):
    UID = 7

    def setUp(self):
        self.admin: dict = {}
        self.blobs: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._read, self._write = storage._read_blob, storage._write_blob
        self._owner, self._log = storage.is_owner, logs.log_event
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage._read_blob = lambda n: (self.admin if n == "admin"
                                        else self.blobs.setdefault(n, {}))
        storage._write_blob = lambda n, d: (self.admin.update(d) if n == "admin"
                                            else self.blobs.__setitem__(n, d))
        storage.is_owner = lambda uid: False

        async def nolog(*a, **kw):
            return True

        logs.log_event = nolog
        storage.set_price(30, 499)
        storage.set_trial_channel("@ch")

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage._read_blob, storage._write_blob = self._read, self._write
        storage.is_owner, logs.log_event = self._owner, self._log

    def start(self):
        m = Msg(self.UID)
        run(S.cmd_start(m, St()))
        text, kb = m.out[-1]
        return plain(text), [b.callback_data for row in kb.inline_keyboard
                             for b in row]

    def tap(self, data, handler):
        cb = type("CB", (), {})()
        cb.data = data
        cb.bot = None
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": self.UID})()
        cb.alerts: list[str] = []

        async def answer(text="", **kw):
            cb.alerts.append(text)

        cb.answer = answer
        run(handler(cb, St()))
        return cb


class StartKnowsWhetherAccessIsAlreadyOpen(Bench):

    def test_without_access_it_sells(self):
        text, data = self.start()
        self.assertIn("access:menu", data)
        self.assertIn("YooMarket", text)

    def test_after_the_trial_it_stops_selling(self):
        """Он нажал «получить доступ», получил его — и снова видел ту же
        кнопку."""
        storage.start_trial(self.UID, 3, kind="free")
        text, data = self.start()
        self.assertIn("Доступ открыт", text)
        self.assertNotIn("access:menu", data[:1],
                         "«Получить доступ» осталось главной кнопкой")

    def test_and_offers_the_one_step_that_is_left(self):
        """Того единственного, что ему оставалось, на экране не было."""
        storage.start_trial(self.UID, 3, kind="free")
        _text, data = self.start()
        self.assertEqual(data[0], "start:connect")

    def test_after_paying_the_same(self):
        """«Та же проблема после того как оплатил» — та же причина."""
        storage.grant_subscription(self.UID, 30, by=1)
        text, data = self.start()
        self.assertIn("Доступ открыт", text)
        self.assertEqual(data[0], "start:connect")

    def test_it_says_how_much_is_left(self):
        storage.grant_subscription(self.UID, 30, by=1)
        text, _data = self.start()
        self.assertIn("29 дн.", text)

    def test_a_lifetime_subscription_is_not_counted_in_days(self):
        storage.grant_subscription(self.UID, storage.LIFETIME_DAYS, by=1)
        text, _data = self.start()
        self.assertIn("навсегда", text)
        self.assertNotIn(str(storage.LIFETIME_DAYS), text)

    def test_an_expired_subscription_sells_again(self):
        """Истёкшая — это отсутствие доступа, а не «был же»."""
        self.admin["subscriptions"] = {str(self.UID): {"expires": 1, "by": 1}}
        _text, data = self.start()
        self.assertIn("access:menu", data)

    def test_a_connected_shop_still_goes_straight_to_the_menu(self):
        """Обратная сторона: у кого магазин на связи, тому ни витрина, ни
        «доступ открыт» не нужны — ему нужно меню."""
        self.blobs["tokens"] = {str(self.UID): "wli-token"}
        saved = S.get_token
        S.get_token = lambda uid: "wli-token"
        try:
            m = Msg(self.UID)
            run(S.cmd_start(m, St()))
            self.assertIn("Главное меню", m.out[-1][0])
        finally:
            S.get_token = saved


class TheAccessScreenDoesNotSellWhatHeAlreadyHas(Bench):

    def test_it_says_access_is_open(self):
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("access:menu", lambda c, s: S.show_access(c))
        self.assertIn("Доступ уже открыт", plain(cb.message.text))

    def test_the_tariffs_become_a_way_to_extend(self):
        """Он пришёл сюда продлевать — «По подписке» звучит так, будто
        подписки у него нет."""
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("access:menu", lambda c, s: S.show_access(c))
        self.assertIn("Продлить", plain(cb.message.text))

    def test_trials_are_not_offered_on_top_of_a_subscription(self):
        """Поверх действующей подписки проба не выдаётся, и кнопка вела бы
        к отказу."""
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("access:menu", lambda c, s: S.show_access(c))
        data = [b.callback_data for row in cb.message.markup.inline_keyboard
                for b in row]
        self.assertNotIn("trial:free", data)
        self.assertNotIn("trial:offer", data)
        self.assertNotIn("Бесплатно", plain(cb.message.text))

    def test_but_paying_stays_possible(self):
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("access:menu", lambda c, s: S.show_access(c))
        data = [b.callback_data for row in cb.message.markup.inline_keyboard
                for b in row]
        self.assertIn("sub:buy", data)

    def test_without_access_the_trials_are_offered_as_before(self):
        cb = self.tap("access:menu", lambda c, s: S.show_access(c))
        data = [b.callback_data for row in cb.message.markup.inline_keyboard
                for b in row]
        self.assertIn("trial:free", data)
        self.assertIn("trial:offer", data)


class ARefusalNamesItsRealReason(Bench):

    def test_having_access_is_said_plainly(self):
        """«Сейчас пробный период не выдаётся» тому, у кого просто уже есть
        доступ, читается как поломка: он видел кнопку, нажал, и бот ответил
        про что-то своё."""
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("trial:free", S.trial_free)
        said = " ".join(cb.alerts)
        self.assertIn("уже есть доступ", said)
        self.assertIn("29 дн.", said)

    def test_a_switched_off_trial_says_that_instead(self):
        storage.set_trial_free_days(0)
        cb = self.tap("trial:free", S.trial_free)
        self.assertIn("выключен", " ".join(cb.alerts))

    def test_an_already_taken_trial_says_that_instead(self):
        storage.note_trial(self.UID, "free")
        cb = self.tap("trial:free", S.trial_free)
        self.assertIn("уже брали", " ".join(cb.alerts))

    def test_the_alert_carries_no_html(self):
        """Всплывающее окно Telegram не форматирует: `<b>` доехало бы до
        человека как есть."""
        storage.grant_subscription(self.UID, 30, by=1)
        cb = self.tap("trial:free", S.trial_free)
        self.assertNotIn("<", " ".join(cb.alerts))


if __name__ == "__main__":
    unittest.main()
