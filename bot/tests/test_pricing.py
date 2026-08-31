"""Тарифы по срокам и пробный период.

Документы обещают градацию цен со скидкой за длинный срок и бесплатные три
дня. До этого в коде не было ни того, ни другого: цена была одним числом, а
пробного периода не существовало вовсе. Обещание в опубликованном
документе, которого код не выполняет, — ложь в том, на что сошлются.

Два места здесь особенно скользкие.

Скидка: приписать «−20%» к тарифу, который выгоды не даёт, — враньё,
которое клиент проверит за десять секунд.

Отметка о выданной пробе: она обязана пережить `/forget_me`, иначе удаление
данных превращается в способ брать три дня бесконечно. И наоборот — она не
должна мешать тому, кто пробу не брал.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402


class Admin(unittest.TestCase):
    """Общее хранилище — словарь в памяти, с откатом в tearDown."""

    def setUp(self):
        self.admin: dict = {}
        self.blobs: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._read, self._write = storage._read_blob, storage._write_blob
        self._owner = storage.is_owner
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage._read_blob = lambda n: (self.admin if n == "admin"
                                        else self.blobs.setdefault(n, {}))
        storage._write_blob = lambda n, d: (self.admin.update(d) if n == "admin"
                                            else self.blobs.__setitem__(n, d))
        storage.is_owner = lambda uid: False

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage._read_blob, storage._write_blob = self._read, self._write
        storage.is_owner = self._owner

    def set_all(self):
        for days, price in ((30, 990), (90, 2490), (180, 4490), (365, 7900)):
            storage.set_price(days, price)


class PricesAreShownByTerm(Admin):

    def test_with_nothing_set_the_client_is_shown_no_price_at_all(self):
        # «0 ₽» на экране читается как «бесплатно» — это обещание.
        self.assertEqual(storage.get_prices(), {})
        self.assertEqual(storage.price_lines(), [])

    def test_every_term_that_was_set_is_offered(self):
        self.set_all()
        shown = "\n".join(storage.price_lines())
        for label in ("1 месяц", "3 месяца", "6 месяцев", "12 месяцев"):
            self.assertIn(label, shown)

    def test_a_term_without_a_price_is_simply_absent(self):
        storage.set_price(30, 990)
        storage.set_price(365, 7900)
        shown = "\n".join(storage.price_lines())
        self.assertIn("1 месяц", shown)
        self.assertNotIn("3 месяца", shown)

    def test_zero_removes_a_term_rather_than_pricing_it_at_nothing(self):
        storage.set_price(30, 990)
        storage.set_price(30, 0)
        self.assertEqual(storage.get_prices(), {})

    def test_a_negative_price_is_not_stored(self):
        storage.set_price(30, -100)
        self.assertEqual(storage.get_prices(), {})

    def test_rubbish_in_storage_does_not_crash_the_screen(self):
        # Хранилище — правимый JSON.
        self.admin["prices"] = {"30": "дёшево", "90": 2490}
        self.assertEqual(storage.get_prices(), {90: 2490})

    def test_the_old_single_price_still_works_for_installs_that_had_one(self):
        self.admin["bot_price"] = 500
        self.assertIn("500", "\n".join(storage.price_lines()))


class TheDiscountIsOnlyClaimedWhereItExists(Admin):

    def test_a_longer_term_that_is_cheaper_per_month_says_so(self):
        self.set_all()
        year = [l for l in storage.price_lines() if "12 месяцев" in l][0]
        self.assertIn("−", year)

    def test_a_term_with_no_saving_is_not_advertised_as_one(self):
        # 3 месяца по цене трёх месяцев — это не скидка.
        storage.set_price(30, 1000)
        storage.set_price(90, 3000)
        quarter = [l for l in storage.price_lines() if "3 месяца" in l][0]
        self.assertNotIn("−", quarter)

    def test_a_term_that_costs_more_is_not_advertised_as_a_saving(self):
        storage.set_price(30, 1000)
        storage.set_price(90, 3600)
        quarter = [l for l in storage.price_lines() if "3 месяца" in l][0]
        self.assertNotIn("−", quarter)

    def test_the_monthly_term_never_advertises_a_discount_against_itself(self):
        self.set_all()
        month = [l for l in storage.price_lines() if "1 месяц" in l][0]
        self.assertNotIn("−", month)

    def test_the_percentage_is_the_real_one(self):
        # 12 месяцев по 6000 против 1000 в месяц — это ровно вдвое дешевле.
        storage.set_price(30, 1000)
        storage.set_price(365, 6083)      # 12 167 ₽ за год по месячной цене
        year = [l for l in storage.price_lines() if "12 месяцев" in l][0]
        self.assertIn("−50%", year)


class TheTrialIsGivenOnceAndOnlyOnce(Admin):

    def test_a_new_seller_gets_the_trial(self):
        self.assertEqual(storage.start_trial(7),
                         storage.TRIAL_DAYS_DEFAULT)
        self.assertTrue(storage.has_active_subscription(7))

    def test_the_same_seller_does_not_get_it_twice(self):
        storage.start_trial(7)
        self.assertEqual(storage.start_trial(7), 0)

    def test_a_second_attempt_does_not_extend_the_first(self):
        storage.start_trial(7)
        first = storage.get_subscription(7)["expires"]
        storage.start_trial(7)
        self.assertEqual(storage.get_subscription(7)["expires"], first)

    def test_with_the_trial_switched_off_nobody_gets_anything(self):
        # Ноль дней означает «выключен», а не «выдать ноль дней»: иначе бот
        # объявлял бы пробный период и не давал доступа.
        storage.set_trial_days(0)
        self.assertEqual(storage.start_trial(7), 0)
        self.assertFalse(storage.has_active_subscription(7))

    def test_a_switched_off_trial_does_not_burn_the_right_to_it(self):
        """Худший из здешних исходов, и он тихий.

        Отметка «уже брал», поставленная тому, кто ничего не получил,
        отбирает пробный период навсегда: владелец выключил его на день,
        человек зашёл, отметку получил, — и когда пробу включат обратно,
        ему уже не положено. Узнать об этом неоткуда ни ему, ни владельцу.
        """
        storage.set_trial_days(0)
        storage.start_trial(7)
        self.assertFalse(storage.trial_used(7))

        # Здесь число задано нарочно и со сроком по умолчанию не связано:
        # проверяется, что право на пробу уцелело, а не сколько дней в ней.
        storage.set_trial_days(3)
        self.assertEqual(storage.start_trial(7), 3)

    def test_a_paying_seller_is_not_given_a_trial_on_top(self):
        # `by` обязателен: именно он отличает оплаченную подписку от
        # пробной. Без него запись неотличима от пробы, и проба поверх
        # пробы — законная (три дня плюс семь), а поверх оплаты — нет.
        storage.grant_subscription(7, 30, by=storage.OWNER_ID)
        self.assertEqual(storage.start_trial(7), 0)

    def test_a_seller_who_never_took_it_still_can_after_someone_else_did(self):
        storage.start_trial(7)
        self.assertEqual(storage.start_trial(8),
                         storage.TRIAL_DAYS_DEFAULT)

    def test_the_length_follows_the_setting(self):
        storage.set_trial_days(14)
        self.assertEqual(storage.start_trial(7), 14)
        # `subscription_days_left` округляет вниз, поэтому сразу после
        # выдачи показывает 13: остаток чуть меньше полных четырнадцати
        # суток. Занижение безопаснее завышения, менять не стали — здесь
        # проверяется сам срок, а не как он округлён.
        left = storage.get_subscription(7)["expires"] - time.time()
        self.assertAlmostEqual(left / 86400, 14, places=2)

    def test_a_negative_setting_is_read_as_switched_off(self):
        storage.set_trial_days(-5)
        self.assertEqual(storage.get_trial_days(), 0)


class DeletingDataIsNotAWayToTakeTheTrialAgain(Admin):

    def test_the_mark_survives_full_deletion(self):
        storage.start_trial(7)
        storage.purge_user(7)
        self.assertTrue(storage.trial_used(7))
        self.assertEqual(storage.start_trial(7), 0)

    def test_everything_else_still_goes(self):
        # Оговорка про пробу не должна превратиться в «ничего не удаляем».
        self.blobs["settings"] = {"7": {"личное": True}}
        storage.start_trial(7)
        storage.purge_user(7)
        self.assertEqual(self.blobs["settings"], {})
        self.assertNotIn("7", self.admin.get("subscriptions", {}))

    def test_someone_who_never_took_the_trial_is_not_marked_by_deletion(self):
        storage.purge_user(7)
        self.assertFalse(storage.trial_used(7))
        self.assertEqual(storage.start_trial(7),
                         storage.TRIAL_DAYS_DEFAULT)

    def test_the_documents_say_this_out_loud(self):
        # Данные, тайно оставленные после «полного удаления», — ложь в
        # опубликованном документе.
        legal = Path(__file__).resolve().parents[2] / "docs" / "legal"

        def flat(name: str) -> str:
            # Перенос строки в середине фразы — не повод считать, что её
            # в документе нет.
            return " ".join((legal / name).read_text().lower().split())

        privacy = flat("privacy.md")
        self.assertIn("пробный период", privacy)
        self.assertIn("не удаляются только две записи", privacy)
        self.assertIn("не удаляется", flat("terms.md"))


class TheAdminCanSetAllOfIt(unittest.TestCase):

    def test_the_panel_offers_tiers_and_the_trial(self):
        src = (Path(__file__).resolve().parents[1]
               / "handlers" / "admin.py").read_text()
        self.assertIn('callback_data="admin:prices"', src)
        self.assertIn('callback_data="admin:trial"', src)

    def test_the_superseded_single_price_screen_is_gone(self):
        # Экран, до которого не ведёт ни одна кнопка, — мёртвый код,
        # который следующая сессия примет за рабочий.
        import handlers.admin as A
        names = [h.callback.__name__ for h in A.router.callback_query.handlers]
        self.assertNotIn("price_start", names)

    def test_the_subscription_screen_shows_the_tiers(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        self.assertIn("price_lines", src)

    def test_the_trial_is_handed_out_only_by_its_own_buttons(self):
        """Проверяется ФУНКЦИЯ, а не наличие строки в файле.

        Первая версия этого теста искала «start_trial» по всему файлу — и
        прошла бы, когда выдача пробы по ошибке оказалась в экране помощи
        «❓ Не нахожу токен». Там она падала с NameError на каждое нажатие,
        а тест бы этого не заметил.

        Раньше проба выдавалась молча из `/start`. Теперь их две — три дня
        без условий и неделя за подписку, — и обе даются по нажатию: молчаливая
        выдача обесценивала условие, а выбор между сроками делала за
        человека. Значит и мест выдачи ровно два, по кнопке на каждое.
        """
        import ast
        src = (Path(__file__).resolve().parents[1]
               / "handlers" / "start.py").read_text()
        where = {n.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and "start_trial" in ast.dump(n)}
        self.assertEqual(where, {"trial_free"},
                         f"проба выдаётся не только своей кнопкой: {where}")


class TwoWeeksIsTheEntryTier(unittest.TestCase):
    """Две недели — точка входа. После десяти бесплатных дней (три просто
    так плюс семь за подписку) человек решает не «стоит ли», а «продлить
    ли», и самый короткий платный срок должен быть рядом с этим решением.
    """

    def test_two_weeks_is_among_the_tiers(self):
        self.assertIn(14, [days for days, _label in storage.PRICE_TIERS])

    def test_it_is_the_shortest_one(self):
        self.assertEqual(storage.PRICE_TIERS[0][0], 14)

    def test_the_documents_list_it(self):
        """Документ, перечисляющий не те сроки, что показывает бот, хуже
        отсутствующего."""
        docs = pathlib.Path(storage.__file__).parents[1] / "docs" / "legal"
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                self.assertIn("2 недели", (docs / name).read_text())

    def test_no_discount_is_claimed_on_the_week(self):
        """Неделя дороже всех в пересчёте на день. Приписка «выгоднее» к
        ней была бы враньём, которое клиент проверит за десять секунд."""
        admin = storage._load_admin()
        try:
            for days, price in ((14, 249), (30, 449), (365, 3990)):
                storage.set_price(days, price)
            week = [ln for ln in storage.price_lines() if "недели" in ln][0]
            self.assertNotIn("−", week)
            self.assertNotIn("%", week)
        finally:
            storage._save_admin(admin)

    def test_a_longer_tier_still_shows_its_discount(self):
        """Обратная сторона: скидка на длинных сроках не должна пропасть
        из-за появления недели."""
        admin = storage._load_admin()
        try:
            for days, price in ((14, 249), (30, 449), (365, 3990)):
                storage.set_price(days, price)
            year = [ln for ln in storage.price_lines() if "12" in ln][0]
            self.assertIn("%", year)
        finally:
            storage._save_admin(admin)

    def test_the_entry_tier_can_actually_be_granted(self):
        """Тариф, который нельзя выдать, — строка на экране и больше
        ничего."""
        admin = storage._load_admin()
        try:
            storage.grant_subscription(4242, 14)
            self.assertTrue(storage.has_active_subscription(4242))
            self.assertLessEqual(storage.subscription_days_left(4242), 14)
        finally:
            storage._save_admin(admin)


class TheAccessScreenShowsOnlyRealPrices(Admin):
    """Экран «🚀 Получить доступ»: сколько стоит и что можно взять даром.

    Раньше тарифы жили на отдельном экране «💳 Тарифы», а кнопки проб — на
    витрине. Нажавший «Получить доступ» обязан увидеть всё сразу: и
    бесплатные дни, и цены — иначе он выбирает, не зная, из чего.

    Тариф без назначенной цены сюда не попадает: строка «1 месяц — 0 ₽»
    читается как «бесплатно», и это обещание, за которое спросят.
    """

    def setUp(self):
        super().setUp()
        import handlers.start as S
        self.S = S
        storage.set_trial_channel("@ch")

    def _open(self, uid=4242):
        import asyncio

        class Cb:
            def __init__(self, uid):
                self.from_user = type("U", (), {"id": uid})()
                self.message = self
                self.text = ""
                self.markup = None

            async def edit_text(self, text, reply_markup=None, **kw):
                self.text, self.markup = text, reply_markup

            async def answer(self, *a, **kw):
                return None

        cb = Cb(uid)
        asyncio.run(self.S.show_access(cb))
        return cb

    def test_a_priced_tier_is_named_with_its_price(self):
        storage.set_price(14, 249)
        self.assertIn("249", self._open().text)

    def test_an_unpriced_tier_is_not_listed(self):
        """«1 месяц — 0 ₽» читается как «бесплатно»."""
        storage.set_price(14, 249)
        text = self._open().text
        self.assertNotIn("1 месяц", text)
        self.assertNotIn("0 ₽", text)

    def test_no_prices_at_all_is_explained_and_not_left_blank(self):
        """Пустое место под заголовком «По подписке» читается как поломка
        бота, а не как «цены ещё не назначены»."""
        text = self._open().text
        self.assertIn("не назначены", text)

    def test_the_free_days_are_named_with_real_numbers(self):
        storage.set_trial_free_days(3)
        storage.set_trial_days(7)
        text = self._open().text
        self.assertIn("3 дня", text)
        self.assertIn("7 дней", text)

    def test_a_used_up_trial_is_not_advertised_in_the_text(self):
        """Строка «3 дня — просто так» тому, кто их уже брал, — обещание
        невозможного, и кнопку мы для него уже прячем."""
        storage.note_trial(4242, "free")
        text = self._open().text
        self.assertNotIn("просто так", text)
        self.assertIn("за подписку на канал", text)

    def test_the_channel_offer_is_not_advertised_without_a_channel(self):
        """Кнопку «за подписку» без заданного канала мы прячем, а текст
        обещал бы дальше: способ, которого нет. Ровно такое расхождение
        кнопки и текста и ловится здесь."""
        storage.set_trial_channel("")
        text = self._open().text
        self.assertNotIn("за подписку на канал", text)
        self.assertIn("просто так", text, "заодно потеряли и короткую пробу")

    def test_paying_is_offered_even_when_nothing_is_free_anymore(self):
        storage.note_trial(4242, "free")
        storage.note_trial(4242, "channel")
        storage.set_price(14, 249)
        cb = self._open()
        data = [b.callback_data for row in cb.markup.inline_keyboard
                for b in row]
        self.assertIn("sub:order", data)
        self.assertNotIn("Бесплатно", cb.text)


if __name__ == "__main__":
    unittest.main()
