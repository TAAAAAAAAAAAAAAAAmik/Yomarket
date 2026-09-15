"""The scheduled position check, end to end, with the network stubbed out.

This is where a mistake costs real money, so what is asserted is not the text
but the spending: which listing was charged, how many times, and that a run
that decides not to pay really does not call the panel.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage                                    # noqa: E402
from automation import market as M                # noqa: E402
from automation import position as P              # noqa: E402


def page(*rows) -> dict:
    return {"offers": M._normalize(
        [{"title": t, "price": p, "shop": s} for t, p, s in rows]), "note": ""}


TOP = page(("Аккаунт Steam", 130, "Спайк"),
           ("Аккаунт Steam", 140, "Конкурент А"),
           ("Аккаунт Steam", 150, "Конкурент Б"))

SLIPPED = page(("Аккаунт Steam", 100, "Конкурент А"),
               ("Аккаунт Steam", 110, "Конкурент Б"),
               ("Аккаунт Steam", 120, "Конкурент В"),
               ("Аккаунт Steam", 130, "Спайк"))


class Harness:
    """Stands in for the panel, the storefront and the settings store."""

    def __init__(self, test, *, settings: dict, offers=SLIPPED, fetch_ok=True):
        self.test = test
        self.uid = 4242
        self.settings = settings
        self.offers = offers
        self.fetch_ok = fetch_ok
        self.charged: list[str] = []
        self.bump_result = (True, "оплатите: https://pay/1")
        self._undo = []

    def _patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def __enter__(self):
        from automation import panel
        from handlers import selenium_settings

        def fetch(url, shop="", category_id=None):
            return (True, self.offers) if self.fetch_ok else (False, "HTTP 503")

        def bump(cookies, item_id, uid=None, confirm=False, params=None):
            self.test.assertTrue(confirm, "a paid action must be confirmed")
            self.test.assertTrue(params, "a paid action must carry the tariff")
            self.charged.append(str(item_id))
            return self.bump_result

        self._patch(M, "fetch_listing", fetch)
        self._patch(panel, "panel_bump_item_sync", bump)
        self._patch(storage, "get_shop_name", lambda uid: "Спайк")
        self._patch(storage, "get_panel_creds", lambda uid: {"cookies": "c=1"})
        self._patch(storage, "get_settings", lambda uid: self.settings)
        self._patch(storage, "save_settings", lambda uid, s: None)
        self._patch(selenium_settings, "get_settings", lambda uid: self.settings)
        return self

    def __exit__(self, *exc):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)
        return False

    def run(self, now=None):
        from tasks.manager import TaskManager
        tm = TaskManager.__new__(TaskManager)          # no Bot needed
        return asyncio.run(tm._check_position(
            self.uid, self.settings, time.time() if now is None else now))


def settings_for(**pp) -> dict:
    base = {
        "enabled": True,
        "interval_hours": 1,
        "auto_promote": True,
        "cooldown_hours": 6,
        "daily_limit": 3,
        "watches": [dict(P.new_watch("https://yoomarket.net/p/1",
                                     title="Аккаунт Steam", item_id="220075",
                                     max_position=3))],
    }
    base.update(pp)
    return {"promo_position": base,
            "auto_bump": {"promo": {"values": {"up_id": "1"}, "price": 49}}}


# Полдень по времени продавца. Тесты дневных лимитов гоняют шесть часовых
# проходов подряд: от `time.time()` они то и дело переваливали за полночь, и
# счётчик суток честно обнулялся — тест падал в зависимости от часа запуска.
# С полудня шесть часов внутри одних суток при любом часовом поясе.
def midday() -> float:
    import localtime as _lt
    from datetime import datetime, timedelta, timezone
    local = _lt.now(None).replace(hour=12, minute=0, second=0, microsecond=0)
    return (local - timedelta(minutes=_lt.offset_minutes(None))).replace(
        tzinfo=timezone.utc).timestamp()


class PaidPromotion(unittest.TestCase):
    def test_slipping_charges_exactly_the_watched_listing(self):
        s = settings_for()
        with Harness(self, settings=s) as h:
            note = h.run()
        self.assertEqual(h.charged, ["220075"])
        self.assertIn("4 место", note)
        self.assertIn("Поднял", note)
        self.assertIn("49 ₽", note)

    def test_holding_position_charges_nothing_and_says_nothing(self):
        s = settings_for()
        with Harness(self, settings=s, offers=TOP) as h:
            note = h.run()
        self.assertEqual(h.charged, [])
        self.assertEqual(note, "")

    def test_signal_mode_never_charges(self):
        s = settings_for(auto_promote=False)
        with Harness(self, settings=s) as h:
            note = h.run()
        self.assertEqual(h.charged, [])
        self.assertIn("4 место", note)

    def test_cooldown_stops_the_second_hour(self):
        s = settings_for()
        now = time.time()
        with Harness(self, settings=s) as h:
            h.run(now)
            self.assertEqual(h.charged, ["220075"])
            h.run(now + 3600)                  # an hour later, still 4th
            self.assertEqual(h.charged, ["220075"], "cooldown must hold")
            h.run(now + 7 * 3600)
            self.assertEqual(h.charged, ["220075", "220075"])

    def test_daily_cap_holds_across_a_long_slip(self):
        s = settings_for(cooldown_hours=0, daily_limit=2)
        now = midday()
        with Harness(self, settings=s) as h:
            for i in range(6):
                h.run(now + i * 3600)
        self.assertEqual(len(h.charged), 2)

    def test_no_tariff_means_no_charge_and_a_clear_reason(self):
        s = settings_for()
        s["auto_bump"] = {}
        with Harness(self, settings=s) as h:
            note = h.run()
        self.assertEqual(h.charged, [])
        self.assertIn("тариф", note)

    def test_unbound_listing_reports_instead_of_charging_something_else(self):
        s = settings_for()
        s["promo_position"]["watches"][0]["item_id"] = ""
        with Harness(self, settings=s) as h:
            note = h.run()
        self.assertEqual(h.charged, [])
        self.assertIn("не привязан", note)

    def test_a_refused_payment_is_reported_not_counted(self):
        s = settings_for()
        now = time.time()
        with Harness(self, settings=s) as h:
            h.bump_result = (False, "У вас нет прав для выполнения этого действия")
            note = h.run(now)
            self.assertIn("Не поднял", note)
            # A refusal is not a promotion, so the cooldown must not start
            h.bump_result = (True, "ok")
            h.run(now + 3600)
        self.assertEqual(len(h.charged), 2)

    def test_the_same_failure_is_not_repeated_every_hour(self):
        s = settings_for(cooldown_hours=0)
        s["auto_bump"] = {}                       # no tariff — fails every time
        now = time.time()
        with Harness(self, settings=s) as h:
            notes = [h.run(now + i * 3600) for i in range(4)]
        self.assertEqual(len([n for n in notes if "тариф" in n]), 1, notes)

    def test_a_changed_failure_is_reported_again(self):
        s = settings_for(cooldown_hours=0)
        now = time.time()
        with Harness(self, settings=s) as h:
            h.bump_result = (False, "нет прав")
            first = h.run(now)
            h.bump_result = (False, "не хватает средств")
            second = h.run(now + 3600)
        self.assertIn("нет прав", first)
        self.assertIn("не хватает средств", second)

    def test_the_daily_budget_stops_the_spending(self):
        """A rouble cap on the whole trigger, honoured by the scheduler.

        The count-per-listing cap says nothing about money once there are
        several watches; this is the number the seller actually cares about.
        """
        s = settings_for(cooldown_hours=0, daily_limit=0, daily_budget=100)
        now = midday()
        with Harness(self, settings=s) as h:
            for i in range(6):
                h.run(now + i * 3600)
        # 49 ₽ apiece: two fit under 100, a third would not
        self.assertEqual(len(h.charged), 2)
        self.assertEqual(s["promo_position"]["spent_today"], 98)

    def test_a_refused_promotion_does_not_eat_the_budget(self):
        s = settings_for(cooldown_hours=0, daily_limit=0, daily_budget=100)
        now = time.time()
        with Harness(self, settings=s) as h:
            h.bump_result = (False, "нет прав")
            h.run(now)
            self.assertEqual(s["promo_position"].get("spent_today", 0), 0)
            h.bump_result = (True, "оплатите: https://pay/1")
            h.run(now + 3600)
        self.assertEqual(s["promo_position"]["spent_today"], 49)

    def test_the_report_says_what_is_left(self):
        s = settings_for(cooldown_hours=0, daily_limit=0, daily_budget=200)
        with Harness(self, settings=s) as h:
            note = h.run()
        self.assertIn("осталось на сегодня 151 ₽", note)

    def test_price_guard_blocks_a_pointless_purchase(self):
        s = settings_for()
        s["promo_position"]["watches"][0]["undercut_guard"] = 20
        with Harness(self, settings=s) as h:      # rivals at 100, ours at 130
            note = h.run()
        self.assertEqual(h.charged, [])
        self.assertIn("Не поднимаю", note)


class Scheduling(unittest.TestCase):
    def test_interval_is_respected_between_runs(self):
        s = settings_for()
        now = time.time()
        with Harness(self, settings=s, offers=TOP) as h:
            h.run(now)
            first = s["promo_position"]["watches"][0]["last_check"]
            h.run(now + 60)                    # too soon — must not re-read
            self.assertEqual(s["promo_position"]["watches"][0]["last_check"],
                             first)

    def test_unreadable_page_is_silent_then_reported_once(self):
        s = settings_for(auto_promote=False)
        now = time.time()
        with Harness(self, settings=s, fetch_ok=False) as h:
            notes = [h.run(now + i * 3600) for i in range(5)]
        self.assertEqual(h.charged, [])
        said = [n for n in notes if "не читается" in n]
        self.assertEqual(len(said), 1, f"expected exactly one alarm, got {notes}")

    def test_the_loudest_possible_run_still_fits_in_one_message(self):
        """Every watch slipping, undercut, below its floor and paid for.

        That is the worst case, and an over-long send fails whole — the part
        that says money was spent included.
        """
        s = settings_for()
        s["promo_position"]["watches"] = [
            dict(P.new_watch(f"https://yoomarket.net/p/{i}",
                             title="Аккаунт Steam для длинного заголовка",
                             item_id=str(i), max_position=1, min_price=200))
            for i in range(P.MAX_WATCHES)]
        with Harness(self, settings=s) as h:
            h.bump_result = (True, "оплатите: https://pay.yoo.market/"
                                   + "x" * 140)
            note = h.run()
        self.assertLessEqual(len(note), 4096, f"{len(note)} chars — Telegram "
                                              f"would reject the whole send")
        self.assertIn("ПОЗИЦИЯ", note)
        self.assertIn("Поднял", note, "the spending must survive the trim")
        # And whatever the trim drops, it says so rather than ending mid-thought
        if note.count("Поднял") < P.MAX_WATCHES:
            self.assertIn("и ещё", note)

    def test_no_watches_is_a_no_op(self):
        s = settings_for(watches=[])
        with Harness(self, settings=s) as h:
            self.assertEqual(h.run(), "")
        self.assertEqual(h.charged, [])

    def test_several_watches_are_each_judged_on_their_own_page(self):
        s = settings_for()
        w2 = dict(P.new_watch("https://yoomarket.net/p/2", title="Аккаунт Steam",
                              item_id="330099", max_position=3))
        s["promo_position"]["watches"].append(w2)
        pages = {"https://yoomarket.net/p/1": TOP,
                 "https://yoomarket.net/p/2": SLIPPED}
        with Harness(self, settings=s) as h:
            M.fetch_listing = lambda url, shop="", category_id=None: (True, pages[url])
            note = h.run()
        self.assertEqual(h.charged, ["330099"],
                         "only the listing that actually slipped is paid for")
        self.assertIn("4 место", note)


if __name__ == "__main__":
    unittest.main()
