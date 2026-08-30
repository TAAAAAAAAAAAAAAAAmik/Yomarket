"""Оборванная запись не стирает настройки продавца.

Файлы хранилища писались через `open(path, "w")`: он обнуляет файл ДО того,
как в него что-то попало. Процесс, убитый в этот миг — перезагрузка сервера,
нехватка памяти, перезапуск службы в неудачную секунду, — оставлял обрезанный
JSON. Это хуже, чем звучит: разбор падает на всём файле целиком, и продавец
теряет не «часть настроек», а токен, правила автоответа, цены и подписку
разом, без единого сообщения.

Вероятность мала ровно до тех пор, пока сервер не перезагружают. Бот пишет
эти файлы на каждом изменении настроек и на каждом ответе покупателю.

Проверяется следствием: запись роняется посередине, и после этого прежние
данные обязаны читаться.
"""
from __future__ import annotations

import glob
import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")


class Bench(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("DATABASE_URL", None)
        import storage
        self.s = importlib.reload(storage)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        import storage
        importlib.reload(storage)


class AnInterruptedWriteDoesNotDestroyWhatWasThere(Bench):
    """Прежняя запись обязана пережить неудачную новую."""

    def _break_next_write(self):
        """Уронить следующую запись ровно посередине — как отключение
        питания: часть байтов уже ушла, остальные не уйдут никогда."""
        real = json.dump

        def half(data, fh, **kw):
            fh.write('{"tokens": {"1": "поло')
            raise KeyboardInterrupt("питание кончилось")

        self.s.json.dump = half
        return real

    def test_the_previous_settings_are_still_readable(self):
        self.s._write_blob("settings", {"1": {"rule": "первое"}})
        real = self._break_next_write()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.s._write_blob("settings", {"1": {"rule": "второе"}})
        finally:
            self.s.json.dump = real
        # Главное: файл читается и в нём прежнее значение.
        self.assertEqual(self.s._read_blob("settings"), {"1": {"rule": "первое"}})

    def test_the_file_is_still_valid_json(self):
        """Отдельно от значения: разбор не должен падать. Обрезанный файл
        роняет `json.load`, и бот не стартует вовсе."""
        self.s._write_blob("tokens", {"1": "токен"})
        real = self._break_next_write()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.s._write_blob("tokens", {"1": "другой"})
        finally:
            self.s.json.dump = real
        with open(self.s._BLOBS["tokens"]) as fh:
            self.assertEqual(json.load(fh), {"1": "токен"})

    def test_the_scratch_file_is_not_mistaken_for_a_store(self):
        """Временный файл лежит рядом с настоящими. Он не должен ни
        читаться как хранилище, ни оставаться после удачной записи."""
        self.s._write_blob("tokens", {"1": "токен"})
        leftovers = glob.glob(os.path.join(self.dir, "*.tmp"))
        self.assertEqual(leftovers, [], "после удачной записи остался мусор")
        self.assertNotIn(self.s._BLOBS["tokens"] + ".tmp",
                         self.s._BLOBS.values())


class AGoodWriteStillWorks(Bench):
    """Обратная сторона: подмена файла не должна ничего сломать в обычной
    работе — иначе «починили» можно было бы и отключением записи."""

    def test_what_was_written_is_read_back(self):
        self.s._write_blob("settings", {"7": {"pause": 30}})
        self.assertEqual(self.s._read_blob("settings"), {"7": {"pause": 30}})

    def test_a_second_write_replaces_the_first(self):
        self.s._write_blob("settings", {"7": {"pause": 30}})
        self.s._write_blob("settings", {"7": {"pause": 60}})
        self.assertEqual(self.s._read_blob("settings"), {"7": {"pause": 60}})

    def test_unicode_survives_the_round_trip(self):
        """`ensure_ascii=False` здесь не украшение: имена магазинов и письма
        покупателей русские, и порча кодировки означала бы порчу данных."""
        self.s._write_blob("settings", {"7": {"shop": "Магазин «Юмаркет»"}})
        self.assertEqual(self.s._read_blob("settings")["7"]["shop"],
                         "Магазин «Юмаркет»")


if __name__ == "__main__":
    unittest.main()
