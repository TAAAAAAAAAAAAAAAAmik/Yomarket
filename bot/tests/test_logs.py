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


class TheNewUsersTopicGetsOneRecordPerPerson(unittest.TestCase):
    """Тема называется «🔔 Новые пользователи · первый заход в бота», а в
    неё уходило по записи на КАЖДОЕ `/start`.

    Строк было две. «Вернулся, магазин не подключён» — событие без
    действия: сделать по нему нечего, а приходило оно каждый раз, когда
    человек открывал бота, то есть чаще всего именно тогда, когда он
    растерян и жмёт `/start` подряд. В этом шуме тонули записи, ради
    которых журнал и заведён, — просьбы о счёте и оплаты.

    Вторая, «Зашёл в бота впервые», решалась по отметке пробного периода —
    то есть означала «пробу не брал». Не бравший её видел «впервые» и на
    пятый свой заход. Считать это первым заходом нельзя: единственная
    цифра, ради которой тема и заведена, — сколько людей вообще дошло до
    бота.
    """

    UID = 5150

    def setUp(self):
        import handlers.start as S
        self.S = S
        self.admin: dict = {}
        self.blobs: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._read, self._write = storage._read_blob, storage._write_blob
        self._owner, self._log = storage.is_owner, logs.log_event
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage._read_blob = lambda n: (self.admin if n == "admin"
                                        else self.blobs.setdefault(n, {}))
        storage._write_blob = lambda n, d: (self.admin.update(d) if n == "admin"
                                            else self.blobs.__setitem__(n, d))
        storage.is_owner = lambda uid: False
        self.written: list[tuple[str, str]] = []

        async def catch(bot, kind, lines, **kw):
            self.written.append((kind, " ".join(lines)))
            return True

        logs.log_event = catch
        import handlers.start as _s
        _s.logs = logs

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage._read_blob, storage._write_blob = self._read, self._write
        storage.is_owner, logs.log_event = self._owner, self._log

    def start(self, uid=None):
        class St:
            async def clear(self):
                return None

            async def set_state(self, x):
                return None

            async def update_data(self, **kw):
                return None

        class Msg:
            def __init__(s, uid):
                s.from_user = type("U", (), {"id": uid, "full_name": "Т",
                                             "username": "t"})()
                s.bot = None

            async def answer(s, text, reply_markup=None, **kw):
                return None

        run(self.S.cmd_start(Msg(self.UID if uid is None else uid), St()))

    def _users(self) -> list[str]:
        return [line for kind, line in self.written if kind == "users"]

    def test_the_first_visit_is_written_down(self):
        self.start()
        self.assertEqual(len(self._users()), 1)
        self.assertIn("впервые", self._users()[0])

    def test_the_second_one_is_not(self):
        """Заход тот же самый человек делает десятками — записью он быть
        перестал."""
        self.start()
        self.start()
        self.start()
        self.assertEqual(len(self._users()), 1,
                         f"журнал повторяется: {self._users()}")

    def test_coming_back_is_not_an_event_at_all(self):
        """«Вернулся, магазин не подключён» — строка, по которой владельцу
        нечего сделать."""
        self.start()
        self.start()
        self.assertNotIn("ернулся", " ".join(self._users()))

    def test_a_different_person_is_a_new_record(self):
        """Отметка личная: общая означала бы «один зашёл — остальных не
        считаем»."""
        self.start()
        self.start(uid=6000)
        self.assertEqual(len(self._users()), 2)

    def test_the_trial_does_not_decide_who_is_new(self):
        """Прежняя версия спрашивала отметку пробы, то есть писала
        «впервые» всякому, кто пробу не брал."""
        self.start()
        storage.note_trial(self.UID, "free")
        self.start()
        self.assertEqual(len(self._users()), 1)

    def test_forgetting_the_person_forgets_the_mark_too(self):
        """Список тех, кого бот видел, — тоже данные о человеке, и держать
        его после «удали мои данные» значит нарушить обещание из политики.
        Оговорки там всего две, и это не одна из них."""
        self.start()
        storage.purge_user(self.UID)
        self.start()
        self.assertEqual(len(self._users()), 2)


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


class AWrongBindingIsUndoableWithoutLosingTheRest(Bench):
    """«Сделал не в том чате» обязано чиниться, и не ценой остального.

    Номер темы принадлежит СВОЕЙ группе: в другой он означает другую тему
    или не означает ничего. Поэтому команда, выполненная в чужом чате, не
    добавляет вторую группу, а переносит журнал целиком — и четыре
    оставшиеся привязки начинают указывать в никуда. Молча это делать
    нельзя: снаружи журнал просто перестанет писаться, причём частично.
    """

    def test_repeating_in_the_right_topic_overwrites(self):
        """Самая частая отмена: ошибся темой — повтори в нужной."""
        storage.set_log_topic("trial", -100111, 5)
        storage.set_log_topic("trial", -100111, 9)
        self.assertEqual(storage.get_log_target()["topics"]["trial"], 9)

    def test_one_kind_can_be_unbound_alone(self):
        """Иначе одна ошибка стоила бы четырёх правильных привязок."""
        storage.set_log_topic("trial", -100111, 5)
        storage.set_log_topic("users", -100111, 6)
        self.assertTrue(storage.clear_log_topic("trial"))
        left = storage.get_log_target()["topics"]
        self.assertNotIn("trial", left)
        self.assertEqual(left.get("users"), 6)

    def test_unbinding_something_unbound_says_so(self):
        self.assertFalse(storage.clear_log_topic("payment"))

    def test_binding_in_another_chat_drops_the_stale_ones(self):
        """Оставить их — значит писать номера тем одной группы в другую."""
        storage.set_log_topic("trial", -100111, 5)
        storage.set_log_topic("users", -100111, 6)
        dropped = storage.set_log_topic("payment", -100222, 7)
        self.assertEqual(dropped, ["trial", "users"])
        target = storage.get_log_target()
        self.assertEqual(target["chat"], -100222)
        self.assertEqual(target["topics"], {"payment": 7})

    def test_the_move_is_reported_not_done_silently(self):
        """Тихий переезд оставил бы владельца с журналом, который пишет
        только одну тему из пяти, — и без единого слова о причине."""
        storage.set_log_topic("trial", -100111, 5)
        self.assertEqual(storage.set_log_topic("users", -100222, 7), ["trial"])

    def test_rebinding_in_the_same_chat_drops_nothing(self):
        """Обычная работа не должна выглядеть как переезд."""
        storage.set_log_topic("trial", -100111, 5)
        self.assertEqual(storage.set_log_topic("users", -100111, 6), [])
        self.assertEqual(len(storage.get_log_target()["topics"]), 2)

    def test_the_first_binding_ever_drops_nothing(self):
        storage.clear_log_target()
        self.assertEqual(storage.set_log_topic("trial", -100111, 5), [])


if __name__ == "__main__":
    unittest.main()
