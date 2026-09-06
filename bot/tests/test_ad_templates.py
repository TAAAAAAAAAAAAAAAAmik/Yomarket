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


class Session:
    """Сессия, отдающая картинку по адресу из карточки объявления."""

    def __init__(self, status=200, body=b"JPEG"):
        self.status, self.body = status, body
        self.asked: list[str] = []

    def get(self, url, **kw):
        self.asked.append(url)
        outer = self

        class Resp:
            status = outer.status

            async def read(self):
                return outer.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return Resp()


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
        # Картинку маркетплейс требует полем `files`, и без неё объявление
        # не примет: подставная сессия отдаёт её так же, как настоящая.
        self.session = Session()

    async def get_ads(self, cursor=None):
        if self.list_raises:
            raise self.list_raises
        return {"data": self.ads}

    async def resolve_category(self, cid):
        return {512: "Telegram Звёзды", 7: "Гифт-карты"}.get(int(cid), "")

    async def get_categories(self, max_pages=40, parent_id=None):
        return [{"id": 512, "name": "Telegram Звёзды"},
                {"id": 7, "name": "Гифт-карты"}]

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


class TheListIsGroupedBySection(Bench):
    """Плоским списком это не читается: объявлений бывает полсотни, а
    копируют обычно соседнее по разделу. И прежняя версия резала список на
    двенадцати МОЛЧА — то есть половины товаров для копии просто не было.

    Разделы считаются тем же кодом, что и на экране «📦 Товары»: второй
    разбор того же ответа однажды разошёлся бы с первым, и один товар лежал
    бы на двух экранах в разных разделах."""

    def sections(self, cb=None):
        cb = cb or CB("create_ad:templates_list")
        fsm = FSM()
        run(C.templates_list(cb, fsm, self.api))
        return cb, fsm

    def test_each_section_gets_a_button_with_a_count(self):
        self.api.ads = [
            dict(self.api.CARD, id=11, category_id=512),
            dict(self.api.CARD, id=12, category_id=512),
            dict(self.api.CARD, id=13, category_id=7),
        ]
        cb, _fsm = self.sections()
        said = cb.message.texts[-1]
        self.assertIn("Telegram Звёзды", said)
        self.assertIn("Гифт-карты", said)
        self.assertIn("(2)", " ".join(
            b.text for row in cb.message.kbs[-1].inline_keyboard for b in row))

    def test_a_single_section_is_not_a_choice_of_one(self):
        """Меню из одного пункта — лишний тап перед единственным
        действием."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        cb, _fsm = self.sections()
        data = self.kb(cb)
        self.assertTrue(any(d.startswith("create_ad:copy:") for d in data),
                        data)

    def test_opening_a_section_lists_its_ads(self):
        self.api.ads = [
            dict(self.api.CARD, id=11, title="Звёзды 100", category_id=512),
            dict(self.api.CARD, id=13, title="Apple 10", category_id=7),
        ]
        cb, fsm = self.sections()
        cb2 = CB("create_ad:sect:0")
        run(C.open_section(cb2, fsm))
        said = cb2.message.texts[-1]
        self.assertIn("Звёзды 100", said)
        self.assertNotIn("Apple 10", said, "показал чужой раздел")

    def test_a_section_screen_leads_back_to_the_sections(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512),
                        dict(self.api.CARD, id=13, category_id=7)]
        cb, fsm = self.sections()
        cb2 = CB("create_ad:sect:0")
        run(C.open_section(cb2, fsm))
        self.assertIn("create_ad:templates_list", self.kb(cb2))

    def test_a_long_section_says_it_was_trimmed(self):
        """Молчаливое обрезание означает, что половины товаров для копии
        просто нет, и понять это неоткуда."""
        self.api.ads = [dict(self.api.CARD, id=i, title=f"Товар {i}",
                             category_id=512) for i in range(30)]
        cb, fsm = self.sections()
        cb2 = CB("create_ad:sect:0")
        run(C.open_section(cb2, fsm))
        said = cb2.message.texts[-1]
        self.assertIn(f"из {len(self.api.ads)}", said)
        copies = [d for d in self.kb(cb2) if d.startswith("create_ad:copy:")]
        self.assertEqual(len(copies), C._COPY_LIMIT)

    def test_the_price_object_is_read_properly(self):
        """Маркетплейс отдаёт цену объектом. Прочитанная как скаляр, она
        превращается в ноль — и «990 ₽» стало бы «0 ₽»."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        cb, fsm = self.sections()
        self.assertIn("990", cb.message.texts[-1])

    def test_an_empty_showcase_says_so_and_offers_the_wizard(self):
        self.api.ads = []
        cb, _fsm = self.sections()
        self.assertIn("нечего", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_a_refusal_is_readable(self):
        self.api.list_raises = RuntimeError("HTTP 401: token expired")
        cb, _fsm = self.sections()
        self.assertIn("401", cb.message.texts[-1])
        self.assertIn("create_ad:new", self.kb(cb))

    def test_without_a_token_it_does_not_pretend(self):
        cb = CB("create_ad:templates_list")
        run(C.templates_list(cb, FSM(), None))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [])

    def test_an_ad_without_an_id_is_skipped(self):
        """Кнопка без номера объявления скопировала бы неизвестно что."""
        self.api.ads = [{"title": "Без номера", "price": 1, "category_id": 512}]
        cb, _fsm = self.sections()
        self.assertIn("нечего", cb.message.texts[-1])


