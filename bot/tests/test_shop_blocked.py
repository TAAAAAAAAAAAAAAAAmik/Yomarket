"""Заблокированный магазин: продавец узнаёт об этом по-русски и один раз.

Живой случай 14.09: продавец нажал «Объявления» и получил на экран
`Shop is blocked (shop_blocked)` — голый английский код, которого не знал ни
один разбор ошибок в боте. А фоновый проход в это же время падал каждую
минуту в лог, не говоря продавцу ничего: бот выглядел живым и молча ничего
не делал.

Беда здесь двойная. Первая — отписка вместо причины. Вторая дороже: все
соседние отказы ведут к «создай токен заново», и продавец, не получив
другого ответа, пойдёт делать именно это — потратит время и ничего не
починит, потому что блокировку снимает только поддержка маркетплейса.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import autoreply as ar                                        # noqa: E402
from api.yoomarket import auth_trouble                        # noqa: E402

RAW = "Shop is blocked (shop_blocked)"


class TheCodeIsTranslated(unittest.TestCase):
    """Английский код на экране продавца — это отписка."""

    def test_the_raw_marketplace_code_never_reaches_the_seller(self):
        text, _ = ar.explain_error(RAW)
        self.assertNotIn("shop is blocked", text.lower())
        self.assertNotIn("shop_blocked", text.lower())
        self.assertIn("заблокировал магазин", text)

    def test_the_explanation_says_the_token_is_not_to_blame(self):
        """Соседние отказы советуют создать токен заново. Здесь это впустую."""
        text, _ = ar.explain_error(RAW)
        self.assertIn("окен ни при чём", text)

    def test_the_explanation_names_who_can_lift_the_block(self):
        text, _ = ar.explain_error(RAW)
        self.assertIn("поддержка Юмаркета", text)

    def test_answering_by_hand_is_not_advised(self):
        """«Ответьте вручную» здесь — тот же совет биться в стену."""
        _text, by_hand = ar.explain_error(RAW)
        self.assertFalse(by_hand)

    def test_an_unrelated_error_is_not_mistaken_for_a_block(self):
        self.assertEqual(ar.shop_blocked("HTTP 502"), "")
        self.assertEqual(ar.shop_blocked(""), "")
        self.assertEqual(ar.shop_blocked(None), "")


class EveryScreenSaysTheSameThing(unittest.TestCase):
    """Читателей четверо. Разойдясь, они советовали бы разное про одну беду."""

    def test_the_ads_screen_explains_the_block_instead_of_the_code(self):
        from handlers.ads import _load_error
        said = _load_error(Exception(RAW))
        self.assertIn("Магазин заблокирован", said)
        self.assertNotIn("shop_blocked", said.lower())

    def test_connecting_a_shop_does_not_ask_for_a_new_token(self):
        why, what, token_to_blame = auth_trouble(RAW)
        self.assertIn("аблокирован", why)
        self.assertFalse(token_to_blame,
                         "экран попросит прислать токен заново — а он ни при чём")
        self.assertIn("окен ни при чём", what)

    def test_the_wording_comes_from_one_place(self):
        """Тексты сверяются между собой, а не переписаны по местам."""
        from handlers.ads import _load_error
        core = ar.SHOP_BLOCKED[:40]
        # Экран объявлений поднимает первую букву — остальное дословно.
        self.assertIn(core[0].upper() + core[1:], _load_error(Exception(RAW)))
        self.assertIn(core, ar.explain_error(RAW)[0])


class ToldOnceAndCalledOff(unittest.IsolatedAsyncioTestCase):
    """Проход идёт раз в минуту. Без отметки это уведомление раз в минуту."""

    def setUp(self):
        from tasks.manager import TaskManager
        self.tm = TaskManager.__new__(TaskManager)
        self.sent: list[str] = []
        self.saved: list[dict] = []

        async def notify(_uid, text, reply_markup=None):
            self.sent.append(text)

        self.tm._notify = notify
        import tasks.manager as mgr
        self._save = mgr.save_settings
        mgr.save_settings = lambda uid, s: self.saved.append(dict(s))
        self.mgr = mgr

    def tearDown(self):
        self.mgr.save_settings = self._save

    async def test_the_seller_is_told_once_not_every_minute(self):
        settings: dict = {}
        for _ in range(5):
            await self.tm._note_shop_block(1, settings, Exception(RAW))
        self.assertEqual(len(self.sent), 1, "уведомление на каждый проход")
        self.assertIn("ЗАБЛОКИРОВАН", self.sent[0])

    async def test_a_successful_pass_gives_the_all_clear(self):
        settings: dict = {}
        await self.tm._note_shop_block(1, settings, Exception(RAW))
        await self.tm._note_shop_block(1, settings, None)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("СНОВА РАБОТАЕТ", self.sent[1])

    async def test_the_all_clear_is_not_repeated(self):
        settings: dict = {}
        await self.tm._note_shop_block(1, settings, Exception(RAW))
        for _ in range(3):
            await self.tm._note_shop_block(1, settings, None)
        self.assertEqual(len(self.sent), 2)

    async def test_nothing_is_said_when_nothing_was_wrong(self):
        settings: dict = {}
        await self.tm._note_shop_block(1, settings, None)
        self.assertEqual(self.sent, [])

    async def test_another_error_does_not_lift_the_mark(self):
        """Сеть отвалилась — это не доказательство, что блокировку сняли."""
        settings: dict = {}
        await self.tm._note_shop_block(1, settings, Exception(RAW))
        await self.tm._note_shop_block(1, settings, Exception("HTTP 502"))
        self.assertTrue(settings.get("_shop_blocked"))
        self.assertEqual(len(self.sent), 1, "дал отбой по чужой ошибке")

    async def test_a_second_block_is_announced_again(self):
        settings: dict = {}
        await self.tm._note_shop_block(1, settings, Exception(RAW))
        await self.tm._note_shop_block(1, settings, None)
        await self.tm._note_shop_block(1, settings, Exception(RAW))
        self.assertEqual(len(self.sent), 3)
        self.assertIn("ЗАБЛОКИРОВАН", self.sent[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
