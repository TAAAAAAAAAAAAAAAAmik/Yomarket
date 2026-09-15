"""Постоянный покупатель — один чат, а не столько чатов, сколько заказов.

Все заказы одного покупателя ведутся в одной переписке: у второго и
третьего заказа тот же chat_id. Опрос шёл по заказам, поэтому один и тот
же чат читался столько раз, сколько у покупателя заказов, — и каждое его
письмо приходило продавцу столько же раз.

Заметить это на случайном покупателе нельзя: у него заказ один, и всё
выглядит исправно. Дубли появлялись ровно тогда, когда писал тот, кто уже
покупал раньше.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from tasks.manager import TaskManager      # noqa: E402


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


class API:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"id": "15488500", "sender_type": "buyer", "text": "привет",
             "created_at": stamp(60)}]
        self.reads: list[str] = []
        self.sent: list[str] = []

    async def get_messages(self, cid):
        self.reads.append(str(cid))
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


def settings(orders: int, chat: str = "1139042", *, seen="15488400",
             seen_key=None) -> dict:
    known, det, marks = {}, {}, {}
    for i in range(orders):
        oid = str(1200750 + i)
        known[oid] = "paid"
        det[oid] = {"chat_id": chat, "seen_at": time.time() - i,
                    "title": "50 звёзд"}
        if seen is not None and seen_key is None:
            marks[oid] = seen
    if seen_key is not None:
        marks[seen_key] = seen
    return {
        "known_orders": known, "known_order_details": det,
        "known_messages": marks,
        "notify_messages": {"enabled": True},
        "autoreplies": {"enabled": False},
        "plugins": {"auto_stars": {"enabled": False}},
    }


def run(s: dict, api: API | None = None):
    api = api or API()
    tm = manager()
    asyncio.run(tm._check_messages(1, api, s))
    return api, tm.notes


class OneChatIsReadOnce(unittest.TestCase):
    def test_a_buyer_with_one_order(self):
        api, notes = run(settings(1))
        self.assertEqual(len(api.reads), 1)
        self.assertEqual(len(notes), 1)

    def test_a_buyer_with_two_orders_still_gets_one_notification(self):
        api, notes = run(settings(2))
        self.assertEqual(len(api.reads), 1, api.reads)
        self.assertEqual(len(notes), 1, notes)

    def test_a_regular_with_five_orders_too(self):
        api, notes = run(settings(5))
        self.assertEqual(len(api.reads), 1, api.reads)
        self.assertEqual(len(notes), 1, notes)

    def test_different_buyers_are_still_read_separately(self):
        """Схлопывать надо одинаковые чаты, а не все подряд."""
        s = settings(1, chat="111")
        s["known_orders"]["999"] = "paid"
        s["known_order_details"]["999"] = {"chat_id": "222",
                                           "seen_at": time.time(),
                                           "title": "Ключ"}
        s["known_messages"]["999"] = "15488400"
        api, notes = run(s)
        self.assertEqual(sorted(api.reads), ["111", "222"])
        self.assertEqual(len(notes), 2)


class TheMarkBelongsToTheChat(unittest.TestCase):
    def test_it_is_stored_under_the_chat(self):
        s = settings(2)
        run(s)
        self.assertEqual(s["known_messages"], {"c1139042": "15488500"})

    def test_the_old_per_order_marks_are_folded_in(self):
        """Иначе при переходе на новый ключ вся переписка объявилась бы
        заново — уже прочитанное считалось бы новым."""
        s = settings(3)
        s["known_messages"]["1200750"] = "15488500"   # этот заказ уже дочитан
        api, notes = run(s)
        self.assertEqual(notes, [], "прочитанное объявлено повторно")
        self.assertEqual(s["known_messages"], {"c1139042": "15488500"})

    def test_the_furthest_of_the_old_marks_wins(self):
        s = settings(3, seen=None)
        s["known_messages"] = {"1200750": "15488400", "1200751": "15488500",
                               "1200752": "15000000"}
        _api, notes = run(s)
        self.assertEqual(notes, [])

    def test_a_second_pass_stays_quiet(self):
        s = settings(2)
        run(s)
        _api, notes = run(s)
        self.assertEqual(notes, [])


class TheNewestOrderRepresentsTheChat(unittest.TestCase):
    """Письмо почти наверняка про последнюю покупку: к ней и привязываем
    товар, ник и очередь выдачи звёзд."""

    def test_the_card_names_the_latest_order(self):
        s = settings(3)
        for i, title in enumerate(("свежий", "средний", "старый")):
            s["known_order_details"][str(1200750 + i)]["title"] = title
        _api, notes = run(s)
        self.assertIn("свежий", notes[0])
        self.assertNotIn("старый", notes[0])


class ThePollPulseCountsChatsNotOrders(unittest.TestCase):
    def test_five_orders_in_one_chat_count_as_one(self):
        s = settings(5)
        run(s)
        self.assertEqual(s["_chat_poll"]["chats"], 1)
        self.assertEqual(s["_chat_poll"]["orders"], 5)


class TheAutoreplyGoesOutOnce(unittest.TestCase):
    def test_a_repeat_buyer_gets_one_answer(self):
        s = settings(3)
        s["autoreplies"] = {"enabled": True, "cooldown_min": 0,
                            "max_per_order": 10,
                            "rules": [{"id": "r", "on": True,
                                       "keywords": ["привет"],
                                       "text": "Здравствуйте!"}]}
        api, _notes = run(s)
        self.assertEqual(api.sent, ["Здравствуйте!"], api.sent)


if __name__ == "__main__":
    unittest.main()
