"""Копия товара: тот же товар из панели за одно нажатие.

Мастер спрашивает название, цену, описание, количество, фото — а потом
раздел панели и её `filter__N`, и вот их пять-шесть штук, причём про
половину панель до выбора категории молчит вовсе. Второй такой же товар
проходил весь этот круг заново.

Главное здесь — **откуда берётся список**. Первая версия помнила товары,
созданные самим ботом: у продавца их было семь, все заведены раньше, и
развилка не появлялась вовсе. Снаружи это «кнопка не работает».

Поэтому список читается ИЗ ПАНЕЛИ, и копирует тоже она: у неё есть
`filter__N` этого товара, которых у бота нет и взяться им неоткуда.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import features                                            # noqa: E402
import storage                                             # noqa: E402
from automation import panel as P                          # noqa: E402
from handlers import create_ad as C                        # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Msg:
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


class CB:
    def __init__(self, data, uid=7):
        self.data = data
        self.message = Msg()
        self.from_user = type("U", (), {"id": uid})()
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)
        return dict(self.data)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.data, self.state = {}, None


class Bench(unittest.TestCase):
    UID = 7

    def setUp(self):
        self._creds = storage.get_panel_creds
        self._shown = features.ad_templates_shown
        self._list = P.panel_list_items_sync
        self._clone = P.panel_clone_item_sync
        storage.get_panel_creds = lambda uid: {"cookies": "c=1"}
        features.ad_templates_shown = lambda uid: True
        self.items = [{"id": 11, "title": "1000 Robux", "price": 990},
                      {"id": 12, "title": "Apple 10 TRY", "price": 350}]
        self.listed = (True, self.items)
        self.cloned = (True, "55")
        self.clone_calls: list = []
        P.panel_list_items_sync = lambda cookies: self.listed
        P.panel_clone_item_sync = lambda cookies, item_id, uid=None: (
            self.clone_calls.append(item_id), self.cloned)[1]

    def tearDown(self):
        storage.get_panel_creds = self._creds
        features.ad_templates_shown = self._shown
        P.panel_list_items_sync = self._list
        P.panel_clone_item_sync = self._clone

    def kb(self, cb):
        return [b.callback_data for row in cb.message.kbs[-1].inline_keyboard
                for b in row]


class TheForkOffersBothWays(Bench):

    def test_it_offers_to_copy(self):
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertIn("create_ad:templates_list", self.kb(cb))
        self.assertIn("create_ad:new", self.kb(cb))

    def test_it_does_not_depend_on_what_the_bot_created_itself(self):
        """Первая версия показывала развилку только тем, у кого бот уже
        создавал товар. У продавца их семь, все заведены раньше, — и
        развилки не было вовсе."""
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertIn("create_ad:templates_list", self.kb(cb))

    def test_the_fork_does_not_wait_for_a_title(self):
        """Экран, ждущий ввода, съел бы нажатие на собственную кнопку."""
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertIsNone(fsm.state)

    def test_without_the_panel_there_is_no_fork(self):
        """Копируется товар ИЗ ПАНЕЛИ. Кнопка, за которой «войди в
        панель», обещает то, чего за ней нет."""
        storage.get_panel_creds = lambda uid: {}
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertNotIn("create_ad:templates_list", self.kb(cb))
        self.assertEqual(fsm.state, C.CreateAdState.title,
                         "мастер стал недоступен вместе с копией")

    def test_the_other_way_still_asks_the_title(self):
        cb, fsm = CB("create_ad:new"), FSM()
        run(C.create_ad_new(cb, fsm))
        self.assertEqual(fsm.state, C.CreateAdState.title)


class TheListComesFromThePanel(Bench):

    def test_every_product_gets_a_button(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        data = self.kb(cb)
        self.assertIn("create_ad:copy:11", data)
        self.assertIn("create_ad:copy:12", data)

    def test_the_names_are_on_the_screen(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        said = cb.message.texts[-1]
        self.assertIn("1000 Robux", said)
        self.assertIn("990", said)

    def test_an_empty_panel_says_so_and_offers_the_wizard(self):
        """Экран, с которого нечего нажать, — тупик."""
        self.listed = (True, [])
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        self.assertIn("нечего", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_a_refusal_is_shown_in_the_panels_own_words(self):
        """«Не вышло» без причины — отписка: разбирать её продавцу нечем."""
        self.listed = (False, "HTTP 419: сессия истекла")
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        self.assertIn("419", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_the_list_does_not_become_a_wall_of_buttons(self):
        """Панель отдаёт до полусотни: клавиатура из полусотни кнопок —
        это не выбор, а свалка."""
        self.listed = (True, [{"id": i, "title": f"Товар {i}", "price": i}
                              for i in range(40)])
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        copies = [d for d in self.kb(cb) if d.startswith("create_ad:copy:")]
        self.assertEqual(len(copies), C._COPY_LIMIT)

    def test_a_product_without_an_id_is_skipped(self):
        """Кнопка без номера товара скопировала бы неизвестно что."""
        self.listed = (True, [{"title": "Без номера", "price": 1}])
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        self.assertEqual([d for d in self.kb(cb)
                          if d.startswith("create_ad:copy:")], [])


class ThePanelDoesTheCopying(Bench):

    def test_the_chosen_product_is_the_one_copied(self):
        cb = CB("create_ad:copy:12")
        run(C.copy_item(cb))
        self.assertEqual(self.clone_calls, ["12"])

    def test_the_new_number_is_reported(self):
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        said = cb.message.texts[-1]
        self.assertIn("55", said)
        self.assertIn("Копия создана", said)

    def test_the_stock_is_offered_right_there(self):
        """Без остатка панель товар не публикует, и оставить продавца
        искать, где его добавить, значит оборвать дело на середине."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        data = self.kb(cb)
        self.assertIn("pitem_stock:55", data)
        self.assertIn("cadpub:55", data)

    def test_it_says_the_codes_are_not_copied(self):
        """Остаток товара с кодами — сами ключи, они одноразовые. Молчание
        здесь читается как «копия готова к продаже»."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        self.assertIn("одноразов", cb.message.texts[-1])

    def test_a_refusal_is_shown_in_the_panels_own_words(self):
        self.cloned = (False, "422: filter__8 — поле Регион обязательно")
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        said = cb.message.texts[-1]
        self.assertIn("Регион", said)
        self.assertIn("не создалась", said)

    def test_a_refusal_offers_no_buttons_for_a_product_that_does_not_exist(self):
        self.cloned = (False, "422")
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        data = self.kb(cb)
        self.assertNotIn("pitem_stock:", " ".join(data))
        self.assertNotIn("cadpub:", " ".join(data))

    def test_a_dead_panel_is_not_reported_as_success(self):
        """«Создал» — не доказательство: панель отвечает 200 и на отказ."""
        def boom(cookies, item_id, uid=None):
            raise RuntimeError("таймаут")

        P.panel_clone_item_sync = boom
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        self.assertIn("не создалась", cb.message.texts[-1])


class TheCopyIsForAdminsOnly(Bench):

    def setUp(self):
        super().setUp()
        # Проверка НАСТОЯЩАЯ: подменённая заглушкой, она не вызывалась бы
        # вовсе, и правка в ней ничего бы не ломала.
        features.ad_templates_shown = self._shown
        self._admin = storage.is_admin
        storage.is_admin = lambda uid: False

    def tearDown(self):
        storage.is_admin = self._admin
        super().tearDown()

    def test_the_check_is_the_real_one(self):
        self.assertFalse(features.ad_templates_shown(self.UID))
        storage.is_admin = lambda uid: True
        self.assertTrue(features.ad_templates_shown(self.UID))

    def test_the_seller_gets_no_fork(self):
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertNotIn("create_ad:templates_list", self.kb(cb))
        self.assertEqual(fsm.state, C.CreateAdState.title)

    def test_the_list_is_closed_from_an_old_message(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [], "показал список постороннему")

    def test_and_so_is_the_copy_itself(self):
        """Нажатие создаёт настоящий товар на витрине — заслон обязателен
        и здесь, а не только на списке."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb))
        self.assertEqual(self.clone_calls, [],
                         "посторонний создал товар копией")


