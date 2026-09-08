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

# Описание. Игра названа ЗДЕСЬ — в заголовке её нет вовсе, и никакая
# пословная догадка её оттуда не достанет: «Black Russia» это два слова.
DESCRIPTION = ("Аккаунт Black Russia, 4 уровень, баланс 3.000.000 ₽ в банке. "
               "Вход по почте, данные меняются.")

# Девять полей формы правки — ровно те, что назвала живая панель.
EDIT_FIELDS = [
    {"attribute": "id", "value": int(ITEM)},
    {"attribute": "public", "value": 1},
    {"attribute": "moderation_status", "value": "approved"},
    {"attribute": "title", "value": TITLE},
    {"attribute": "content", "value": DESCRIPTION},
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
    {"attribute": "category", "value": {"display": "Black Russia"},
     "belongsToId": 613, "component": "belongs-to-field"},
    {"attribute": "subcategory", "value": {"display": "Аккаунты"},
     "belongsToId": 3, "component": "belongs-to-field"},
    {"attribute": "type", "value": {"display": "Авто-выдача"},
     "belongsToId": "auto-delivery", "component": "belongs-to-field"},
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
_GAMES.insert(700, "Black Russia")
CATEGORY_OPTIONS = [{"value": 613 if g == "Black Russia" else 1000 + i,
                     "display": g} for i, g in enumerate(_GAMES)]
SUBCATEGORY_OPTIONS = [{"value": 3, "display": "Аккаунты с виртами"},
                       {"value": 4, "display": "Ключи"},
                       {"value": 5, "display": "Валюта"}]
# Значения — те же слова, какими маркетплейс называет `type` у объявления:
# отчёт живого бота печатал «Авто-выдача (auto-delivery)», а карточка того
# же товара отвечает `type = auto-delivery`.
TYPE_OPTIONS = [{"value": "auto-delivery", "display": "Авто-выдача"},
                {"value": "auto-value", "display": "Авто-выбор"},
                {"value": "unlimited", "display": "Безлимитная"},
                {"value": "simple", "display": "Ограниченная выдача"}]

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
    # Отдаёт ли панель ФОРМУ ПРАВКИ. Живой отказ 08.09: `update-fields:
    # 403` у своего же товара, при живой карточке.
    form_open: bool = True
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
            if not LiveNova.form_open:
                self._json(403, {"message": "нет доступа"})
                return
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
    # Адрес картинки у маркетплейса — свой; подставляется в setUpClass,
    # когда известен порт подставной панели.
    image = ""

    async def get_ads(self, cursor=None):
        return {"data": [{"id": int(ITEM), "category_id": 5221,
                          "title": TITLE,
                          "price": {"amount": 1490}}]}

    async def get_all_ads(self, max_pages=25):
        return (await self.get_ads()).get("data")

    # Вид товара: от него зависит, что бот может с остатком сделать сам.
    kind: str = ""
    value_block: dict = {}
    items_left: list = []
    updated: list = []

    # Молчит ли и маркетплейс тоже: тогда брать название неоткуда вовсе.
    nameless: bool = False

    async def get_ad(self, ad_id):
        # Маркетплейс знает тот же товар со своей стороны: название,
        # описание и картинку. При закрытой форме правки панели это
        # единственное место, откуда их взять.
        card = {"id": ad_id, "stock": 3, "category_id": 5221,
                "type": Api.kind, "title": TITLE, "description": DESCRIPTION,
                "images": [{"original_url": Api.image}],
                "price": {"amount": 1490, "currency": "RUB"}}
        if Api.nameless:
            card.pop("title")
            card.pop("description")
        return {"data": card}

    async def get_ad_value(self, ad_id):
        return {"data": dict(Api.value_block)}

    async def update_ad_value(self, ad_id, **fields):
        Api.updated.append(fields)

    async def get_ad_items(self, ad_id, cursor=None):
        return {"data": list(Api.items_left)}

    # Что приняли и что видно при перечитывании — РАЗНЫЕ вещи: маркетплейс
    # может взять не все строки, а публикует он по второму числу.
    sent: list = []
    accepts: int | None = None
    # Что он отвечает на саму отправку. Живой ответ 08.09 — «принял»,
    # и это не то же самое, что «положил».
    answer: dict | None = None

    async def add_ad_items(self, ad_id, items):
        rows = list(items)
        Api.sent.append((str(ad_id), rows))
        take = len(rows) if Api.accepts is None else Api.accepts
        Api.items_left = [{"status": "available"} for _ in rows[:take]]
        if Api.answer is not None:
            return dict(Api.answer)
        return {"status": "ok", "accepted": len(rows)}

    # Дерево разделов маркетплейса. Товар лежит в ЛИСТЕ («Аккаунты»), а
    # панель раскладывает по играм («Standoff 2»): нужное слово стоит на
    # среднем уровне, и одним именем листа его не достать.
    path: list = ["Игры", "Black Russia", "Аккаунты"]

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
        Api.image = f"{cls.base}/media/photo.jpg"

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
        LiveNova.form_open = True
        LiveNova.refuse = False
        Api.section = "Аккаунты"
        Api.stock, Api.refills = 0, []
        Api.path = ["Игры", "Black Russia", "Аккаунты"]
        Api.kind, Api.value_block, Api.items_left, Api.updated = "", {}, [], []
        Api.sent, Api.accepts, Api.answer = [], None, None
        Api.nameless = False
        # Маркетплейс кладёт позиции не мгновенно, и бот его ждёт. В
        # прогоне ждать нечего: ответ подставной и меняться не будет.
        from api import yoomarket as Y
        self._waits, Y._CONFIRM_WAITS = Y._CONFIRM_WAITS, (0.0,)
        self.Y = Y
        # Заготовка остатков — своя на каждый тест. По умолчанию её нет:
        # подставленная за продавца, она уехала бы живому покупателю.
        self.default_stock: list = []
        self._get_stock = storage.get_copy_stock
        storage.get_copy_stock = lambda uid: list(self.default_stock)

    def tearDown(self):
        self.Y._CONFIRM_WAITS = self._waits
        if hasattr(self, "_was_content"):
            for f in EDIT_FIELDS:
                if f["attribute"] == "content":
                    f["value"] = self._was_content
            del self._was_content
        self.storage.get_copy_marks = self._get_marks
        self.storage.remember_copy_marks = self._set_marks
        self.storage.forget_copy_marks = self._del_marks
        self.storage.get_copy_stock = self._get_stock
        self.storage.get_panel_creds = self._creds
        self.storage._DATA_DIR = self._dir
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def set_description(self, text: str) -> None:
        """Подменить описание образца — с откатом в `tearDown`.

        `EDIT_FIELDS[:]` копирует СПИСОК, а словари в нём общие: правка
        `f["value"]` доживала до соседнего теста и меняла его исход в
        зависимости от порядка. Ровно так и упали две проверки.
        """
        for f in EDIT_FIELDS:
            if f["attribute"] == "content":
                if not hasattr(self, "_was_content"):
                    self._was_content = f["value"]
                f["value"] = text
                return

    def press(self):
        fsm, api = FSM(), Api()
        asyncio.run(C.templates_list(CB("create_ad:templates_list"), fsm, api))
        cb = CB("create_ad:copy:0:0")
        asyncio.run(C.copy_item(cb, fsm, api))
        # Копия могла остановиться на вопросе — тогда создания нет вовсе, и
        # проверять надо ВЫБРАННОЕ, а не отправленное.
        self.fsm, self.cb = fsm, cb
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
        self.assertEqual(str(body.get("type")), "auto-delivery", body)

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
        self.assertIn("раздел: Black Russia (613)", said,
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

    def test_not_a_single_question_even_with_nothing_remembered(self):
        """Ради этого всё и делалось.

        Панель не показывает ни раздела, ни подраздела, ни типа. Памяти
        нет. И всё равно спрашивать нечего: раздел и подраздел находятся в
        тексте товара, а тип выдачи маркетплейс называет тем же словом,
        каким его называет панель — `auto-delivery`."""
        Api.path = []
        Api.kind = "auto-delivery"
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(asked, [], asked)
        body = self.created()
        self.assertEqual(str(body.get("category")), "613", body)
        self.assertEqual(str(body.get("subcategory")), "3", body)
        self.assertEqual(str(body.get("type")), "auto-delivery", body)

    def test_the_type_is_not_asked_when_the_marketplace_names_it(self):
        """Тип выдачи был последним, что бот спрашивал у каждой копии."""
        Api.path = []
        Api.kind = "auto-value"
        cb = self.press()
        self.assertFalse([t for t in cb.message.texts if "Выбери" in t],
                         cb.message.texts)
        self.assertEqual(str(self.created().get("type")), "auto-value")

    def test_a_type_the_panel_does_not_know_is_still_asked(self):
        """Словарь мог разойтись — тогда вопрос честнее подстановки."""
        Api.path = []
        Api.kind = "чего-то-такого-нет"
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(len(asked), 1, asked)
        self.assertIn("type", asked[0])

    def test_the_game_is_taken_from_the_description(self):
        """Ради этого правка и делалась. Раздел панели — игра, а в НАЗВАНИИ
        товара её нет: «💖Аккаунт 💖Баланс: 3.000.000 ₽». Зато она есть в
        описании, и панель прислала все 825 названий сама — значит искать
        надо их в тексте товара, а не свои слова в их списке."""
        Api.path = []                  # дерево маркетплейса молчит
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(len(asked), 1, asked)
        self.assertIn("type", asked[0])
        self.assertEqual(self.fsm.data["chosen"].get("category"), 613)

    def test_and_it_says_where_it_found_it(self):
        Api.path = []
        cb = self.press()
        # Проверяем данные, а не прозу: строка отчёта собирается из них,
        # а до самого отчёта копия ещё не дошла — стоит на вопросе о типе.
        self.assertTrue(
            any("найден в тексте товара" in n
                for n in (self.fsm.data.get("autopicked") or [])),
            self.fsm.data.get("autopicked"))

    def test_without_the_game_anywhere_it_asks(self):
        """Ни в описании, ни в дереве — тогда вопрос честный. Выдумывать
        раздел бот не должен: ошибка видна только по отсутствию продаж."""
        Api.path = []
        self.set_description("Аккаунт с виртами, вход по почте.")
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertTrue(any("category" in t for t in asked), asked)


class ThreeSourcesOfTheTypeAndTheOrderBetweenThem(Bench):
    """Вид выдачи бот берёт из трёх мест, и порядок между ними не случаен.

    Панель отвечает о СВОЁМ поле — том самом, в которое значение и уедет.
    Маркетплейс называет тот же товар, но своей стороной. Память — ответ,
    подходивший раньше. Разъехаться они могут молча, и тогда копия уйдёт
    с другим способом доставки: покупатель нажмёт «купить» и получит не
    то, что у образца.
    """

    def test_the_panel_beats_the_marketplace(self):
        """Карточка панели показывает `type` = auto-delivery. Маркетплейс
        того же товара в этом тесте говорит другое — и уступает: значение
        уедет в форму ПАНЕЛИ, и о её поле она сказала сама."""
        Api.kind = "auto-value"
        self.press()
        body = self.created()
        self.assertEqual(str(body.get("type")), "auto-delivery", body)

    def test_but_the_marketplace_beats_what_we_remembered(self):
        """Запомненное — прошлогодний ответ: товар мог сменить вид выдачи
        после того, как копию делали в прошлый раз. Панель здесь молчит
        (живой случай), и спор идёт между памятью и маркетплейсом."""
        LiveNova.section_visible = False
        self.marks[ITEM] = {"values": {"type": "unlimited", "category": 613,
                                       "subcategory": 3}, "labels": {}}
        Api.kind = "auto-value"
        self.press()
        body = self.created()
        self.assertEqual(str(body.get("type")), "auto-value", body)
        # А пропуски память всё так же заполняет — иначе тест выше
        # проходил бы и на выброшенной памяти.
        self.assertEqual(str(body.get("category")), "613", body)


class AClosedEditFormIsNotADeadEnd(Bench):
    """Живой отказ 08.09: «❌ Копия не создалась — Товар прочитать не
    вышло: update-fields: 403».

    Панель закрыла ОДИН из трёх своих ответов, а копия отказалась целиком.
    Это та же ошибка, что была с карточкой: Nova разрешает форму правки,
    карточку и список независимо, и закрытая форма правки значит только
    то, что название, описание и картинку надо взять в другом месте.
    """

    def test_the_copy_still_goes_out(self):
        LiveNova.form_open = False
        cb = self.press()
        body = self.created()
        self.assertTrue(body, "копия не ушла: " + str(cb.message.texts[-1]))
        self.assertEqual(str(body.get("category")), "613", body)

    def test_the_name_and_the_text_come_from_the_marketplace(self):
        """Их знает форма правки — а она закрыта. Но тот же товар есть у
        маркетплейса, и там они тоже есть."""
        LiveNova.form_open = False
        self.press()
        body = self.created()
        self.assertEqual(body.get("title"), TITLE, body)
        self.assertIn("Black Russia", str(body.get("content")), body)

    def test_and_it_still_asks_nothing(self):
        LiveNova.form_open = False
        cb = self.press()
        asked = [t for t in cb.message.texts if "Выбери" in t]
        self.assertEqual(asked, [], asked)

    def test_a_nameless_item_is_a_refusal_and_says_so(self):
        """Название — единственное, без чего создавать нечего. Молчат оба
        источника — это отказ, а не товар с пустым заголовком на витрине."""
        LiveNova.form_open = False
        Api.nameless = True
        cb = self.press()
        self.assertEqual(self.created(), {}, "отправлять было нечего")
        self.assertIn("названия", cb.message.texts[-1].lower())

    def test_but_all_three_closed_is_a_refusal_in_russian(self):
        """Когда молчат все три, отказ честный — и не кодом панели:
        «update-fields: 403» не говорит продавцу ничего."""
        LiveNova.form_open = False
        LiveNova.card_open = False
        LiveNova.list_open = False
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertEqual(self.created(), {}, "отправлять было нечего")
        self.assertIn("панель", said.lower(), said)
        self.assertIn("форму правки", said, said)


class ThePourGoesTheSameRoadWithoutAnybodyToAsk(Bench):
    """Залив — это копия, у которой некого спросить.

    Идти он обязан ТЕМ ЖЕ кодом: разойдись они, залив клал бы товары не
    туда, куда кладёт копия по нажатию, — и заметить это было бы нечем.
    Поэтому проверяется он на той же подставной панели, отвечающей как
    живая.
    """

    def pour(self):
        return asyncio.run(C.pour_once(7, ITEM, 5221, Api()))

    def test_it_creates_the_item_and_returns_its_number(self):
        got = self.pour()
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["id"], "900002", got)
        self.assertTrue(self.created(), "товар не ушёл в панель")

    def test_the_section_goes_out_the_same_way(self):
        """Тот же раздел, что и у копии по нажатию: путь один."""
        self.pour()
        body = self.created()
        self.assertEqual(str(body.get("category")), "613", body)
        self.assertEqual(str(body.get("subcategory")), "3", body)

    def test_a_question_becomes_a_skip_with_a_reason(self):
        """Спросить некого. Тупик здесь был бы тишиной, а тишина — это
        день, потерянный продавцом."""
        LiveNova.section_visible = False      # раздела в панели нет
        Api.path = []                         # и в дереве маркетплейса тоже
        self.set_description("Аккаунт с виртами, вход по почте.")
        got = self.pour()
        self.assertFalse(got["ok"], got)
        self.assertIn("category", got["why"])
        self.assertIn("руками", got["why"], "сказано, чем это чинится")

    def test_and_nothing_is_created_then(self):
        LiveNova.section_visible = False
        Api.path = []
        self.set_description("Аккаунт с виртами, вход по почте.")
        self.pour()
        self.assertEqual(self.created(), {}, "отправлять было нечего")

    def test_a_panel_refusal_is_a_reason_too(self):
        LiveNova.refuse = True
        got = self.pour()
        self.assertFalse(got["ok"], got)
        self.assertTrue(got["why"], "молчаливый отказ хуже любого текста")

    def test_an_unreadable_item_is_named_as_such(self):
        LiveNova.form_open = False
        LiveNova.card_open = False
        LiveNova.list_open = False
        got = self.pour()
        self.assertFalse(got["ok"], got)
        self.assertIn("панель", got["why"].lower(), got)

    def test_the_answer_it_remembers_is_reused_next_time(self):
        """Первый залив запомнил раздел — второй уже не ищет его заново."""
        self.pour()
        LiveNova.section_visible = False
        Api.path = []
        self.set_description("Аккаунт с виртами, вход по почте.")
        got = self.pour()
        self.assertTrue(got["ok"], got)


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
        # И дорога к настройке — оттуда, где о ней спрашивают. Экран копии
        # ищут не здесь, а кнопка нужна именно в этот момент.
        self.assertIn("⚙️ Класть их всегда", self.keyboard_texts(cb))

    def test_but_not_to_a_seller_the_screen_is_closed_for(self):
        """Отчёт один на копию и на мастер, а копия открыта не всем.
        Кнопка, отвечающая «этого раздела сейчас нет», — дохлая кнопка.

        Проверяется настоящий путь мастера: копия при закрытом разделе
        отказывает раньше кнопок, и проверка через неё была бы пустой."""
        Api.kind = "auto-delivery"
        self.features.ad_templates_shown = lambda uid: False
        msg = Msg()
        asyncio.run(C._panel_create_and_report(
            msg, 7, {"title": "Товар", "price": 100, "description": "текст",
                     "quantity": 1},
            extra={"category": 613}, state=FSM(), api=Api()))
        kb = next((k for k in reversed(msg.kbs) if k), None)
        texts = [b.text for row in (kb.inline_keyboard if kb else []) for b in row]
        self.assertIn("📦 Прислать остатки", texts, texts)
        self.assertNotIn("⚙️ Класть их всегда", texts, texts)

    def test_the_default_list_is_put_in_without_a_single_question(self):
        """Ради этого правка и делалась: продавец задал заготовку один раз,
        и дальше остатки у копии появляются сами."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111", "KEY-2222", "KEY-3333"]
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertEqual([rows for _id, rows in Api.sent],
                         [["KEY-1111", "KEY-2222", "KEY-3333"]], Api.sent)
        self.assertNotEqual(Api.sent[0][0], ITEM,
                            "позиции кладутся КОПИИ, а не образцу")
        self.assertIn("Остаток проставлен: 3 поз.", said)
        self.assertNotIn("одноразовые", said, "просить нечего — список есть")

    def test_and_it_warns_whose_rows_the_buyer_will_get(self):
        """Заготовка, забытая на витрине, — это оплаченный заказ с мусором
        внутри. Молчание здесь дороже лишней строки."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111"]
        said = self.press().message.texts[-1]
        self.assertIn("получит именно эти строки", said)
        self.assertIn("📦 Прислать остатки", self.keyboard_texts(self.cb),
                      "заменить заготовку настоящими — тут же")

    def test_the_number_comes_from_the_re_read_not_from_what_we_sent(self):
        """«Отправлено 3» и «в наличии 3» — разные утверждения, а
        публиковать маркетплейс даёт по второму."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111", "KEY-2222", "KEY-3333"]
        Api.accepts = 1                      # взял одну из трёх
        said = self.press().message.texts[-1]
        self.assertIn("пока 1", said, said)
        self.assertNotIn("Остаток проставлен", said, said)

    def test_a_marketplace_that_says_it_took_them_is_not_called_a_refusal(self):
        """Живая проба 08.09: на отправку он отвечает `{"status": "ok",
        "accepted": 1}`, а в списке позиции появляются позже. «В наличии
        их нет» сразу после отправки — это ещё не отказ, и назвать его
        отказом значит послать продавца выложить те же ключи дважды."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111"]
        Api.accepts = 0                      # список ещё не обновился
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("Остатки отправлены", said)
        self.assertIn("принял: 1", said, said)
        self.assertIn("не мгновенно", said)
        self.assertIn("слать те же ключи не", said,
                      "второй список положил бы те же ключи дважды")
        self.assertNotIn("в наличии их нет", said, said)
        self.assertIn("🔄 Проверить остаток", self.keyboard_texts(cb))

    def test_but_a_silent_marketplace_is_still_a_refusal(self):
        """Ни одной позиции, и о принятых он не сказал ни слова — тогда
        «отправлено» без «в наличии» было бы обещанием."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111"]
        Api.accepts, Api.answer = 0, {"status": "ok"}
        said = self.press().message.texts[-1]
        self.assertIn("в наличии их нет", said, said)

    def test_and_the_report_carries_what_the_marketplace_answered(self):
        """Живой случай 08.09: позиции ушли, публикация отказала
        `empty_stock`, а ответ маркетплейса на саму отправку бот
        выбрасывал — и «отправлены, но их нет» осталось загадкой."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111"]
        Api.accepts, Api.answer = 0, {"status": "ok"}
        said = self.press().message.texts[-1]
        self.assertIn("Маркетплейс на отправку ответил", said)
        self.assertIn("status", said, said)

    def test_a_marketplace_that_refuses_the_list_says_so(self):
        """Исключение отсюда съело бы весь отчёт о созданном товаре."""
        Api.kind = "auto-delivery"
        self.default_stock = ["KEY-1111"]

        async def boom(ad_id, items):
            raise RuntimeError("остатки не принимаются")

        was = Api.add_ad_items
        Api.add_ad_items = lambda self, ad_id, items: boom(ad_id, items)
        try:
            said = self.press().message.texts[-1]
        finally:
            Api.add_ad_items = was
        self.assertIn("Товар создан", said, said)
        self.assertIn("не вышло", said, said)
        self.assertIn("остатки не принимаются", said, said)

    def test_an_unlimited_copy_is_not_nagged_about_stock(self):
        Api.kind = "unlimited"
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("безлимит", said.lower())
        self.assertNotIn("📦 Прислать остатки", self.keyboard_texts(cb))


