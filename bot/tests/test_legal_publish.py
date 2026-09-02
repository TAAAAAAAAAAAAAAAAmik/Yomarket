"""Выкладка правовых документов на Telegraph.

Юридический текст правится вместе с кодом: тесты сверяют, что оферта
перечисляет те же тарифы, что продаёт бот. Но сверяют они ФАЙЛЫ, а продавец
читает опубликованную страницу — и разъехаться они могут молча. Скрипт
закрывает эту щель, и здесь проверяется то, на чём он способен соврать:

* **разметка.** Из `**Fragment (`fragment.com`)**` первая версия сделала
  текст со звёздочками: моноширинное забирало середину строки, и жирное
  больше не видело пары `**` целиком. В договоре это читается как опечатка
  в условии;
* **таблица.** Таблиц в Telegraph нет, а таблица «кому передаются данные» —
  юридическая суть. Разворачивается списком, и об этом говорится вслух;
* **подпись.** Ради неё скрипт и написан: имя без ссылки. Проверяется не
  тем, что мы отправили, а тем, что вернул сервер;
* **адрес.** Он вписан в бота руками. Второй запуск обязан править ту же
  страницу, а не создавать новую: новый адрес молча оставил бы кнопку
  «Политика конфиденциальности» на прежней странице.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# В КОНЕЦ, а не в начало: в `scripts/` однажды появится файл с именем,
# какое уже есть у бота, и вставленный первым он молча заслонил бы его —
# для всего прогона, а не только для этого файла.
REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.append(str(REPO / "scripts"))

import publish_legal as P                                  # noqa: E402


def texts(node):
    """Весь текст узла — то, что в итоге прочитает человек."""
    if isinstance(node, str):
        yield node
        return
    for child in node.get("children", []):
        yield from texts(child)


def flat(nodes) -> str:
    return " ".join(t for n in nodes for t in texts(n))


class TheMarkupSurvivesTheTrip(unittest.TestCase):
    """Разметка, доехавшая до страницы буквами, — это опечатка в договоре."""

    def test_the_title_comes_from_the_first_heading(self):
        title, nodes, _ = P.md_to_nodes("# Оферта\n\nтекст\n")
        self.assertEqual(title, "Оферта")
        self.assertNotIn("Оферта", flat(nodes), "заголовок остался и в теле")

    def test_headings_become_h3_and_h4(self):
        """В Telegraph заголовков ровно два, h1 и h2 он не принимает."""
        _t, nodes, _ = P.md_to_nodes("# З\n\n## Раздел\n\n### Подраздел\n")
        self.assertEqual([n["tag"] for n in nodes], ["h3", "h4"])

    def test_bold_around_code_does_not_leak_stars(self):
        """Живая строка из политики: `**Fragment (`fragment.com`)**`."""
        got = P.inline("**Fragment (`fragment.com`) и TON**")
        self.assertEqual(got[0]["tag"], "b")
        self.assertNotIn("*", flat(got))
        self.assertIn("fragment.com", flat(got))

    def test_the_code_inside_stays_code(self):
        got = P.inline("**Fragment (`fragment.com`)**")
        tags = [c.get("tag") for c in got[0]["children"]
                if not isinstance(c, str)]
        self.assertIn("code", tags, "моноширинное съелось жирным")

    def test_markup_inside_code_stays_literal(self):
        """Внутри обратных кавычек разметки нет — там всё буквально."""
        got = P.inline("`**не жирное**`")
        self.assertEqual(got, [{"tag": "code", "children": ["**не жирное**"]}])

    def test_a_hard_wrapped_paragraph_becomes_one(self):
        """Переносы в файле — вёрстка файла, а не текста договора."""
        _t, nodes, _ = P.md_to_nodes("# З\n\nодна строка\nвторая строка\n")
        self.assertEqual(flat(nodes), "одна строка вторая строка")

    def test_a_list_item_may_span_lines(self):
        _t, nodes, _ = P.md_to_nodes("# З\n\n- первый пункт\n  и его хвост\n"
                                     "- второй\n")
        ul = nodes[0]
        self.assertEqual(ul["tag"], "ul")
        self.assertEqual(len(ul["children"]), 2)
        self.assertEqual(flat([ul["children"][0]]), "первый пункт и его хвост")

    def test_a_rule_becomes_a_rule(self):
        _t, nodes, _ = P.md_to_nodes("# З\n\nа\n\n---\n\nб\n")
        self.assertIn({"tag": "hr"}, nodes)

    def test_no_raw_markdown_reaches_any_of_the_three_documents(self):
        """Самая широкая проверка здесь: настоящие файлы, весь текст.

        Она и поймала разъехавшийся разбор — придуманная строка в тесте
        такого сочетания не содержала."""
        for key, fname in P.DOCS:
            raw = (P.SRC / fname).read_text(encoding="utf-8")
            _t, nodes, _n = P.md_to_nodes(raw)
            said = flat(nodes)
            for mark in ("**", "`", "](", "|"):
                with self.subTest(doc=key, mark=mark):
                    self.assertNotIn(mark, said, f"{key}: разметка не разобрана")


class TheTableIsUnfoldedNotDropped(unittest.TestCase):
    """Таблиц в Telegraph нет. Таблица «кому передаются данные» — это
    перечень получателей персональных данных, то есть ровно то, ради чего
    политику и читают: потерять её значит опубликовать другой документ."""

    TABLE = ("# З\n\n"
             "| Кому | Что передаётся | Когда |\n"
             "|---|---|---|\n"
             "| Telegram | сообщения | всегда |\n"
             "| AppRoute | ключ API | при автовыдаче |\n")

    def test_every_cell_survives(self):
        _t, nodes, _ = P.md_to_nodes(self.TABLE)
        said = flat(nodes)
        for cell in ("Telegram", "сообщения", "всегда",
                     "AppRoute", "ключ API", "при автовыдаче"):
            with self.subTest(cell):
                self.assertIn(cell, said)

    def test_the_column_names_survive_too(self):
        """«всегда» без «когда» не значит ничего: смысл ячейке даёт
        заголовок столбца, а он в таблице сказан один раз."""
        _t, nodes, _ = P.md_to_nodes(self.TABLE)
        said = flat(nodes)
        self.assertIn("что передаётся", said.lower())
        self.assertIn("когда", said.lower())

    def test_it_becomes_a_list_with_a_row_per_item(self):
        _t, nodes, _ = P.md_to_nodes(self.TABLE)
        self.assertEqual(nodes[0]["tag"], "ul")
        self.assertEqual(len(nodes[0]["children"]), 2)

    def test_the_change_is_said_out_loud(self):
        """Молчаливое преобразование юридического текста недопустимо."""
        _t, _n, notes = P.md_to_nodes(self.TABLE)
        self.assertTrue(notes)
        self.assertIn("таблиц", " ".join(notes))

    def test_the_real_policy_has_one_and_it_is_announced(self):
        raw = (P.SRC / "privacy.md").read_text(encoding="utf-8")
        _t, _n, notes = P.md_to_nodes(raw)
        self.assertTrue(notes, "таблица в политике перестала замечаться")


class TheHandPasteVersionComesFromTheSameNodes(unittest.TestCase):
    """Вставлять руками — законный путь: ключа может не быть под рукой.

    Но текст для вставки обязан собираться из ТЕХ ЖЕ узлов, что уходят в
    API. Второй разбор того же файла — это второй источник правды, и
    однажды опубликованное перестанет совпадать с проверенным.

    Формата два, потому что редактор Telegraph понимает при вставке
    разметку: HTML сохраняет жирное и заголовки, простой текст — на случай,
    когда вставить получается только его.
    """

    SAMPLE = ("# З\n\n## Раздел\n\nтекст с **жирным** и `кодом`\n\n"
              "- пункт\n\n---\n")

    def nodes(self, raw=None):
        return P.md_to_nodes(raw or self.SAMPLE)[1]

    def test_the_html_keeps_the_formatting(self):
        got = P.nodes_to_html(self.nodes())
        self.assertIn("<h3>Раздел</h3>", got)
        self.assertIn("<b>жирным</b>", got)
        self.assertIn("<code>кодом</code>", got)
        self.assertIn("<li>пункт</li>", got)
        self.assertIn("<hr>", got)

    def test_the_html_carries_no_raw_markdown(self):
        self.assertNotIn("**", P.nodes_to_html(self.nodes()))

    def test_someone_elses_angle_bracket_does_not_break_the_html(self):
        """В договорах встречается «<», и вставленный как есть он съел бы
        кусок текста."""
        got = P.nodes_to_html(P.md_to_nodes("# З\n\nусловие a < b\n")[1])
        self.assertIn("a &lt; b", got)

    def test_the_plain_text_carries_no_tags(self):
        got = P.nodes_to_text(self.nodes())
        for mark in ("<", ">", "**", "`"):
            with self.subTest(mark):
                self.assertNotIn(mark, got)

    def test_the_plain_text_keeps_the_words(self):
        got = P.nodes_to_text(self.nodes())
        for word in ("Раздел", "жирным", "кодом", "пункт"):
            with self.subTest(word):
                self.assertIn(word, got)

    def test_a_list_stays_readable_without_markup(self):
        self.assertIn("— пункт", P.nodes_to_text(self.nodes()))

    def test_the_table_survives_both_ways(self):
        """Ради неё всё и проверяется: перечень получателей персональных
        данных потерять нельзя ни в одном формате."""
        raw = (P.SRC / "privacy.md").read_text(encoding="utf-8")
        nodes = self.nodes(raw)
        for render in (P.nodes_to_html, P.nodes_to_text):
            with self.subTest(render.__name__):
                said = render(nodes)
                self.assertIn("AppRoute", said)
                self.assertIn("только при автовыдаче звёзд", said)


class Bench(unittest.TestCase):
    """Подставной Telegraph: настоящий отсюда недоступен, а проверять надо
    то, что уходит на сервер, и то, что скрипт делает с ответом."""

    def setUp(self):
        self.calls: list[tuple[str, dict]] = []
        self.pages: dict[str, dict] = {}
        self.fail_on = ""
        self.author_says = P.AUTHOR
        self.url_says = ""
        self._call = P.call
        self._record = P.RECORD
        self.tmp = tempfile.TemporaryDirectory()
        P.RECORD = pathlib.Path(self.tmp.name) / "published.json"

        def fake(method, **params):
            self.calls.append((method, params))
            if self.fail_on == method:
                raise P.TelegraphError(f"{method}: PAGE_ACCESS_DENIED")
            if method == "createPage":
                path = f"Stranica-{len(self.pages) + 1}"
                self.pages[path] = dict(params, path=path)
                return {"path": path, "url": f"https://telegra.ph/{path}"}
            if method == "editPage":
                path = params["path"]
                self.pages[path] = dict(params, path=path)
                return {"path": path, "url": f"https://telegra.ph/{path}"}
            if method == "getPage":
                saved = self.pages.get(params["path"], {})
                return {"path": params["path"], "title": saved.get("title"),
                        "author_name": self.author_says,
                        "author_url": self.url_says}
            raise AssertionError(f"неожиданный вызов {method}")

        P.call = fake

    def tearDown(self):
        P.call, P.RECORD = self._call, self._record
        self.tmp.cleanup()

    def run_(self, *argv, token="tok"):
        """Запуск с подставным Telegraph. Напечатанное складывается в
        `self.said` — вывод скрипта тоже обещание, и проверять его надо."""
        import contextlib
        import io as _io
        old = os.environ.get("TELEGRAPH_TOKEN")
        if token is None:
            os.environ.pop("TELEGRAPH_TOKEN", None)
        else:
            os.environ["TELEGRAPH_TOKEN"] = token
        out, err = _io.StringIO(), _io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return P.main(list(argv))
        finally:
            self.said = out.getvalue() + err.getvalue()
            if old is None:
                os.environ.pop("TELEGRAPH_TOKEN", None)
            else:
                os.environ["TELEGRAPH_TOKEN"] = old

    def sent(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


class TheBylineIsAPlainName(Bench):
    """Ради этого скрипт и написан: страница, созданная руками, берёт
    автором ник создателя да ещё и ссылкой на его профиль."""

    def test_the_author_name_is_ours(self):
        self.assertEqual(self.run_(), 0)
        for params in self.sent("createPage"):
            self.assertEqual(params["author_name"], "YooMarket Manager")

    def test_the_name_carries_no_link(self):
        self.run_()
        for params in self.sent("createPage"):
            self.assertEqual(params["author_url"], "")

    def test_the_link_field_is_sent_and_not_omitted(self):
        """«Поля нет» и «поле пустое» — разные вещи, и какая из них снимает
        ссылку, мы решать не вправе: шлём пустым и перечитываем."""
        self.run_()
        for params in self.sent("createPage"):
            self.assertIn("author_url", params)

    def test_a_wrong_author_on_the_page_is_a_failure(self):
        """Ответ на публикацию собирает наш же запрос. Показывают продавцу
        то, что лежит на сервере, — его и сверяем."""
        self.author_says = "√α×°|¤"
        self.assertEqual(self.run_(), 1)

    def test_a_name_that_stayed_a_link_is_a_failure_too(self):
        self.url_says = "https://t.me/somebody"
        self.assertEqual(self.run_(), 1)

    def test_the_page_is_read_back_at_all(self):
        self.run_()
        self.assertEqual(len(self.sent("getPage")), len(P.DOCS))


class TheAddressStaysTheSame(Bench):
    """Адрес вписан в бота руками, и бот его не проверяет: новая страница
    вместо правки оставила бы кнопку на прежней, с прежней подписью."""

    def test_the_second_run_edits_instead_of_creating(self):
        self.run_()
        self.assertEqual(len(self.sent("createPage")), len(P.DOCS))
        # Документ изменился — иначе скрипт законно ничего не шлёт.
        rec = json.loads(P.RECORD.read_text(encoding="utf-8"))
        for key in rec:
            rec[key]["sha"] = "другое"
        P.RECORD.write_text(json.dumps(rec), encoding="utf-8")
        self.calls.clear()
        self.run_()
        self.assertEqual(len(self.sent("createPage")), 0, "создал заново")
        self.assertEqual(len(self.sent("editPage")), len(P.DOCS))

    def test_the_url_does_not_change(self):
        self.run_()
        first = json.loads(P.RECORD.read_text(encoding="utf-8"))
        self.run_()
        self.assertEqual(json.loads(P.RECORD.read_text(encoding="utf-8")),
                         first)

    def test_an_unchanged_document_is_not_sent_again(self):
        """Второй запуск подряд не переписывает то, что не менялось."""
        self.run_()
        self.calls.clear()
        self.run_()
        self.assertEqual(self.sent("editPage"), [])
        self.assertEqual(len(self.sent("getPage")), len(P.DOCS),
                         "перестал перечитывать — сверять стало нечем")

    def test_the_record_names_every_document(self):
        self.run_()
        rec = json.loads(P.RECORD.read_text(encoding="utf-8"))
        self.assertEqual(set(rec), {k for k, _f in P.DOCS})

    def test_the_record_keeps_no_secret(self):
        """Файл лежит в репозитории, а ключ — единственное право править
        эти страницы."""
        self.run_()
        self.assertNotIn("tok", P.RECORD.read_text(encoding="utf-8"))

    def test_the_key_is_never_printed(self):
        """Обычный запуск делают при людях и снимают на экран."""
        self.run_(token="секретный-ключ")
        self.assertNotIn("секретный-ключ", self.said)


class ARefusalIsNeverSwallowed(Bench):
    def test_a_refusal_is_reported_in_telegraphs_own_words(self):
        """«Не вышло» без слов сервера — отписка: причина у Telegraph одна
        на все отказы только в нашем пересказе."""
        self.fail_on = "createPage"
        self.assertEqual(self.run_(), 1)
        self.assertIn("PAGE_ACCESS_DENIED", self.said)

    def test_a_refusal_does_not_get_written_down_as_success(self):
        self.fail_on = "createPage"
        self.run_()
        rec = json.loads(P.RECORD.read_text(encoding="utf-8"))
        self.assertEqual(rec, {}, "запомнил страницу, которой нет")

    def test_a_bad_envelope_is_a_refusal_not_a_crash(self):
        """Незнакомый конверт — отказ, а не KeyError на чужом поле."""
        with self.assertRaises(P.TelegraphError) as e:
            P.call = self._call
            import urllib.request

            class Fake:
                def read(s):
                    return b'{"result": 1}'

                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False

            old = urllib.request.urlopen
            urllib.request.urlopen = lambda *a, **k: Fake()
            try:
                P.call("getPage", path="x")
            finally:
                urllib.request.urlopen = old
        self.assertIn("конверт", str(e.exception))


class ItNeverTouchesTheNetworkByAccident(Bench):
    def test_a_dry_run_sends_nothing(self):
        self.assertEqual(self.run_("--dry-run", token=None), 0)
        self.assertEqual(self.calls, [])

    def test_without_a_token_it_says_so_and_stops(self):
        self.assertEqual(self.run_(token=None), 2)
        self.assertEqual(self.calls, [], "пошёл в сеть без ключа")

    def test_and_says_how_to_get_one(self):
        """Отказ без выхода — тупик: правило проекта «не советуйте
        невозможного» читается здесь как «советуйте выполнимое»."""
        self.run_(token=None)
        self.assertIn("--new-account", self.said)

    def test_the_advice_names_an_interpreter_that_exists(self):
        """Живой отказ на сервере: `python: command not found`. Совет был
        написан словом «python», а в Debian и Ubuntu такой команды нет —
        есть `python3`, а у бота свой питон в окружении. Команда собирается
        из того, чем скрипт запущен."""
        self.run_(token=None)
        self.assertIn(sys.executable, self.said)

    def test_the_ellipsis_from_the_instructions_is_not_a_key(self):
        """`export TELEGRAPH_TOKEN=…`, вставленное вместе с остальными
        строками, кладёт в переменную многоточие. Отказ Telegraph про
        «неверный ключ» отправил бы искать беду не там."""
        self.assertEqual(self.run_(token="…"), 2)
        self.assertIn("многоточие", self.said)
        self.assertEqual(self.calls, [], "пошёл в сеть с многоточием")

    def test_a_new_address_is_never_announced_for_a_page_that_failed(self):
        """Страница, не прошедшая сверку, адресом не является: советовать
        вписать её в бота значит советовать вписать неизвестно что."""
        self.author_says = "чужой ник"
        self.assertEqual(self.run_(), 1)
        self.assertNotIn("Впиши их в бота", self.said)


class TheRequestItselfIsShapedRight(unittest.TestCase):
    """Единственная часть, которую отсюда не проверить настоящим ответом:
    Telegraph закрыт сетевой политикой окружения. Проверяется хотя бы то,
    что нарушить его пределы мы не пытаемся."""

    def test_the_content_goes_in_the_body_not_the_url(self):
        """26 КБ политики в адресной строке не поместятся ни у кого."""
        seen = {}

        class Fake:
            def read(s):
                return b'{"ok": true, "result": {}}'

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        import urllib.request
        old = urllib.request.urlopen

        def catch(req, **kw):
            seen["url"] = req.full_url
            seen["body"] = req.data
            seen["method"] = req.get_method()
            return Fake()

        urllib.request.urlopen = catch
        try:
            P.call("createPage", access_token="t", content="х" * 30000)
        finally:
            urllib.request.urlopen = old
        self.assertEqual(seen["method"], "POST")
        self.assertNotIn("content=", seen["url"])
        self.assertGreater(len(seen["body"]), 30000)

    def test_empty_values_are_sent_and_only_none_is_dropped(self):
        """`author_url=""` — это «сними ссылку», а не «поля нет»."""
        seen = {}

        class Fake:
            def read(s):
                return b'{"ok": true, "result": {}}'

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        import urllib.request
        old = urllib.request.urlopen
        urllib.request.urlopen = lambda req, **kw: (
            seen.__setitem__("body", req.data.decode()), Fake())[1]
        try:
            P.call("createPage", author_url="", short_name=None)
        finally:
            urllib.request.urlopen = old
        self.assertIn("author_url=", seen["body"])
        self.assertNotIn("short_name", seen["body"])


class TheExportTouchesNothingAndSaysTheByline(Bench):
    def test_it_writes_both_formats_for_every_document(self):
        out = pathlib.Path(self.tmp.name) / "paste"
        self.assertEqual(self.run_("--export", str(out), token=None), 0)
        for key, _f in P.DOCS:
            with self.subTest(key):
                self.assertTrue((out / f"{key}.html").exists())
                self.assertTrue((out / f"{key}.txt").exists())

    def test_it_names_what_to_type_in_the_author_field(self):
        """Подпись на Telegraph — отдельное поле, а не часть текста: не
        сказать про неё значит не сделать того, ради чего всё затевалось."""
        out = pathlib.Path(self.tmp.name) / "paste"
        self.run_("--export", str(out), token=None)
        self.assertIn("YooMarket Manager", self.said)
        self.assertIn("сылку не добавляй", self.said)

    def test_it_never_goes_online(self):
        out = pathlib.Path(self.tmp.name) / "paste"
        self.run_("--export", str(out), token=None)
        self.assertEqual(self.calls, [])

    def test_the_table_change_is_announced_here_too(self):
        out = pathlib.Path(self.tmp.name) / "paste"
        self.run_("--export", str(out), token=None)
        self.assertIn("таблиц", self.said)


class ThePastePageIsRebuiltFromTheDocuments(unittest.TestCase):
    """Страница с кнопками собирается из шаблона рядом с документами — по
    тому же уговору, что и `page.tpl` у общей страницы.

    Собирается, а не лежит готовой: страница, которую нечем пересобрать,
    после первой же правки оферты станет показывать прежние тарифы, и
    заметить это будет нечем.
    """

    def page(self) -> str:
        ready = [(key, *P.prepare(fname)) for key, fname in P.DOCS]
        return P.paste_page(ready)

    def test_every_document_is_on_it(self):
        got = self.page()
        for title in ("Пользовательское соглашение", "Договор-оферта",
                      "Политика конфиденциальности"):
            with self.subTest(title):
                self.assertIn(title, got)

    def test_it_names_the_byline_to_type(self):
        """Ради подписи всё и затевалось: не сказать про неё значит отдать
        страницу, которая не решает задачу."""
        self.assertIn("YooMarket Manager", self.page())

    def test_each_document_has_its_own_copy_button(self):
        got = self.page()
        for key, _f in P.DOCS:
            with self.subTest(key):
                self.assertIn(f'data-copy="{key}"', got)
                self.assertIn(f'id="doc-{key}"', got)

    def test_the_table_warning_is_on_the_page(self):
        self.assertIn("таблиц", self.page())

    def test_no_raw_markdown_reaches_the_page(self):
        self.assertNotIn("**", self.page())

    def test_a_template_without_a_slot_is_refused(self):
        """Шаблон правят руками, и место для документов из него однажды
        пропадёт. Молча отдать страницу без текста нельзя."""
        import tempfile as _tf
        old = P.SRC
        try:
            P.SRC = pathlib.Path(_tf.mkdtemp())
            (P.SRC / P.PASTE_TPL).write_text("<p>пусто</p>", encoding="utf-8")
            with self.assertRaises(SystemExit):
                P.paste_page([])
        finally:
            P.SRC = old


class TheLimitsAreCheckedHere(unittest.TestCase):
    def test_all_three_documents_fit(self):
        """64 КБ — предел Telegraph. Отказ по размеру приходит так, что по
        нему не понять, насколько перебрали."""
        for key, fname in P.DOCS:
            with self.subTest(key):
                _raw, _t, _n, _notes, size = P.prepare(fname)
                self.assertLess(size, P.CONTENT_LIMIT)

    def test_a_document_without_a_title_is_refused(self):
        """Telegraph требует заголовок, а выдумывать его нельзя."""
        import tempfile as _tf
        old = P.SRC
        try:
            P.SRC = pathlib.Path(_tf.mkdtemp())
            (P.SRC / "x.md").write_text("без заголовка\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                P.prepare("x.md")
        finally:
            P.SRC = old


if __name__ == "__main__":
    unittest.main()
