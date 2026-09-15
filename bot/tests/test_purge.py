"""Удаление данных продавца: по кнопке и по истечении подписки.

Политика конфиденциальности обещает два удаления — по запросу и через три
календарных дня после окончания подписки. Обещание, которого код не
выполняет, — не недоделка, а ложь в публичном документе.

Отсюда требования, которые здесь и проверяются.

Удаление, прошедшее мимо одного из шести хранилищ, ХУЖЕ отсутствующего:
оно заявляет полноту. Поэтому список хранилищ проверяется целиком, а не
выборочно, и тест падает, если в боте заведётся седьмое.

И наоборот: проход, стирающий лишнее, сносит живой магазин человеку,
который ничего не нарушал. Поэтому проверяется и то, чего он НЕ трогает.
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


class Blobs(unittest.TestCase):
    """Хранилище — словарь в памяти. Без отката в tearDown соседние тесты
    падали бы «сами по себе»: это уже случалось с `promo_params`."""

    UID = 7

    def setUp(self):
        self.blobs: dict[str, dict] = {}
        self._read, self._write = storage._read_blob, storage._write_blob
        self._owner = storage.is_owner
        storage._read_blob = lambda name: self.blobs.setdefault(name, {})
        storage._write_blob = lambda name, data: self.blobs.__setitem__(name, data)
        storage.is_owner = lambda uid: int(uid) == 1
        self.fill()

    def tearDown(self):
        storage._read_blob, storage._write_blob = self._read, self._write
        storage.is_owner = self._owner

    def fill(self, uid: int | None = None):
        """Продавец с двумя магазинами во всех хранилищах разом."""
        uid = self.UID if uid is None else uid
        for name in storage._USER_BLOBS:
            blob = self.blobs.setdefault(name, {})
            blob[str(uid)] = {"что-то": "личное"}
            blob[f"{uid}::второй"] = {"что-то": "личное"}
        self.blobs.setdefault("admin", {}).setdefault("subscriptions", {})[
            str(uid)] = {"expires": time.time() + 86400}

    def left(self, uid: int | None = None) -> dict:
        uid = self.UID if uid is None else uid
        return {name: storage._user_keys(blob, uid)
                for name, blob in self.blobs.items()
                if name != "admin" and storage._user_keys(blob, uid)}


class DeletingMeansEveryStore(Blobs):

    def test_nothing_of_the_seller_is_left_anywhere(self):
        storage.purge_user(self.UID)
        self.assertEqual(self.left(), {})

    def test_every_store_the_bot_has_is_covered(self):
        """Список хранилищ берётся из ИСХОДНИКА, а не из проверяемой константы.

        Первая версия этого теста собирала список из `_USER_BLOBS` — то
        есть сверяла константу с ней же. Убрать оттуда `ns_creds` (ключи
        поставщика) она позволяла молча: удаление продолжало отвечать
        «готово», оставляя ключ на месте.
        """
        import ast
        src = (Path(__file__).resolve().parents[1] / "storage.py").read_text()
        used = {n.args[0].value
                for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("_read_blob", "_write_blob")
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)}
        # «admin» — общее на весь бот: подписки, админы, чёрный список.
        # Оно разбирается в `purge_user` отдельно и по одной записи.
        forgotten = used - {"admin"} - set(storage._USER_BLOBS)
        self.assertEqual(
            forgotten, set(),
            f"в этих хранилищах лежат данные продавца, а удаление про них "
            f"не знает: {sorted(forgotten)}")

    def test_all_of_his_shops_go_not_just_the_active_one(self):
        # Данные разложены по «{uid}::{магазин}» — удаление одного ключа
        # оставило бы второй магазин с токеном и seed-фразой.
        storage.purge_user(self.UID)
        for name in storage._USER_BLOBS:
            self.assertNotIn(f"{self.UID}::второй", self.blobs[name])

    def test_the_report_says_what_was_actually_removed(self):
        # «✅ удалено» без перечня — то самое бодрое сообщение об успехе,
        # по которому нельзя понять, случилось ли что-нибудь.
        report = storage.purge_user(self.UID)
        for name in storage._USER_BLOBS:
            self.assertEqual(report.get(name), 2, name)

    def test_deleting_twice_is_honest_about_finding_nothing(self):
        storage.purge_user(self.UID)
        self.assertEqual(storage.purge_user(self.UID), {})

    def test_a_neighbour_with_a_similar_id_is_not_touched(self):
        # «7» не должен зацепить «71»: у соседа свой магазин.
        self.fill(71)
        storage.purge_user(self.UID)
        self.assertEqual(len(self.left(71)), len(storage._USER_BLOBS))

    def test_the_subscription_record_goes_too(self):
        storage.purge_user(self.UID)
        self.assertNotIn(str(self.UID),
                         self.blobs["admin"].get("subscriptions", {}))


class SomeThingsSurviveOnPurpose(Blobs):

    def test_a_block_survives_deletion(self):
        # Иначе «удалить мои данные» становится способом снять блокировку:
        # заблокировали — стёр — вернулся.
        self.blobs["admin"]["blocked"] = [self.UID]
        storage.purge_user(self.UID)
        self.assertIn(self.UID, self.blobs["admin"]["blocked"])

    def test_the_owner_cannot_wipe_himself(self):
        self.fill(1)
        report = storage.purge_user(1)
        self.assertIn("отказ", report)
        self.assertEqual(len(self.left(1)), len(storage._USER_BLOBS))

    def test_admin_rights_are_data_about_a_person_and_go(self):
        self.blobs["admin"]["admins"] = [self.UID, 99]
        storage.purge_user(self.UID)
        self.assertEqual(self.blobs["admin"]["admins"], [99])


class OnlyRealExpiriesCount(Blobs):

    def subs(self, **rows):
        self.blobs.setdefault("admin", {})["subscriptions"] = rows

    def test_a_subscription_that_ran_out_long_ago_is_listed(self):
        self.subs(**{"7": {"expires": time.time() - 10 * 86400}})
        self.assertEqual(storage.expired_before(time.time() - 3 * 86400), [7])

    def test_one_that_ran_out_yesterday_is_not(self):
        self.subs(**{"7": {"expires": time.time() - 86400}})
        self.assertEqual(storage.expired_before(time.time() - 3 * 86400), [])

    def test_a_live_subscription_is_not(self):
        self.subs(**{"7": {"expires": time.time() + 86400}})
        self.assertEqual(storage.expired_before(time.time() - 3 * 86400), [])

    def test_a_seller_with_no_subscription_record_is_never_listed(self):
        # Это не «подписка кончилась бесконечно давно», а человек,
        # работающий с выключенной проверкой подписки. Стереть его данные
        # значит снести живой магазин.
        self.subs()
        self.assertEqual(storage.expired_before(time.time()), [])

    def test_an_empty_expiry_is_not_read_as_the_beginning_of_time(self):
        # Ноль или пусто в `expires` — это «срок неизвестен», а не «истёк
        # в 1970 году». Прочитать это как истёкший срок значит стереть
        # данные тому, у кого запись просто неполная.
        self.subs(**{"7": {"expires": 0}, "8": {}, "9": {"expires": None}})
        self.assertEqual(storage.expired_before(time.time()), [])

    def test_a_broken_date_does_not_crash_the_sweep(self):
        # Хранилище — правимый JSON: одно кривое значение не должно
        # останавливать удаление у всех остальных.
        self.subs(**{"7": {"expires": "позавчера"},
                     "8": {"expires": time.time() - 10 * 86400}})
        self.assertEqual(storage.expired_before(time.time() - 3 * 86400), [8])


class TheSweepIsCarefulAboutWhoItTouches(Blobs):

    def setUp(self):
        super().setUp()
        self.tm = M.TaskManager(bot=None)
        self.notified: list[int] = []
        self.stopped: list[int] = []

        async def notify(uid, text, **kw):
            self.notified.append(uid)
        self.tm._notify = notify
        self.tm.stop_for_user = lambda uid: self.stopped.append(uid)
        self._req = storage.require_subscription_enabled
        storage.require_subscription_enabled = lambda: True
        M.require_subscription_enabled = lambda: True

    def tearDown(self):
        storage.require_subscription_enabled = self._req
        super().tearDown()

    def expire(self, uid: int, days_ago: float):
        self.blobs["admin"]["subscriptions"][str(uid)] = {
            "expires": time.time() - days_ago * 86400}

    def test_data_goes_three_days_after_the_subscription_ended(self):
        self.expire(self.UID, 4)
        self.assertEqual(run(self.tm.purge_expired()), [self.UID])
        self.assertEqual(self.left(), {})

    def test_on_the_second_day_nothing_is_touched(self):
        self.expire(self.UID, 2)
        self.assertEqual(run(self.tm.purge_expired()), [])
        self.assertEqual(len(self.left()), len(storage._USER_BLOBS))

    def test_the_loops_are_stopped_before_the_data_is_wiped(self):
        # Проход продавца держит его настройки в памяти и сохраняет их в
        # конце: удаление на ходу вернулось бы обратно секундой позже, а
        # человек получил бы «удалено» про данные, оставшиеся на месте.
        self.expire(self.UID, 4)
        run(self.tm.purge_expired())
        self.assertIn(self.UID, self.stopped)

    def test_the_seller_is_told_and_not_wiped_in_silence(self):
        self.expire(self.UID, 4)
        run(self.tm.purge_expired())
        self.assertEqual(self.notified, [self.UID])

    def test_with_subscriptions_switched_off_the_sweep_does_nothing(self):
        # При выключенной проверке подписки истёкшая отметка ничего не
        # значит: продавец работает и без неё.
        storage.require_subscription_enabled = lambda: False
        M.require_subscription_enabled = lambda: False
        self.expire(self.UID, 40)
        self.assertEqual(run(self.tm.purge_expired()), [])
        self.assertEqual(len(self.left()), len(storage._USER_BLOBS))

    def test_the_same_seller_is_not_wiped_and_told_about_it_twice(self):
        # Запись о подписке удаляется вместе с остальным, поэтому второй
        # проход этого продавца уже не видит. Иначе он получал бы «данные
        # удалены» каждые шесть часов до конца времён.
        self.expire(self.UID, 4)
        run(self.tm.purge_expired())
        self.assertEqual(run(self.tm.purge_expired()), [])
        self.assertEqual(self.notified, [self.UID])

    def test_the_period_matches_what_the_policy_promises(self):
        # Три календарных дня — это написано в опубликованном документе.
        self.assertEqual(M.TaskManager.PURGE_AFTER, 3 * 86400)
        doc = (Path(__file__).resolve().parents[2]
               / "docs" / "legal" / "privacy.md").read_text()
        self.assertIn("3 календарных дня", doc)


class TheSweepIsActuallyStarted(unittest.TestCase):
    """Проход, который никто не запускает, — обещание, не выполняемое молча."""

    def test_start_all_starts_it(self):
        import inspect
        src = inspect.getsource(M.TaskManager.start_all)
        self.assertIn("start_purge_loop", src)

    def test_only_one_sweep_runs_at_a_time(self):
        # Два прохода — это два удаления и два уведомления об одном и том же.
        async def go():
            tm = M.TaskManager(bot=None)
            tm.start_purge_loop()
            first = tm._purge_task
            tm.start_purge_loop()
            self.assertIs(tm._purge_task, first)
            first.cancel()
        run(go())


if __name__ == "__main__":
    unittest.main()
