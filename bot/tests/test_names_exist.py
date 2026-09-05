"""Имена, которые зовут и импортируют, существуют.

`_run_panel` удалили 05.08 вместе с соседней функцией, а четыре её вызова и
импорт из `stats_source` остались. Поймать это было нечем: вызовы стоят
внутри обработчиков, импорт — внутри функции, и оба срабатывают только в
бою. Статистика на каждом экране получала ImportError, молча уходила в
запасной источник и подписывалась «история бота — cannot import name
'_run_panel'»; баланс, реквизиты и вывод падали на NameError. Цифры не
сходились с панелью, потому что в панель их не спрашивали.

Проверка идёт по AST: импортировать все модули подряд в тестах нельзя —
половина требует токена и сети.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import unittest

BOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT))
os.environ.setdefault("BOT_TOKEN", "x")

# Модули проекта, а не библиотеки: только их мы можем проверить у себя.
_OURS = {p.stem for p in BOT.glob("*.py")} | {
    f"{d.name}.{p.stem}"
    for d in BOT.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    for p in d.glob("*.py")}


def _module_path(name: str) -> pathlib.Path | None:
    p = BOT / (name.replace(".", "/") + ".py")
    return p if p.is_file() else None


def _top_level_names(tree: ast.Module) -> set[str]:
    """Что модуль определяет на верхнем уровне — включая импортированное."""
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
    return out


def _every_import() -> list[tuple[str, str, str]]:
    """(файл, модуль, имя) для каждого `from наш_модуль import имя`."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(BOT.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                     # разберётся отдельным тестом
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            if node.module not in _OURS:
                continue
            for alias in node.names:
                if alias.name != "*":
                    found.append((str(path.relative_to(BOT)), node.module,
                                  alias.name))
    return found


class EveryImportedNameIsDefined(unittest.TestCase):
    """`from handlers.balance import _run_panel` жил в коде, а функции не
    было. Импорт стоял внутри функции, поэтому падал только в бою."""

    def test_no_import_points_at_a_name_that_does_not_exist(self):
        missing = []
        cache: dict[str, set[str]] = {}
        for where, module, name in _every_import():
            if module not in cache:
                p = _module_path(module)
                cache[module] = (_top_level_names(ast.parse(p.read_text()))
                                 if p else set())
            if not cache[module]:
                continue                        # модуль не наш или не читается
            if name not in cache[module]:
                missing.append(f"{where}: from {module} import {name}")
        self.assertEqual(missing, [], "импорт указывает в пустоту:\n"
                         + "\n".join(missing))

    def test_the_check_actually_sees_our_modules(self):
        """Если список наших модулей пуст, предыдущий тест ничего не проверяет."""
        self.assertIn("stats_source", _OURS)
        self.assertIn("handlers.balance", _OURS)
        self.assertTrue(_every_import(), "импортов не найдено вовсе")

    def test_the_name_that_started_this_is_there(self):
        from handlers.balance import _run_panel     # noqa: F401


if __name__ == "__main__":
    unittest.main()
