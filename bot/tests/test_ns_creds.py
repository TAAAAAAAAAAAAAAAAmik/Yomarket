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
        self.assertIn("/ns_stock", got)

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
