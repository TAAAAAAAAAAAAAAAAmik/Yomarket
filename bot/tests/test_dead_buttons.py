"""Кнопки, которые обещали и не делали.

Три места отчитывались об успехе или вели в тупик: «Поднять весь пак» рисовал
«✅ Пак поднят · Поднято: 0», кнопки вывода ходили в несуществующий API, а
отзывы читались оттуда же и молча возвращали пустоту. Здесь проверяется, что
каждая теперь идёт рабочим путём — через панель — и говорит правду об исходе.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

BOT = pathlib.Path(__file__).resolve().parent.parent

from handlers import packs as PK          # noqa: E402
from handlers import balance as BL        # noqa: E402


class Screen:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


class CB:
    def __init__(self, data):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": 1})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    async def clear(self):
        pass

    async def set_state(self, s):
        pass


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


class Patched(unittest.TestCase):
    """Подмена, которая сама за собой убирает.

    Здесь подменяются функции в общих модулях — `promo_params`, чтение
    настроек, вызов панели. Без отката они остаются подменёнными до конца
    процесса: «тарифа нет» из одного теста уезжало в чужой набор, и
    тринадцать тестов автоподнятия падали, хотя код был исправен.
    """

    def setUp(self) -> None:
        self._undo: list[tuple[object, str, object, bool]] = []

    def patch(self, module, name: str, value) -> None:
        had = hasattr(module, name)
        self._undo.append((module, name, getattr(module, name, None), had))
        setattr(module, name, value)

    def tearDown(self) -> None:
        for module, name, old, had in reversed(self._undo):
            if had:
                setattr(module, name, old)
            else:
                delattr(module, name)


class NothingUsesTheApiThatHasNoSuchMethod(unittest.TestCase):
    """У Юмаркета нет ни поднятия, ни вывода, ни отзывов через API."""

    def _sources(self):
        for path in (BOT / "handlers").glob("*.py"):
            yield path, path.read_text()
        for name in ("tasks/manager.py", "api/yoomarket.py"):
            yield BOT / name, (BOT / name).read_text()

    def test_no_caller_of_bump_ad_is_left(self):
        for path, text in self._sources():
            self.assertNotIn("api.bump_ad", text, path.name)
            self.assertNotIn("bump_all_ads", text, path.name)

    def test_the_withdraw_api_method_is_gone(self):
        for path, text in self._sources():
            self.assertNotIn("withdraw_balance", text, path.name)

    def test_the_reviews_api_method_is_gone(self):
        for path, text in self._sources():
            self.assertNotIn("get_reviews", text, path.name)


class TakingAListingOffSaleByHandIsGone(unittest.TestCase):
    """Ручные «⏸ Снять с продажи» и «▶️ Вернуть в продажу» сняты по решению
    продавца: то же самое делается на сайте, а в боте это была лишняя кнопка
    рядом с платными.

    Автовозврат истёкших объявлений — отдельная функция и остаётся: он
    ходит через `restore_ad`, и трогать его нельзя.
    """

    def _sources(self):
        for path in (BOT / "handlers").glob("*.py"):
            yield path, path.read_text()
        for name in ("tasks/manager.py", "api/yoomarket.py"):
            yield BOT / name, (BOT / name).read_text()

    def test_the_buttons_are_not_offered_anywhere(self):
        for path, text in self._sources():
            self.assertNotIn("Снять с продажи", text, path.name)
            self.assertNotIn("Вернуть в продажу", text, path.name)

    def test_their_handlers_are_gone(self):
        for path, text in self._sources():
            self.assertNotIn("ad_pause", text, path.name)
            self.assertNotIn("ad_activate", text, path.name)

    def test_the_api_method_behind_them_is_gone_too(self):
        """Мёртвый метод — приглашение вернуть кнопку не подумав."""
        for path, text in self._sources():
            self.assertNotIn("unpublish_ad", text, path.name)

    def test_but_restoring_expired_listings_still_works(self):
        """Автовозврат — это C3 и C4, их никто не отменял."""
        from api.yoomarket import YooMarketAPI
        self.assertTrue(hasattr(YooMarketAPI, "restore_ad"))
        self.assertTrue(hasattr(YooMarketAPI, "restore_ads"))


class BumpingAPack(Patched):
    def setUp(self):
        super().setUp()
        self.settings = {"ad_packs": {"Мой пак": ["1", "2", "3"]}}
        import storage
        import handlers.selenium_settings as SS
        self.patch(PK, "get_settings", lambda uid: self.settings)
        self.patch(PK, "get_panel_creds", None)   # ставится через модуль storage
        self.patch(storage, "get_panel_creds", lambda uid: {"cookies": {"a": "b"}})
        self.patch(SS, "promo_params", lambda s: {"up_id": "1"})
        self.patch(SS, "promo_price", lambda s: 19)
        self.ss = SS

    def test_it_asks_before_spending(self):
        """«Премиум» платный и берётся за каждый товар — молча нельзя."""
        cb = CB("pack:bump:0")
        asyncio.run(PK.pack_bump_ask(cb))
        self.assertIn("платная", cb.message.last)
        self.assertIn("pack:bumpok:0", callbacks(cb.message.kbs[-1]))

    def test_the_estimate_counts_every_item(self):
        cb = CB("pack:bump:0")
        asyncio.run(PK.pack_bump_ask(cb))
        self.assertIn("57 ₽", cb.message.last, cb.message.last)   # 19 × 3

    def test_without_a_tariff_it_sends_you_to_pick_one(self):
        self.patch(self.ss, "promo_params", lambda s: {})
        cb = CB("pack:bump:0")
        asyncio.run(PK.pack_bump_ask(cb))
        self.assertIn("promo:setup", callbacks(cb.message.kbs[-1]))

    def _run_bump(self, outcomes):
        import automation.panel as PP
        seq = list(outcomes)

        def fake(cookies, item_id, uid=None, confirm=False, params=None):
            return seq.pop(0)

        self.patch(PP, "panel_bump_item_sync", fake)
        cb = CB("pack:bumpok:0")
        asyncio.run(PK.pack_bump(cb))
        return cb

    def test_all_three_promoted_reads_as_promoted(self):
        cb = self._run_bump([(True, "ок")] * 3)
        self.assertIn("поднят", cb.message.last)
        self.assertIn("3</b> из 3", cb.message.last)

    def test_none_promoted_is_not_called_promoted(self):
        """Ровно прежняя ложь: «✅ Пак поднят · Поднято: 0»."""
        cb = self._run_bump([(False, "нет тарифа")] * 3)
        self.assertIn("не поднят", cb.message.last, cb.message.last)
        self.assertNotIn("✅ <b>", cb.message.last)
        self.assertIn("0</b> из 3", cb.message.last)

    def test_partial_success_says_partial(self):
        cb = self._run_bump([(True, "ок"), (False, "отказ"), (True, "ок")])
        self.assertIn("частично", cb.message.last, cb.message.last)
        self.assertIn("2</b> из 3", cb.message.last)

    def test_the_reason_is_shown_once_not_per_item(self):
        cb = self._run_bump([(False, "нет прав")] * 3)
        self.assertEqual(cb.message.last.count("нет прав"), 1, cb.message.last)


class WithdrawingMoney(Patched):
    def setUp(self):
        super().setUp()
        self.settings = {"auto_withdraw": {}}
        self.patch(BL, "get_settings", lambda uid: self.settings)
        self.patch(BL, "save_settings", lambda uid, s: None)

    def _ready(self):
        self.settings["auto_withdraw"] = {
            "panel_balance_id": "5", "panel_action_key": "withdraw",
            "panel_values": {"card": "1234567890123456", "system": "sbp"}}

    def test_without_details_it_says_what_to_do_first(self):
        cb = CB("balance:withdraw")
        asyncio.run(BL.withdraw_start(cb, FSM(), None))
        self.assertIn("Сначала", cb.message.last)
        self.assertIn("balance:auto", callbacks(cb.message.kbs[-1]))

    def test_with_details_it_asks_for_an_amount(self):
        self._ready()
        import tasks.manager as M
        self.patch(M, "shop_balance", _fake_balance(1500.0, "1 500"))
        cb = CB("balance:withdraw")
        asyncio.run(BL.withdraw_start(cb, FSM(), None))
        self.assertIn("1 500", cb.message.last)
        self.assertIn("balance:withdraw_all", callbacks(cb.message.kbs[-1]))

    def test_withdraw_all_confirms_before_moving_money(self):
        self._ready()
        import tasks.manager as M
        self.patch(M, "shop_balance", _fake_balance(1500.0, "1 500"))
        cb = CB("balance:withdraw_all")
        asyncio.run(BL.withdraw_all(cb, FSM(), None))
        self.assertIn("Подтверди", cb.message.last)
        self.assertIn("wd:go:1500", callbacks(cb.message.kbs[-1]))

    def test_an_empty_balance_is_not_offered_for_withdrawal(self):
        self._ready()
        import tasks.manager as M
        self.patch(M, "shop_balance", _fake_balance(0.0, "0"))
        cb = CB("balance:withdraw_all")
        asyncio.run(BL.withdraw_all(cb, FSM(), None))
        self.assertIn("выводить нечего", cb.message.last)
        self.assertFalse([c for c in callbacks(cb.message.kbs[-1])
                          if c.startswith("wd:go")])

    def test_the_confirmation_masks_the_card(self):
        self._ready()
        msg = Screen()
        asyncio.run(BL._confirm_payout(msg, 1, 500))
        self.assertNotIn("1234567890123456", msg.last)
        self.assertIn("500 ₽", msg.last)

    def test_the_details_screen_no_longer_offers_automation(self):
        """Автовывод убран: заявку отправляет человек."""
        self._ready()
        cb = CB("balance:auto")
        asyncio.run(BL.auto_menu(cb, FSM()))
        data = callbacks(cb.message.kbs[-1])
        self.assertNotIn("balance:auto_toggle", data)
        self.assertIn("wd:manual", data)
        self.assertIn("Автоматического вывода нет", cb.message.last)


def _fake_balance(amount, text):
    async def shop_balance(uid, api):
        return amount, text
    return shop_balance


class ReadingReviews(Patched):
    """Отзывы теперь читаются из панели — в API их нет вовсе."""

    def test_the_parser_understands_a_nova_row(self):
        from automation.panel import _parse_review
        row = {"id": 12, "fields": [
            {"attribute": "rating", "value": 5},
            {"attribute": "comment", "value": "Всё быстро, спасибо"},
            {"attribute": "user", "value": "Вася"},
            {"attribute": "created_at", "value": "2026-08-01 10:00:00"},
        ]}
        got = _parse_review(row)
        self.assertEqual(got["rating"], 5)
        self.assertEqual(got["author"], "Вася")
        self.assertIn("быстро", got["text"])
        self.assertTrue(got["ts"])

    def test_a_count_field_is_not_mistaken_for_a_score(self):
        """«rating_count: 1620» — не оценка 1620."""
        from automation.panel import _parse_review
        got = _parse_review({"id": 1, "fields": [
            {"attribute": "rating_count", "value": 1620}]})
        self.assertIsNone(got["rating"])

    def test_a_resource_without_reviews_is_not_accepted_as_one(self):
        from automation import panel as P

        class Sess:
            def get(self, url, params=None, **kw):
                class R:
                    status_code = 200

                    @staticmethod
                    def json():
                        # что-то есть, но на отзыв не похоже
                        return {"resources": [{"id": 1, "fields": [
                            {"attribute": "name", "value": "категория"}]}]}
                return R()

        self.patch(P, "_make_panel_requests_session", lambda ck: Sess())
        self.patch(P, "_panel_xsrf_headers", lambda s, ck: {})
        ok, got = P.panel_reviews_sync("cookie")
        self.assertFalse(ok)
        self.assertIn("похожих на отзыв 0", str(got))


if __name__ == "__main__":
    unittest.main()
