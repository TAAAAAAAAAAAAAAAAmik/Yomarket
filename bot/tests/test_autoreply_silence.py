"""Почему бот промолчал — должно быть записано всегда.

«Автоответы не работают» — жалоба, за которой стоит с десяток разных
причин: выключено, нет правил, чат не опрашивается, сработала пауза,
сообщение пришло слишком давно. Экран «Почему молчит» отвечает на это
только тогда, когда причина действительно записана.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import autoreply as ar                     # noqa: E402
from tasks.manager import TaskManager      # noqa: E402
import tasks.manager as M                  # noqa: E402


def stamp(ago_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(time.time() - ago_seconds))


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


def settings_with_rules(**over) -> dict:
    s = {
        "known_orders": {"77": "paid"},
        "known_messages": {"77": "1"},
        "known_order_details": {"77": {"chat_id": "99", "seen_at": time.time(),
                                       "title": "Ключ Steam"}},
        "notify_messages": {"enabled": False},
        "autoreplies": {
            "enabled": True,
            "rules": [{"id": "r1", "on": True, "keywords": ["ключ"],
                       "text": "Ключ придёт в течение часа."}],
            "cooldown_min": 0, "max_per_order": 10,
        },
        "plugins": {"auto_stars": {"enabled": False}},
    }
    s.update(over)
    return s


class AFreshMessageIsAnswered(unittest.TestCase):
    def test_the_rule_fires(self):
        s = settings_with_rules()
        api = FakeAPI([{"id": "2", "sender_type": "buyer",
                        "text": "а где ключ?", "created_at": stamp(60)}])
        asyncio.run(manager()._check_messages(1, api, s))
        self.assertEqual(api.sent, ["Ключ придёт в течение часа."], api.sent)

    def test_and_nothing_is_recorded_as_a_skip(self):
        s = settings_with_rules()
        api = FakeAPI([{"id": "2", "sender_type": "buyer",
                        "text": "а где ключ?", "created_at": stamp(60)}])
        asyncio.run(manager()._check_messages(1, api, s))
        self.assertFalse(ar.cfg(s).get("last_skip"))


class AStaleMessageSaysWhy(unittest.TestCase):
    """Сообщению сутки — отвечать поздно, и это надо записать: без записи
    экран «Почему молчит» уверенно сообщает «не молчал»."""

    def _run(self):
        s = settings_with_rules()
        api = FakeAPI([{"id": "2", "sender_type": "buyer",
                        "text": "а где ключ?",
                        "created_at": stamp(30 * 3600)}])
        asyncio.run(manager()._check_messages(1, api, s))
        return s, api

    def test_no_answer_is_sent(self):
        _s, api = self._run()
        self.assertEqual(api.sent, [])

    def test_the_reason_is_written_down(self):
        s, _api = self._run()
        skip = ar.cfg(s).get("last_skip") or {}
        self.assertTrue(skip, "причина молчания не записана")
        self.assertIn("поздно", skip.get("why", ""))

    def test_it_names_how_old_the_message_was(self):
        s, _api = self._run()
        self.assertIn("30 ч", ar.cfg(s)["last_skip"]["why"])

    def test_nothing_is_recorded_when_autoreplies_are_off(self):
        """Выключенные автоответы молчат по своей причине, и она уже есть."""
        s = settings_with_rules()
        s["autoreplies"]["enabled"] = False
        api = FakeAPI([{"id": "2", "sender_type": "buyer", "text": "ключ",
                        "created_at": stamp(30 * 3600)}])
        asyncio.run(manager()._check_messages(1, api, s))
        self.assertFalse(ar.cfg(s).get("last_skip"))


class EveryOtherSilenceIsExplained(unittest.TestCase):
    def _skip_reason(self, **over) -> str:
        s = settings_with_rules(**over)
        api = FakeAPI([{"id": "2", "sender_type": "buyer",
                        "text": "здравствуйте", "created_at": stamp(60)}])
        asyncio.run(manager()._check_messages(1, api, s))
        return (ar.cfg(s).get("last_skip") or {}).get("why", "")

    def test_no_rule_matched(self):
        self.assertIn("правило", self._skip_reason())

    def test_switched_off(self):
        s = settings_with_rules()
        s["autoreplies"]["enabled"] = False
        api = FakeAPI([{"id": "2", "sender_type": "buyer", "text": "ключ",
                        "created_at": stamp(60)}])
        asyncio.run(manager()._check_messages(1, api, s))
        self.assertIn("выключены", (ar.cfg(s).get("last_skip") or {})["why"])

    def test_the_cooldown_is_named_as_such(self):
        s = settings_with_rules()
        s["autoreplies"]["cooldown_min"] = 60
        ar.note_sent(ar.cfg(s), "99")
        api = FakeAPI([{"id": "2", "sender_type": "buyer", "text": "ключ",
                        "created_at": stamp(60)}])
        asyncio.run(manager()._check_messages(1, api, s))
        why = (ar.cfg(s).get("last_skip") or {}).get("why", "")
        self.assertTrue(why, "пауза должна быть названа причиной")


class Screen:
    def __init__(self):
        self.texts: list[str] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return self


class CB:
    def __init__(self):
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.data = "ar:why"

    async def answer(self, text="", **kw):
        pass


class TheScreenTellsWhichChatsArePolled(unittest.TestCase):
    """Опрашиваются самые свежие заказы. Сообщение в старом чате бот не
    увидит вовсе — и это не «не сработало правило», а другое."""

    def _show(self, poll: dict) -> str:
        import handlers.responders as R
        s = settings_with_rules()
        s["_chat_poll"] = poll
        R.get_settings = lambda uid: s
        cb = CB()
        asyncio.run(R.ar_why(cb))
        return cb.message.texts[-1]

    def test_it_warns_when_some_orders_are_left_out(self):
        text = self._show({"ts": time.time(), "chats": 25, "orders": 60})
        self.assertIn("не опрашиваются", text, text)
        self.assertIn("35", text, "должно быть видно, скольких не хватает")

    def test_it_stays_quiet_when_every_order_is_polled(self):
        text = self._show({"ts": time.time(), "chats": 12, "orders": 12})
        self.assertNotIn("не опрашиваются", text, text)

    def test_a_poll_that_never_ran_is_named(self):
        self.assertIn("ни разу", self._show({}))


if __name__ == "__main__":
    unittest.main()
