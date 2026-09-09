"""Подключить магазин может только тот, кому это уже можно.

Живая жалоба 31.08: «при команде меню снова выходит кнопка подключить
магазин, нажимая на которую клиент получает бесплатный доступ». Обе
половины оказались правдой, и по разным причинам:

* **при выключенных воротах** подключение и правда открывает бота даром —
  фоновый цикл работает всем, у кого есть токен, и подписку не
  спрашивает. Это решение владельца: пока «Требовать подписку» выключено,
  бот бесплатный для всех, и мешать этому мы не вправе;
* **при включённых** кнопка показывалась всё равно, а нажатие упиралось в
  ворота: «🔒 Нужна подписка» и ничего больше. Кнопка, которая не может
  сработать, — обещание невозможного, и виноватым выглядит бот.

Поэтому кнопка появляется, только когда ею можно воспользоваться, а тому,
кому нельзя, экран говорит, ЧТО делать, а не только что нельзя.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.start as S                                 # noqa: E402
import storage                                             # noqa: E402


def run(coro):
    return asyncio.run(coro)


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class Msg:
    def __init__(self, uid):
        self.from_user = type("U", (), {"id": uid})()
        self.out: list = []

    async def answer(self, text, reply_markup=None, **kw):
        self.out.append((text, reply_markup))


class St:
    def __init__(self):
        self.state = None

    async def clear(self):
        self.state = None

    async def set_state(self, x):
        self.state = x

    async def update_data(self, **kw):
        return None


class Bench(unittest.TestCase):
    UID = 7

    def setUp(self):
        self.admin: dict = {}
        self.blobs: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        self._read, self._write = storage._read_blob, storage._write_blob
        self._owner = storage.is_owner
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)
        storage._read_blob = lambda n: (self.admin if n == "admin"
                                        else self.blobs.setdefault(n, {}))
        storage._write_blob = lambda n, d: (self.admin.update(d) if n == "admin"
                                            else self.blobs.__setitem__(n, d))
        storage.is_owner = lambda uid: False

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save
        storage._read_blob, storage._write_blob = self._read, self._write
        storage.is_owner = self._owner

    def menu(self):
        m, st = Msg(self.UID), St()
        run(S.cmd_menu(m, st))
        text, kb = m.out[-1]
        return plain(text), [b.callback_data for row in kb.inline_keyboard
                             for b in row], st


class WithTheGateOffTheBotIsFreeForEveryone(Bench):
    """Так решил владелец, и мешать ему мы не вправе."""

    def test_connecting_is_offered(self):
        _text, data, _st = self.menu()
        self.assertIn("start:connect", data)

    def test_and_the_token_is_awaited(self):
        """Кто знает, где брать токен, вставит его не нажимая кнопку."""
        _text, _data, st = self.menu()
        self.assertIsNotNone(st.state)


class WithTheGateOnConnectingIsBehindIt(Bench):

    def setUp(self):
        super().setUp()
        storage.set_require_subscription(True)

    def test_a_seller_without_access_is_not_offered_to_connect(self):
        """Кнопка показывалась, а нажатие упиралось в ворота: «🔒 Нужна
        подписка» и ничего больше."""
        _text, data, _st = self.menu()
        self.assertNotIn("start:connect", data)

    def test_he_is_told_what_to_do_instead(self):
        """«Нельзя» без «а что можно» — тупик: человек уже здесь, значит
        хочет работать."""
        text, data, _st = self.menu()
        self.assertIn("Сначала открой доступ", text)
        self.assertIn("access:menu", data)

    def test_and_no_token_is_awaited_from_him(self):
        """Ждать токен от того, кому подключаться нельзя, значит принять
        его и тут же отказать."""
        _text, _data, st = self.menu()
        self.assertIsNone(st.state)

    def test_a_paying_seller_gets_the_button_back(self):
        storage.grant_subscription(self.UID, 30)
        text, data, st = self.menu()
        self.assertIn("start:connect", data)
        self.assertNotIn("Сначала открой доступ", text)
        self.assertIsNotNone(st.state)

    def test_an_expired_subscription_does_not_count(self):
        """Истёкшая подписка — это отсутствие подписки, а не «была же»."""
        self.admin["subscriptions"] = {str(self.UID): {"expires": 1, "by": 1}}
        _text, data, _st = self.menu()
        self.assertNotIn("start:connect", data)

    def test_the_owner_is_never_locked_out_of_his_own_bot(self):
        storage.is_owner = lambda uid: uid == self.UID
        _text, data, _st = self.menu()
        self.assertIn("start:connect", data)


class ThePressItselfIsGuardedToo(Bench):
    """Кнопку можно не показать, но адрес остаётся: нажатие со старого
    сообщения обязано упереться в те же ворота."""

    def test_connecting_is_not_in_the_free_list(self):
        import main as M
        self.assertNotIn("start:connect", M.FREE_CALLBACKS)
        self.assertFalse("start:connect".startswith(M.FREE_PREFIXES))

    def test_getting_access_is(self):
        """Обратная сторона: путь к оплате запирать нельзя."""
        import main as M
        self.assertIn("access:menu", M.FREE_CALLBACKS)


if __name__ == "__main__":
    unittest.main()
