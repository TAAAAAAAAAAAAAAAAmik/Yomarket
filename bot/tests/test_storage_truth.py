"""`/version` не пугает потерей данных там, где они не теряются.

Строка про хранилище была написана под Railway, где диск эфемерный: без
`DATABASE_URL` она объявляла «данные сотрутся при редеплое!». На своём
сервере это неправда — файлы переживают и перезапуск бота, и перезагрузку
машины.

Неправда здесь дороже, чем кажется. Продавец, поверивший предупреждению,
либо заводит базу, которая ему не нужна, либо решает, что боту нельзя
доверить деньги, — и в обоих случаях виноват не он. Правило проекта «бот не
должен врать» относится и к предупреждениям: ложная тревога так же
подрывает доверие к экрану диагностики, как и молчание о настоящей беде.

Проверяется не текст исходника, а ответ, который увидит продавец: экран
вызывается по-настоящему, с подставленным окружением.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")


class FakeUser:
    id = 1


class FakeMessage:
    """Сообщение, запоминающее единственный ответ экрана."""

    def __init__(self):
        self.from_user = FakeUser()
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class Bench(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for name in [k for k in os.environ if k.startswith("RAILWAY_")]:
            del os.environ[name]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def screen(self, railway=False, db=False):
        """Показать /version при заданном окружении и вернуть его текст."""
        import handlers.start as start
        import storage

        if railway:
            os.environ["RAILWAY_ENVIRONMENT"] = "production"
        was_db = storage._USE_DB
        storage._USE_DB = db
        # С поднятым флагом базы экран пойдёт в PostgreSQL по-настоящему, а
        # драйвера в прогоне нет. Подменяем только чтение: проверяем строку
        # про хранилище, а не psycopg2.
        saved = (storage._db_read_raw, storage._read_blob)
        if db:
            storage._db_read_raw = lambda key: None
            storage._read_blob = lambda key: {}
        msg = FakeMessage()
        try:
            asyncio.run(start.cmd_version(msg))
        finally:
            storage._USE_DB = was_db
            storage._db_read_raw, storage._read_blob = saved
        self.assertEqual(len(msg.sent), 1, "экран не ответил")
        return msg.sent[0]


class TheStorageLineTellsTheTruthAboutThisMachine(Bench):
    """Одно и то же хранилище опасно на Railway и безопасно на своём
    сервере. Экран обязан различать эти два случая."""

    def test_on_a_own_server_it_does_not_threaten_to_wipe_the_data(self):
        text = self.screen(railway=False, db=False)
        self.assertNotIn("сотрутся", text,
                         "на своём сервере обещает потерю данных, которой нет")
        self.assertNotIn("эфемерн", text)

    def test_on_a_own_server_it_says_the_files_survive_a_restart(self):
        """Мало не соврать — надо ответить на вопрос, который продавец и
        задаёт: переживут ли данные перезагрузку."""
        text = self.screen(railway=False, db=False)
        self.assertIn("Переживают перезапуск", text)

    def test_on_railway_it_still_warns_loudly(self):
        """Там предупреждение правдиво, и снимать его нельзя: именно так
        на Railway теряли настройки продавцов."""
        text = self.screen(railway=True, db=False)
        self.assertIn("сотрутся", text)
        self.assertIn("DATABASE_URL", text)

    def test_both_cases_name_the_folder(self):
        """Где лежат данные — первое, что спрашивают при переносе и при
        резервной копии."""
        for railway in (False, True):
            with self.subTest(railway=railway):
                self.assertIn("📁", self.screen(railway=railway, db=False))

    def test_a_database_is_reported_as_a_database(self):
        text = self.screen(railway=False, db=True)
        self.assertIn("PostgreSQL", text)
        self.assertNotIn("сотрутся", text)


class RailwayIsRecognisedByItsOwnMarks(Bench):
    """Признак площадки берётся из переменных, которые выставляет она
    сама, — гадать по путям или именам файлов было бы вернее ошибиться."""

    def test_without_railway_marks_the_disk_is_permanent(self):
        import storage
        self.assertFalse(storage.ephemeral_disk())

    def test_any_railway_variable_is_enough(self):
        """Их несколько, и набор со временем менялся; привязка к одной
        конкретной сломалась бы молча."""
        import storage
        for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
                     "RAILWAY_SERVICE_NAME"):
            with self.subTest(name):
                os.environ.pop("RAILWAY_ENVIRONMENT", None)
                os.environ[name] = "x"
                self.assertTrue(storage.ephemeral_disk())
                del os.environ[name]

    def test_a_similar_looking_variable_is_not_railway(self):
        """`RAILWAYS_HOBBY` или `MY_RAILWAY` — не признак площадки."""
        import storage
        os.environ["MY_RAILWAY"] = "x"
        self.assertFalse(storage.ephemeral_disk())


if __name__ == "__main__":
    unittest.main()
