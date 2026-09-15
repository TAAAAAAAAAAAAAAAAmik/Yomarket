"""Проверка на сам набор тестов: выполняется ли он весь.

Правило простое и написано кровью: **тесты пишутся до
`if __name__ == "__main__"`**. Дописанные после — просто не выполняются,
когда файл запускают напрямую (`python3 tests/test_x.py`): `unittest.main()`
срабатывает на строке guard'а, а классы ниже к этому моменту ещё не
объявлены.

Поймано 15.09 в двух файлах разом: `test_giftcards_screens.py` показывал 30
тестов вместо 46, `test_giftcards.py` — 52 вместо 60. Двадцать четыре
проверки молчали, и молчали они самым неприятным образом: `discover` их
находит, значит общий прогон зелёный и полный, а человек, правящий один
файл и запускающий его напрямую, видит «OK» и не видит своей поломки.

Перечень файлов здесь не переписан руками, а считается: добавленный
завтра файл попадает под проверку сам.
"""
from __future__ import annotations

import ast
import pathlib
import unittest


def _files() -> list[pathlib.Path]:
    return sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py"))


class EveryTestInTheFileActuallyRuns(unittest.TestCase):
    """Guard в середине файла отрезает всё, что ниже."""

    def test_nothing_is_declared_after_the_main_guard(self):
        for path in _files():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            guard = next(
                (n.lineno for n in tree.body
                 if isinstance(n, ast.If)
                 and ast.dump(n.test).find("__name__") != -1),
                None)
            if guard is None:
                continue
            after = [n.name for n in tree.body
                     if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                                       ast.AsyncFunctionDef))
                     and n.lineno > guard]
            with self.subTest(path.name):
                self.assertFalse(
                    after,
                    f"{path.name}: после `if __name__` объявлено "
                    f"{', '.join(after)} — при запуске файла напрямую это "
                    f"не выполнится. Перенеси guard в конец файла.")

    def test_the_guard_is_the_last_thing_in_the_file(self):
        """Не только классы: любой код ниже guard'а — код после выхода."""
        for path in _files():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            guards = [n for n in tree.body
                      if isinstance(n, ast.If)
                      and ast.dump(n.test).find("__name__") != -1]
            if not guards:
                continue
            with self.subTest(path.name):
                self.assertIs(
                    guards[-1], tree.body[-1],
                    f"{path.name}: `if __name__` не последний в файле")


class TheCheckItselfCatchesTheTrap(unittest.TestCase):
    """Проверка, которая не падает на поломке, не проверяет ничего."""

    def test_a_class_below_the_guard_is_noticed(self):
        broken = ('import unittest\n\n\n'
                  'if __name__ == "__main__":\n    unittest.main()\n\n\n'
                  'class Late(unittest.TestCase):\n    pass\n')
        tree = ast.parse(broken)
        guard = next(n.lineno for n in tree.body
                     if isinstance(n, ast.If)
                     and ast.dump(n.test).find("__name__") != -1)
        after = [n.name for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.lineno > guard]
        self.assertEqual(after, ["Late"])


if __name__ == "__main__":
    unittest.main()
