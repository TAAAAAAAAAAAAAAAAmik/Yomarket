"""Как быстро письмо покупателя доходит до продавца.

Опрос чатов стоял в самом конце общего прохода: сначала список заказов,
потом дочитывание карточек, потом выдача подарочных карт — и только затем
письма. Общий проход шёл раз в минуту, а выдача, ждущая кода у поставщика,
занимала поток до десяти минут. Всё это время чаты не читались вообще, и
снаружи это выглядело как «бот проснулся через полчаса».

Плюс сами чаты читались подряд: двадцать пять чатов — двадцать пять
ожиданий сети друг за другом.

Здесь проверяется, что у чатов свой цикл, что читаются они пачкой и что
темп подбирается по ответу маркетплейса, а не по угаданному числу.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import tasks.manager as M                                      # noqa: E402
from tasks.manager import TaskManager, _fetch_chats            # noqa: E402


def manager() -> TaskManager:
    return TaskManager(bot=None)


class SlowAPI:
    """Каждый чат отвечает не мгновенно — иначе параллельность не измерить."""

    def __init__(self, delay: float = 0.05, fail: set[str] | None = None):
        self.delay, self.fail = delay, fail or set()
        self.busy = 0
        self.peak = 0
        self.asked: list[str] = []

    async def get_messages(self, chat_id, cursor=None):
        self.busy += 1
        self.peak = max(self.peak, self.busy)
        self.asked.append(str(chat_id))
        try:
            await asyncio.sleep(self.delay)
            if str(chat_id) in self.fail:
                raise RuntimeError("resource_not_found")
            return {"data": [{"id": f"m{chat_id}", "text": "привет"}]}
        finally:
            self.busy -= 1


class ChatsAreReadInOneBatch(unittest.TestCase):
    """Двадцать пять запросов подряд занимали проход целиком."""

    def test_every_chat_is_asked(self):
        api = SlowAPI()
        got = asyncio.run(_fetch_chats(api, ["1", "2", "3"]))
        self.assertEqual(sorted(got), ["1", "2", "3"])
        self.assertEqual(sorted(api.asked), ["1", "2", "3"])

    def test_they_are_asked_at_the_same_time_and_not_one_by_one(self):
        api = SlowAPI()
        asyncio.run(_fetch_chats(api, [str(i) for i in range(10)], parallel=5))
        self.assertGreater(api.peak, 1, "чаты по-прежнему читаются подряд")

    def test_the_batch_size_is_respected(self):
        # Без потолка двадцать пять запросов уходят разом — это и есть тот
        # всплеск, за который маркетплейс отвечает «сбавьте темп».
        api = SlowAPI()
        asyncio.run(_fetch_chats(api, [str(i) for i in range(20)], parallel=4))
        self.assertLessEqual(api.peak, 4)

    def test_a_batch_is_faster_than_one_by_one(self):
        api = SlowAPI(delay=0.05)
        start = time.monotonic()
        asyncio.run(_fetch_chats(api, [str(i) for i in range(12)], parallel=6))
        spent = time.monotonic() - start
        self.assertLess(spent, 12 * 0.05 * 0.6, f"заняло {spent:.2f} с")

    def test_one_broken_chat_does_not_take_the_others_with_it(self):
        api = SlowAPI(fail={"2"})
        got = asyncio.run(_fetch_chats(api, ["1", "2", "3"]))
        self.assertIsInstance(got["2"], Exception)
        self.assertNotIsInstance(got["1"], Exception)
        self.assertNotIsInstance(got["3"], Exception)

    def test_the_failure_is_handed_back_and_not_swallowed(self):
        # Проглоченная ошибка — это чат, который «прочитался пустым»: бот
        # молчит, и причины не видно нигде.
        api = SlowAPI(fail={"1"})
        got = asyncio.run(_fetch_chats(api, ["1"]))
        self.assertIn("resource_not_found", str(got["1"]))

    def test_no_chats_at_all_is_not_an_error(self):
        self.assertEqual(asyncio.run(_fetch_chats(SlowAPI(), [])), {})


class ThePaceIsLearnedFromTheMarketplace(unittest.TestCase):
    """Сколько запросов в минуту разрешает Юмаркет, нигде не написано.

    Подставленное сюда угаданное число означало бы либо недобор скорости,
    либо бан. Темп нащупывается: идём с пола, отступаем на просьбу сбавить.
    """

    def pace(self, tm, error: str) -> float:
        s: dict = {"_chat_poll": {"error": error}}
        tm._pace_chats(1, s)
        return s["_chat_poll"]["every"]

    def test_a_clean_pass_polls_at_the_floor(self):
        self.assertEqual(self.pace(manager(), ""), M._CHAT_FAST)

    def test_being_asked_to_slow_down_actually_slows_it_down(self):
        tm = manager()
        self.assertGreater(self.pace(tm, "HTTP 429: too_many_requests"),
                           M._CHAT_FAST)

    def test_repeated_refusals_back_off_further_but_not_past_the_ceiling(self):
        tm = manager()
        for _ in range(20):
            every = self.pace(tm, "too_many_requests")
        self.assertEqual(every, M._CHAT_SLOW)

    def test_the_pace_comes_back_after_the_marketplace_calms_down(self):
        tm = manager()
        slowed = self.pace(tm, "too_many_requests")
        recovered = self.pace(tm, "")
        self.assertLess(recovered, slowed)

    def test_it_never_speeds_past_the_floor(self):
        tm = manager()
        for _ in range(50):
            every = self.pace(tm, "")
        self.assertEqual(every, M._CHAT_FAST)

    def test_an_ordinary_failure_is_not_mistaken_for_a_rate_limit(self):
        # «Чата нет» — это про один заказ, а не просьба сбавить темп.
        tm = manager()
        self.assertEqual(self.pace(tm, "#77: чата нет — больше не опрашиваю"),
                         M._CHAT_FAST)

    def test_slowing_down_is_said_out_loud_and_not_done_silently(self):
        # Молча замедлившийся опрос неотличим от сломанного.
        tm = manager()
        s: dict = {"_chat_poll": {"error": "HTTP 429"}}
        tm._pace_chats(1, s)
        self.assertIn("сбавить темп", s["_chat_poll"]["paced"])

    def test_each_seller_keeps_his_own_pace(self):
        # Лимит считается на токен: один магазин, упёршийся в него, не
        # должен замедлять опрос у соседа.
        tm = manager()
        for _ in range(5):
            tm._pace_chats(1, {"_chat_poll": {"error": "too_many_requests"}})
        s2: dict = {"_chat_poll": {"error": ""}}
        tm._pace_chats(2, s2)
        self.assertEqual(s2["_chat_poll"]["every"], M._CHAT_FAST)
        self.assertGreater(tm._chat_pace[1], tm._chat_pace[2])


class TheFastPassReadsOnlyWhatCanRingNow(unittest.TestCase):
    """Двадцать пять чатов раз в десять секунд — полтораста запросов в
    минуту на продавца. Маркетплейс ответил бы «сбавьте темп», опрос осел бы
    на потолке, и вышло бы МЕДЛЕННЕЕ, чем было раньше. Поэтому быстрый
    проход берёт только горячие чаты, а полный идёт в прежнем темпе.
    """

    def setUp(self):
        now = time.time()
        self.orders = {"1": "new", "2": "new", "3": "new", "4": "new"}
        self.details = {
            # покупатель написал и ждёт ответа — самый горячий случай
            "1": {"seen_at": now - 10 * 86400, "waiting": now - 30},
            # заказ свежий: вопросы по нему ещё будут
            "2": {"seen_at": now - 600},
            # старый заказ, никто не пишет
            "3": {"seen_at": now - 10 * 86400},
            "4": {"seen_at": now - 30 * 86400},
        }

    def test_a_buyer_waiting_for_an_answer_is_hot(self):
        self.assertIn("1", M._hot_chats(self.orders, self.details))

    def test_a_fresh_order_is_hot(self):
        self.assertIn("2", M._hot_chats(self.orders, self.details))

    def test_an_old_quiet_order_is_not_hot(self):
        hot = M._hot_chats(self.orders, self.details)
        self.assertNotIn("3", hot)
        self.assertNotIn("4", hot)

    def test_the_fast_pass_is_far_cheaper_than_the_full_one(self):
        hot = M._hot_chats(self.orders, self.details)
        allc = M._chats_to_poll(self.orders, self.details)
        self.assertLess(len(hot), len(allc))

    def test_no_chat_falls_out_of_watch_it_only_changes_how_often(self):
        # Горячий отбор — это частота, а не фильтр наблюдения. Полный обход
        # по-прежнему видит всех.
        self.assertEqual(sorted(M._chats_to_poll(self.orders, self.details)),
                         ["1", "2", "3", "4"])

    def test_a_chat_the_marketplace_lost_stays_excluded_even_when_fresh(self):
        det = dict(self.details)
        det["2"] = {**det["2"], "chat_gone": time.time()}
        self.assertNotIn("2", M._hot_chats(self.orders, det))

    def test_a_broken_timestamp_does_not_crash_the_selection(self):
        # Одно кривое значение в настройках роняло опрос НЕ одного заказа, а
        # всех чатов продавца разом: уведомления замолкали целиком, и
        # снаружи это выглядело как сломавшийся бот. Настройки приезжают из
        # базы и переживают все прежние версии формата.
        det = dict(self.details)
        det["3"] = {"seen_at": "позавчера"}
        self.assertIn("1", M._hot_chats(self.orders, det))
        self.assertIn("3", M._chats_to_poll(self.orders, det))

    def test_the_full_sweep_still_runs_at_the_old_cadence(self):
        self.assertLessEqual(M._CHAT_FULL_EVERY, 60)


class ChatsDoNotWaitBehindEverythingElse(unittest.TestCase):
    """Опрос чатов стоял в конце общего прохода — за заказами и за выдачей."""

    def test_the_order_pass_no_longer_reads_chats(self):
        import ast
        import inspect
        src = inspect.getsource(TaskManager._process_orders)
        called = {n.func.attr for n in ast.walk(ast.parse(src.lstrip()))
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("_check_messages", called)
        self.assertNotIn("_check_watched_chats", called)

    def test_the_chat_pass_does_read_them(self):
        import ast
        import inspect
        src = inspect.getsource(TaskManager._chat_tick)
        called = {n.func.attr for n in ast.walk(ast.parse(src.lstrip()))
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        self.assertIn("_check_messages", called)
        self.assertIn("_check_watched_chats", called)

    def test_the_chat_pass_is_much_more_frequent_than_the_order_pass(self):
        self.assertLess(M._CHAT_FAST, 60)

    def test_the_fast_pass_does_not_postpone_the_full_one(self):
        # Если быстрый проход обновляет отметку полного обхода, полный не
        # наступит никогда, и старые чаты не прочитаются вовсе.
        import ast
        import inspect
        src = inspect.getsource(TaskManager._check_messages)
        tree = ast.parse(src.lstrip())
        guarded = [n for n in ast.walk(tree)
                   if isinstance(n, ast.If)
                   and "full_ts" in ast.dump(n)]
        self.assertTrue(guarded, "отметка полного обхода ставится безусловно")

    def test_waiting_for_a_supplier_code_cannot_hold_the_seller_for_minutes(self):
        # Ожидание кода занимает поток, а на нём общий замок — тот же, что
        # у опроса чатов. Потолок в десять минут глушил уведомления.
        self.assertLessEqual(TaskManager._GIFT_POLL_CEILING, 180)
        self.assertLessEqual(TaskManager._ROBUX_POLL_CEILING, 180)


class TheChatLoopIsStartedAndStopped(unittest.TestCase):
    """Цикл, который никто не запускает, — это тишина без единой ошибки."""

    def test_starting_a_seller_starts_his_chat_loop(self):
        async def go():
            tm = manager()
            tm.start_for_user(1)
            try:
                self.assertIn(1, tm._chat_tasks)
                self.assertFalse(tm._chat_tasks[1].done())
            finally:
                tm.stop_for_user(1)
        asyncio.run(go())

    def test_stopping_him_stops_it(self):
        async def go():
            tm = manager()
            tm.start_for_user(1)
            task = tm._chat_tasks[1]
            tm.stop_for_user(1)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled() or task.done())
            self.assertNotIn(1, tm._chat_tasks)
        asyncio.run(go())

    def test_stopping_him_forgets_his_pace_too(self):
        async def go():
            tm = manager()
            tm.start_for_user(1)
            tm._chat_pace[1] = 90.0
            tm.stop_for_user(1)
            self.assertNotIn(1, tm._chat_pace)
        asyncio.run(go())

    def test_starting_twice_does_not_leave_two_loops_polling(self):
        # Два цикла на одного продавца — это вдвое больше запросов и
        # уведомления по два раза.
        async def go():
            tm = manager()
            tm.start_for_user(1)
            first = tm._chat_tasks[1]
            tm.start_for_user(1)
            await asyncio.sleep(0)
            self.assertTrue(first.cancelled() or first.done())
            self.assertIsNot(tm._chat_tasks[1], first)
            tm.stop_for_user(1)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
