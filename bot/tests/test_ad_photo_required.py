"""Фото при создании товара обязательно — и отказ приходит сразу.

Панель YooMarket объявление без картинки не принимает. Пока в мастере была
кнопка «Без фото», продавец проходил все пять шагов, нажимал «Создать
товар» — и получал отказ панели в самом конце, когда всё уже введено.

Проверяется здесь не текст экрана, а следствие: чего нельзя нажать и что
не уходит на маркетплейс.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import create_ad as C          # noqa: E402


def callbacks(kb) -> list[str]:
    return [] if kb is None else [b.callback_data for row in kb.inline_keyboard
                                  for b in row]


class Screen:
    """Сообщение бота: помним, что показали и с какой клавиатурой."""

    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []

    async def edit_text(self, text="", reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)

    async def answer(self, text="", reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)


class CB:
    def __init__(self, data="", uid=1):
        self.data = data
        self.message = Screen()
        self.from_user = type("U", (), {"id": uid})()
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self, **data):
        self.data = dict(data)
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None
        self.data = {}

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class API:
    """Двойник маркетплейса: считает, что до него дошло."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create_ad(self, **kw):
        self.calls.append(kw)
        return {"id": 1}


class ThePreviewDoesNotOfferWhatThePanelWillRefuse(unittest.TestCase):

    def test_without_a_photo_there_is_no_create_button(self):
        """Кнопка, отвечающая отказом, — обещание невыполнимого."""
        self.assertNotIn("create_ad:submit", callbacks(C._confirm_kb(has_photo=False)))

    def test_with_a_photo_the_create_button_is_there(self):
        self.assertIn("create_ad:submit", callbacks(C._confirm_kb(has_photo=True)))

    def test_adding_a_photo_is_offered_first_when_it_is_missing(self):
        """Единственная дорога дальше не должна лежать второй."""
        self.assertEqual(callbacks(C._confirm_kb(has_photo=False))[0],
                         "create_ad:edit:photo")


class ThePhotoStepHasNoWayPast(unittest.TestCase):

    def test_the_step_does_not_offer_to_skip(self):
        screen, fsm = Screen(), FSM()
        asyncio.run(C._ask_photo(screen, fsm, edit=False))
        self.assertNotIn("create_ad:photo_skip", callbacks(screen.kbs[-1]))

    def test_the_step_still_lets_you_out(self):
        """Обязательное фото не значит запертый экран."""
        screen, fsm = Screen(), FSM()
        asyncio.run(C._ask_photo(screen, fsm, edit=False))
        self.assertIn("menu:ads", callbacks(screen.kbs[-1]))

    def test_the_old_skip_button_answers_instead_of_hanging(self):
        """Кнопка снята, но висит в прежних сообщениях: нажатие без ответа
        Telegram гасит молча, и экран выглядит сломанным."""
        cb, fsm = CB("create_ad:photo_skip"), FSM()
        asyncio.run(C.ad_photo_skip(cb, fsm))
        self.assertEqual(fsm.state, C.CreateAdState.photo)
        self.assertTrue(any(a for a in cb.alerts))


class NothingWithoutAPhotoReachesTheMarketplace(unittest.TestCase):

    def test_submitting_without_a_photo_sends_nothing(self):
        api = API()
        cb = CB("create_ad:submit")
        fsm = FSM(title="1000 Robux", price=990, description="код", quantity=1)
        asyncio.run(C.submit_ad(cb, fsm, api))
        self.assertEqual(api.calls, [])

    def test_submitting_without_a_photo_returns_to_the_photo_step(self):
        api = API()
        cb = CB("create_ad:submit")
        fsm = FSM(title="1000 Robux", price=990, description="код", quantity=1)
        asyncio.run(C.submit_ad(cb, fsm, api))
        self.assertEqual(fsm.state, C.CreateAdState.photo)

    def test_a_photo_whose_file_is_gone_counts_as_no_photo(self):
        """Путь в состоянии есть, файла нет — после редеплоя без volume это
        обычное дело. Отправить такое значит создать товар без картинки."""
        api = API()
        cb = CB("create_ad:submit")
        fsm = FSM(title="1000 Robux", price=990, description="код", quantity=1,
                  photo_path="/nonexistent/photo.jpg")
        asyncio.run(C.submit_ad(cb, fsm, api))
        self.assertEqual(api.calls, [])


class ATemplateWhosePhotoVanishedSaysSo(unittest.TestCase):
    """Каталог данных на Railway стирается при редеплое, и путь к
    картинке остаётся, а файла нет. Панель товар без картинки не примет —
    молчаливая пропажа читается как забывчивость образца: поля на месте,
    фото нет, и почему, непонятно."""

    TEMPLATE = {"title": "1000 Robux", "price": 990, "description": "код",
                "quantity": 1, "photo_path": "/nonexistent/photo.jpg"}

    def setUp(self):
        import features
        import storage
        self.storage = storage
        self._get, self._save = storage.get_settings, storage.save_settings
        self._shown = features.ad_templates_shown
        self.features = features
        storage.get_settings = lambda uid: {"ad_templates": [dict(self.TEMPLATE)]}
        storage.save_settings = lambda uid, s: None
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        self.storage.get_settings = self._get
        self.storage.save_settings = self._save
        self.features.ad_templates_shown = self._shown

    def test_the_loss_is_announced_not_swallowed(self):
        cb = CB("create_ad:use_template:0")
        asyncio.run(C.use_template(cb, FSM()))
        self.assertTrue(any("Фото" in a for a in cb.alerts), cb.alerts)

    def test_and_the_preview_will_not_let_it_through(self):
        cb = CB("create_ad:use_template:0")
        asyncio.run(C.use_template(cb, FSM()))
        self.assertNotIn("create_ad:submit", callbacks(cb.message.kbs[-1]))


if __name__ == "__main__":
    unittest.main()
