"""Залив: копия по расписанию и уборка вчерашних копий.

Продавец поднимал объявление наверх выдачи руками — заводил такое же
заново, а старое удалял. Залив делает то же самое сам, по часам.

Здесь проверяется то, что стоит товара и денег:

* **удаляются ТОЛЬКО свои копии** — те, чьи номера бот записал за собой
  при создании. Не «такие же по названию»: удаление необратимо, и
  объяснять пропажу заведённого руками товара было бы нечем;
* **шаг соблюдается.** Минутный залив — это больше тысячи объявлений в
  сутки на товар, и проход, не смотрящий на часы, превращает шаг в
  «каждый общий цикл»;
* **каждый отказ записывается.** Залив идёт без человека, и «ничего не
  создалось» без причины — тишина, в которой продавец теряет день.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402
import tasks.manager as M                                  # noqa: E402
from tasks.manager import TaskManager                      # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Bench(unittest.TestCase):
    """Один продавец, своё хранилище и подставные панель с маркетплейсом."""

    UID = 7

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")

        import features
        self.features = features
        self._shown = features.ad_templates_shown
        features.ad_templates_shown = lambda uid: True

        self._token = M.get_token
        M.get_token = lambda uid: "tok"

        # Копия: подставная — настоящая ходит в панель по HTTP, и её путь
        # проверяется отдельно (test_ad_copy_as_panel_answers).
        import handlers.create_ad as C
        self.C = C
        self._pour_once = C.pour_once
        self.made: list = []
        self.answers: list = []

        self.published: list = []

        async def fake_pour(uid, ad_id, cid=None, api=None, publish=True):
            self.made.append(str(ad_id))
            self.published.append(bool(publish))
            return (self.answers.pop(0) if self.answers
                    else {"ok": True, "id": f"new{len(self.made)}", "why": "",
                          "stock_ok": True, "stock": "", "published": True,
                          "publish": ""})

        C.pour_once = fake_pour

        # Удаление: считаем, кого просили убрать.
        self.deleted: list = []
        self.delete_fails: set = set()
        self.mgr = TaskManager(bot=None)

        async def fake_delete(uid, item_id):
            self.deleted.append(str(item_id))
            if str(item_id) in self.delete_fails:
                return False, "HTTP 403"
            return True, "удалён"

        self.mgr._pour_delete = fake_delete

        # Маркетплейс заливу нужен только чтобы отдать клиент: сама копия
        # подставная.
        class Api:
            async def start(self): pass
            async def close(self): pass

        self._api_cls = M.YooMarketAPI
        M.YooMarketAPI = lambda token: Api()

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.features.ad_templates_shown = self._shown
        M.get_token = self._token
        M.YooMarketAPI = self._api_cls
        self.C.pour_once = self._pour_once
        self.tmp.cleanup()

    def pour(self, **conf):
        base = {"enabled": True, "every": 1, "items": ["11"], "made": [],
                "last_run": 0.0, "log": []}
        base.update(conf)
        storage.save_pour(self.UID, base)
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        return storage.get_pour(self.UID)


class TheSelectedItemsArePouredOnTheirOwn(Bench):
    def test_each_selected_item_is_created_again(self):
        conf = self.pour(items=["11", "12"])
        self.assertEqual(self.made, ["11", "12"])
        self.assertEqual([r["id"] for r in conf["made"]], ["new1", "new2"])

    def test_the_new_number_is_written_down_with_its_source(self):
        """Без номера удалить эту копию завтра будет нечем, а без образца —
        непонятно, чья она."""
        conf = self.pour(items=["11"])
        row = conf["made"][0]
        self.assertEqual(row["src"], "11")
        self.assertTrue(row["day"], "день нужен: «вчерашние» считаются по нему")

    def test_it_does_nothing_when_switched_off(self):
        self.pour(enabled=False)
        self.assertEqual(self.made, [])

    def test_and_nothing_when_no_item_is_chosen(self):
        self.pour(items=[])
        self.assertEqual(self.made, [])

    def test_the_screen_is_closed_for_the_seller_and_so_is_the_pour(self):
        """Копия создаёт товар на витрине без единого вопроса, а залив
        делает это ещё и сам. Заслон тот же."""
        self.features.ad_templates_shown = lambda uid: False
        self.pour()
        self.assertEqual(self.made, [])


class TheStepIsRespected(Bench):
    def test_a_second_run_within_the_step_does_nothing(self):
        self.pour(every=10)
        self.made.clear()
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(self.made, [], "шаг десять минут, а залил дважды")

    def test_but_after_the_step_it_runs_again(self):
        conf = self.pour(every=10)
        conf["last_run"] = time.time() - 11 * 60
        storage.save_pour(self.UID, conf)
        self.made.clear()
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(self.made, ["11"])

    def test_a_minute_step_runs_on_the_next_pass(self):
        """Ровно то, о чём просили: шаг в минуту — залив каждую минуту."""
        conf = self.pour(every=1)
        conf["last_run"] = time.time() - 61
        storage.save_pour(self.UID, conf)
        self.made.clear()
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(self.made, ["11"])


class OnePassIsNotTheWholeShift(Bench):
    """Копия товара — это чтение панели, создание, остатки с ожиданием и
    публикация, секунд пятнадцать на товар. Восемнадцать отмеченных заняли
    бы проход на пять минут, а в том же проходе идут заказы и напоминания,
    и на общем замке ждёт опрос чатов: письмо покупателя лежало бы всё это
    время."""

    def many(self, n=18):
        return [str(100 + i) for i in range(n)]

    def test_a_pass_takes_only_a_handful(self):
        self.pour(items=self.many())
        self.assertEqual(len(self.made), self.mgr._POUR_PER_PASS, self.made)

    def test_the_rest_go_on_the_next_pass(self):
        """Очередь круговая: никто не остаётся навсегда последним."""
        conf = self.pour(items=self.many())
        first = list(self.made)
        self.made.clear()
        conf["last_run"] = 0.0
        storage.save_pour(self.UID, conf)
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(self.made, [str(100 + i) for i in range(5, 10)],
                         self.made)
        self.assertNotEqual(self.made, first)

    def test_the_circle_closes(self):
        """Пройдя всех, залив начинает сначала, а не встаёт."""
        items = self.many(7)
        conf = self.pour(items=items)
        for _ in range(3):
            conf["last_run"] = 0.0
            storage.save_pour(self.UID, conf)
            run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
            conf = storage.get_pour(self.UID)
        self.assertEqual(sorted(set(self.made)), sorted(items),
                         "кто-то не получил очереди")

    def test_a_short_list_goes_whole(self):
        self.pour(items=["11", "12"])
        self.assertEqual(self.made, ["11", "12"])

    def test_and_says_that_the_rest_are_waiting(self):
        """Молчание здесь читается как «залив берёт только первые пять»."""
        conf = self.pour(items=self.many())
        self.assertTrue(any("следующим" in r for r in conf["log"]),
                        conf["log"])

    def test_a_full_list_says_nothing_of_the_kind(self):
        conf = self.pour(items=["11", "12"])
        self.assertFalse([r for r in conf["log"] if "следующим" in r])


class OnlyItsOwnCopiesAreDeleted(Bench):
    """Самое дорогое место: удаление необратимо."""

    def yesterday(self, *ids):
        return [{"id": str(i), "src": "11", "day": "2000-01-01", "at": 0.0}
                for i in ids]

    def test_yesterdays_copies_go(self):
        conf = self.pour(made=self.yesterday("900", "901"))
        self.assertEqual(sorted(self.deleted), ["900", "901"])
        self.assertEqual([r["id"] for r in conf["made"]], ["new1"],
                         "остаётся только сегодняшняя")

    def test_todays_copies_stay(self):
        import localtime as _lt
        today = _lt.today_str(storage.get_settings(self.UID))
        conf = self.pour(made=[{"id": "800", "src": "11", "day": today,
                                "at": time.time()}])
        self.assertEqual(self.deleted, [], "сегодняшнюю трогать нельзя")
        self.assertIn("800", [r["id"] for r in conf["made"]])

    def test_nothing_the_bot_did_not_create_is_touched(self):
        """Список `made` — единственный источник для удаления. Товары
        продавца, похожие на копии, здесь не при чём."""
        self.pour(items=["11"], made=[])
        self.assertEqual(self.deleted, [])

    def test_a_copy_that_would_not_delete_stays_in_the_list(self):
        """Забыть номер значит потерять единственный след, по которому
        товар вообще можно убрать."""
        self.delete_fails = {"900"}
        conf = self.pour(made=self.yesterday("900"))
        self.assertEqual(self.deleted, ["900"])
        self.assertIn("900", [r["id"] for r in conf["made"]])

    def test_and_says_why_it_did_not(self):
        self.delete_fails = {"900"}
        conf = self.pour(made=self.yesterday("900"))
        self.assertTrue(any("900" in r and "403" in r for r in conf["log"]),
                        conf["log"])


class EveryRefusalIsWrittenDown(Bench):
    def test_a_skipped_item_says_why(self):
        self.answers = [{"ok": False, "id": "",
                         "why": "нечем заполнить поле «category»"}]
        conf = self.pour(items=["11"])
        self.assertTrue(any("category" in r for r in conf["log"]), conf["log"])
        self.assertEqual(conf["made"], [], "не создалось — и записывать нечего")

    def test_a_success_is_written_down_too(self):
        conf = self.pour(items=["11"])
        self.assertTrue(any("new1" in r for r in conf["log"]), conf["log"])

    def test_the_journal_does_not_grow_forever(self):
        conf = self.pour(items=["11"])
        conf["log"] = [f"строка {i}" for i in range(200)]
        storage.save_pour(self.UID, conf)
        self.assertLessEqual(len(storage.get_pour(self.UID)["log"]), 30)

    def test_neither_does_the_list_of_made_copies(self):
        """Минутный залив за сутки создаёт больше тысячи записей на товар."""
        conf = storage.get_pour(self.UID)
        conf["made"] = [{"id": str(i), "src": "11", "day": "2000-01-01"}
                        for i in range(5000)]
        storage.save_pour(self.UID, conf)
        self.assertLessEqual(len(storage.get_pour(self.UID)["made"]),
                             storage._POUR_MADE_MAX)


class TheWorkingHoursAreTheSellers(Bench):
    """Ночью поднимать некому: покупатели спят, а объявления и лимиты
    тратятся так же."""

    def at(self, hour: int, **conf):
        import localtime as _lt
        was = _lt.hour
        _lt.hour = lambda settings: hour
        M._lt = _lt if hasattr(M, "_lt") else None
        try:
            return self.pour(**conf)
        finally:
            _lt.hour = was

    def test_outside_the_window_it_does_not_pour(self):
        self.at(3, from_hour=9, to_hour=23)
        self.assertEqual(self.made, [])

    def test_inside_the_window_it_does(self):
        self.at(10, from_hour=9, to_hour=23)
        self.assertEqual(self.made, ["11"])

    def test_equal_bounds_mean_around_the_clock(self):
        """0–24 и 9–9 — это «всегда»: пустое окно значило бы «никогда», и
        залив молчал бы, не сказав почему."""
        self.at(3, from_hour=0, to_hour=24)
        self.assertEqual(self.made, ["11"])

    def test_a_window_over_midnight_works_too(self):
        """22–6 — это вечер и ночь, а не пустой промежуток."""
        self.at(23, from_hour=22, to_hour=6)
        self.assertEqual(self.made, ["11"], "23:00 внутри 22–6")
        self.made.clear()
        self.at(12, from_hour=22, to_hour=6)
        self.assertEqual(self.made, [], "полдень вне 22–6")

    def test_the_step_is_not_burned_while_waiting_for_the_window(self):
        """Отметка «залил» вне окна означала бы, что первый залив в девять
        утра случится не сразу, а через шаг."""
        conf = self.at(3, from_hour=9, to_hour=23)
        self.assertEqual(conf["last_run"], 0.0)


class TheDailyCapHolds(Bench):
    def test_it_stops_at_the_cap(self):
        conf = self.pour(cap=2)
        for _ in range(4):
            conf["last_run"] = 0.0
            storage.save_pour(self.UID, conf)
            run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
            conf = storage.get_pour(self.UID)
        self.assertEqual(len(self.made), 2, self.made)

    def test_and_says_so_once(self):
        """Минутный залив написал бы эту строку тысячу раз и вытеснил из
        журнала всё остальное."""
        conf = self.pour(cap=1)
        for _ in range(3):
            conf["last_run"] = 0.0
            storage.save_pour(self.UID, conf)
            run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
            conf = storage.get_pour(self.UID)
        said = [r for r in conf["log"] if "потолок" in r]
        self.assertEqual(len(said), 1, conf["log"])

    def test_a_new_day_starts_the_count_over(self):
        conf = self.pour(cap=1)
        conf["day"] = "2000-01-01"
        conf["last_run"] = 0.0
        storage.save_pour(self.UID, conf)
        self.made.clear()
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(self.made, ["11"], "новый день — новый счёт")

    def test_no_cap_means_no_cap(self):
        conf = self.pour(cap=0)
        conf["last_run"] = 0.0
        storage.save_pour(self.UID, conf)
        run(self.mgr._maybe_pour(self.UID, storage.get_settings(self.UID)))
        self.assertEqual(len(self.made), 2)


class KeepingOnlyTheLastCopies(Bench):
    """«Держать N» — про сегодняшние тоже: за сутки минутного залива их
    накопится больше тысячи, и «убирать вчерашние» тут не спасает."""

    def today_rows(self, n):
        import localtime as _lt
        today = _lt.today_str(storage.get_settings(self.UID))
        return [{"id": f"c{i}", "src": "11", "day": today, "at": float(i)}
                for i in range(n)]

    def test_the_extra_ones_go(self):
        conf = self.pour(keep=2, made=self.today_rows(4))
        # Четыре старых плюс одна новая — держим две последние.
        self.assertEqual(sorted(self.deleted), ["c0", "c1", "c2"])
        self.assertEqual(len(conf["made"]), 2)

    def test_the_newest_stay(self):
        conf = self.pour(keep=2, made=self.today_rows(4))
        kept = [r["id"] for r in conf["made"]]
        self.assertIn("new1", kept, "только что созданная — самая свежая")
        self.assertIn("c3", kept)

    def test_zero_keeps_the_old_behaviour(self):
        self.pour(keep=0, made=self.today_rows(4))
        self.assertEqual(self.deleted, [], "без «держать N» трогаем вчерашние")

    def test_each_item_is_counted_on_its_own(self):
        """Три копии одного товара и три другого — это не шесть подряд."""
        rows = self.today_rows(2)
        rows += [{"id": "d0", "src": "12", "day": rows[0]["day"], "at": 0.0},
                 {"id": "d1", "src": "12", "day": rows[0]["day"], "at": 1.0}]
        conf = self.pour(keep=2, items=["11", "12"], made=rows)
        self.assertEqual(sorted(self.deleted), ["c0", "d0"])


class ThePauseAndThePublishFlag(Bench):
    def test_the_gap_is_waited_between_items(self):
        slept: list = []

        async def fake_sleep(sec):
            slept.append(sec)

        was = M.asyncio.sleep
        M.asyncio.sleep = fake_sleep
        try:
            self.pour(items=["11", "12"], gap=7)
        finally:
            M.asyncio.sleep = was
        self.assertEqual(slept, [7], "пауза одна — между двумя товарами")

    def test_no_gap_no_waiting(self):
        slept: list = []

        async def fake_sleep(sec):
            slept.append(sec)

        was = M.asyncio.sleep
        M.asyncio.sleep = fake_sleep
        try:
            self.pour(items=["11", "12"], gap=0)
        finally:
            M.asyncio.sleep = was
        self.assertEqual(slept, [])

    def test_the_publish_choice_reaches_the_copy(self):
        self.pour(publish=False)
        self.assertEqual(self.published, [False])

    def test_and_by_default_it_publishes(self):
        self.pour()
        self.assertEqual(self.published, [True])


class TheJournalTellsAboutTheStock(Bench):
    """«Иногда остатки не вписываются» — это про молчание: причина была
    написана в отчёте копии и выброшена вместе с ним."""

    def test_a_copy_without_stock_is_marked(self):
        self.answers = [{"ok": True, "id": "77", "why": "", "stock_ok": False,
                         "stock": "Остаток — это сам товар", "published": False,
                         "publish": "empty_stock"}]
        conf = self.pour()
        line = "\n".join(conf["log"])
        self.assertIn("без остатка", line, conf["log"])
        self.assertIn("сам товар", line, "причина — словами маркетплейса")

    def test_and_a_failed_publication_too(self):
        self.answers = [{"ok": True, "id": "77", "why": "", "stock_ok": False,
                         "stock": "", "published": False,
                         "publish": "empty_stock"}]
        conf = self.pour()
        self.assertIn("empty_stock", "\n".join(conf["log"]))

    def test_a_good_copy_is_marked_good(self):
        conf = self.pour()
        self.assertIn("✅", "\n".join(conf["log"]))


class TheStockIsPerItemFirst(unittest.TestCase):
    """Одна заготовка на все товары кладёт покупателю ключ от чужой игры."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.tmp.cleanup()

    def ready(self, src=""):
        import handlers.create_ad as C
        return run(C._default_stock(7, src))

    def test_its_own_list_wins(self):
        storage.set_copy_stock(7, ["ОБЩИЙ"])
        storage.set_pour_stock(7, "11", ["СВОЙ-1", "СВОЙ-2"])
        self.assertEqual(self.ready("11"), ["СВОЙ-1", "СВОЙ-2"])

    def test_the_common_one_fills_the_gap(self):
        storage.set_copy_stock(7, ["ОБЩИЙ"])
        self.assertEqual(self.ready("12"), ["ОБЩИЙ"])

    def test_and_nothing_is_still_nothing(self):
        """Подставить за продавца нечего — и выдумывать нельзя: эти строки
        уходят живому покупателю."""
        self.assertEqual(self.ready("12"), [])

    def test_an_emptied_list_falls_back_to_the_common_one(self):
        storage.set_copy_stock(7, ["ОБЩИЙ"])
        storage.set_pour_stock(7, "11", ["СВОЙ"])
        storage.set_pour_stock(7, "11", [])
        self.assertEqual(self.ready("11"), ["ОБЩИЙ"])


