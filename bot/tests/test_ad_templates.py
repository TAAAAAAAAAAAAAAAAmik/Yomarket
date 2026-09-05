"""Копия товара по образцу: тот же товар за одно нажатие.

Мастер спрашивает название, цену, описание, количество, фото — а потом
раздел панели и её `filter__N`, и вот их пять-шесть штук, причём про
половину панель до выбора категории молчит вовсе. Второй такой же товар
проходил весь этот круг заново.

Отсюда три требования, и каждое здесь проверяется:

* **образец помнит ВЕСЬ пакет**, ушедший в панель, а не то, что удобно
  показать. Название с ценой — не то, что съедает время;
* **записывается он сам**, по факту созданного товара. Отдельная кнопка
  «сохранить как шаблон» означала бы, что копия есть только у того, кто
  заранее догадался её нажать, — и сохраняла бы товар ДО выбора раздела,
  то есть образец, которым нельзя воспользоваться;
* **копия не врёт о себе.** Пропало фото — сказано; раздела в образце нет
  — сказано до нажатия; остаток не проставился — сказано числом.
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
        self.settings: dict = {}
        self._get, self._save = storage.get_settings, storage.save_settings
        self._shown = features.ad_templates_shown
        storage.get_settings = lambda uid: self.settings
        storage.save_settings = lambda uid, s: self.settings.update(s)
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        storage.get_settings, storage.save_settings = self._get, self._save
        features.ad_templates_shown = self._shown

    def made(self, **over):
        row = {"title": "1000 Robux", "price": 990, "description": "код",
               "quantity": 5, "category": "12", "photo_path": None,
               "extra": {"filter__8": 3}}
        row.update(over)
        return row


class AnEveryCreationBecomesATemplate(Bench):

    def test_the_whole_payload_is_remembered(self):
        """Время съедают раздел и `filter__N`, а не название с ценой."""
        storage.note_ad_made(self.UID, self.made(), {"filter__8": 3}, "55")
        t = storage.ad_templates(self.UID)[0]
        self.assertEqual(t["category"], "12")
        self.assertEqual(t["extra"], {"filter__8": 3})
        self.assertEqual(t["item_id"], "55")

    def test_the_newest_is_first(self):
        storage.note_ad_made(self.UID, self.made(title="Первый"), {})
        storage.note_ad_made(self.UID, self.made(title="Второй"), {})
        self.assertEqual(storage.ad_templates(self.UID)[0]["title"], "Второй")

    def test_the_same_product_does_not_pile_up(self):
        """После трёх копий список состоял бы из одного товара."""
        for _ in range(3):
            storage.note_ad_made(self.UID, self.made(), {"filter__8": 3})
        self.assertEqual(len(storage.ad_templates(self.UID)), 1)

    def test_but_the_same_name_at_another_price_is_another_product(self):
        storage.note_ad_made(self.UID, self.made(price=990), {})
        storage.note_ad_made(self.UID, self.made(price=1490), {})
        self.assertEqual(len(storage.ad_templates(self.UID)), 2)

    def test_the_list_does_not_grow_without_end(self):
        for i in range(storage.AD_TEMPLATES_MAX + 5):
            storage.note_ad_made(self.UID, self.made(title=f"Товар {i}"), {})
        self.assertEqual(len(storage.ad_templates(self.UID)),
                         storage.AD_TEMPLATES_MAX)

    def test_a_nameless_record_is_not_offered(self):
        """Кнопка без надписи — кнопка, о которой не понять, что она."""
        storage.note_ad_made(self.UID, self.made(title="  "), {})
        self.assertEqual(storage.ad_templates(self.UID), [])


class TheTemplateIsWrittenByTheCreationItself(Bench):
    """Отдельная кнопка «сохранить как шаблон» означала бы, что копия есть
    только у того, кто заранее догадался её нажать. Пишется образец там, где
    товар создан, — и проверять это надо на самом создании, а не на функции
    записи: вызов из него можно потерять, и никто не заметит."""

    def setUp(self):
        super().setUp()
        from automation import panel
        self.panel = panel
        self._make = panel.panel_create_product_sync
        self._pub = panel.panel_publish_item_sync
        self._creds = storage.get_panel_creds
        panel.panel_create_product_sync = lambda *a, **kw: (True, "55")
        panel.panel_publish_item_sync = lambda *a, **kw: (True, "ок")
        storage.get_panel_creds = lambda uid: {"cookies": "c=1"}

    def tearDown(self):
        self.panel.panel_create_product_sync = self._make
        self.panel.panel_publish_item_sync = self._pub
        storage.get_panel_creds = self._creds
        super().tearDown()

    def create(self, values=None, extra=None):
        run(C._panel_create_and_report(
            Msg(), self.UID, values or self.made(), extra=extra or {"f": 1},
            state=FSM(), api=None))

    def test_a_created_product_becomes_a_template(self):
        self.assertEqual(storage.ad_templates(self.UID), [])
        self.create()
        made = storage.ad_templates(self.UID)
        self.assertEqual(len(made), 1, "образец не записан")
        self.assertEqual(made[0]["title"], "1000 Robux")

    def test_it_remembers_the_section_and_its_fields(self):
        """То, ради чего образец и заводится: раздел с полями."""
        self.create(extra={"filter__8": 3})
        t = storage.ad_templates(self.UID)[0]
        self.assertEqual(t["extra"], {"filter__8": 3})
        self.assertEqual(t["category"], "12")

    def test_a_refused_creation_leaves_no_template(self):
        """Образец несозданного товара — обещание копии, которая не
        пройдёт."""
        self.panel.panel_create_product_sync = lambda *a, **kw: (False, "422")
        self.create()
        self.assertEqual(storage.ad_templates(self.UID), [])

    def test_a_broken_store_does_not_eat_the_report(self):
        """Товар уже создан. Незаписанный образец — потеря удобства, а
        исключение отсюда съело бы отчёт о создании."""
        def boom(*a, **kw):
            raise RuntimeError("хранилище")

        # Возвращаем ПРЕЖНЮЮ функцию, а не удаляем подменённую: `del` снёс
        # бы её из модуля насовсем, и соседние тесты падали бы «сами по
        # себе». Так и вышло на первом же прогоне.
        was = storage.note_ad_made
        storage.note_ad_made = boom
        try:
            msg = Msg()
            run(C._panel_create_and_report(msg, self.UID, self.made(),
                                           extra={}, state=FSM(), api=None))
        finally:
            storage.note_ad_made = was
        self.assertTrue(any("создан" in t for t in msg.texts), msg.texts)


class TheForkAppearsOnlyWhenThereIsSomethingToCopy(Bench):

    def kb(self, cb):
        return [b.callback_data for row in cb.message.kbs[-1].inline_keyboard
                for b in row]

    def test_without_templates_it_goes_straight_to_the_title(self):
        """Развилка у того, кому нечего копировать, — лишний шаг перед
        единственным настоящим действием."""
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertNotIn("create_ad:templates_list", self.kb(cb))
        self.assertEqual(fsm.state, C.CreateAdState.title)

    def test_with_templates_both_ways_are_offered(self):
        storage.note_ad_made(self.UID, self.made(), {})
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        data = self.kb(cb)
        self.assertIn("create_ad:new", data)
        self.assertIn("create_ad:templates_list", data)

    def test_the_fork_does_not_wait_for_a_title(self):
        """Экран, ждущий ввода, съел бы нажатие на собственную кнопку."""
        storage.note_ad_made(self.UID, self.made(), {})
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertIsNone(fsm.state)

    def test_the_other_way_still_asks_the_title(self):
        cb, fsm = CB("create_ad:new"), FSM()
        run(C.create_ad_new(cb, fsm))
        self.assertEqual(fsm.state, C.CreateAdState.title)


class TheCopyGoesStraightToThePanel(Bench):

    def setUp(self):
        super().setUp()
        self.sent: list[dict] = []
        self._create = C._panel_create_and_report

        async def fake(msg, uid, values, extra=None, picked=None,
                       state=None, api=None):
            self.sent.append({"values": values, "extra": extra, "api": api})

        C._panel_create_and_report = fake

    def tearDown(self):
        C._panel_create_and_report = self._create
        super().tearDown()

    def test_no_questions_are_asked(self):
        storage.note_ad_made(self.UID, self.made(), {"filter__8": 3})
        cb, fsm = CB("create_ad:use_template:0"), FSM()
        run(C.use_template(cb, fsm))
        self.assertEqual(len(self.sent), 1, "товар в панель не ушёл")
        self.assertEqual(self.sent[0]["extra"], {"filter__8": 3})

    def test_the_product_is_the_same_one(self):
        storage.note_ad_made(self.UID, self.made(), {"filter__8": 3})
        cb, fsm = CB("create_ad:use_template:0"), FSM()
        run(C.use_template(cb, fsm))
        got = self.sent[0]["values"]
        self.assertEqual(got["title"], "1000 Robux")
        self.assertEqual(got["price"], 990)
        self.assertEqual(got["category"], "12")
        self.assertEqual(got["quantity"], 5)

    def test_a_template_without_a_section_asks_instead_of_failing(self):
        """Образец прежней кнопки «сохранить как шаблон» раздела не помнит.
        Отправить его как есть значит показать продавцу отказ 422."""
        self.settings["ad_templates"] = [{"title": "Старый", "price": 10}]
        cb, fsm = CB("create_ad:use_template:0"), FSM()
        run(C.use_template(cb, fsm))
        self.assertEqual(self.sent, [], "ушёл заведомо неполный товар")
        self.assertTrue(any("раздел" in a for a in cb.alerts), cb.alerts)

    def test_a_missing_template_says_so(self):
        cb, fsm = CB("create_ad:use_template:9"), FSM()
        run(C.use_template(cb, fsm))
        self.assertEqual(self.sent, [])
        self.assertTrue(cb.alerts)

    def test_a_vanished_photo_is_announced(self):
        """Панель товар без картинки не принимает, а путь в образце
        остался: каталог данных на Railway стирается при редеплое."""
        storage.note_ad_made(self.UID,
                             self.made(photo_path="/нет/такого.jpg"), {"f": 1})
        cb, fsm = CB("create_ad:use_template:0"), FSM()
        run(C.use_template(cb, fsm))
        self.assertTrue(any("Фото" in a for a in cb.alerts), cb.alerts)
        self.assertIsNone(self.sent[0]["values"]["photo_path"])


class TheCopyIsForAdminsOnly(Bench):

    def setUp(self):
        super().setUp()
        # Проверка НАСТОЯЩАЯ: подменённая заглушкой, она не вызывалась бы
        # вовсе, и правка в ней ничего бы не ломала — мутация это и нашла.
        features.ad_templates_shown = self._shown
        self._admin = storage.is_admin
        storage.is_admin = lambda uid: False
        storage.note_ad_made(self.UID, self.made(), {"filter__8": 3})

    def tearDown(self):
        storage.is_admin = self._admin
        super().tearDown()

    def test_the_check_is_the_real_one(self):
        """Без этого весь набор ниже проверял бы заглушку."""
        self.assertFalse(features.ad_templates_shown(self.UID))
        storage.is_admin = lambda uid: True
        self.assertTrue(features.ad_templates_shown(self.UID))

    def test_the_seller_gets_no_fork(self):
        """Кнопка, отвечающая отказом, хуже, чем её отсутствие."""
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        data = [b.callback_data for row in cb.message.kbs[-1].inline_keyboard
                for b in row]
        self.assertNotIn("create_ad:templates_list", data)
        self.assertEqual(fsm.state, C.CreateAdState.title,
                         "мастер стал недоступен вместе с копией")

    def test_the_list_is_closed_from_an_old_message(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [], "показал список постороннему")

    def test_and_so_is_the_copy_itself(self):
        """Нажатие создаёт настоящий товар на витрине — заслон обязателен
        и здесь, а не только на списке."""
        sent = []
        create = C._panel_create_and_report

        async def fake(*a, **kw):
            sent.append(a)

        C._panel_create_and_report = fake
        try:
            cb, fsm = CB("create_ad:use_template:0"), FSM()
            run(C.use_template(cb, fsm))
        finally:
            C._panel_create_and_report = create
        self.assertEqual(sent, [], "посторонний создал товар копией")


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
