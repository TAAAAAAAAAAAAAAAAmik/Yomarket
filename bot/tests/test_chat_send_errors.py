"""Отказ маркетплейса — по-русски и с верным советом.

«There are no active orders in this chat» на экране продавца — это не
диагностика, а отписка. Хуже того, рядом стоял совет «ответьте вручную»:
в закрытый чат маркетплейс не пустит и человека, так что совет отправлял
продавца биться в стену.

Здесь же — чат, которого нет: он отвечал 404 на каждом проходе, занимал
место в очереди из 25 чатов и висел предупреждением, по которому нечего
делать.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import autoreply as ar                                   # noqa: E402
from tasks.manager import TaskManager, _chats_to_poll     # noqa: E402


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


class ErrorsAreTranslated(unittest.TestCase):
    def test_a_closed_chat_is_explained(self):
        why, fixable = ar.explain_error(
            "There are no active orders in this chat (no_active_orders_in_chat)")
        self.assertIn("закрыл чат", why)
        self.assertNotIn("no_active_orders", why)

    def test_and_is_not_something_the_seller_can_fix(self):
        _why, fixable = ar.explain_error("no_active_orders_in_chat")
        self.assertFalse(fixable, "нельзя советовать «ответьте вручную»")

    def test_a_missing_chat_is_explained(self):
        why, fixable = ar.explain_error("Resource not found (resource_not_found)")
        self.assertIn("чата больше нет", why)
        self.assertFalse(fixable)

    def test_a_dead_token_is_the_seller_s_to_fix(self):
        why, fixable = ar.explain_error("401 unauthenticated")
        self.assertIn("токен", why)
        self.assertTrue(fixable)

    def test_an_unknown_error_is_passed_through_verbatim(self):
        """Придумывать объяснение опаснее, чем показать как есть."""
        why, fixable = ar.explain_error("connection reset by peer")
        self.assertEqual(why, "connection reset by peer")
        self.assertTrue(fixable)

    def test_an_empty_error_still_says_something(self):
        self.assertTrue(ar.explain_error("")[0])


class FakeAPI:
    def __init__(self, rows, send_error: str = ""):
        self.rows = rows
        self.send_error = send_error
        self.sent: list[str] = []

    async def get_messages(self, cid):
        return {"data": self.rows}

    async def send_message(self, cid, text):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.sent.append(text)
        return {"ok": True}


def manager() -> TaskManager:
    tm = TaskManager.__new__(TaskManager)
    tm.notes: list[str] = []

    async def _notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = _notify
    return tm


def settings() -> dict:
    return {
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


class AFailedSendTellsTheTruth(unittest.TestCase):
    def _run(self, error: str):
        s = settings()
        api = FakeAPI([{"id": "2", "sender_type": "buyer", "text": "где ключ",
                        "created_at": stamp(60)}], send_error=error)
        tm = manager()
        asyncio.run(tm._check_messages(1, api, s))
        return s, tm.notes

    def test_a_closed_chat_does_not_advise_answering_by_hand(self):
        _s, notes = self._run("There are no active orders in this chat "
                              "(no_active_orders_in_chat)")
        self.assertTrue(notes, "продавца надо предупредить")
        self.assertNotIn("ответьте вручную", notes[0])
        self.assertIn("чат закрыт", notes[0])

    def test_the_english_code_never_reaches_the_seller(self):
        _s, notes = self._run("no_active_orders_in_chat")
        self.assertNotIn("no_active_orders", notes[0])

    def test_an_ordinary_failure_still_asks_for_a_manual_answer(self):
        _s, notes = self._run("connection reset by peer")
        self.assertIn("ответьте вручную", notes[0])

    def test_the_failure_is_recorded_as_a_reason_for_silence(self):
        """Экран «Почему молчит» должен знать и про этот случай."""
        s, _notes = self._run("no_active_orders_in_chat")
        why = (ar.cfg(s).get("last_skip") or {}).get("why", "")
        self.assertIn("отправка не прошла", why)
        self.assertIn("закрыл чат", why)

    def test_the_journal_keeps_the_failure(self):
        s, _notes = self._run("no_active_orders_in_chat")
        entries = ar.cfg(s).get("log") or []
        self.assertTrue(entries)
        self.assertFalse(entries[0]["ok"])


class ADeadChatDropsOutOfThePoll(unittest.TestCase):
    class Missing:
        async def get_messages(self, cid):
            raise RuntimeError("Resource not found (resource_not_found)")

    def _poll(self, times: int) -> dict:
        s = settings()
        api = self.Missing()
        tm = manager()
        for _ in range(times):
            asyncio.run(tm._check_messages(1, api, s))
        return s

    def test_one_miss_is_not_enough_to_give_up(self):
        """Разовый сбой сети не должен хоронить живой чат."""
        s = self._poll(1)
        self.assertFalse(s["known_order_details"]["77"].get("chat_gone"))

    def test_three_misses_in_a_row_are(self):
        s = self._poll(3)
        self.assertTrue(s["known_order_details"]["77"].get("chat_gone"))

    def test_and_then_the_order_is_no_longer_polled(self):
        s = self._poll(3)
        self.assertEqual(
            _chats_to_poll(s["known_orders"], s["known_order_details"]), [])

    def test_the_screen_says_it_stopped_rather_than_showing_404(self):
        s = self._poll(3)
        self.assertIn("больше не опрашиваю", s["_chat_poll"]["error"])

    def test_a_chat_that_reads_again_forgets_the_misses(self):
        s = settings()
        tm = manager()
        asyncio.run(tm._check_messages(1, self.Missing(), s))
        self.assertEqual(s["known_order_details"]["77"]["chat_misses"], 1)
        ok = FakeAPI([{"id": "1", "sender_type": "buyer", "text": "привет",
                       "created_at": stamp(60)}])
        asyncio.run(tm._check_messages(1, ok, s))
        self.assertNotIn("chat_misses", s["known_order_details"]["77"])


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self


class CB:
    def __init__(self, data="ar:why"):
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.data = data
        self.answers: list[str] = []

    async def answer(self, text="", **kw):
        self.answers.append(text)


def buttons(kb) -> list[str]:
    return [btn.text for row in kb.inline_keyboard for btn in row]


class NightModeIsVisibleBeforeItBites(unittest.TestCase):
    """Режим «только ночью» включается набором «🌙 Ночной ответ» и потом
    молчит весь день. Раньше он был виден только постфактум — в строке
    «последний раз промолчал»."""

    def _show(self, **over):
        import handlers.responders as R
        s = settings()
        s["autoreplies"].update(over)
        s["_chat_poll"] = {"ts": time.time(), "chats": 1, "orders": 1}
        R.get_settings = lambda uid: s
        cb = CB()
        asyncio.run(R.ar_why(cb))
        return s, cb

    def test_the_schedule_is_named_on_the_screen(self):
        _s, cb = self._show(quiet_only=True, from_hour=22, to_hour=9)
        self.assertIn("22:00—09:00", cb.message.texts[-1])

    def test_it_says_plainly_that_the_bot_is_silent_right_now(self):
        hour = time.localtime().tm_hour
        # Окно строим так, чтобы текущий час в него точно не попал.
        _s, cb = self._show(quiet_only=True, from_hour=(hour + 2) % 24,
                            to_hour=(hour + 3) % 24)
        self.assertIn("сейчас бот молчит по расписанию", cb.message.texts[-1])

    def test_the_schedule_can_be_switched_off_right_here(self):
        _s, cb = self._show(quiet_only=True)
        self.assertIn("🕐 Отвечать круглосуточно", buttons(cb.message.kbs[-1]))

    def test_round_the_clock_needs_no_such_line_or_button(self):
        _s, cb = self._show(quiet_only=False)
        self.assertNotIn("Режим:", cb.message.texts[-1])
        self.assertNotIn("🕐 Отвечать круглосуточно", buttons(cb.message.kbs[-1]))

    def test_pressing_it_turns_the_schedule_off(self):
        import handlers.responders as R
        s, _cb = self._show(quiet_only=True)
        R.get_settings = lambda uid: s
        R._save = lambda uid, data: None
        cb2 = CB("ar:why:quiet")
        asyncio.run(R.ar_why_quiet_off(cb2))
        self.assertFalse(s["autoreplies"]["quiet_only"])

    def test_and_the_refreshed_screen_no_longer_offers_it(self):
        import handlers.responders as R
        s, _cb = self._show(quiet_only=True)
        R.get_settings = lambda uid: s
        R._save = lambda uid, data: None
        cb2 = CB("ar:why:quiet")
        asyncio.run(R.ar_why_quiet_off(cb2))
        self.assertNotIn("🕐 Отвечать круглосуточно",
                         buttons(cb2.message.kbs[-1]))

    def test_the_button_answers_the_press_exactly_once(self):
        """Второй answer() Telegram не примет — экран останется висеть."""
        import handlers.responders as R
        s, _cb = self._show(quiet_only=True)
        R.get_settings = lambda uid: s
        R._save = lambda uid, data: None
        cb2 = CB("ar:why:quiet")
        asyncio.run(R.ar_why_quiet_off(cb2))
        self.assertEqual(len(cb2.answers), 1, cb2.answers)


class TheScreenShowsRussianForTheLastSend(unittest.TestCase):
    def _show(self, err: str) -> str:
        import handlers.responders as R
        s = settings()
        ar.log(ar.cfg(s), chat_id="99", text="Ключ придёт.", ok=False, err=err)
        s["_chat_poll"] = {"ts": time.time(), "chats": 1, "orders": 1}
        R.get_settings = lambda uid: s
        cb = CB()
        asyncio.run(R.ar_why(cb))
        return cb.message.texts[-1]

    def test_the_code_is_replaced_by_an_explanation(self):
        text = self._show("There are no active orders in this chat "
                          "(no_active_orders_in_chat)")
        self.assertNotIn("no_active_orders_in_chat", text)
        self.assertIn("закрыл чат", text)

    def test_it_says_this_is_not_the_bot_s_fault(self):
        text = self._show("no_active_orders_in_chat")
        self.assertIn("не поломка бота", text)

    def test_an_ordinary_error_gets_no_such_excuse(self):
        text = self._show("connection reset by peer")
        self.assertNotIn("не поломка бота", text)


class AWatermarkFromAnotherChatDeafensThisOne(unittest.TestCase):
    """Номера сообщений на маркетплейсе сквозные. Проход по чужому чату —
    когда номер чата ещё не был известен и подставлялся номер заказа —
    записывал сюда чужой номер. Он выше всего, что есть в этой переписке,
    и «есть ли что новее» отвечает «нет». Навсегда, и молча.
    """

    def _run(self, watermark: str, rows):
        s = settings()
        s["known_messages"]["77"] = watermark
        api = FakeAPI(rows)
        asyncio.run(manager()._check_messages(1, api, s))
        return s, api

    FRESH = [{"id": "5", "sender_type": "buyer", "text": "где ключ",
              "created_at": stamp(60)}]

    def test_a_poisoned_watermark_no_longer_swallows_the_message(self):
        _s, api = self._run("15488419", self.FRESH)
        self.assertEqual(api.sent, ["Ключ придёт в течение часа."], api.sent)

    def test_the_watermark_is_repaired_to_this_chat_s_own_newest(self):
        s, _api = self._run("15488419", self.FRESH)
        self.assertEqual(s["known_messages"]["77"], "5")

    def test_the_repair_happens_once_not_every_pass(self):
        """Второй проход не должен отвечать на то же самое снова."""
        s = settings()
        s["known_messages"]["77"] = "15488419"
        api = FakeAPI(self.FRESH)
        tm = manager()
        asyncio.run(tm._check_messages(1, api, s))
        asyncio.run(tm._check_messages(1, api, s))
        self.assertEqual(len(api.sent), 1, api.sent)

    def test_an_ordinary_watermark_is_left_alone(self):
        _s, api = self._run("5", self.FRESH)
        self.assertEqual(api.sent, [], "уже зачтённое отвечать заново нельзя")

    def test_a_reversed_api_order_does_not_look_poisoned(self):
        """Ответ от нового к старому — не повод считать отметку чужой."""
        rows = [{"id": "9", "sender_type": "buyer", "text": "где ключ",
                 "created_at": stamp(60)},
                {"id": "3", "sender_type": "buyer", "text": "привет",
                 "created_at": stamp(600)}]
        s, api = self._run("9", rows)
        self.assertEqual(api.sent, [])
        self.assertEqual(s["known_messages"]["77"], "9")


if __name__ == "__main__":
    unittest.main()
