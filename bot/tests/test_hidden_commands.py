"""Служебные команды не для клиента.

`/version` печатал PostgreSQL, Redis, PID процесса, путь к каталогу данных
и имена переменных окружения; `*_debug` печатают сырые ответы маркетплейса
и панели. Продавец купил подписку на сервис, а не экскурсию по серверу.

Здесь проверяется три вещи, и каждая ломалась бы молча:

* **скрытая команда не выполняется** — не «выполняется и печатает меньше»,
  а не доходит до обработчика вовсе;
* **ответ такой же, как на несуществующую команду.** «Эта команда не для
  тебя» рассказывало бы о существовании скрытой части — то есть делало бы
  ровно то, чего мы избегаем;
* **каждая зарегистрированная в боте команда осознанно отнесена к одной из
  двух половин.** Новая диагностика, добавленная через полгода, иначе
  оказалась бы публичной молча, и заметил бы это продавец.
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

import commandlist as CL                                  # noqa: E402
import main as M                                          # noqa: E402
import storage                                            # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Msg:
    def __init__(self, text):
        self.text = text
        self.said: list[str] = []

    async def answer(self, text, **kw):
        self.said.append(text)


class Bench(unittest.TestCase):
    UID = 4242

    def setUp(self):
        self._is_admin = storage.is_admin
        storage.is_admin = lambda uid: False
        self.mw = M.HideSystemCommands()

    def tearDown(self):
        storage.is_admin = self._is_admin

    def send(self, text):
        reached = []

        async def handler(event, data):
            reached.append(event)
            return "выполнено"

        msg = Msg(text)
        run(self.mw(handler, msg,
                    {"event_from_user": type("U", (), {"id": self.UID})()}))
        return msg, bool(reached)


class AClientCannotRunTheDiagnostics(Bench):

    def test_version_does_not_reach_its_handler(self):
        _msg, reached = self.send("/version")
        self.assertFalse(reached, "клиент выполнил /version")

    def test_neither_do_the_commands_that_show_the_bot_itself(self):
        for cmd in ("/order_debug 1218314", "/chat_debug 1076867",
                    "/panel_debug", "/panel_map", "/stats_debug",
                    "/sent", "/scan", "/withdraw_debug"):
            with self.subTest(cmd):
                _msg, reached = self.send(cmd)
                self.assertFalse(reached, f"клиент выполнил {cmd}")

    def test_the_answer_is_the_same_as_for_a_command_that_does_not_exist(self):
        """Разный ответ выдал бы существование скрытой части."""
        hidden, _r = self.send("/version")
        junk, _r = self.send("/nosuchcommand")
        self.assertEqual(hidden.said, junk.said)
        self.assertTrue(hidden.said, "команда ушла в тишину")

    def test_the_answer_does_not_admit_that_the_command_exists(self):
        """«Только для администратора» рассказывает ровно то, что мы прячем:
        что скрытая часть есть. Для продавца команды просто нет."""
        msg, _r = self.send("/version")
        said = " ".join(msg.said).lower()
        for giveaway in ("админ", "только для", "прав", "доступ",
                         "владельц", "служебн", "не для"):
            with self.subTest(giveaway):
                self.assertNotIn(giveaway, said)

    def test_the_answer_names_no_internals(self):
        msg, _r = self.send("/version")
        said = " ".join(msg.said).lower()
        for leak in ("postgres", "redis", "python", "aiogram", "railway",
                     "database_url", "secret_key", "pid", "fsm"):
            with self.subTest(leak):
                self.assertNotIn(leak, said)

    def test_hiding_it_by_name_of_the_bot_does_not_help(self):
        """В группе Telegram шлёт команду как `/version@YoMarketBot`."""
        _msg, reached = self.send("/version@YoMarketBot")
        self.assertFalse(reached)


class TheOwnerKeepsHisTools(Bench):
    """Без диагностики «не работает» неотличимо от «не задеплоено»."""

    def setUp(self):
        super().setUp()
        storage.is_admin = lambda uid: True

    def test_version_still_works_for_the_owner(self):
        _msg, reached = self.send("/version")
        self.assertTrue(reached)

    def test_and_so_does_the_rest_of_it(self):
        for cmd in ("/order_debug 1", "/apr_debug", "/panel_map", "/sent"):
            with self.subTest(cmd):
                _msg, reached = self.send(cmd)
                self.assertTrue(reached)


class TheClientsOwnCommandsStillWork(Bench):

    def test_every_public_command_reaches_its_handler(self):
        for name in sorted(CL.PUBLIC):
            with self.subTest(name):
                _msg, reached = self.send(f"/{name}")
                self.assertTrue(reached, f"/{name} отнят у продавца")

    def test_a_command_addressed_to_the_bot_by_name_is_not_taken_away(self):
        """В группе Telegram шлёт команду как `/menu@YoMarketBot`. Сверка со
        словом целиком, забывшая отрезать `@имя`, отняла бы у продавца ВСЕ
        его команды разом — и выглядело бы это как «команды не существует»,
        то есть ровно как задумано для служебных.
        """
        for text in ("/menu@YoMarketBot", "/orders@YoMarketBot 12"):
            with self.subTest(text):
                _msg, reached = self.send(text)
                self.assertTrue(reached, f"{text} отнято у продавца")

    def test_a_command_with_arguments_too(self):
        _msg, reached = self.send("/watch_chat 1076867 Возвраты")
        self.assertTrue(reached)

    def test_plain_text_is_not_touched(self):
        """Нажатие постоянной клавиатуры — обычный текст, не команда."""
        for text in ("🛒 Заказы", "wli-QQ1", ""):
            with self.subTest(text):
                _msg, reached = self.send(text)
                self.assertTrue(reached)


def _registered_commands() -> set[str]:
    """Все имена команд бота, какими бы они ни были объявлены.

    Одного `Command("x")` мало: `/admin` объявлен как `F.text == "/admin"`,
    и проверка, знающая только первый способ, объявила бы его
    несуществующим.

    Функция общая: по ней и делят команды на публичные и скрытые, и ищут
    советы набрать скрытую. Два перечня расходятся молча — этот как раз и
    разошёлся: `/cat_debug` попала в один и не попала во второй.
    """
    names: set[str] = set()
    root = pathlib.Path(M.__file__).parent
    for f in sorted((root / "handlers").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Command"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and \
                            isinstance(arg.value, str):
                        names.add(arg.value)
            elif isinstance(node, ast.Compare):
                for side in [node.left] + list(node.comparators):
                    if (isinstance(side, ast.Constant)
                            and isinstance(side.value, str)
                            and side.value.startswith("/")
                            and side.value[1:].isidentifier()):
                        names.add(side.value[1:])
    return names


class EveryCommandIsClassified(unittest.TestCase):
    """Список закрытый — иначе новая диагностика окажется публичной молча."""

    @staticmethod
    def _registered() -> set[str]:
        return _registered_commands()

    def test_there_are_commands_to_classify_at_all(self):
        self.assertGreater(len(self._registered()), 30)

    def test_the_public_list_names_only_commands_that_exist(self):
        """Команда в меню, которой нет в боте, — кнопка, ведущая в тишину."""
        registered = self._registered() | {"start"}   # CommandStart()
        for name in sorted(CL.PUBLIC):
            with self.subTest(name):
                self.assertIn(name, registered)

    def test_the_menu_is_a_subset_of_what_is_allowed(self):
        for name, _desc in CL.MENU:
            with self.subTest(name):
                self.assertIn(name, CL.PUBLIC)

    def test_the_owner_menu_names_only_commands_that_exist(self):
        registered = self._registered()
        for name, _desc in M._OWNER_MENU:
            with self.subTest(name):
                self.assertIn(name, registered)

    # Что обязано быть скрыто. Граница проходит не по слову «debug», а по
    # тому, ЧЬЁ устройство команда показывает: здесь — устройство самого
    # бота, его выката, хранилища, разбора ответов маркетплейса и панели.
    MUST_HIDE = (
        "version", "sent", "scan", "log_here",
        "panel_map", "panel_debug", "items_debug", "accounts_debug",
        "order_debug", "orders_debug", "chat_debug", "chats_debug",
        "chat_send_probe", "stats_debug", "ads_debug", "cat_debug",
        "pos_api", "pos_debug", "pos_find", "pos_raw", "promo_debug",
        "restore_debug", "reviews_debug", "withdraw_debug", "withdraw_form",
        "apr_order_probe",
    )

    def test_what_shows_the_bot_itself_is_hidden(self):
        registered = self._registered()
        for name in self.MUST_HIDE:
            with self.subTest(name):
                self.assertIn(name, registered, "команды больше нет — "
                                                "поправь перечень")
                self.assertNotIn(name, CL.PUBLIC)

    def test_the_public_list_is_exactly_this_and_changing_it_is_deliberate(self):
        """Список закрытый. Добавленная в него команда обязана стоить
        отдельной правки здесь — иначе новая диагностика однажды окажется
        публичной, и заметит это продавец.

        Открыты продавцу его собственные дела: заказы, чаты, объявления,
        деньги — и его СОБСТВЕННЫЕ интеграции. Кабинет поставщика и сессия
        Fragment принадлежат ему, чинит их он, и про устройство бота они не
        говорят ничего.
        """
        self.assertEqual(CL.PUBLIC, frozenset({
            # день за днём
            "start", "menu", "orders", "chats", "ads", "balance", "stats",
            "prices", "stars", "keyboard", "help", "policy",
            "proxy", "pubg", "watch_chat", "unwatch_chat",
            "logout", "forget_me",
            # свои поставщики
            "apr_login", "apr_forget", "apr_stock", "apr_balance",
            "apr_item", "apr_whoami", "apr_debug",
            "ns_login", "ns_forget", "ns_stock", "ns_balance",
            # свой Fragment
            "fragment_cookies", "fragment_debug", "fragment_js",
            "stars_probe",
        }))

    def test_the_menu_descriptions_are_filled_in(self):
        for name, desc in CL.MENU:
            with self.subTest(name):
                self.assertTrue(desc.strip(), f"/{name} без описания")
                self.assertLessEqual(len(desc), 256)



class TheMenuTelegramShowsIsSetByTheBot(unittest.TestCase):
    """Пока список команд набивали руками в BotFather, он был один на всех,
    и `/version` вместе со всей диагностикой висел у каждого продавца на
    виду. Теперь его ставит бот, и разъехаться с кодом список не может."""

    def _publish(self, bot):
        run(M.publish_commands(bot))

    class Bot:
        def __init__(s, fail=False):
            s.calls: list = []
            s.fail = fail

        async def set_my_commands(s, cmds, scope=None):
            if s.fail:
                raise RuntimeError("нет связи")
            s.calls.append((type(scope).__name__,
                            [c.command for c in cmds]))

    def test_the_client_sees_only_his_own_commands(self):
        bot = self.Bot()
        self._publish(bot)
        default = [names for scope, names in bot.calls
                   if scope == "BotCommandScopeDefault"]
        self.assertEqual(len(default), 1, bot.calls)
        for name in default[0]:
            with self.subTest(name):
                self.assertIn(name, CL.PUBLIC)

    def test_the_default_scope_is_the_one_that_replaces_botfather(self):
        """Список из BotFather лежит в области по умолчанию. Поставив свой
        в «личные чаты», мы оставили бы прежний в группах — то есть
        служебные команды остались бы на виду там."""
        bot = self.Bot()
        self._publish(bot)
        self.assertIn("BotCommandScopeDefault", [s for s, _n in bot.calls])

    def test_the_owner_keeps_the_diagnostics_in_his_own_menu(self):
        bot = self.Bot()
        self._publish(bot)
        mine = [names for scope, names in bot.calls
                if scope == "BotCommandScopeChat"]
        self.assertEqual(len(mine), 1, bot.calls)
        self.assertIn("version", mine[0])
        for name, _desc in CL.MENU:
            with self.subTest(name):
                self.assertIn(name, mine[0], "у владельца пропали обычные "
                                             "команды")

    def test_a_refusal_from_telegram_does_not_break_the_start(self):
        """Меню — удобство, а не работа: упасть из-за него бот не должен."""
        self._publish(self.Bot(fail=True))

    def test_the_start_actually_publishes_it(self):
        """Функция, которую никто не зовёт, — не исправление. Список тогда
        остаётся тем, что набит в BotFather, и служебные команды у продавца
        на виду; заметить это по зелёным тестам нельзя."""
        tree = ast.parse(pathlib.Path(M.__file__).read_text(encoding="utf-8"))
        started = [fn for fn in ast.walk(tree)
                   if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "main"]
        self.assertEqual(len(started), 1)
        called = {node.func.id for node in ast.walk(started[0])
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)}
        self.assertIn("publish_commands", called)

class ServiceScreensAreHiddenAsWholeThingsNotJustByName(unittest.TestCase):
    """Скрыть команду и оставить её вывод на экране значит не скрыть ничего.

    `/version` продавцу не советуют — а код сборки, который она печатает,
    висел прямо на экране поддержки и на каждом экране отказа. Продавцу он
    не объясняет ничего и рассказывает о боте то, чего тот не спрашивал:
    как часто его пересобирают и по каким веткам.

    Владельцу метка остаётся — он открывает те же экраны у себя.
    """

    def _support_screen(self, uid: int = 7) -> str:
        import handlers.start as S

        class Screen:
            def __init__(s):
                s.text = ""

            async def edit_text(s, text, reply_markup=None, **kw):
                s.text = text

        cb = type("CB", (), {})()
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": uid})()
        cb.answer = lambda *a, **k: asyncio.sleep(0)
        run(S.show_help(cb))
        return cb.message.text

    def _help_command(self, uid: int = 7) -> str:
        import handlers.commands as C
        msg = Msg("/help")

        async def answer(text, reply_markup=None, **kw):
            msg.said.append(text)

        msg.answer = answer
        msg.from_user = type("U", (), {"id": uid})()
        run(C.cmd_help(msg))
        return " ".join(msg.said)

    def test_the_showcase_support_screen_hides_the_build_code(self):
        import handlers.start as S
        text = self._support_screen()
        self.assertNotIn(S.BOT_VERSION, text)
        self.assertNotIn("/version", text)

    def test_the_help_command_hides_it_too(self):
        from handlers.start import BOT_VERSION
        said = self._help_command()
        self.assertNotIn(BOT_VERSION, said)
        self.assertNotIn("/version", said)

    def test_the_admin_sees_the_build_code_on_the_same_screens(self):
        """Проверка, которая ничего не оставляет, не отличает «спрятали» от
        «выкинули»: убрать метку совсем значило бы вернуть починку к
        угадыванию версии."""
        import handlers.start as S
        admin = dict(storage._load_admin())
        try:
            storage.add_admin(4242)
            self.assertIn(S.BOT_VERSION, self._support_screen(4242))
            self.assertIn(S.BOT_VERSION, self._help_command(4242))
        finally:
            storage._save_admin(admin)


class NoScreenAdvisesAHiddenCommand(unittest.TestCase):
    """Совет нажать то, что бот у продавца отнял, — это «не советуйте
    невозможного» в чистом виде: он нажмёт и получит «такой команды нет»."""

    # Перечень не переписывается руками: прежний собирался так, и
    # `/cat_debug` в него не попал — а экран «каталог не читается» её
    # советовал. Список, который надо не забыть пополнить, однажды не
    # пополняют.
    HIDDEN = tuple(sorted("/" + n for n in _registered_commands()
                          if n not in CL.PUBLIC))

    @staticmethod
    def _seller_facing(fn) -> bool:
        """Экраны, до которых продавец доходит: обработчик его собственной
        команды и любой обработчик нажатия.

        Обработчики служебных команд сюда не попадают намеренно: подсказка
        «а ещё есть /chat_debug» внутри самой `/chat_debug` законна — кроме
        владельца, туда никто не заглянет.

        По той же причине не попадает экран, который сам проверяет
        `is_admin`: продавец получит отказ раньше, чем текст.
        """
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and \
                    getattr(node.func, "id", "") == "is_admin":
                return False
        for dec in fn.decorator_list:
            src = ast.dump(dec)
            if "callback_query" in src:
                return True
            for node in ast.walk(dec):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "Command"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and \
                                arg.value in CL.PUBLIC:
                            return True
        return False

    def test_no_seller_facing_screen_tells_him_to_run_one(self):
        """Совет нажать то, чего у продавца нет, — «не советуйте
        невозможного» в чистом виде: он нажмёт и получит «команды нет».

        Проверяются экраны, до которых продавец доходит: обработчики самих
        служебных команд из проверки исключены целиком.
        """
        self.assertEqual(self._offenders(), [],
                         "экран советует служебную команду")

    def _offenders(self, extra: str = "") -> list[str]:
        root = pathlib.Path(M.__file__).parent
        bad: list[str] = []
        files = sorted((root / "handlers").glob("*.py"))
        sources = [(f.name, f.read_text(encoding="utf-8")) for f in files]
        if extra:
            sources.append(("подделка.py", extra))
        for name, src in sources:
            tree = ast.parse(src, filename=name)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not self._seller_facing(fn):
                    continue
                for_owner = self._owner_only(fn)
                for node in ast.walk(fn):
                    if node in for_owner:
                        continue
                    if isinstance(node, ast.Constant) and \
                            isinstance(node.value, str):
                        for cmd in self.HIDDEN:
                            if cmd in node.value:
                                bad.append(f"{name}:{node.lineno} "
                                           f"…{node.value[:60]}…")
        return bad

    @staticmethod
    def _owner_only(fn) -> set:
        """Строки внутри `ui.admin_hint(...)` — они до продавца не доходят.

        Исключение точечное, на аргументы одного вызова, а не на функцию
        целиком: экран, где владельцу советуют `/cat_debug`, продавцу
        по-прежнему не вправе советовать её же строкой рядом.
        """
        inside = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "attr", "") or \
                getattr(node.func, "id", "")
            if fname != "admin_hint":
                continue
            for arg in node.args:
                inside.update(ast.walk(arg))
        return inside

    def test_the_check_would_notice_such_a_screen(self):
        """Проверка, которая ничего не запрещает, — не проверка. Здесь она
        получает экран с советом и обязана его назвать."""
        fake = ('@router.callback_query(F.data == "menu:x")\n'
                'async def show(cb):\n'
                '    await cb.answer("если не работает — жми /version")\n')
        self.assertTrue([x for x in self._offenders(fake)
                         if x.startswith("подделка")])


if __name__ == "__main__":
    unittest.main()
