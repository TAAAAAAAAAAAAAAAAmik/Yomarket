"""Автовозврат отдаёт деньги — и только там, где точно знает, что вправе.

Это единственная автоматика в боте, которая не зарабатывает, а **отдаёт**.
Ошибка здесь стоит дважды: покупатель получает и товар, и деньги обратно, а
продавец узнаёт об этом из отчёта.

Бот достоверно знает о выдаче только своей. Заказ, который AutoStars всё
ещё ждёт — покупатель не прислал ник, — товара точно не получил, и вернуть
за него деньги можно. Про заказ, выданный вручную, бот не знает ничего,
поэтому осторожный режим его не трогает, а широкий включается через
отдельное предупреждение.

Остальное здесь — предохранители: суточный потолок, отметка уже
возвращённых, и перечитывание статуса после запроса. Код 200 в этом проекте
доказательством не считается нигде, но здесь «вернул» при невернувшемся
заказе дороже любой другой неправды.
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


class FakeApi:
    def __init__(self, after="refunded", fail=False):
        self.refunded: list = []
        self.after, self.fail = after, fail

    async def refund_order(self, oid):
        if self.fail:
            raise RuntimeError("incorrect_status")
        self.refunded.append(str(oid))
        return {"ok": True}

    async def get_order(self, oid):
        return {"id": oid, "status": self.after}


class Bench(unittest.TestCase):
    HOURS_AGO = 60 * 3600

    def setUp(self):
        self.sent: list = []
        self.saved: dict = {}

        class Fake(M.TaskManager):
            def __init__(inner):
                pass

            async def _notify(inner, uid, text, reply_markup=None, **kw):
                self.sent.append(text)

        self.mgr = Fake()
        self._save = M.save_settings
        M.save_settings = lambda uid, s: self.saved.update(s)

    def tearDown(self):
        M.save_settings = self._save

    def settings(self, *, status="work", scope="stars", enabled=True,
                 pending=True, hours=48, age=None, **extra):
        now = time.time()
        s = {
            "auto_refund": {"enabled": enabled, "hours": hours, "scope": scope,
                            "max_per_day": 3, "day": "", "count": 0,
                            "done": []},
            "known_orders": {"1210755": status},
            "known_order_details": {
                "1210755": {"work_at": now - (self.HOURS_AGO if age is None
                                              else age),
                            "title": "100.000.000р Бонусы"}},
            "plugins": {"auto_stars": {"enabled": True,
                                       "pending": {"1210755": {}} if pending
                                       else {}}},
        }
        s.update(extra)
        return s

    def run_it(self, s, api=None):
        api = api or FakeApi()
        asyncio.run(self.mgr._auto_refund(1, api, s))
        return api


class ItRefundsWhatTheBotItselfCouldNotDeliver(Bench):
    def test_an_order_waiting_for_a_username_is_refunded(self):
        api = self.run_it(self.settings())
        self.assertEqual(api.refunded, ["1210755"])

    def test_the_seller_is_told_why(self):
        self.run_it(self.settings())
        self.assertTrue(any("не прислал ник" in t for t in self.sent), self.sent)

    def test_a_fresh_order_is_left_alone(self):
        api = self.run_it(self.settings(age=3600))
        self.assertEqual(api.refunded, [])

    def test_switched_off_it_does_nothing(self):
        api = self.run_it(self.settings(enabled=False))
        self.assertEqual(api.refunded, [])


class ItDoesNotRefundWhatItCannotVouchFor(Bench):
    """Про заказ, выданный вручную, бот не знает ничего. Возврат за него —
    это и товар, и деньги покупателю разом."""

    def test_a_plain_stuck_order_is_not_touched_in_the_careful_mode(self):
        api = self.run_it(self.settings(pending=False))
        self.assertEqual(api.refunded, [])

    def test_and_is_touched_only_when_the_wide_mode_is_chosen(self):
        api = self.run_it(self.settings(pending=False, scope="any"))
        self.assertEqual(api.refunded, ["1210755"])

    def test_a_finished_order_is_never_refunded(self):
        """«success» — это выполненный заказ. Тот самый статус, которого не
        знали пять рукописных списков."""
        api = self.run_it(self.settings(status="success", scope="any"))
        self.assertEqual(api.refunded, [])

    def test_an_already_refunded_one_is_not_refunded_twice(self):
        api = self.run_it(self.settings(status="refunded", scope="any"))
        self.assertEqual(api.refunded, [])

    def test_an_unpaid_order_is_not_refunded(self):
        """Денег ещё нет — возвращать нечего, а запрос выглядел бы как
        отказ покупателю."""
        api = self.run_it(self.settings(status="new", scope="any"))
        self.assertEqual(api.refunded, [])


class TheGuardsAroundIt(Bench):
    def test_the_daily_cap_stops_it(self):
        s = self.settings(scope="any")
        s["known_orders"] = {str(i): "work" for i in range(10)}
        s["known_order_details"] = {
            str(i): {"work_at": time.time() - self.HOURS_AGO} for i in range(10)}
        api = self.run_it(s)
        self.assertEqual(len(api.refunded), 3)

    def test_a_refunded_order_is_remembered(self):
        s = self.settings()
        self.run_it(s)
        self.assertIn("1210755", s["auto_refund"]["done"])

    def test_and_not_attempted_again(self):
        s = self.settings()
        self.run_it(s)
        api = self.run_it(s)
        self.assertEqual(api.refunded, [])

    def test_a_failed_refund_is_reported_and_not_retried(self):
        """Долбиться в отказ каждую минуту — не настойчивость, а шум."""
        s = self.settings()
        api = self.run_it(s, FakeApi(fail=True))
        self.assertEqual(api.refunded, [])
        self.assertTrue(any("НЕ ПРОШЁЛ" in t for t in self.sent), self.sent)
        self.assertIn("1210755", s["auto_refund"]["done"])


class TwoHundredIsNotProof(Bench):
    """Здесь это дороже, чем где бы то ни было: «вернул» при невернувшемся
    заказе продавец увидит только в арбитраже."""

    def test_a_confirmed_refund_says_so(self):
        self.run_it(self.settings(), FakeApi(after="refunded"))
        self.assertTrue(any("подтвердил возврат" in t for t in self.sent),
                        self.sent)

    def test_a_status_that_did_not_change_is_flagged(self):
        self.run_it(self.settings(), FakeApi(after="work"))
        said = "\n".join(self.sent)
        self.assertIn("СТАТУС НЕ ИЗМЕНИЛСЯ", said)
        self.assertIn("вручную", said)

    def test_the_real_status_is_written_down(self):
        s = self.settings()
        self.run_it(s, FakeApi(after="refunded"))
        self.assertEqual(s["known_orders"]["1210755"], "refunded")


class TheScreenSaysWhatItWillDo(unittest.TestCase):
    def test_the_wide_mode_warns_before_switching(self):
        import inspect
        from handlers import auto_settings as A
        src = inspect.getsource(A.set_refund_scope)
        self.assertIn("не знает, выдал ты товар или нет", src)

    def test_switching_back_needs_no_warning(self):
        import inspect
        from handlers import auto_settings as A
        self.assertIn('rf["scope"] = "stars"',
                      inspect.getsource(A.set_refund_scope))

    def test_turning_it_on_says_the_terms_once(self):
        import inspect
        from handlers import auto_settings as A
        self.assertIn("show_alert=True", inspect.getsource(A.toggle_refund))

    def test_the_default_is_off_and_careful(self):
        from storage import _DEFAULT_SETTINGS
        rf = _DEFAULT_SETTINGS["auto_refund"]
        self.assertFalse(rf["enabled"])
        self.assertEqual(rf["scope"], "stars")
        self.assertTrue(rf["max_per_day"])


if __name__ == "__main__":
    unittest.main()
