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

        async def fake_pour(uid, ad_id, cid=None, api=None):
            self.made.append(str(ad_id))
            return (self.answers.pop(0) if self.answers
                    else {"ok": True, "id": f"new{len(self.made)}", "why": ""})

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

    class Msg:
        def __init__(s, text, uid=7):
            s.text = text
            s.from_user = type("U", (), {"id": uid})()
            s.said: list = []

        async def answer(s, text, reply_markup=None, **kw):
            s.said.append(str(text))
            return s


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
