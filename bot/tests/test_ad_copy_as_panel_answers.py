"""Копия против панели, отвечающей ТАК ЖЕ, как ответила живая.

Всё здесь снято с живого `/copy_debug` 07.09, а не придумано:

* форма правки отдаёт девять полей — раздела, подраздела, типа и цены в
  ней нет вовсе;
* форма создания — раздел `items`, двадцать два поля: `title, category,
  subcategory, type, price, content, images, filter__1…filter__11,
  has_chat, created_order, wait_order, confirmed_order`;
* `category` отдаёт **825** вариантов и все они — названия игр;
* `subcategory` без выбранного раздела отдаёт **0**;
* `type` отдаёт **4**.

Проверяется одно: копия не спрашивает НИЧЕГО и уходит в панель с теми же
номерами, что у образца. Ради этого всё и делалось — продавец трижды
спрашивал, почему он должен заполнять раздел у товара, с которого копируют.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote_plus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P                          # noqa: E402
from handlers import create_ad as C                        # noqa: E402

ITEM = "250614"

# Название живого товара дословно. Игры в нём нет: слова-подсказки из него —
# «Аккаунт» и «Баланс», и по ним раздел-игру не найти. Отсюда и берётся
# цепочка разделов маркетплейса.
TITLE = "💖Аккаунт 💖Баланс: 3.000.000 ₽⚡4 LVL 💖"

# Девять полей формы правки — ровно те, что назвала живая панель.
EDIT_FIELDS = [
    {"attribute": "id", "value": int(ITEM)},
    {"attribute": "public", "value": 1},
    {"attribute": "moderation_status", "value": "approved"},
    {"attribute": "title", "value": TITLE},
    {"attribute": "content", "value": "Аккаунт с внутриигровой валютой."},
    {"attribute": "quantity", "value": 3},
    {"attribute": "images", "component": "advanced-media-library-field",
     "value": [{"original_url": "http://ЗАМЕНА/media/photo.jpg"}]},
    {"attribute": "created_at", "value": "2026-08-01 10:00:00"},
    {"attribute": "slug", "value": "akkaunt-standoff-2"},
]

# Карточка: раздел, подраздел, тип и атрибуты раздела. Цена на ней уже
# оформлена — числа из такой строки не достать, и берётся она у маркетплейса.
CARD_FIELDS = [
    {"attribute": "id", "value": int(ITEM)},
    {"attribute": "price", "value": "1 490 ₽"},
    {"attribute": "category", "value": {"display": "Standoff 2"},
     "belongsToId": 613, "component": "belongs-to-field"},
    {"attribute": "subcategory", "value": {"display": "Аккаунты"},
     "belongsToId": 3, "component": "belongs-to-field"},
    {"attribute": "type", "value": {"display": "Мгновенная выдача"},
     "belongsToId": 1, "component": "belongs-to-field"},
    {"attribute": "filter__8", "value": "Россия"},
]

# Двадцать два поля формы создания — перечень живой панели дословно.
CREATION_FIELDS = (
    [{"attribute": "title", "rules": ["required", "max:120"]},
     {"attribute": "category", "rules": ["required"]},
     {"attribute": "subcategory", "rules": ["required"]},
     {"attribute": "type", "rules": ["required"]},
     {"attribute": "price", "rules": ["required"]},
     {"attribute": "content", "rules": []},
     {"attribute": "images", "rules": []}]
    + [{"attribute": f"filter__{i}", "rules": []} for i in range(1, 12)]
    + [{"attribute": "has_chat", "rules": ["required"], "value": 0,
        "options": {"0": "Нет", "1": "Да"}},
       {"attribute": "created_order", "rules": [], "value": ""},
       {"attribute": "wait_order", "rules": [], "value": ""},
       {"attribute": "confirmed_order", "rules": [], "value": ""}]
)

# 825 игр — столько отдаёт живая панель. «Standoff 2» стоит на 700-м
# месте, то есть за любым разумным обрезком: на экране продавца список
# начинался с «Dying Light, 7 Days to Die, 8 Ball Pool…», и нужного там не
# было. Обрезать список ДО сверки — это и есть та ошибка.
_GAMES = [f"Игра {i:04d}" for i in range(824)]
_GAMES.insert(700, "Standoff 2")
CATEGORY_OPTIONS = [{"value": 613 if g == "Standoff 2" else 1000 + i,
                     "display": g} for i, g in enumerate(_GAMES)]
SUBCATEGORY_OPTIONS = [{"value": 3, "display": "Аккаунты"},
                       {"value": 4, "display": "Ключи"},
                       {"value": 5, "display": "Валюта"}]
TYPE_OPTIONS = [{"value": 1, "display": "Мгновенная выдача"},
                {"value": 2, "display": "Ручная выдача"},
                {"value": 3, "display": "По запросу"},
                {"value": 4, "display": "Предзаказ"}]

_DETAIL = re.compile(r"^/nova-api/[\w-]+/\d+$")


class LiveNova(BaseHTTPRequestHandler):
    posted: list = []
    card_open: bool = True
    list_open: bool = True
    # Показывает ли панель раздел хоть где-нибудь. Живой ответ 07.09 по
    # товару 250614: НЕ показывает — ни в форме правки (7 полей), ни на
    # карточке (11), ни в строке списка (6). Раздел задаётся один раз при
    # создании, и больше его в панели не видно.
    section_visible: bool = True
    refuse: bool = False

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _param(self, name: str) -> str:
        for part in self.path.split("?")[-1].split("&"):
            if part.startswith(name + "="):
                return unquote_plus(part[len(name) + 1:])
        return ""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/sanctum/csrf-cookie"):
            self.send_response(204)
            self.send_header("Set-Cookie", "XSRF-TOKEN=t; Path=/")
            self.end_headers()
            return
        if path.endswith("/update-fields"):
            self._json(200, {"fields": EDIT_FIELDS})
            return
        if path.endswith("/creation-fields"):
            if path == "/nova-api/items/creation-fields":
                self._json(200, {"fields": CREATION_FIELDS})
            else:
                self._json(404, {"message": "нет такого раздела"})
            return
        if "/associatable/" in path:
            attr = path.split("/associatable/")[1]
            self._json(200, {"resources": self._options(attr)})
            return
        if _DETAIL.match(path):
            if not LiveNova.card_open:
                self._json(403, {"message": "нет доступа"})
                return
            self._json(200, {"resource": {"fields": self._card()}})
            return
        if path == "/nova-api/items":
            if not LiveNova.list_open:
                self._json(403, {"message": "нет доступа"})
                return
            self._json(200, {"resources": [
                {"id": {"value": int(ITEM)}, "fields": self._card()}]})
            return
        if path.startswith("/media/"):
            body = b"\xff\xd8\xff\xe0JPEG"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"message": "нет такого"})

    def _card(self) -> list:
        if LiveNova.section_visible:
            return CARD_FIELDS
        # Карточка живого товара: ни раздела, ни подраздела, ни типа —
        # только показные поля с русскими именами и связь на единицы.
        return [{"attribute": "id", "value": int(ITEM)},
                {"attribute": "изображение", "value": None},
                {"attribute": "заголовок", "value": "Аккаунт"},
                {"attribute": "ComputedField", "value": "—"},
                {"attribute": "статус", "value": "Опубликован"},
                {"attribute": "items", "value": None}]

    def _options(self, attr: str) -> list:
        search = self._param("search").lower()
        if attr == "category":
            rows = CATEGORY_OPTIONS
        elif attr == "subcategory":
            # Без выбранного раздела панель не отдаёт ничего — живой ответ
            # «subcategory: без поиска 0 вариантов».
            rows = SUBCATEGORY_OPTIONS if self._param("category") else []
        elif attr == "type":
            rows = TYPE_OPTIONS
        else:
            rows = []
        if search:
            return [r for r in rows if search in str(r["display"]).lower()]
        return rows

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        LiveNova.posted.append({"path": self.path,
                                "ctype": self.headers.get("Content-Type") or "",
                                "raw": self.rfile.read(n) if n else b""})
        if LiveNova.refuse:
            self._json(422, {"errors": {"content": ["Ссылки запрещены."]}})
            return
        self._json(201, {"resource": {"id": 900002}})


def _body(row: dict) -> dict:
    """Разобрать отправленное — и multipart, и JSON."""
    raw, ctype = row["raw"], row["ctype"]
    if "multipart" not in ctype:
        try:
            return json.loads(raw.decode())
        except Exception:
            return {}
    boundary = ctype.split("boundary=")[-1].strip().encode()
    out: dict = {}
    for part in raw.split(b"--" + boundary):
        if b'name="' not in part:
            continue
        name = part.split(b'name="')[1].split(b'"')[0].decode()
        value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0] \
            if b"\r\n\r\n" in part else b""
        if b"filename=" in part.split(b"\r\n")[0] or name.startswith("__media__"):
            out[name] = f"<файл {len(value)} байт>"
        else:
            out[name] = value.decode("utf-8", "replace")
    return out


class Msg:
    def __init__(self):
        self.texts: list[str] = []
        self.kbs: list = []
        self.chat = type("C", (), {"id": 1})()
        self.message_id = 1

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.kbs.append(reply_markup)
        return self


class CB:
    def __init__(self, data):
        self.data, self.message = data, Msg()
        self.from_user = type("U", (), {"id": 7})()
        self.alerts: list = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


class FSM:
    def __init__(self):
        self.data, self.state = {}, None

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
    """Маркетплейс: список, цена и остаток. Название раздела он отдаёт своё,
    и совпадать с надписью панели оно не обязано."""

    section = "Аккаунты"

    async def get_ads(self, cursor=None):
        return {"data": [{"id": int(ITEM), "category_id": 5221,
                          "title": TITLE,
                          "price": {"amount": 1490}}]}

    # Вид товара: от него зависит, что бот может с остатком сделать сам.
    kind: str = ""
    value_block: dict = {}
    items_left: list = []
    updated: list = []

    async def get_ad(self, ad_id):
        return {"data": {"id": ad_id, "stock": 3, "category_id": 5221,
                         "type": Api.kind,
                         "price": {"amount": 1490, "currency": "RUB"}}}

    async def get_ad_value(self, ad_id):
        return {"data": dict(Api.value_block)}

    async def update_ad_value(self, ad_id, **fields):
        Api.updated.append(fields)

    async def get_ad_items(self, ad_id, cursor=None):
        return {"data": list(Api.items_left)}

    # Дерево разделов маркетплейса. Товар лежит в ЛИСТЕ («Аккаунты»), а
    # панель раскладывает по играм («Standoff 2»): нужное слово стоит на
    # среднем уровне, и одним именем листа его не достать.
    path: list = ["Игры", "Standoff 2", "Аккаунты"]

    async def resolve_category(self, cid):
        return Api.section

    async def category_path(self, cid, max_requests=80):
        return list(Api.path)

    async def get_categories(self, **kw):
        return [{"id": 5221, "name": Api.section}]

    # Остаток: у формы создания панели поля количества НЕТ вовсе (живой
    # перечень 07.09), поэтому он проставляется маркетплейсом после
    # создания. Здесь считается, сколько раз и на сколько его пополняли.
    stock: int = 0
    refills: list = []

    async def ad_stock(self, ad_id, ad=None):
        return True, f"Остаток: {Api.stock}"

    async def refill_ad_value(self, ad_id, count):
        Api.refills.append((str(ad_id), count))
        Api.stock += count
        return {"data": {"stock": Api.stock}}


class Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), LiveNova)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls._url = P.PANEL_URL
        P.PANEL_URL = cls.base
        for f in EDIT_FIELDS:
            if f["attribute"] == "images":
                f["value"] = [{"original_url": f"{cls.base}/media/photo.jpg"}]

    @classmethod
    def tearDownClass(cls):
        P.PANEL_URL = cls._url
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        import features
        import storage
        self.storage, self.features = storage, features
        self._creds, self._dir = storage.get_panel_creds, storage._DATA_DIR
        self._shown = features.ad_templates_shown
        self.tmp = tempfile.TemporaryDirectory()
        # Настройки продавца — свои на каждый тест: запомненный раздел
        # иначе утёк бы в соседний и проверял бы не то.
        self.marks: dict = {}
        self._get_marks = storage.get_copy_marks
        self._set_marks = storage.remember_copy_marks
        self._del_marks = storage.forget_copy_marks
        storage.get_copy_marks = lambda uid, ad: dict(
            self.marks.get(str(ad)) or {})
        storage.remember_copy_marks = lambda uid, ad, values, labels=None: (
            self.marks.__setitem__(str(ad), {"values": dict(values),
                                             "labels": dict(labels or {})}))
        storage.forget_copy_marks = lambda uid, ad: (
            self.marks.pop(str(ad), None) is not None)
        storage.get_panel_creds = lambda uid: {"cookies": "session=1"}
        storage._DATA_DIR = self.tmp.name
        features.ad_templates_shown = lambda uid: True
        LiveNova.posted = []
        LiveNova.card_open = True
        LiveNova.list_open = True
        LiveNova.section_visible = True
        LiveNova.refuse = False
        Api.section = "Аккаунты"
        Api.stock, Api.refills = 0, []
        Api.path = ["Игры", "Standoff 2", "Аккаунты"]
        Api.kind, Api.value_block, Api.items_left, Api.updated = "", {}, [], []

    def tearDown(self):
        self.storage.get_copy_marks = self._get_marks
        self.storage.remember_copy_marks = self._set_marks
        self.storage.forget_copy_marks = self._del_marks
        self.storage.get_panel_creds = self._creds
        self.storage._DATA_DIR = self._dir
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def press(self):
        fsm, api = FSM(), Api()
        asyncio.run(C.templates_list(CB("create_ad:templates_list"), fsm, api))
        cb = CB("create_ad:copy:0:0")
        asyncio.run(C.copy_item(cb, fsm, api))
        # Копия могла остановиться на вопросе — тогда создания нет вовсе, и
        # проверять надо ВЫБРАННОЕ, а не отправленное.
        self.fsm = fsm
        self.chosen = dict(fsm.data.get("chosen") or {})
        return cb

    def created(self) -> dict:
        for row in reversed(LiveNova.posted):
            if "/nova-api/items?" in row["path"] and "editMode=create" in row["path"]:
                return _body(row)
        return {}


class TheCopyAsksNothingWhenTheSampleHasTheAnswers(Bench):
    """Ради этого всё и делалось."""

    def test_not_a_single_question(self):
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(asked, [], asked)

    def test_and_the_seller_is_told_it_worked(self):
        cb = self.press()
        self.assertIn("создан", cb.message.texts[-1].lower(),
                      cb.message.texts[-1])

    def test_the_section_reaches_the_panel_by_number(self):
        self.press()
        body = self.created()
        self.assertEqual(str(body.get("category")), "613", body)
        self.assertEqual(str(body.get("subcategory")), "3", body)
        self.assertEqual(str(body.get("type")), "1", body)

    def test_the_rest_of_the_item_travels_too(self):
        self.press()
        body = self.created()
        self.assertEqual(body.get("title"), TITLE)
        self.assertEqual(str(body.get("price")), "1490")
        self.assertEqual(body.get("filter__8"), "Россия")
        self.assertIn("файл", str(body.get("__media__[images][0]")))

    def test_the_stock_goes_through_the_marketplace_not_the_panel(self):
        """У формы создания панели поля количества НЕТ вовсе — живой
        перечень её полей 07.09. Остаток поэтому проставляется после
        создания, через маркетплейс, и молчать об этом нельзя: без остатка
        товар не публикуется."""
        cb = self.press()
        self.assertNotIn("quantity", self.created())
        self.assertEqual(Api.refills, [("900002", 3)])
        self.assertIn("Остаток проставлен: 3", cb.message.texts[-1])

    def test_nothing_of_the_source_item_goes_out(self):
        self.press()
        body = self.created()
        self.assertNotIn("id", body)
        self.assertNotIn("public", body)
        self.assertNotIn("moderation_status", body)

    def test_a_field_the_item_never_has_goes_by_the_forms_default(self):
        """`has_chat` обязателен и списком, а у товара его нет вовсе."""
        self.press()
        self.assertEqual(str(self.created().get("has_chat")), "0")

    def test_the_seller_sees_what_the_bot_filled_in(self):
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("Заполнено ботом", said)
        self.assertIn("раздел: Standoff 2 (613)", said,
                      "и надпись, и номер: спор закрывают числа")


class TheSectionIsFoundEvenWithoutTheCard(Bench):
    """Карточка может быть закрыта — Nova разрешает её отдельно от списка."""

    def test_the_card_carries_it_when_the_list_is_closed(self):
        """И наоборот: список бывает закрыт, а карточка открыта. Источника
        два не для красоты — Nova разрешает их независимо."""
        LiveNova.list_open = False
        cb = self.press()
        self.assertEqual([t for t in cb.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")

    def test_the_list_row_carries_the_section(self):
        LiveNova.card_open = False
        cb = self.press()
        self.assertEqual([t for t in cb.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")


class ThePanelShowsNoSectionAtAllAndItStillWorks(Bench):
    """Живой ответ 07.09 по товару 250614: раздела в панели нет НИГДЕ.

    Форма правки — семь полей (title, content, images и тексты сообщений),
    карточка — одиннадцать показных с русскими именами, строка списка —
    шесть. Ни `category`, ни `subcategory`, ни `type`, ни `filter__N`:
    панель задаёт их один раз при создании и больше не показывает.

    Значит источник один — маркетплейс. И берётся из него не имя листа, а
    ЦЕПОЧКА: товар лежит в «Аккаунтах», а панель раскладывает по играм, и
    нужное слово стоит на среднем уровне дерева.
    """

    def setUp(self):
        super().setUp()
        LiveNova.section_visible = False

    def test_the_section_comes_from_the_marketplace_tree(self):
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(len(asked), 1, asked)
        self.assertIn("type", asked[0], "спрошен должен быть только тип")
        self.assertEqual(self.chosen.get("category"), 613, self.chosen)
        self.assertEqual(self.chosen.get("subcategory"), 3, self.chosen)

    def test_the_leaf_name_alone_would_not_have_been_enough(self):
        """Раздел в панели — игра, а лист дерева — «Аккаунты». Версия,
        читавшая только лист, искала «Аккаунты» среди 825 игр — и, конечно,
        не находила: в названии товара игры тоже нет."""
        Api.path = ["Аккаунты"]
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertTrue(any("category" in t for t in asked), asked)


class AnAnswerGivenOnceIsNotAskedAgain(Bench):
    """Раздела у товара в панели нет нигде, и второй раз узнать его
    неоткуда. Значит спрошенное однажды надо помнить за образцом — иначе
    один и тот же вопрос повторяется при каждой копии одного товара.

    Запоминается только после того, как панель товар ПРИНЯЛА: отказ
    означал бы, что значения не подошли, а запомненная неправда хуже
    вопроса — раздел после создания не меняется.
    """

    def setUp(self):
        super().setUp()
        LiveNova.section_visible = False
        Api.path = []                      # раздел взять неоткуда вовсе

    def answer_the_questions(self, cb):
        """Ответить на все вопросы так, как ответил бы продавец.

        Их бывает несколько подряд: ответ на раздел открывает подраздел.
        Остановиться на первом значит не дойти до создания — а запоминается
        выбранное только после того, как панель товар приняла.
        """
        fsm = self.fsm
        for _ in range(6):
            options = fsm.data.get("current_view") or []
            if not fsm.data.get("current_attr") or not options:
                break
            want = next((i for i, o in enumerate(options)
                         if o["label"] in ("Standoff 2", "Аккаунты",
                                           "Мгновенная выдача")), 0)
            pick = CB(f"cadopt:{want}")
            pick.message = cb.message
            asyncio.run(C.choose_select_option(pick, fsm, Api()))
        return fsm

    def test_the_first_copy_asks_and_the_answer_is_remembered(self):
        cb = self.press()
        self.assertIn("category", [t for t in cb.message.texts
                                   if "Выбери" in t][0])
        self.answer_the_questions(cb)
        self.assertEqual(self.marks.get(ITEM, {}).get("values", {}).get(
            "category"), 613, self.marks)

    def test_and_the_name_is_remembered_with_the_number(self):
        """Номер 613 продавцу не говорит ничего, «Standoff 2» — всё."""
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertEqual(self.marks[ITEM]["labels"].get("category"),
                         "Standoff 2")

    def test_a_refusal_shows_the_names_it_tried(self):
        """Продавец решает по отказу, туда ли шёл товар. Форма к этому
        моменту уже закрыта, и надписи надо снять раньше — иначе на экране
        голые номера."""
        LiveNova.refuse = True
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertIn("Standoff 2", cb.message.texts[-1])

    def test_a_refusal_remembers_nothing(self):
        """Значения не подошли — запомненная неправда хуже вопроса."""
        was, LiveNova.refuse = getattr(LiveNova, "refuse", False), True
        try:
            cb = self.press()
            self.answer_the_questions(cb)
            self.assertEqual(self.marks, {}, self.marks)
        finally:
            LiveNova.refuse = was

    def test_the_second_copy_does_not_ask_the_same_thing(self):
        """Ради этого всё и делалось."""
        self.marks[ITEM] = {"values": {"category": 613, "subcategory": 3,
                                       "type": 1},
                            "labels": {"category": "Standoff 2"}}
        cb = self.press()
        self.assertEqual([t for t in cb.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")

    def test_the_report_names_the_section_not_its_number(self):
        self.marks[ITEM] = {"values": {"category": 613, "subcategory": 3,
                                       "type": 1},
                            "labels": {"category": "Standoff 2"}}
        cb = self.press()
        self.assertIn("раздел: Standoff 2", cb.message.texts[-1])

    def test_the_seller_can_take_the_answer_back(self):
        """Ошибиться разделом можно один раз: панель менять его не даёт."""
        self.marks[ITEM] = {"values": {"category": 613}, "labels": {}}
        cb = CB(f"create_ad:forget:{ITEM}")
        asyncio.run(C.forget_marks(cb))
        self.assertEqual(self.marks, {})
        self.assertTrue(cb.alerts)

    def test_what_the_panel_shows_beats_what_we_remember(self):
        """Прочитанное у панели свежее запомненного.

        Запомненный номер взят НАСТОЯЩИЙ, из того же списка: иначе сверка
        отвергла бы его сама, и проверка проходила бы при любом порядке —
        то есть не проверяла бы ничего."""
        LiveNova.section_visible = True
        other = CATEGORY_OPTIONS[0]
        self.assertNotEqual(other["value"], 613)
        self.marks[ITEM] = {"values": {"category": other["value"]},
                            "labels": {"category": other["display"]}}
        self.press()
        self.assertEqual(str(self.created().get("category")), "613")


class TheStockIsPutInByTheBotAsFarAsItHonestlyCan(Bench):
    """Что бот может сделать с остатком сам, зависит от вида товара.

    У авто-выбора остаток — число, и настройки выдачи: их бот переносит
    целиком. У авто-выдачи остаток — сами коды или аккаунты: скопировать их
    с образца значит продать одно и то же дважды, а выдумать — положить на
    витрину пустышку. Там бот честно говорит, сколько нужно, и принимает их
    одним сообщением.
    """

    def keyboard_texts(self, cb) -> list:
        kb = next((k for k in reversed(cb.message.kbs) if k), None)
        return [b.text for row in (kb.inline_keyboard if kb else [])
                for b in row]

    def test_an_auto_value_copy_gets_its_settings_and_its_stock(self):
        Api.kind = "auto-value"
        Api.value_block = {"min": 10, "max": 900, "step": 5, "label_id": 3,
                           "stock": 7}
        cb = self.press()
        self.assertEqual(Api.updated,
                         [{"min": 10, "max": 900, "step": 5, "label_id": 3}])
        self.assertIn("Настройки выдачи перенесены", cb.message.texts[-1])
        self.assertIn("Остаток проставлен", cb.message.texts[-1])

    def test_codes_are_not_copied_and_the_button_appears(self):
        Api.kind = "auto-delivery"
        Api.items_left = [{"status": "available"}, {"status": "available"}]
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("одноразовые", said)
        self.assertIn("2 шт.", said, "сколько нужно — числом")
        self.assertEqual(Api.refills, [], "коды нельзя копировать")
        self.assertIn("📦 Прислать остатки", self.keyboard_texts(cb))

    def test_an_unlimited_copy_is_not_nagged_about_stock(self):
        Api.kind = "unlimited"
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("безлимит", said.lower())
        self.assertNotIn("📦 Прислать остатки", self.keyboard_texts(cb))


class WithNoSourceAtAllItAsksOnlyWhatItCannotKnow(Bench):
    """Панель закрыла и карточку, и список, а маркетплейс не назвал раздел.

    Тогда остаются слова названия товара — а в нём игры нет. Копия
    спрашивает, и это честный вопрос: выдумывать раздел она не должна.
    Важно другое — она не отказывается копировать, как делала версия 03.09.
    """

    def setUp(self):
        super().setUp()
        LiveNova.card_open = False
        LiveNova.list_open = False
        Api.path = []

    def test_it_asks_instead_of_inventing(self):
        cb = self.press()
        self.assertTrue([t for t in cb.message.texts if "Выбери" in t])

    def test_but_it_is_not_a_dead_end(self):
        """Прежняя версия на этом месте отказывалась копировать вовсе."""
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertNotIn("не создалась", said)
        self.assertNotIn("прочитать не вышло", said)


if __name__ == "__main__":
    unittest.main()
