"""Команда работает всегда — даже поверх незаконченной формы.

Продавец набрал `/fragment_cookies` и получил в ответ «📷 Отправьте фото
(как изображение) или нажмите „Без фото“». За час до этого он начал
создавать объявление и не довёл до конца, а экран, ждущий ввода, ловит
**любое** сообщение — команду в том числе.

Экранов, ждущих ввода, в боте девяносто три. То есть одна брошенная форма
выключала все команды разом, включая всю диагностику: `/version`,
`/order_debug`, `/chat_debug`. И выглядело это как поломка той команды,
которую в этот момент набрали, — та же тишина, что была у команд-двойников,
только причина другая.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import main as M                                  # noqa: E402


class FakeState:
    def __init__(self, current=None, broken=False, data=None,
                 data_broken=False):
        self.current = current
        self.broken = broken
        # Отдельно от `broken`: состояние может читаться, а данные — нет.
        # Со сломанным `get_state` до чтения данных дело не доходит вовсе,
        # и проверка «что если данные не прочитались» проверяла бы не тот
        # `except`.
        self.data_broken = data_broken
        self.cleared = False
        self.data = dict(data or {})

    async def get_state(self):
        if self.broken:
            raise RuntimeError("хранилище FSM недоступно")
        return self.current

    async def get_data(self):
        if self.broken or self.data_broken:
            raise RuntimeError("хранилище FSM недоступно")
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.current = None
        self.data = {}


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.said: list[str] = []

    async def answer(self, text, **kw):
        self.said.append(text)


class Bench(unittest.TestCase):
    def run_it(self, text, state):
        passed: list = []
        self.data = {"state": state,
                     # Ровно то, что кладёт FSM-middleware самого aiogram:
                     # состояние он читает один раз, до нас, и фильтры
                     # сверяются с этим значением, а не с хранилищем.
                     "raw_state": getattr(state, "current", None)}

        async def handler(event, data):
            passed.append(event)
            return "выполнено"

        msg = FakeMessage(text)
        got = asyncio.run(M.CommandsEscapeForms()(handler, msg, self.data))
        return msg, got, bool(passed)


class ACommandGetsThrough(Bench):
    def test_the_half_finished_form_is_closed(self):
        state = FakeState("CreateAdState:waiting_photo")
        self.run_it("/fragment_cookies", state)
        self.assertTrue(state.cleared)

    def test_and_the_command_still_reaches_its_handler(self):
        state = FakeState("CreateAdState:waiting_photo")
        _msg, got, reached = self.run_it("/version", state)
        self.assertTrue(reached)
        self.assertEqual(got, "выполнено")

    def test_the_seller_is_told_the_form_was_dropped(self):
        """Наполовину заполненная форма, исчезнувшая молча, удивляет не
        меньше, чем неработающая команда. «Наполовину заполненная» — это
        буквально: в форме уже лежит введённое."""
        state = FakeState("CreateAdState:waiting_photo",
                          data={"title": "1000 Robux", "price": 990})
        msg, _got, _r = self.run_it("/order_debug 1218314", state)
        self.assertTrue(any("форма закрыта" in t for t in msg.said), msg.said)

    def test_the_state_filter_sees_the_form_as_closed(self):
        """Одного `clear()` мало — и это стоило отдельного круга.

        Фильтр состояния сверяется не с хранилищем, а с `raw_state`, которое
        aiogram прочитал один раз ещё до нас. Форма закрывалась, бот писал
        «выполняю команду», а фильтр видел старое состояние и отдавал
        сообщение той же форме: продавец получал обе строки подряд —
        «форма закрыта» и снова «отправьте фото».
        """
        self.run_it("/fragment_cookies", FakeState("CreateAdState:photo"))
        self.assertIsNone(self.data["raw_state"])

    def test_the_real_aiogram_filter_agrees(self):
        """Проверка не нашим представлением о фильтре, а самим фильтром."""
        from aiogram.filters import StateFilter
        self.run_it("/fragment_cookies", FakeState("CreateAdState:photo"))
        passes = asyncio.run(StateFilter("CreateAdState:photo")(
            FakeMessage("/fragment_cookies"),
            raw_state=self.data["raw_state"]))
        self.assertFalse(passes, "форма всё ещё перехватывает команду")

    def test_an_untouched_form_still_holds_its_state(self):
        self.run_it("обычный текст", FakeState("CreateAdState:photo"))
        self.assertEqual(self.data["raw_state"], "CreateAdState:photo")

    def test_a_command_with_arguments_counts_as_a_command(self):
        state = FakeState("ChatState:waiting_text")
        self.run_it("/chat_debug 1217623", state)
        self.assertTrue(state.cleared)

    def test_a_command_addressed_to_the_bot_counts_too(self):
        state = FakeState("ChatState:waiting_text")
        self.run_it("/version@YouMarketBot", state)
        self.assertTrue(state.cleared)


class OrdinaryInputIsNotTouched(Bench):
    """Форма должна работать как форма: перехватывать всё подряд нельзя."""

    def test_plain_text_leaves_the_form_alone(self):
        state = FakeState("CreateAdState:waiting_title")
        self.run_it("100 звёзд Telegram", state)
        self.assertFalse(state.cleared)

    def test_and_says_nothing(self):
        state = FakeState("CreateAdState:waiting_title")
        msg, _got, _r = self.run_it("просто текст", state)
        self.assertEqual(msg.said, [])

    def test_a_price_is_not_a_command(self):
        state = FakeState("CreateAdState:waiting_price")
        self.run_it("199", state)
        self.assertFalse(state.cleared)

    def test_a_lone_slash_is_not_a_command(self):
        state = FakeState("CreateAdState:waiting_title")
        self.run_it("/", state)
        self.assertFalse(state.cleared)

    def test_a_path_typed_as_text_is_not_a_command(self):
        """«/home/user/фото.jpg» — это текст, а не вызов команды."""
        state = FakeState("CreateAdState:waiting_title")
        self.run_it("/home/user/фото.jpg", state)
        self.assertFalse(state.cleared)

    def test_with_no_form_open_nothing_is_said(self):
        state = FakeState(None)
        msg, _got, reached = self.run_it("/version", state)
        self.assertEqual(msg.said, [])
        self.assertTrue(reached)

    def test_a_message_without_state_still_passes(self):
        _msg, got, reached = self.run_it("/version", None)
        self.assertTrue(reached)
        self.assertEqual(got, "выполнено")


class NothingHereMaySwallowTheMessage(Bench):
    """Middleware, который сам стал причиной тишины, — худший исход."""

    def test_a_broken_fsm_storage_does_not_eat_the_command(self):
        state = FakeState("CreateAdState:waiting_photo", broken=True)
        _msg, got, reached = self.run_it("/version", state)
        self.assertTrue(reached)
        self.assertEqual(got, "выполнено")

    def test_an_unsendable_notice_does_not_eat_it_either(self):
        class Mute(FakeMessage):
            async def answer(self, text, **kw):
                raise RuntimeError("Telegram молчит")

        passed: list = []

        async def handler(event, data):
            passed.append(event)
            return "выполнено"

        state = FakeState("CreateAdState:waiting_photo")
        got = asyncio.run(M.CommandsEscapeForms()(handler, Mute("/version"),
                                                  {"state": state}))
        self.assertEqual(got, "выполнено")
        self.assertTrue(passed)


class ItIsActuallyInstalled(unittest.TestCase):
    """Проверка проводки: middleware может считать верно, а не быть
    подключённым — или быть подключённым не внешним, и тогда состояние
    перехватит сообщение раньше него."""

    def test_the_dispatcher_installs_it_as_an_outer_middleware(self):
        import inspect
        src = inspect.getsource(M.main)
        self.assertIn("dp.message.outer_middleware(CommandsEscapeForms())", src)

    def test_it_is_installed_before_the_others(self):
        import inspect
        src = inspect.getsource(M.main)
        mine = src.index("CommandsEscapeForms()")
        access = src.index("AccessMiddleware()")
        self.assertLess(mine, access)


class TheFormsItRescuesFrom(unittest.TestCase):
    def test_there_really_are_many_of_them(self):
        """Если экранов с состоянием не останется, эта защита станет лишней —
        а пока их девяносто с лишним, и каждый глушит команды."""
        import pathlib
        root = pathlib.Path(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))) / "handlers"
        found = 0
        for path in root.glob("*.py"):
            found += len(re.findall(r"@router\.message\([A-Za-z_]+State\.",
                                    path.read_text(encoding="utf-8")))
        self.assertGreater(found, 50, found)


class TheBotDoesNotAnnounceFormsNobodyOpened(Bench):
    """«↩️ Незаконченная форма закрыта» на КАЖДУЮ команду подряд.

    Живая переписка 31.08: `/admin` → строка про форму, `/menu` → «магазин
    не подключён», `/orders` → снова строка про форму, `/orders` → снова.
    Замкнутый круг, и заводит его сам бот: экран «магазин ещё не подключён»
    взводит ожидание токена (чтобы вставленный токен не пропал), а
    следующая команда честно докладывает, что закрыла эту форму. Продавец
    формы не открывал и ничего в неё не вводил — ему сообщают о событии,
    которого для него не было.

    Правило то же, что и было записано, только теперь оно выполняется
    буквально: сообщают о пропаже НАПОЛОВИНУ ЗАПОЛНЕННОЙ формы. Пустая
    закрывается молча — терять в ней нечего.

    Ожидание токена молчит всегда, даже когда в нём лежит запомненный после
    сорвавшейся проверки токен: эту форму взводит сам бот, на приветствии и
    на каждом экране «магазин не подключён», а не продавец.
    """

    def test_an_empty_form_closes_without_a_word(self):
        state = FakeState("AutoState:waiting_confirm_hours")
        msg, _got, reached = self.run_it("/orders", state)
        self.assertEqual(msg.said, [], "доложили о форме, куда ничего не ввели")
        self.assertTrue(state.cleared, "форма осталась висеть")
        self.assertTrue(reached, "команда не дошла до обработчика")

    def test_the_token_wait_never_says_anything(self):
        """Её взводит бот — на приветствии и на «магазин не подключён»."""
        state = FakeState("AuthState:waiting_for_token")
        msg, _got, reached = self.run_it("/orders", state)
        self.assertEqual(msg.said, [])
        self.assertTrue(reached)

    def test_not_even_with_a_token_remembered_from_a_failed_attempt(self):
        """`retry_token` кладёт токен в форму, чтобы предложить повтор. По
        общему правилу «есть данные — доложи» круг вернулся бы: у продавца
        без магазина ожидание токена взводится каждой командой."""
        state = FakeState("AuthState:waiting_for_token",
                          data={"token": "wli-QQ1"})
        msg, _got, reached = self.run_it("/menu", state)
        self.assertEqual(msg.said, [])
        self.assertTrue(reached)

    def test_a_command_after_the_no_shop_screen_is_answered_once(self):
        """Ровно то, что на скриншоте: на команду приходит ОДИН ответ —
        самой команды, а не два."""
        state = FakeState("AuthState:waiting_for_token")
        msg, _got, _r = self.run_it("/admin", state)
        self.assertEqual(len(msg.said), 0)

    def test_a_half_filled_form_still_says_so(self):
        """Обратная сторона: у мастера объявления введены название и цена —
        молча потерять их нельзя."""
        state = FakeState("CreateAdState:waiting_photo",
                          data={"title": "1000 Robux", "price": 990})
        msg, _got, _r = self.run_it("/version", state)
        self.assertTrue(any("форма закрыта" in t for t in msg.said), msg.said)

    def test_unreadable_form_data_is_not_guessed_to_be_filled(self):
        """Состояние прочиталось, данные — нет. Доложить «форма закрыта»
        значит выдумать, что в ней что-то было; форму при этом всё равно
        надо закрыть, иначе команда снова достанется ей."""
        state = FakeState("CreateAdState:waiting_photo", data_broken=True)
        msg, _got, reached = self.run_it("/version", state)
        self.assertEqual(msg.said, [])
        self.assertTrue(state.cleared, "форма осталась висеть")
        self.assertTrue(reached)

if __name__ == "__main__":
    unittest.main()
