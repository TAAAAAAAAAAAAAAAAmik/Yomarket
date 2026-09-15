"""Данные первого магазина не достаются второму.

Продавец добавил второй магазин — и увидел в меню название первого. Это
верхушка: три хранилища (настройки, куки панели, данные Fragment) заведены
ещё до многомагазинности, под голым номером пользователя, и запись
переносилась **в активный аккаунт**. А `add_account` делает новый аккаунт
активным сразу, так что «первое чтение после переноса» наступало уже под
вторым магазином — ему и доставалось чужое.

Название магазина здесь самое безобидное из трёх. Вместе с ним уезжали
куки панели и **seed-фраза TON**, то есть доступ к чужому кошельку.
Проверка «после переключения видны чужие заказы» этого не ловит: заказы
разъедутся заметно, ключи — нет.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage as S      # noqa: E402


UID = 777001


class Blobs(unittest.TestCase):
    """Хранилище подменяется словарями в памяти — на диск ничего не идёт."""

    def setUp(self):
        self.tokens = {}
        self.blobs = {"settings": {}, "panel_creds": {}, "fragment_creds": {}}
        self._load, self._save = S._load, S._save_tokens
        self._read, self._write = S._read_blob, S._write_blob
        S._load = lambda: self.tokens
        S._save_tokens = lambda d: self.tokens.update(d)
        S._read_blob = lambda name: self.blobs.setdefault(name, {})
        S._write_blob = lambda name, d: self.blobs.__setitem__(name, d)

    def tearDown(self):
        S._load, S._save_tokens = self._load, self._save
        S._read_blob, S._write_blob = self._read, self._write

    def one_shop_from_before_accounts(self):
        """Установка, какой она была до многомагазинности."""
        self.tokens[str(UID)] = {"accounts": {"Основной": {"token": "t1"}},
                                 "active": "Основной"}
        self.blobs["settings"][str(UID)] = {"shop_name": "Spike"}
        self.blobs["panel_creds"][str(UID)] = {"cookies": "первого магазина"}
        self.blobs["fragment_creds"][str(UID)] = {"mnemonic": "двадцать четыре слова",
                                                  "cookies": {"stel_token": "x"}}

    def add_second(self, name="Магазин 2"):
        S.add_account(UID, name, "t2", make_active=True)


class TheSecondShopStartsEmpty(Blobs):
    def test_it_does_not_inherit_the_first_shops_name(self):
        """Ровно то, что увидел продавец: в меню имя первого магазина."""
        self.one_shop_from_before_accounts()
        self.add_second()
        self.assertEqual(S.get_shop_name(UID), "")

    def test_it_does_not_inherit_the_panel_login(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        self.assertIsNone(S.get_panel_creds(UID))

    def test_it_does_not_inherit_the_ton_seed_phrase(self):
        """Дороже всего остального: это доступ к чужому кошельку."""
        self.one_shop_from_before_accounts()
        self.add_second()
        self.assertIsNone(S.get_fragment_creds(UID))

    def test_it_does_not_inherit_the_order_history(self):
        self.one_shop_from_before_accounts()
        self.blobs["settings"][str(UID)]["known_orders"] = {"1200750": "done"}
        self.add_second()
        self.assertEqual(S.get_settings(UID).get("known_orders"), {})


class TheFirstShopKeepsEverything(Blobs):
    """Перенос обязан состояться — просто в другой адрес. Если запись
    потеряется, продавец лишится входа в панель и seed-фразы разом."""

    def test_the_name_is_there_after_switching_back(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.set_active_account(UID, "Основной")
        self.assertEqual(S.get_shop_name(UID), "Spike")

    def test_the_panel_login_is_there_after_switching_back(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.set_active_account(UID, "Основной")
        self.assertEqual((S.get_panel_creds(UID) or {}).get("cookies"),
                         "первого магазина")

    def test_the_seed_phrase_is_there_after_switching_back(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.set_active_account(UID, "Основной")
        self.assertEqual((S.get_fragment_creds(UID) or {}).get("mnemonic"),
                         "двадцать четыре слова")

    def test_a_single_shop_install_still_migrates(self):
        """Без второго аккаунта перенос работал и должен работать дальше."""
        self.one_shop_from_before_accounts()
        self.assertEqual(S.get_shop_name(UID), "Spike")
        self.assertNotIn(str(UID), self.blobs["settings"])

    def test_the_legacy_entry_is_read_even_when_the_second_is_active(self):
        """Перенос происходит один раз, кто бы ни читал первым."""
        self.one_shop_from_before_accounts()
        self.add_second()
        S.get_settings(UID)                       # читает второй аккаунт
        self.assertNotIn(str(UID), self.blobs["settings"])
        self.assertEqual(self.blobs["settings"][f"{UID}::Основной"]["shop_name"],
                         "Spike")


class TheTwoShopsDoNotBleedIntoEachOther(Blobs):
    def test_settings_saved_under_the_second_stay_there(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.save_shop_name(UID, "Второй магазин")
        S.set_active_account(UID, "Основной")
        self.assertEqual(S.get_shop_name(UID), "Spike")
        S.set_active_account(UID, "Магазин 2")
        self.assertEqual(S.get_shop_name(UID), "Второй магазин")

    def test_a_panel_login_under_the_second_does_not_touch_the_first(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.save_panel_creds(UID, {"cookies": "второго магазина"})
        S.set_active_account(UID, "Основной")
        self.assertEqual((S.get_panel_creds(UID) or {}).get("cookies"),
                         "первого магазина")

    def test_an_existing_first_account_entry_is_not_overwritten(self):
        """Если у первого уже есть своя запись, долистовая её не затирает."""
        self.one_shop_from_before_accounts()
        self.blobs["settings"][f"{UID}::Основной"] = {"shop_name": "новее"}
        self.assertEqual(S.get_shop_name(UID), "новее")
        self.assertNotIn(str(UID), self.blobs["settings"])


class WhichAccountsCanReachThePanel(Blobs):
    """«Баланс не показывается» после переключения обычно значит не «ноль»,
    а «в панель этого магазина никто не входил»."""

    def test_it_names_the_account_that_has_a_login(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.get_panel_creds(UID)                    # запускает перенос
        self.assertEqual(S.accounts_with_panel(UID), ["Основной"])

    def test_an_account_without_cookies_is_not_listed(self):
        self.one_shop_from_before_accounts()
        self.add_second()
        S.save_panel_creds(UID, {})               # пусто, не куки
        S.set_active_account(UID, "Основной")
        self.assertNotIn("Магазин 2", S.accounts_with_panel(UID))

    def test_the_balance_screen_says_it_out_loud(self):
        from handlers import balance as B
        self.one_shop_from_before_accounts()
        self.add_second()
        S.get_panel_creds(UID)
        hint = B._other_account_hint(UID)
        self.assertIn("Основной", hint)
        self.assertIn("Магазин 2", hint)

    def test_a_single_shop_install_gets_no_such_hint(self):
        """Подсказка про соседний аккаунт там, где аккаунт один, — шум."""
        from handlers import balance as B
        self.one_shop_from_before_accounts()
        self.assertEqual(B._other_account_hint(UID), "")


class TheShopNameComesFromTheMarketplace(unittest.TestCase):
    """/check отвечает {status, shop:{id,title}, …} — имя лежит в `title`.

    Без него в `save_shop_name` попадало название аккаунта, придуманное
    продавцом, и магазин в меню назывался «Магазин 2».
    """

    def test_the_add_account_step_reads_title(self):
        import inspect
        from handlers import accounts as A
        self.assertIn('shop.get("title")',
                      inspect.getsource(A.add_account_token))

    def test_the_switch_step_reads_title_too(self):
        import inspect
        from handlers import accounts as A
        self.assertIn('shop.get("title")', inspect.getsource(A._refresh_shop))


class TheDiagnosticKeepsSecretsSecret(unittest.TestCase):
    """Куки Fragment и seed-фраза не попадают в отчёты — это доступ к
    чужому кошельку. Печатается только наличие."""

    def test_it_prints_presence_not_values(self):
        import inspect
        from handlers import accounts as A
        src = inspect.getsource(A.accounts_debug)
        self.assertIn("frag.get('mnemonic')", src)
        self.assertNotIn("frag['mnemonic']", src)
        self.assertNotIn("_esc(frag", src)

    def test_the_command_name_is_not_taken_by_another_router(self):
        """Две команды с одним именем — не ошибка, а тишина."""
        import ast
        import pathlib
        names = []
        for path in pathlib.Path(
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "handlers")).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "id", "") == "Command"):
                    names += [a.value for a in node.args
                              if isinstance(a, ast.Constant)]
        self.assertEqual(names.count("accounts_debug"), 1, names)


if __name__ == "__main__":
    unittest.main()
