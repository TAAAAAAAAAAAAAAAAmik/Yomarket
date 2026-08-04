"""Walking the screens the way a seller does, with the handlers called for real.

Rules and keyboards are covered elsewhere; what is covered here is the wiring
between them — a handler that saves to the wrong place, or forgets to save at
all, passes every other test in this directory.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage                                     # noqa: E402
from automation import market as M                 # noqa: E402
from automation import position as P               # noqa: E402
import handlers.selenium_settings as S             # noqa: E402


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []
        self.from_user = type("U", (), {"id": 7})()

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = type("U", (), {"id": 7})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.state = None
        self.data = {}

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


PAGE = {"offers": M._normalize([
    {"title": "Аккаунт Steam", "price": 100, "shop": {"name": "Конкурент"}},
    {"title": "Аккаунт Steam", "price": 130, "shop": {"name": "Спайк"},
     "id": 220075},
    {"title": "Аккаунт Steam", "price": 140, "shop": {"name": "Другой"}},
]), "note": "тест"}


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.store = {"promo_position": {}}
        self._undo = []
        self.patch(storage, "get_settings", self._load)
        self.patch(storage, "save_settings", self._save)
        self.patch(storage, "get_shop_name", lambda uid: "Спайк")
        self.patch(S, "get_settings", self._load)
        self.patch(S, "save_settings", self._save)
        self.patch(M, "fetch_listing", lambda url, shop="": (True, PAGE))

    # Copies on the way in and out, like the real store: settings live in the
    # database, so a handler that mutates its dict and forgets to save changes
    # nothing. A shared dict would hide exactly that bug.
    def _load(self, uid):
        return copy.deepcopy(self.store)

    def _save(self, uid, s):
        self.store = copy.deepcopy(s)

    def patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def tearDown(self):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def run_(self, coro):
        return asyncio.run(coro)

    def watches(self):
        return P.watches(self.store.setdefault("promo_position", {}))


class AddingAWatch(FlowCase):
    def test_a_pasted_link_becomes_a_watch_and_finds_us(self):
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["url"], "https://yoomarket.net/p/1")
        # The probe recognises us and remembers what to match on later
        self.assertEqual(ws[0]["title"], "Аккаунт Steam")
        self.assertEqual(ws[0]["market_id"], "220075")
        # The panel id is a different number and must not be invented from it:
        # binding the listing in the panel is a separate, deliberate step.
        self.assertEqual(ws[0]["item_id"], "")
        self.assertEqual(ws[0]["last_pos"], 2)
        self.assertTrue(any("2-м месте" in t for t in msg.sent), msg.sent)

    def test_a_link_we_are_not_on_says_so_instead_of_pretending(self):
        self.patch(storage, "get_shop_name", lambda uid: "Другой магазин")
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(len(self.watches()), 1)
        self.assertTrue(any("нет" in t and "магазина" in t for t in msg.sent),
                        msg.sent)

    def test_an_unreadable_page_still_saves_the_watch_with_the_reason(self):
        self.patch(M, "fetch_listing", lambda url, shop="": (False, "HTTP 503"))
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(len(self.watches()), 1)
        self.assertTrue(any("503" in t for t in msg.sent), msg.sent)

    def test_junk_is_rejected_without_saving(self):
        msg = FakeMessage("это не ссылка")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(self.watches(), [])
        self.assertIn("https://", msg.sent[0])

    def test_the_same_page_is_not_watched_twice(self):
        for _ in range(2):
            self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                     FakeState()))
        self.assertEqual(len(self.watches()), 1)

    def test_the_list_is_capped(self):
        for i in range(P.MAX_WATCHES + 3):
            self.run_(S.pos_url_save(FakeMessage(f"https://yoomarket.net/p/{i}"),
                                     FakeState()))
        self.assertEqual(len(self.watches()), P.MAX_WATCHES)


class EditingAWatch(FlowCase):
    def setUp(self):
        super().setUp()
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))

    def test_threshold_round_trip(self):
        state = FakeState()
        cb = FakeCallback("pos:wt:0")
        self.run_(S.pos_threshold_start(cb, state))
        self.assertEqual(state.data.get("pos_idx"), 0)
        self.run_(S.pos_threshold_save(FakeMessage("5"), state))
        self.assertEqual(self.watches()[0]["max_position"], 5)

    def test_threshold_rejects_nonsense_and_keeps_the_state(self):
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        msg = FakeMessage("сто")
        self.run_(S.pos_threshold_save(msg, state))
        self.assertEqual(self.watches()[0]["max_position"],
                         P.DEFAULT_MAX_POSITION)
        self.assertIn("❌", msg.sent[0])
        self.assertIsNotNone(state.state, "the wizard must not drop out")

    def test_price_guard_round_trip(self):
        state = FakeState()
        self.run_(S.pos_guard_start(FakeCallback("pos:wg:0"), state))
        self.run_(S.pos_guard_save(FakeMessage("25,5"), state))
        self.assertEqual(self.watches()[0]["undercut_guard"], 25.5)

    def test_price_alarm_round_trip(self):
        state = FakeState()
        self.run_(S.pos_price_start(FakeCallback("pos:wp:0"), state))
        self.run_(S.pos_price_save(FakeMessage("90"), state))
        self.assertEqual(self.watches()[0]["min_price"], 90.0)

    def test_a_new_threshold_rearms_the_alert(self):
        self.watches()[0]["last_alert_pos"] = 9
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        self.run_(S.pos_threshold_save(FakeMessage("2"), state))
        self.assertEqual(self.watches()[0]["last_alert_pos"], 0)

    def test_editing_a_deleted_watch_does_not_crash(self):
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        self.store["promo_position"]["watches"] = []
        msg = FakeMessage("4")
        self.run_(S.pos_threshold_save(msg, state))
        self.assertIn("удалено", msg.sent[0])

    def test_delete_removes_it_and_turns_the_watcher_off(self):
        self.store["promo_position"]["enabled"] = True
        cb = FakeCallback("pos:wd:0")
        self.run_(S.pos_watch_delete(cb))
        self.assertEqual(self.watches(), [])
        self.assertFalse(self.store["promo_position"]["enabled"])


class Switches(FlowCase):
    def test_cannot_arm_an_empty_watcher(self):
        cb = FakeCallback("pos:toggle")
        self.run_(S.pos_toggle(cb))
        self.assertFalse(self.store["promo_position"].get("enabled"))
        self.assertTrue(cb.answers[0][1], "should be an alert, not a toast")

    def test_arming_schedules_an_immediate_check(self):
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))
        self.watches()[0]["last_check"] = 10 ** 10
        self.run_(S.pos_toggle(FakeCallback("pos:toggle")))
        self.assertTrue(self.store["promo_position"]["enabled"])
        self.assertEqual(self.watches()[0]["last_check"], 0)

    def test_auto_mode_warns_when_no_tariff_is_picked(self):
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))
        cb = FakeCallback("pos:auto")
        self.run_(S.pos_auto(cb))
        self.assertTrue(self.store["promo_position"]["auto_promote"])
        self.assertIn("тариф", cb.answers[0][0])

    def test_shop_wide_numbers_round_trip(self):
        for start, save, text, key, want in (
                (S.pos_interval_start, S.pos_interval_save, "0,5",
                 "interval_hours", 0.5),
                (S.pos_cooldown_start, S.pos_cooldown_save, "12",
                 "cooldown_hours", 12.0),
                (S.pos_limit_start, S.pos_limit_save, "1", "daily_limit", 1),
        ):
            state = FakeState()
            self.run_(start(FakeCallback("x"), state))
            self.run_(save(FakeMessage(text), state))
            self.assertEqual(self.store["promo_position"][key], want)


class LegacyUpgrade(FlowCase):
    def test_the_old_single_page_setup_shows_up_as_a_watch(self):
        self.store = {"promo_position": {
            "enabled": True, "url": "https://yoomarket.net/old",
            "max_position": 4, "min_price": 80, "last_pos": 6}}
        cb = FakeCallback("pos:menu")
        self.run_(S.pos_menu(cb, FakeState()))
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["max_position"], 4)
        self.assertEqual(ws[0]["min_price"], 80)
        screen = cb.message.sent[-1]
        self.assertIn("Товаров под наблюдением", screen)
        self.assertIn("6 место", screen)


if __name__ == "__main__":
    unittest.main()
