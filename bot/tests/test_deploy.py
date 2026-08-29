"""Выкат на сервер: сверяет ли он, что поднялась новая версия.

Правило проекта «HTTP 200 — не доказательство» здесь читается так:
**«перезапустил» — не доказательство**. Молчаливый успешный выкат, после
которого работает старая сборка, неотличим от сломанной функции — и разбор
такого случая начинается с вопроса «а код-то доехал?», на который у продавца
нет ответа. Поэтому скрипт заканчивается сверкой версии, и проверяется здесь
именно она.

Скрипт запускается по-настоящему, во временном репозитории, с подставным
health-эндпоинтом: проверять текст bash-файла глазами — это проверять прозу,
а не следствие.

Отдельно проверяется `health_payload`: если он перестанет называть версию,
сверять выкату будет нечем, и все проверки ниже станут бессмысленными.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEPLOY = os.path.join(REPO_ROOT, "scripts", "deploy.sh")


class TheHealthAnswerCarriesTheVersion(unittest.TestCase):
    """Без версии в ответе выкат не сможет отличить «код доехал» от
    «поднялась старая сборка» — и любая его проверка станет обещанием."""

    def test_it_names_the_running_version(self):
        import main
        from handlers import start
        self.assertEqual(main.health_payload()["version"], start.BOT_VERSION)

    def test_it_says_it_is_alive(self):
        import main
        self.assertEqual(main.health_payload()["status"], "ok")

    def test_it_says_where_the_data_lives(self):
        """JSON-файлы на бесплатном хостинге стираются при выкате — узнать об
        этом лучше до того, как пропадут настройки."""
        import main
        was = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "postgresql://x"
            self.assertEqual(main.health_payload()["storage"], "postgres")
            os.environ.pop("DATABASE_URL")
            self.assertEqual(main.health_payload()["storage"], "files")
        finally:
            if was is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = was


class _Health(BaseHTTPRequestHandler):
    version = "не задана"

    def do_GET(self):                                   # noqa: N802
        # `ensure_ascii=False` — как у настоящего сервера: подставка,
        # отвечающая иначе, проверяла бы не то, что работает в боте.
        body = json.dumps({"status": "ok", "version": type(self).version},
                          ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                          # тишина в тестах
        pass


class DeployCase(unittest.TestCase):
    """Общая заготовка: настоящий репозиторий и настоящий запуск скрипта."""

    def setUp(self):
        if not shutil.which("git") or not shutil.which("bash"):
            self.skipTest("нет git или bash")
        self.dir = tempfile.mkdtemp()
        self.origin = os.path.join(self.dir, "origin.git")
        self.work = os.path.join(self.dir, "server")
        self.server = None
        self.thread = None
        self._make_repo()

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- репозиторий ------------------------------------------------------
    def git(self, *args, cwd=None):
        return subprocess.run(("git",) + args, cwd=cwd or self.work,
                              capture_output=True, text=True, check=True)

    def write_version(self, version, cwd=None):
        path = os.path.join(cwd or self.work, "bot", "handlers", "start.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'BOT_VERSION = "{version}"\n')

    def _make_repo(self):
        subprocess.run(["git", "init", "--bare", "-q", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.work], check=True)
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.write_version("версия-старая")
        self.git("add", "-A")
        self.git("commit", "-qm", "первый")
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "-q", "origin", "main")
        # Голый репозиторий заводится с HEAD на master, а ветка у нас main —
        # без этого `git clone` не выкладывает файлы вовсе.
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                       cwd=self.origin, check=True)

    def push_new_version(self, version="версия-новая"):
        """Коммит «с сервера не виден» — как настоящий пуш из другой сессии."""
        clone = os.path.join(self.dir, "clone")
        subprocess.run(["git", "clone", "-q", self.origin, clone], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=clone, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
        self.write_version(version, cwd=clone)
        # Отдельный файл — чтобы коммит состоялся и тогда, когда версию
        # намеренно не меняли: именно этот случай и надо проверить.
        with open(os.path.join(clone, "изменение.txt"), "a", encoding="utf-8") as f:
            f.write("правка\n")
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", "новая версия"], cwd=clone, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)
        shutil.rmtree(clone, ignore_errors=True)

    # -- health -----------------------------------------------------------
    def serve_version(self, version):
        _Health.version = version
        self.server = HTTPServer(("127.0.0.1", 0), _Health)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/health"

    # -- запуск -----------------------------------------------------------
    def deploy(self, health_url="", restart="true", **extra):
        env = {**os.environ,
               "DEPLOY_PATH": self.work,
               "DEPLOY_BRANCH": "main",
               "HEALTH_URL": health_url,
               "DEPLOY_RESTART_CMD": restart,
               **extra}
        env.pop("PORT", None)
        return subprocess.run(["bash", DEPLOY], env=env, capture_output=True,
                              text=True, timeout=180)

    def version_on_disk(self):
        with open(os.path.join(self.work, "bot", "handlers", "start.py"),
                  encoding="utf-8") as f:
            return f.read().split('"')[1]


class ADeployIsOnlyDoneWhenTheNewVersionAnswers(DeployCase):
    def test_the_matching_version_is_reported_as_success(self):
        self.push_new_version()
        url = self.serve_version("версия-новая")
        got = self.deploy(url)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("версия-новая", got.stdout)
        self.assertIn("✅", got.stdout)

    def test_the_code_is_actually_pulled(self):
        self.push_new_version()
        self.deploy(self.serve_version("версия-новая"))
        self.assertEqual(self.version_on_disk(), "версия-новая")

    def test_the_restart_command_really_runs(self):
        """Иначе «выкачено» означало бы только «git pull прошёл»."""
        mark = os.path.join(self.dir, "перезапущено")
        self.push_new_version()
        self.deploy(self.serve_version("версия-новая"),
                    restart=f"touch {mark}")
        self.assertTrue(os.path.exists(mark))


class AnOldVersionAfterRestartIsAFailureNotASuccess(DeployCase):
    """Самая дорогая поломка этого проекта — бодрый отчёт об успехе там, где
    ничего не произошло. Выкат, после которого работает старая сборка, — её
    прямой случай: в чате «обновлено», в боте прежний код."""

    def test_it_fails_loudly(self):
        self.push_new_version()
        url = self.serve_version("версия-старая")      # новая не поднялась
        got = self.deploy(url)
        self.assertNotEqual(got.returncode, 0)
        self.assertNotIn("✅", got.stdout)

    def test_it_says_which_version_it_expected_and_which_it_got(self):
        self.push_new_version()
        got = self.deploy(self.serve_version("версия-старая"))
        self.assertIn("версия-новая", got.stdout)
        self.assertIn("версия-старая", got.stdout)

    def test_the_code_is_rolled_back_so_repo_and_bot_mean_the_same(self):
        self.push_new_version()
        self.deploy(self.serve_version("версия-старая"))
        self.assertEqual(self.version_on_disk(), "версия-старая")

    def test_the_rollback_can_be_switched_off_for_a_post_mortem(self):
        self.push_new_version()
        got = self.deploy(self.serve_version("версия-старая"), NO_ROLLBACK="1")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.version_on_disk(), "версия-новая")

    def test_a_silent_health_endpoint_is_not_taken_for_success(self):
        """Не ответил — значит неизвестно, а не «получилось»."""
        self.push_new_version()
        got = self.deploy("http://127.0.0.1:9/health")   # никто не слушает
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("не подтверждено", got.stdout + got.stderr)


class WhatCannotBeCheckedIsNotClaimed(DeployCase):
    def test_without_a_health_url_it_admits_the_version_was_not_verified(self):
        self.push_new_version()
        got = self.deploy("")
        self.assertEqual(got.returncode, 0)
        self.assertIn("не сверена", got.stdout)

    def test_and_it_says_where_to_look_instead(self):
        self.push_new_version()
        self.assertIn("/version", self.deploy("").stdout)

    def test_an_unchanged_version_is_flagged_as_unverifiable(self):
        """Код изменился, а BOT_VERSION нет — сверять будет нечем, и об этом
        надо сказать заранее, а не после неудачного разбора."""
        self.push_new_version(version="версия-старая")
        got = self.deploy(self.serve_version("версия-старая"))
        self.assertIn("BOT_VERSION", got.stdout)


class TheHealthPortIsFoundWhereItActuallyLives(DeployCase):
    """На сервере порт бота лежит в `.env`, а в окружении того, кто запускает
    выкат, его нет. Без этого выкат всегда отвечал «версия не сверена» — то
    есть проверка была написана и не работала ни у кого."""

    def env_with_port(self, port):
        with open(os.path.join(self.work, ".env"), "w", encoding="utf-8") as f:
            f.write(f"BOT_TOKEN=1:x\nPORT={port}\n")

    def deploy_without_health(self, **extra):
        env = {**os.environ, "DEPLOY_PATH": self.work, "DEPLOY_BRANCH": "main",
               "DEPLOY_RESTART_CMD": "true", **extra}
        env.pop("PORT", None)
        env.pop("HEALTH_URL", None)
        return subprocess.run(["bash", DEPLOY], env=env, capture_output=True,
                              text=True, timeout=180)

    def test_the_port_from_env_is_used_for_the_check(self):
        self.push_new_version()
        url = self.serve_version("версия-новая")
        self.env_with_port(url.rsplit(":", 1)[-1].split("/")[0])
        got = self.deploy_without_health()
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn(".env", got.stdout)
        self.assertNotIn("не сверена", got.stdout)

    def test_an_old_version_is_still_caught_that_way(self):
        """Иначе порт нашёлся бы, а толку от него не было."""
        self.push_new_version()
        url = self.serve_version("версия-старая")
        self.env_with_port(url.rsplit(":", 1)[-1].split("/")[0])
        self.assertNotEqual(self.deploy_without_health().returncode, 0)

    def test_without_a_port_anywhere_it_admits_it_did_not_check(self):
        """Угадывать порт нельзя: промах превратил бы удачный выкат в
        «бот не ответил», то есть в ложную тревогу."""
        self.push_new_version()
        got = self.deploy_without_health()
        self.assertEqual(got.returncode, 0)
        self.assertIn("не сверена", got.stdout)

    def test_the_compose_file_actually_gives_the_bot_that_port(self):
        """Проводка: скрипт может искать порт сколько угодно, если бот его
        не слушает."""
        path = os.path.join(REPO_ROOT, "docker-compose.yml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("PORT:", text)
        self.assertIn("127.0.0.1:", text, "порт health не должен смотреть наружу")


class LocalWorkOnTheServerIsNeverThrownAway(DeployCase):
    """`git reset --hard` в выкате молча выбрасывает правки, сделанные на
    сервере, и найти их потом негде. Отказ с объяснением лучше."""

    def test_local_commits_stop_the_deploy_with_an_explanation(self):
        self.push_new_version()
        with open(os.path.join(self.work, "чужое.txt"), "w") as f:
            f.write("правка прямо на сервере")
        self.git("add", "-A")
        self.git("commit", "-qm", "правка на сервере")
        got = self.deploy(self.serve_version("версия-новая"))
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("git status", got.stdout + got.stderr)

    def test_and_that_local_work_survives(self):
        self.push_new_version()
        with open(os.path.join(self.work, "чужое.txt"), "w") as f:
            f.write("правка прямо на сервере")
        self.git("add", "-A")
        self.git("commit", "-qm", "правка на сервере")
        self.deploy(self.serve_version("версия-новая"))
        self.assertTrue(os.path.exists(os.path.join(self.work, "чужое.txt")))


class ItRefusesRatherThanGuess(DeployCase):
    def test_an_unknown_runtime_is_named_not_papered_over(self):
        """Скрипт не делает вид, что перезапустил то, чего не нашёл.

        Во временном репозитории нет ни compose-файла, ни службы, которая
        ссылалась бы на него, ни запущенного `python main.py` — то есть ровно
        случай «не понял, чем запущено».
        """
        self.push_new_version()
        env = {**os.environ, "DEPLOY_PATH": self.work, "DEPLOY_BRANCH": "main"}
        env.pop("DEPLOY_RESTART_CMD", None)
        env.pop("PORT", None)
        got = subprocess.run(["bash", DEPLOY], env=env, capture_output=True,
                             text=True, timeout=120)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("не понял, чем запущен", got.stdout + got.stderr)

    def test_a_missing_repository_is_reported_at_once(self):
        got = subprocess.run(
            ["bash", DEPLOY],
            env={**os.environ, "DEPLOY_PATH": os.path.join(self.dir, "нет-такого")},
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("нет каталога", got.stdout + got.stderr)


class TheWorkflowRunsThisVeryScript(unittest.TestCase):
    """Проводка: скрипт может быть хорош, а запускаться из workflow может
    что-то другое — и тогда проверенного здесь на сервере не окажется."""

    def workflow(self) -> str:
        path = os.path.join(REPO_ROOT, ".github", "workflows", "deploy-bot.yml")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_it_feeds_the_script_from_the_commit_being_deployed(self):
        self.assertIn("< scripts/deploy.sh", self.workflow())

    def test_it_refuses_to_pretend_when_the_secrets_are_missing(self):
        """Зелёная галочка на невыполненном выкате — то же враньё."""
        text = self.workflow()
        self.assertIn("SSH_HOST", text)
        self.assertIn("exit 1", text)

    def test_the_key_is_removed_afterwards(self):
        self.assertIn("rm -f ~/.ssh/id_deploy", self.workflow())

    def test_the_script_is_executable(self):
        self.assertTrue(os.access(DEPLOY, os.X_OK),
                        "scripts/deploy.sh без права на запуск")


class TheRootlessSetupScriptMatchesTheCode(unittest.TestCase):
    """`scripts/setup-user.sh` ставит бота там, где нет ни sudo, ни root.

    Такой сервер попался при переезде 29.08: у пользователя нет прав, пароль
    root неизвестен, системного Python с модулем venv нет. Скрипт обходится
    без администратора: свой Python через `uv`, код архивом, служба в
    профиле пользователя.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def setUp(self):
        self.sh = (self.ROOT / "scripts" / "setup-user.sh").read_text()

    def test_it_is_valid_shell(self):
        r = subprocess.run(["bash", "-n",
                            str(self.ROOT / "scripts" / "setup-user.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_it_refuses_to_run_as_root(self):
        # От root правильный скрипт другой: он заведёт СИСТЕМНУЮ службу,
        # которая переживает перезагрузку без плясок с linger.
        self.assertIn('[ "$(id -u)" != 0 ]', self.sh)
        self.assertIn("setup-server.sh", self.sh)

    def test_it_never_calls_sudo_or_apt(self):
        """Весь смысл скрипта в том, что прав нет: один такой ВЫЗОВ — и он
        падает ровно там, где должен был помочь.

        Ищется команда, а не слово. Первая версия запрещала подстроку
        «apt » где угодно и запретила заодно совет «попросите админа:
        apt install cron» — то есть проверяла прозу. Совет админу с правами
        законен; беззаконен вызов.
        """
        import re
        # Строки без комментариев: комментарий про apt ничего не выполняет.
        body = "\n".join(re.sub(r"(?<!\\)#.*$", "", ln)
                         for ln in self.sh.splitlines())
        # Позиция команды: начало строки, после ;/&/| или внутри $( ).
        hits = re.findall(
            r"(?:^\s*|[;&|]\s*|\$\(\s*|\bthen\s+|\bdo\s+|\belse\s+)"
            r"(sudo|apt-get|apt)\b",
            body, re.M)
        self.assertEqual(hits, [], f"скрипт вызывает {set(hits)}")

    def test_it_brings_its_own_python(self):
        self.assertIn("uv python install", self.sh)

    def test_uv_has_a_second_source(self):
        """astral.sh может быть недоступен у провайдера, а GitHub нужен и так.

        Проверяется строка СКАЧИВАНИЯ, а не любое упоминание адреса: первая
        версия этого теста проходила по ссылке из текста ошибки «неизвестная
        архитектура» — то есть запасной источник можно было выбросить, и
        тест бы этого не заметил.
        """
        self.assertIn("astral.sh/uv/install.sh", self.sh)
        self.assertIn("releases/latest/download/uv-", self.sh)

    def test_it_works_without_git(self):
        # git на том сервере не установлен, и поставить его нечем.
        self.assertIn("codeload.github.com", self.sh)

    def test_data_lives_outside_the_code_directory(self):
        """Повторный запуск перекачивает код поверх — данные внутри него
        стёрлись бы вместе со старой версией.

        Проверяется само присваивание. Первая версия искала «yomarket-data»
        по всему файлу и проходила по строке из шапки с описанием
        переменных: каталог данных можно было увести внутрь кода, и тест
        молчал бы.
        """
        import re
        line = re.search(r'^DATA="\$\{DATA_DIR:-([^}]*)\}"', self.sh, re.M)
        self.assertIsNotNone(line, "не нашёл, куда кладутся данные")
        self.assertNotIn("$REPO", line.group(1),
                         "данные лежат внутри каталога кода")
        self.assertIn("$HOME", line.group(1))

    def test_it_never_overwrites_an_existing_env(self):
        self.assertIn('if [ -f "$ENV_FILE" ]', self.sh)

    def test_it_asks_for_the_encryption_key_and_says_why(self):
        self.assertIn("SECRET_KEY", self.sh)
        self.assertIn("открытым текстом", self.sh)

    def test_it_says_when_autostart_is_weaker_than_promised(self):
        """Без linger служба гаснет при выходе из SSH; cron не поднимет бота
        после падения. И то и другое — «работает», но по-разному.

        Проверяются сами предупреждения. Первая версия искала слово
        «linger» где угодно и проходила по комментарию — то есть
        предупреждение можно было убрать незаметно.
        """
        self.assertIn("linger включить не удалось", self.sh)
        self.assertIn("loginctl enable-linger", self.sh)
        self.assertIn("сам не", self.sh)

    def test_it_checks_the_version_and_the_polling_before_declaring_success(self):
        self.assertIn("поднялась версия", self.sh)
        self.assertIn("НЕ получает сообщения", self.sh)

    def test_no_secret_is_baked_into_it(self):
        import re
        self.assertIsNone(re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", self.sh))
        addresses = [a for a in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.sh)
                     if not a.startswith("127.")]
        self.assertEqual(addresses, [])


class TheFirstTimeSetupScriptMatchesTheCode(unittest.TestCase):
    """`scripts/setup-server.sh` поднимает бота на чистом сервере.

    Скрипт живёт отдельно от кода и разойтись с ним может молча: путь до
    `main.py` переехал, ключ в ответе `/health` переименовали — а узнаётся
    это на живом переезде, когда бот уже не поднялся.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def setUp(self):
        self.sh = (self.ROOT / "scripts" / "setup-server.sh").read_text()

    def test_it_is_valid_shell(self):
        r = subprocess.run(["bash", "-n",
                            str(self.ROOT / "scripts" / "setup-server.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_it_starts_the_entry_point_that_actually_exists(self):
        self.assertTrue((self.ROOT / "bot" / "main.py").exists())
        self.assertIn("main.py", self.sh)

    def test_it_installs_from_the_real_requirements_file(self):
        self.assertTrue((self.ROOT / "bot" / "requirements.txt").exists())
        self.assertIn("bot/requirements.txt", self.sh)

    def test_it_reads_the_keys_health_actually_returns(self):
        # Ключи берутся из `health_payload`, а не из памяти: переименуют —
        # тест упадёт здесь, а не на переезде.
        main = (self.ROOT / "bot" / "main.py").read_text()
        for key in ('"version"', '"polling"'):
            self.assertIn(key, main, f"{key} пропал из health_payload")
            self.assertIn(key.strip('"'), self.sh)

    def test_it_asks_for_the_encryption_key_and_says_why(self):
        # Без SECRET_KEY seed-фразы лежат в базе открытыми, и `/version`
        # говорит об этом прямо. Скрипт обязан спросить ключ, а не молча
        # поднять бота без него.
        self.assertIn("SECRET_KEY", self.sh)
        self.assertIn("открытым текстом", self.sh)

    def test_it_warns_that_a_new_key_makes_old_phrases_unreadable(self):
        self.assertIn("не расшифруются", self.sh)

    def test_it_never_overwrites_an_existing_env(self):
        # В `.env` работающего сервера лежит ключ, которым зашифрованы чужие
        # seed-фразы. Перезаписать его — потерять доступ к чужим кошелькам.
        self.assertIn('if [ -f "$ENV_FILE" ]', self.sh)

    def test_it_checks_the_version_before_saying_it_is_done(self):
        self.assertIn("код не доехал", self.sh)

    def test_it_installs_the_venv_package_for_the_actual_python(self):
        """На свежих Ubuntu пакет venv привязан к версии интерпретатора.

        `python3-venv` может оказаться пустышкой, и тогда окружение не
        создаётся с «ensurepip is not available» — а причина названа только
        в тексте ошибки. Скрипт спрашивает версию у самого Python и ставит
        ровно её пакет.
        """
        self.assertIn("sys.version_info", self.sh)
        self.assertIn("-venv", self.sh)

    def test_the_service_user_can_be_named_explicitly(self):
        """Запускать скрипт и работать под ботом могут разные пользователи.

        На сервере продавца у `tamik` не оказалось прав sudo вовсе, и
        заходить пришлось root'ом. Без этой возможности бот остался бы
        работать от root — процесс с чужими токенами и seed-фразами.
        """
        self.assertIn("RUN_USER:-", self.sh)

    def test_a_missing_service_user_is_named_and_not_silently_created(self):
        self.assertIn("на сервере нет", self.sh)

    def test_a_failed_venv_stops_the_run_instead_of_going_on(self):
        # Без окружения нет pip, а без pip любая следующая проверка врёт:
        # «пакета нет» вместо «проверять было нечем».
        self.assertIn("не создалось виртуальное окружение", self.sh)

    def test_failed_dependencies_stop_the_run_too(self):
        self.assertIn("зависимости не поставились", self.sh)

    def test_it_does_not_call_a_deaf_bot_a_success(self):
        # «Поднялся» и «слышит Telegram» — разные вещи, и при переезде они
        # расходятся особенно часто.
        self.assertIn("НЕ получает сообщения", self.sh)

    def test_no_secret_is_baked_into_the_script(self):
        """Секреты спрашиваются при запуске и в репозиторий не попадают.

        Образцы утечек подбираются ПО ФОРМЕ, а не перечислением настоящих
        значений: первая версия этого теста искала подстрокой реальные адрес
        и логин сервера — и тем самым занесла их в репозиторий сама.
        """
        import re
        self.assertNotIn("BOT_TOKEN=", self.sh)
        self.assertNotIn("SECRET_KEY=", self.sh)
        # Токен Telegram: цифры, двоеточие, длинная строка.
        self.assertIsNone(re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", self.sh),
                          "в скрипте похоже на токен бота")
        # Адрес сервера в открытом виде. Петля (127.0.0.1) законна: по ней
        # скрипт стучится в health на самой машине.
        addresses = [a for a in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.sh)
                     if not a.startswith("127.")]
        self.assertEqual(addresses, [], f"в скрипте зашит адрес: {addresses}")
        self.assertNotIn("sshpass", self.sh,
                         "вход по паролю: ключ надёжнее и нужен для Actions")


class AnEmptyOptionalAnswerDoesNotKillTheSetup(unittest.TestCase):
    """Пустой ответ на необязательный вопрос обязан быть просто пустым
    ответом, а не концом установки.

    Оба скрипта настройки спрашивают секреты функцией `ask` и работают под
    `set -e`. Последней строкой в `ask` стояло
    `[ -n "$value" ] && printf ... >> .env`: при пустом ответе проверка
    возвращает 1, это последняя команда функции — значит и функция вернула
    1, и `set -e` оборвал скрипт. Ровно там, где мы сами написали «Пусто —
    данные лягут в файлы».

    Снаружи это выглядело бы как самая дорогая поломка проекта: установка
    молча прекращается на середине, `.env` уже создан с токеном и ключом, а
    ни службы, ни запуска, ни сверки версии нет. Второй запуск сказал бы
    «.env уже есть — не трогаю» и прошёл дальше — то есть беда исчезла бы
    сама, не будучи понятой.

    Проверяется не текст строки, а поведение: функция `ask` вынимается из
    скрипта и выполняется настоящим bash с теми же ключами.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]
    SCRIPTS = ("setup-user.sh", "setup-server.sh")

    def _ask_source(self, name):
        """Вырезать из скрипта определение `ask` целиком."""
        text = (self.ROOT / "scripts" / name).read_text()
        start = text.index("    ask() {")
        end = text.index("\n    }\n", start) + len("\n    }\n")
        body = text[start:end]
        self.assertIn("read -r value", body, f"{name}: вырезали не ту функцию")
        return body

    def _run(self, name, answer):
        """Выполнить `ask` под `set -Eeuo pipefail`, подсунув ответ."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, ".env")
            script = "\n".join([
                "set -Eeuo pipefail",
                f'ENV_FILE={env_file!r}',
                'die() { printf "%s\\n" "$*" >&2; exit 1; }',
                'RUN_USER="$(id -un)"',
                ': > "$ENV_FILE"',
                self._ask_source(name),
                'ask DATABASE_URL "адрес базы"',
                'echo ДОШЛИ-ДО-КОНЦА',
            ])
            # /dev/tty в тесте нет — подменяем на файл с ответом.
            answer_file = os.path.join(tmp, "answer")
            with open(answer_file, "w") as fh:
                fh.write(answer + "\n")
            script = script.replace("</dev/tty", f"<{answer_file}")
            r = subprocess.run(["bash", "-c", script],
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=60)
            written = ""
            if os.path.exists(env_file):
                with open(env_file) as fh:
                    written = fh.read()
            return r, written

    def test_an_empty_answer_lets_the_setup_go_on(self):
        for name in self.SCRIPTS:
            with self.subTest(name):
                r, written = self._run(name, "")
                self.assertIn("ДОШЛИ-ДО-КОНЦА", r.stdout,
                              f"{name}: скрипт оборвался на пустом ответе\n"
                              f"код {r.returncode}, stderr: {r.stderr}")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(written, "",
                                 f"{name}: пустой ответ записан в .env")

    def test_an_answer_that_was_given_is_written_down(self):
        """Обратная сторона: непустой ответ обязан доехать до `.env` —
        иначе «поправили» можно было бы и удалением строки."""
        for name in self.SCRIPTS:
            with self.subTest(name):
                r, written = self._run(name, "postgresql://x/y")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(written, "DATABASE_URL=postgresql://x/y\n",
                                 f"{name}: ответ не записан")

    def test_a_missing_required_answer_still_stops_everything(self):
        """А вот пустой ОБЯЗАТЕЛЬНЫЙ ответ обязан останавливать: бот без
        токена не запустится, и лучше сказать это здесь, чем показать
        пустую службу, падающую в цикле."""
        for name in self.SCRIPTS:
            with self.subTest(name):
                body = self._ask_source(name)
                with tempfile.TemporaryDirectory() as tmp:
                    env_file = os.path.join(tmp, ".env")
                    answer_file = os.path.join(tmp, "answer")
                    with open(answer_file, "w") as fh:
                        fh.write("\n")
                    script = "\n".join([
                        "set -Eeuo pipefail",
                        f'ENV_FILE={env_file!r}',
                        'die() { printf "%s\\n" "$*" >&2; exit 1; }',
                        'RUN_USER="$(id -un)"',
                        ': > "$ENV_FILE"',
                        body,
                        'ask BOT_TOKEN "токен" yes',
                        'echo ДОШЛИ-ДО-КОНЦА',
                    ]).replace("</dev/tty", f"<{answer_file}")
                    r = subprocess.run(["bash", "-c", script],
                                       capture_output=True, text=True,
                                       stdin=subprocess.DEVNULL, timeout=60)
                self.assertNotEqual(r.returncode, 0,
                                    f"{name}: без токена установка продолжилась")
                self.assertNotIn("ДОШЛИ-ДО-КОНЦА", r.stdout)
                self.assertIn("BOT_TOKEN", r.stderr,
                              f"{name}: отказ не назвал, чего не хватает")


class AMissingCronIsSaidOutLoudNotDiedOn(unittest.TestCase):
    """Когда автозапуска нет, скрипт обязан это назвать, а не упасть и не
    промолчать.

    Запасной путь установки без root — задание `@reboot` в cron. Но на
    урезанных образах cron не стоит, и вызов `crontab -` там оборвал бы
    скрипт чужим сообщением «command not found», уже после того как бот
    запущен: снаружи это «установка сломалась», хотя бот работает.

    Второй, худший исход — промолчать. Бот тогда живёт до первой
    перезагрузки, и обнаружится это через недели, когда сервер перезагрузят
    ночью, а заказы перестанут приходить.

    Проверяется поведением: кусок скрипта выполняется настоящим bash с
    подставленным PATH — сначала без `crontab`, потом с ним.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def _autostart_fallback(self):
        text = (self.ROOT / "scripts" / "setup-user.sh").read_text()
        start = text.index("    if command -v crontab")
        end = text.index("\n    fi\n", start) + len("\n    fi\n")
        return text[start:end]

    def _run(self, with_cron):
        with tempfile.TemporaryDirectory() as tmp:
            binn = os.path.join(tmp, "bin")
            os.mkdir(binn)
            if with_cron:
                stub = os.path.join(binn, "crontab")
                with open(stub, "w") as fh:
                    # Читать stdin только для `crontab -`: у `crontab -l`
                    # его нет, и `cat` вычитывал бы stdin самого прогона —
                    # первая версия заглушки на этом и повисла.
                    fh.write('#!/bin/sh\n'
                             '[ "$1" = - ] && cat >/dev/null\n'
                             'exit 0\n')
                os.chmod(stub, 0o755)
            script = "\n".join([
                "set -Eeuo pipefail",
                f'export PATH={binn!r}:/bin:/usr/bin',
                'say() { printf "%s\\n" "$*"; }',
                f'RUNNER={tmp!r}/run-bot.sh',
                f'DATA={tmp!r}',
                'USER=tester',
                'MODE=""',
                self._autostart_fallback(),
                'echo "MODE=$MODE"',
            ])
            return subprocess.run(["bash", "-c", script],
                                  capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=60)

    def test_without_cron_it_warns_instead_of_dying(self):
        r = self._run(with_cron=False)
        self.assertEqual(r.returncode, 0,
                         f"скрипт умер там, где cron просто нет: {r.stderr}")
        self.assertIn("АВТОЗАПУСКА НЕТ", r.stdout,
                      "автозапуска нет, а скрипт об этом не сказал")
        self.assertIn("после перезагрузки сервера НЕ", r.stdout)
        self.assertNotIn("MODE=cron", r.stdout,
                         "объявил запуск через cron, которого нет")

    def test_without_cron_it_says_how_to_start_the_bot_by_hand(self):
        """Совет обязан быть выполнимым: правило проекта — не советовать
        невозможного. Команду запуска называем целиком."""
        r = self._run(with_cron=False)
        self.assertIn("nohup", r.stdout)
        self.assertIn("run-bot.sh", r.stdout)

    def test_with_cron_it_uses_cron_and_still_names_the_catch(self):
        r = self._run(with_cron=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MODE=cron", r.stdout, "cron есть, а он им не воспользовался")
        # cron поднимает после перезагрузки, но не после падения — и это
        # слабее systemd, о чём сказать надо.
        self.assertIn("сам не", r.stdout)


if __name__ == "__main__":
    unittest.main()