class TheScreenActuallyWiresItUp(Bench):
    """Настройку можно написать правильно и забыть позвать из экрана —
    снаружи это «нажал, а ничего не изменилось»."""

    class CB:
        def __init__(s, data, uid=7):
            s.data = data
            s.from_user = type("U", (), {"id": uid})()
            s.said: list = []
            s.alerts: list = []
            outer = s

            class Msg:
                async def edit_text(m, text, reply_markup=None, **kw):
                    outer.said.append(str(text))
                    outer.kb = reply_markup
                    return m

                async def answer(m, text, reply_markup=None, **kw):
                    outer.said.append(str(text))
                    return m

            s.message = Msg()
            s.kb = None

        async def answer(s, text="", show_alert=False):
            s.alerts.append(str(text))

    class Api:
        rows = [{"id": 11, "title": "Аккаунт Black Russia"},
                {"id": 12, "title": "Вирты 3кк"}]

        async def get_all_ads(self, max_pages=25):
            return list(TheScreenActuallyWiresItUp.Api.rows)

    class FSM:
        def __init__(s):
            s.data: dict = {}

        async def get_data(s):
            return dict(s.data)

        async def update_data(s, **kw):
            s.data.update(kw)
            return dict(s.data)

        async def set_state(s, st=None):
            s.state = st

        async def clear(s):
            s.data = {}

    def buttons(self, cb) -> list:
        kb = cb.kb
        return [b.callback_data
                for row in (kb.inline_keyboard if kb else []) for b in row]

    def test_the_switch_turns_it_on(self):
        cb = self.CB("pour:toggle")
        run(self.C.pour_toggle(cb, self.FSM(), self.Api()))
        self.assertTrue(storage.get_pour(self.UID)["enabled"])

    def test_turning_it_on_without_items_leads_where_they_are_chosen(self):
        """Включить и промолчать — худший вариант: залив ничего не сделает,
        а продавец будет ждать."""
        cb = self.CB("pour:toggle")
        run(self.C.pour_toggle(cb, self.FSM(), self.Api()))
        self.assertTrue(any("не выбраны" in a for a in cb.alerts), cb.alerts)
        self.assertTrue(any("Какие товары" in t for t in cb.said), cb.said)

    def test_a_tap_on_an_item_saves_it(self):
        fsm = self.FSM()
        run(self.C.pour_pick(self.CB("pour:pick"), fsm, self.Api()))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        self.assertEqual(storage.get_pour(self.UID)["items"], ["11"])

    def test_and_a_second_tap_takes_it_back(self):
        fsm = self.FSM()
        run(self.C.pour_pick(self.CB("pour:pick"), fsm, self.Api()))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        self.assertEqual(storage.get_pour(self.UID)["items"], [])

    def test_a_stale_list_does_not_pick_a_stranger(self):
        """Кнопка осталась в старом сообщении, а список с тех пор другой."""
        cb = self.CB("pour:tog:9")
        run(self.C.pour_toggle_item(cb, self.FSM(), self.Api()))
        self.assertEqual(storage.get_pour(self.UID)["items"], [])
        self.assertTrue(any("устарел" in a for a in cb.alerts), cb.alerts)

    def test_the_step_is_saved(self):
        msg = self.Msg("15")
        run(self.C.pour_every_save(msg, self.FSM()))
        self.assertEqual(storage.get_pour(self.UID)["every"], 15)

    def test_but_less_than_a_minute_is_refused_out_loud(self):
        msg = self.Msg("0")
        run(self.C.pour_every_save(msg, self.FSM()))
        self.assertTrue(any("минут" in t for t in msg.said), msg.said)
        self.assertEqual(storage.get_pour(self.UID)["every"], 1)

    def test_and_so_is_a_word_instead_of_a_number(self):
        msg = self.Msg("часто")
        run(self.C.pour_every_save(msg, self.FSM()))
        self.assertTrue(any("число" in t for t in msg.said), msg.said)

    def test_the_screen_says_what_the_minute_step_costs(self):
        """Минутный шаг — больше тысячи объявлений в сутки на товар.
        Молчать об этом значит дать продавцу узнать это от маркетплейса."""
        cb = self.CB("pour:menu")
        run(self.C.pour_menu(cb, self.FSM(), self.Api()))
        self.assertTrue(any("1440" in t or "тысяч" in t for t in cb.said),
                        cb.said)

    def test_the_screen_says_how_many_fit_in_one_pass(self):
        """Молчание читается как «залив берёт только первые пять и всё»."""
        conf = storage.get_pour(self.UID)
        conf["items"] = [str(i) for i in range(18)]
        storage.save_pour(self.UID, conf)
        cb = self.CB("pour:menu")
        run(self.C.pour_menu(cb, self.FSM(), self.Api()))
        self.assertIn("по кругу", cb.said[-1])

    def test_but_a_short_list_is_not_lectured(self):
        conf = storage.get_pour(self.UID)
        conf["items"] = ["11"]
        storage.save_pour(self.UID, conf)
        cb = self.CB("pour:menu")
        run(self.C.pour_menu(cb, self.FSM(), self.Api()))
        self.assertNotIn("по кругу", cb.said[-1])

    def test_and_that_it_deletes_only_its_own(self):
        cb = self.CB("pour:menu")
        run(self.C.pour_menu(cb, self.FSM(), self.Api()))
        self.assertTrue(any("созданные ботом" in t for t in cb.said), cb.said)

    def test_the_journal_shows_the_reasons(self):
        conf = storage.get_pour(self.UID)
        conf["log"] = ["11: нечем заполнить поле «category»"]
        storage.save_pour(self.UID, conf)
        cb = self.CB("pour:log")
        run(self.C.pour_log(cb))
        self.assertTrue(any("category" in t for t in cb.said), cb.said)

    def test_nothing_of_this_opens_for_the_seller(self):
        """Экран закрыт тем же заслоном, что копия: она заводит товар на
        витрине без единого вопроса, а залив делает это ещё и сам."""
        self.features.ad_templates_shown = lambda uid: False
        for call, args in (
            (self.C.pour_menu, (self.FSM(), self.Api())),
            (self.C.pour_toggle, (self.FSM(), self.Api())),
            (self.C.pour_pick, (self.FSM(), self.Api())),
            (self.C.pour_every_ask, (self.FSM(),)),
            (self.C.pour_log, ()),
        ):
            with self.subTest(call=call.__name__):
                cb = self.CB("pour:menu")
                run(call(cb, *args))
                self.assertEqual(cb.said, [], call.__name__)
                self.assertTrue(cb.alerts, call.__name__)

    def test_the_cap_is_saved(self):
        cb = self.CB("pour:cap")
        run(self.C.pour_cap(cb, self.FSM()))
        fsm = self.FSM()
        run(fsm.update_data(pour_key="cap"))
        msg = self.Msg("50")
        run(self.C.pour_number_save(msg, fsm))
        self.assertEqual(storage.get_pour(self.UID)["cap"], 50)

    def test_and_so_are_keep_and_gap(self):
        for key, value in (("keep", 3), ("gap", 10)):
            with self.subTest(key=key):
                fsm = self.FSM()
                run(fsm.update_data(pour_key=key))
                run(self.C.pour_number_save(self.Msg(str(value)), fsm))
                self.assertEqual(storage.get_pour(self.UID)[key], value)

    def test_a_negative_number_is_refused(self):
        fsm = self.FSM()
        run(fsm.update_data(pour_key="cap"))
        msg = self.Msg("-5")
        run(self.C.pour_number_save(msg, fsm))
        self.assertTrue(any("нельзя" in t for t in msg.said), msg.said)
        self.assertEqual(storage.get_pour(self.UID)["cap"], 0)

    def test_the_hours_preset_is_saved(self):
        cb = self.CB("pour:hours:9-23")
        run(self.C.pour_hours_preset(cb, self.FSM(), self.Api()))
        conf = storage.get_pour(self.UID)
        self.assertEqual((conf["from_hour"], conf["to_hour"]), (9, 23))

    def test_the_hours_can_be_typed(self):
        run(self.C.pour_hours_save(self.Msg("8-22"), self.FSM()))
        conf = storage.get_pour(self.UID)
        self.assertEqual((conf["from_hour"], conf["to_hour"]), (8, 22))

    def test_a_dash_of_any_kind_works(self):
        """Телефон подставляет длинное тире сам, и продавец об этом не
        знает — отказ выглядел бы как «не понимает цифры»."""
        run(self.C.pour_hours_save(self.Msg("9–21"), self.FSM()))
        self.assertEqual(storage.get_pour(self.UID)["to_hour"], 21)

    def test_nonsense_hours_are_refused(self):
        msg = self.Msg("с утра до вечера")
        run(self.C.pour_hours_save(msg, self.FSM()))
        self.assertTrue(any("дефис" in t for t in msg.said), msg.said)

    def test_hours_out_of_range_are_refused(self):
        msg = self.Msg("9-30")
        run(self.C.pour_hours_save(msg, self.FSM()))
        self.assertTrue(any("от 0 до 24" in t for t in msg.said), msg.said)

    def test_the_publish_switch_flips(self):
        cb = self.CB("pour:pub")
        run(self.C.pour_publish_toggle(cb, self.FSM(), self.Api()))
        self.assertFalse(storage.get_pour(self.UID)["publish"])
        run(self.C.pour_publish_toggle(cb, self.FSM(), self.Api()))
        self.assertTrue(storage.get_pour(self.UID)["publish"])

    def test_pour_now_creates_and_remembers(self):
        """Ручной прогон записывает созданное за ботом так же: иначе
        завтра эти копии не удалятся — бот не будет знать, что они его."""
        conf = storage.get_pour(self.UID)
        conf["items"] = ["11"]
        storage.save_pour(self.UID, conf)
        cb = self.CB("pour:now")
        run(self.C.pour_now(cb, self.FSM(), self.Api()))
        self.assertEqual(self.made, ["11"])
        self.assertEqual([r["id"] for r in storage.get_pour(self.UID)["made"]],
                         ["new1"])

    def test_pour_now_without_items_leads_to_choosing_them(self):
        cb = self.CB("pour:now")
        run(self.C.pour_now(cb, self.FSM(), self.Api()))
        self.assertEqual(self.made, [])
        self.assertTrue(any("Какие товары" in t for t in cb.said), cb.said)

    def test_the_items_own_stock_is_saved(self):
        fsm = self.FSM()
        run(self.C.pour_pick(self.CB("pour:pick"), fsm, self.Api()))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        run(self.C.pour_stock_ask(self.CB("pour:st:0"), fsm))
        run(self.C.pour_stock_save(self.Msg("KEY-A\nKEY-B"), fsm))
        self.assertEqual(storage.get_pour_stock(self.UID, "11"),
                         ["KEY-A", "KEY-B"])

    def test_and_says_the_buyer_gets_exactly_those(self):
        fsm = self.FSM()
        run(self.C.pour_pick(self.CB("pour:pick"), fsm, self.Api()))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        run(self.C.pour_stock_ask(self.CB("pour:st:0"), fsm))
        msg = self.Msg("KEY-A")
        run(self.C.pour_stock_save(msg, fsm))
        self.assertTrue(any("получит именно эти" in t for t in msg.said),
                        msg.said)

    def test_the_stock_button_shows_only_on_chosen_items(self):
        """У неотмеченного товара задавать остатки незачем: заливать его
        никто не собирался."""
        fsm = self.FSM()
        cb = self.CB("pour:pick")
        run(self.C.pour_pick(cb, fsm, self.Api()))
        self.assertNotIn("pour:st:0", self.buttons(cb))
        run(self.C.pour_toggle_item(self.CB("pour:tog:0"), fsm, self.Api()))
        cb2 = self.CB("pour:pick")
        run(self.C.pour_pick(cb2, fsm, self.Api()))
        self.assertIn("pour:st:0", self.buttons(cb2))

    class Msg:
        def __init__(s, text, uid=7):
            s.text = text
            s.from_user = type("U", (), {"id": uid})()
            s.said: list = []

        async def answer(s, text, reply_markup=None, **kw):
            s.said.append(str(text))
            return s


