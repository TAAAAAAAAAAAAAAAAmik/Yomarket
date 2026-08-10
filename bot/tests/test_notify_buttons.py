"""Кнопки в уведомлениях нажимаются и куда-то ведут.

Уведомление — единственное место, где продавец видит бота, не открывая его.
Кнопка на карточке, у которой нет обработчика, не даёт ни ошибки, ни ответа:
нажатие уходит в никуда, и снаружи это неотличимо от зависшего бота. Ровно
так молча не работали две команды с одинаковыми именами.

Проверка идёт настоящей маршрутизацией aiogram — теми же фильтрами и в том
же порядке роутеров, что в main.py. Сравнение строк тут бесполезно:
`OrderCallback.filter()` разбирает данные на поля и при лишнем двоеточии
просто не срабатывает, а глазами это не видно.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import os
import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

BOT = pathlib.Path(__file__).resolve().parent.parent

from aiogram.types import CallbackQuery, Chat, Message, User  # noqa: E402

import tasks.manager as M                                      # noqa: E402


def router_order() -> list[str]:
    """Порядок подключения — из самого main.py.

    Переписанный руками список разошёлся бы с приложением, и тест начал бы
    проверять несуществующую конфигурацию. Порядок здесь важен: побеждает
    первый подошедший роутер.
    """
    tree = ast.parse((BOT / "main.py").read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router" and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                names.append(arg.value.id)
    return names


ORDER = router_order()
MODULES = [importlib.import_module(f"handlers.{n}") for n in ORDER]

# Последним подключён fallback, и он ловит ВСЁ: `@router.callback_query(F.data)`.
# Пока он в списке, необработанных нажатий не бывает в принципе — первая
# версия этого теста молча проходила и на заведомо мёртвой кнопке. Разбор
# ведём без него: кнопка, до которой добрался только fallback, и есть мёртвая.
REAL = [(n, m) for n, m in zip(ORDER, MODULES) if n != "fallback"]


def _event(data: str) -> CallbackQuery:
    user = User(id=1, is_bot=False, first_name="t")
    chat = Chat(id=1, type="private")
    msg = Message(message_id=1, date=datetime.now(), chat=chat,
                  from_user=user, text="x")
    return CallbackQuery(id="1", from_user=user, chat_instance="c",
                         data=data, message=msg)


async def _resolve(data: str, *, with_fallback: bool = False
                   ) -> tuple[str, str] | None:
    event = _event(data)
    routers = list(zip(ORDER, MODULES)) if with_fallback else REAL
    for name, mod in routers:
        for handler in mod.router.observers["callback_query"].handlers:
            got = await handler.check(event)
            if (got[0] if isinstance(got, tuple) else got):
                return name, getattr(handler.callback, "__name__", "?")
    return None


def handler_of(data: str) -> tuple[str, str] | None:
    """Кто разберёт нажатие, не считая fallback. None — кнопка мёртвая."""
    return asyncio.run(_resolve(data))


def presses(kb) -> list[str]:
    """Только нажимаемые кнопки: у ссылок обработчика и не должно быть."""
    return [b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data]


class EveryButtonOnANotificationLeadsSomewhere(unittest.TestCase):
    KEYBOARDS = {
        "новый заказ": lambda: M._order_notify_kb("1200750", "1139042"),
        "сообщение покупателя": lambda: M._message_notify_kb("1139042", "1200750"),
        "порог баланса": lambda: M._balance_notify_kb(),
        "чат поддержки": lambda: M._watched_notify_kb("1076867"),
        "пропущенные письма": lambda: M._missed_kb(),
    }

    def test_no_button_falls_through(self):
        dead: list[str] = []
        for what, build in self.KEYBOARDS.items():
            for data in presses(build()):
                if handler_of(data) is None:
                    dead.append(f"{what}: {data}")
        self.assertEqual(dead, [], f"нажатие уходит в никуда — {dead}")

    def test_every_card_actually_has_buttons(self):
        """Пустая клавиатура прошла бы предыдущий тест не поморщившись."""
        for what, build in self.KEYBOARDS.items():
            self.assertTrue(presses(build()) or build().inline_keyboard,
                            f"{what}: кнопок нет вовсе")

    def test_callback_data_fits_telegram_s_limit(self):
        """64 байта. Длиннее — Telegram не примет саму клавиатуру."""
        for what, build in self.KEYBOARDS.items():
            for data in presses(build()):
                self.assertLessEqual(
                    len(data.encode("utf-8")), 64, f"{what}: {data}")


class TheNewOrderCardOffersTheThreeActionsItPromises(unittest.TestCase):
    """Пункт A3 приёмки: карточка с действиями по заказу, и кнопки
    срабатывают."""

    def setUp(self):
        self.kb = M._order_notify_kb("1200750", "1139042")
        self.labels = [b.text for row in self.kb.inline_keyboard for b in row]

    def test_taking_into_work_is_not_offered_in_one_tap(self):
        """Что делает POST /orders/{id}/work на этом маркетплейсе, не
        подтверждено: на тестовом заказе покупатель в ту же минуту увидел
        «магазин сообщил, что выполнил заказ». Такое не вешают на кнопку в
        уведомлении — это заявление перед покупателем и площадкой."""
        self.assertNotIn("▶️ В работу", self.labels)
        self.assertNotIn("order:1200750:work", presses(self.kb))

    def test_and_it_is_gone_from_the_other_screens_too(self):
        """Убирать надо везде, иначе половинчато и непонятно."""
        from keyboards.main import order_actions_keyboard
        for kb in (order_actions_keyboard("1200750", "1139042"),
                   M._message_notify_kb("1139042", "1200750")):
            self.assertFalse(
                [d for d in presses(kb) if d.endswith(":work")], presses(kb))

    def test_the_handler_stays_for_auto_accept(self):
        """Автопринятие ходит тем же путём — обработчик убирать нельзя."""
        self.assertEqual(handler_of("order:1200750:work"),
                         ("orders", "handle_order_action"))

    def test_confirm_is_there_and_wired(self):
        self.assertIn("✅ Подтвердить", self.labels)
        self.assertEqual(handler_of("order:1200750:confirm"),
                         ("orders", "handle_order_action"))

    def test_the_chat_button_opens_the_chat(self):
        self.assertIn("💬 Чат", self.labels)
        self.assertEqual(handler_of("chat:1139042:")[1], "show_chat_messages")

    def test_a_refund_asks_before_doing_it(self):
        """Возврат — деньги: сразу выполнять нельзя, сначала подтверждение."""
        self.assertEqual(handler_of("order:1200750:refundask")[1], "ask_refund")

    def test_the_card_carries_the_chat_id_not_the_order_id(self):
        """Переписка лежит под чатом; подставленный номер заказа открывал бы
        чужой чат или не открывал ничего."""
        self.assertIn("chat:1139042:", presses(self.kb))
        self.assertNotIn("chat:1200750:", presses(self.kb))


class TheFallbackIsLastSoItCatchesOnlyLeftovers(unittest.TestCase):
    def test_it_is_registered_after_everything_else(self):
        self.assertEqual(ORDER[-1], "fallback", ORDER[-5:])

    def test_a_button_nobody_claims_still_gets_an_answer(self):
        """Иначе нажатие пропадает молча, и это неотличимо от поломки."""
        self.assertIsNotNone(
            asyncio.run(_resolve("совершенно:неизвестное:нажатие",
                                 with_fallback=True)))

    def test_and_the_check_above_does_not_count_that_as_wired(self):
        """Проверка мёртвых кнопок обязана не видеть fallback — иначе она
        проходит всегда. Первая версия этого файла так и делала."""
        self.assertIsNone(handler_of("совершенно:неизвестное:нажатие"))


class PressingAnActionSurvivesAFailure(unittest.TestCase):
    """Кнопки карточки ходят в API, а он отвечает и отказами. Текст отказа
    уходит в HTML-сообщение: одиночный «<» роняет всю отправку, и продавец
    видит вечно крутящуюся кнопку вместо причины."""

    class Screen:
        def __init__(self):
            self.texts: list[str] = []

        async def edit_text(self, text, reply_markup=None, **kw):
            self.texts.append(text)
            return self

    class Press:
        def __init__(self):
            self.message = PressingAnActionSurvivesAFailure.Screen()
            self.from_user = type("U", (), {"id": 1})()
            self.data = "order:1200750:work"

        async def answer(self, text="", **kw):
            pass

    class API:
        def __init__(self, error=None):
            self.error = error
            self.called: list[str] = []

        async def work_order(self, oid):
            self.called.append(str(oid))
            if self.error:
                raise RuntimeError(self.error)
            return {"ok": True}

    def _press(self, error=None):
        from keyboards.main import OrderCallback
        import handlers.orders as O
        cb = self.Press()
        api = self.API(error)
        asyncio.run(O.handle_order_action(
            cb, OrderCallback(order_id="1200750", action="work"), api))
        return cb.message.texts[-1], api

    def test_a_successful_press_says_so(self):
        text, api = self._press()
        self.assertIn("взят в работу", text)
        self.assertEqual(api.called, ["1200750"])

    def test_an_angle_bracket_in_the_error_cannot_break_the_message(self):
        """Ровно так однажды упало сообщение с «<ник>» внутри."""
        text, _api = self._press("unexpected <tag> in response")
        self.assertNotIn("<tag>", text)
        self.assertIn("&lt;tag&gt;", text)

    def test_a_known_refusal_is_explained_in_russian(self):
        text, _api = self._press("no_active_orders_in_chat")
        self.assertIn("закрыл чат", text)
        self.assertNotIn("no_active_orders", text)

    def test_the_failure_does_not_pretend_to_have_worked(self):
        text, _api = self._press("boom")
        self.assertNotIn("взят в работу", text)
        self.assertIn("Не получилось", text)


class TakingIntoWorkReportsWhatActuallyHappened(unittest.TestCase):
    """«Брать заказы в работу» — так называется кнопка. Что при этом делает
    маркетплейс, из кода не видно, и бот раньше просто назначал статус
    «work» сам. Если на деле заказ уходит в «выполнен», продавец обязан
    увидеть это в ту же минуту, а не узнать от покупателя."""

    class API:
        """Подменяет YooMarketAPI целиком: _process_orders создаёт его сам."""

        after = "work"
        worked: list = []

        def __init__(self, token=None):
            pass

        async def start(self):
            pass

        async def close(self):
            pass

        async def get_orders(self, cursor=None):
            return {"data": [{"id": "1200750", "status": "paid",
                              "title": "50 звёзд", "price": 60,
                              "chat_id": "1139042"}]}

        async def get_order(self, oid):
            return {"data": {"id": str(oid), "status": type(self).after,
                             "title": "50 звёзд", "price": 60,
                             "chat_id": "1139042"}}

        async def work_order(self, oid):
            type(self).worked.append(str(oid))
            return {"ok": True}

        async def send_message(self, cid, text):
            return {"ok": True}

        async def get_messages(self, cid):
            return {"data": []}

    def _run(self, after):
        import tasks.manager as MM
        self.API.after = after
        self.API.worked = []
        real_api = MM.YooMarketAPI
        MM.YooMarketAPI = self.API
        try:
            tm = MM.TaskManager.__new__(MM.TaskManager)
            tm.notes = []

            async def notify(uid, text, **kw):
                tm.notes.append(text)

            tm._notify = notify
            s = {"known_orders": {"999": "paid"},   # не первый проход
                 "known_order_details": {}, "orders_initialized": True,
                 "auto_accept": {"enabled": True},
                 "notify_orders": {"enabled": True},
                 "auto_reply": {"enabled": False},
                 "plugins": {"auto_stars": {"enabled": False}}}
            asyncio.run(tm._process_orders(1, "tok", s))
            return s, self.API.worked, tm.notes
        finally:
            MM.YooMarketAPI = real_api

    def test_the_order_is_taken_into_work(self):
        _s, worked, _notes = self._run("work")
        self.assertEqual(worked, ["1200750"])

    def test_the_status_is_read_back_not_assumed(self):
        """Раньше здесь стояло `status = "work"` — догадка без проверки."""
        s, _w, _notes = self._run("success")
        self.assertEqual(s["known_orders"]["1200750"], "success")

    def test_a_surprising_outcome_reaches_the_card(self):
        _s, _w, notes = self._run("success")
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertIn("взят в работу автоматически", card)
        self.assertIn("Выполнен", card)

    def test_the_ordinary_outcome_is_reported_too(self):
        _s, _w, notes = self._run("work")
        card = next(n for n in notes if "НОВАЯ ПОКУПКА" in n)
        self.assertIn("взят в работу автоматически", card)


class TheAutoStarsScreenHasNoDeadButtons(unittest.TestCase):
    """Экран AutoStars — тот, откуда идёт выдача за чужие деньги.

    Кнопку сюда добавляют чаще всего, а маршрут забывают: нажатие тогда
    молча ловит fallback, и продавец видит пустоту вместо ответа.
    """

    def _kb(self):
        from handlers.plugins import _stars_keyboard
        return _stars_keyboard({"plugins": {"auto_stars": {"enabled": True}}})

    def test_every_button_leads_somewhere(self):
        for data in presses(self._kb()):
            self.assertIsNotNone(handler_of(data), f"мёртвая кнопка: {data}")

    def test_the_nick_check_is_on_the_screen(self):
        self.assertIn("plugins:stars:whois", presses(self._kb()))

    def test_the_nick_check_is_wired(self):
        self.assertIsNotNone(handler_of("plugins:stars:whois"))


if __name__ == "__main__":
    unittest.main()
