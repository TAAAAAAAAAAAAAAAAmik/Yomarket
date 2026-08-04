"""Rules for position-triggered promotion — the part that spends money.

Run: python3 -m unittest discover -s bot/tests -t bot
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import position as P            # noqa: E402
from automation.market import _normalize, find_position   # noqa: E402


def offers(*rows) -> list[dict]:
    """rows: (title, price, seller) in the order the page shows them."""
    return _normalize([{"title": t, "price": p, "shop": s} for t, p, s in rows])


PAGE = offers(
    ("Аккаунт Steam", 100, "Конкурент А"),
    ("Аккаунт Steam", 120, "Конкурент Б"),
    ("Аккаунт Steam", 130, "Спайк"),
    ("Аккаунт Steam", 140, "Конкурент В"),
)


class FindPosition(unittest.TestCase):
    def test_by_seller(self):
        row = find_position(PAGE, seller="Спайк")
        self.assertEqual(row["pos"], 3)

    def test_by_title_when_seller_absent(self):
        page = offers(("Ключ Windows", 50, ""), ("Аккаунт Steam", 130, ""))
        self.assertEqual(find_position(page, title="Аккаунт Steam")["pos"], 2)

    def test_id_wins_over_title(self):
        page = _normalize([
            {"title": "Аккаунт Steam", "price": 100, "id": "1"},
            {"title": "Аккаунт Steam", "price": 130, "id": "220075"},
        ])
        self.assertEqual(find_position(page, ad_id="220075",
                                       title="Аккаунт Steam")["pos"], 2)

    def test_missing_is_none(self):
        self.assertIsNone(find_position(PAGE, seller="Кого-нет"))

    def test_identical_titles_do_not_beat_the_seller(self):
        """An offers list is everyone's copy of the same product.

        Matching the title first used to land on row 1 — reporting "you are
        first" whatever the real standing, which is the one answer that makes
        the whole feature lie.
        """
        row = find_position(PAGE, title="Аккаунт Steam", seller="Спайк")
        self.assertEqual(row["pos"], 3)

    def test_title_separates_two_listings_of_one_shop(self):
        page = offers(("Конкурент", 90, "Другой"),
                      ("Аккаунт Steam 5 лет", 130, "Спайк"),
                      ("Аккаунт Steam новый", 110, "Спайк"))
        self.assertEqual(
            find_position(page, seller="Спайк", title="Аккаунт Steam новый")["pos"], 3)

    def test_seller_alone_takes_the_first_of_its_rows(self):
        page = offers(("A", 90, "Другой"), ("B", 130, "Спайк"),
                      ("C", 110, "Спайк"))
        self.assertEqual(find_position(page, seller="Спайк")["pos"], 2)

    def test_falls_back_to_title_when_page_has_no_sellers(self):
        page = offers(("Ключ", 50, ""), ("Аккаунт Steam", 130, ""),
                      ("Другое", 70, ""))
        self.assertEqual(
            find_position(page, seller="Спайк", title="Аккаунт Steam")["pos"], 2)


class TwoKindsOfId(unittest.TestCase):
    """The storefront id finds the row; the panel id gets charged.

    They are different numbers for the same listing, and swapping them is the
    mistake that answers 404 at the moment money would be spent.
    """

    def test_the_row_is_found_by_the_storefront_id(self):
        page = _normalize([
            {"title": "Аккаунт Steam", "price": 100, "id": "1"},
            {"title": "Аккаунт Steam", "price": 130, "id": "220075"},
        ])
        w = P.new_watch("u", market_id="220075", item_id="99999")
        v = P.evaluate(w, page, shop="", pp={}, now=time.time())
        self.assertTrue(v.found)
        self.assertEqual(v.pos, 2)

    def test_the_panel_id_is_never_used_to_find_the_row(self):
        page = _normalize([
            {"title": "Аккаунт Steam", "price": 100, "id": "99999"},
            {"title": "Аккаунт Steam", "price": 130, "id": "220075"},
        ])
        w = P.new_watch("u", market_id="220075", item_id="99999")
        v = P.evaluate(w, page, shop="", pp={}, now=time.time())
        self.assertEqual(v.pos, 2, "matched the panel id — wrong row")

    def test_a_fresh_watch_has_no_panel_id_to_guess_from(self):
        self.assertEqual(P.new_watch("u")["item_id"], "")


class Thresholds(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.pp = {"auto_promote": True, "cooldown_hours": 6, "daily_limit": 3}

    def w(self, **kw):
        return P.new_watch("https://yoomarket.net/x", title="Аккаунт Steam", **kw)

    def test_within_threshold_is_silent(self):
        v = P.evaluate(self.w(max_position=3), PAGE, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertEqual(v.pos, 3)
        self.assertFalse(v.slipped)
        self.assertFalse(v.promote)

    def test_slipping_below_threshold_promotes(self):
        v = P.evaluate(self.w(max_position=2), PAGE, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertTrue(v.slipped)
        self.assertTrue(v.promote)
        self.assertTrue(any("3-е место" in l for l in v.lines))

    def test_manual_mode_never_spends(self):
        v = P.evaluate(self.w(max_position=2), PAGE, shop="Спайк",
                       pp={"auto_promote": False}, now=self.now)
        self.assertTrue(v.slipped)
        self.assertFalse(v.promote)
        self.assertIn("вручную", v.reason)

    def test_alert_fires_once_then_only_on_worsening(self):
        w = self.w(max_position=2)
        first = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now)
        self.assertTrue(first.lines)
        again = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp,
                           now=self.now + 3600)
        self.assertFalse([l for l in again.lines if "место" in l])

        worse = offers(("a", 1, "X"), ("b", 2, "Y"), ("c", 3, "Z"),
                       ("d", 4, "W"), ("Аккаунт Steam", 130, "Спайк"))
        third = P.evaluate(w, worse, shop="Спайк", pp=self.pp,
                           now=self.now + 7200)
        self.assertTrue(any("5-е место" in l for l in third.lines))

    def test_recovery_is_announced_once(self):
        w = self.w(max_position=2)
        P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now)
        good = offers(("Аккаунт Steam", 130, "Спайк"), ("b", 2, "Y"))
        back = P.evaluate(w, good, shop="Спайк", pp=self.pp, now=self.now + 3600)
        self.assertTrue(any("Вернулись" in l for l in back.lines))
        quiet = P.evaluate(w, good, shop="Спайк", pp=self.pp, now=self.now + 7200)
        self.assertFalse([l for l in quiet.lines if "Вернулись" in l])


class SpendingGuards(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.pp = {"auto_promote": True, "cooldown_hours": 6, "daily_limit": 3}

    def w(self, **kw):
        return P.new_watch("u", title="Аккаунт Steam", max_position=2, **kw)

    def test_undercut_guard_blocks_pointless_spend(self):
        # ours 130, cheapest rival 100 → beaten by 30, guard is 20
        v = P.evaluate(self.w(undercut_guard=20), PAGE, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertTrue(v.slipped)
        self.assertFalse(v.promote)
        self.assertIn("дешевле", v.reason)
        self.assertTrue(any("Не поднимаю" in l for l in v.lines))

    def test_undercut_guard_allows_when_close(self):
        v = P.evaluate(self.w(undercut_guard=50), PAGE, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertTrue(v.promote)

    def test_guard_off_by_default(self):
        self.assertTrue(P.evaluate(self.w(), PAGE, shop="Спайк",
                                   pp=self.pp, now=self.now).promote)

    def test_cooldown_blocks_second_promotion(self):
        w = self.w()
        v1 = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now)
        self.assertTrue(v1.promote)
        P.note_promotion(w, self.now)
        v2 = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now + 3600)
        self.assertFalse(v2.promote)
        self.assertIn("пауза", v2.reason)
        v3 = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp,
                        now=self.now + 7 * 3600)
        self.assertTrue(v3.promote)

    def test_daily_limit_stops_the_bleeding(self):
        w = self.w()
        pp = {"auto_promote": True, "cooldown_hours": 0, "daily_limit": 2}
        for i in range(2):
            v = P.evaluate(w, PAGE, shop="Спайк", pp=pp, now=self.now + i)
            self.assertTrue(v.promote, f"promotion {i + 1} should be allowed")
            P.note_promotion(w, self.now + i)
        v = P.evaluate(w, PAGE, shop="Спайк", pp=pp, now=self.now + 2)
        self.assertFalse(v.promote)
        self.assertIn("лимит", v.reason)

    def test_daily_limit_resets_next_day(self):
        w = self.w()
        pp = {"auto_promote": True, "cooldown_hours": 0, "daily_limit": 1}
        P.evaluate(w, PAGE, shop="Спайк", pp=pp, now=self.now)
        P.note_promotion(w, self.now)
        self.assertFalse(P.evaluate(w, PAGE, shop="Спайк", pp=pp,
                                    now=self.now + 60).promote)
        tomorrow = self.now + 26 * 3600
        self.assertTrue(P.evaluate(w, PAGE, shop="Спайк", pp=pp,
                                   now=tomorrow).promote)

    def test_zero_daily_limit_means_unlimited(self):
        w = self.w()
        pp = {"auto_promote": True, "cooldown_hours": 0, "daily_limit": 0}
        for i in range(5):
            self.assertTrue(P.evaluate(w, PAGE, shop="Спайк", pp=pp,
                                       now=self.now + i).promote)
            P.note_promotion(w, self.now + i)


class TheCompetitionThatMatters(unittest.TestCase):
    """Only the offers standing above us justify paying for a position.

    A real listing had a 1 ₽ lot far below — a price nobody competes with.
    Taken as "the competition", it would have blocked every promotion forever.
    """

    def setUp(self):
        self.now = time.time()
        self.pp = {"auto_promote": True, "cooldown_hours": 6, "daily_limit": 3}
        # We are 4th at 270 ₽; the rubbish lot is 6th.
        self.page = offers(("Лот", 239, "MoneyTravis"),
                           ("Лот", 1975, "Timka"),
                           ("Лот", 4775, "Timka"),
                           ("Аккаунт с виртами", 270, "Спайк"),
                           ("Лот", 125, "Timka"),
                           ("Лот", 1, "Timka"))

    def w(self, **kw):
        kw.setdefault("max_position", 3)
        return P.new_watch("u", title="Аккаунт с виртами", **kw)

    def test_a_cut_price_lot_below_us_does_not_block_the_promotion(self):
        v = P.evaluate(self.w(undercut_guard=50), self.page, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertEqual(v.pos, 4)
        self.assertEqual(v.cheapest, 1.0, "the raw minimum is still reported")
        self.assertEqual(v.cheapest_above, 239.0)
        self.assertTrue(v.promote, f"blocked by a lot nobody competes with: "
                                   f"{v.reason}")

    def test_someone_cheaper_above_us_does_block_it(self):
        # 270 − 239 = 31, over a guard of 20
        v = P.evaluate(self.w(undercut_guard=20), self.page, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertFalse(v.promote)
        self.assertIn("выше вас", v.reason)

    def test_first_place_has_nothing_above_it_to_measure_against(self):
        page = offers(("Аккаунт с виртами", 270, "Спайк"),
                      ("Лот", 1, "Timka"), ("Лот", 2, "Timka"),
                      ("Лот", 3, "Timka"), ("Лот", 4, "Timka"))
        v = P.evaluate(self.w(undercut_guard=1), page, shop="Спайк",
                       pp=self.pp, now=self.now)
        self.assertEqual(v.pos, 1)
        self.assertIsNone(v.cheapest_above)
        self.assertEqual(v.cheapest, 1.0)
        # Nothing to pay for and nothing to be undercut on: the reason must be
        # the position, never the price of somebody below.
        self.assertIn("порога", v.reason)
        self.assertNotIn("дешевле", v.reason)


class TheBudget(unittest.TestCase):
    """A rouble cap on the whole trigger, not a count per listing."""

    def setUp(self):
        self.now = time.time()
        self.pp = {"auto_promote": True, "cooldown_hours": 0, "daily_limit": 0,
                   "daily_budget": 100}

    def w(self):
        return P.new_watch("u", title="Аккаунт Steam", max_position=2)

    def test_spending_stops_at_the_cap(self):
        w = self.w()
        spent = 0
        for _ in range(5):
            v = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now,
                           price=49)
            if not v.promote:
                break
            P.note_promotion(w, self.now, 49, self.pp)
            spent += 49
        self.assertEqual(spent, 98, "a third 49 ₽ would pass 100 ₽")
        self.assertIn("бюджет", v.reason)

    def test_the_cap_is_shared_across_watches(self):
        a, b = self.w(), self.w()
        P.evaluate(a, PAGE, shop="Спайк", pp=self.pp, now=self.now, price=60)
        P.note_promotion(a, self.now, 60, self.pp)
        v = P.evaluate(b, PAGE, shop="Спайк", pp=self.pp, now=self.now,
                       price=60)
        self.assertFalse(v.promote, "the second watch spent the same money")

    def test_a_refused_promotion_costs_nothing(self):
        w = self.w()
        P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now, price=60)
        # nothing recorded — the panel refused
        self.assertEqual(P.spent_today(self.pp, self.now), 0)
        self.assertTrue(P.evaluate(w, PAGE, shop="Спайк", pp=self.pp,
                                   now=self.now, price=60).promote)

    def test_the_budget_resets_next_day(self):
        w = self.w()
        P.note_promotion(w, self.now, 100, self.pp)
        self.assertFalse(P.evaluate(w, PAGE, shop="Спайк", pp=self.pp,
                                    now=self.now, price=49).promote)
        tomorrow = self.now + 26 * 3600
        self.assertEqual(P.spent_today(self.pp, tomorrow), 0)
        self.assertTrue(P.evaluate(w, PAGE, shop="Спайк", pp=self.pp,
                                   now=tomorrow, price=49).promote)

    def test_no_budget_set_means_no_cap(self):
        pp = {"auto_promote": True, "cooldown_hours": 0, "daily_limit": 0}
        w = self.w()
        for _ in range(6):
            v = P.evaluate(w, PAGE, shop="Спайк", pp=pp, now=self.now,
                           price=999)
            self.assertTrue(v.promote)
            P.note_promotion(w, self.now, 999, pp)

    def test_what_is_left_is_reportable(self):
        self.assertEqual(P.budget_left(self.pp, self.now), 100)
        P.note_promotion(self.w(), self.now, 49, self.pp)
        self.assertEqual(P.budget_left(self.pp, self.now), 51)
        self.assertEqual(P.budget_left({}, self.now), -1, "no cap set")

    def test_a_promotion_that_would_overshoot_is_refused_whole(self):
        """No partial spending: 60 ₽ left, a 99 ₽ promotion does not happen."""
        pp = dict(self.pp, daily_budget=60)
        w = self.w()
        v = P.evaluate(w, PAGE, shop="Спайк", pp=pp, now=self.now, price=99)
        self.assertFalse(v.promote)
        self.assertEqual(P.spent_today(pp, self.now), 0)

    def test_the_cost_of_the_decision_is_carried_on_the_verdict(self):
        v = P.evaluate(self.w(), PAGE, shop="Спайк", pp=self.pp, now=self.now,
                       price=49)
        self.assertTrue(v.promote)
        self.assertEqual(v.cost, 49)


class MissingListing(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.pp = {"auto_promote": True}

    def test_absent_listing_never_promotes(self):
        w = P.new_watch("u", title="Нет такого")
        v = P.evaluate(w, PAGE, shop="Никто", pp=self.pp, now=self.now)
        self.assertFalse(v.found)
        self.assertFalse(v.promote)
        self.assertFalse(v.lines)               # first miss is not worth a ping

    def test_repeated_misses_are_reported_once(self):
        w = P.new_watch("u", title="Нет такого")
        said = []
        for i in range(6):
            said.extend(P.evaluate(w, PAGE, shop="Никто", pp=self.pp,
                                   now=self.now + i * 3600).lines)
        self.assertEqual(len([l for l in said if "Не нахожу" in l]), 1)

    def test_recovery_after_misses(self):
        w = P.new_watch("u", title="Аккаунт Steam")
        for i in range(3):
            P.evaluate(w, offers(("Другое", 1, "X"), ("Ещё", 2, "Y"),
                                 ("Третье", 3, "Z")),
                       shop="Никто", pp=self.pp, now=self.now + i)
        v = P.evaluate(w, PAGE, shop="Спайк", pp=self.pp, now=self.now + 10)
        self.assertTrue(v.found)
        self.assertTrue(any("снова найден" in l for l in v.lines))
        self.assertEqual(w["misses"], 0)


class Scheduling(unittest.TestCase):
    def test_is_due_respects_interval(self):
        now = time.time()
        w = P.new_watch("u")
        self.assertTrue(P.is_due(w, {"interval_hours": 1}, now))
        w["last_check"] = now
        self.assertFalse(P.is_due(w, {"interval_hours": 1}, now + 600))
        self.assertTrue(P.is_due(w, {"interval_hours": 1}, now + 3700))

    def test_evaluate_stamps_last_check(self):
        w = P.new_watch("u", title="Аккаунт Steam")
        now = time.time()
        P.evaluate(w, PAGE, shop="Спайк", pp={}, now=now)
        self.assertEqual(w["last_check"], now)

    def test_broken_interval_falls_back(self):
        self.assertEqual(P.interval_hours({"interval_hours": "nonsense"}),
                         P.DEFAULT_INTERVAL_HOURS)
        self.assertEqual(P.interval_hours({}), P.DEFAULT_INTERVAL_HOURS)


class LegacyMigration(unittest.TestCase):
    def test_single_page_settings_become_a_watch(self):
        pp = {"url": "https://yoomarket.net/p/1", "max_position": 5,
              "min_price": 90, "last_pos": 7, "last_alert_pos": 7}
        got = P.watches(pp)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["url"], "https://yoomarket.net/p/1")
        self.assertEqual(got[0]["max_position"], 5)
        self.assertEqual(got[0]["min_price"], 90)
        self.assertEqual(got[0]["last_pos"], 7)
        self.assertIs(P.watches(pp), pp["watches"])     # persisted, not rebuilt

    def test_empty_settings_give_no_watches(self):
        self.assertEqual(P.watches({}), [])

    def test_existing_watches_win(self):
        pp = {"url": "legacy", "watches": [P.new_watch("real")]}
        self.assertEqual(P.watches(pp)[0]["url"], "real")


if __name__ == "__main__":
    unittest.main()
