"""Переезд бота на PostgreSQL: скрипт установки и утилита переноса.

Живой прогон 30.08 поймал здесь поломку самого дорогого сорта — бодрый
отчёт об успехе при потерянных данных. Перенос запускался с одним
`DATABASE_URL`, без `DATA_DIR`; утилита смотрела в каталог по умолчанию,
не находила там ничего и честно писала «пусто в файлах», а скрипт после
этого переключал бота на ПУСТУЮ базу и печатал «✅ Готово». Снаружи —
бот, потерявший всех продавцов, при живых и целых файлах, о которых никто
не знает.

Второй находкой стал `Type=notify` у службы базы: готовая сборка
PostgreSQL, которая ставится без прав администратора, собрана без
поддержки systemd (ни libsystemd, ни sd_notify в двоичном файле). Служба
ждала бы уведомления, которого не будет, полторы минуты, считалась бы
упавшей — и бот крутился бы в перезапусках при работающей базе.

Тесты ниже держат оба случая и разбор `db_tool.py` — на настоящих данных,
без сети и без PostgreSQL: проверяется то, что можно проверить здесь.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SETUP = ROOT / "scripts" / "setup-postgres-user.sh"
TOOL = ROOT / "scripts" / "db_tool.py"


class TheMoveScriptCarriesTheSellersDataOver(unittest.TestCase):
    """Перенос обязан идти с окружением бота, а его итог — сверяться."""

    def setUp(self):
        self.sh = SETUP.read_text()

    def test_it_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", str(SETUP)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_migration_runs_with_the_bots_environment(self):
        """Без `.env` утилита не знает, где лежат файлы, и переносит пустоту.

        Проверяется само место запуска: перед вызовом `migrate` обязано
        подгружаться окружение бота. Искать «ENV_FILE» по всему файлу
        нельзя — он там в каждом втором шаге.
        """
        block = self.sh[self.sh.index('"$TOOL" migrate') - 400:
                        self.sh.index('"$TOOL" migrate')]
        self.assertIn('. "$ENV_FILE"', block,
                      "перенос запускается без окружения бота — "
                      "он не найдёт файлы и молча перенесёт пустоту")

    def test_the_migration_is_checked_not_believed(self):
        """«Перенёс» — не доказательство, как и «HTTP 200»."""
        self.assertIn("НЕ доехали до базы разделы", self.sh)
        self.assertIn("SELECT k FROM kv_store", self.sh)

    def test_a_failed_migration_leaves_the_bot_on_files(self):
        """Если перенос не сошёлся, DATABASE_URL вписывать нельзя: бот
        уедет на пустую базу, а данные останутся в файлах."""
        env_line = self.sh.index("printf 'DATABASE_URL=%s")
        check = self.sh.index("перенос не сошёлся")
        self.assertLess(check, env_line,
                        "адрес базы вписывается раньше, чем сверен перенос")
        self.assertIn("НЕ трогаю, бот работает по-старому", self.sh)

    def test_the_database_service_does_not_promise_systemd_support(self):
        """Сборка PostgreSQL без libsystemd + Type=notify = служба, которая
        «упала», работая. Проверено на двоичном файле 30.08."""
        unit = self.sh[self.sh.index("yomarket-db.service\" <<UNITEOF"):
                       self.sh.index("WantedBy=default.target")]
        # Строки настройки, а не комментарии: объяснение «не Type=notify»
        # содержит эти же слова, и проверка по тексту прошла бы при живой
        # ошибке. Ровно так первая версия этого теста и упала.
        directives = [ln for ln in unit.splitlines()
                      if ln and not ln.lstrip().startswith("#")]
        types = [ln for ln in directives if ln.startswith("Type=")]
        self.assertEqual(types, ["Type=simple"], f"тип службы: {types}")

    def test_started_is_not_mistaken_for_ready(self):
        """Без ожидания сокета After= означает «процесс порождён», и бот
        стартует раньше, чем база начнёт отвечать."""
        self.assertIn("ExecStartPost=", self.sh)
        self.assertIn(".s.PGSQL.5432", self.sh)

    def test_the_bot_is_ordered_after_the_database(self):
        self.assertIn("After=yomarket-db.service", self.sh)
        # Надстройка, а не правка чужого файла: setup-user.sh перезапишет
        # свой при следующем запуске.
        self.assertIn("yomarket.service.d", self.sh)

    def test_an_existing_cluster_is_never_reinitialised(self):
        """initdb поверх кластера с данными — это потеря всего."""
        self.assertIn('if [ -f "$PGDATA/data/PG_VERSION" ]', self.sh)
        self.assertIn("не трогаю", self.sh)

    def test_a_foreign_database_address_is_not_overwritten_silently(self):
        self.assertIn("не подменяю его молча", self.sh)

    def test_the_database_is_not_exposed_to_the_network(self):
        self.assertIn("listen_addresses=''", self.sh)
        self.assertIn("--auth-host=reject", self.sh)
        self.assertIn("--auth-local=peer", self.sh)

    def test_it_never_calls_sudo_or_apt(self):
        body = "\n".join(re.sub(r"(?<!\\)#.*$", "", ln)
                         for ln in self.sh.splitlines())
        hits = re.findall(
            r"(?:^\s*|[;&|]\s*|\$\(\s*|\bthen\s+|\bdo\s+|\belse\s+)"
            r"(sudo|apt-get|apt)\b", body, re.M)
        self.assertEqual(hits, [], f"скрипт вызывает {set(hits)}")

    def test_the_first_backup_is_taken_at_once(self):
        """Расписание, ни разу не сработавшее, — обещание, а не копия."""
        self.assertIn('"$BACKUP_SH" ||', self.sh)

    def test_no_secret_is_baked_into_it(self):
        self.assertIsNone(re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", self.sh))
        addresses = [a for a in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.sh)
                     if not a.startswith("127.")]
        self.assertEqual(addresses, [])


class TheToolAsksTheBotWhereTheDataIs(unittest.TestCase):
    """Список разделов берётся у самого хранилища, а не переписан рядом."""

    def setUp(self):
        self._env = dict(os.environ)
        self.dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("DATABASE_URL", None)
        import storage
        self.storage = importlib.reload(storage)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        import storage
        importlib.reload(storage)

    def _tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("db_tool", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_every_store_the_bot_has_is_known_to_the_tool(self):
        """Переписанный список имён разошёлся бы с кодом при первом же
        новом разделе, и перенос потерял бы его целиком — молча."""
        self.assertEqual(set(self._tool().blobs()),
                         set(self.storage._BLOBS))

    def test_the_paths_follow_the_data_dir(self):
        """Утилита обязана смотреть туда же, куда бот. Ровно на этом и
        сломался живой прогон."""
        for path in self._tool().blobs().values():
            self.assertTrue(path.startswith(self.dir),
                            f"{path} лежит не в каталоге данных бота")

    def test_unpacking_a_backup_restores_readable_stores(self):
        """Путь на случай, когда база не поднимается: данные не должны
        оказаться в заложниках у сервера, который не стартует."""
        backup = os.path.join(self.dir, "copy.json")
        with open(backup, "w") as fh:
            json.dump({"tokens": {"555": "токен"},
                       "settings": {"555": {"pause": 30}}}, fh)
        tool = self._tool()
        tool.cmd_unpack(backup)
        self.assertEqual(self.storage.get_token(555), "токен")
        self.assertEqual(self.storage._read_blob("settings"),
                         {"555": {"pause": 30}})

    def test_an_unpacked_store_is_not_world_readable(self):
        """В этих файлах токены маркетплейса и шифротекст seed-фраз."""
        backup = os.path.join(self.dir, "copy.json")
        with open(backup, "w") as fh:
            json.dump({"tokens": {"555": "токен"}}, fh)
        self._tool().cmd_unpack(backup)
        mode = os.stat(self.storage._BLOBS["tokens"]).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"права {oct(mode)}")

    def test_an_unknown_store_in_a_backup_is_skipped_not_crashed(self):
        """Копия могла быть снята с более новой версии бота."""
        backup = os.path.join(self.dir, "copy.json")
        with open(backup, "w") as fh:
            json.dump({"tokens": {"555": "токен"}, "чего_то_новое": {"1": 2}}, fh)
        self._tool().cmd_unpack(backup)
        self.assertEqual(self.storage.get_token(555), "токен")

    def test_it_says_what_to_do_without_a_database_address(self):
        tool = self._tool()
        with self.assertRaises(SystemExit):
            tool.connect()


if __name__ == "__main__":
    unittest.main()
