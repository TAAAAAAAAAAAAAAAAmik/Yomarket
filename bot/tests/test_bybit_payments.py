"""Оплата подписки переводом внутри Bybit.

Здесь чужие деньги, и каждая проверка написана про конкретный способ их
потерять:

* **выдать дважды за один перевод** — бот перезапустился, поступление
  прочиталось снова, продавец получил вторые дни бесплатно;
* **выдать за неподтверждённый перевод** — `status: 1` означает «ещё
  неизвестно», а не «почти успех»;
* **взять деньги и не дать ничего** — продавец заплатил раньше, чем назвал
  свой UID, и перевод было бы некому приписать;
* **отдать чужую оплату** — вписал чужой UID и забрал чужие дни;
* **принять HTTP 200 за успех** — у Bybit всё существенное в конверте.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import billing                                             # noqa: E402
import storage                                             # noqa: E402
from automation import bybit                                # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Bot:
    """Фальшивый бот: запоминает, кому что сказали."""

    def __init__(self):
        self.said: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.said.append((int(chat_id), str(text)))


def row(tx="tx1", uid="118027304", amount="5", coin="USDT", status=2):
    """Запись поступления — ровно той формы, что описана у Bybit."""
    return {"id": "1103", "type": 1, "coin": coin, "amount": str(amount),
            "status": status, "address": "xxxx***@gmail.com",
            "createdTime": str(int(time.time())), "fromMemberId": uid,
            "txID": tx}


class Bench(unittest.TestCase):
    SELLER = 4242
    UID = "118027304"

    def setUp(self):
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage.set_bybit(uid="999", key="K", secret="S", enabled=True)
        for days, price in ((14, 3.0), (30, 5.0), (365, 40.0)):
            storage.set_usdt_price(days, price)
        storage.set_payer_uid(self.SELLER, self.UID)
        self.bot = Bot()
        # Журнал владельца в тестах не нужен и требует живого бота.
        self._log = billing._tell_owner
        self.told: list[str] = []

        async def told(bot, text):
            self.told.append(text)

        billing._tell_owner = told

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        billing._tell_owner = self._log

    def feed(self, *rows):
        """Прогнать поступления через разбор, подменив поход в Bybit."""
        saved = billing.internal_deposits
        billing.internal_deposits = lambda *a, **k: list(rows)
        try:
            return run(billing.collect(self.bot))
        finally:
            billing.internal_deposits = saved

    def days_left(self):
        return storage.subscription_days_left(self.SELLER)


class TheSignatureIsBuiltTheWayBybitDocumentsIt(unittest.TestCase):
    """Секрета из примеров документации нам не дали — сверить готовый хеш
    не с чем. А вот подписываемую строку сверить можно, и ошибаются
    именно в ней."""

    def test_it_matches_the_documented_example_byte_for_byte(self):
        self.assertEqual(
            bybit._payload("1658384314791", "XXXXXXXXXX",
                           "category=option&symbol=BTC-29JUL22-25000-C",
                           "5000"),
            "1658384314791XXXXXXXXXX5000"
            "category=option&symbol=BTC-29JUL22-25000-C")

    def test_the_signature_is_lowercase_hex(self):
        sign = bybit._sign("secret", "1", "key", "a=1")
        self.assertEqual(sign, sign.lower())
        self.assertEqual(len(sign), 64)

    def test_a_different_secret_gives_a_different_signature(self):
        self.assertNotEqual(bybit._sign("a", "1", "k", "q=1"),
                            bybit._sign("b", "1", "k", "q=1"))


class ARefusalIsNotSuccessEvenWithHttp200(unittest.TestCase):
    """У Bybit снаружи всегда двухсотый, а получилось ли — говорит
    `retCode` в теле. Ровно та же беда, что была с AppRoute."""

    class Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class Session:
        def __init__(self, body):
            self.body = body

        def get(self, *a, **k):
            return ARefusalIsNotSuccessEvenWithHttp200.Resp(self.body)

    def test_a_nonzero_retcode_raises(self):
        s = self.Session({"retCode": 10005, "retMsg": "permission denied"})
        with self.assertRaises(bybit.BybitError) as e:
            bybit.internal_deposits("k", "s", session=s)
        self.assertEqual(e.exception.code, 10005)

    def test_the_refusal_is_in_russian(self):
        s = self.Session({"retCode": 10005, "retMsg": "permission denied"})
        with self.assertRaises(bybit.BybitError) as e:
            bybit.internal_deposits("k", "s", session=s)
        said = str(e.exception)
        self.assertNotIn("permission denied", said)
        self.assertIn("прав", said.lower())

    def test_retcode_zero_is_success(self):
        s = self.Session({"retCode": 0, "result": {"rows": [row()]}})
        self.assertEqual(len(bybit.internal_deposits("k", "s", session=s)), 1)

    def test_an_unknown_code_is_not_dressed_up_as_known(self):
        why, _fixable = bybit.explain(999999, "нечто")
        self.assertIn("999999", why)

    def test_a_wait_it_out_refusal_does_not_advise_fixing_it(self):
        """`explain` вторым значением отдаёт «можно ли починить руками» —
        совет не должен расходиться с делом."""
        self.assertFalse(bybit.explain(10006)[1])
        self.assertTrue(bybit.explain(10005)[1])

    def test_the_clock_refusal_is_not_confused_with_a_bad_key(self):
        """10002 и 10003 лечатся разными действиями, и назвать одно другим
        значит отправить владельца чинить не то."""
        clock, _ = bybit.explain(10002)
        self.assertIn("час", clock.lower())
        self.assertNotIn("ключ от другого", clock)


class TheMoneyIsCountedOnce(Bench):

    def test_a_payment_grants_the_tier_it_covers(self):
        got = self.feed(row(amount="5"))
        self.assertEqual([g["days"] for g in got], [30])
        # `subscription_days_left` округляет вниз: выданные ровно 30 дней
        # читаются как 29 через миг после выдачи.
        self.assertGreaterEqual(self.days_left(), 29)

    def test_the_same_payment_twice_grants_nothing_the_second_time(self):
        """Бот перезапустился, поступление прочиталось снова. Вернуть
        выданные дни нечем."""
        self.feed(row(tx="same"))
        before = self.days_left()
        again = self.feed(row(tx="same"))
        self.assertEqual(again, [])
        self.assertEqual(self.days_left(), before)

    def test_a_pending_transfer_is_not_counted(self):
        """`status: 1` — «ещё неизвестно», а не «почти успех»."""
        self.assertEqual(self.feed(row(status=1)), [])
        self.assertEqual(self.days_left(), 0)

    def test_a_failed_transfer_is_not_counted(self):
        self.assertEqual(self.feed(row(status=3)), [])

    def test_but_the_same_transfer_counts_once_it_succeeds(self):
        """Пропущенный по единице перевод не должен пропасть навсегда."""
        self.feed(row(tx="t", status=1))
        got = self.feed(row(tx="t", status=2))
        self.assertEqual([g["days"] for g in got], [30])

    def test_a_failed_notification_does_not_cost_a_second_grant(self):
        """Отметка о засчитанном переводе ставится ДО сообщений. Иначе
        продавец, заблокировавший бота, получал бы дни заново на каждом
        проходе — за одни и те же деньги."""
        class Deaf:
            async def send_message(self, *a, **kw):
                raise RuntimeError("бот заблокирован")

        deaf = Deaf()
        saved = billing.internal_deposits
        billing.internal_deposits = lambda *a, **k: [row(tx="t")]
        try:
            first = run(billing.collect(deaf))
            second = run(billing.collect(deaf))
        finally:
            billing.internal_deposits = saved
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "выдали второй раз за тот же перевод")

    def test_the_longest_affordable_tier_wins(self):
        """Прислал больше — значит хотел длиннее, а не переплатил за две
        недели."""
        self.assertEqual(storage.tier_for_amount(40), 365)
        self.assertEqual(storage.tier_for_amount(39.99), 30)
        self.assertEqual(storage.tier_for_amount(2.99), 0)


class MoneyThatArrivedIsNeverLost(Bench):

    def test_a_payment_from_an_unknown_uid_is_kept_not_dropped(self):
        """Продавец мог заплатить раньше, чем назвал UID."""
        self.feed(row(uid="777", amount="5"))
        self.assertIn("tx1", storage.unresolved_payments())

    def test_and_the_owner_is_told_about_it(self):
        self.feed(row(uid="777"))
        self.assertTrue(any("777" in t for t in self.told), self.told)

    def test_naming_the_uid_later_settles_it(self):
        """Ждать следующего перевода продавцу не за что."""
        self.feed(row(uid="777", amount="5"))
        other = 999
        self.assertTrue(storage.set_payer_uid(other, "777"))
        got = run(billing.settle_pending(self.bot, other, "777"))
        self.assertEqual([g["days"] for g in got], [30])
        self.assertGreaterEqual(storage.subscription_days_left(other), 29)
        self.assertNotIn("tx1", storage.unresolved_payments())

    def test_too_small_a_payment_is_kept_and_the_seller_is_told(self):
        self.feed(row(amount="1"))
        self.assertEqual(self.days_left(), 0)
        self.assertIn("tx1", storage.unresolved_payments())
        self.assertTrue(any(uid == self.SELLER for uid, _t in self.bot.said),
                        "продавцу не сказали, что денег не хватило")

    def test_the_wrong_coin_is_kept_and_not_counted(self):
        self.feed(row(coin="BTC", amount="5"))
        self.assertEqual(self.days_left(), 0)
        self.assertEqual(storage.unresolved_payments()["tx1"]["coin"], "BTC")

    def test_a_refusal_from_bybit_is_reported_not_swallowed(self):
        """«Оплата не засчиталась» и «мы не смогли спросить» снаружи
        выглядят одинаково, а лечатся по-разному."""
        saved = billing.internal_deposits

        def boom(*a, **k):
            raise bybit.BybitError("ключ не принят", 10003)

        billing.internal_deposits = boom
        try:
            self.assertEqual(run(billing.collect(self.bot)), [])
        finally:
            billing.internal_deposits = saved
        self.assertTrue(any("ключ не принят" in t for t in self.told), self.told)


class AUidBelongsToOneSeller(Bench):

    def test_a_uid_taken_by_someone_else_is_refused(self):
        """Иначе вписавший чужой UID забирал бы чужие оплаты, а
        пострадавший видел бы только, что деньги ушли, а подписки нет."""
        self.assertFalse(storage.set_payer_uid(999, self.UID))
        self.assertEqual(storage.payer_by_uid(self.UID), self.SELLER)

    def test_the_owner_of_the_uid_can_set_it_again(self):
        self.assertTrue(storage.set_payer_uid(self.SELLER, self.UID))

    def test_a_free_uid_is_taken(self):
        self.assertTrue(storage.set_payer_uid(999, "555"))
        self.assertEqual(storage.payer_by_uid("555"), 999)


class PayingIsOfferedOnlyWhenItCanWork(Bench):

    def test_everything_set_means_ready(self):
        self.assertTrue(storage.bybit_ready())
        self.assertEqual(storage.bybit_trouble(), "")

    def test_without_the_owners_uid_it_is_not_offered(self):
        """Кнопка «оплатить», за которой не задан UID, — обещание
        невозможного: нажмёт и увидит пустоту."""
        storage.set_bybit(uid="")
        self.assertFalse(storage.bybit_ready())
        self.assertIn("UID", storage.bybit_trouble())

    def test_without_a_key_it_is_not_offered(self):
        storage.set_bybit(key="")
        self.assertFalse(storage.bybit_ready())
        self.assertIn("ключ", storage.bybit_trouble())

    def test_without_a_usdt_price_it_is_not_offered(self):
        for days, _l in storage.PRICE_TIERS:
            storage.set_usdt_price(days, 0)
        self.assertFalse(storage.bybit_ready())
        self.assertIn("USDT", storage.bybit_trouble())

    def test_switched_off_says_so_rather_than_naming_a_missing_field(self):
        """Владелец всё задал и выключил — «не задан ключ» отправило бы
        его искать несуществующую поломку."""
        storage.set_bybit(enabled=False)
        self.assertIn("выключен", storage.bybit_trouble())

    def test_nothing_is_read_from_bybit_while_it_is_off(self):
        storage.set_bybit(enabled=False)
        self.assertEqual(self.feed(row()), [])


class TheKeyNeverReachesTheScreen(Bench):
    """Ключ на чтение показывает все поступления владельца, то есть его
    выручку. Экран админки пересылают в поддержку, снимают на видео и
    показывают через плечо — печатать там ключ нельзя ни целиком, ни
    началом, ни длиной."""

    def _admin_screen(self):
        import handlers.admin as A
        A.is_admin = lambda uid: True

        class Screen:
            def __init__(s):
                s.text = ""

            async def edit_text(s, text, reply_markup=None, **kw):
                s.text, s.markup = text, reply_markup

        class St:
            async def clear(s):
                return None

        cb = type("CB", (), {})()
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": 1})()

        async def answer(*a, **kw):
            return None

        cb.answer = answer
        run(A.bybit_menu(cb, St()))
        return cb.message.text

    def setUp(self):
        super().setUp()
        import handlers.admin as A
        self._is_admin = A.is_admin
        storage.set_bybit(key="SUPERSECRETKEY", secret="SUPERSECRETSECRET")

    def tearDown(self):
        import handlers.admin as A
        A.is_admin = self._is_admin
        super().tearDown()

    def test_neither_the_key_nor_the_secret_is_printed(self):
        text = self._admin_screen()
        self.assertNotIn("SUPERSECRETKEY", text)
        self.assertNotIn("SUPERSECRETSECRET", text)

    def test_not_even_the_first_few_characters(self):
        """«SUPER…» — это тоже сведения о ключе, а пользы на экране от них
        никакой."""
        text = self._admin_screen()
        self.assertNotIn("SUPER", text)

    def test_but_it_says_whether_a_key_is_set_at_all(self):
        """Иначе владелец не отличит «ключ не задан» от «задан и не
        работает», а лечится это по-разному."""
        self.assertIn("задан", self._admin_screen())
        storage.set_bybit(key="", secret="")
        self.assertIn("нет", self._admin_screen())

    def test_the_owners_uid_is_printed_because_sellers_need_it(self):
        storage.set_bybit(uid="777888")
        self.assertIn("777888", self._admin_screen())


class PayingIsOfferedOnlyWhereItWorks(Bench):

    def _access_buttons(self, uid=None):
        import handlers.start as S
        return [b.callback_data
                for row in S._access_kb(uid or self.SELLER).inline_keyboard
                for b in row]

    def test_the_button_appears_when_everything_is_set(self):
        self.assertIn("pay:menu", self._access_buttons())

    def test_and_is_gone_when_it_would_lead_nowhere(self):
        """Кнопка «оплатить», за которой не задан UID, — обещание
        невозможного: нажмёт и увидит пустоту."""
        storage.set_bybit(uid="")
        self.assertNotIn("pay:menu", self._access_buttons())

    def test_asking_the_owner_for_an_invoice_always_remains(self):
        """Оплата Bybit может быть выключена — способ заплатить обязан
        остаться в любом случае."""
        storage.set_bybit(enabled=False)
        self.assertIn("sub:order", self._access_buttons())

    def test_paying_is_not_locked_behind_the_subscription(self):
        """Заперев оплату подпиской, мы заперли бы единственный способ
        подписку купить."""
        import main as M
        for data in ("pay:menu", "pay:uid", "pay:check"):
            with self.subTest(data):
                self.assertIn(data, M.FREE_CALLBACKS)


class TheSellerIsToldWhatToDoBeforeHePays(Bench):

    def _screen(self, uid=None):
        import handlers.pay as P
        return P._screen(uid or self.SELLER)[0]

    def test_it_names_the_uid_to_send_to(self):
        storage.set_bybit(uid="777888")
        self.assertIn("777888", self._screen())

    def test_it_names_the_prices_in_usdt(self):
        text = self._screen()
        self.assertIn("USDT", text)
        self.assertIn("5", text)

    def test_a_seller_without_a_uid_is_warned_before_paying_not_after(self):
        """Сказать «я не пойму, что перевод от тебя» надо ДО перевода.
        После — это уже «деньги ушли, а подписки нет»."""
        text = self._screen(uid=999)
        self.assertIn("укажи свой UID", text)

    def test_a_seller_with_a_uid_is_told_we_are_waiting_for_it(self):
        text = self._screen()
        self.assertIn(self.UID, text)
        self.assertNotIn("укажи свой UID", text)

    def test_claiming_someone_elses_uid_is_refused_out_loud(self):
        """Проверка на уровне экрана, а не хранилища: вернуть False мало —
        продавец должен УВИДЕТЬ отказ, иначе он решит, что всё записалось,
        и будет ждать подписку за перевод, который уйдёт другому."""
        import handlers.pay as P

        said: list[str] = []

        class Msg:
            def __init__(s, text):
                s.text = text
                s.from_user = type("U", (), {"id": 999})()
                s.bot = None

            async def answer(s, text, **kw):
                said.append(text)

        class St:
            cleared = 0

            async def clear(s):
                St.cleared += 1

            async def set_state(s, x):
                return None

        run(P.pay_uid_save(Msg(self.UID), St()))
        self.assertTrue(any("другим продавцом" in t for t in said), said)
        self.assertEqual(storage.payer_by_uid(self.UID), self.SELLER,
                         "UID переписали на чужого")
        self.assertEqual(storage.get_payer_uid(999), "")

    def test_a_free_uid_is_accepted_and_confirmed(self):
        """Обратная сторона: отказ не должен доставаться тому, кто вписал
        свой собственный UID."""
        import handlers.pay as P
        said: list[str] = []

        class Msg:
            def __init__(s, text):
                s.text = text
                s.from_user = type("U", (), {"id": 999})()
                s.bot = None

            async def answer(s, text, **kw):
                said.append(text)

        class St:
            async def clear(s):
                return None

        run(P.pay_uid_save(Msg("555444"), St()))
        self.assertEqual(storage.get_payer_uid(999), "555444")
        self.assertTrue(any("Запомнил" in t for t in said), said)

    def test_the_screen_speaks_to_the_seller_as_ty(self):
        """Это экран бота, а не магазина: продавцу — на «ты»."""
        text = self._screen()
        for polite in ("Переведите", "Укажите", "вашего", "Ваш"):
            with self.subTest(polite):
                self.assertNotIn(polite, text)

if __name__ == "__main__":
    unittest.main()
