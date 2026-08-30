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
        self.assertTrue(storage.trial_used(self.UID))

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

    def test_the_offer_promises_the_same_number(self):
        self.assertIn("7 календарных дней", (self.DOCS / "offer.md").read_text())

    def test_the_terms_promise_the_same_number(self):
        self.assertIn("7 календарных\nдней", (self.DOCS / "terms.md").read_text())

    def test_both_mention_the_channel_condition(self):
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                self.assertIn("подписка на Telegram-канал",
                              (self.DOCS / name).read_text())

    def test_neither_still_says_it_is_given_at_first_start(self):
        """Прежнее обещание «при первом запуске» стало неверным: теперь
        сначала условие."""
        for name in ("offer.md", "terms.md"):
            with self.subTest(name):
                text = (self.DOCS / name).read_text()
                self.assertNotIn("при первом запуске Бота", text)


if __name__ == "__main__":
    unittest.main()
