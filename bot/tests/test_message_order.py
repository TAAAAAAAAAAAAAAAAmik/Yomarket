"""Разговор читается в том порядке, в котором он шёл.

Юмаркет отдаёт переписку ОТ НОВЫХ К СТАРЫМ. Три защиты от этого в коде уже
были — `_newest_id`, `_newest_msg` и сортировка в отчёте, у каждой в
комментарии «не rows[-1]». А те места, которые по списку ходили и
показывали, остались как есть: уведомления приходили задом наперёд, а экран
чата показывал начало переписки вместо свежих сообщений.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from tasks.manager import (TaskManager, _msg_rows, _newest_id,  # noqa: E402
                           _msg_sort_key)


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


# Так это приходит от Юмаркета: свежее сверху.
NEWEST_FIRST = [
    {"id": "15488500", "sender_type": "buyer", "text": "где ключ",
     "created_at": stamp(60)},
    {"id": "15488419", "sender_type": "buyer", "text": "привет",
     "created_at": stamp(120)},
    {"id": "15360529", "sender_type": "shop", "text": "Спасибо за заказ!",
     "created_at": stamp(3600)},
]


class TheEnvelopeIsUnpackedInOrder(unittest.TestCase):
    def test_a_reversed_reply_comes_back_chronological(self):
        rows = _msg_rows({"data": NEWEST_FIRST})
        self.assertEqual([m["text"] for m in rows],
                         ["Спасибо за заказ!", "привет", "где ключ"])

    def test_a_bare_list_is_sorted_too(self):
        rows = _msg_rows(list(NEWEST_FIRST))
        self.assertEqual(rows[-1]["text"], "где ключ")

    def test_the_newest_is_now_simply_the_last(self):
        rows = _msg_rows({"data": NEWEST_FIRST})
        self.assertEqual(str(rows[-1]["id"]), _newest_id(rows))

    def test_a_single_message_still_works(self):
        rows = _msg_rows({"data": [NEWEST_FIRST[0]]})
        self.assertEqual(len(rows), 1)

    def test_an_empty_chat_is_still_empty(self):
        self.assertEqual(_msg_rows({"data": []}), [])

    def test_non_numeric_ids_do_not_crash_the_sort(self):
        """Смешивать int и str в одном ключе нельзя — падало бы на TypeError."""
        rows = _msg_rows({"data": [
            {"id": "abc", "text": "буквенный", "created_at": stamp(10)},
            {"id": "7", "text": "числовой", "created_at": stamp(20)},
        ]})
        self.assertEqual(len(rows), 2)

    def test_a_message_without_an_id_is_kept(self):
        rows = _msg_rows({"data": [{"text": "без номера"}]})
        self.assertEqual(len(rows), 1)

    def test_the_key_puts_numbers_before_strings(self):
        self.assertLess(_msg_sort_key({"id": "9"})[0],
                        _msg_sort_key({"id": "x"})[0])


class FakeAPI:
    def __init__(self, rows):
        self.rows = rows
        self.sent: list[str] = []

    async def get_messages(self, cid):
        return {"data": self.rows}

    async def send_message(self, cid, text):
        self.sent.append(text)
        return {"ok": True}


def manager() -> TaskManager:
    tm = TaskManager.__new__(TaskManager)
    tm.notes: list[str] = []

    async def _notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = _notify
    return tm


def settings(seen="15360529") -> dict:
    return {
        "known_orders": {"77": "paid"},
        "known_messages": {"77": seen},
        "known_order_details": {"77": {"chat_id": "99", "seen_at": time.time(),
                                       "title": "Ключ Steam"}},
        "notify_messages": {"enabled": True},
        "autoreplies": {
            "enabled": True,
            "rules": [
                {"id": "r1", "on": True, "keywords": ["ключ"],
                 "text": "Ключ придёт в течение часа."},
                {"id": "r2", "on": True, "weak": True, "keywords": ["привет"],
                 "text": "Здравствуйте!"},
            ],
            "cooldown_min": 0, "max_per_order": 10,
        },
        "plugins": {"auto_stars": {"enabled": False}},
    }


class NotificationsArriveInTheOrderTheyWereWritten(unittest.TestCase):
    def _run(self):
        s = settings()
        api = FakeAPI(NEWEST_FIRST)
        tm = manager()
        asyncio.run(tm._check_messages(1, api, s))
        return s, api, tm.notes

    def test_the_greeting_comes_before_the_question(self):
        _s, _api, notes = self._run()
        letters = [n for n in notes if "СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ" in n]
        self.assertEqual(len(letters), 2, notes)
        self.assertIn("привет", letters[0])
        self.assertIn("где ключ", letters[1])

    def test_the_shop_s_own_line_is_not_announced(self):
        _s, _api, notes = self._run()
        self.assertFalse([n for n in notes if "Спасибо за заказ" in n])


class OneAnswerPerBatchAndItIsTheLast(unittest.TestCase):
    """«привет» и следом «где ключ» — вопрос во втором. Раньше отвечало то
    письмо, которое API вернул первым, а он отдаёт от новых к старым: выбор
    был случайностью формата ответа, а не решением."""

    def _run(self):
        s = settings()
        api = FakeAPI(NEWEST_FIRST)
        asyncio.run(manager()._check_messages(1, api, s))
        return s, api

    def test_exactly_one_answer_goes_out(self):
        _s, api = self._run()
        self.assertEqual(len(api.sent), 1, api.sent)

    def test_and_it_answers_the_question_not_the_greeting(self):
        _s, api = self._run()
        self.assertEqual(api.sent, ["Ключ придёт в течение часа."])

    def test_the_unanswered_letters_are_not_hidden(self):
        import autoreply as ar
        s, _api = self._run()
        why = (ar.cfg(s).get("last_skip") or {}).get("why", "")
        self.assertIn("писем за один проход: 2", why)

    def test_a_lone_letter_records_no_such_note(self):
        import autoreply as ar
        s = settings(seen="15488419")
        api = FakeAPI(NEWEST_FIRST)
        asyncio.run(manager()._check_messages(1, api, s))
        self.assertEqual(api.sent, ["Ключ придёт в течение часа."])
        self.assertNotIn("писем за один проход",
                         (ar.cfg(s).get("last_skip") or {}).get("why", ""))


class TheMissedSummaryReadsForwards(unittest.TestCase):
    def test_old_letters_are_listed_oldest_first(self):
        s = settings(seen="1")
        rows = [
            {"id": "30", "sender_type": "buyer", "text": "третье",
             "created_at": stamp(20 * 3600)},
            {"id": "10", "sender_type": "buyer", "text": "первое",
             "created_at": stamp(40 * 3600)},
            {"id": "20", "sender_type": "buyer", "text": "второе",
             "created_at": stamp(30 * 3600)},
        ]
        tm = manager()
        asyncio.run(tm._check_messages(1, FakeAPI(rows), s))
        summary = next(n for n in tm.notes if "первое" in n)
        self.assertLess(summary.index("первое"), summary.index("второе"))
        self.assertLess(summary.index("второе"), summary.index("третье"))


class TheChatScreenShowsTheRecentTalk(unittest.TestCase):
    """`messages[-10:]` означало «десять самых старых, вверх ногами»."""

    def test_the_slice_now_holds_the_freshest(self):
        rows = _msg_rows({"data": [
            {"id": str(i), "text": f"m{i}", "created_at": stamp(1000 - i)}
            for i in range(1, 21)]})
        self.assertEqual([m["text"] for m in rows[-3:]], ["m18", "m19", "m20"])


if __name__ == "__main__":
    unittest.main()