class AnAnswerGivenOnceIsNotAskedAgain(Bench):
    """Раздела у товара в панели нет нигде, и узнать его второй раз
    неоткуда. Спрошенное однажды помнится за образцом — и за РАЗДЕЛОМ
    маркетплейса: аккаунтов одной игры у продавца десяток, и переспрашивать
    про Black Russia на каждом — тот самый круг, ради которого всё это.

    Запоминается только после того, как панель товар ПРИНЯЛА: отказ значит,
    что значения не подошли, а запомненная неправда хуже вопроса.
    """

    def setUp(self):
        super().setUp()
        LiveNova.section_visible = False
        Api.path = []
        # Игру убираем и из описания: иначе раздел находится сам, и
        # проверка проверяла бы не память, а поиск по тексту.
        self.set_description("Аккаунт с виртами, вход по почте.")

    def answer_the_questions(self, cb):
        """Ответить на все вопросы так, как ответил бы продавец."""
        fsm = self.fsm
        for _ in range(6):
            options = fsm.data.get("current_view") or []
            if not fsm.data.get("current_attr") or not options:
                break
            want = next((i for i, o in enumerate(options)
                         if o["label"] in ("Black Russia", "Аккаунты с виртами",
                                           "Авто-выдача")), 0)
            pick = CB(f"cadopt:{want}")
            pick.message = cb.message
            asyncio.run(C.choose_select_option(pick, fsm, Api()))
        return fsm

    def test_the_answers_are_remembered_after_success(self):
        cb = self.press()
        self.assertTrue([t for t in cb.message.texts if "Выбери" in t])
        self.answer_the_questions(cb)
        self.assertEqual(self.marks.get(ITEM, {}).get("values", {}).get(
            "category"), 613, self.marks)

    def test_and_the_name_is_remembered_with_the_number(self):
        """Номер 613 продавцу не говорит ничего, «Black Russia» — всё."""
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertEqual(self.marks[ITEM]["labels"].get("category"),
                         "Black Russia")

    def test_another_item_of_the_same_game_reuses_the_answer(self):
        """Аккаунтов Black Russia у продавца десяток, и все они лежат в
        одном разделе маркетплейса. Спросить про игру один раз и
        переспрашивать на каждом новом аккаунте — тот самый круг."""
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertIn("cat:5221", self.marks, self.marks)
        # Другой товар, тот же раздел маркетплейса, своей памяти нет.
        self.marks.pop(ITEM, None)
        cb2 = self.press()
        self.assertEqual([t for t in cb2.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")

    def test_a_refusal_remembers_nothing(self):
        """Значения не подошли — запомненная неправда хуже вопроса."""
        LiveNova.refuse = True
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertEqual(self.marks, {}, self.marks)

    def test_a_refusal_shows_the_names_it_tried(self):
        """Продавец решает по отказу, туда ли шёл товар. Форма к этому
        моменту уже закрыта, и надписи надо снять раньше."""
        LiveNova.refuse = True
        cb = self.press()
        self.answer_the_questions(cb)
        self.assertIn("Black Russia", cb.message.texts[-1])

    def test_the_second_copy_does_not_ask_the_same_thing(self):
        self.marks[ITEM] = {"values": {"category": 613, "subcategory": 3,
                                       "type": "auto-delivery"},
                            "labels": {"category": "Black Russia"}}
        cb = self.press()
        self.assertEqual([t for t in cb.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")

    def test_the_report_names_the_section_not_its_number(self):
        self.marks[ITEM] = {"values": {"category": 613, "subcategory": 3,
                                       "type": "auto-delivery"},
                            "labels": {"category": "Black Russia"}}
        cb = self.press()
        self.assertIn("раздел: Black Russia", cb.message.texts[-1])

    def test_the_seller_can_take_the_answer_back(self):
        """Ошибиться разделом можно один раз: панель менять его не даёт."""
        self.marks[ITEM] = {"values": {"category": 613}, "labels": {}}
        cb = CB(f"create_ad:forget:{ITEM}")
        asyncio.run(C.forget_marks(cb))
        self.assertEqual(self.marks, {})
        self.assertTrue(cb.alerts)

    def test_what_the_panel_shows_beats_what_we_remember(self):
        """Прочитанное у панели свежее запомненного. Номер взят настоящий,
        из того же списка: иначе сверка отвергла бы его сама, и проверка
        проходила бы при любом порядке."""
        LiveNova.section_visible = True
        other = CATEGORY_OPTIONS[0]
        self.assertNotEqual(other["value"], 613)
        self.marks[ITEM] = {"values": {"category": other["value"]},
                            "labels": {"category": other["display"]}}
        self.press()
        self.assertEqual(str(self.created().get("category")), "613")


class TheMarketplaceNameNeedNotMatchThePanels(Bench):
    """Раздел у маркетплейса называется по-своему. Это подсказка, а не
    источник номера: номер берётся у панели."""

    def test_a_name_that_matches_nothing_changes_nothing(self):
        Api.section = "Игровые ценности и аккаунты"
        cb = self.press()
        self.assertEqual([t for t in cb.message.texts if "Выбери" in t], [])
        self.assertEqual(str(self.created().get("category")), "613")


class TheAdviceMatchesWhatIsActuallyMissing(Bench):
    """«Добавь остатки, потом жми На модерацию» при полном остатке — это
    совет сделать ровно то, что только что не сработало.

    Живой случай 08.09: остатки в товаре есть, публикация не проходит, а
    отчёт советовал добавить остатки.
    """

    def setUp(self):
        super().setUp()
        from handlers import panel_items as PI
        self.PI = PI
        self._pub = PI.publish_item_sync_first
        self.answer = (False, "маркетплейс через API его не публикует")

        async def refused(api, cookies, item_id, uid):
            return self.answer

        PI.publish_item_sync_first = refused

    def tearDown(self):
        self.PI.publish_item_sync_first = self._pub
        super().tearDown()

    def test_with_stock_in_place_it_does_not_ask_for_stock(self):
        Api.kind = "auto-value"
        Api.value_block = {"stock": 5}
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("остаток на месте", said.lower(), said)
        self.assertNotIn("Добавь остатки", said)

    def test_without_stock_the_two_steps_stay(self):
        """А когда остатка правда нет — совет прежний и верный."""
        Api.kind = "auto-delivery"
        cb = self.press()
        self.assertIn("Добавь остатки", cb.message.texts[-1])

    def test_the_marketplace_answer_reaches_the_seller(self):
        """Причина отказа — то единственное, ради чего этот экран читают."""
        cb = self.press()
        self.assertIn("через API его не публикует", cb.message.texts[-1])

    def test_the_button_follows_the_outcome_not_the_wording(self):
        """Кнопка «На модерацию» показывалась по слову «модерац» в своём же
        отчёте. Разбор собственной прозы — тихая поломка, которая ждёт
        правки текста."""
        cb = self.press()
        kb = next((k for k in reversed(cb.message.kbs) if k), None)
        texts = [b.text for row in (kb.inline_keyboard if kb else []) for b in row]
        self.assertIn("🚀 На модерацию", texts, texts)
        self.answer = (True, "через маркетплейс, статус: moderate")
        cb2 = self.press()
        kb2 = next((k for k in reversed(cb2.message.kbs) if k), None)
        texts2 = [b.text for row in (kb2.inline_keyboard if kb2 else []) for b in row]
        self.assertNotIn("🚀 На модерацию", texts2, texts2)


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
