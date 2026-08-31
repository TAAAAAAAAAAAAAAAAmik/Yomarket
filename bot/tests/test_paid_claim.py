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

        async def fake_log(bot, kind, lines, user=None, markup=None):
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


class TheOwnerGrantsInOneTap(Bench):
    """Проверить оплату бот не может — она идёт вне его. Но всё, что ПОСЛЕ
    проверки, он делает сам: владелец жмёт кнопку под записью, и дни уходят
    тому, кто их просил, ровно в том количестве, которое он оплатил.

    Без этого владелец шёл в админку и вводил номер продавца руками — а
    номер брал из той же записи, то есть переписывал цифры из сообщения в
    сообщение. Ошибиться цифрой здесь значит выдать месяц чужому человеку.
    """

    def setUp(self):
        super().setUp()
        self.markups: list = []

        async def fake_log(bot, kind, lines, user=None, markup=None):
            self.logged.append(list(lines))
            self.markups.append(markup)
            return True

        logs.log_event = fake_log
        self._is_admin = storage.is_admin
        storage.is_admin = lambda uid: True

    def tearDown(self):
        storage.is_admin = self._is_admin
        super().tearDown()

    def claim(self, data="pay:paid:30:1"):
        """Продавец жмёт «Я оплатил» и отдаёт кнопки, что ушли владельцу."""
        storage.set_price(30, 499)
        storage.add_pay_method("СБП", "+7 900")
        cb = type("CB", (), {})()
        cb.data = data
        cb.bot = None
        cb.message = type("M", (), {})()

        async def answer(text, reply_markup=None, **kw):
            return None

        cb.message.answer = answer
        cb.from_user = type("U", (), {"id": self.UID, "full_name": "Иван",
                                      "username": "ivan"})()
        cb.answer = lambda *a, **kw: asyncio.sleep(0)
        run(C.paid_claim(cb))
        return [(b.text, b.callback_data)
                for row in self.markups[-1].inline_keyboard for b in row]

    def press(self, data, admin=True):
        """Владелец жмёт кнопку под записью в журнале."""
        storage.is_admin = lambda uid: admin
        told: list[tuple[int, str]] = []
        edits: list[str] = []
        alerts: list[str] = []

        class Bot:
            async def send_message(s, chat, text, **kw):
                told.append((int(chat), str(text)))

        cb = type("CB", (), {})()
        cb.data = data
        cb.bot = Bot()
        cb.message = type("M", (), {})()
        cb.message.text = "заявка"
        cb.message.html_text = "заявка"

        async def edit_text(text, reply_markup=None, **kw):
            edits.append(text)

        async def edit_markup(**kw):
            edits.append("<сняты кнопки>")

        cb.message.edit_text = edit_text
        cb.message.edit_reply_markup = edit_markup

        async def answer(text="", **kw):
            alerts.append(text)

        cb.answer = answer
        cb.from_user = type("U", (), {"id": 1})()
        run(C.claim_confirm(cb) if ":ok:" in data else C.claim_reject(cb))
        return told, edits, alerts

    def _cid(self, buttons):
        return [d for _t, d in buttons if d.startswith("claim:ok:")][0]

    def test_the_entry_carries_a_grant_button_with_the_term_on_it(self):
        """Кнопка без числа дней заставляет вспоминать, сколько выдавать."""
        buttons = self.claim()
        grant = [t for t, d in buttons if d.startswith("claim:ok:")]
        self.assertEqual(len(grant), 1, buttons)
        self.assertIn("30", grant[0])

    def test_pressing_it_grants_exactly_those_days_to_that_seller(self):
        cid = self._cid(self.claim())
        self.press(cid)
        self.assertGreaterEqual(storage.subscription_days_left(self.UID), 29)

    def test_the_seller_is_told_his_access_is_open(self):
        cid = self._cid(self.claim())
        told, _e, _a = self.press(cid)
        self.assertTrue(any(uid == self.UID and "подтверждена" in t
                            for uid, t in told), told)

    def test_pressing_it_twice_does_not_grant_twice(self):
        """Нажимают дважды именно тогда, когда не поняли, сработало ли, а
        вторая выдача — это подаренный месяц."""
        cid = self._cid(self.claim())
        self.press(cid)
        before = storage.subscription_days_left(self.UID)
        _t, _e, alerts = self.press(cid)
        self.assertEqual(storage.subscription_days_left(self.UID), before)
        self.assertTrue(any("уже разобрали" in a for a in alerts), alerts)

    def test_the_entry_itself_shows_the_outcome(self):
        """Отдельное «выдал» ниже по ленте оставило бы прежнюю запись
        выглядеть неразобранной."""
        cid = self._cid(self.claim())
        _t, edits, _a = self.press(cid)
        self.assertTrue(any("Выдано 30" in e for e in edits), edits)

    def test_an_undatable_message_still_loses_its_buttons(self):
        """Сообщение старше двух суток Telegram править не даёт — но
        кнопки снять обязан, иначе по ним нажмут ещё раз."""
        cid = self._cid(self.claim())

        class Cb:
            pass

        told: list = []
        stripped: list = []

        class Bot:
            async def send_message(s, *a, **kw):
                told.append(1)

        cb = Cb()
        cb.data = cid
        cb.bot = Bot()
        cb.message = type("M", (), {})()
        cb.message.text = cb.message.html_text = "заявка"

        async def boom(*a, **kw):
            raise RuntimeError("message is too old")

        async def strip(**kw):
            stripped.append(1)

        cb.message.edit_text = boom
        cb.message.edit_reply_markup = strip
        cb.from_user = type("U", (), {"id": 1})()
        cb.answer = lambda *a, **kw: asyncio.sleep(0)
        run(C.claim_confirm(cb))
        self.assertTrue(stripped, "кнопки остались под старой заявкой")
        self.assertGreaterEqual(storage.subscription_days_left(self.UID), 29)

    def test_rejecting_grants_nothing_and_tells_the_seller(self):
        buttons = self.claim()
        cid = [d for _t, d in buttons if d.startswith("claim:no:")][0]
        told, _e, _a = self.press(cid)
        self.assertEqual(storage.subscription_days_left(self.UID), 0)
        self.assertTrue(any("не нашли" in t for _u, t in told), told)

    def test_a_stranger_cannot_grant_himself_a_subscription(self):
        """Кнопка живёт в группе, и нажать её может любой, кто там есть."""
        cid = self._cid(self.claim())
        told, _e, alerts = self.press(cid, admin=False)
        self.assertEqual(storage.subscription_days_left(self.UID), 0)
        self.assertTrue(any("доступа" in a for a in alerts), alerts)

    def test_a_claim_without_a_term_offers_no_grant_button(self):
        """Кнопка «Выдать» без числа дней выдала бы неизвестно сколько."""
        buttons = self.claim("pay:paid")
        self.assertEqual([d for _t, d in buttons if d.startswith("claim:ok:")],
                         [])
        self.assertTrue(any("вручную" in " ".join(self.logged[-1])
                            for _ in [0]), self.logged[-1])

    def test_a_claim_too_old_to_be_found_says_so(self):
        told, _e, alerts = self.press("claim:ok:99999")
        self.assertEqual(told, [])
        self.assertTrue(any("слишком старая" in a for a in alerts), alerts)

