"""Выручка ложится на тот день, когда заказ был, а не когда бот его увидел.

Статистика без панели собирается из истории самого бота, и временем продажи
там служила метка `seen_at` — когда опрос впервые заметил заказ. На первом
проходе её получает вся история магазина разом, то есть «Сегодня» показывает
выручку за все месяцы сразу. У продавца с панелью это не всплывало: цифры
берутся оттуда. У продавца без панели — а таких большинство в первый день —
экран врал в разы.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import stats_source as S          # noqa: E402

DAY = 86400


def settings_with(**det) -> dict:
    """Один выполненный заказ на 500 ₽ с заданными полями времени."""
    base = {"title": "Ключ", "buyer": "Иван", "price": 500}
    base.update(det)
    return {"known_orders": {"77": "success"},
            "known_order_details": {"77": base}}


class ASaleIsDatedByTheOrderNotByTheGlance(unittest.TestCase):
    def test_an_old_order_seen_today_is_not_todays_revenue(self):
        """Первый проход бота на живом магазине: заказам месячной давности
        ставится сегодняшняя метка, и «Сегодня» показывает всю историю."""
        old = time.time() - 10 * DAY
        from datetime import datetime, timezone
        s = settings_with(
            created=datetime.fromtimestamp(old, timezone.utc).isoformat(),
            seen_at=time.time())
        today = S.summarize(S.local_events(s), since=S.day_start())
        self.assertEqual(today["revenue"], 0.0, today)

    def test_it_still_counts_in_the_month(self):
        """Заказ не должен и потеряться — он был, просто не сегодня."""
        old = time.time() - 10 * DAY
        from datetime import datetime, timezone
        s = settings_with(
            created=datetime.fromtimestamp(old, timezone.utc).isoformat(),
            seen_at=time.time())
        month = S.summarize(S.local_events(s), since=time.time() - 30 * DAY)
        self.assertEqual(month["revenue"], 500.0, month)

    def test_a_sale_made_today_still_counts_today(self):
        from datetime import datetime, timezone
        now = time.time()
        s = settings_with(
            created=datetime.fromtimestamp(now, timezone.utc).isoformat(),
            seen_at=now)
        today = S.summarize(S.local_events(s), since=S.day_start())
        self.assertEqual(today["revenue"], 500.0, today)


class WhenTheOrderCarriesNoDate(unittest.TestCase):
    """Дату маркетплейс отдаёт не всегда, и терять из-за этого заказ нельзя:
    ноль вместо времени выбросил бы его из любого окна."""

    def test_the_glance_is_used_as_a_fallback(self):
        now = time.time()
        s = settings_with(seen_at=now)
        today = S.summarize(S.local_events(s), since=S.day_start())
        self.assertEqual(today["revenue"], 500.0, today)

    def test_rubbish_in_the_date_falls_back_too(self):
        now = time.time()
        s = settings_with(created="позавчера", seen_at=now)
        today = S.summarize(S.local_events(s), since=S.day_start())
        self.assertEqual(today["revenue"], 500.0, today)

    def test_an_order_with_no_time_at_all_is_not_dropped(self):
        s = settings_with()
        self.assertEqual(len(S.local_events(s)), 1)


class ReadingTheDateInEveryShapeItComes(unittest.TestCase):
    def test_iso_with_a_zulu_suffix(self):
        self.assertAlmostEqual(
            S._created_ts("2026-08-01T12:00:00Z"), 1785585600.0, delta=1)

    def test_iso_without_a_zone_is_read_as_utc(self):
        self.assertAlmostEqual(
            S._created_ts("2026-08-01T12:00:00"), 1785585600.0, delta=1)

    def test_seconds_as_a_number(self):
        self.assertEqual(S._created_ts(1785585600), 1785585600.0)

    def test_milliseconds_are_not_taken_for_the_year_58000(self):
        self.assertEqual(S._created_ts(1785585600000), 1785585600.0)

    def test_nothing_readable_is_zero_not_a_guess(self):
        for bad in ("", None, "вчера", True, {}):
            self.assertEqual(S._created_ts(bad), 0.0, bad)


class ThePollerRemembersWhenTheOrderWasMade(unittest.TestCase):
    """Без этого поля предыдущим проверкам не на что было бы опереться:
    в истории бота даты заказа просто не было."""

    def test_the_created_field_is_stored_with_the_order(self):
        import inspect
        from tasks import manager as M
        src = inspect.getsource(M.TaskManager._process_orders)
        self.assertIn('"created": time_raw', src)


if __name__ == "__main__":
    unittest.main()