class TheAdIsCreatedAgainThroughTheApi(Bench):

    def tap(self, where="create_ad:copy:0:0"):
        """Пройти путь целиком: разделы → объявления → копия."""
        fsm = FSM()
        run(C.templates_list(CB("create_ad:templates_list"), fsm, self.api))
        cb = CB(where)
        run(C.copy_item(cb, fsm, self.api))
        return cb

    def test_the_same_fields_go_out(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.tap()
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
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.card = dict(self.api.CARD, category_id=777)
        self.tap()
        self.assertEqual(self.api.created[0]["category_id"], 777)

    def test_the_stock_comes_along(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.tap()
        self.assertEqual(self.api.created[0]["stock"], 4)

    def test_the_photo_comes_along(self):
        """Маркетплейс требует картинку полем `files` и без неё отказывает
        (живой отказ 02.09)."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.tap()
        self.assertTrue(self.api.created[0]["photos"], "ушло без картинки")
        self.assertTrue(self.api.session.asked, "картинку даже не скачивали")

    def test_without_a_picture_it_does_not_even_try(self):
        """Отправлять заведомо отвергаемое значит показать продавцу отказ
        вместо понятной причины."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.card = {"id": 11, "title": "Без фото", "price": 10,
                         "category_id": 512, "type": "simple"}
        cb = self.tap()
        self.assertEqual(self.api.created, [])
        self.assertIn("картинк", cb.message.texts[-1])

    def test_it_is_not_published_by_itself(self):
        """Публикация без остатка отвергается, а остаток у копии свой."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.tap()
        self.assertFalse(self.api.created[0]["publish"])

    def test_codes_are_never_copied_and_it_says_so(self):
        """Остаток товара с авто-выдачей — сами ключи, одноразовые. Взять
        их из образца значит продать один код дважды."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.card = dict(self.api.CARD, type="auto-delivery")
        cb = self.tap()
        said = cb.message.texts[-1]
        self.assertIn("одноразов", said)
        self.assertIn("Не скопировалось", said)

    def test_a_missing_section_refuses_and_names_it(self):
        """«Не вышло» без причины продавцу разбирать нечем."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.card = {"id": 11, "title": "Есть", "price": 10}
        cb = self.tap()
        self.assertEqual(self.api.created, [])
        self.assertIn("раздел", cb.message.texts[-1])

    def test_the_new_number_is_reported(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        cb = self.tap()
        said = cb.message.texts[-1]
        self.assertIn("55", said)
        self.assertIn("Копия создана", said)

    def test_the_stock_and_moderation_are_offered_right_there(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        cb = self.tap()
        data = self.kb(cb)
        self.assertIn("pitem_stock:55", data)
        self.assertIn("cadpub:55", data)

    def test_a_refusal_is_shown_in_words_not_in_escape_codes(self):
        """Живой отказ уехал на экран как `\\u041f\\u043e\\u043b\\u0435` —
        прочитать это нельзя, то есть отказ есть, а причины нет."""
        import json
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.create_raises = RuntimeError("HTTP 422: " + json.dumps(
            {"error": {"status": 422, "message": "The given data was invalid.",
                       "errors": {"files": ["Поле files обязательно."]}}}))
        cb = self.tap()
        said = cb.message.texts[-1]
        self.assertIn("files", said)
        self.assertNotIn("u041f", said)

    def test_a_refusal_is_never_called_a_success(self):
        """Бодрый отчёт об успехе там, где ничего не создалось, — самая
        дорогая поломка этого проекта."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.create_raises = RuntimeError("422: нет раздела")
        cb = self.tap()
        said = cb.message.texts[-1]
        self.assertIn("не создалась", said)
        self.assertNotIn("Копия создана", said)

    def test_a_refusal_offers_no_buttons_for_an_ad_that_does_not_exist(self):
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        self.api.create_raises = RuntimeError("422")
        cb = self.tap()
        data = " ".join(self.kb(cb))
        self.assertNotIn("pitem_stock:", data)
        self.assertNotIn("cadpub:", data)

    def test_a_stale_button_does_not_copy_the_wrong_ad(self):
        """Кнопка осталась в старом сообщении, а список с тех пор другой.
        Скопировать «что-то похожее по счёту» хуже, чем не скопировать."""
        self.api.ads = [dict(self.api.CARD, id=11, category_id=512)]
        cb = self.tap("create_ad:copy:9:9")
        self.assertEqual(self.api.created, [])
        self.assertTrue(cb.alerts)

    def test_without_a_token_it_says_so_and_does_not_try(self):
        cb = CB("create_ad:copy:0:0")
        run(C.copy_item(cb, FSM(), None))
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
        run(C.templates_list(cb, FSM(), self.api))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [], "показал список постороннему")

    def test_the_sections_screen_is_closed_too(self):
        """Состояние набирается ПРИ ПРАВАХ, и только потом они снимаются:
        с пустым состоянием экран и так отвечает «список устарел», то есть
        проверка проходила бы и без всякого заслона."""
        storage.is_admin = lambda uid: True
        fsm = FSM()
        run(C.templates_list(CB("create_ad:templates_list"), fsm, self.api))
        storage.is_admin = lambda uid: False

        cb = CB("create_ad:sect:1")
        run(C.open_section(cb, fsm))
        self.assertTrue(cb.alerts)
        self.assertEqual(cb.message.texts, [], "показал раздел постороннему")

    def test_and_so_is_the_copy_itself(self):
        """Нажатие создаёт настоящее объявление — заслон обязателен и
        здесь, а не только на списке."""
        cb = CB("create_ad:copy:0:0")
        run(C.copy_item(cb, FSM(), self.api))
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
