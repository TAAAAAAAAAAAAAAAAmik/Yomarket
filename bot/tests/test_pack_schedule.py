"""Пак поднимается по расписанию — и не превращается в весь магазин.

Расписание в боте одно на магазин. Пак не заводит свой будильник, а
становится тем набором, который поднимает общее расписание: второй путь к
тратам означал бы второй потолок, второй счётчик и второе место, где можно
ошибиться деньгами.

Отсюда две вещи, которые здесь и проверяются.

**Состав берётся из пака на каждом запуске**, а не из копии, снятой в
момент привязки. Копия разъехалась бы молча, и продавец платил бы за
вчерашний набор.

**Пустой список товаров в этом коде означает «поднимать все».** Поэтому
исчезнувший или опустевший пак обязан останавливать прогон. Иначе удаление
пака оборачивается оплатой поднятия всего магазина — тихо и один раз, до
конца дневного потолка.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.packs as PK                    # noqa: E402
import handlers.selenium_settings as SS        # noqa: E402


def shop(**extra):
    s = {"ad_packs": {"Утренний": ["101", "102", "103"],
                      "Вечерний": ["201"]},
         "auto_bump": {}}
    s.update(extra)
    return s


class TheListComesFromThePack(unittest.TestCase):
    def test_the_pack_decides_what_is_promoted(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        self.assertEqual(SS.promo_only_ids(s), ["101", "102", "103"])

    def test_an_item_added_later_is_picked_up_without_rebinding(self):
        """Копия состава разъехалась бы молча — платили бы за вчерашний
        набор."""
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        s["ad_packs"]["Утренний"].append("104")
        self.assertIn("104", SS.promo_only_ids(s))

    def test_an_item_removed_stops_being_promoted(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        s["ad_packs"]["Утренний"].remove("102")
        self.assertNotIn("102", SS.promo_only_ids(s))

    def test_without_a_pack_the_old_manual_choice_still_works(self):
        s = shop()
        s["auto_bump"]["only_items"] = ["777"]
        self.assertEqual(SS.promo_only_ids(s), ["777"])

    def test_the_pack_wins_over_a_leftover_manual_choice(self):
        s = shop()
        s["auto_bump"] = {"only_pack": "Вечерний", "only_items": ["777"]}
        self.assertEqual(SS.promo_only_ids(s), ["201"])


class AVanishedPackStopsTheRun(unittest.TestCase):
    """Пустой список — это «поднимать все». Удалённый пак не имеет права
    выглядеть как пустой список."""

    def test_a_deleted_pack_is_a_problem_not_an_empty_list(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        del s["ad_packs"]["Утренний"]
        self.assertIn("нет", SS.promo_pack_problem(s))

    def test_and_the_reason_says_what_it_prevented(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        del s["ad_packs"]["Утренний"]
        self.assertIn("весь магазин", SS.promo_pack_problem(s))

    def test_an_emptied_pack_stops_it_too(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Вечерний"
        s["ad_packs"]["Вечерний"] = []
        self.assertIn("пуст", SS.promo_pack_problem(s))

    def test_a_healthy_pack_is_no_problem(self):
        s = shop()
        s["auto_bump"]["only_pack"] = "Утренний"
        self.assertEqual(SS.promo_pack_problem(s), "")

    def test_no_pack_at_all_is_no_problem(self):
        self.assertEqual(SS.promo_pack_problem(shop()), "")

    def test_the_scheduled_run_refuses_instead_of_promoting_everything(self):
        """Проверка стоит в самом прогоне, а не только на экране."""
        import inspect
        from tasks import manager as M
        src = inspect.getsource(M.TaskManager._panel_bump)
        self.assertIn("promo_pack_problem", src)
        self.assertIn("return 0, problem", src)


class Screen:
    def __init__(self, data):
        self.data = data
        self.text = ""
        self.kb = None
        self.message = self
        self.alerts: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text, self.kb = text, reply_markup

    async def answer(self, text="", show_alert=False, **kw):
        if text:
            self.alerts.append(text)

    from_user = type("U", (), {"id": 42})()

    def presses(self):
        return [b.callback_data for row in self.kb.inline_keyboard for b in row]


class BindingAPackToTheSchedule(unittest.TestCase):
    def setUp(self):
        self.store = shop()
        self._get, self._save = PK.get_settings, PK.save_settings
        PK.get_settings = lambda uid: self.store
        PK.save_settings = lambda uid, s: self.store.update(s)

    def tearDown(self):
        PK.get_settings, PK.save_settings = self._get, self._save

    def bind(self, idx):
        cb = Screen(f"pack:sched:{idx}")
        asyncio.run(PK.pack_schedule(cb))
        return cb

    def idx_of(self, name):
        # Порядок паков — вставки, а не алфавита: экран нумерует их так же.
        return list(self.store["ad_packs"]).index(name)

    def test_it_remembers_which_pack(self):
        self.bind(self.idx_of("Утренний"))
        self.assertEqual(self.store["auto_bump"]["only_pack"], "Утренний")

    def test_a_previous_manual_choice_is_dropped(self):
        """Иначе непонятно, что победит — пак или старый список."""
        self.store["auto_bump"]["only_items"] = ["777"]
        self.bind(self.idx_of("Утренний"))
        self.assertNotIn("only_items", self.store["auto_bump"])

    def test_switching_packs_is_said_out_loud(self):
        """Молча переключить расписание с одного пака на другой — значит
        начать платить не за те товары."""
        self.bind(self.idx_of("Утренний"))
        cb = self.bind(self.idx_of("Вечерний"))
        self.assertIn("Утренний", cb.text)
        self.assertIn("теперь поднимается этот", cb.text)

    def test_binding_the_same_pack_twice_does_not_nag(self):
        self.bind(self.idx_of("Утренний"))
        cb = self.bind(self.idx_of("Утренний"))
        self.assertNotIn("Раньше по расписанию", cb.text)

    def test_an_empty_pack_is_refused(self):
        self.store["ad_packs"]["Вечерний"] = []
        cb = self.bind(self.idx_of("Вечерний"))
        self.assertEqual(self.store["auto_bump"].get("only_pack"), None)
        self.assertTrue(any("пуст" in a for a in cb.alerts), cb.alerts)

    def test_it_leads_to_the_screen_that_sets_the_hours(self):
        cb = self.bind(self.idx_of("Утренний"))
        self.assertIn("sched:menu", cb.presses())

    def test_it_says_the_schedule_is_one_for_the_shop(self):
        """Иначе продавец решит, что у каждого пака свои часы."""
        cb = self.bind(self.idx_of("Утренний"))
        self.assertIn("Расписание в боте одно", cb.text)


class ThePackScreenShowsItsOwnState(unittest.TestCase):
    def setUp(self):
        self.store = shop(bump_schedule={"enabled": True, "times": ["10:00"]})
        self.store["auto_bump"]["only_pack"] = "Утренний"
        self._get, self._save = PK.get_settings, PK.save_settings
        PK.get_settings = lambda uid: self.store
        PK.save_settings = lambda uid, s: self.store.update(s)

    def tearDown(self):
        PK.get_settings, PK.save_settings = self._get, self._save

    def view(self, name):
        cb = Screen(f"pack:view:{list(self.store['ad_packs']).index(name)}")
        asyncio.run(PK.pack_view(cb))
        return cb

    def test_the_scheduled_pack_says_when_it_fires(self):
        cb = self.view("Утренний")
        self.assertIn("10:00", cb.text)

    def test_another_pack_says_nothing_about_the_schedule(self):
        cb = self.view("Вечерний")
        self.assertNotIn("10:00", cb.text)

    def test_a_switched_off_schedule_is_not_shown_as_working(self):
        """«Поднимается по расписанию» при выключенном расписании — это
        обещание, которого никто не выполнит."""
        self.store["bump_schedule"]["enabled"] = False
        cb = self.view("Утренний")
        self.assertIn("выключено", cb.text)

    def test_every_pack_offers_the_button(self):
        for name in ("Утренний", "Вечерний"):
            self.assertTrue(
                [p for p in self.view(name).presses()
                 if p.startswith("pack:sched:")], name)


if __name__ == "__main__":
    unittest.main()
