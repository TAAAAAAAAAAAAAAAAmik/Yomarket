"""Постоянная клавиатура под полем ввода — только основное.

Меню бота живёт в сообщениях, и чтобы вернуться к заказам, надо сначала
найти нужное сообщение в переписке. Клавиатура снизу не уезжает вверх
вместе с историей.

Две вещи здесь ломаются тихо, и обе выглядят как сломанная кнопка:

* **Нажатие приходит обычным текстом, а не командой.** Значит недописанная
  форма съедает его так же, как съедала команды, — а экранов, ждущих
  ввода, в боте девяносто три. Кнопка нажата, ничего не открылось, и
  виновата будто бы кнопка.
* **Подпись и маршрут живут в разных местах.** Переименовали подпись —
  обработчик, ловящий нажатие ПО ТЕКСТУ, перестал совпадать, и кнопка
  молча умерла.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                            # noqa: E402
from handlers import commands as C                        # noqa: E402
from keyboards.reply import (BY_LABEL, LABELS,             # noqa: E402
                             MAIN_BUTTONS, main_reply_keyboard)

BOT = pathlib.Path(storage.__file__).parent


def run(coro):
    return asyncio.run(coro)


class Sent:
    def __init__(self, text, markup=None):
        self.text, self.markup = text, markup


class FakeMessage:
    def __init__(self, text="", uid=7):
        self.text = text
        self.from_user = type("U", (), {"id": uid})()
        self.sent: list[Sent] = []
        self.bot = None

    async def answer(self, text, reply_markup=None, **kw):
        s = Sent(text, reply_markup)
        self.sent.append(s)
        return s


class FakeState:
    def __init__(self):
        self.state = None

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.state = None


class OnlyTheMainSectionsAreOnIt(unittest.TestCase):
    """Постоянная клавиатура занимает треть экрана телефона: восемь пунктов
    главного меню сделали бы её ширмой вместо подспорья."""

    def test_it_stays_small(self):
        self.assertLessEqual(len(MAIN_BUTTONS), 5,
                             "клавиатура закроет собой переписку")

    def test_it_carries_the_everyday_sections(self):
        kinds = {kind for _label, kind in MAIN_BUTTONS}
        self.assertEqual(kinds, {"orders", "chats", "balance", "stats", "menu"})

    def test_the_way_to_everything_else_is_on_it(self):
        """Иначе разделы, не попавшие на клавиатуру, теряются."""
        self.assertIn("menu", {kind for _l, kind in MAIN_BUTTONS})

    def test_no_row_is_left_with_one_lonely_button(self):
        """Одинокая кнопка в паре с соседней вдвое уже и читается как
        обрезанная."""
        rows = main_reply_keyboard().keyboard
        self.assertEqual([len(r) for r in rows], [2, 2, 1])

    def test_it_does_not_hide_after_a_tap(self):
        kb = main_reply_keyboard()
        self.assertTrue(kb.resize_keyboard)
        self.assertTrue(kb.is_persistent)


class EveryButtonActuallyOpensSomething(unittest.TestCase):
    """Кнопка, которая нажимается и молчит, хуже отсутствующей."""

    def test_every_label_has_a_route(self):
        for label, kind in MAIN_BUTTONS:
            with self.subTest(label):
                self.assertEqual(BY_LABEL[label], kind)

    def test_every_kind_is_a_real_command(self):
        """Виды совпадают с именами команд — значит и обработчик у них
        общий, а не вторая копия экрана."""
        declared = set()
        for path in sorted((BOT / "handlers").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                for dec in getattr(node, "decorator_list", []):
                    for sub in ast.walk(dec):
                        if (isinstance(sub, ast.Call)
                                and getattr(sub.func, "id", "") == "Command"):
                            declared |= {a.value for a in sub.args
                                         if isinstance(a, ast.Constant)}
        for _label, kind in MAIN_BUTTONS:
            with self.subTest(kind):
                self.assertIn(kind, declared)

    def test_a_tap_reaches_the_screen(self):
        was = C.get_token
        C.get_token = lambda uid: "токен"
        try:
            m, st = FakeMessage("📋 Меню"), FakeState()
            run(C.kb_tap(m, st))
            self.assertTrue(m.sent, "нажатие ничего не открыло")
        finally:
            C.get_token = was

    def test_a_tap_without_a_shop_explains_itself(self):
        was = C.get_token
        C.get_token = lambda uid: ""
        try:
            m, st = FakeMessage("🛒 Заказы"), FakeState()
            run(C.kb_tap(m, st))
            self.assertIn("не подключён", m.sent[0].text)
        finally:
            C.get_token = was


class ATapEscapesAnUnfinishedForm(unittest.TestCase):
    """Нажатие — обычный текст, а экранов, ждущих ввода, девяносто три."""

    def test_the_middleware_knows_the_labels(self):
        src = (BOT / "main.py").read_text()
        self.assertIn("LABELS", src,
                      "форма съест нажатие так же, как съедала команды")

    def test_the_escape_covers_both_commands_and_labels(self):
        """Проверяется сама строка условия: побег только по команде вернул
        бы прежнюю беду для кнопок."""
        src = (BOT / "main.py").read_text()
        cond = re.search(r"if state is not None and \((.*?)\):", src, re.S)
        self.assertIsNotNone(cond, "не нашёл условие побега из формы")
        self.assertIn("_COMMAND_RE", cond.group(1))
        self.assertIn("_KB_LABELS", cond.group(1))


class ItIsShownOnceAndCanBeRemoved(unittest.TestCase):
    """Постоянная клавиатура не исчезает сама: слать её на каждый /menu
    значит сыпать в переписку пустые сообщения. А кнопка, которую нельзя
    убрать, — навязанный интерфейс, а не удобство."""

    def setUp(self):
        self._settings = storage.get_settings(7)

    def tearDown(self):
        storage.save_settings(7, self._settings)

    def _show(self):
        import handlers.start as S
        m = FakeMessage(uid=7)
        run(S._show_keyboard(m))
        return m

    def test_it_arrives_with_an_explanation_not_an_empty_line(self):
        s = storage.get_settings(7)
        s.pop("reply_keyboard_shown", None)
        s["reply_keyboard"] = True
        storage.save_settings(7, s)
        m = self._show()
        self.assertTrue(m.sent)
        self.assertIn("/keyboard", m.sent[0].text)
        self.assertIsNotNone(m.sent[0].markup)

    def test_it_is_not_resent_every_time(self):
        s = storage.get_settings(7)
        s.pop("reply_keyboard_shown", None)
        s["reply_keyboard"] = True
        storage.save_settings(7, s)
        self._show()
        self.assertEqual(self._show().sent, [], "прислал повторно")

    def test_a_seller_who_removed_it_is_left_alone(self):
        s = storage.get_settings(7)
        s["reply_keyboard"] = False
        s.pop("reply_keyboard_shown", None)
        storage.save_settings(7, s)
        self.assertEqual(self._show().sent, [], "вернул убранную клавиатуру")

    def test_the_toggle_turns_it_off_and_on(self):
        s = storage.get_settings(7)
        s["reply_keyboard"] = True
        storage.save_settings(7, s)
        run(C.cmd_keyboard(FakeMessage(uid=7)))
        self.assertFalse(storage.get_settings(7)["reply_keyboard"])
        run(C.cmd_keyboard(FakeMessage(uid=7)))
        self.assertTrue(storage.get_settings(7)["reply_keyboard"])

    def test_switching_it_off_actually_removes_it(self):
        """Ответ «убрал» без снятия клавиатуры — то же враньё, что и
        «✅ поднято: 0»."""
        from aiogram.types import ReplyKeyboardRemove
        s = storage.get_settings(7)
        s["reply_keyboard"] = True
        storage.save_settings(7, s)
        m = FakeMessage(uid=7)
        run(C.cmd_keyboard(m))
        self.assertIsInstance(m.sent[0].markup, ReplyKeyboardRemove)

    def test_switching_it_back_on_brings_it_with_the_answer(self):
        """Клавиатура приезжает прямо с ответом `/keyboard`, а не следующим
        сообщением: два сообщения подряд ради одной кнопки — мусор."""
        from aiogram.types import ReplyKeyboardMarkup
        s = storage.get_settings(7)
        s["reply_keyboard"] = False
        storage.save_settings(7, s)
        m = FakeMessage(uid=7)
        run(C.cmd_keyboard(m))
        self.assertIsInstance(m.sent[0].markup, ReplyKeyboardMarkup)

    def test_after_the_toggle_the_menu_does_not_send_it_twice(self):
        """`/keyboard` уже показал её — `/menu` следом обязан промолчать."""
        import handlers.start as S
        s = storage.get_settings(7)
        s["reply_keyboard"] = False
        storage.save_settings(7, s)
        run(C.cmd_keyboard(FakeMessage(uid=7)))   # включили, клавиатура ушла
        m = FakeMessage(uid=7)
        run(S._show_keyboard(m))
        self.assertEqual(m.sent, [], "прислал клавиатуру вторым сообщением")


if __name__ == "__main__":
    unittest.main()