class SayingItIsNotLockedBehindTheSubscription(unittest.TestCase):
    """Заплативший не может сказать об этом ровно потому, что ещё не
    получил то, за что заплатил, — это и есть запертая снаружи дверь."""

    def test_the_button_passes_the_gate(self):
        import main as M
        self.assertIn("pay:paid", M.FREE_CALLBACKS)

    def test_the_tails_of_the_purchase_flow_pass_the_gate_too(self):
        """Адреса покупки носят срок и способ (`sub:buy:30`, `sub:m:30:2`,
        `pay:paid:30:2`) — перечнем их не задать, и проверка списка их бы
        не заметила."""
        import main as M
        for data in ("sub:buy:30", "sub:m:30:2", "pay:paid:30:2"):
            with self.subTest(data):
                self.assertTrue(data.startswith(M.FREE_PREFIXES), data)

    def test_it_is_offered_at_the_end_of_the_purchase_flow(self):
        """Кнопка переехала с экрана доступа на экран с реквизитами: там
        она и нужна — человек только что заплатил, и следующий его шаг
        именно этот."""
        import ast
        import pathlib
        src = pathlib.Path(
            storage.__file__).parent / "handlers" / "subscribe.py"
        found = [n.value for n in ast.walk(ast.parse(src.read_text("utf-8")))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and "pay:paid" in n.value]
        self.assertTrue(found, "«Я оплатил» пропало из потока покупки")


if __name__ == "__main__":
    unittest.main()
