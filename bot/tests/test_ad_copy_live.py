"""Копия товара, прогнанная через НАСТОЯЩИЙ HTTP.

Здесь не подменяются ни `requests`, ни разбор ответа: поднимается
подставная панель Nova, и по ней ходит тот же код, что ходит по живой —
`panel_item_values_sync` читает поля товара, `panel_create_product_sync`
создаёт новый. Проверяется то, чего заглушками не проверить:

* **раздел доезжает номером, а не надписью.** У полей BelongsTo выбранное
  лежит в `belongsToId`, и `str(value)` отправил бы «Аккаунты» вместо
  `12` — именно из-за этого копия теряла раздел и получала 422;
* **`filter__N` доезжают все.** Какие из них обязательны, зависит от
  раздела, и форма создания об этом молчит: перечислить их поимённо
  нельзя, можно только перенести всё, что у товара есть;
* **картинка уходит файлом.** Панель принимает её только настоящей
  загрузкой (`__media__[images][0]`), а без картинки товар не создаётся;
* **чужое не переносится.** `id` исходного товара в теле создания — это
  попытка создать товар с чужим номером.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P                          # noqa: E402

# Товар-образец: ровно та форма, в которой Nova отдаёт update-fields.
# Раздел и подраздел — BelongsTo: выбранное в `belongsToId`, а в `value`
# лежит НАДПИСЬ. Отправленная надпись и есть та поломка, из-за которой
# копия падала с 422.
ITEM_FIELDS = [
    {"attribute": "id", "value": 219206},
    # Поля, которых при СОЗДАНИИ нет вовсе: у товара они есть, а форма
    # создания их не знает. Отправленные, они дали бы отказ по полю,
    # которого продавец не заполнял и заполнить не может.
    {"attribute": "public", "value": 1},
    {"attribute": "moderation_status", "value": "approved"},
    {"attribute": "title", "value": "Аккаунт Standoff 2 с виртами"},
    # Цены здесь НЕТ намеренно: у товара в панели поля цены не существует
    # (давняя запись в CLAUDE.md, из-за неё же снята правка цены из бота).
    # Живой отказ 02.09: копия читала ноль и вставала.
    {"attribute": "content", "value": "Аккаунт с внутриигровой валютой."},
    {"attribute": "quantity", "value": 3},
    {"attribute": "category", "value": {"display": "Аккаунты"},
     "belongsToId": 12, "relationshipType": "belongsTo",
     "component": "belongs-to-field"},
    {"attribute": "subcategory", "value": {"display": "Standoff 2"},
     "belongsToId": 44, "relationshipType": "belongsTo",
     "component": "belongs-to-field"},
    {"attribute": "type", "value": {"display": "Мгновенная выдача"},
     "belongsToId": 2, "relationshipType": "belongsTo",
     "component": "belongs-to-field"},
    {"attribute": "filter__8", "value": "Россия"},
    {"attribute": "filter__3", "value": 7},
    {"attribute": "images", "component": "advanced-media-library-field",
     "value": [{"original_url": "http://ЗАМЕНА/media/photo.jpg"}]},
    {"attribute": "created_at", "value": "2026-08-01 10:00:00"},
    {"attribute": "slug", "value": "akkaunt-standoff-2"},
]

# Форма СОЗДАНИЯ. Фильтры в ней есть — на этом построен весь разбор
# отказов: «filter__8: Поле Регион обязательно» мастер ищет именно здесь и
# спрашивает (живой случай 20.08). А `public` и `moderation_status` — поля
# товара, при создании их нет: они и не должны доехать.
CREATION_FIELDS = [
    {"attribute": "title", "rules": ["required", "max:120"]},
    {"attribute": "price", "rules": ["required"]},
    {"attribute": "content", "rules": []},
    {"attribute": "quantity", "rules": []},
    {"attribute": "category", "rules": ["required"]},
    {"attribute": "subcategory", "rules": []},
    {"attribute": "type", "rules": []},
    {"attribute": "filter__8", "rules": []},
    {"attribute": "filter__3", "rules": []},
    {"attribute": "images", "rules": []},
]


class Nova(BaseHTTPRequestHandler):
    """Панель, отвечающая как настоящая. Что приняла — складывает в `posted`."""

    posted: list = []
    refuse_first: dict | None = None

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/sanctum/csrf-cookie"):
            self.send_response(204)
            self.send_header("Set-Cookie", "XSRF-TOKEN=tok123; Path=/")
            self.end_headers()
            return
        if "/update-fields" in self.path:
            self._json(200, {"fields": ITEM_FIELDS})
            return
        if "/creation-fields" in self.path:
            # Только `items`: настоящая панель на прочие разделы отвечает
            # 403/404, и создание перебирает их, пока не найдёт форму
            # товара. Отвечая всем, подставная панель уводила создание в
            # первый попавшийся раздел — и проверка смотрела не туда.
            if "/nova-api/items/" in self.path:
                self._json(200, {"fields": CREATION_FIELDS})
            else:
                self._json(404, {"message": "нет такого раздела"})
            return
        if self.path.startswith("/media/"):
            body = b"\xff\xd8\xff\xe0JPEGDATA"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"message": "нет такого"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type") or ""
        Nova.posted.append({"path": self.path, "ctype": ctype, "raw": raw})
        if Nova.refuse_first is not None:
            body, Nova.refuse_first = Nova.refuse_first, None
            self._json(422, body)
            return
        self._json(201, {"resource": {"id": 900001}})


class Bench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Nova)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls._url = P.PANEL_URL
        P.PANEL_URL = cls.base
        for f in ITEM_FIELDS:
            if f["attribute"] == "images":
                f["value"] = [{"original_url": f"{cls.base}/media/photo.jpg"}]

    @classmethod
    def tearDownClass(cls):
        P.PANEL_URL = cls._url
        cls.srv.shutdown()

    def setUp(self):
        Nova.posted = []
        Nova.refuse_first = None

    def read(self):
        return P.panel_item_values_sync("session=1", "219206", uid=None)

    def sent(self) -> dict:
        """Что панель получила при создании: поля тела, как строки."""
        for row in Nova.posted:
            if "items" in row["path"] and "editMode=create" in row["path"]:
                return _parse_body(row)
        return {}


def _parse_body(row: dict) -> dict:
    """Тело запроса → {поле: значение}. Понимает и JSON, и multipart."""
    ctype, raw = row["ctype"], row["raw"]
    if ctype.startswith("application/json"):
        return dict(json.loads(raw.decode()))
    if "multipart/form-data" not in ctype:
        return {}
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    out: dict = {}
    for part in raw.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        head_s = head.decode("utf-8", "replace")
        if 'name="' not in head_s:
            continue
        name = head_s.split('name="', 1)[1].split('"', 1)[0]
        body = body.rstrip(b"\r\n-")
        out[name] = (body if b"filename=" in head.lower() or b"\xff\xd8" in body
                     else body.decode("utf-8", "replace"))
    return out


class TheSourceItemIsReadAsCreationExpectsIt(Bench):

    def test_the_plain_fields_come_back(self):
        ok, values, _extra, _url, err = self.read()
        self.assertTrue(ok, err)
        self.assertEqual(values["title"], "Аккаунт Standoff 2 с виртами")
        self.assertEqual(values["description"],
                         "Аккаунт с внутриигровой валютой.")
        self.assertEqual(values["quantity"], 3)

    def test_a_missing_price_is_none_not_zero(self):
        """У товара в панели поля цены НЕТ. Ноль по умолчанию уехал бы на
        витрину ценой; `None` означает «поля не было», и вызывающий берёт
        цену из второго источника."""
        _ok, values, _extra, _url, _err = self.read()
        self.assertIsNone(values["price"])

    def test_the_section_is_a_number_not_a_label(self):
        """`str(value)` отправил бы «Аккаунты» — и панель ответила бы 422
        по полю `category`. Живой отказ, из-за которого всё и переделано."""
        _ok, values, extra, _url, _err = self.read()
        self.assertEqual(extra["category"], 12)
        self.assertEqual(extra["subcategory"], 44)
        self.assertEqual(values["category"], 12)

    def test_every_filter_comes_along(self):
        """Какие из них обязательны, зависит от раздела, и форма создания
        об этом молчит — перечислить поимённо нельзя."""
        _ok, _values, extra, _url, _err = self.read()
        self.assertEqual(extra["filter__8"], "Россия")
        self.assertEqual(extra["filter__3"], 7)

    def test_the_items_own_fields_stay_behind(self):
        """`id` исходного товара в теле создания — это попытка создать
        товар с чужим номером."""
        _ok, _values, extra, _url, _err = self.read()
        for own in ("id", "created_at", "slug"):
            with self.subTest(own):
                self.assertNotIn(own, extra)

    def test_the_picture_address_is_found(self):
        _ok, _values, _extra, url, _err = self.read()
        self.assertTrue(url.endswith("/media/photo.jpg"), url)

    def test_a_relative_picture_address_still_works(self):
        """Панель отдаёт адрес и относительным. Отвергнутый, он оставлял
        копию с «у товара не нашлось картинки» на товаре, у которого она
        есть."""
        for f in ITEM_FIELDS:
            if f["attribute"] == "images":
                was = f["value"]
                f["value"] = [{"original_url": "/media/photo.jpg"}]
                try:
                    _ok, _v, _e, url, _err = self.read()
                    self.assertTrue(url.startswith(self.base), url)
                    data = P.panel_fetch_image_sync("session=1", url)
                    self.assertTrue(data.startswith(b"\xff\xd8"))
                finally:
                    f["value"] = was
                return
        self.fail("в образце нет картинки")

    def test_the_picture_downloads(self):
        _ok, _v, _e, url, _err = self.read()
        data = P.panel_fetch_image_sync("session=1", url)
        self.assertTrue(data.startswith(b"\xff\xd8"), "это не картинка")

    def test_a_panel_that_says_nothing_is_a_refusal(self):
        """«Пустые поля» — не пустой товар, а неудачное чтение."""
        old = ITEM_FIELDS[:]
        try:
            ITEM_FIELDS.clear()
            ok, _v, _e, _u, err = self.read()
            self.assertFalse(ok)
            self.assertTrue(err)
        finally:
            ITEM_FIELDS.extend(old)


class TheCopyGoesOutAsARealCreation(Bench):
    """Создание идёт тем же вызовом, что и мастер: путь проверен живьём, а
    отказ по полю он умеет превращать в вопрос."""

    def make(self, photo=True):
        _ok, values, extra, url, _err = self.read()
        path = ""
        if photo:
            import tempfile
            data = P.panel_fetch_image_sync("session=1", url)
            fd, path = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        try:
            return P.panel_create_product_sync(
                "session=1", values["title"], values["price"] or 1490,
                values["description"], values["quantity"],
                values["category"], None, extra, path or None)
        finally:
            if path:
                os.unlink(path)

    def test_the_panel_accepts_it(self):
        ok, said = self.make()
        self.assertTrue(ok, said)
        self.assertIn("900001", str(said))

    def test_the_section_reaches_the_panel_as_a_number(self):
        self.make()
        body = self.sent()
        self.assertEqual(str(body.get("category")), "12")
        self.assertEqual(str(body.get("subcategory")), "44")

    def test_the_delivery_type_travels_too(self):
        """Раздел, подраздел и тип выдачи продавец у копии не заполняет:
        у товара они есть, копия их и несёт."""
        self.make()
        self.assertEqual(str(self.sent().get("type")), "2")

    def test_the_filters_reach_the_panel(self):
        self.make()
        body = self.sent()
        self.assertEqual(body.get("filter__8"), "Россия")
        self.assertEqual(str(body.get("filter__3")), "7")

    def test_the_text_fields_reach_the_panel(self):
        self.make()
        body = self.sent()
        self.assertEqual(body.get("title"), "Аккаунт Standoff 2 с виртами")
        self.assertEqual(str(body.get("price")), "1490")
        self.assertEqual(body.get("content"), "Аккаунт с внутриигровой валютой.")
        self.assertEqual(str(body.get("quantity")), "3")

    def test_the_picture_goes_as_a_real_upload(self):
        """Панель принимает картинку только настоящей загрузкой; поле
        `__media__[images][0]` — то, которым её принимает Nova."""
        self.make()
        body = self.sent()
        keys = [k for k in body if "media" in k or k.startswith("images")]
        self.assertTrue(keys, f"картинка не ушла: {list(body)}")
        self.assertTrue(any(isinstance(body[k], bytes) and
                            body[k].startswith(b"\xff\xd8") for k in keys),
                        "ушла не картинка")

    def test_the_source_id_never_goes_out(self):
        self.make()
        self.assertNotIn("id", self.sent())

    def test_fields_creation_does_not_know_are_left_behind(self):
        """У товара есть поля, которых при создании нет вовсе. Отправленные,
        они дали бы отказ по полю, которого продавец не заполнял."""
        self.make()
        body = self.sent()
        for own in ("public", "moderation_status"):
            with self.subTest(own):
                self.assertNotIn(own, body)

    def test_a_refusal_is_not_called_a_success(self):
        Nova.refuse_first = {"category": ["Поле Категория обязательно."]}
        ok, said = self.make()
        self.assertFalse(ok, said)

    def test_the_refusal_names_the_field(self):
        """Отказ панели называет поле прямым текстом — на этом и построен
        вопрос продавцу вместо тупика."""
        Nova.refuse_first = {"filter__8": ["Поле Регион обязательно."]}
        _ok, said = self.make()
        self.assertIn("filter__8", str(P.validation_fields(str(said))) + str(said))


class TheWholeCopyRunsEndToEnd(Bench):
    """Нажатие продавца → чтение товара → создание → отчёт, целиком и по
    настоящему HTTP.

    Заглушками этого не проверить: между обработчиком и панелью лежат
    исполнитель потока, сборка multipart и разбор ответа Nova, и именно там
    ломалось всё, что ломалось. Подменяются только куки, каталог для
    картинки и список объявлений — всё остальное настоящее.
    """

    def setUp(self):
        import tempfile
        import features
        import storage
        from handlers import create_ad as C
        self.C, self.storage, self.features = C, storage, features
        self._creds = storage.get_panel_creds
        self._dir = storage._DATA_DIR
        self._shown = features.ad_templates_shown
        self.tmp = tempfile.TemporaryDirectory()
        storage.get_panel_creds = lambda uid: {"cookies": "session=1"}
        storage._DATA_DIR = self.tmp.name
        features.ad_templates_shown = lambda uid: True
        Nova.posted = []
        Nova.refuse_first = None

    def tearDown(self):
        self.storage.get_panel_creds = self._creds
        self.storage._DATA_DIR = self._dir
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def press(self):
        """Пройти путь продавца: список разделов → раздел → копия."""
        import asyncio

        class Msg:
            def __init__(s):
                s.texts, s.kbs = [], []

            async def edit_text(s, text, reply_markup=None, **kw):
                s.texts.append(text)
                s.kbs.append(reply_markup)
                return s

            async def answer(s, text, reply_markup=None, **kw):
                s.texts.append(text)
                s.kbs.append(reply_markup)
                return s

        class CB:
            def __init__(s, data):
                s.data, s.message = data, Msg()
                s.from_user = type("U", (), {"id": 7})()
                s.alerts = []

            async def answer(s, text="", **kw):
                s.alerts.append(text)

        class FSM:
            def __init__(s):
                s.data, s.state = {}, None

            async def get_data(s):
                return dict(s.data)

            async def update_data(s, **kw):
                s.data.update(kw)
                return dict(s.data)

            async def set_state(s, x):
                s.state = x

            async def clear(s):
                s.data, s.state = {}, None

        class Api:
            """Список и ЦЕНА: у товара в панели поля цены нет вовсе, и
            взять её можно только здесь. Маркетплейс отдаёт её объектом."""

            asked: list = []

            async def get_ad(s, ad_id):
                Api.asked.append(str(ad_id))
                return {"data": {"id": ad_id, "stock": 3,
                                 "price": {"amount": 1490,
                                           "currency": "RUB"}}}

            async def get_ads(s, cursor=None):
                return {"data": [{"id": 219206,
                                  "title": "Аккаунт Standoff 2 с виртами",
                                  "price": {"amount": 1490},
                                  "category_id": 12}]}

            async def resolve_category(s, cid):
                return "Аккаунты"

            async def get_categories(s, **kw):
                return [{"id": 12, "name": "Аккаунты"}]

        fsm, api = FSM(), Api()
        Api.asked = []
        self.api, self.fsm = api, fsm
        asyncio.run(self.C.templates_list(CB("create_ad:templates_list"),
                                          fsm, api))
        cb = CB("create_ad:copy:0:0")
        asyncio.run(self.C.copy_item(cb, fsm, api))
        return cb

    def created_body(self) -> dict:
        for row in Nova.posted:
            if "/nova-api/items?" in row["path"] and "editMode=create" in row["path"]:
                return _parse_body(row)
        return {}

    def test_the_seller_is_told_it_worked(self):
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertIn("создан", said.lower(), said)
        self.assertNotIn("не создалась", said)

    def test_the_panel_got_a_real_creation(self):
        self.press()
        self.assertTrue(self.created_body(), "создание до панели не дошло")

    def test_everything_the_item_had_reached_the_panel(self):
        """Копия — пробег по тем же шагам, но с готовыми значениями. Здесь
        и проверяется, что «готовые значения» доехали все."""
        self.press()
        body = self.created_body()
        self.assertEqual(body.get("title"), "Аккаунт Standoff 2 с виртами")
        self.assertEqual(str(body.get("price")), "1490")
        self.assertEqual(body.get("content"), "Аккаунт с внутриигровой валютой.")
        self.assertEqual(str(body.get("quantity")), "3")
        self.assertEqual(str(body.get("category")), "12")
        self.assertEqual(str(body.get("subcategory")), "44")
        # Тип выдачи — тоже BelongsTo, и тоже уходит номером. Продавцу его
        # заполнять не нужно: у товара он есть, копия его и несёт.
        self.assertEqual(str(body.get("type")), "2")
        self.assertEqual(body.get("filter__8"), "Россия")
        self.assertEqual(str(body.get("filter__3")), "7")

    def test_the_picture_reached_the_panel_as_a_file(self):
        self.press()
        body = self.created_body()
        pic = [v for k, v in body.items()
               if isinstance(v, bytes) and v.startswith(b"\xff\xd8")]
        self.assertTrue(pic, f"картинка не ушла: {list(body)}")

    def test_the_source_id_never_goes_out(self):
        self.press()
        self.assertNotIn("id", self.created_body())

    def test_the_stock_is_taken_from_the_marketplace_when_the_panel_lacks_it(self):
        """У товара может не быть и количества — как нет цены. Молчаливая
        единица вместо трёх это копия, отличающаяся от образца остатком."""
        keep = [f for f in ITEM_FIELDS if f["attribute"] == "quantity"]
        for f in keep:
            ITEM_FIELDS.remove(f)
        try:
            self.press()
            self.assertEqual(str(self.created_body().get("quantity")), "3")
        finally:
            ITEM_FIELDS.extend(keep)

    def test_the_price_is_taken_from_the_marketplace(self):
        """Живой отказ 02.09: «цена товара не прочиталась (в полях панели
        0)». У товара в панели поля цены НЕТ — она есть только в API, и
        объектом: прочитанная как скаляр, она превращается в ноль."""
        self.press()
        self.assertTrue(self.api.asked, "за ценой в маркетплейс не ходили")
        self.assertEqual(str(self.created_body().get("price")), "1490")

    def test_without_the_marketplace_it_refuses_instead_of_giving_it_away(self):
        """Бесплатный товар на витрине хуже несозданной копии."""
        import asyncio
        from handlers import create_ad as C
        values, _extra, why = asyncio.run(
            C._copy_source_values(7, "219206", None))
        self.assertEqual(values, {})
        self.assertIn("бесплатной", why)

    def test_the_new_number_is_shown(self):
        cb = self.press()
        self.assertIn("900001", cb.message.texts[-1])

    def test_a_refusal_is_not_called_a_success(self):
        """Самая дорогая поломка этого проекта — бодрый отчёт об успехе
        там, где ничего не создалось."""
        Nova.refuse_first = {"content": ["Ссылки запрещены."]}
        cb = self.press()
        said = cb.message.texts[-1]
        # Смотрим ЗАГОЛОВОК, а не весь экран: подробности отказа лежат под
        # спойлером и содержат свои галочки («✅ Ресурс items найден»), к
        # успеху создания отношения не имеющие.
        head = said.split("\n", 1)[0]
        self.assertNotIn("создан", head.lower(), head)
        self.assertIn("не приняла", head)
        self.assertIn("Ссылки запрещены", said)

    def test_a_text_field_refusal_is_explained_not_dumped(self):
        """Панель отказывает и по текстовым полям. Такое поле спрашивалось
        как список, вариантов у него нет — и продавец получал отладку
        «Не удалось получить варианты поля content. Пришли этот текст
        разработчику» вместо русской причины, которую панель назвала сама."""
        Nova.refuse_first = {"content": ["Ссылки запрещены."]}
        cb = self.press()
        said = cb.message.texts[-1]
        self.assertNotIn("разработчику", said)
        self.assertIn("описание", said.lower())

    def test_a_refused_field_becomes_a_question(self):
        """Раздел мог поменяться с прошлого раза. Отказ панели по полю —
        не тупик: мастер спрашивает и досылает."""
        Nova.refuse_first = {"filter__9": ["Поле Платформа обязательно."]}
        cb = self.press()
        said = " ".join(cb.message.texts)
        self.assertIn("Платформа", said)

    def test_a_forbidden_word_can_be_fixed_in_one_tap(self):
        """Живой отказ 02.09: «Запрещено использовать «розыгрыш» в тексте».
        Слово панель называет сама — заставлять после этого перенабирать
        описание целиком значит требовать работы на ровном месте."""
        import asyncio
        from handlers import create_ad as C

        Nova.refuse_first = {"content": [
            "Запрещено использовать «валютой» в тексте. Уберите это слово."]}
        cb = self.press()
        data = [x.callback_data
                for row in cb.message.kbs[-1].inline_keyboard for x in row]
        self.assertIn("create_ad:strip", data, "кнопки исправления нет")

        # Нажимаем её — и товар уходит заново, уже без слова.
        Nova.posted = []

        class Tap(cb.__class__):
            pass

        cb2 = cb.__class__("create_ad:strip")
        cb2.message = cb.message
        asyncio.run(C.strip_and_create(cb2, self.fsm, None))
        body = self.created_body()
        self.assertTrue(body, "второй заход до панели не дошёл")
        self.assertNotIn("валют", str(body.get("content", "")).lower())
        self.assertIn("Аккаунт", str(body.get("content", "")))

    def test_the_removed_word_is_named_not_swallowed(self):
        """Правка чужого текста молча — не то, за что нажимали.

        Смотрим ИМЕННО сообщение чистки: слово есть и в тексте отказа выше,
        и проверка по всей переписке проходила бы при любом молчании."""
        import asyncio
        from handlers import create_ad as C

        Nova.refuse_first = {"content": [
            "Запрещено использовать «валютой» в тексте."]}
        cb = self.press()
        before = len(cb.message.texts)
        cb2 = cb.__class__("create_ad:strip")
        cb2.message = cb.message
        asyncio.run(C.strip_and_create(cb2, self.fsm, None))
        told = " ".join(cb.message.texts[before:])
        self.assertIn("Убрал из описания", told)
        self.assertIn("валютой", told)

    def test_endings_go_with_the_word(self):
        """Панель ищет подстроку, а не словоформу. Убрав ровно «валют», мы
        оставили бы «валютой» — и получили бы тот же отказ вторым заходом,
        то есть кнопка выглядела бы работающей, не работая."""
        import asyncio
        from handlers import create_ad as C

        Nova.refuse_first = {"content": [
            "Запрещено использовать «валют» в тексте."]}
        cb = self.press()
        data = [x.callback_data
                for row in cb.message.kbs[-1].inline_keyboard for x in row]
        self.assertIn("create_ad:strip", data)
        Nova.posted = []
        cb2 = cb.__class__("create_ad:strip")
        cb2.message = cb.message
        asyncio.run(C.strip_and_create(cb2, self.fsm, None))
        sent = str(self.created_body().get("content", "")).lower()
        self.assertNotIn("валют", sent, "окончание осталось — панель откажет")

    def test_no_button_when_the_word_survives_inside_another(self):
        """Запрещённое может сидеть ВНУТРИ другого слова — целиком его не
        выкинешь. Предлагать «убрать и создать», не убрав, значит обещать
        исход, которого не будет."""
        Nova.refuse_first = {"content": [
            "Запрещено использовать «нутриигров» в тексте."]}
        cb = self.press()
        data = [x.callback_data
                for row in cb.message.kbs[-1].inline_keyboard for x in row]
        self.assertNotIn("create_ad:strip", data,
                         "обещали убрать то, что убрать нечем")

    def test_no_button_when_nothing_would_be_left(self):
        """Пустое описание — не исправленный товар."""
        for f in ITEM_FIELDS:
            if f["attribute"] == "content":
                was, f["value"] = f["value"], "розыгрыш"
                try:
                    Nova.refuse_first = {"content": [
                        "Запрещено использовать «розыгрыш» в тексте."]}
                    cb = self.press()
                    data = [x.callback_data for row in
                            cb.message.kbs[-1].inline_keyboard for x in row]
                    self.assertNotIn("create_ad:strip", data)
                finally:
                    f["value"] = was
                return
        self.fail("в образце нет описания")

    def test_a_dead_panel_does_not_report_success(self):
        old = P.PANEL_URL
        try:
            P.PANEL_URL = "http://127.0.0.1:1"      # никто не слушает
            cb = self.press()
        finally:
            P.PANEL_URL = old
        said = cb.message.texts[-1]
        self.assertNotIn("✅ <b>Товар создан", said)


if __name__ == "__main__":
    unittest.main()