class TheSameItemsAreFoldedIntoOne(Bench):
    """Залив заводит копии с ТЕМ ЖЕ названием — иначе это был бы другой
    товар. После суток минутного шага список объявлений это одно название
    тысячу раз, и остальных товаров в нём не найти."""

    def fold(self, rows, made=()):
        return self.C._group_same(rows, made)

    def test_the_same_title_becomes_one_row(self):
        got = self.fold([{"id": "1", "title": "Аккаунт"},
                         {"id": "2", "title": "Аккаунт"},
                         {"id": "3", "title": "Вирты"}])
        self.assertEqual(len(got), 2, got)

    def test_and_carries_how_many_there_are(self):
        got = self.fold([{"id": "1", "title": "Аккаунт"},
                         {"id": "2", "title": "Аккаунт"},
                         {"id": "3", "title": "Аккаунт"}])
        self.assertEqual(got[0]["count"], 3)

    def test_case_and_spaces_do_not_split_a_pair(self):
        """«Аккаунт  BR» и «аккаунт br» — один товар: копия заводится тем
        же названием, но пробелы по дороге схлопываются."""
        got = self.fold([{"id": "1", "title": "Аккаунт  BR"},
                         {"id": "2", "title": "аккаунт br"}])
        self.assertEqual(len(got), 1, got)

    def test_the_sample_is_never_one_of_the_bots_copies(self):
        """Свои копии бот завтра удалит, и залив, привязанный к удалённому
        номеру, назавтра встанет с «панель не нашла этот товар»."""
        got = self.fold([{"id": "900", "title": "Аккаунт"},
                         {"id": "12", "title": "Аккаунт"}],
                        made=["900"])
        self.assertEqual(got[0]["id"], "12")

    def test_and_among_its_own_it_takes_the_oldest(self):
        """Заведённый руками — самый старый номер."""
        got = self.fold([{"id": "900", "title": "Аккаунт"},
                         {"id": "800", "title": "Аккаунт"}],
                        made=["900", "800"])
        self.assertEqual(got[0]["id"], "800")

    def test_nothing_is_lost_in_the_fold(self):
        rows = [{"id": str(i), "title": f"Товар {i % 3}"} for i in range(9)]
        got = self.fold(rows)
        self.assertEqual(sum(g["count"] for g in got), 9)


