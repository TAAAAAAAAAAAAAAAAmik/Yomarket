"""Продавец, которому бот не может писать, не должен пропадать молча.

`_notify` при неудачной отправке писал строчку в лог и на этом
заканчивал. Пока это редкость — терпимо; но при смене токена бота так
становится СРАЗУ СО ВСЕМИ: продавцы остаются в переписке со старым ботом, а
новому Telegram запрещает писать первым, пока человек не нажал /start.

Снаружи это выглядит идеально исправным ботом: заказы обрабатываются, чаты
читаются, ошибок нет. Просто никто ничего не получает.

Отличать постоянную недостижимость от сетевого сбоя обязательно: объявить
недостижимым по таймауту — значит замолчать на ровном месте.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402
import tasks.manager as M                                  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TellsPermanentFromTemporary(unittest.TestCase):

    def test_a_seller_who_never_started_this_bot_is_permanent(self):
        self.assertTrue(M._unreachable(
            "Forbidden: bot can't initiate conversation with a user"))

    def test_being_blocked_is_permanent(self):
        self.assertTrue(M._unreachable("Forbidden: bot was blocked by the user"))

    def test_a_deleted_account_is_permanent(self):
        self.assertTrue(M._unreachable("Forbidden: user is deactivated"))

    def test_a_timeout_is_not(self):
        # Объявить продавца недостижимым по таймауту — замолчать на ровном
        # месте: сеть моргнула, а уведомления больше не идут.
        self.assertFalse(M._unreachable("timeout"))

    def test_a_dropped_connection_is_not(self):
        self.assertFalse(M._unreachable("Connection reset by peer"))

    def test_a_rate_limit_is_not(self):
        self.assertFalse(M._unreachable("Too Many Requests: retry after 30"))

    def test_nothing_at_all_is_not(self):
        self.assertFalse(M._unreachable(""))
        self.assertFalse(M._unreachable(None))


class Store(unittest.TestCase):

    UID = 7

    def setUp(self):
        self.settings: dict = {}
        self._get, self._save = storage.get_settings, storage.save_settings
        storage.get_settings = lambda uid: self.settings.setdefault(uid, {})
        storage.save_settings = lambda uid, s: self.settings.__setitem__(uid, s)
        M.get_settings = storage.get_settings
        M.save_settings = storage.save_settings

        self.sent: list[tuple[int, str]] = []
        self.fail_with: Exception | None = None

        outer = self

        class Bot:
            async def send_message(self, uid, text, **kw):
                if outer.fail_with is not None and uid == outer.UID:
                    raise outer.fail_with
                outer.sent.append((uid, text))

        self.tm = M.TaskManager(bot=Bot())

    def tearDown(self):
        storage.get_settings, storage.save_settings = self._get, self._save
        M.get_settings, M.save_settings = self._get, self._save

    def to_owner(self) -> list[str]:
        return [t for uid, t in self.sent if uid == storage.OWNER_ID]


class TheOwnerIsToldOnceAndOnlyOnce(Store):

    def test_the_owner_learns_that_a_seller_is_unreachable(self):
        self.fail_with = RuntimeError(
            "Forbidden: bot can't initiate conversation with a user")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertEqual(len(self.to_owner()), 1)
        self.assertIn(str(self.UID), self.to_owner()[0])

    def test_the_message_says_what_to_do_about_it(self):
        self.fail_with = RuntimeError("Forbidden: bot can't initiate conversation")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertIn("/start", self.to_owner()[0])

    def test_it_is_said_once_and_not_every_pass(self):
        # Фоновый проход идёт каждую минуту: без отметки владелец получал бы
        # одно и то же круглые сутки и перестал бы читать.
        self.fail_with = RuntimeError("Forbidden: bot was blocked by the user")
        for _ in range(5):
            run(self.tm._notify(self.UID, "заказ"))
        self.assertEqual(len(self.to_owner()), 1)

    def test_a_temporary_failure_does_not_bother_the_owner(self):
        self.fail_with = RuntimeError("timeout")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertEqual(self.to_owner(), [])

    def test_a_temporary_failure_does_not_mark_the_seller(self):
        self.fail_with = RuntimeError("Connection reset")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertNotIn("unreachable_since", self.settings.get(self.UID, {}))

    def test_two_different_sellers_are_both_reported(self):
        self.fail_with = RuntimeError("Forbidden: chat not found")
        run(self.tm._notify(self.UID, "заказ"))
        self.UID = 8
        run(self.tm._notify(8, "заказ"))
        self.assertEqual(len(self.to_owner()), 2)


class TheMarkClearsItselfWhenItIsFixed(Store):

    def test_a_delivered_notification_clears_the_mark(self):
        self.fail_with = RuntimeError("Forbidden: bot can't initiate conversation")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertIn("unreachable_since", self.settings[self.UID])

        self.fail_with = None                     # продавец нажал /start
        run(self.tm._notify(self.UID, "заказ"))
        self.assertNotIn("unreachable_since", self.settings[self.UID])

    def test_a_later_failure_is_reported_again(self):
        # Иначе отметка стоит вечно, и о НОВОЙ поломке владелец не узнает.
        self.fail_with = RuntimeError("Forbidden: bot was blocked by the user")
        run(self.tm._notify(self.UID, "заказ"))
        self.fail_with = None
        run(self.tm._notify(self.UID, "заказ"))
        self.fail_with = RuntimeError("Forbidden: bot was blocked by the user")
        run(self.tm._notify(self.UID, "заказ"))
        self.assertEqual(len(self.to_owner()), 2)

    def test_a_working_seller_is_never_marked(self):
        run(self.tm._notify(self.UID, "заказ"))
        self.assertEqual(self.settings.get(self.UID, {}), {})
        self.assertEqual(self.to_owner(), [])


class TheNotificationItselfStillGoesOut(Store):

    def test_a_reachable_seller_gets_his_message(self):
        run(self.tm._notify(self.UID, "новый заказ"))
        self.assertIn((self.UID, "новый заказ"), self.sent)

    def test_the_token_is_never_part_of_any_of_this(self):
        # Токен бота приходит только из окружения и в настройках продавца
        # ему делать нечего.
        src = (Path(__file__).resolve().parents[1] / "config.py").read_text()
        self.assertIn('os.environ["BOT_TOKEN"]', src)
        for path in ("tasks/manager.py", "storage.py"):
            text = (Path(__file__).resolve().parents[1] / path).read_text()
            self.assertNotIn("BOT_TOKEN", text, path)


if __name__ == "__main__":
    unittest.main()
