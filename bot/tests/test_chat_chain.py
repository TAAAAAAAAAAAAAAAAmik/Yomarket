"""Цепочка «увидел → узнал → ответил», по одному заказу.

Снаружи все обрывы выглядят одинаково: бот промолчал. А причин пять, и
лечатся они по-разному — заказ вне очереди опроса, сообщение уже зачтено,
заказ закрыт и маркетплейс не примет ответ, правило не совпало, сработала
пауза. /chat_debug обязан назвать то звено, которое порвалось.
"""
from __future__ import annotations

import asyncio
import ast
import os
import pathlib
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

BOT = pathlib.Path(__file__).resolve().parent.parent

import handlers.chats as C           # noqa: E402


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago))


class FakeAPI:
    def __init__(self, rows, error: str = ""):
        self.rows = rows
        self.error = error

    async def get_messages(self, cid):
        if self.error:
            raise RuntimeError(self.error)
        return {"data": self.rows}


def settings(status="paid", *, orders=1, seen="1", rules=True) -> dict:
    known = {"77": status}
    details = {"77": {"chat_id": "99", "seen_at": time.time(),
                      "title": "Ключ Steam"}}
    # Добиваем список свежими заказами, чтобы проверить вытеснение из очереди.
    for i in range(1, orders):
        known[f"9{i:03d}"] = "paid"
        details[f"9{i:03d}"] = {"chat_id": f"8{i:03d}",
                                "seen_at": time.time() + i}
    return {
        "known_orders": known,
        "known_order_details": details,
        "known_messages": {"77": seen} if seen else {},
        "autoreplies": {
            "enabled": True,
            "rules": ([{"id": "r1", "on": True, "keywords": ["долго"],
                        "text": "Извините за ожидание."}] if rules else []),
            "cooldown_min": 0, "max_per_order": 10,
        },
    }


def report(s: dict, api, order="77", chat="99") -> str:
    C.get_settings = lambda uid: s
    return "\n".join(asyncio.run(
        C._chain_report(1, api, order, chat, s["known_order_details"].get(order, {}))))


MSG = [{"id": "5", "sender_type": "buyer", "text": "долго",
        "created_at": stamp(120)}]


class TheQueueIsNamed(unittest.TestCase):
    def test_a_watched_order_says_where_it_stands(self):
        text = report(settings(), FakeAPI(MSG))
        self.assertIn("В очереди опроса", text)
        self.assertIn("1-й", text)

    def test_an_order_pushed_out_by_newer_ones_is_called_out(self):
        """30 заказов, опрашивается 25 — пять старых не читаются вовсе."""
        text = report(settings(orders=30), FakeAPI(MSG))
        self.assertIn("Не опрашивается", text)
        self.assertIn("бот не увидит вовсе", text)

    def test_a_chat_marked_gone_says_so(self):
        s = settings()
        s["known_order_details"]["77"]["chat_gone"] = time.time()
        self.assertIn("несуществующим", report(s, FakeAPI(MSG)))


class TheMessagesAreShownAsTheBotSeesThem(unittest.TestCase):
    def test_a_chat_that_will_not_read_stops_the_report_there(self):
        text = report(settings(), FakeAPI([], error="Resource not found"))
        self.assertIn("Чат не читается", text)
        self.assertNotIn("4️⃣", text)

    def test_service_notes_are_not_counted_as_buyer_letters(self):
        rows = [{"id": "4", "sender_type": "system",
                 "text": "Заказ №1191285 был возвращен!", "created_at": stamp(600)}]
        text = report(settings(seen=""), FakeAPI(rows))
        self.assertIn("Писем покупателя в чате нет", text)

    def test_an_already_counted_message_is_not_called_new(self):
        text = report(settings(seen="5"), FakeAPI(MSG))
        self.assertIn("всё уже зачтено", text)

    def test_a_never_read_chat_warns_the_first_pass_answers_nothing(self):
        text = report(settings(seen=""), FakeAPI(MSG))
        self.assertIn("первый проход только запомнит", text)


class TheRuleIsRunForReal(unittest.TestCase):
    def test_a_matching_keyword_is_named(self):
        text = report(settings(), FakeAPI(MSG))
        self.assertIn("правило «долго»", text)

    def test_no_rule_is_said_plainly(self):
        text = report(settings(rules=False), FakeAPI(MSG))
        self.assertIn("Ни одно правило не совпало", text)

    def test_the_gate_verdict_is_shown(self):
        text = report(settings(), FakeAPI(MSG))
        self.assertIn("Отправка:", text)

    def test_a_stale_letter_is_flagged_before_the_rule(self):
        rows = [{"id": "5", "sender_type": "buyer", "text": "долго",
                 "created_at": stamp(30 * 3600)}]
        text = report(settings(), FakeAPI(rows))
        self.assertIn("бот на такое не отвечает", text)


