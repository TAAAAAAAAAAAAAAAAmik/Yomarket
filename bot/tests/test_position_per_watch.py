"""Настройки поднятия по позиции — у каждого товара свои.

Один интервал и один режим на весь магазин заставляли выбирать вслепую:
15 минут ради одного горячего товара — это 15 минут для всех десяти, а
«поднимать автоматически» — это платить за любой просевший, включая те, где
19 ₽ съедают продажу. Здесь проверяется, что товар может переопределить оба,
а общее значение остаётся значением по умолчанию.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import position as P          # noqa: E402


def watch(**kw) -> dict:
    w = P.new_watch("https://yoomarket.net/x", title="Товар", market_id="1",
                    item_id="900")
    w.update(kw)
    return w


class TheInterval(unittest.TestCase):
    def test_a_watch_without_one_follows_the_shop(self):
        self.assertEqual(P.watch_interval_hours(watch(), {"interval_hours": 3}), 3)

    def test_its_own_wins(self):
        self.assertEqual(
            P.watch_interval_hours(watch(interval_hours=0.5),
                                   {"interval_hours": 6}), 0.5)

    def test_nothing_anywhere_falls_back_to_the_default(self):
        self.assertEqual(P.watch_interval_hours(watch(), {}),
                         P.DEFAULT_INTERVAL_HOURS)

    def test_nothing_is_checked_more_often_than_the_loop_runs(self):
        """Фоновый цикл просыпается раз в полчаса — «каждые 5 минут» было бы
        обещанием, которого он не выполнит."""
        self.assertEqual(
            P.watch_interval_hours(watch(interval_hours=0.05), {}),
            P.MIN_INTERVAL_HOURS)
        self.assertEqual(P.interval_hours({"interval_hours": 0.05}),
                         P.MIN_INTERVAL_HOURS)

    def test_rubbish_in_the_field_does_not_crash_the_pass(self):
        self.assertEqual(
            P.watch_interval_hours(watch(interval_hours="скоро"),
                                   {"interval_hours": 2}), 2)

    def test_due_is_decided_per_watch(self):
        now = time.time()
        hot = watch(interval_hours=0.5)
        calm = watch(interval_hours=6)
        hot["last_check"] = calm["last_check"] = now - 3600      # час назад
        pp = {"interval_hours": 1}
        self.assertTrue(P.is_due(hot, pp, now), "горячий должен проверяться")
        self.assertFalse(P.is_due(calm, pp, now), "спокойный — ещё рано")


class TheMode(unittest.TestCase):
    def test_a_watch_without_one_follows_the_shop(self):
        self.assertTrue(P.watch_auto_promote(watch(), {"auto_promote": True}))
        self.assertFalse(P.watch_auto_promote(watch(), {"auto_promote": False}))

    def test_a_watch_can_pay_while_the_shop_only_warns(self):
        self.assertTrue(
            P.watch_auto_promote(watch(auto_promote=True), {"auto_promote": False}))

    def test_and_can_stay_silent_while_the_shop_pays(self):
        self.assertFalse(
            P.watch_auto_promote(watch(auto_promote=False), {"auto_promote": True}))

    def test_none_and_false_are_not_the_same_thing(self):
        """«Как у магазина» и «этому товару — нельзя» должны различаться."""
        self.assertIsNone(watch()["auto_promote"])
        self.assertTrue(P.watch_auto_promote(watch(), {"auto_promote": True}))
        self.assertFalse(
            P.watch_auto_promote(watch(auto_promote=False), {"auto_promote": True}))


class EvaluateHonoursIt(unittest.TestCase):
    OFFERS = [
        {"pos": 1, "id": "aa", "title": "Чужой", "price": 100, "seller": "Rival"},
        {"pos": 2, "id": "bb", "title": "Чужой 2", "price": 110, "seller": "Rival2"},
        {"pos": 3, "id": "cc", "title": "Чужой 3", "price": 120, "seller": "R3"},
        {"pos": 4, "id": "1", "title": "Товар", "price": 130, "seller": "Мой"},
    ]

    def _verdict(self, w, pp):
        return P.evaluate(w, self.OFFERS, shop="Мой", pp=pp, now=time.time(),
                          price=19)

    def test_a_paying_watch_pays_in_a_silent_shop(self):
        v = self._verdict(watch(auto_promote=True, max_position=3),
                          {"auto_promote": False})
        self.assertTrue(v.promote, v.reason)

    def test_a_silent_watch_does_not_pay_in_a_paying_shop(self):
        v = self._verdict(watch(auto_promote=False, max_position=3),
                          {"auto_promote": True})
        self.assertFalse(v.promote)
        self.assertIn("авто выключено", v.reason)

    def test_the_shop_default_still_applies(self):
        v = self._verdict(watch(max_position=3), {"auto_promote": True})
        self.assertTrue(v.promote, v.reason)

    def test_the_spending_guards_are_untouched(self):
        """Режим на товар не должен обходить защиту от невыгодного поднятия."""
        v = self._verdict(watch(auto_promote=True, max_position=3,
                                undercut_guard=10),
                          {"auto_promote": False})
        self.assertFalse(v.promote)
        self.assertIn("не окупится", v.reason)


class TheSpendingLog(unittest.TestCase):
    def test_a_promotion_is_written_down(self):
        pp, w = {}, watch()
        w["last_pos"] = 7
        now = time.time()
        P.note_promotion(w, now, 19, pp)
        self.assertEqual(len(pp["promo_log"]), 1)
        entry = pp["promo_log"][0]
        self.assertEqual(entry["price"], 19)
        self.assertEqual(entry["pos_before"], 7)
        self.assertEqual(entry["item_id"], "900")

    def test_the_next_check_records_what_came_of_it(self):
        pp, w = {}, watch()
        w["last_pos"] = 7
        now = time.time()
        P.note_promotion(w, now, 19, pp)
        P.note_position_after(pp, "900", 2, now + 3600)
        self.assertEqual(pp["promo_log"][0]["pos_after"], 2)

    def test_it_is_recorded_once_not_rewritten_every_pass(self):
        pp, w = {}, watch()
        w["last_pos"] = 7
        now = time.time()
        P.note_promotion(w, now, 19, pp)
        P.note_position_after(pp, "900", 2, now + 3600)
        P.note_position_after(pp, "900", 9, now + 7200)
        self.assertEqual(pp["promo_log"][0]["pos_after"], 2)

    def test_a_stale_promotion_gets_no_result(self):
        """Место через сутки говорит уже не о поднятии, а о рынке."""
        pp, w = {}, watch()
        w["last_pos"] = 7
        now = time.time()
        P.note_promotion(w, now, 19, pp)
        P.note_position_after(pp, "900", 2, now + 30 * 3600)
        self.assertIsNone(pp["promo_log"][0].get("pos_after"))

    def test_another_listings_result_is_not_written_here(self):
        pp, w = {}, watch()
        P.note_promotion(w, time.time(), 19, pp)
        P.note_position_after(pp, "777", 1, time.time())
        self.assertIsNone(pp["promo_log"][0].get("pos_after"))

    def test_the_log_does_not_grow_without_end(self):
        pp, w = {}, watch()
        for _ in range(P.PROMO_LOG_LIMIT + 40):
            P.note_promotion(w, time.time(), 19, pp)
        self.assertEqual(len(pp["promo_log"]), P.PROMO_LOG_LIMIT)

    def test_the_days_budget_is_still_charged_once(self):
        pp, w = {}, watch()
        now = time.time()
        P.note_promotion(w, now, 19, pp)
        P.note_promotion(w, now, 19, pp)
        self.assertEqual(pp["spent_today"], 38)


class TheReportScreen(unittest.TestCase):
    def _text(self, log):
        from handlers.selenium_settings import _promo_report_text
        return _promo_report_text({"promo_position": {"promo_log": log}})

    def test_with_nothing_spent_it_says_so(self):
        self.assertIn("Пока пусто", self._text([]))

    def test_it_adds_up_the_money(self):
        now = time.time()
        text = self._text([
            {"ts": now - 100, "title": "A", "price": 19, "item_id": "1"},
            {"ts": now - 200, "title": "B", "price": 19, "item_id": "2"},
        ])
        self.assertIn("38 ₽", text, text)

    def test_it_says_whether_it_helped(self):
        now = time.time()
        text = self._text([
            {"ts": now - 100, "title": "A", "price": 19, "item_id": "1",
             "pos_before": 7, "pos_after": 2},
            {"ts": now - 200, "title": "B", "price": 19, "item_id": "2",
             "pos_before": 5, "pos_after": 4},
        ])
        self.assertIn("помогло: <b>2</b> из 2", text, text)
        self.assertIn("3.0", text, text)          # (5 + 1) / 2 мест вверх

    def test_without_an_after_it_does_not_pretend_to_know(self):
        now = time.time()
        text = self._text([{"ts": now, "title": "A", "price": 19,
                            "item_id": "1", "pos_before": 7}])
        self.assertIn("Результат появится", text, text)

    def test_yesterdays_spending_is_not_todays(self):
        now = time.time()
        text = self._text([
            {"ts": now - 100, "title": "A", "price": 19, "item_id": "1"},
            {"ts": now - 3 * 86400, "title": "B", "price": 500, "item_id": "2"},
        ])
        today = text.split("За 7 дней")[0]
        self.assertIn("19 ₽", today, today)
        self.assertNotIn("519", today, today)

    def test_a_title_cannot_break_the_message(self):
        text = self._text([{"ts": time.time(), "title": "<b>x</b>",
                            "price": 19, "item_id": "1"}])
        self.assertIn("&lt;b&gt;", text)


if __name__ == "__main__":
    unittest.main()
