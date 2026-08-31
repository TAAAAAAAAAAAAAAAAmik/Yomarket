"""«Я оплатил» — продавец говорит владельцу, что деньги ушли.

Оплата идёт вне бота, и увидеть её он не может. Узнать об этом ему
неоткуда, кроме как от самого продавца, — значит кнопка обязана честно
называться передачей заявки, а не проверкой оплаты. «Проверяю оплату» там,
где проверять нечем, отправило бы человека ждать того, чего не будет.

Три способа испортить это, и каждый проверяется:

* **потерять заявку** — журнал не настроен, а продавцу сказали «передал»;
* **превратить журнал в рассылку** — кнопку жмут по десять раз подряд
  именно тогда, когда ждут ответа, и настоящая заявка утонет среди
  повторов;
* **запереть кнопку подпиской** — заплативший не сможет об этом сказать
  ровно потому, что ещё не получил то, за что заплатил.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.commands as C                              # noqa: E402
import logs                                                # noqa: E402
import storage                                             # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Bench(unittest.TestCase):
    UID = 4242

    def setUp(self):
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        self.logged: list[tuple[str, list]] = []
        self._log = logs.log_event
        self.log_ok = True

        async def fake_log(bot, kind, lines, user=None):
            self.logged.append((kind, list(lines)))
            return self.log_ok

        logs.log_event = fake_log

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        logs.log_event = self._log

    def tap(self, uid=None):
        said: list[str] = []
        alerts: list[str] = []

        class Screen:
            async def answer(s, text, reply_markup=None, **kw):
                said.append(text)

        cb = type("CB", (), {})()
        cb.data = "pay:paid"
        cb.bot = None
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": uid or self.UID,
                                      "full_name": "Продавец",
                                      "username": "seller"})()

        async def answer(text="", **kw):
            alerts.append(text)

        cb.answer = answer
        run(C.paid_claim(cb))
        return said, alerts


class TheOwnerLearnsThatSomebodyPaid(Bench):

    def test_pressing_it_writes_to_the_log(self):
        self.tap()
        self.assertEqual([kind for kind, _l in self.logged], ["payment"])

    def test_the_entry_says_what_happened_and_what_to_do(self):
        """Строка «оплатил» без «проверь и выдай» оставляет владельца
        гадать, зачем ему это показали."""
        self.tap()
        text = " ".join(self.logged[0][1])
        self.assertIn("оплатил", text)
        self.assertIn("выдай", text)

    def test_the_seller_is_told_it_was_passed_on(self):
        said, alerts = self.tap()
        self.assertTrue(any("Передал" in t or "передал" in t
                            for t in said + alerts), said + alerts)

    def test_a_broken_log_is_not_dressed_up_as_success(self):
        """Худший исход здесь: продавец заплатил, ему сказали «передал», а
        владельцу не пришло ничего. Тогда надо сказать, куда писать."""
        self.log_ok = False
        said, _alerts = self.tap()
        self.assertTrue(any(storage.get_support_contact() in t for t in said),
                        said)
        self.assertFalse(any("Передал владельцу" in t for t in said), said)

    def test_it_does_not_promise_to_check_the_payment(self):
        """Проверять нечем: оплата идёт вне бота. Обещание проверки
        отправило бы человека ждать того, чего не будет."""
        said, alerts = self.tap()
        joined = " ".join(said + alerts).lower()
        for lie in ("проверяю", "проверю оплату", "нашёл оплату",
                    "оплата подтверждена"):
            with self.subTest(lie):
                self.assertNotIn(lie, joined)


class TheLogDoesNotBecomeASpamFeed(Bench):

    def test_a_second_press_in_a_row_writes_nothing_new(self):
        self.tap()
        self.tap()
        self.assertEqual(len(self.logged), 1, "заявка ушла дважды")

    def test_but_the_seller_is_answered_rather_than_ignored(self):
        """Молчание снаружи неотличимо от сломанной кнопки."""
        self.tap()
        said, alerts = self.tap()
        self.assertTrue(said or alerts)
        self.assertTrue(any("Уже передал" in t for t in said + alerts),
                        said + alerts)

    def test_another_seller_is_not_silenced_by_the_first(self):
        """Ограничение — на человека, а не на бота. Общий счётчик закрыл бы
        кнопку всем, кто нажал следом."""
        self.tap()
        self.tap(uid=999)
        self.assertEqual(len(self.logged), 2)

    def test_after_the_hour_it_can_be_said_again(self):
        self.tap()
        claims = self.admin["paid_claims"]
        claims[str(self.UID)] = claims[str(self.UID)] - storage.PAID_CLAIM_EVERY - 1
        self.tap()
        self.assertEqual(len(self.logged), 2)


class SayingItIsNotLockedBehindTheSubscription(unittest.TestCase):
    """Заплативший не может сказать об этом ровно потому, что ещё не
    получил то, за что заплатил, — это и есть запертая снаружи дверь."""

    def test_the_button_passes_the_gate(self):
        import main as M
        self.assertIn("pay:paid", M.FREE_CALLBACKS)

    def test_it_is_offered_next_to_asking_for_an_invoice(self):
        """Следующий шаг после «прошу счёт» — сказать, что оплатил. Искать
        для этого другой экран человек не должен."""
        import handlers.start as S
        data = [b.callback_data for row in S._access_kb(1).inline_keyboard
                for b in row]
        self.assertIn("pay:paid", data)
        self.assertIn("sub:order", data)


if __name__ == "__main__":
    unittest.main()
