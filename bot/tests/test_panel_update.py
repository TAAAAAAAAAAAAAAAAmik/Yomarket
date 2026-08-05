"""Правка товара через панель: «обновлено» = «в панели теперь так».

Nova отвечает 200 и на форму, часть которой молча выбросила. Продавец
увидел «✅ Цена товара #223960 обновлена: 139 ₽», а на сайте осталась
прежняя цена — и после такого боту нельзя верить ни в чём.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import panel as P          # noqa: E402


class Reply:
    def __init__(self, payload=None, code=200, text=""):
        self._payload = payload
        self.status_code = code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeNova:
    """Панель, которая хранит поля и по-разному относится к правкам."""

    def __init__(self, fields=None, accepts=("price", "title"), code=200):
        self.fields = dict(fields or {"price": "450", "title": "Товар",
                                      "count": "3"})
        self.accepts = set(accepts)
        self.code = code
        self.puts: list[dict] = []

    def _payload(self):
        return {"fields": [{"attribute": k, "value": v}
                           for k, v in self.fields.items()]}

    def get(self, url, **kw):
        return Reply(self._payload())

    def post(self, url, data=None, **kw):
        form = dict(data or {})
        self.puts.append(form)
        if self.code == 200:
            for k, v in form.items():
                # Атрибута, которого у ресурса нет, Nova не заводит — она
                # его просто не видит.
                if k in self.accepts and k in self.fields:
                    self.fields[k] = v
        return Reply({"ok": True}, self.code, "текст ошибки")


class Case(unittest.TestCase):
    def setUp(self):
        self.nova = FakeNova()
        self._session = P._make_panel_requests_session
        self._hdrs = P._panel_xsrf_headers
        self._save = P._save_refreshed_cookies
        P._make_panel_requests_session = lambda ck: self.nova
        P._panel_xsrf_headers = lambda s, ck: {}
        P._save_refreshed_cookies = lambda *a, **kw: None

    def tearDown(self):
        P._make_panel_requests_session = self._session
        P._panel_xsrf_headers = self._hdrs
        P._save_refreshed_cookies = self._save

    def update(self, overrides):
        return P.panel_update_item_sync("cookie", "223960", overrides)


class WhenItWorks(Case):
    def test_a_change_that_stuck_is_reported_as_done(self):
        ok, msg = self.update({"price": 139})
        self.assertTrue(ok, msg)
        self.assertEqual(self.nova.fields["price"], "139")

    def test_the_panels_own_formatting_is_not_a_mismatch(self):
        """Отправили 139 — панель вернула «139.00». Это то же самое."""
        class Formatting(FakeNova):
            def post(self, url, data=None, **kw):
                out = FakeNova.post(self, url, data=data, **kw)
                self.fields["price"] = "139.00"
                return out

        self.nova = Formatting()
        ok, msg = self.update({"price": 139})
        self.assertTrue(ok, msg)

    def test_other_fields_are_preserved(self):
        self.update({"price": 139})
        self.assertEqual(self.nova.puts[0].get("title"), "Товар")
        self.assertEqual(self.nova.puts[0].get("_method"), "PUT")


class WhenTheChangeDoesNotStick(Case):
    def test_a_silently_ignored_field_is_not_called_success(self):
        """Ровно случай со скриншота: 200, «✅», и старая цена на сайте."""
        self.nova = FakeNova(accepts=("title",))     # цену панель игнорирует
        ok, msg = self.update({"price": 139})
        self.assertFalse(ok, msg)
        self.assertIn("не изменилось", msg)

    def test_it_says_what_is_there_instead(self):
        self.nova = FakeNova(accepts=("title",))
        _ok, msg = self.update({"price": 139})
        self.assertIn("450", msg, msg)
        self.assertIn("139", msg, msg)

    def test_a_field_the_panel_does_not_have_is_named_as_such(self):
        self.nova = FakeNova(fields={"cost": "450", "title": "Товар"})
        ok, msg = self.update({"price": 139})
        self.assertFalse(ok)
        self.assertIn("нет", msg, msg)

    def test_and_it_points_at_the_field_that_looks_like_the_price(self):
        """Иначе «поля price нет» — тупик: как оно называется, неизвестно."""
        self.nova = FakeNova(fields={"cost": "450", "title": "Товар"})
        _ok, msg = self.update({"price": 139})
        self.assertIn("cost", msg, msg)

    def test_an_http_error_is_still_an_http_error(self):
        self.nova = FakeNova(code=422)
        ok, msg = self.update({"price": 139})
        self.assertFalse(ok)
        self.assertIn("422", msg)

    def test_an_unreadable_recheck_is_neither_success_nor_failure(self):
        """Не выдаём за успех, но и не пугаем: запрос-то панель приняла."""
        nova = self.nova
        calls = {"n": 0}

        def get(url, **kw):
            calls["n"] += 1
            if calls["n"] > 1:               # перечитать не вышло
                return Reply(None, 500, "")
            return Reply(nova._payload())

        nova.get = get
        ok, msg = self.update({"price": 139})
        self.assertTrue(ok)
        self.assertIn("проверить не удалось", msg)


class ComparingValues(unittest.TestCase):
    def test_numbers_are_compared_as_numbers(self):
        for want, got in ((139, "139.00"), ("139", 139.0), (139, " 139 ₽")):
            self.assertTrue(P._same_value(want, got), (want, got))

    def test_different_numbers_are_different(self):
        self.assertFalse(P._same_value(139, "450"))

    def test_text_is_compared_as_text(self):
        self.assertTrue(P._same_value("Товар", "товар"))
        self.assertFalse(P._same_value("Товар", "Другой"))

    def test_a_missing_value_never_counts_as_equal(self):
        self.assertFalse(P._same_value(139, None))


if __name__ == "__main__":
    unittest.main()
