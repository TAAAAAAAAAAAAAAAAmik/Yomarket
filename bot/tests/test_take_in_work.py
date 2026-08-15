"""Оплаченный заказ, который никто не взял в работу.

Продавец прислал снимок панели: заказ #1217623, статус «Оплачен», кнопка
«В работу» не нажата, автопринятие включено. Бот заказ видел — автоответ
покупателю по этому же заказу ушёл в 04:32.

Автопринятие знало ровно два момента: заказ увиден впервые и пришла оплата.
Оба пропускаются штатно:

* первый проход после чистого хранилища записывает все заказы молча, а без
  DATABASE_URL хранилище на Railway стирается при каждом редеплое;
* тумблер могли включить уже после покупки.

Дальше статус не менялся — значит не было и повода что-то делать. Заказ
оставался оплаченным навсегда, и понять это со стороны было нельзя ничем:
ни экраном, ни сообщением.

Вторая половина файла — про то, что каждое «не взял» теперь названо вслух.
Догоняющее принятие обязано молчать в нескольких случаях (заказ старый, уже
жали, маркетплейс отказал), и молчаливый пропуск здесь — та же беда, с
которой начинали.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import tasks.manager as M      # noqa: E402
from handlers.orders import _why_not_in_work      # noqa: E402


class API:
    """Магазин, где заказ уже лежит оплаченным — не появился только что."""

    def __init__(self, orders, after="work", fail=None):
        self.orders = orders
        self.after, self.fail = after, fail
        self.worked: list[str] = []
        self.tries: list[str] = []      # включая неудачные

    async def start(self):
        pass

    async def close(self):
        pass

    async def get_orders(self, cursor=None):
        return {"data": self.orders}

    async def get_order(self, oid):
        for o in self.orders:
            if str(o["id"]) == str(oid):
                return {"data": dict(o, status=self.after) if str(oid)
                        in self.worked else {**o}}
        return {}

    async def work_order(self, oid):
        self.tries.append(str(oid))
        if self.fail:
            raise RuntimeError(self.fail)
        self.worked.append(str(oid))
        return {"ok": True}

    async def get_messages(self, cid):
        return {"data": []}

    async def send_message(self, cid, text):
        return {"ok": True}


def order(oid="1217623", status="paid", created=None):
    row = {"id": oid, "status": status, "title": "50 звёзд", "price": 60,
           "chat_id": "1139042"}
    if created is not None:
        row["created_at"] = created
    return row


def run(orders, *, known=None, details=None, enabled=True, after="work",
        fail=None, extra=None, passes=1):
    """Прогон фонового цикла по заказам, которые бот уже знает."""
    api = API(orders, after, fail)
    real_api, real_save = M.YooMarketAPI, M.save_settings
    M.YooMarketAPI = lambda token=None: api
    M.save_settings = lambda uid, s: None
    tm = M.TaskManager.__new__(M.TaskManager)
    tm.notes: list[str] = []

    async def notify(uid, text, **kw):
        tm.notes.append(text)

    tm._notify = notify
    now = time.time()
    s = {
        "known_orders": known if known is not None
        else {str(o["id"]): o["status"] for o in orders},
        "known_order_details": details if details is not None
        else {str(o["id"]): {"seen_at": now - 600, "chat_id": "1139042"}
              for o in orders},
        "orders_initialized": True,
        "auto_accept": {"enabled": enabled},
        "notify_orders": {"enabled": True},
        "auto_reply": {"enabled": False},
        "plugins": {"auto_stars": {"enabled": False}},
    }
    s.update(extra or {})
    try:
        for _ in range(passes):
            asyncio.run(tm._process_orders(1, "tok", s))
    finally:
        M.YooMarketAPI, M.save_settings = real_api, real_save
    return s, api, tm.notes


class AnOrderLyingPaidIsPickedUpLater(unittest.TestCase):
    """Тот самый #1217623: бот его знает, статус не меняется, кнопка не
    нажата."""

    def test_it_is_taken_into_work(self):
        _s, api, _n = run([order()])
        self.assertEqual(api.worked, ["1217623"])

    def test_the_real_status_is_written_down(self):
        s, _api, _n = run([order()], after="work")
        self.assertEqual(s["known_orders"]["1217623"], "work")

    def test_the_seller_is_told_it_happened(self):
        _s, _api, notes = run([order()])
        self.assertTrue(any("ВЗЯЛ В РАБОТУ" in n for n in notes), notes)

    def test_and_only_once(self):
        """Заказ, который после нажатия остался оплаченным, — это круг на
        каждый проход, а не настойчивость."""
        _s, api, _n = run([order()], after="paid", passes=3)
        self.assertEqual(len(api.worked), 1, api.worked)

    def test_a_switched_off_autoaccept_takes_nothing(self):
        _s, api, _n = run([order()], enabled=False)
        self.assertEqual(api.worked, [])


class ItDoesNotPressWhatItShouldNotPress(unittest.TestCase):
    def test_an_order_already_in_work_is_left_alone(self):
        _s, api, _n = run([order(status="work")])
        self.assertEqual(api.worked, [])

    def test_a_finished_one_too(self):
        """«success» — выполненный заказ. Нажать по нему «в работу» значило
        бы отчитаться о выдаче второй раз."""
        _s, api, _n = run([order(status="success")])
        self.assertEqual(api.worked, [])

    def test_a_refunded_one_too(self):
        _s, api, _n = run([order(status="refunded")])
        self.assertEqual(api.worked, [])

    def test_an_unpaid_one_too(self):
        """Панель на неоплаченный заказ прямо пишет «не выдавайте товар»."""
        _s, api, _n = run([order(status="new")])
        self.assertEqual(api.worked, [])

    def test_an_old_order_is_not_taken_retroactively(self):
        """Старый оплаченный заказ мог быть выдан вручную. Там, где «в
        работу» означает «выдал», нажатие по нему — ложный отчёт."""
        old = time.time() - 30 * 86400
        _s, api, _n = run([order()],
                          details={"1217623": {"seen_at": old,
                                               "chat_id": "1139042"}})
        self.assertEqual(api.worked, [])

    def test_the_marketplace_creation_time_wins_over_when_the_bot_saw_it(self):
        """После чистки хранилища бот видит всю историю магазина заново, и по
        «когда увидел» ей всей выходит сегодняшний возраст."""
        from datetime import datetime, timedelta, timezone
        long_ago = (datetime.now(timezone.utc)
                    - timedelta(days=30)).isoformat()
        _s, api, _n = run([order(created=long_ago)],
                          details={"1217623": {"seen_at": time.time(),
                                               "chat_id": "1139042"}})
        self.assertEqual(api.worked, [])

    def test_a_batch_is_not_pressed_all_at_once(self):
        """Чистка хранилища открывает разом все незакрытые заказы магазина.
        Залп из десятков действий — не то, что продавец включал."""
        rows = [order(oid=str(1217600 + i)) for i in range(10)]
        _s, api, _n = run(rows)
        self.assertEqual(len(api.worked), M._CATCHUP_PER_PASS, api.worked)

    def test_an_order_awaiting_stars_is_not_reported_as_delivered(self):
        _s, api, _n = run(
            [order()],
            extra={"auto_accept": {"enabled": True, "means_fulfilled": True},
                   "plugins": {"auto_stars": {
                       "enabled": True,
                       "pending": {"1217623": {"quantity": 50}}}}})
        self.assertEqual(api.worked, [])


class ARefusalIsRecordedAndNotRepeatedForever(unittest.TestCase):
    def test_a_refusal_does_not_change_the_status(self):
        s, _api, _n = run([order()], fail="incorrect_status")
        self.assertEqual(s["known_orders"]["1217623"], "paid")

    def test_the_reason_is_kept_where_the_seller_can_see_it(self):
        s, _api, _n = run([order()], fail="incorrect_status")
        det = s["known_order_details"]["1217623"]
        self.assertIn("incorrect_status", det.get("work_error", ""))

    def test_and_the_bot_stops_after_a_few_tries(self):
        """Долбиться в отказ каждую минуту — не настойчивость, а шум."""
        _s, api, _n = run([order()], fail="incorrect_status", passes=6)
        self.assertEqual(api.worked, [])
        self.assertEqual(len(api.tries), M._CATCHUP_TRIES, api.tries)


class TheOrderCardAnswersWhyItIsNotInWork(unittest.TestCase):
    """Раньше на этот вопрос отвечал лог на сервере, то есть никто."""

    def card(self, status="paid", **extra):
        s = {"auto_accept": {"enabled": True}, "known_order_details": {},
             "plugins": {"auto_stars": {"enabled": False}}}
        s.update(extra)
        return _why_not_in_work(s, status, "1217623")

    def test_a_working_order_raises_no_question(self):
        self.assertEqual(self.card("work"), "")

    def test_nor_a_finished_one(self):
        self.assertEqual(self.card("success"), "")

    def test_nor_a_refunded_one(self):
        self.assertEqual(self.card("refunded"), "")

    def test_a_switched_off_autoaccept_says_where_to_switch_it_on(self):
        self.assertIn("Автопилот",
                      self.card(auto_accept={"enabled": False}))

    def test_an_unpaid_order_says_so_instead_of_blaming_the_switch(self):
        got = self.card("new")
        self.assertIn("оплач", got)
        self.assertNotIn("Автопилот", got)

    def test_a_marketplace_refusal_is_quoted(self):
        got = self.card(known_order_details={
            "1217623": {"work_error": "incorrect_status"}})
        self.assertIn("incorrect_status", got)

    def test_a_skip_reason_is_shown_as_written(self):
        got = self.card(known_order_details={
            "1217623": {"work_skip": "заказ старше 48 ч"}})
        self.assertIn("старше 48 ч", got)

    def test_an_english_code_is_not_the_whole_answer(self):
        """Английский код ошибки на экране продавца — это отписка."""
        got = self.card(known_order_details={
            "1217623": {"work_error": "incorrect_status"}})
        self.assertTrue(got.split(":")[0].strip("⚙️ "))
        self.assertIn("отказал", got)


class TheCardActuallyPrintsIt(unittest.TestCase):
    """Проверка проводки, а не расчёта. В этой сессии уже дважды выходило:
    функция считает верно, а на экран её никто не зовёт — и тест этого не
    замечает, потому что зовёт её сам."""

    def setUp(self):
        import handlers.orders as O
        self.O = O
        self._get = O.get_settings
        self.settings = {
            "auto_accept": {"enabled": True},
            "known_order_details": {
                "1217623": {"work_skip": "заказ старше 48 ч, нажмите вручную"}},
        }
        O.get_settings = lambda uid: self.settings

    def tearDown(self):
        self.O.get_settings = self._get

    def shown(self, status="paid"):
        class Api:
            async def get_order(self, oid):
                return {"data": {"id": oid, "status": status,
                                 "title": "50 звёзд", "price": 60}}

            async def get_ad(self, aid):
                return {}

        said: list[str] = []

        class Msg:
            async def edit_text(self, text, **kw):
                said.append(text)

        class Cb:
            message = Msg()
            from_user = type("U", (), {"id": 1})()

            async def answer(self, *a, **kw):
                pass

        from keyboards.main import OrderCallback
        asyncio.run(self.O.show_order_detail(
            Cb(), OrderCallback(action="view", order_id="1217623"), Api()))
        return said[-1]

    def test_the_reason_is_on_the_card(self):
        self.assertIn("нажмите вручную", self.shown())

    def test_the_raw_status_is_there_too(self):
        """«Оплачен» на экране и `paid` в ответе API — разные вещи, и когда
        автоматика на заказ не сработала, по одному переводу это не
        различить."""
        got = self.shown()
        self.assertIn("paid", got)
        self.assertIn("Оплачен", got)

    def test_a_working_order_gets_no_such_line(self):
        self.assertNotIn("нажмите вручную", self.shown("work"))


class TheSkipReasonsReachTheCard(unittest.TestCase):
    """Проверяется следствие: причина, записанная фоновым циклом, читается
    карточкой заказа. Две половины писались порознь и разошлись бы молча."""

    def test_an_old_order_explains_itself_on_the_card(self):
        old = time.time() - 30 * 86400
        s, _api, _n = run([order()],
                          details={"1217623": {"seen_at": old,
                                               "chat_id": "1139042"}})
        self.assertIn("вручную", _why_not_in_work(s, "paid", "1217623"))

    def test_an_order_awaiting_stars_explains_itself_too(self):
        s, _api, _n = run(
            [order()],
            extra={"auto_accept": {"enabled": True, "means_fulfilled": True},
                   "plugins": {"auto_stars": {
                       "enabled": True,
                       "pending": {"1217623": {"quantity": 50}}}}})
        self.assertIn("звёзд", _why_not_in_work(s, "paid", "1217623"))

    def test_a_refusal_explains_itself_too(self):
        s, _api, _n = run([order()], fail="incorrect_status")
        self.assertIn("incorrect_status", _why_not_in_work(s, "paid", "1217623"))


class TheStatusListIsSharedNotWrittenOutAgain(unittest.TestCase):
    def test_needs_work_is_true_only_for_a_paid_untouched_order(self):
        from orderfields import needs_work
        self.assertTrue(needs_work("paid"))
        for status in ("work", "working", "processing", "success", "confirmed",
                       "refunded", "expired", "new", "created", ""):
            self.assertFalse(needs_work(status), status)

    def test_the_loop_uses_it(self):
        import inspect
        self.assertIn("_needs_work(status)",
                      inspect.getsource(M.TaskManager._process_orders))

    def test_and_so_does_the_card(self):
        import inspect
        import handlers.orders as O
        self.assertIn("needs_work", inspect.getsource(O._why_not_in_work))


if __name__ == "__main__":
    unittest.main()
