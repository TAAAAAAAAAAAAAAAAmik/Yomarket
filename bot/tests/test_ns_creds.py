"""Доступ к поставщику ns.gifts: ввод, хранение, диагностика.

Пароль и `api_secret` вместе дают право тратить баланс кабинета — это чужие
деньги, ровно как seed-фраза TON. Поэтому здесь три вещи: разобрать то, что
человек скопировал из письма оператора; спрятать в хранилище то, что
секретно; и не показать секреты ни в одном отчёте.

Отдельно — про честность диагностики. `/ns_stock` печатает схему полей
услуги как есть. Угадывание имён полей в этом проекте уже стоило дня на
Fragment, и повторять его на поставщике, который берёт деньги за каждый
заказ, незачем.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import nsgifts as H          # noqa: E402

SECRET = "oS0dXgAqakz0uybnt/rSx5VLR6j7D8FItBHPcfqfkag="
PASSWORD = "0hvDCRHhOBasSJH0nIgM"


class TheAccessIsReadFromWhateverWasPasted(unittest.TestCase):
    """Продавец копирует данные из письма оператора, где они лежат как
    попало. Требовать точного порядка — требовать аккуратности в тот момент,
    когда человек уже устал."""

    def test_the_plain_shape_works(self):
        got = H.parse_creds("user_id: 1234\nlogin: candbug\n"
                            f"password: {PASSWORD}\napi_secret: {SECRET}")
        self.assertEqual(got["user_id"], "1234")
        self.assertEqual(got["login"], "candbug")
        self.assertEqual(got["password"], PASSWORD)
        self.assertEqual(got["api_secret"], SECRET)

    def test_equals_instead_of_colon(self):
        self.assertEqual(H.parse_creds("login=candbug")["login"], "candbug")

    def test_russian_names_are_understood(self):
        got = H.parse_creds("логин: candbug\nпароль: 123\nсекрет: abc")
        self.assertEqual(got, {"login": "candbug", "password": "123",
                               "api_secret": "abc"})

    def test_the_order_does_not_matter(self):
        a = H.parse_creds("login: a\npassword: b")
        b = H.parse_creds("password: b\nlogin: a")
        self.assertEqual(a, b)

    def test_everything_on_one_line_through_semicolons(self):
        got = H.parse_creds("user_id: 1; login: a; password: b")
        self.assertEqual(got["user_id"], "1")
        self.assertEqual(got["login"], "a")

    def test_quotes_around_a_value_are_stripped(self):
        self.assertEqual(H.parse_creds('password: "b c"')["password"], "b c")

    def test_a_base64_secret_keeps_its_padding(self):
        """Секрет кончается на «=», и обрезать по первому знаку нельзя."""
        self.assertEqual(H.parse_creds(f"api_secret: {SECRET}")["api_secret"],
                         SECRET)

    def test_prose_yields_nothing(self):
        self.assertEqual(H.parse_creds("не знаю где это брать"), {})
        self.assertEqual(H.parse_creds(""), {})

    def test_an_unknown_field_is_ignored(self):
        self.assertEqual(H.parse_creds("телефон: +7999\nlogin: a"),
                         {"login": "a"})


class WhatIsSecretIsHiddenInStorage(unittest.TestCase):
    """Пароль и ключ вместе — это право тратить чужие деньги."""

    def load(self, key="ключ-для-проверки"):
        import storage
        was = os.environ.get("SECRET_KEY")
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
        mod._USE_DB = True
        return mod

    def test_neither_the_secret_nor_the_password_is_stored_as_is(self):
        s = self.load()
        s.save_ns_creds(1, {"user_id": "1234", "login": "candbug",
                            "password": PASSWORD, "api_secret": SECRET})
        stored = self.blob["1"]
        self.assertNotEqual(stored["api_secret"], SECRET)
        self.assertNotEqual(stored["password"], PASSWORD)

    def test_and_both_read_back_intact(self):
        s = self.load()
        s.save_ns_creds(1, {"password": PASSWORD, "api_secret": SECRET})
        got = s.get_ns_creds(1)
        self.assertEqual(got["api_secret"], SECRET)
        self.assertEqual(got["password"], PASSWORD)

    def test_the_login_stays_readable(self):
        """Шифруем то, ради чего стоит заводить шифрование, а не всё подряд."""
        s = self.load()
        s.save_ns_creds(1, {"login": "candbug", "user_id": "1234"})
        self.assertEqual(self.blob["1"]["login"], "candbug")

    def test_saving_twice_does_not_double_wrap(self):
        s = self.load()
        s.save_ns_creds(1, {"api_secret": SECRET})
        s.save_ns_creds(1, {"login": "candbug"})
        self.assertEqual(s.get_ns_creds(1)["api_secret"], SECRET)

    def test_forgetting_removes_everything(self):
        s = self.load()
        s.save_ns_creds(1, {"api_secret": SECRET})
        s.delete_ns_creds(1)
        self.assertEqual(s.get_ns_creds(1), {})

    def test_nothing_stored_means_nothing_returned(self):
        self.assertEqual(self.load().get_ns_creds(999), {})


class Msg:
    def __init__(self, text):
        self.text = text
        self.said: list[str] = []
        self.deleted = False
        self.from_user = type("U", (), {"id": 1})()

    async def answer(self, text, **kw):
        self.said.append(text)
        return Status(self.said)

    async def delete(self):
        self.deleted = True


class Status:
    def __init__(self, sink):
        self.sink = sink

    async def edit_text(self, text, **kw):
        self.sink.append(text)


class TheMessageWithSecretsDoesNotLinger(unittest.TestCase):
    def setUp(self):
        self.saved: dict = {}
        self._save, self._get = H.save_ns_creds, H.get_ns_creds
        H.save_ns_creds = lambda uid, c: self.saved.update(c)
        H.get_ns_creds = lambda uid: dict(self.saved)

    def tearDown(self):
        H.save_ns_creds, H.get_ns_creds = self._save, self._get

    def run_login(self, text):
        msg = Msg(text)
        asyncio.run(H.ns_login(msg))
        return msg

    def test_it_is_deleted_after_being_read(self):
        msg = self.run_login(f"/ns_login login: a\npassword: {PASSWORD}")
        self.assertTrue(msg.deleted)

    def test_it_is_deleted_even_when_nothing_parsed(self):
        """Неразобранное сообщение содержало пароль ровно так же."""
        msg = self.run_login("/ns_login я не понял что присылать")
        self.assertTrue(msg.deleted)

    def test_the_answer_never_repeats_the_secret(self):
        msg = self.run_login(f"/ns_login api_secret: {SECRET}\n"
                             f"password: {PASSWORD}")
        said = "\n".join(msg.said)
        self.assertNotIn(SECRET, said)
        self.assertNotIn(PASSWORD, said)

    def test_it_says_what_is_still_missing(self):
        msg = self.run_login("/ns_login login: candbug")
        said = "\n".join(msg.said)
        self.assertIn("password", said)
        self.assertIn("api_secret", said)

    def test_a_full_set_points_at_the_next_step(self):
        msg = self.run_login(f"/ns_login user_id: 1\nlogin: a\n"
                             f"password: {PASSWORD}\napi_secret: {SECRET}")
        self.assertIn("/ns_stock", "\n".join(msg.said))

    def test_an_empty_command_explains_the_format(self):
        msg = self.run_login("/ns_login")
        self.assertIn("user_id", "\n".join(msg.said))


class TheCatalogueIsPrintedAsItCame(unittest.TestCase):
    """Схема полей печатается как есть. Угадывание имён полей в этом проекте
    уже стоило дня — повторять его на поставщике, который берёт деньги за
    каждый заказ, незачем."""

    STOCK = {"categories": [
        {"category_name": "Roblox", "category_id": 42,
         "fields": [{"key": "account", "type": "string", "required": True},
                    {"key": "amount", "type": "int"}],
         "services": [{"service_id": 900, "service_name": "Roblox | 100 Robux",
                       "price": 1.1, "currency": "USD", "in_stock": 50}]},
    ]}

    def setUp(self):
        self._get = H.get_ns_creds
        H.get_ns_creds = lambda uid: {"user_id": "1", "login": "a",
                                      "password": "b", "api_secret": "c"}
        import automation.nsgifts as N
        self.N = N
        self._stock = N.stock_sync

    def tearDown(self):
        H.get_ns_creds = self._get
        self.N.stock_sync = self._stock

    def report(self, text="/ns_stock robux", answer=None):
        self.N.stock_sync = lambda creds: (answer or (True, self.STOCK))
        msg = Msg(text)
        asyncio.run(H.ns_stock(msg))
        return "\n".join(msg.said)

    def test_the_service_id_is_shown(self):
        self.assertIn("service_id=900", self.report())

    def test_the_order_fields_are_shown(self):
        got = self.report()
        self.assertIn("account", got)
        self.assertIn("amount", got)

    def test_a_required_field_is_marked(self):
        self.assertIn("account (string)*", self.report())

    def test_the_price_and_stock_are_shown(self):
        got = self.report()
        self.assertIn("1.1", got)
        self.assertIn("50", got)

    def test_nothing_found_says_so_and_suggests_another_word(self):
        got = self.report("/ns_stock minecraft")
        self.assertIn("ничего не нашлось", got)
        self.assertIn("другое слово", got)

    def test_a_refusal_is_shown_instead_of_a_catalogue(self):
        got = self.report(answer=(False, "доступ запрещён — возможно, наш IP "
                                         "не в белом списке у поставщика"))
        self.assertIn("белом списке", got)

    def test_missing_credentials_stop_it_before_the_network(self):
        H.get_ns_creds = lambda uid: {"login": "a"}
        called: list = []
        self.N.stock_sync = lambda creds: called.append(1) or (True, {})
        msg = Msg("/ns_stock")
        asyncio.run(H.ns_stock(msg))
        self.assertEqual(called, [])
        self.assertIn("Не задано", "\n".join(msg.said))


class CB:
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


class FSM:
    def __init__(self):
        self.data: dict = {}
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def buttons(kb) -> list[str]:
    return [] if kb is None else [b.text for row in kb.inline_keyboard
                                  for b in row]


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


class TheAccessIsEnteredByButtons(unittest.TestCase):
    """Продавцу неоткуда узнать про команду `/ns_login`: раздел он открывает
    кнопками, значит и доступ вводится там же."""

    def setUp(self):
        self.saved: dict = {}
        self._save, self._get = H.save_ns_creds, H.get_ns_creds
        self._del = H.delete_ns_creds
        H.save_ns_creds = lambda uid, c: self.saved.update(c)
        H.get_ns_creds = lambda uid: dict(self.saved)
        H.delete_ns_creds = lambda uid: self.saved.clear()

    def tearDown(self):
        H.save_ns_creds, H.get_ns_creds = self._save, self._get
        H.delete_ns_creds = self._del

    def screen(self):
        cb = CB("ns:creds")
        asyncio.run(H.ns_creds_screen(cb, FSM()))
        return cb

    def test_the_roblox_plugin_leads_here(self):
        """Проверка проводки: экран может быть хорош, а попасть в него
        неоткуда."""
        from handlers import plugins as P
        kb = P._roblox_keyboard({"plugins": {"auto_roblox": {}}})
        self.assertIn("ns:creds", callbacks(kb))

    def test_every_field_has_its_own_button(self):
        cb = self.screen()
        marks = callbacks(cb.message.kbs[-1])
        for i in range(4):
            self.assertIn(f"ns:set:{i}", marks)

    def test_an_empty_field_is_marked_as_empty(self):
        cb = self.screen()
        self.assertTrue(any("▫️" in t for t in buttons(cb.message.kbs[-1])))

    def test_a_filled_one_is_marked_as_filled(self):
        self.saved["login"] = "candbug"
        cb = self.screen()
        self.assertTrue(any("✅" in t for t in buttons(cb.message.kbs[-1])))

    def test_checking_the_login_is_offered_only_when_all_is_there(self):
        """Кнопка, которая заведомо не сработает, — обещание невозможного."""
        cb = self.screen()
        self.assertNotIn("ns:check", callbacks(cb.message.kbs[-1]))
        self.saved.update({"user_id": "1", "login": "a", "password": "b",
                           "api_secret": "c"})
        cb = self.screen()
        self.assertIn("ns:check", callbacks(cb.message.kbs[-1]))

    def test_the_secret_is_shown_as_a_length_not_a_value(self):
        self.saved.update({"api_secret": SECRET, "password": PASSWORD})
        cb = self.screen()
        self.assertNotIn(SECRET, cb.message.last)
        self.assertNotIn(PASSWORD, cb.message.last)
        self.assertIn("символов", cb.message.last)

    def test_encryption_is_promised_only_when_it_is_real(self):
        """Без `SECRET_KEY` пароль ложится в базу открытым — и экран обязан
        это сказать, а не обещать шифрование."""
        self.saved.update({"login": "candbug", "api_secret": SECRET})
        real = H.encryption_on
        try:
            H.encryption_on = lambda: False
            said = self.screen().message.last
            self.assertIn("в открытом виде", said)
            self.assertNotIn("хранятся зашифрованными", said)
        finally:
            H.encryption_on = real

    def test_the_login_is_shown_as_is(self):
        """Он не секрет, и видеть его полезно: опечатку иначе не заметить."""
        self.saved["login"] = "candbug"
        self.assertIn("candbug", self.screen().message.last)

    def test_a_field_typed_in_is_saved_and_the_message_deleted(self):
        cb = CB("ns:set:2")            # пароль
        fsm = FSM()
        asyncio.run(H.ns_set_field(cb, fsm))
        msg = Msg(PASSWORD)
        asyncio.run(H.ns_one_field_input(msg, fsm))
        self.assertEqual(self.saved["password"], PASSWORD)
        self.assertTrue(msg.deleted)

    def test_and_the_answer_does_not_echo_it(self):
        cb = CB("ns:set:3")            # ключ
        fsm = FSM()
        asyncio.run(H.ns_set_field(cb, fsm))
        msg = Msg(SECRET)
        asyncio.run(H.ns_one_field_input(msg, fsm))
        self.assertNotIn(SECRET, "\n".join(msg.said))

    def test_an_empty_value_changes_nothing(self):
        cb = CB("ns:set:1")
        fsm = FSM()
        asyncio.run(H.ns_set_field(cb, fsm))
        msg = Msg("   ")
        asyncio.run(H.ns_one_field_input(msg, fsm))
        self.assertNotIn("login", self.saved)

    def test_the_bulk_paste_still_works_from_the_screen(self):
        fsm = FSM()
        asyncio.run(H.ns_bulk_prompt(CB("ns:bulk"), fsm))
        msg = Msg(f"user_id: 1\nlogin: a\npassword: {PASSWORD}")
        asyncio.run(H.ns_bulk_input(msg, fsm))
        self.assertEqual(self.saved["user_id"], "1")
        self.assertTrue(msg.deleted)

    def test_deleting_clears_everything(self):
        self.saved.update({"login": "a", "api_secret": SECRET})
        asyncio.run(H.ns_delete(CB("ns:del")))
        self.assertEqual(self.saved, {})

    def test_the_check_button_only_reads(self):
        """«Проверить вход» спрашивает баланс — покупок здесь быть не может."""
        import automation.nsgifts as N
        self.saved.update({"user_id": "1", "login": "a", "password": "b",
                           "api_secret": "c"})
        real = N.balance_sync
        N.balance_sync = lambda creds: (True, "305.2403")
        try:
            cb = CB("ns:check")
            asyncio.run(H.ns_check(cb))
        finally:
            N.balance_sync = real
        self.assertIn("305.2403", cb.message.last)

    def test_a_refused_check_says_why(self):
        import automation.nsgifts as N
        self.saved.update({"user_id": "1", "login": "a", "password": "b",
                           "api_secret": "c"})
        real = N.balance_sync
        N.balance_sync = lambda creds: (False, "доступ запрещён — возможно, "
                                               "наш IP не в белом списке")
        try:
            cb = CB("ns:check")
            asyncio.run(H.ns_check(cb))
        finally:
            N.balance_sync = real
        self.assertIn("белом списке", cb.message.last)


class TheCommandsAreWiredIn(unittest.TestCase):
    def test_the_router_is_included_in_main(self):
        import inspect

        import main as M
        self.assertIn("dp.include_router(nsgifts.router)",
                      inspect.getsource(M.main))

    def test_none_of_the_commands_buys_anything(self):
        """Диагностика, которая тратит деньги, — не диагностика."""
        import inspect
        src = inspect.getsource(H)
        for forbidden in ("create_order", "pay_order"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
