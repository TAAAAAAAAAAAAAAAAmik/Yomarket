"""Экран поддержки — один на команду и на кнопку.

Их было два: свой у `/help` и свой у «🧡 Поддержка». Тексты разъехались —
один звал прислать код сборки служебной командой, второй нет, — и заметить
это было нечем: это были разные куски кода. Один экран, два входа —
расходиться теперь нечему, и здесь это проверяется буквально.

Остальное на экране — то, чего человек про себя не знает и спрашивает у
поддержки первым делом:

* **свой Telegram ID** — без него разговор начинается с «а какой у вас
  номер», а найти его негде;
* **до какого числа подписка** — половина обращений именно про это.

Кода сборки продавцу здесь НЕ показывают. Он ему ничего не объясняет, а
рассказывает о боте то, чего тот не спрашивал, — прячется по тому же
правилу, что и `/version`. Владельцу метка остаётся: он открывает тот же
экран у себя и видит её на том же месте.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.commands as C                              # noqa: E402
import handlers.start as S                                 # noqa: E402
import storage                                             # noqa: E402
import support                                             # noqa: E402


def run(coro):
    return asyncio.run(coro)


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class Bench(unittest.TestCase):
    UID = 949865989

    def setUp(self):
        self.admin: dict = {}
        self.blobs: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._read, self._write = storage._read_blob, storage._write_blob
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage._read_blob = lambda n: (self.admin if n == "admin"
                                        else self.blobs.setdefault(n, {}))
        storage._write_blob = lambda n, d: (self.admin.update(d) if n == "admin"
                                            else self.blobs.__setitem__(n, d))

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage._read_blob, storage._write_blob = self._read, self._write

    def by_command(self) -> str:
        said: list[str] = []

        class Msg:
            from_user = type("U", (), {"id": Bench.UID})()

            async def answer(s, text, reply_markup=None, **kw):
                said.append(text)

        run(C.cmd_help(Msg()))
        return said[0]

    def by_button(self) -> str:
        seen: list[str] = []

        class Screen:
            async def edit_text(s, text, reply_markup=None, **kw):
                seen.append(text)

        cb = type("CB", (), {})()
        cb.message = Screen()
        cb.from_user = type("U", (), {"id": self.UID})()
        cb.answer = lambda *a, **kw: asyncio.sleep(0)
        run(S.show_help(cb))
        return seen[0]


class TheCommandAndTheButtonShowTheSameScreen(Bench):

    def test_word_for_word_the_same(self):
        """Два экрана расходятся молча: правят один, второй остаётся."""
        self.assertEqual(self.by_command(), self.by_button())

    def test_neither_is_empty(self):
        """Сравнение двух пустых строк равенство тоже даёт."""
        self.assertGreater(len(self.by_command()), 100)


class ItTellsHimWhatHeCannotLookUpHimself(Bench):

    def test_his_telegram_id_is_on_the_screen(self):
        """Первое, что спрашивает поддержка, и первое, чего он про себя не
        знает."""
        self.assertIn(str(self.UID), self.by_command())

    def test_the_id_is_tappable_to_copy(self):
        """Его пересылают в поддержку — переписывать девять цифр руками
        значит ошибиться в одной."""
        self.assertIn(f"<code>{self.UID}</code>", self.by_command())

    def test_the_build_code_is_not_shown_to_the_seller(self):
        """Служебное прячется целиком: и команда `/version`, и то, что она
        печатает. Скрыть команду, оставив её вывод на экране, значит не
        скрыть ничего."""
        for text in (self.by_command(), self.by_button()):
            self.assertNotIn(S.BOT_VERSION, text)
            self.assertNotIn("сборки", plain(text))

    def test_but_the_admin_still_sees_it(self):
        """Метку читает владелец — он воспроизводит беду у себя. Убрать её
        совсем значило бы вернуть починку к угадыванию версии."""
        storage.add_admin(self.UID)
        self.assertIn(S.BOT_VERSION, self.by_command())
        self.assertIn(S.BOT_VERSION, self.by_button())

    def test_the_support_contact_is_in_the_text_not_only_on_a_button(self):
        """Кнопка-ссылка рисуется лишь для контакта вида `@ник`, а вписать
        можно что угодно — тогда экран поддержки остался бы без
        поддержки."""
        storage.set_support_contact("почта help@example.com")
        text = self.by_command()
        self.assertIn("help@example.com", plain(text))
        data = [b.url for row in support.support_kb(self.UID).inline_keyboard
                for b in row if getattr(b, "url", None)]
        self.assertEqual(data, [], "для не-@ника нарисовали ссылку")


class TheSubscriptionLineIsNotGuessed(Bench):

    def test_an_active_subscription_shows_its_end_date(self):
        storage.grant_subscription(self.UID, 180)
        text = plain(self.by_command())
        self.assertIn("активна до", text)
        self.assertRegex(text, r"\d{2}\.\d{2}\.\d{4}")

    def test_and_how_many_days_are_left(self):
        """Дата без остатка заставляет считать в уме."""
        storage.grant_subscription(self.UID, 180)
        self.assertRegex(plain(self.by_command()), r"17[89] дн\.")

    def test_a_lifetime_subscription_is_called_forever_not_dated(self):
        """Дата через сто лет читается как сбой, а не как «навсегда»."""
        storage.grant_subscription(self.UID, storage.LIFETIME_DAYS)
        text = plain(self.by_command())
        self.assertIn("навсегда", text)
        self.assertNotIn(str(storage.LIFETIME_DAYS), text)
        self.assertNotRegex(text, r"\d{2}\.\d{2}\.21\d\d")

    def test_no_subscription_says_so_and_where_to_get_one(self):
        """«Активна до» с пустой датой — худшее из возможного: человек
        решит, что она есть."""
        text = plain(self.by_command())
        self.assertIn("Подписки нет", text)
        self.assertNotIn("активна до", text)

    def test_a_subscription_ending_today_is_not_called_active(self):
        """Ноль дней — это не «активна», а «кончается сегодня». Написать
        «активна до» и дать нулевой остаток значит предложить человеку
        догадаться самому."""
        self.admin["subscriptions"] = {
            str(self.UID): {"expires": time.time() + 3600, "by": 0}}
        text = plain(self.by_command())
        self.assertIn("кончается сегодня", text)
        self.assertNotIn("0 дн.", text)


class TheWayBackLeadsWhereHeCameFrom(Bench):

    def _back(self, uid):
        return [b.callback_data
                for row in support.support_kb(uid).inline_keyboard
                for b in row if (b.callback_data or "").startswith(
                    ("menu:main", "start:hello"))]

    def test_without_a_shop_it_leads_to_the_showcase(self):
        """У кого магазина нет, тот пришёл с витрины, и меню ему покажет
        «магазин не подключён»."""
        self.assertEqual(self._back(self.UID), ["start:hello"])

    def test_with_a_shop_it_leads_to_the_menu(self):
        self.blobs["tokens"] = {str(self.UID): "wli-token"}
        self.assertEqual(self._back(self.UID), ["menu:main"])

    def test_the_documents_button_returns_to_support_not_to_the_menu(self):
        """Путь «поддержка → документы → назад» приводил в полный бот."""
        data = [b.callback_data
                for row in support.support_kb(self.UID).inline_keyboard
                for b in row if "policy" in (b.callback_data or "")]
        self.assertEqual(data, ["menu:policy:help"])


if __name__ == "__main__":
    unittest.main()
