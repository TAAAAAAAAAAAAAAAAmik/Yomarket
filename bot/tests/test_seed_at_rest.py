"""Seed-фраза TON в хранилище — это чужой кошелёк, а не строка настроек.

Долг был записан прямо в CLAUDE.md: «seed-фраза хранится незашифрованной, до
платного релиза чинить обязательно». Хранилище при этом общее: без
DATABASE_URL это JSON-файл рядом с кодом, с ним — таблица в базе, куда ходит
не только бот. Двадцать четыре слова оттуда — это перевод всех денег
кошелька куда угодно, и обратно их не вернуть.

Чего здесь намеренно нет: подстраховки «ключа нет — выведем свой». Ключ,
собранный из чего-то, что лежит рядом с шифротекстом, шифрованием не
является — он даёт ощущение защиты вместо защиты. Нет ключа — храним как
раньше и говорим об этом в /version, а не делаем вид.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

# Проверка «битая библиотека не роняет бота» намеренно ломает импорт, и её
# трассировка в выводе — ожидаемый шум, а не находка.
logging.getLogger("storage").setLevel(logging.CRITICAL)

SEED = " ".join(["abandon"] * 23 + ["art"])


class Bench(unittest.TestCase):
    """Хранилище перезагружается с ключом и без — ключ читается на импорте."""

    KEY = "проверочный-ключ-1"

    def load(self, key: str | None):
        import storage
        was = os.environ.get("SECRET_KEY")
        if key is None:
            os.environ.pop("SECRET_KEY", None)
        else:
            os.environ["SECRET_KEY"] = key
        try:
            mod = importlib.reload(storage)
        finally:
            if was is None:
                os.environ.pop("SECRET_KEY", None)
            else:
                os.environ["SECRET_KEY"] = was
        self.addCleanup(importlib.reload, storage)
        self.blob: dict = {}
        mod._read_blob = lambda name: self.blob
        mod._write_blob = lambda name, data: self.blob.update(data)
        mod._account_key = lambda uid: str(uid)
        mod._take_legacy = lambda data, uid: False
        mod._USE_DB = True                      # не трогать файлы и chmod
        return mod


class WithAKeyItIsNotReadable(Bench):
    def test_the_stored_text_is_not_the_seed(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED, "cookies": {"c": "1"}})
        stored = self.blob["1"]["mnemonic"]
        self.assertNotIn("abandon", stored)
        self.assertNotEqual(stored, SEED)

    def test_and_the_bot_still_reads_it_back(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_saving_twice_does_not_encrypt_the_ciphertext_again(self):
        """Иначе каждое сохранение наращивало бы слой, а расшифровка отдавала
        бы предыдущий шифротекст вместо слов."""
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        s.save_fragment_creds(1, {"api_hash": "abc"})
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_other_fields_are_untouched(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED, "wallet_version": "v4r2"})
        self.assertEqual(self.blob["1"]["wallet_version"], "v4r2")

    def test_a_record_written_before_encryption_still_opens(self):
        """Продавцы с уже сохранённой фразой не должны заводить её заново."""
        s = self.load(self.KEY)
        self.blob["1"] = {"mnemonic": SEED, "wallet_version": "v4r2"}
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_and_is_encrypted_on_the_next_save(self):
        s = self.load(self.KEY)
        self.blob["1"] = {"mnemonic": SEED}
        s.save_fragment_creds(1, {"api_hash": "abc"})
        self.assertNotIn("abandon", self.blob["1"]["mnemonic"])
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_a_wrong_key_gives_nothing_rather_than_garbage(self):
        """Отдать шифротекст как seed-фразу — значит показать продавцу
        «неверная seed-фраза» вместо «потерян ключ»."""
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        sealed = dict(self.blob)
        s2 = self.load("совсем-другой-ключ")
        self.blob.update(sealed)
        self.assertEqual(s2.get_fragment_creds(1)["mnemonic"], "")

    def test_two_long_keys_alike_at_the_start_are_still_different_keys(self):
        """Ключ приводится к нужной длине хешем, а не обрезанием. Обрезание
        молча свело бы длинный ключ к первым тридцати двум символам, и два
        разных пароля открывали бы одно и то же."""
        base = "a" * 32
        s = self.load(base + "1")
        s.save_fragment_creds(1, {"mnemonic": SEED})
        sealed = dict(self.blob)
        s2 = self.load(base + "2")
        self.blob.update(sealed)
        self.assertEqual(s2.get_fragment_creds(1)["mnemonic"], "")

    def test_a_lost_key_gives_nothing_too(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        sealed = dict(self.blob)
        s2 = self.load(None)
        self.blob.update(sealed)
        self.assertEqual(s2.get_fragment_creds(1)["mnemonic"], "")


class WithoutAKeyItSaysSoInsteadOfPretending(Bench):
    def test_nothing_is_invented_to_encrypt_with(self):
        s = self.load(None)
        self.assertFalse(s.encryption_on())

    def test_the_seed_is_stored_as_before(self):
        """Ломать работающего бота из-за незаданной переменной нельзя."""
        s = self.load(None)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_with_a_key_it_reports_honestly(self):
        self.assertTrue(self.load(self.KEY).encryption_on())


class ABrokenCryptoLibraryDoesNotTakeTheBotDown(Bench):
    """Поймано на этой машине: битая сборка cryptography роняет импорт
    `pyo3_runtime.PanicException`, а он наследуется от BaseException и мимо
    обычного `except Exception` проходит насквозь. Падал не только вход в
    настройки, но и /version — та самая команда, которой выясняют, что
    вообще происходит."""

    class Panic(BaseException):
        pass

    def with_broken_import(self, key=Bench.KEY):
        s = self.load(key)
        import builtins
        real = builtins.__import__

        def boom(name, *a, **kw):
            if name.startswith("cryptography"):
                raise ABrokenCryptoLibraryDoesNotTakeTheBotDown.Panic(
                    "Python API call failed")
            return real(name, *a, **kw)

        builtins.__import__ = boom
        self.addCleanup(setattr, builtins, "__import__", real)
        return s

    def test_asking_whether_encryption_is_on_does_not_raise(self):
        s = self.with_broken_import()
        self.assertFalse(s.encryption_on())

    def test_saving_still_works(self):
        s = self.with_broken_import()
        s.save_fragment_creds(1, {"mnemonic": SEED})
        self.assertEqual(s.get_fragment_creds(1)["mnemonic"], SEED)

    def test_an_already_encrypted_record_is_not_shown_as_the_seed(self):
        """Отдать шифротекст как фразу хуже, чем не отдать ничего: кошелёк
        из него не соберётся, а ошибка будет про «неверную seed-фразу»."""
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        sealed = dict(self.blob)
        s2 = self.with_broken_import()
        self.blob.update(sealed)
        got = s2.get_fragment_creds(1)["mnemonic"]
        self.assertEqual(got, "")
        self.assertNotIn("enc:v1:", got)


class TheSellerCanSeeWhichOfTheTwoItIs(Bench):
    """«Наверное, зашифрована» — не тот ответ, который можно дать про доступ
    к чужому кошельку."""

    def version_line(self, key, seed=SEED):
        import handlers.start as start
        s = self.load(key)
        s.save_fragment_creds(1, {"mnemonic": seed} if seed else {})
        src = __import__("inspect").getsource(start.cmd_version)
        self.assertIn("encryption_on", src, "экран не спрашивает хранилище")
        return s

    def test_the_screen_asks_the_storage_and_not_the_environment(self):
        self.version_line(self.KEY)

    def test_an_unencrypted_seed_is_called_that_on_screen(self):
        import inspect

        import handlers.start as start
        src = inspect.getsource(start.cmd_version)
        self.assertIn("в открытом виде", src)
        self.assertIn("SECRET_KEY", src)


class NothingSecretLeaksIntoTheClear(Bench):
    def test_the_prefix_alone_is_not_mistaken_for_ciphertext(self):
        """Продавец мог вписать в поле что угодно, в том числе строку,
        начинающуюся так же."""
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        self.assertTrue(self.blob["1"]["mnemonic"].startswith("enc:v1:"))

    def test_an_empty_seed_stays_empty(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": "", "cookies": {}})
        self.assertEqual(self.blob["1"]["mnemonic"], "")

    def test_deleting_removes_the_record(self):
        s = self.load(self.KEY)
        s.save_fragment_creds(1, {"mnemonic": SEED})
        s.delete_fragment_creds(1)
        self.assertNotIn("1", self.blob)


if __name__ == "__main__":
    unittest.main()
