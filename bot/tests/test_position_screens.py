"""The screens: every button leads somewhere, every message is sendable.

A dead button and an unbalanced <b> both fail the same way in Telegram — the
seller taps and nothing happens — and neither shows up in a unit test of the
rules. So the keyboards are rendered here and checked against the handlers that
are actually registered on the router.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import position as P              # noqa: E402
import handlers.selenium_settings as S            # noqa: E402


def settings(n_watches=1, **pp) -> dict:
    base = {
        "enabled": True, "interval_hours": 1, "auto_promote": False,
        "undercut_notify": True, "cooldown_hours": 6, "daily_limit": 3,
        "watches": [dict(P.new_watch(f"https://yoomarket.net/p/{i}",
                                     title=f"Товар {i}", item_id=str(1000 + i)))
                    for i in range(n_watches)],
    }
    base.update(pp)
    return {"promo_position": base,
            "auto_bump": {"promo": {"values": {"up_id": "1"}, "price": 49}},
            "bump_stats": {}}


def buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


# --- what the router will actually answer ----------------------------------

def _registered() -> tuple[set, list]:
    """(exact callback_data handled, prefixes handled) — read off the source.

    Reading the source beats introspecting aiogram's filters: the magic-filter
    objects do not expose the compared value in a stable way, and a test that
    silently inspects nothing is worse than no test.
    """
    src = open(S.__file__, encoding="utf-8").read()
    exact = set(re.findall(r'F\.data == "([^"]+)"', src))
    prefix = re.findall(r'F\.data\.startswith\("([^"]+)"\)', src)
    for group in re.findall(r'F\.data\.in_\(\{([^}]+)\}\)', src):
        exact |= set(re.findall(r'"([^"]+)"', group))
    return exact, prefix


EXACT, PREFIX = _registered()
# Buttons that hand off to screens living in other modules.
FOREIGN = {"menu:ads", "menu:main"}


def assert_live(test, kb, where):
    for b in buttons(kb):
        data = b.callback_data
        test.assertIsNotNone(data, f"{where}: button «{b.text}» has no callback")
        test.assertLessEqual(len(data.encode()), 64,
                             f"{where}: callback_data too long: {data}")
        ok = (data in EXACT or data in FOREIGN
              or any(data.startswith(p) for p in PREFIX))
        test.assertTrue(ok, f"{where}: nothing handles «{data}» (button «{b.text}»)")


TAG = re.compile(r"</?([a-z]+)[^>]*>")


def assert_html_ok(test, text, where):
    stack = []
    for m in TAG.finditer(text):
        tag = m.group(1)
        if m.group(0).startswith("</"):
            test.assertTrue(stack and stack[-1] == tag,
                            f"{where}: </{tag}> does not close anything")
            stack.pop()
        elif not m.group(0).endswith("/>"):
            stack.append(tag)
    test.assertEqual(stack, [], f"{where}: unclosed {stack}")
    test.assertLessEqual(len(text), 4096, f"{where}: too long for Telegram")


class Screens(unittest.TestCase):
    def test_overview_in_every_shape(self):
        for n in (0, 1, 3, P.MAX_WATCHES):
            for auto in (False, True):
                s = settings(n, auto_promote=auto)
                where = f"pos:menu n={n} auto={auto}"
                assert_html_ok(self, S._pos_text(s), where)
                assert_live(self, S._pos_kb(s), where)

    def test_watch_card(self):
        s = settings(2)
        s["promo_position"]["watches"][0].update(
            {"last_pos": 7, "min_price": 90, "undercut_guard": 25,
             "alarmed": True, "promos_today": 2})
        for idx in (0, 1):
            assert_html_ok(self, S._watch_text(s, idx), f"card {idx}")
            assert_live(self, S._watch_kb(s, idx), f"card {idx}")

    def test_watch_card_survives_a_deleted_watch(self):
        s = settings(1)
        self.assertIn("удалено", S._watch_text(s, 5))
        assert_live(self, S._watch_kb(s, 5), "card out of range")

    def test_unbound_listing_is_called_out(self):
        s = settings(1)
        s["promo_position"]["watches"][0]["item_id"] = ""
        self.assertIn("не привязан", S._watch_text(s, 0))

    def test_titles_from_the_marketplace_cannot_break_the_send(self):
        s = settings(1)
        s["promo_position"]["watches"][0]["title"] = 'Аккаунт <b>"Steam"</b> & Co'
        assert_html_ok(self, S._watch_text(s, 0), "hostile title (card)")
        assert_html_ok(self, S._pos_text(s), "hostile title (list)")

    def test_button_rows_match_the_buttons(self):
        for n in (0, 1, 3):
            for auto in (False, True):
                kb = S._pos_kb(settings(n, auto_promote=auto))
                flat = buttons(kb)
                self.assertEqual(len(flat), len(set(b.callback_data for b in flat)),
                                 "a callback appears twice on one screen")
                for row in kb.inline_keyboard:
                    self.assertLessEqual(len(row), 3)

    def test_the_spending_limits_are_reachable_in_both_modes(self):
        """Including «сигнал».

        They were hidden behind the «поднимать» switch, which put the budget
        out of reach exactly when a seller wants to set it — before letting the
        bot spend anything.
        """
        for auto in (False, True):
            got = [b.callback_data
                   for b in buttons(S._pos_kb(settings(1, auto_promote=auto)))]
            for cb in ("pos:budget", "pos:limit", "pos:cooldown"):
                self.assertIn(cb, got, f"missing in auto={auto}")

    def test_the_screen_says_the_limits_are_not_active_yet_in_signal_mode(self):
        text = S._pos_text(settings(1, auto_promote=False))
        self.assertIn("Ограничения трат", text)
        self.assertIn("вступят в силу", text)
        self.assertNotIn("вступят в силу",
                         S._pos_text(settings(1, auto_promote=True)))

    def test_a_budget_is_shown_with_what_it_buys(self):
        text = S._pos_text(settings(1, auto_promote=True, daily_budget=300))
        self.assertIn("300 ₽", text)
        self.assertIn("6 поднятий", text)      # tariff is 49 ₽ in the fixture

    def test_every_watch_is_reachable_from_the_list(self):
        s = settings(4)
        got = [b.callback_data for b in buttons(S._pos_kb(s))]
        for i in range(4):
            self.assertIn(f"pos:w:{i}", got)


class WatchLookup(unittest.TestCase):
    def test_index_out_of_range_is_refused_not_crashed(self):
        import storage
        old = storage.get_settings
        storage.get_settings = lambda uid: settings(2)
        S.get_settings = storage.get_settings
        try:
            self.assertIsNotNone(S._watch_at(1, "pos:w:1"))
            self.assertIsNone(S._watch_at(1, "pos:w:9"))
            self.assertIsNone(S._watch_at(1, "pos:w:-1"))
            self.assertIsNone(S._watch_at(1, "pos:w:abc"))
        finally:
            storage.get_settings = old
            S.get_settings = old


class Naming(unittest.TestCase):
    def test_a_watch_without_a_title_is_named_by_its_page(self):
        w = P.new_watch("https://yoomarket.net/game/steam/offers?id=7")
        self.assertTrue(S._watch_label(w, 0).startswith("1. "))
        self.assertNotIn("https", S._short_url(w["url"]))

    def test_long_urls_are_trimmed_for_a_button(self):
        w = P.new_watch("https://yoomarket.net/" + "x" * 200)
        self.assertLessEqual(len(S._watch_label(w, 0)), 40)


if __name__ == "__main__":
    unittest.main()
