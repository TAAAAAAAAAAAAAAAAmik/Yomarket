"""Время — глазами продавца, а не сервера.

Контейнер живёт по UTC. Пока это не учитывалось, смещались не только даты
на экране, но и решения: ночной режим «22:00—09:00» применялся к часам
сервера и у продавца из UTC+5 работал с 03:00 до 14:00 — бот молчал весь
его вечер и отвечал среди рабочего дня. Так же сдвигались сутки: дневной
лимит трат обнулялся посреди дня, а утренние продажи попадали во
вчерашний отчёт.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import localtime as lt        # noqa: E402


def utc(y, m, d, hh, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


MSK = {"tz_offset_min": 180}
EKB = {"tz_offset_min": 300}
UTC = {"tz_offset_min": 0}


class TheOffsetIsReadSafely(unittest.TestCase):
    def test_moscow_is_the_default(self):
        """Маркетплейс сам пишет сроки «по МСК» — это ближе к правде, чем UTC."""
        self.assertEqual(lt.offset_minutes({}), 180)
        self.assertEqual(lt.offset_minutes(None), 180)

    def test_rubbish_falls_back_instead_of_crashing(self):
        for bad in ("завтра", None, [], {}):
            self.assertEqual(lt.offset_minutes({"tz_offset_min": bad}), 180)

    def test_impossible_zones_are_clamped(self):
        self.assertEqual(lt.offset_minutes({"tz_offset_min": 99999}),
                         lt.MAX_OFFSET_MIN)
        self.assertEqual(lt.offset_minutes({"tz_offset_min": -99999}),
                         lt.MIN_OFFSET_MIN)

    def test_the_label_reads_like_a_zone(self):
        self.assertEqual(lt.offset_label(MSK), "UTC+3 (МСК)")
        self.assertEqual(lt.offset_label(EKB), "UTC+5")
        self.assertEqual(lt.offset_label({"tz_offset_min": -210}), "UTC−3:30")


class AMomentLooksDifferentInDifferentZones(unittest.TestCase):
    MOMENT = utc(2026, 8, 9, 6, 50)          # 09:50 в Москве, 11:50 в Екб

    def test_moscow(self):
        self.assertEqual(lt.fmt(self.MOMENT, MSK), "09.08 09:50")

    def test_yekaterinburg(self):
        self.assertEqual(lt.fmt(self.MOMENT, EKB), "09.08 11:50")

    def test_utc(self):
        self.assertEqual(lt.fmt(self.MOMENT, UTC), "09.08 06:50")

    def test_an_empty_moment_shows_nothing(self):
        self.assertEqual(lt.fmt(0, MSK), "")
        self.assertEqual(lt.fmt(None, MSK), "")

    def test_a_broken_moment_does_not_crash_the_screen(self):
        self.assertEqual(lt.fmt(1e30, MSK), "")


class MidnightBelongsToTheSeller(unittest.TestCase):
    """Сутки продавца, а не сервера: у продавца из UTC+5 «сегодня»
    начиналось в пять утра, и утренние продажи попадали во вчерашний
    отчёт."""

    def test_just_after_local_midnight_is_already_the_new_day(self):
        moment = utc(2026, 8, 8, 19, 30)      # 00:30 девятого в Екатеринбурге
        self.assertEqual(lt.to_local(moment, EKB).strftime("%Y-%m-%d"),
                         "2026-08-09")

    def test_and_still_the_old_day_in_moscow(self):
        moment = utc(2026, 8, 8, 19, 30)      # 22:30 восьмого в Москве
        self.assertEqual(lt.to_local(moment, MSK).strftime("%Y-%m-%d"),
                         "2026-08-08")

    def test_day_start_is_local_midnight(self):
        from stats_source import day_start
        moment = utc(2026, 8, 9, 6, 0)
        started = day_start(moment, EKB)
        self.assertEqual(lt.to_local(started, EKB).strftime("%H:%M"), "00:00")
        self.assertLess(started, moment)

    def test_a_sale_at_local_dawn_counts_as_today(self):
        """Ровно тот случай: продажа в 06:00 по Екатеринбургу — это 01:00 по
        UTC, и по серверным суткам она уезжала во вчера."""
        from stats_source import day_start
        sale = utc(2026, 8, 9, 1, 0)          # 06:00 девятого в Екатеринбурге
        self.assertGreaterEqual(sale, day_start(sale, EKB))


class TheQuietWindowFollowsTheSellersClock(unittest.TestCase):
    """Окно «22:00—09:00» у продавца из UTC+5 работало с 03:00 до 14:00."""

    CONF = {"enabled": True, "quiet_only": True, "from_hour": 22, "to_hour": 9}

    def test_late_evening_in_yekaterinburg_is_quiet_time(self):
        import autoreply as ar
        moment = utc(2026, 8, 9, 17, 30)      # 22:30 в Екатеринбурге
        self.assertTrue(ar.in_quiet_window(self.CONF, moment, EKB))

    def test_the_same_moment_is_working_hours_in_moscow(self):
        import autoreply as ar
        moment = utc(2026, 8, 9, 17, 30)      # 20:30 в Москве
        self.assertFalse(ar.in_quiet_window(self.CONF, moment, MSK))

    def test_the_gate_says_no_during_the_seller_s_working_hours(self):
        import autoreply as ar
        moment = utc(2026, 8, 9, 9, 0)        # 14:00 в Екатеринбурге
        allowed, why = ar.gate(self.CONF, "1", moment, EKB)
        self.assertFalse(allowed)
        self.assertIn("рабочее время", why)

    def test_and_yes_during_his_night(self):
        import autoreply as ar
        moment = utc(2026, 8, 9, 20, 0)       # 01:00 десятого в Екатеринбурге
        allowed, _why = ar.gate(self.CONF, "1", moment, EKB)
        self.assertTrue(allowed)


class TheSubstitutedTimeIsTheSellersToo(unittest.TestCase):
    def test_the_placeholder_uses_his_clock(self):
        """{время} уходит покупателю — там было время сервера, то есть чужое."""
        import autoreply as ar
        ctx = ar.context({}, "1", "Магазин", EKB)
        self.assertEqual(ctx["time"], lt.now(EKB).strftime("%H:%M"))


class TheScreenLetsHimPickAZone(unittest.TestCase):
    class Screen:
        def __init__(self):
            self.texts: list[str] = []
            self.kbs: list = []

        async def edit_text(self, text, reply_markup=None, **kw):
            self.texts.append(text)
            self.kbs.append(reply_markup)
            return self

    class CB:
        def __init__(self, data):
            self.data = data
            self.message = TheScreenLetsHimPickAZone.Screen()
            self.from_user = type("U", (), {"id": 1})()

        async def answer(self, text="", **kw):
            pass

    def _wire(self, store):
        import handlers.settings as S
        self._real = (S.get_settings, S.save_settings)
        S.get_settings = lambda uid: store
        S.save_settings = lambda uid, s: store.update(s)
        return S

    def tearDown(self):
        import handlers.settings as S
        if hasattr(self, "_real"):
            S.get_settings, S.save_settings = self._real

    def test_picking_a_city_stores_its_offset(self):
        store: dict = {}
        S = self._wire(store)
        asyncio.run(S.tz_set(self.CB("tz:set:300")))
        self.assertEqual(store["tz_offset_min"], 300)

    def test_the_chosen_one_is_ticked(self):
        store = {"tz_offset_min": 300}
        S = self._wire(store)
        cb = self.CB("tz:menu")
        asyncio.run(S.tz_menu(cb))
        labels = [b.text for row in cb.message.kbs[-1].inline_keyboard
                  for b in row]
        self.assertIn("✅ Екатеринбург", labels)

    def test_nonsense_is_refused_rather_than_stored(self):
        store: dict = {}
        S = self._wire(store)
        asyncio.run(S.tz_set(self.CB("tz:set:99999")))
        self.assertNotIn("tz_offset_min", store)

    def test_the_screen_shows_the_time_he_should_recognise(self):
        store = {"tz_offset_min": 300}
        S = self._wire(store)
        cb = self.CB("tz:menu")
        asyncio.run(S.tz_menu(cb))
        self.assertIn(lt.now(store).strftime("%H:%M"), cb.message.texts[-1])


class TheClockIsVisibleWithoutHuntingForIt(unittest.TestCase):
    """От часового пояса зависят час итогов дня, окно ночного режима и
    граница суток в статистике, а увидеть его можно было только внутри
    «Настроек». Вопрос «какое время в боте» не должен требовать хождения
    по экранам: /version отвечает на все такие вопросы разом.
    """

    def test_version_shows_the_sellers_own_clock(self):
        import inspect
        from handlers import start as S
        src = inspect.getsource(S.cmd_version)
        self.assertIn("Твоё время", src)
        self.assertIn("offset_label", src)

    def test_the_line_actually_reaches_the_message(self):
        """Строку легко собрать и забыть подставить."""
        import inspect
        from handlers import start as S
        src = inspect.getsource(S.cmd_version)
        self.assertIn("{time_line}", src)


if __name__ == "__main__":
    unittest.main()
