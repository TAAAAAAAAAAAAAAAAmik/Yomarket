"""Два мелких вранья, каждое из знакомого класса.

Первое: продавец отвечает покупателю из бота — письмо возвращается из чата
следующим проходом, и бот присылает его же обратно с подписью «СООБЩЕНИЕ ОТ
ПОКУПАТЕЛЯ». Отпечатки своих сообщений ставил только автоответчик, руками
написанное в эту книгу не попадало.

Второе: «Отправлен на модерацию» писалось по коду ответа панели. Nova
отвечает 200 и на отказ, так что товар оставался черновиком, а продавец
считал его отправленным.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import tasks.manager as M      # noqa: E402


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


class API:
    def __init__(self, text: str):
        self.rows = [{"id": "500", "sender_type": "buyer", "text": text,
                      "created_at": stamp(60)}]
        self.sent: list[str] = []

    async def get_messages(self, cid):
        return {"data": self.rows}

    async def send_message(self, cid, text):
        self.sent.append(text)
        return {"ok": True}


def manager():
    tm = M.TaskManager.__new__(M.TaskManager)
    tm.notes: list[str] = []

    async def _notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = _notify
    return tm


def settings() -> dict:
    return {
        "known_orders": {"77": "paid"},
        "known_messages": {"c99": "1"},
        "known_order_details": {"77": {"chat_id": "99", "seen_at": time.time(),
                                       "title": "Ключ Steam"}},
        "notify_messages": {"enabled": True},
        "autoreplies": {"enabled": False},
        "plugins": {"auto_stars": {"enabled": False}},
    }


MINE = "Здравствуйте! Ключ отправлю в течение часа."


class MyOwnReplyIsNotABuyerMessage(unittest.TestCase):
    def _run(self, *, by_hand: bool):
        s = settings()
        if by_hand:
            M._note_sent_by_hand(s, "99", MINE)
        else:
            M._note_sent_text(s, "99", MINE)
        tm = manager()
        asyncio.run(tm._check_messages(1, API(MINE), s))
        return tm.notes

    def test_it_is_never_called_a_buyer_message(self):
        for by_hand in (True, False):
            notes = self._run(by_hand=by_hand)
            self.assertFalse(
                [n for n in notes if "СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ" in n],
                f"by_hand={by_hand}: {notes}")

    def test_a_hand_written_reply_is_shown_as_mine(self):
        notes = self._run(by_hand=True)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("ТЫ ОТВЕТИЛ", notes[0])
        self.assertIn("Ключ отправлю", notes[0])

    def test_an_autoreply_echo_stays_silent(self):
        """Автоответ продавец не писал — эхо ему ни к чему."""
        self.assertEqual(self._run(by_hand=False), [])

    def test_a_real_buyer_message_still_arrives(self):
        s = settings()
        M._note_sent_by_hand(s, "99", MINE)
        tm = manager()
        asyncio.run(tm._check_messages(1, API("а где ключ?"), s))
        self.assertTrue(any("СООБЩЕНИЕ ОТ ПОКУПАТЕЛЯ" in n for n in tm.notes),
                        tm.notes)

    def test_the_fingerprint_is_scoped_to_its_chat(self):
        """Тот же текст в другом чате — чужой."""
        s = settings()
        M._note_sent_by_hand(s, "12345", MINE)
        self.assertFalse(M._is_hand_text(s, "99", MINE))
        self.assertTrue(M._is_hand_text(s, "12345", MINE))

    def test_switching_notifications_off_silences_it_too(self):
        s = settings()
        s["notify_messages"]["enabled"] = False
        M._note_sent_by_hand(s, "99", MINE)
        tm = manager()
        asyncio.run(tm._check_messages(1, API(MINE), s))
        self.assertEqual(tm.notes, [])


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class ARefusalHiddenInASuccessfulAnswer(unittest.TestCase):
    """Nova возвращает 200 и когда действие отказалось: причина лежит в
    поле `danger`. По коду ответа это неотличимо от успеха."""

    def test_a_danger_message_is_a_refusal(self):
        from automation.panel import _nova_refusal
        self.assertIn("остатк", _nova_refusal(
            Resp(200, {"danger": "Нельзя опубликовать: нет остатков"})))

    def test_a_plain_success_is_not(self):
        from automation.panel import _nova_refusal
        self.assertEqual(_nova_refusal(Resp(200, {"message": "Готово"})), "")

    def test_a_body_that_is_not_json_does_not_crash(self):
        from automation.panel import _nova_refusal

        class Broken:
            status_code = 200

            @staticmethod
            def json():
                raise ValueError("no json")

        self.assertEqual(_nova_refusal(Broken()), "")


class ThePublishStateIsComparable(unittest.TestCase):
    def test_status_fields_are_picked_up(self):
        from automation.panel import _publish_state
        got = _publish_state([{"attribute": "status", "value": "draft"},
                              {"attribute": "title", "value": "Ключ"}])
        self.assertIn("status=", got)
        self.assertNotIn("title", got)

    def test_a_record_without_status_gives_nothing_to_compare(self):
        from automation.panel import _publish_state
        self.assertEqual(_publish_state([{"attribute": "title",
                                          "value": "Ключ"}]), "")

    def test_two_different_states_do_not_look_equal(self):
        from automation.panel import _publish_state
        draft = _publish_state([{"attribute": "status", "value": "draft"}])
        live = _publish_state([{"attribute": "status", "value": "moderation"}])
        self.assertNotEqual(draft, live)


class SendingFromTheBotLeavesAFingerprint(unittest.TestCase):
    """Отпечатки ставил только автоответчик. Написанное продавцом руками в
    книгу не попадало — и возвращалось к нему же как письмо покупателя.
    Проверяется через сам обработчик: подмена вспомогательной функции
    прошла бы и без проводки."""

    class FSM:
        def __init__(self, chat_id="99"):
            self.chat_id = chat_id

        async def get_data(self):
            return {"chat_id": self.chat_id}

        async def clear(self):
            pass

    class Msg:
        def __init__(self, text):
            self.text = text
            self.from_user = type("U", (), {"id": 1})()
            self.out: list[str] = []

        async def answer(self, text, **kw):
            self.out.append(text)

    class CB:
        def __init__(self, data):
            self.data = data
            self.from_user = type("U", (), {"id": 1})()
            self.alerts: list[str] = []

        async def answer(self, text="", **kw):
            self.alerts.append(text)

    class API:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, cid, text):
            self.sent.append((str(cid), text))
            return {"ok": True}

    def _wire(self, store: dict):
        import handlers.chats as C
        self._real = (C.get_settings, C.save_settings)
        C.get_settings = lambda uid: store
        C.save_settings = lambda uid, s: store.update(s)
        return C

    def tearDown(self):
        import handlers.chats as C
        if hasattr(self, "_real"):
            C.get_settings, C.save_settings = self._real

    def test_a_typed_reply_is_remembered_as_mine(self):
        store = settings()
        C = self._wire(store)
        api = self.API()
        msg = self.Msg(MINE)
        asyncio.run(C.send_reply(msg, self.FSM("77"), api))
        self.assertTrue(api.sent, "сообщение вообще не ушло")
        self.assertTrue(M._is_hand_text(store, api.sent[0][0], MINE),
                        "отпечаток не поставлен — вернётся как чужое письмо")

    def test_a_quick_reply_is_remembered_too(self):
        store = settings()
        store["quick_replies"] = ["Спасибо за заказ!"]
        C = self._wire(store)
        api = self.API()
        cb = self.CB("qr:77:0")
        asyncio.run(C.send_quick_reply(cb, api))
        self.assertTrue(api.sent, "шаблон не ушёл")
        self.assertTrue(M._is_hand_text(store, api.sent[0][0],
                                        "Спасибо за заказ!"))

    def test_a_send_that_failed_leaves_nothing_behind(self):
        """Не дошло — значит и обратно не вернётся, помечать нечего."""
        store = settings()
        C = self._wire(store)

        class Broken:
            async def send_message(self, cid, text):
                raise RuntimeError("no_active_orders_in_chat")

        asyncio.run(C.send_reply(self.Msg(MINE), self.FSM("77"), Broken()))
        self.assertFalse(M._is_hand_text(store, "99", MINE))


if __name__ == "__main__":
    unittest.main()
