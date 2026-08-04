"""Entering Fragment credentials one field at a time.

Two of the three cookies are HttpOnly, so `document.cookie` does not contain
them: pasting that string built half a session and the failure only surfaced at
the first delivery, with a buyer already waiting. Each value is asked for on its
own, and nothing already entered may be quietly undone.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage                                   # noqa: E402
import handlers.plugins as PL                    # noqa: E402
from tests.test_position_flow import (FakeCallback, FakeMessage,  # noqa: E402
                                      FakeState)


class CredsCase(unittest.TestCase):
    def setUp(self):
        self.store: dict = {}
        self._undo = []
        self.patch(storage, "get_fragment_creds",
                   lambda uid: copy.deepcopy(self.store))
        self.patch(PL, "get_fragment_creds",
                   lambda uid: copy.deepcopy(self.store))
        self.patch(PL, "save_fragment_creds", self._save)
        self.patch(PL, "get_settings", lambda uid: {
            "plugins": {"auto_stars": {"wallet_version": "v4r2"}}})

    def _save(self, uid, creds):
        self.store.update(copy.deepcopy(creds))

    def patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def tearDown(self):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def run_(self, coro):
        return asyncio.run(coro)

    def enter(self, index, text):
        """Tap a cookie button and send a value."""
        state = FakeState()
        self.run_(PL.stars_set_one_cookie_prompt(
            FakeCallback(f"plugins:stars:ck:{index}"), state))
        msg = FakeMessage(text)
        self.run_(PL.stars_set_one_cookie_input(msg, state))
        return msg

    def cookies(self):
        return self.store.get("cookies") or {}


class OneAtATime(CredsCase):
    def test_each_cookie_is_stored_under_its_own_name(self):
        for i, (name, _label) in enumerate(PL.FRAGMENT_COOKIES):
            self.enter(i, f"value-of-{name}")
        self.assertEqual(
            self.cookies(),
            {name: f"value-of-{name}" for name, _l in PL.FRAGMENT_COOKIES})

    def test_entering_one_does_not_drop_the_others(self):
        self.enter(0, "aaa")
        self.enter(1, "bbb")
        self.assertEqual(self.cookies()["stel_token"], "aaa")
        self.assertEqual(self.cookies()["stel_ssid"], "bbb")

    def test_a_value_pasted_with_its_name_is_cleaned_up(self):
        """People copy the whole row out of DevTools."""
        self.enter(0, "stel_token=abc123")
        self.assertEqual(self.cookies()["stel_token"], "abc123")

    def test_a_trailing_semicolon_is_not_part_of_the_value(self):
        self.enter(2, "tontoken;")
        self.assertEqual(self.cookies()["stel_ton_token"], "tontoken")

    def test_an_empty_value_changes_nothing(self):
        self.enter(0, "aaa")
        msg = self.enter(0, "   ")
        self.assertEqual(self.cookies()["stel_token"], "aaa")
        self.assertIn("❌", msg.sent[-1])

    def test_the_prompt_says_where_to_find_it(self):
        state = FakeState()
        cb = FakeCallback("plugins:stars:ck:0")
        self.run_(PL.stars_set_one_cookie_prompt(cb, state))
        said = cb.message.sent[-1]
        self.assertIn("stel_token", said)
        self.assertIn("Cookies", said)          # where in DevTools

    def test_what_is_still_missing_is_named(self):
        msg = self.enter(0, "aaa")
        said = msg.sent[-1]
        self.assertIn("stel_ssid", said)
        self.assertIn("Seed", said)

    def test_an_unknown_button_is_refused_not_crashed(self):
        cb = FakeCallback("plugins:stars:ck:99")
        self.run_(PL.stars_set_one_cookie_prompt(cb, FakeState()))
        self.assertTrue(cb.answers[0][1])


class PastingTheWholeString(CredsCase):
    def test_it_does_not_undo_the_hand_entered_ones(self):
        """`document.cookie` lacks the HttpOnly ones — replacing would lose them.

        That is the whole reason they were typed in by hand.
        """
        self.enter(0, "typed-token")
        self.enter(1, "typed-ssid")
        state = FakeState()
        self.run_(PL.stars_set_cookies_input(
            FakeMessage("stel_ton_token=from-console; other=x"), state))
        self.assertEqual(self.cookies()["stel_token"], "typed-token")
        self.assertEqual(self.cookies()["stel_ssid"], "typed-ssid")
        self.assertEqual(self.cookies()["stel_ton_token"], "from-console")

    def test_it_says_which_ones_the_console_could_not_give(self):
        state = FakeState()
        msg = FakeMessage("stel_ton_token=x; other=y")
        self.run_(PL.stars_set_cookies_input(msg, state))
        said = msg.sent[-1]
        self.assertIn("Не хватает", said)
        self.assertIn("stel_token", said)
        self.assertIn("document.cookie", said)

    def test_a_complete_string_is_accepted_without_complaint(self):
        state = FakeState()
        msg = FakeMessage("stel_token=a; stel_ssid=b; stel_ton_token=c")
        self.run_(PL.stars_set_cookies_input(msg, state))
        self.assertNotIn("Не хватает", msg.sent[-1])
        self.assertEqual(len(self.cookies()), 3)


class TheChecklist(CredsCase):
    def test_the_screen_shows_each_field_separately(self):
        self.enter(0, "aaa")
        cb = FakeCallback("plugins:stars:creds")
        self.run_(PL.stars_creds(cb, FakeState()))
        said = cb.message.sent[-1]
        self.assertIn("stel_token", said)
        self.assertIn("stel_ssid", said)
        self.assertIn("stel_ton_token", said)
        self.assertIn("Seed", said)
        self.assertEqual(said.count("🟢"), 1, "only one is filled in")

    def test_the_buttons_mark_what_is_done(self):
        self.enter(0, "aaa")
        kb = PL._creds_kb(False, self.cookies())
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("✅") and "stel_token" in l
                            for l in labels), labels)
        self.assertTrue(any(l.startswith("▫️") and "stel_ssid" in l
                            for l in labels), labels)


if __name__ == "__main__":
    unittest.main()
