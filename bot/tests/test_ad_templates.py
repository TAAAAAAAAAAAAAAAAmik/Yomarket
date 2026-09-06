"""Копия товара: то же объявление, заведённое заново по API.

Мастер спрашивает название, цену, описание, количество, фото — а потом
раздел панели и её `filter__N`, и вот их пять-шесть штук, причём про
половину панель до выбора категории молчит вовсе. Второй такой же товар
проходил весь этот круг заново.

Здесь два живых отказа подряд, и оба про то, ОТКУДА берутся данные.

Первая версия помнила товары, созданные самим ботом: у продавца их семь,
все заведены раньше, и развилка не появлялась вовсе — снаружи «кнопка не
работает». Вторая копировала формой панели, и панель отказала 422 на
четырёх полях сразу: её клон не умеет пересылать вложенные значения, а
раздел у товара как раз такое.

Теперь объявление заводится заново через Integration API: раздел лежит в
самом объявлении, полем `category_id`. И список читается ТЕМ ЖЕ API — у
панели свои номера товаров, и номер из её списка указал бы не туда.
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


class Api:
    """Integration API продавца: и список, и создание идут через него."""

    CARD = {"id": 11, "title": "1000 Robux", "description": "код сразу",
            "price": {"amount": 990, "currency": "RUB"},
            "category_id": 512, "type": "simple", "stock": 4,
            "images": [{"original_url": "https://ya/photo.jpg"}]}

    def __init__(self):
        self.ads = [dict(self.CARD),
                    {"id": 12, "title": "Apple 10 TRY", "price": 350,
                     "category_id": 7, "type": "simple", "stock": 1}]
        self.created: list[dict] = []
        self.card = dict(self.CARD)
        self.list_raises = None
        self.create_raises = None
        self.session = None

    async def get_ads(self, cursor=None):
        if self.list_raises:
            raise self.list_raises
        return {"data": self.ads}

    async def get_ad(self, ad_id):
        return dict(self.card, id=ad_id)

    async def create_and_publish(self, **kw):
        if self.create_raises:
            raise self.create_raises
        self.created.append(kw)
        return "55", "✅ создан"


class Bench(unittest.TestCase):
    UID = 7

    def setUp(self):
        self._token = storage.get_token
        self._shown = features.ad_templates_shown
        storage.get_token = lambda uid: "tok"
        features.ad_templates_shown = lambda uid: True
        self.api = Api()

    def tearDown(self):
        storage.get_token = self._token
        features.ad_templates_shown = self._shown

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

    def test_without_a_token_there_is_no_fork(self):
        """И список, и создание идут по API. Кнопка, за которой «подключи
        магазин», обещает то, чего за ней нет."""
        storage.get_token = lambda uid: ""
        cb, fsm = CB("create_ad:start"), FSM()
        run(C.create_ad_start(cb, fsm))
        self.assertNotIn("create_ad:templates_list", self.kb(cb))
        self.assertEqual(fsm.state, C.CreateAdState.title,
                         "мастер стал недоступен вместе с копией")

    def test_the_other_way_still_asks_the_title(self):
        cb, fsm = CB("create_ad:new"), FSM()
        run(C.create_ad_new(cb, fsm))
        self.assertEqual(fsm.state, C.CreateAdState.title)


class TheListComesFromTheSameApiThatCreates(Bench):
    """У панели свои номера товаров. Номер из её списка, отданный в
    `GET /ads/{id}`, указал бы не туда — копия создала бы не то объявление
    или не создала бы ничего."""

    def test_every_ad_gets_a_button(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        data = self.kb(cb)
        self.assertIn("create_ad:copy:11", data)
        self.assertIn("create_ad:copy:12", data)

    def test_the_names_and_prices_are_on_the_screen(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("1000 Robux", said)
        self.assertIn("990", said)

    def test_the_price_object_is_read_properly(self):
        """Маркетплейс отдаёт цену объектом. Прочитанная как скаляр, она
        превращается в ноль — и «990 ₽» на экране стало бы «0 ₽»."""
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        self.assertNotIn("1000 Robux</b> — 0 ₽", cb.message.texts[-1])

    def test_an_empty_showcase_says_so_and_offers_the_wizard(self):
        self.api.ads = []
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        self.assertIn("нечего", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_a_refusal_is_readable(self):
        self.api.list_raises = RuntimeError("HTTP 401: token expired")
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        self.assertIn("401", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_without_a_token_it_does_not_pretend(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, None))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [])

    def test_the_list_does_not_become_a_wall_of_buttons(self):
        """Клавиатура из полусотни кнопок — это не выбор, а свалка."""
        self.api.ads = [{"id": i, "title": f"Товар {i}", "price": i,
                         "category_id": 1} for i in range(40)]
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        copies = [d for d in self.kb(cb) if d.startswith("create_ad:copy:")]
        self.assertEqual(len(copies), C._COPY_LIMIT)

    def test_an_ad_without_an_id_is_skipped(self):
        self.api.ads = [{"title": "Без номера", "price": 1}]
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, self.api))
        self.assertEqual([d for d in self.kb(cb)
                          if d.startswith("create_ad:copy:")], [])


class TheAdIsCreatedAgainThroughTheApi(Bench):

    def test_the_same_fields_go_out(self):
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertEqual(len(self.api.created), 1, "объявление не создано")
        got = self.api.created[0]
        self.assertEqual(got["title"], "1000 Robux")
        self.assertEqual(got["price"], 990)
        self.assertEqual(got["description"], "код сразу")
        self.assertEqual(got["category_id"], 512)
        self.assertEqual(got["ad_type"], "simple")

    def test_the_section_is_taken_from_the_ad_itself(self):
        """Ради этого всё и переписано: у панели раздел лежал вложенным
        значением, и её клон его не отправлял — отказ 422 по `category`."""
        self.api.card = dict(self.api.CARD, category_id=777)
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertEqual(self.api.created[0]["category_id"], 777)

    def test_the_stock_comes_along(self):
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertEqual(self.api.created[0]["stock"], 4)

    def test_it_is_not_published_by_itself(self):
        """Публикация без остатка отвергается, а остаток у копии свой."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertFalse(self.api.created[0]["publish"])

    def test_codes_are_never_copied_and_it_says_so(self):
        """Остаток товара с авто-выдачей — сами ключи, одноразовые. Взять
        их из образца значит продать один код дважды."""
        self.api.card = dict(self.api.CARD, type="auto-delivery")
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("одноразов", said)
        self.assertIn("Не скопировалось", said)

    def test_a_missing_section_refuses_and_names_it(self):
        """«Не вышло» без причины продавцу разбирать нечем."""
        self.api.card = {"id": 11, "title": "Есть", "price": 10}
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertEqual(self.api.created, [])
        self.assertIn("раздел", cb.message.texts[-1])

    def test_the_new_number_is_reported(self):
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("55", said)
        self.assertIn("Копия создана", said)

    def test_the_stock_and_moderation_are_offered_right_there(self):
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        data = self.kb(cb)
        self.assertIn("pitem_stock:55", data)
        self.assertIn("cadpub:55", data)

    def test_a_refusal_is_shown_in_words_not_in_escape_codes(self):
        """Живой отказ уехал на экран как `\\u041f\\u043e\\u043b\\u0435` —
        прочитать это нельзя, то есть отказ есть, а причины нет."""
        import json
        self.api.create_raises = RuntimeError("422: " + json.dumps(
            {"message": "Поле Категория обязательно для заполнения.",
             "errors": {"category": ["Поле Категория обязательно."]}}))
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("Категория", said)
        self.assertNotIn("u041f", said)

    def test_a_refusal_offers_no_buttons_for_an_ad_that_does_not_exist(self):
        self.api.create_raises = RuntimeError("422")
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        data = " ".join(self.kb(cb))
        self.assertNotIn("pitem_stock:", data)
        self.assertNotIn("cadpub:", data)

    def test_a_refusal_is_never_called_a_success(self):
        """Бодрый отчёт об успехе там, где ничего не создалось, — самая
        дорогая поломка этого проекта."""
        self.api.create_raises = RuntimeError("422: нет раздела")
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("не создалась", said)
        self.assertNotIn("Копия создана", said)

    def test_the_refusal_on_screen_is_readable_whatever_the_path(self):
        """Отказ печатается через разбор и на экране тоже, а не только там,
        где его поймали: путей до экрана два, и разъехаться им нельзя."""
        import json
        self.api.create_raises = RuntimeError("422: " + json.dumps(
            {"errors": {"category": ["Поле Категория обязательно."]}}))
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        said = cb.message.texts[-1]
        self.assertIn("Категория", said)
        self.assertNotIn("u041a", said)

    def test_without_a_token_it_says_so_and_does_not_try(self):
        """Отказ должен назвать причину — «токена нет», а не свалиться на
        первом же обращении к пустому клиенту."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, None))
        self.assertTrue(any("токен" in a.lower() for a in cb.alerts),
                        cb.alerts)
        self.assertEqual(cb.message.texts, [], "полез создавать без токена")
        self.assertEqual(self.api.created, [])


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
        run(C.templates_list(cb, self.api))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [], "показал список постороннему")

    def test_and_so_is_the_copy_itself(self):
        """Нажатие создаёт настоящее объявление — заслон обязателен и
        здесь, а не только на списке."""
        cb = CB("create_ad:copy:11")
        run(C.copy_item(cb, self.api))
        self.assertEqual(self.api.created, [],
                         "посторонний создал объявление копией")


class ARefusalIsReadable(unittest.TestCase):
    """Отказ маркетплейса приходит JSON-ом, и русский текст в нём —
    экранированными кодами. На экране это нечитаемо: отказ есть, а причины
    нет."""

    def test_escaped_russian_becomes_russian(self):
        import json
        raw = "422: " + json.dumps({"message": "Поле Категория обязательно."})
        self.assertIn("u041f", raw, "тест проверяет не то — текст не экранирован")
        self.assertIn("Категория", C._readable(raw))

    def test_the_field_errors_win_over_the_summary(self):
        """«(and 3 more errors)» не говорит, каких именно."""
        import json
        raw = "422: " + json.dumps({"message": "x (and 3 more errors)",
                                    "errors": {"category": ["Нужен раздел"]}})
        self.assertIn("Нужен раздел", C._readable(raw))

    def test_plain_text_survives(self):
        """Сырой текст хуже перевода, но лучше молчания."""
        self.assertIn("таймаут", C._readable("таймаут сети"))

    def test_it_never_returns_a_wall(self):
        self.assertLessEqual(len(C._readable("э" * 5000)), 400)


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
