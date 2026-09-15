"""Истёкшая сессия панели называется словами, а не кодом.

Продавец сообщил, что баланс второго магазина не показывается. Мы искали
поломку в многомагазинности — нашли две настоящие, но не эту: сессия
панели второго аккаунта просто истекла. Догадался продавец сам.

Бот это знал и не сказал. Проверялся один код — 401. Laravel на истёкшей
сессии отвечает ещё и 419 («Page Expired», просроченный CSRF), и
редиректом на /login — а редиректы мы намеренно не ходим, поэтому на
экран выходило «shops → 302». Английский код вместо объяснения — ровно та
отписка, которую проект запрещает.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P     # noqa: E402


class Resp:
    def __init__(self, code, location=""):
        self.status_code = code
        self.headers = {"Location": location} if location else {}

    def json(self):
        return {}


class EveryShapeOfAnExpiredSession(unittest.TestCase):
    def test_the_plain_unauthorised(self):
        self.assertIn("истекла", P._panel_expired(Resp(401)))

    def test_the_laravel_page_expired(self):
        """419 — самый частый вид истёкшей сессии у Laravel, и он не
        проверялся вовсе."""
        self.assertIn("истекла", P._panel_expired(Resp(419)))

    def test_a_redirect_to_the_login_page(self):
        self.assertIn("истекла",
                      P._panel_expired(Resp(302, "https://panel.yoomarket.net/login")))

    def test_a_redirect_with_no_destination_at_all(self):
        self.assertIn("истекла", P._panel_expired(Resp(302)))

    def test_it_tells_where_to_log_in_again(self):
        """Совет обязан быть выполнимым — иначе это та же отписка."""
        self.assertIn("Панель продавца", P._panel_expired(Resp(401)))

    def test_no_english_code_reaches_the_seller(self):
        for code in (401, 419, 302):
            self.assertNotIn("401", P._panel_expired(Resp(code)))
            self.assertNotIn("Page Expired", P._panel_expired(Resp(code)))


class ThingsThatAreNotAnExpiredSession(unittest.TestCase):
    """Назвать чужую беду истёкшей сессией — послать продавца входить
    заново туда, где он и так внутри."""

    def test_a_normal_answer(self):
        self.assertEqual(P._panel_expired(Resp(200)), "")

    def test_a_server_error(self):
        self.assertEqual(P._panel_expired(Resp(500)), "")

    def test_a_forbidden_is_about_rights_not_the_session(self):
        self.assertEqual(P._panel_expired(Resp(403)), "")

    def test_a_redirect_somewhere_else_entirely(self):
        self.assertEqual(
            P._panel_expired(Resp(302, "https://panel.yoomarket.net/resources/shops")),
            "")


class TheBalanceReaderSaysItOutLoud(unittest.TestCase):
    """Именно здесь продавец и увидел «shops → 302»."""

    def setUp(self):
        self._mk, self._hd = P._make_panel_requests_session, P._panel_xsrf_headers
        P._panel_xsrf_headers = lambda s, c: {}

    def tearDown(self):
        P._make_panel_requests_session, P._panel_xsrf_headers = self._mk, self._hd

    def run_with(self, resp):
        class S:
            def get(self_inner, url, **kw):
                return resp

        P._make_panel_requests_session = lambda c: S()
        return P.panel_shop_balance_sync("c", "", "Spike")

    def test_a_redirect_becomes_a_sentence(self):
        ok, err = self.run_with(Resp(302, "/login"))
        self.assertFalse(ok)
        self.assertIn("истекла", err)

    def test_and_not_a_bare_status_line(self):
        _ok, err = self.run_with(Resp(302, "/login"))
        self.assertNotIn("shops → 302", err)

    def test_page_expired_too(self):
        _ok, err = self.run_with(Resp(419))
        self.assertIn("истекла", err)


class EveryPanelReaderUsesTheSameCheck(unittest.TestCase):
    """Раньше «сессия истекла» было выписано строкой в шестнадцати местах,
    и починка одного оставляла пятнадцать как были."""

    def test_no_reader_hand_rolls_the_401_check(self):
        import inspect
        src = inspect.getsource(P)
        self.assertNotIn('"401: сессия панели истекла — войдите снова"', src)

    def test_the_shared_check_is_actually_used(self):
        import inspect
        src = inspect.getsource(P)
        self.assertGreater(src.count("_panel_expired("), 10)


if __name__ == "__main__":
    unittest.main()
