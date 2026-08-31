"""Команды из списка Telegram открывают настоящие экраны.

Продавец задал список в BotFather, и Telegram показывает его в поле ввода.
Команда, которой в боте нет, ничего не делает вовсе — а выглядит это как
сломанный бот: человек выбрал пункт меню, и ничего не произошло. Из
тринадцати пунктов работали три.

Отдельное правило: команда обязана звать ТОТ ЖЕ обработчик, что и кнопка
рядом с ней в меню. Второй, урезанный экран под ту же задачу — это два
места, где чинить одну беду, и одно из них обязательно забудут.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                            # noqa: E402
from handlers import commands as C                        # noqa: E402

HANDLERS = pathlib.Path(C.__file__).resolve().parent

# Список из BotFather. Он у продавца на экране, и каждая строка — обещание.
PROMISED = ("menu", "ads", "orders", "chats", "proxy", "balance",
            "stars", "pubg", "stats", "help", "policy", "start")


def run(coro):
    return asyncio.run(coro)


class Sent:
    def __init__(self, text, markup=None):
        self.text, self.markup = text, markup

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text, self.markup = text, reply_markup


class FakeMessage:
    def __init__(self, uid=7):
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


def declared_commands() -> dict:
    """Какие команды объявлены в боте и где — разбором кода, а не догадкой."""
    found: dict[str, list[str]] = {}
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                for sub in ast.walk(dec):
                    if isinstance(sub, ast.Call):
                        name = getattr(sub.func, "id", "")
                        if name == "CommandStart":
                            found.setdefault("start", []).append(path.name)
                        elif name == "Command":
                            for arg in sub.args:
                                if isinstance(arg, ast.Constant):
                                    found.setdefault(arg.value,
                                                     []).append(path.name)
    return found


class EveryPromisedCommandExists(unittest.TestCase):
    """Из тринадцати пунктов списка работали три."""

    def test_all_of_them_are_handled(self):
        have = declared_commands()
        missing = [c for c in PROMISED if c not in have]
        self.assertEqual(missing, [],
                         f"обещаны в Telegram, но молчат: {missing}")

    def test_none_of_them_is_declared_twice(self):
        """Две команды с одним именем — не ошибка, а тишина: побеждает
        роутер, подключённый раньше в main.py."""
        have = declared_commands()
        doubles = {c: have[c] for c in PROMISED if len(have.get(c, [])) > 1}
        self.assertEqual(doubles, {}, f"объявлены дважды: {doubles}")

    def test_the_shortcuts_router_is_connected(self):
        """Роутер, который забыли подключить, — это те же молчащие команды."""
        main = (HANDLERS.parent / "main.py").read_text()
        self.assertIn("dp.include_router(commands.router)", main)
        self.assertLess(main.index("dp.include_router(commands.router)"),
                        main.index("dp.include_router(fallback.router)"),
                        "команды подключены после ловца нажатий")


class ACommandOpensTheSameScreenAsItsButton(unittest.TestCase):
    """Не вторую копию экрана, а тот же самый обработчик."""

    def test_the_shortcuts_call_the_real_handlers(self):
        src = pathlib.Path(C.__file__).read_text()
        for module, func in (("ads", "ads_menu"), ("orders", "show_orders"),
                             ("chats", "chats_hub"),
                             ("balance", "show_balance"),
                             ("stats", "show_stats"),
                             ("plugins", "stars_screen")):
            with self.subTest(func):
                self.assertIn(f"from handlers.{module} import {func}", src)

    def test_the_adapter_sends_first_and_edits_after(self):
        """Экраны написаны под кнопку и говорят `edit_text`. Если каждый
        такой вызов слать новым сообщением, «⏳ Загружаю…» и результат лягут
        в чат по отдельности — и продавец прочитает устаревшее первым."""
        m = FakeMessage()
        cb = C.AsCallback(m, "menu:orders")
        run(cb.message.edit_text("⏳ Загружаю…"))
        run(cb.message.edit_text("готово", reply_markup="кнопки"))
        self.assertEqual(len(m.sent), 1, "экран разъехался на два сообщения")
        self.assertEqual(m.sent[0].text, "готово")
        self.assertEqual(m.sent[0].markup, "кнопки")

    def test_answering_a_command_does_not_explode(self):
        """У кнопки Telegram есть `answer()`, у команды крутиться нечему —
        но экраны его зовут, и он обязан быть."""
        run(C.AsCallback(FakeMessage(), "x").answer())


class WithoutAShopTheSectionSaysSo(unittest.TestCase):
    """Раздел без токена уйдёт в маркетплейс и вернётся с отказом — а
    выглядит это поломкой бота, а не отсутствием доступа."""

    def setUp(self):
        self._token = C.get_token
        C.get_token = lambda uid: ""

    def tearDown(self):
        C.get_token = self._token

    def test_it_names_the_reason_and_offers_to_connect(self):
        m, st = FakeMessage(), FakeState()
        run(C.cmd_orders(m, st))
        self.assertIn("не подключён", m.sent[0].text)

    def test_it_waits_for_a_token_right_there(self):
        m, st = FakeMessage(), FakeState()
        run(C.cmd_ads(m, st))
        self.assertIsNotNone(st.state)


class TheHonestAnswerAboutPubg(unittest.TestCase):
    """AutoPUBG в боте нет. Молчать нельзя — команда обещана в Telegram;
    рисовать экран несуществующего плагина нельзя тем более."""

    def test_it_says_the_plugin_is_not_built(self):
        m = FakeMessage()
        run(C.cmd_pubg(m))
        self.assertIn("пока нет", m.sent[0].text)

    def test_it_names_what_does_work_instead(self):
        m = FakeMessage()
        run(C.cmd_pubg(m))
        said = m.sent[0].text.lower()
        self.assertIn("звёзды", said)
        self.assertIn("гифт-карт", said)

    def test_it_says_what_is_needed_to_add_it(self):
        """Совет обязан быть выполнимым: правило проекта — не советовать
        невозможного."""
        m = FakeMessage()
        run(C.cmd_pubg(m))
        self.assertIn("/apr_stock", m.sent[0].text)

    def test_no_pubg_delivery_is_faked_anywhere(self):
        """Если плагин однажды появится, этот тест упадёт — и правильно:
        значит экран «его нет» пора убирать."""
        from automation import giftcards
        self.assertNotIn("pubg", [c.slug for c in giftcards.CARDS])


class SupportHasSomewhereToWrite(unittest.TestCase):
    def test_the_contact_is_never_empty(self):
        """Экран поддержки без контакта — экран, которого нет."""
        self.assertTrue(storage.get_support_contact().strip())

    def test_it_matches_the_one_in_the_legal_documents(self):
        """Два разных контакта в документах и на экране означали бы, что
        один из них неверный."""
        docs = HANDLERS.parents[1] / "docs" / "legal"
        self.assertIn(storage.get_support_contact(),
                      (docs / "privacy.md").read_text())

    def test_the_screen_carries_what_makes_an_answer_possible(self):
        """Раньше экран просил прислать `/version`. Команда стала
        служебной — просить её значит советовать невозможное, — поэтому код
        сборки печатается прямо здесь. Без него первый вопрос поддержки
        будет «а какая у тебя версия», и починка начнётся с угадывания."""
        from handlers.start import BOT_VERSION
        m = FakeMessage()
        run(C.cmd_help(m))
        said = m.sent[0].text
        self.assertIn(BOT_VERSION, said)
        self.assertNotIn("/version", said)
        self.assertIn(storage.get_support_contact(), said)


class ProxiesAreShownWithoutTheirPasswords(unittest.TestCase):
    """В строке прокси лежат логин и пароль — такой же чужой доступ, как
    куки. Печатать их нельзя."""

    def setUp(self):
        self._ar = storage.get_ar_creds
        self._fr = storage.get_fragment_creds
        storage.get_ar_creds = lambda uid: {
            "proxy": "http://логин:СЕКРЕТ@proxy.example.com:8080"}
        storage.get_fragment_creds = lambda uid: {}

    def tearDown(self):
        storage.get_ar_creds = self._ar
        storage.get_fragment_creds = self._fr

    def test_the_password_is_not_printed(self):
        m = FakeMessage()
        run(C.cmd_proxy(m, FakeState()))
        said = m.sent[0].text
        self.assertNotIn("СЕКРЕТ", said, "пароль от прокси попал на экран")
        self.assertNotIn("логин", said)

    def test_the_address_is_printed(self):
        """Иначе экран не отвечает на вопрос, ради которого открыт."""
        m = FakeMessage()
        run(C.cmd_proxy(m, FakeState()))
        self.assertIn("proxy.example.com:8080", m.sent[0].text)

    def test_both_proxies_are_shown_in_one_place(self):
        """Их два, они настраивались в разных углах бота, и продавец,
        задавший один, о втором не знал."""
        m = FakeMessage()
        run(C.cmd_proxy(m, FakeState()))
        said = m.sent[0].text
        self.assertIn("AppRoute", said)
        self.assertIn("AutoStars", said)


if __name__ == "__main__":
    unittest.main()
