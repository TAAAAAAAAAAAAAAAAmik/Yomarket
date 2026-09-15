"""Плагин, который «пока не показываем», прячется целиком.

Убрать кнопку и оставить команду — это кнопка, о которой знает только тот,
кто её уже видел. Убрать и то и другое, но оставить фоновую выдачу — хуже
всего: она тратит чужие TON, а выключить её продавцу нечем, экрана-то нет.

Владельцу плагин остаётся: он его и доводит. Правило то же, что у кода
сборки и служебных команд — прячется не имя, а всё, что за ним.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import commandlist as CL                                   # noqa: E402
import features                                            # noqa: E402
import storage                                             # noqa: E402
from handlers import commands as C                         # noqa: E402
from handlers import plugins as P                          # noqa: E402
from tasks.manager import TaskManager                      # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Bench(unittest.TestCase):
    SELLER = 7
    OWNER = 4242

    def setUp(self):
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._owner = storage.is_owner
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage.is_owner = lambda uid: False
        storage.add_admin(self.OWNER)
        self._was = features.STARS_HIDDEN
        features.STARS_HIDDEN = True

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage.is_owner = self._owner
        features.STARS_HIDDEN = self._was


class OneSwitchDecidesForEveryone(Bench):
    """Переключатель один на весь бот. Разложенный по местам («тут не
    покажем, там не пустим»), он однажды не сойдётся сам с собой — и
    разойтись это может молча."""

    def test_the_seller_does_not_see_it(self):
        self.assertFalse(features.stars_shown(self.SELLER))

    def test_the_owner_still_does(self):
        """Плагин доводит владелец: спрятать его от себя значит остаться без
        способа его проверить."""
        self.assertTrue(features.stars_shown(self.OWNER))

    def test_nobody_at_all_is_nobody(self):
        """Фоновый проход зовёт с нулём, когда продавца в этом месте нет."""
        self.assertFalse(features.stars_shown(0))

    def test_switching_it_off_shows_it_to_everyone(self):
        features.STARS_HIDDEN = False
        self.assertTrue(features.stars_shown(self.SELLER))


class TheButtonsAndCommandsGoTogether(Bench):

    def test_the_plugins_screen_has_no_stars_button(self):
        data = [b.callback_data
                for row in P._plugins_menu_keyboard({}, self.SELLER)
                .inline_keyboard for b in row]
        self.assertNotIn("plugins:auto_stars", data)

    def test_but_the_owner_keeps_it(self):
        data = [b.callback_data
                for row in P._plugins_menu_keyboard({}, self.OWNER)
                .inline_keyboard for b in row]
        self.assertIn("plugins:auto_stars", data)

    def test_the_other_buttons_do_not_shift(self):
        """Ряды считаются от числа карт, и убранная первая кнопка сдвинула
        бы раскладку на одну — карты встали бы по одной вместо двух."""
        kb = P._plugins_menu_keyboard({}, self.SELLER).inline_keyboard
        self.assertTrue(all(len(row) <= 2 for row in kb))

    def test_the_command_is_silent(self):
        """Ответ тот же, что у несуществующей команды: «этот раздел не для
        тебя» рассказывал бы о существовании скрытого."""
        said = []

        class Msg:
            from_user = type("U", (), {"id": Bench.SELLER})()

            async def answer(s, *a, **kw):
                said.append(a)

        class St:
            async def clear(s):
                return None

        run(C.cmd_stars(Msg(), St()))
        self.assertEqual(said, [])

    def test_the_menu_list_does_not_offer_it(self):
        self.assertNotIn("stars", [n for n, _d in CL.MENU])

    def test_its_diagnostics_go_with_it(self):
        """`/fragment_debug` без экрана — диагностика того, чего продавцу
        негде включить."""
        for name in ("stars_probe", "fragment_debug", "fragment_cookies",
                     "fragment_js"):
            with self.subTest(name):
                self.assertNotIn(name, CL.PUBLIC)


class AButtonFromAnOldMessageIsNotAWayIn(Bench):
    """Раздел был открыт раньше, и кнопки остались в прежних сообщениях.
    Заслон один на все нажатия, а не проверка в каждом из двадцати
    обработчиков."""

    def tap(self, data: str, uid: int):
        seen = {"passed": False, "alerts": []}

        async def handler(event, extra):
            seen["passed"] = True

        cb = type("CB", (), {})()
        cb.data = data
        cb.from_user = type("U", (), {"id": uid})()

        async def answer(text="", **kw):
            seen["alerts"].append(text)

        cb.answer = answer
        run(P._stars_gate(handler, cb, {}))
        return seen

    def test_the_screen_itself_is_closed(self):
        got = self.tap("plugins:auto_stars", self.SELLER)
        self.assertFalse(got["passed"])

    def test_and_so_is_every_button_inside_it(self):
        """Тумблер «включить» тратит деньги — его закрывать обязательно; но
        и «выключить» закрыто, иначе половина раздела осталась бы живой."""
        for data in ("plugins:stars:toggle", "plugins:stars:manual",
                     "plugins:stars:set_mnemonic", "plugins:stars:balance"):
            with self.subTest(data):
                self.assertFalse(self.tap(data, self.SELLER)["passed"])

    def test_the_owner_passes_through(self):
        self.assertTrue(self.tap("plugins:auto_stars", self.OWNER)["passed"])

    def test_other_plugins_are_not_touched(self):
        """Заслон стоит на своём разделе, а не на всех: гифт-карты работают."""
        self.assertTrue(self.tap("plugins:gc:apple", self.SELLER)["passed"])
        self.assertTrue(self.tap("plugins:menu", self.SELLER)["passed"])

    def test_the_dead_button_says_something(self):
        """Кнопка, оставшаяся в загрузке, читается как сломанный бот."""
        self.assertTrue(self.tap("plugins:auto_stars",
                                 self.SELLER)["alerts"])


class TheBackgroundWorkStopsToo(Bench):
    """Спрятать экран и оставить выдачу — худшее из всех: она тратит TON
    продавца, а выключить её ему нечем."""

    def setUp(self):
        super().setUp()
        self.mgr = TaskManager.__new__(TaskManager)
        self.settings = {"plugins": {"auto_stars": {
            "enabled": True, "ask_username": True, "keyword": "",
            "pending": {"55": {"qty": 100, "chat_id": "9"}}}}}
        self.sent: list = []

        async def send(api, chat_id, text, settings, **kw):
            self.sent.append(text)
            return True, ""

        self.mgr._send_chat = send

    def test_no_new_order_is_taken_into_the_queue(self):
        run(self.mgr._maybe_ask_stars_username(
            None, self.settings, "77", "100 звёзд Telegram", "9", "paid",
            self.SELLER))
        self.assertEqual(self.sent, [], "спросил ник по скрытому плагину")

    def test_and_nothing_is_bought_for_the_ones_already_queued(self):
        got = run(self.mgr._maybe_deliver_stars_reply(
            self.SELLER, None, self.settings, "55", "@nick", "9"))
        self.assertFalse(got, "письмо ушло в покупку по скрытому плагину")

    def test_the_owner_is_not_stopped(self):
        """У владельца плагин работает — он его и доводит."""
        run(self.mgr._maybe_ask_stars_username(
            None, self.settings, "77", "100 звёзд Telegram", "9", "paid",
            self.OWNER))
        self.assertTrue(self.sent, "у владельца выдача тоже встала")


if __name__ == "__main__":
    unittest.main()