class TheChoiceScreenShowsEveryItem(Bench):
    """«В добавление товаров в залив я вижу не все товары» — список резался
    на сороковом молча, то есть остальные для залива не существовали."""

    class CB:
        def __init__(s, data="pour:pick", uid=7):
            s.data = data
            s.from_user = type("U", (), {"id": uid})()
            s.said: list = []
            s.kb = None
            outer = s

            class Msg:
                async def edit_text(m, text, reply_markup=None, **kw):
                    outer.said.append(str(text))
                    outer.kb = reply_markup
                    return m

                async def answer(m, text, reply_markup=None, **kw):
                    outer.said.append(str(text))
                    return m

            s.message = Msg()

        async def answer(s, text="", show_alert=False):
            pass

    class FSM:
        def __init__(s):
            s.data: dict = {}

        async def get_data(s):
            return dict(s.data)

        async def update_data(s, **kw):
            s.data.update(kw)
            return dict(s.data)

        async def set_state(s, st=None):
            s.state = st

        async def clear(s):
            s.data = {}

    def api(self, n=50, title=None):
        rows = [{"id": 100 + i, "title": title or f"Товар {i}"}
                for i in range(n)]

        class Api:
            async def get_all_ads(self, max_pages=25):
                return list(rows)

        return Api()

    def screen(self, api, page=0, fsm=None):
        cb = self.CB()
        run(self.C.pour_pick(cb, fsm or self.FSM(), api, page=page))
        return cb

    def buttons(self, cb):
        return [b.callback_data
                for row in (cb.kb.inline_keyboard if cb.kb else []) for b in row]

    def test_the_list_is_paged_not_cut(self):
        cb = self.screen(self.api(50))
        self.assertIn("pour:pick:1", self.buttons(cb), "листалки нет")
        self.assertIn("Страница 1 из", cb.said[-1])

    def test_every_page_is_reachable(self):
        """Пятьдесят товаров по двенадцати — пять страниц, и на последней
        лежат те, которых раньше не было видно вовсе."""
        seen: set = set()
        fsm = self.FSM()
        for page in range(5):
            cb = self.screen(self.api(50), page=page, fsm=fsm)
            rows = list((run(fsm.get_data())).get("pour_ads") or [])
            for data in self.buttons(cb):
                if data.startswith("pour:tog:"):
                    seen.add(rows[int(data.split(":")[2])]["id"])
        self.assertEqual(len(seen), 50, len(seen))

    def test_a_page_past_the_end_shows_the_last_one(self):
        """Кнопка из старого сообщения указывает за конец укоротившегося
        списка — это не повод показать пустой экран."""
        cb = self.screen(self.api(50), page=99)
        self.assertTrue([d for d in self.buttons(cb)
                         if d.startswith("pour:tog:")])

    def test_the_same_items_are_one_row_with_the_count(self):
        cb = self.screen(self.api(20, title="Аккаунт Black Russia"))
        rows = [b.text for row in (cb.kb.inline_keyboard if cb.kb else [])
                for b in row if b.text.startswith(("▫️", "☑️"))]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("(20)", rows[0])

    def test_and_says_what_the_number_means(self):
        cb = self.screen(self.api(20, title="Аккаунт"))
        self.assertIn("сколько копий", cb.said[-1])

    def test_a_single_item_gets_no_brackets(self):
        """«(1)» рядом с каждым товаром — шум, а не сведения."""
        cb = self.screen(self.api(3))
        rows = [b.text for row in (cb.kb.inline_keyboard if cb.kb else [])
                for b in row if b.text.startswith(("▫️", "☑️"))]
        self.assertTrue(rows and not any("(" in t for t in rows), rows)

    def test_a_tick_keeps_you_on_the_same_page(self):
        """Отметив товар на третьей странице, продавец оказывался в начале
        списка и искал место заново — а отмечают обычно несколько подряд."""
        api, fsm = self.api(50), self.FSM()
        self.screen(api, page=2, fsm=fsm)          # третья страница
        rows = list((run(fsm.get_data())).get("pour_ads") or [])
        idx = 26                                    # товар с этой страницы
        cb = self.CB(f"pour:tog:{idx}")
        run(self.C.pour_toggle_item(cb, fsm, api))
        self.assertIn("Страница 3 из", cb.said[-1], cb.said[-1])
        self.assertIn(rows[idx]["id"], storage.get_pour(self.UID)["items"])

    def test_the_totals_are_named_honestly(self):
        """Свёрнутый список показывает два числа: товаров и объявлений.
        Одно из них без другого читается как «половина пропала»."""
        cb = self.screen(self.api(20, title="Аккаунт"))
        said = cb.said[-1]
        self.assertIn("товаров: 1", said)
        self.assertIn("всего объявлений: 20", said)


