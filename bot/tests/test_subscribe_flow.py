"""Покупка подписки: срок → способ оплаты → «я оплатил».

Прежний экран предлагал «Прошу счёт» — то есть просил подождать, пока с
человеком свяжутся. Половина не дожидалась. Теперь он сам выбирает срок,
сам видит реквизиты и платит, не выходя из бота.

Проверяется то, что здесь стоит денег:

* **срок без цены не показывается** — «1 месяц — 0 ₽» читается как
  «бесплатно», а это обещание, за которое спросят;
* **способ без реквизитов не показывается** — кнопка, за которой пусто,
  это обещание невозможного;
* **исчезнувший тариф не роняет экран** — цену могли снять, пока человек
  смотрел на кнопку;
* **заявка называет, за что и чем заплачено** — иначе владелец
  переспрашивает, то есть возвращается ровно тот круг переписки, ради
  которого кнопку и делали.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.commands as C                              # noqa: E402
import handlers.start as S                                 # noqa: E402
import handlers.subscribe as SUB                           # noqa: E402
import logs                                                # noqa: E402
import storage                                             # noqa: E402


def run(coro):
    return asyncio.run(coro)


class St:
    async def clear(self):
        return None

    async def set_state(self, x):
        return None

    async def update_data(self, **kw):
        return None


class Screen:
    def __init__(self):
        self.text = ""
        self.markup = None
        self.sent: list[str] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text, self.markup = text, reply_markup

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(text)


class Bench(unittest.TestCase):
    UID = 4242

    def setUp(self):
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage.set_price(14, 249)
        storage.set_price(30, 499)
        self.sbp = storage.add_pay_method("СБП", "+7 900 000-00-00, Иван И.")
        self.logged: list[list[str]] = []
        self._log = logs.log_event

        async def fake_log(bot, kind, lines, user=None, markup=None):
            self.logged.append(list(lines))
            return True

        logs.log_event = fake_log

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        logs.log_event = self._log

    def tap(self, data, handler):
        cb = type("CB", (), {})()
        cb.data = data
        cb.bot = None
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": self.UID, "full_name": "Иван",
                                      "username": "ivan"})()
        cb.alerts: list[str] = []

        async def answer(text="", **kw):
            cb.alerts.append(text)

        cb.answer = answer
        run(handler(cb, St()))
        return cb

    def buttons(self, cb):
        return [(b.text, b.callback_data)
                for row in cb.message.markup.inline_keyboard for b in row]


class TheAccessScreenOffersTwoThingsAndNoMore(Bench):
    """Раньше здесь лежали обе пробы, «Прошу счёт» и «Я оплатил» — четыре
    кнопки, из которых две про одно. Человек решает не «какой кнопкой», а
    «платить или попробовать»."""

    def _data(self, uid=None):
        return [b.callback_data
                for row in S._access_kb(uid or self.UID).inline_keyboard
                for b in row]

    def test_it_offers_paying_and_taking_it_free(self):
        self.assertEqual(self._data(), ["sub:buy", "trial:menu", "start:hello"])

    def test_asking_for_an_invoice_is_gone(self):
        """«Прошу счёт» просил подождать, пока свяжутся."""
        self.assertNotIn("sub:order", self._data())

    def test_and_so_is_saying_you_paid_before_you_could(self):
        """«Я оплатил» до выбора срока — заявка, по которой владельцу
        нечего проверять."""
        self.assertNotIn("pay:paid", self._data())


class ChoosingTheTerm(Bench):

    def test_only_priced_terms_are_offered(self):
        cb = self.tap("sub:buy", SUB.choose_term)
        data = [d for _t, d in self.buttons(cb) if d.startswith("sub:buy:")]
        self.assertEqual(data, ["sub:buy:14", "sub:buy:30"])

    def test_the_price_is_on_the_button_itself(self):
        """Цена, спрятанная за нажатием, заставляет тыкать наугад."""
        cb = self.tap("sub:buy", SUB.choose_term)
        texts = [t for t, d in self.buttons(cb) if d == "sub:buy:30"]
        self.assertIn("499", texts[0])

    def test_with_no_prices_it_says_so_instead_of_showing_nothing(self):
        for days, _l in storage.PRICE_TIERS:
            storage.set_price(days, 0)
        cb = self.tap("sub:buy", SUB.choose_term)
        self.assertIn("не назначены", cb.message.text)
        self.assertIn(storage.get_support_contact(), cb.message.text)

    def test_each_term_gets_its_own_row(self):
        """«2 недели» и «12 месяцев» рядом читаются как один тариф, а
        промах пальцем здесь стоит денег."""
        cb = self.tap("sub:buy", SUB.choose_term)
        for row in cb.message.markup.inline_keyboard:
            data = [b.callback_data for b in row]
            if any(d.startswith("sub:buy:") for d in data):
                self.assertEqual(len(row), 1, data)


class ChoosingTheMethod(Bench):

    def test_the_term_and_its_price_are_repeated(self):
        """Человек выбрал срок два экрана назад — на реквизитах он уже не
        помнит, сколько платит."""
        cb = self.tap("sub:buy:30", SUB.choose_method)
        self.assertIn("1 месяц", cb.message.text)
        self.assertIn("499", cb.message.text)

    def test_a_method_without_details_is_not_offered(self):
        """Кнопка, за которой пусто, — обещание невозможного."""
        storage.add_pay_method("Пустой", "")
        cb = self.tap("sub:buy:30", SUB.choose_method)
        self.assertNotIn("Пустой", [t for t, _d in self.buttons(cb)])

    def test_with_no_methods_at_all_it_says_where_to_write(self):
        storage.del_pay_method(self.sbp)
        cb = self.tap("sub:buy:30", SUB.choose_method)
        self.assertIn(storage.get_support_contact(), cb.message.text)

    def test_a_term_whose_price_vanished_does_not_break_the_screen(self):
        """Цену могли снять, пока человек смотрел на кнопку. Молча вернуть
        его назад — значит оставить с ощущением, что бот сломался."""
        storage.set_price(30, 0)
        cb = self.tap("sub:buy:30", SUB.choose_method)
        self.assertTrue(any("не продаётся" in a for a in cb.alerts), cb.alerts)
        self.assertIn("Оплатить подписку", cb.message.text)


class SeeingTheDetails(Bench):

    def test_the_details_are_shown_in_full(self):
        cb = self.tap(f"sub:m:30:{self.sbp}", SUB.show_details)
        self.assertIn("+7 900 000-00-00", cb.message.text)

    def test_the_amount_is_repeated_next_to_them(self):
        cb = self.tap(f"sub:m:30:{self.sbp}", SUB.show_details)
        self.assertIn("499", cb.message.text)
        self.assertIn("1 месяц", cb.message.text)

    def test_it_does_not_pretend_the_bot_checks_the_payment(self):
        """Проверять нечем: оплата идёт вне бота."""
        cb = self.tap(f"sub:m:30:{self.sbp}", SUB.show_details)
        self.assertIn("вручную", cb.message.text)
        self.assertNotIn("проверю оплату", cb.message.text.lower())

    def test_the_paid_button_carries_the_term_and_the_method(self):
        cb = self.tap(f"sub:m:30:{self.sbp}", SUB.show_details)
        data = [d for _t, d in self.buttons(cb) if d.startswith("pay:paid")]
        self.assertEqual(data, [f"pay:paid:30:{self.sbp}"])

    def test_a_deleted_method_does_not_break_the_screen(self):
        storage.del_pay_method(self.sbp)
        cb = self.tap(f"sub:m:30:{self.sbp}", SUB.show_details)
        self.assertTrue(any("не доступен" in a for a in cb.alerts), cb.alerts)


class TheDetailsAreTappableToCopy(Bench):
    """Продавец переписывает номер карты руками с экрана телефона — и
    ошибается цифрой. Обёрнутое в `<code>` Telegram рисует моноширинным и
    копирует по нажатию целиком.

    «Целиком» здесь и есть загвоздка: обернуть весь текст было бы проще, но
    тогда вместе с картой скопировалось бы «в комментарии — свой ник», и
    это уехало бы в поле перевода.
    """

    def _details(self, text):
        storage.del_pay_method(self.sbp)
        mid = storage.add_pay_method("Карта", text)
        cb = self.tap(f"sub:m:30:{mid}", SUB.show_details)
        return cb.message.text

    def test_the_card_number_is_wrapped_for_copying(self):
        got = self._details("2204 3204 9541 4437, Иван И.")
        self.assertIn("<code>2204 3204 9541 4437</code>", got)

    def test_but_the_instruction_next_to_it_is_not(self):
        """Скопированное «в комментарии — свой ник» уедет в поле перевода и
        останется там."""
        got = self._details("2204 3204 9541 4437, Иван И.\\n"
                            "В комментарии — свой ник")
        self.assertNotIn("<code>В комментарии", got)
        self.assertIn("В комментарии — свой ник", got)

    def test_a_phone_keeps_its_plus(self):
        """Скопированный «7 900…» без плюса в поле перевода не годится."""
        got = self._details("+7 900 000-00-00 (СБП)")
        self.assertIn("<code>+7 900 000-00-00</code>", got)

    def test_a_wallet_address_is_wrapped_whole(self):
        got = self._details("USDT TRC20: TXk9aBcDeFgHiJkLmNoPqRsTuVwXyZ1234")
        self.assertIn("<code>TXk9aBcDeFgHiJkLmNoPqRsTuVwXyZ1234</code>", got)

    def test_an_address_starting_with_digits_is_not_bitten_in_half(self):
        """Нажатие обязано скопировать адрес целиком. Скопированная
        половина кошелька — это деньги, ушедшие в никуда.

        Держит это `(?![\\w/])` в цифровом правиле: за цифрами не должно
        идти буквы. Порядок правил здесь ни при чём — проверено мутацией,
        перестановка ничего не меняет."""
        got = self._details("TON: 1234567890abcdefghijklmnop")
        self.assertIn("<code>1234567890abcdefghijklmnop</code>", got)

    def test_plain_words_are_left_alone(self):
        """Лишний `<code>` вокруг фразы хуже, чем его отсутствие."""
        got = self._details("Напиши мне, договоримся")
        self.assertNotIn("<code>", got.split("К оплате")[-1])

    def test_a_short_number_is_not_mistaken_for_a_requisite(self):
        """«Оплата до 5 числа» — не реквизит, и копировать там нечего."""
        got = self._details("Оплата до 5 числа, срок 30 дней")
        self.assertNotIn("<code>5</code>", got)
        self.assertNotIn("<code>30</code>", got)

    def test_the_details_are_still_escaped(self):
        """Одиночный `<` в реквизитах роняет отправку целиком, и продавец
        не получает ничего."""
        got = self._details("Карта <2204> & Ко")
        self.assertNotIn("<2204>", got)
        self.assertIn("&amp;", got)

class TheClaimSaysWhatWasPaidFor(Bench):

    def _claim(self, data):
        cb = type("CB", (), {})()
        cb.data = data
        cb.bot = None
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": self.UID, "full_name": "Иван",
                                      "username": "ivan"})()

        async def answer(text="", **kw):
            return None

        cb.answer = answer
        run(C.paid_claim(cb))
        return " ".join(self.logged[-1]) if self.logged else ""

    def test_the_owner_sees_the_term_the_price_and_the_method(self):
        said = self._claim(f"pay:paid:30:{self.sbp}")
        self.assertIn("1 месяц", said)
        self.assertIn("499", said)
        self.assertIn("СБП", said)

    def test_a_bare_claim_still_reaches_the_owner(self):
        """Кнопку могли нажать со старого сообщения, без хвоста. Заявка
        важнее подробностей — терять её из-за них нельзя."""
        said = self._claim("pay:paid")
        self.assertIn("оплатил", said)

    def test_and_so_does_one_whose_method_was_deleted(self):
        storage.del_pay_method(self.sbp)
        said = self._claim(f"pay:paid:30:{self.sbp}")
        self.assertIn("оплатил", said)
        self.assertIn("1 месяц", said)


class TheFreeScreenKeepsBothTrialsApart(Bench):

    def test_a_newcomer_is_offered_both(self):
        storage.set_trial_channel("@ch")
        cb = self.tap("trial:menu", SUB.free_menu)
        data = [d for _t, d in self.buttons(cb)]
        self.assertIn("trial:free", data)
        self.assertIn("trial:offer", data)

    def test_having_taken_both_is_said_out_loud(self):
        """Пустой экран без объяснения читается как поломка."""
        storage.set_trial_channel("@ch")
        storage.note_trial(self.UID, "free")
        storage.note_trial(self.UID, "channel")
        cb = self.tap("trial:menu", SUB.free_menu)
        self.assertIn("уже брал", cb.message.text)
        self.assertEqual([d for _t, d in self.buttons(cb)], ["access:menu"])


if __name__ == "__main__":
    unittest.main()
