"""Журнал событий в группу владельца — по темам форума.

Владелец завёл группу с темами и хочет видеть там, что происходит: кто
зашёл, кто подключил магазин, кому открылся пробный период, кто просит
счёт, кому выдали подписку.

Три вещи, которые здесь легко сломать, и все три дорогие:

* **Журнал не должен мешать работе.** Отвалившаяся группа не может ронять
  подключение магазина: запись о событии дешевле самого события.
* **Молчание должно объясняться.** Не заданная группа, удалённая тема,
  отобранные права — снаружи одинаково, и без записанной причины разбор
  начинается с гадания.
* **Секреты в журнал не попадают.** В месте, где пишется «подключил
  магазин», токен лежит в соседней переменной.
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

import logs                                               # noqa: E402
import storage                                            # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    """Бот, запоминающий отправленное. Может и отказать."""

    def __init__(self, fail: str = ""):
        self.sent: list[dict] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append({"chat": chat_id, "text": text, **kw})


class User:
    def __init__(self, uid=77, name="Тамик", nick="tamik"):
        self.id, self.full_name, self.username = uid, name, nick


class Bench(unittest.TestCase):
    def setUp(self):
        self._admin = dict(storage._load_admin())
        logs._COMPLAINED.clear()

    def tearDown(self):
        storage._save_admin(self._admin)

    def bind(self, **topics):
        for kind, thread in topics.items():
            storage.set_log_topic(kind, -1002222, thread)


class EachEventGoesToItsOwnTopic(Bench):
    """Темы для того и заведены: пять потоков вместо одной ленты."""

    def test_the_thread_id_is_sent(self):
        self.bind(trial=42)
        bot = FakeBot()
        run(logs.log_event(bot, "trial", ["строка"], user=User()))
        self.assertEqual(bot.sent[0]["message_thread_id"], 42)
        self.assertEqual(bot.sent[0]["chat"], -1002222)

    def test_events_do_not_share_a_thread(self):
        self.bind(trial=42, users=43)
        bot = FakeBot()
        run(logs.log_event(bot, "trial", ["a"]))
        run(logs.log_event(bot, "users", ["b"]))
        self.assertNotEqual(bot.sent[0]["message_thread_id"],
                            bot.sent[1]["message_thread_id"])

    def test_an_unbound_kind_goes_to_the_group_itself(self):
        """Тему могли удалить, не трогая группу. Запись должна уйти в общий
        поток, а не пропасть."""
        self.bind(trial=42)
        bot = FakeBot()
        run(logs.log_event(bot, "payment", ["строка"]))
        self.assertNotIn("message_thread_id", bot.sent[0])
        self.assertEqual(bot.sent[0]["chat"], -1002222)

    def test_the_record_names_the_person(self):
        """Имена меняются и повторяются — выдавать подписку придётся по
        номеру."""
        self.bind(order=1)
        bot = FakeBot()
        run(logs.log_event(bot, "order", ["просит счёт"], user=User(uid=505)))
        self.assertIn("505", bot.sent[0]["text"])
        self.assertIn("@tamik", bot.sent[0]["text"])

    def test_a_bare_user_id_is_enough(self):
        """В половине мест объекта пользователя уже нет — есть только uid."""
        self.bind(account=1)
        bot = FakeBot()
        run(logs.log_event(bot, "account", ["подключил"], user=909))
        self.assertIn("909", bot.sent[0]["text"])


class TheJournalNeverBreaksTheWork(Bench):
    """Запись о подключении магазина дешевле самого подключения."""

    def test_a_refusal_does_not_raise(self):
        self.bind(account=1)
        bot = FakeBot(fail="chat not found")
        self.assertFalse(run(logs.log_event(bot, "account", ["x"])))

    def test_an_unconfigured_group_is_not_an_error(self):
        """Журнал не настроен — это не беда, а выбор владельца."""
        storage.clear_log_target()
        bot = FakeBot()
        self.assertFalse(run(logs.log_event(bot, "users", ["x"])))
        self.assertEqual(bot.sent, [])

    def test_the_reason_is_written_down(self):
        """«Логов нет» без причины — это разбор, начинающийся с гадания."""
        self.bind(account=1)
        run(logs.log_event(FakeBot(fail="bot was kicked"), "account", ["x"]))
        self.assertIn("bot was kicked", storage.get_log_target()["error"])

    def test_a_recovered_journal_forgets_the_old_complaint(self):
        self.bind(account=1)
        run(logs.log_event(FakeBot(fail="kicked"), "account", ["x"]))
        run(logs.log_event(FakeBot(), "account", ["x"]))
        self.assertEqual(storage.get_log_target()["error"], "")

    def test_a_broken_journal_does_not_spam_the_owner(self):
        """Сломанный журнал не должен превращаться в рассылку."""
        self.bind(account=1)
        told = []

        class Bot(FakeBot):
            async def send_message(self, chat_id, text, **kw):
                if chat_id == storage.OWNER_ID:
                    told.append(text)
                    return
                raise RuntimeError("kicked")

        for _ in range(5):
            run(logs.log_event(Bot(), "account", ["x"]))
        self.assertEqual(len(told), 1, f"владельцу написали {len(told)} раз")


class NoSecretEverReachesTheGroup(Bench):
    """В месте, где пишется «подключил магазин», токен лежит рядом."""

    def test_no_call_site_passes_a_secret_looking_name(self):
        """Журнал зовут из нескольких мест, и добавить ещё одно с токеном
        будет так же легко."""
        root = pathlib.Path(storage.__file__).parent
        bad = []
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", "") == "log_event"):
                    dumped = ast.dump(node).lower()
                    for word in ("token", "cookie", "mnemonic", "password",
                                 "api_key", "secret"):
                        if word in dumped:
                            bad.append(f"{path.name}: {word}")
        self.assertEqual(bad, [], f"секрет уходит в журнал: {bad}")


class BindingATopicIsCheckedNotAnnounced(Bench):
    """«Привязал» — не доказательство, как и «HTTP 200»."""

    def test_binding_stores_both_the_group_and_the_thread(self):
        storage.set_log_topic("trial", -100777, 9)
        target = storage.get_log_target()
        self.assertEqual(target["chat"], -100777)
        self.assertEqual(target["topics"]["trial"], 9)

    def test_binding_without_a_thread_clears_it(self):
        """Команда, набранная не в теме, а в общем потоке группы."""
        storage.set_log_topic("trial", -100777, 9)
        storage.set_log_topic("trial", -100777, None)
        self.assertNotIn("trial", storage.get_log_target()["topics"])

    def test_turning_it_off_leaves_nothing_behind(self):
        storage.set_log_topic("trial", -100777, 9)
        storage.clear_log_target()
        self.assertEqual(storage.get_log_target()["chat"], 0)
        self.assertEqual(storage.get_log_target()["topics"], {})


class EveryTopicInTheGroupHasAKind(unittest.TestCase):
    """Темы в группе заведены заранее, и под каждую должен быть вид
    события: тема без записей выглядит сломанным ботом."""

    def test_the_five_topics_are_covered(self):
        kinds = {k for k, _t, _h in logs.KINDS}
        self.assertEqual(kinds, {"users", "account", "trial",
                                 "order", "payment"})

    def test_every_kind_is_actually_written_somewhere(self):
        """Вид, который никто не пишет, — обещанная и пустая тема."""
        root = pathlib.Path(storage.__file__).parent
        written = set()
        for path in root.rglob("*.py"):
            if "tests" in path.parts or path.name == "logs.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", "") == "log_event"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant):
                            written.add(arg.value)
        for kind, title, _hint in logs.KINDS:
            with self.subTest(kind):
                self.assertIn(kind, written, f"в тему «{title}» никто не пишет")


class BindingNeverAnswersWithSilence(unittest.TestCase):
    """`/log_here` в группе не ответил вовсе — и это выглядело как «команды
    не существует».

    Причина была другая: сообщение отправлено «от имени группы». При такой
    отправке Telegram не называет человека — вместо него приходит служебный
    аккаунт, проверка прав не проходит, и обработчик выходил молча. Владелец
    в этот момент чинит не то: перезапускает бота, проверяет версию, ищет
    опечатку в команде.
    """

    def setUp(self):
        self._admin = dict(storage._load_admin())

    def tearDown(self):
        storage._save_admin(self._admin)

    def _say(self, sender_chat=None, uid=storage.OWNER_ID, args=""):
        from handlers import commands as C

        said = []

        class Chat:
            id = -1002222

        class Msg:
            text = f"/log_here {args}".strip()
            chat = Chat()
            message_thread_id = 5
            bot = None

            def __init__(self):
                self.from_user = type("U", (), {"id": uid})()
                self.sender_chat = sender_chat

            async def answer(self, text, **kw):
                said.append(text)

        run(C.cmd_log_here(Msg()))
        return said

    def test_an_anonymous_sender_is_told_why(self):
        said = self._say(sender_chat=object(), args="users")
        self.assertTrue(said, "бот промолчал — снаружи это «команды нет»")
        self.assertIn("от имени группы", said[0])

    def test_an_anonymous_sender_is_told_how_to_fix_it(self):
        """Совет обязан быть выполнимым: правило проекта — не советовать
        невозможного."""
        said = self._say(sender_chat=object(), args="users")
        self.assertIn("анонимн", said[0].lower())

    def test_an_anonymous_sender_binds_nothing(self):
        """Прав мы не проверили — значит и привязывать нельзя."""
        storage.clear_log_target()
        self._say(sender_chat=object(), args="users")
        self.assertEqual(storage.get_log_target()["chat"], 0)

    def test_a_stranger_is_told_and_shown_their_id(self):
        """Владелец мог писать с другого аккаунта — без номера он этого не
        поймёт."""
        said = self._say(uid=999123)
        self.assertTrue(said, "бот промолчал")
        self.assertIn("999123", said[0])

    def test_an_admin_binds_and_gets_an_answer(self):
        storage.clear_log_target()
        said = self._say(args="users")
        self.assertTrue(said)
        self.assertEqual(storage.get_log_target()["topics"].get("users"), 5)

    def test_the_answer_says_whether_the_test_record_arrived(self):
        """«Привязал» — не доказательство: тема могла быть закрыта."""
        storage.clear_log_target()
        said = self._say(args="users")
        # Бота в заглушке нет, значит пробная запись не уйдёт — и об этом
        # обязано быть сказано, а не «готово».
        self.assertIn("не прошла", said[0])


if __name__ == "__main__":
    unittest.main()