class AClosedOrderIsTheEndOfTheLine(unittest.TestCase):
    """Возврат — и маркетплейс, скорее всего, откажет. Именно «скорее
    всего»: догадка, поданная как факт, — та самая болезнь, от которой
    этот экран и заводился."""

    def test_a_refunded_order_is_flagged_as_probably_refused(self):
        text = report(settings("refunded"), FakeAPI(MSG))
        self.assertIn("no_active_orders_in_chat", text)
        self.assertIn("скорее всего", text)

    def test_an_unproven_guess_is_not_dressed_up_as_a_fact(self):
        text = report(settings("refunded"), FakeAPI(MSG))
        self.assertIn("ещё не проверялось", text)
        self.assertNotIn("уже отказал", text)

    def test_a_refusal_that_did_happen_is_stated_outright(self):
        """Отказ в журнале — уже не догадка."""
        import autoreply as ar
        s = settings("refunded")
        ar.log(ar.cfg(s), chat_id="99", text="Извините.", ok=False,
               err="There are no active orders in this chat "
                   "(no_active_orders_in_chat)")
        text = report(s, FakeAPI(MSG))
        self.assertIn("уже отказал", text)
        self.assertIn("вручную", text)

    def test_a_refusal_in_another_chat_is_not_borrowed(self):
        import autoreply as ar
        s = settings("refunded")
        ar.log(ar.cfg(s), chat_id="12345", text="Извините.", ok=False,
               err="no_active_orders_in_chat")
        self.assertNotIn("уже отказал", report(s, FakeAPI(MSG)))

    def test_an_active_order_is_not_scared_off(self):
        text = report(settings("paid"), FakeAPI(MSG))
        self.assertIn("Заказ активен", text)

    def test_the_status_is_shown_in_russian(self):
        text = report(settings("refunded"), FakeAPI(MSG))
        self.assertNotIn("refunded", text)


class TheOrderOfMessagesIsNotAssumed(unittest.TestCase):
    """API может отдать переписку от новой к старой. Тогда rows[-1] — самое
    СТАРОЕ письмо, и экран уверенно докладывал про трёхдневную давность,
    пока сегодняшнее сообщение висело непрочитанным."""

    REVERSED = [
        {"id": "15488500", "sender_type": "buyer", "text": "долго",
         "created_at": stamp(120)},
        {"id": "15360529", "sender_type": "buyer", "text": "спасибо",
         "created_at": stamp(69 * 3600)},
    ]

    def test_the_newest_letter_wins_even_when_the_api_sorts_backwards(self):
        text = report(settings(seen="1"), FakeAPI(self.REVERSED))
        self.assertIn("долго", text)
        self.assertIn("0.0 ч назад", text)

    def test_the_newest_id_is_the_largest_not_the_last(self):
        text = report(settings(seen="1"), FakeAPI(self.REVERSED))
        self.assertIn("15488500", text)

    def test_an_unread_letter_is_announced(self):
        text = report(settings(seen="15360529"), FakeAPI(self.REVERSED))
        self.assertIn("Есть непрочитанное", text)


class AWatermarkFromElsewhereIsCalledOut(unittest.TestCase):
    """Отметка выше всего, что есть в чате, глушит переписку навсегда —
    и снаружи это выглядит просто как «бот молчит»."""

    def test_it_is_named_as_the_broken_link(self):
        text = report(settings(seen="15488419"), FakeAPI(MSG))
        self.assertIn("Отметка выше всего", text)
        self.assertIn("не из этой переписки", text)

    def test_an_equal_watermark_is_just_nothing_new(self):
        text = report(settings(seen="5"), FakeAPI(MSG))
        self.assertIn("всё уже зачтено", text)
        self.assertNotIn("Отметка выше", text)


class OurOwnAnswerComingBackIsNotABuyerLetter(unittest.TestCase):
    """Бот отвечал сам себе и доставал «@ник» из собственного вопроса —
    с этого начинался круг покупок в пустоту. Экран обязан показывать
    авторство так же, как его считает фоновый цикл."""

    def _with_fingerprint(self):
        from tasks.manager import _note_sent_text
        s = settings(seen="1")
        _note_sent_text(s, "99", "Спасибо за заказ! Скоро свяжемся с вами.")
        rows = [{"id": "7", "sender_type": "buyer", "created_at": stamp(300),
                 "text": "Спасибо за заказ! Скоро свяжемся с вами."}]
        return report(s, FakeAPI(rows))

    def test_it_is_marked_as_ours(self):
        self.assertIn("🏪", self._with_fingerprint())

    def test_and_is_not_counted_as_a_letter_to_answer(self):
        self.assertIn("Писем покупателя в чате нет", self._with_fingerprint())


class EveryCommandNameIsUnique(unittest.TestCase):
    """/chat_debug был объявлен дважды. Побеждал тот роутер, что подключён
    раньше, и вторая команда молча не работала — без единой ошибки."""

    def _commands(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for path in (BOT / "handlers").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Name) and func.id == "Command"):
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.setdefault(arg.value, []).append(path.name)
        return found

    def test_no_command_is_registered_twice(self):
        dupes = {name: files for name, files in self._commands().items()
                 if len(files) > 1}
        self.assertEqual(dupes, {}, f"одно имя на два обработчика: {dupes}")

    def test_the_chain_report_is_the_one_that_answers_chat_debug(self):
        where = self._commands().get("chat_debug", [])
        self.assertEqual(where, ["chats.py"], where)


if __name__ == "__main__":
    unittest.main()