class TheStockIsFilledInWithoutAsking(Bench):
    """Мастер спросил «сколько штук», а отчёт всё равно требовал нажать
    «📦 Добавить остатки» и ввести то же число. Без остатка панель товар не
    публикует, то есть круг был обязательным."""

    class Api:
        def __init__(self, kind="auto-value", stock=0, after=None):
            self.kind, self.stock = kind, stock
            self.after = stock if after is None else after
            self.refilled: list = []
            self.reads = 0

        async def get_ad(self, ad_id):
            return {"data": {"type": self.kind}}

        async def ad_stock(self, ad_id, ad=None):
            self.reads += 1
            n = self.stock if self.reads == 1 else self.after
            return bool(n), f"остаток: {n}"

        async def refill_ad_value(self, ad_id, amount):
            self.refilled.append(amount)

    def test_it_tops_up_to_the_asked_number(self):
        api = self.Api(stock=0, after=5)
        said = run(C._fill_stock(api, "55", 5))
        self.assertEqual(api.refilled, [5])
        self.assertIn("5", said)

    def test_it_does_not_double_what_the_panel_already_put_there(self):
        """Панель кладёт количество в свою форму при создании. Прибавить
        сверху столько же значит удвоить остаток."""
        api = self.Api(stock=5, after=5)
        said = run(C._fill_stock(api, "55", 5))
        self.assertEqual(api.refilled, [], "остаток удвоился")
        self.assertIn("на месте", said)

    def test_it_tops_up_only_the_difference(self):
        api = self.Api(stock=2, after=5)
        run(C._fill_stock(api, "55", 5))
        self.assertEqual(api.refilled, [3])

    def test_codes_are_never_copied(self):
        """Остаток товара с авто-выдачей — это сами ключи, одноразовые.
        Взять их из образца значит продать один код дважды."""
        api = self.Api(kind="auto-delivery")
        said = run(C._fill_stock(api, "55", 5))
        self.assertEqual(api.refilled, [])
        self.assertIn("код", said.lower())

    def test_the_report_names_the_number_the_server_returned(self):
        """HTTP 200 не доказательство: перечитываем и печатаем то, что
        ответил маркетплейс, а не то, что отправили."""
        api = self.Api(stock=0, after=2)
        said = run(C._fill_stock(api, "55", 5))
        self.assertIn("2", said)
        self.assertIn("5", said)
        self.assertIn("вручную", said)

    def test_a_broken_call_does_not_eat_the_report(self):
        """Товар уже создан. Исключение отсюда съело бы отчёт о нём."""
        class Dead:
            async def get_ad(self, ad_id):
                raise RuntimeError("сеть")

        said = run(C._fill_stock(Dead(), "55", 5))
        self.assertIn("вручную", said)

    def test_nothing_to_fill_says_nothing(self):
        api = self.Api()
        self.assertEqual(run(C._fill_stock(api, "55", 0)), "")
        self.assertEqual(run(C._fill_stock(None, "55", 5)), "")


if __name__ == "__main__":
    unittest.main()
