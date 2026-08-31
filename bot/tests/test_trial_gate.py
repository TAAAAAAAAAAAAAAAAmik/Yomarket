"""Неделя бесплатно за подписку на канал.

Проверка одна — `getChatMember` у Telegram, — но ответов у неё ТРИ, и
путать их дорого:

* подписан — выдаём;
* не подписан — отказываем и говорим, что делать;
* **проверить не вышло — это НЕ отказ.**

Третий случай и есть главное. Бот не админ канала, канал переехал, владелец
вписал не тот адрес — всё это ошибки на нашей стороне, а выглядят они как
«ты не подписан». Продавец, которого отфутболили за чужую поломку, второй
раз не придёт, и владелец об этом не узнает: экран-то отвечает уверенно.

Здесь проверяется, что сорванная проверка приводит к ВЫДАЧЕ и жалобе в
журнал, а не к отказу.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                            # noqa: E402
import trialgate                                          # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Member:
    def __init__(self, status, is_member=False):
        self.status, self.is_member = status, is_member


class Bot:
    """Telegram, отвечающий заданным состоянием — или отказом."""

    def __init__(self, result):
        self.result = result

    async def get_chat_member(self, chat, uid):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Bench(unittest.TestCase):
    UID = 424242

    def setUp(self):
        self._admin = dict(storage._load_admin())
        storage.set_trial_channel("@yoomarket")
        storage.set_trial_days(7)
        # Отметка о выданной пробе не удаляется вместе с данными, поэтому
        # чистим её руками — иначе второй тест ничего не проверит.
        data = storage._load_admin()
        data["trials"] = [x for x in data.get("trials", [])
                          if int(x) != self.UID]
        storage._save_admin(data)
        self._subs = dict(storage._load_admin().get("subscriptions", {}))

    def tearDown(self):
        storage._save_admin(self._admin)


class TheWeekIsGivenForASubscription(Bench):
    def test_a_subscriber_gets_the_week(self):
        days, why = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 7)
        self.assertEqual(why, "")

    def test_a_channel_admin_counts_as_subscribed(self):
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("administrator")), self.UID))
        self.assertEqual(days, 7)

    def test_a_restricted_member_still_counts(self):
        """Ограниченный участник — всё ещё участник канала."""
        ok, why = run(trialgate.check_member(
            Bot(Member("restricted", True)), "@ch", 1))
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_someone_who_left_does_not(self):
        ok, why = run(trialgate.check_member(
            Bot(Member("restricted", False)), "@ch", 1))
        self.assertFalse(ok)
        self.assertEqual(why, "", "выход из канала — это отказ, а не сбой")


class NotSubscribedIsARefusalNotAFailure(Bench):
    """Отказ обязан отличаться от сбоя: у них разные ответы продавцу."""

    def test_a_non_subscriber_gets_nothing(self):
        days, why = run(trialgate.grant_for_subscription(
            Bot(Member("left")), self.UID))
        self.assertEqual(days, 0)
        self.assertEqual(why, "", "отказ выдал себя за сбой")

    def test_a_refusal_does_not_burn_the_trial(self):
        """Иначе одна попытка до подписки стоила бы человеку недели —
        навсегда, потому что отметка не удаляется."""
        run(trialgate.grant_for_subscription(Bot(Member("left")), self.UID))
        self.assertFalse(storage.trial_used(self.UID))
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 7)


class AFailedCheckNeverRefuses(Bench):
    """Продавца нельзя наказывать за нашу поломку."""

    def test_an_unreachable_channel_still_grants(self):
        days, why = run(trialgate.grant_for_subscription(
            Bot(Exception("bot is not a member of the channel")), self.UID))
        self.assertEqual(days, 7, "отказали за то, что бот не админ канала")
        self.assertIn("not a member", why)

    def test_the_reason_comes_back_for_the_journal(self):
        """Молча выдать — значит оставить владельца с неработающим условием
        и без единого признака этого."""
        _days, why = run(trialgate.grant_for_subscription(
            Bot(Exception("chat not found")), self.UID))
        self.assertTrue(why, "причина сбоя потерялась")

    def test_no_channel_means_no_condition(self):
        storage.set_trial_channel("")
        days, why = run(trialgate.grant_for_subscription(
            Bot(Exception("не должно спрашиваться")), self.UID))
        self.assertEqual(days, 7)
        self.assertEqual(why, "")


class TheWeekIsGivenOnlyOnce(Bench):
    def test_a_second_attempt_gives_nothing(self):
        run(trialgate.grant_for_subscription(Bot(Member("member")), self.UID))
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 0)

    def test_the_mark_survives_data_deletion(self):
        """Иначе `/forget_me` стал бы способом брать неделю бесконечно."""
        run(trialgate.grant_for_subscription(Bot(Member("member")), self.UID))
        storage.purge_user(self.UID)
        self.assertTrue(storage.trial_used(self.UID, "channel"))

    def test_a_switched_off_trial_does_not_burn_the_right_to_it(self):
        storage.set_trial_days(0)
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 0)
        self.assertFalse(storage.trial_used(self.UID),
                         "отметили выдачу, ничего не выдав")


class ItIsAWeekAndTheDocumentsSaySo(unittest.TestCase):
    """Документ, обещающий не то, что делает код, хуже отсутствующего."""

    DOCS = pathlib.Path(storage.__file__).parents[1] / "docs" / "legal"

    def test_the_default_is_a_week(self):
        self.assertEqual(storage.TRIAL_DAYS_DEFAULT, 7)

    def _flat(self, name: str) -> str:
        """Текст документа одной строкой.

        Искать точную подстроку в размеченном тексте нельзя: перенос строки
        посреди фразы ломает проверку, ничего не сказав о самом обещании.
        Первая версия этих тестов падала именно так — на переносе внутри
        «7 календарных дней».
        """
        import re
        return re.sub(r"\s+", " ", (self.DOCS / name).read_text())

    def test_the_offer_promises_the_same_number(self):
        self.assertIn("7 календарных дней", self._flat("offer.md"))

    def test_the_terms_promise_the_same_number(self):
        self.assertIn("7 календарных дней", self._flat("terms.md"))

    def test_the_documents_describe_both_variants(self):
        """Документ, обещающий один вариант там, где их два, — половина
        правды: спросят по нему целиком."""
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                text = self._flat(name)
                self.assertIn("3 календарных", text)
                self.assertIn("двух частях", text)
                self.assertIn("суммируются", text)

    def test_the_documents_say_each_part_is_given_once(self):
        """Не сказать «каждая однократно» значит обещать пробу без конца
        тому, кто нажимает кнопку повторно."""
        self.assertIn("каждая — однократно", self._flat("offer.md"))

    def test_both_mention_the_channel_condition(self):
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                # Падеж у слова разный в разных фразах — проверяем корень.
                self.assertIn("подписки на Telegram-канал", self._flat(name))

    def test_neither_still_says_it_is_given_at_first_start(self):
        """Прежнее обещание «при первом запуске» стало неверным: теперь
        сначала условие."""
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                self.assertNotIn("при первом запуске Бота",
                                 self._flat(name))


class TheTwoKindsOfSubscriptionAreNeverConfused(unittest.TestCase):
    """В боте два разных «подписка», и путать их дорого.

    Первое — ПЛАТНЫЙ доступ к боту и его функциям. Второе — подписка на
    канал, которая всего лишь открывает бесплатную неделю и к оплате
    отношения не имеет.

    На этом запнулся сам владелец, читая экран «Канал для подписки»: он
    прочитался как «канал, где продаётся подписка». Если сбивает того, кто
    задумывал функцию, продавца собьёт тем более.
    """

    HANDLERS = pathlib.Path(storage.__file__).parent / "handlers"

    def test_the_channel_setting_is_not_called_a_subscription(self):
        src = (self.HANDLERS / "admin.py").read_text()
        self.assertNotIn("Канал для подписки", src,
                         "читается как «канал, где продаётся подписка»")

    def test_the_trial_screen_says_it_is_free_access_to_the_bot(self):
        src = (self.HANDLERS / "admin.py").read_text()
        self.assertIn("не путать с", src,
                      "экран не разводит платный доступ и бесплатную пробу")

    def test_the_paywall_names_what_is_being_bought(self):
        """«Нужна подписка» не отвечает на вопрос «подписка на что»."""
        text = storage.CUSTOM_TEXTS["subscription"]["default"]
        self.assertIn("боту", text)

    def test_the_paywall_does_not_mention_any_channel(self):
        """Канал к оплате отношения не имеет — упомянуть его здесь значит
        пообещать, что подписка на канал откроет платные функции."""
        text = storage.CUSTOM_TEXTS["subscription"]["default"]
        self.assertNotIn("канал", text.lower())

    def test_the_trial_offer_asks_for_the_channel_not_for_money(self):
        import handlers.start as S
        storage.set_trial_channel("@ch")
        try:
            text = S._trial_offer_text()
        finally:
            storage.set_trial_channel("")
        self.assertIn("канал", text.lower())
        self.assertNotIn("оплат", text.lower().replace("без оплаты", ""))


class TwoTrialsThatAddUp(Bench):
    """Проб две, и берутся ОБЕ: три дня просто так, потом ещё семь за
    подписку на канал. Вместе — десять дней.

    Отметок тоже две, по одной на вид. Одна общая означала бы «взял три дня
    — семь уже не дадут», а это ровно то, чего быть не должно: подписка на
    канал ради семи дней теряет смысл, если человек уже нажал первую кнопку.

    Второе здесь важное — сложение. `grant_subscription` прибавляет к
    остатку, а не заменяет его: подписка на канал на второй день пробы
    иначе отнимала бы у человека день.
    """

    def test_the_short_one_is_shorter(self):
        self.assertLess(storage.get_trial_free_days(),
                        storage.get_trial_days())

    def test_both_can_be_taken(self):
        self.assertEqual(storage.start_trial(self.UID, 3, kind="free"), 3)
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 7, "вторая проба не досталась")

    def test_they_add_up_instead_of_replacing(self):
        """Семь дней, выданные поверх трёх, обязаны дать десять, а не семь."""
        storage.start_trial(self.UID, 3, kind="free")
        run(trialgate.grant_for_subscription(Bot(Member("member")), self.UID))
        left = storage.subscription_days_left(self.UID)
        self.assertGreaterEqual(left, 9, f"осталось {left} дн. вместо десяти")

    def test_each_one_is_still_given_only_once(self):
        storage.start_trial(self.UID, 3, kind="free")
        self.assertEqual(storage.start_trial(self.UID, 3, kind="free"), 0)
        run(trialgate.grant_for_subscription(Bot(Member("member")), self.UID))
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 0, "неделя за подписку досталась дважды")

    def test_the_marks_do_not_leak_into_each_other(self):
        """Общая отметка — самая вероятная поломка здесь, и тихая."""
        storage.start_trial(self.UID, 3, kind="free")
        self.assertTrue(storage.trial_used(self.UID, "free"))
        self.assertFalse(storage.trial_used(self.UID, "channel"),
                         "короткая проба закрыла длинную")

    def test_a_paying_seller_gets_no_trial_on_top(self):
        """Оплаченную подписку от пробной отличает поле `by`: у выданной
        админом там его номер. Без этого различия проба поверх оплаты была
        бы неотличима от пробы поверх пробы."""
        storage.grant_subscription(self.UID, 30, by=storage.OWNER_ID)
        self.assertEqual(storage.start_trial(self.UID, 3, kind="free"), 0)

    def test_switching_off_the_short_one_leaves_the_long_one(self):
        storage.set_trial_free_days(0)
        self.assertEqual(storage.get_trial_free_days(), 0)
        days, _ = run(trialgate.grant_for_subscription(
            Bot(Member("member")), self.UID))
        self.assertEqual(days, 7)


class TheFirstScreenSellsBeforeItAsks(Bench):
    """Новый человек видит витрину: что бот умеет, сколько стоит и что
    можно взять бесплатно. Кнопки — под ней."""

    def setUp(self):
        super().setUp()
        import handlers.start as S
        self.S = S
        storage.set_trial_channel("@ch")

    def _labels(self, uid):
        return [b.text for row in self.S._hello_kb(uid).inline_keyboard
                for b in row]

    def test_it_names_both_trials_with_real_numbers(self):
        """Числа берутся из настроек: приветствие, обещающее прежние сроки,
        — то же враньё, только на первом экране."""
        storage.set_trial_free_days(4)
        text = self.S._welcome_text()
        self.assertIn("4 дня доступа", text)
        self.assertIn("7 дней сверху", text)

    def test_it_names_how_much_is_free_in_total(self):
        """Три и семь по отдельности не складываются в голове у того, кто
        читает экран впервые."""
        storage.set_trial_free_days(3)
        self.assertIn("10 дней бесплатно", self.S._welcome_text())

    def test_it_names_the_price_when_there_is_one(self):
        admin = storage._load_admin()
        try:
            storage.set_price(14, 249)
            self.assertIn("249", self.S._welcome_text())
        finally:
            storage._save_admin(admin)

    def test_it_does_not_invent_a_price_when_there_is_none(self):
        """«от 0 ₽» читается как «бесплатно» — это обещание, за которое
        спросят."""
        admin = storage._load_admin()
        try:
            for days, _l in storage.PRICE_TIERS:
                storage.set_price(days, 0)
            storage.set_bot_price(0)
            self.assertNotIn("0 ₽", self.S._welcome_text())
        finally:
            storage._save_admin(admin)

    def test_both_trial_buttons_are_offered_to_a_newcomer(self):
        labels = " ".join(self._labels(self.UID))
        self.assertIn("3 дня доступа", labels)
        self.assertIn("за подписку", labels)

    def test_a_used_up_trial_hides_only_its_own_button(self):
        """Кнопка «3 дня» тому, кто их уже брал, — обещание невозможного.
        Но вторая обязана остаться: её ещё не брали."""
        storage.note_trial(self.UID, "free")
        labels = " ".join(self._labels(self.UID))
        self.assertNotIn("дня доступа", labels)
        self.assertIn("за подписку", labels, "скрыли и вторую пробу")

    def test_both_used_up_shows_neither(self):
        storage.note_trial(self.UID, "free")
        storage.note_trial(self.UID, "channel")
        labels = " ".join(self._labels(self.UID))
        self.assertNotIn("дня доступа", labels)
        self.assertNotIn("за подписку", labels)

    def test_without_a_channel_only_the_short_one_is_offered(self):
        """Кнопка «за подписку» без заданного канала вела бы в пустоту."""
        storage.set_trial_channel("")
        labels = " ".join(self._labels(self.UID))
        self.assertIn("3 дня доступа", labels)
        self.assertNotIn("за подписку", labels)

    def test_connecting_the_shop_is_always_there(self):
        labels = self._labels(self.UID)
        self.assertTrue(any("одключить" in x for x in labels))

    def test_the_long_trial_shows_the_channel_before_refusing(self):
        """Кнопка вела прямо на проверку: не подписан — получи отказ и ищи
        канал сам. Ссылку надо дать до отказа, а не вместо него."""
        labels = self._labels(self.UID)
        target = [b.callback_data
                  for row in self.S._hello_kb(self.UID).inline_keyboard
                  for b in row if "за подписку" in b.text]
        self.assertEqual(target, ["trial:offer"])
        offer = [b.text for row in self.S._trial_kb().inline_keyboard
                 for b in row]
        self.assertTrue(any("анал" in x for x in offer), offer)


if __name__ == "__main__":
    unittest.main()
