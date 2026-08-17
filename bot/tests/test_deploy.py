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


if __name__ == "__main__":
    unittest.main()