class ThePourHasItsOwnButtonInTheMenu(unittest.TestCase):
    """Залив живёт под копией, а нужен он каждый день. Кнопка на первом
    экране — то, ради чего просили; но только тем, кому раздел открыт:
    кнопка, отвечающая «этого раздела сейчас нет», — дохлая кнопка."""

    def kb(self, **kw):
        from keyboards.main import main_menu_keyboard
        return [b.callback_data
                for row in main_menu_keyboard(**kw).inline_keyboard
                for b in row]

    def test_it_is_there_for_whoever_the_section_is_open_for(self):
        self.assertIn("pour:menu", self.kb(pour_shown=True))

    def test_and_absent_for_everyone_else(self):
        self.assertNotIn("pour:menu", self.kb(pour_shown=False))

    def test_the_rest_of_the_menu_is_unchanged(self):
        base = self.kb(pour_shown=False)
        self.assertEqual([x for x in self.kb(pour_shown=True)
                          if x != "pour:menu"], base)


class TheSettingsHaveHonestDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.tmp.cleanup()

    def test_off_and_empty_until_asked(self):
        conf = storage.get_pour(1)
        self.assertFalse(conf["enabled"])
        self.assertEqual(conf["items"], [])

    def test_a_list_written_before_the_cap_existed_is_cut_on_reading(self):
        """Потолок стоит и на чтении не для симметрии: запись, сделанная
        прежней версией (или руками в базе), иначе приезжала бы целиком —
        а это те самые мегабайты в настройках продавца."""
        settings = storage.get_settings(1)
        settings["pour"] = {"made": [{"id": str(i)} for i in range(9000)],
                            "log": [f"строка {i}" for i in range(400)]}
        storage.save_settings(1, settings)
        conf = storage.get_pour(1)
        self.assertLessEqual(len(conf["made"]), storage._POUR_MADE_MAX)
        self.assertLessEqual(len(conf["log"]), 30)

    def test_the_step_is_never_below_a_minute(self):
        """Общий проход и так раз в минуту: обещать чаще — обещать
        несуществующее."""
        storage.save_pour(1, {"every": 0})
        self.assertEqual(storage.get_pour(1)["every"], 1)
        storage.save_pour(1, {"every": -5})
        self.assertEqual(storage.get_pour(1)["every"], 1)


if __name__ == "__main__":
    unittest.main()
