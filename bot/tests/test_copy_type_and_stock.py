"""Тип выдачи и остатки — без ручного ввода.

Два места, где продавец ещё вписывал руками:

* **тип выдачи.** Он есть у объявления в Integration API (`type =
  auto-delivery`), и словарь у панели ТОТ ЖЕ: отчёт бота печатает «надпись
  (значение)», и у живого товара там стояло «Авто-выдача (auto-delivery)».
  Совпадение буквой, а не по смыслу, — значит брать можно;
* **остатки.** Позиции авто-выдачи бот придумать не может: это сам товар.
  Но список, присланный руками ОДИН раз, запоминается и кладётся дальше
  сам.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402
from handlers import panel_items as PI                     # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TheDeliveryTypeComesFromTheMarketplace(unittest.TestCase):
    """Живой ответ 08.09: у объявления `type = auto-delivery`, а панель тем
    же словом называет значение своего поля «Тип выдачи»."""

    def test_the_source_type_is_offered_to_the_form(self):
        from handlers import create_ad as C
        options = [{"label": "Мгновенная выдача", "value": "instant"},
                   {"label": "Авто-выдача", "value": "auto-delivery"},
                   {"label": "Ручная выдача", "value": "manual"}]
        got, how = C._pick_option(options, "auto-delivery", "", [], "", "")
        self.assertEqual(got["value"], "auto-delivery")
        self.assertEqual(how, "номер образца")

    def test_a_type_the_form_does_not_know_is_not_forced(self):
        """Словарь мог разойтись — тогда вопрос честнее подстановки."""
        from handlers import create_ad as C
        options = [{"label": "Ручная выдача", "value": "manual"}]
        got, _how = C._pick_option(options, "auto-delivery", "", [], "", "")
        self.assertIsNone(got)


class TheStockListIsRememberedAfterTheFirstTime(unittest.TestCase):
    """Позиции авто-выдачи бот не выдумывает — это сам товар. Но список,
    присланный руками один раз, дальше кладётся сам."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")
        import features
        self.features = features
        self._shown = features.ad_templates_shown
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def test_the_first_list_becomes_the_default(self):
        self.assertTrue(PI._remember_stock(7, ["KEY-1111", "KEY-2222"]))
        self.assertEqual(storage.get_copy_stock(7), ["KEY-1111", "KEY-2222"])

    def test_a_later_list_does_not_overwrite_it_silently(self):
        """Заготовка уходит живым покупателям. Подменить её тем, что
        продавец прислал одному товару, значит подменить молча."""
        PI._remember_stock(7, ["KEY-1111"])
        self.assertFalse(PI._remember_stock(7, ["ДРУГОЕ"]))
        self.assertEqual(storage.get_copy_stock(7), ["KEY-1111"])

    def test_it_is_not_remembered_for_whom_the_copy_is_closed(self):
        """Копия — только админам, и заготовка нужна ей одной."""
        self.features.ad_templates_shown = lambda uid: False
        self.assertFalse(PI._remember_stock(7, ["KEY-1111"]))
        self.assertEqual(storage.get_copy_stock(7), [])

    def test_a_broken_storage_does_not_eat_the_report(self):
        """Остатки уже добавлены. Исключение отсюда съело бы отчёт о них."""
        was = storage.set_copy_stock

        def boom(uid, rows):
            raise RuntimeError("диск")

        storage.set_copy_stock = boom
        try:
            self.assertFalse(PI._remember_stock(7, ["KEY-1111"]))
        finally:
            storage.set_copy_stock = was


class TheHandlerActuallyWiresItUp(unittest.TestCase):
    """Проверка самой проводки: `_remember_stock` можно вызвать правильно
    в тесте и забыть позвать в обработчике — снаружи это «прислал список,
    а он не запомнился»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._blob = storage._BLOBS["settings"]
        storage._BLOBS["settings"] = os.path.join(self.tmp.name, "s.json")
        import features
        self.features = features
        self._shown = features.ad_templates_shown
        features.ad_templates_shown = lambda uid: True

    def tearDown(self):
        storage._BLOBS["settings"] = self._blob
        self.features.ad_templates_shown = self._shown
        self.tmp.cleanup()

    def send(self, text="KEY-1111\nKEY-2222\nKEY-3333", shows=None):
        class Sent:
            def __init__(s):
                s.texts: list = []

            async def edit_text(s, t, reply_markup=None, **kw):
                s.texts.append(t)
                return s

        class Msg:
            def __init__(s):
                s.text = text
                s.from_user = type("U", (), {"id": 7})()
                s.sent = Sent()

            async def answer(s, t, reply_markup=None, **kw):
                s.sent.texts.append(t)
                return s.sent

        class FSM:
            def __init__(s):
                s.data = {"item_id": "250730"}

            async def get_data(s):
                return dict(s.data)

            async def clear(s):
                s.data = {}

        class Api:
            added: list = []
            # Сколько позиций маркетплейс покажет при ПЕРЕЧИТЫВАНИИ. У
            # живого случая 08.09 отправка прошла без ошибки, а обратно
            # он не отдал ни одной.
            shows: int | None = None

            async def get_ad(s, ad_id):
                return {"data": {"type": "auto-delivery"}}

            async def add_ad_items(s, ad_id, items):
                Api.added.append(list(items))
                return {"data": {"added": len(items)}}

            async def get_ad_items(s, ad_id, cursor=None):
                rows = [r for batch in Api.added for r in batch]
                if Api.shows is not None:
                    rows = rows[:Api.shows]
                return {"data": [{"status": "available", "value": r}
                                 for r in rows]}

        self.Api = Api
        Api.added, Api.shows = [], shows
        m = Msg()
        run(PI.item_stock_save(m, FSM(), Api()))
        return m.sent.texts[-1], Api.added

    def test_the_list_reaches_the_marketplace_and_the_settings(self):
        said, added = self.send()
        self.assertEqual(added, [["KEY-1111", "KEY-2222", "KEY-3333"]])
        self.assertEqual(storage.get_copy_stock(7),
                         ["KEY-1111", "KEY-2222", "KEY-3333"])

    def test_and_the_seller_is_told_it_was_remembered(self):
        """Эти строки уходят живым покупателям. Молчаливая заготовка
        однажды уедет вместо товара."""
        said, _added = self.send()
        self.assertIn("Запомнил этот список", said)
        self.assertIn("получит именно эти строки", said)

    def test_a_list_the_marketplace_did_not_take_is_not_called_done(self):
        """Живой случай 08.09: три позиции ушли без ошибки, маркетплейс
        отдал ноль, а публикация отказала `empty_stock`. «✅ Готово —
        добавлено позиций: 3» здесь было бодрым враньём."""
        said, added = self.send(shows=0)
        self.assertEqual(added, [["KEY-1111", "KEY-2222", "KEY-3333"]],
                         "отправить всё равно пробуем")
        self.assertNotIn("Готово", said)
        self.assertIn("не встал", said)
        self.assertIn("отправлено 3, а в наличии 0", said)
        self.assertNotIn("можно отправить на модерацию", said)

    def test_and_it_shows_what_the_marketplace_answered(self):
        """Причину знает только он: наш отчёт её не содержит."""
        said, _added = self.send(shows=0)
        self.assertIn("Маркетплейс ответил", said)
        self.assertIn("added", said, said)

    def test_but_the_diagnostic_is_advised_only_to_the_owner(self):
        """`/stock_debug` продавцу скрыта, и на скрытую команду бот
        отвечает то же, что на несуществующую. Совет набрать её — это
        совет невозможного; владельцу тот же экран обязан подсказать."""
        was = storage.is_admin
        storage.is_admin = lambda uid: False
        try:
            self.assertNotIn("/stock_debug", self.send(shows=0)[0])
        finally:
            storage.is_admin = was
        storage.is_admin = lambda uid: True
        try:
            self.assertIn("/stock_debug", self.send(shows=0)[0])
        finally:
            storage.is_admin = was

    def test_a_list_that_did_not_land_is_not_remembered_as_the_default(self):
        """Заготовка кладётся каждому новому товару. Запомнить ту, что
        маркетплейс не принял, значит получать пустой остаток каждый раз."""
        self.send(shows=0)
        self.assertEqual(storage.get_copy_stock(7), [])

    def test_a_second_list_is_not_announced_as_remembered(self):
        """Заготовка уже есть, и её не подменяли — обещать обратное значит
        соврать на ровном месте."""
        storage.set_copy_stock(7, ["СТАРОЕ"])
        said, _added = self.send()
        self.assertNotIn("Запомнил этот список", said)
        self.assertEqual(storage.get_copy_stock(7), ["СТАРОЕ"])


class TheStockListIsReadByOnePlace(unittest.TestCase):
    """Ответ `/ads/{id}/items` разбирали четверо, и каждый по-своему.

    Один смотрел только в `data`, второй — ещё и в `items`. Разница
    вылезла живьём 08.09: копия отправила три позиции и сказала «в
    наличии их нет», потому что смотрела в ключ, которого в ответе не
    было. Теперь читает одно место, и оно проверяется на всех формах,
    какие маркетплейс присылал.
    """

    def free(self, payload):
        from orderfields import ad_items_free
        return ad_items_free(payload)

    def test_the_usual_envelope(self):
        self.assertEqual(len(self.free({"data": [{"status": "available"}]})), 1)

    def test_the_items_key_counts_too(self):
        """Так его читает `ad_stock` — и читал ещё до этой правки."""
        self.assertEqual(len(self.free({"items": [{"status": "available"}]})), 1)

    def test_a_bare_list_counts_too(self):
        self.assertEqual(len(self.free([{"status": "available"}])), 1)

    def test_a_row_without_a_status_is_free(self):
        """У только что положенной позиции статуса может не быть вовсе.
        Считать её проданной значит объявить новый товар пустым."""
        self.assertEqual(len(self.free({"data": [{"value": "KEY-1"}]})), 1)

    def test_a_sold_row_is_not_free(self):
        rows = [{"status": "sold"}, {"status": "available"}]
        self.assertEqual(len(self.free({"data": rows})), 1)

    def test_the_marketplaces_other_words_for_free(self):
        """Слова его, не наши: перечень взят с запасом, потому что
        «не то слово» здесь выглядит как «остатка нет»."""
        for word in ("active", "in_stock", "new", "AVAILABLE"):
            with self.subTest(word=word):
                self.assertEqual(
                    len(self.free({"data": [{"status": word}]})), 1, word)

    def test_nothing_at_all_is_not_a_crash(self):
        for payload in ({}, {"data": None}, [], None, "текст"):
            with self.subTest(payload=payload):
                self.assertEqual(self.free(payload), [])


class TheStockDiagnosticShowsWhatTheMarketplaceSays(unittest.TestCase):
    """`/stock_debug` — ответ на живой случай 08.09.

    Копия отправила три позиции, маркетплейс не возразил, публикация тут
    же отказала `empty_stock`. Догадываться о форме тела запроса тут
    бессмысленно: команда печатает, что мы отправили и что он ответил.
    """

    class Api:
        rows: list = []
        added: list = []
        boom: bool = False

        async def get_ad(self, ad_id):
            return {"data": {"type": "auto-delivery", "status": "unpublish",
                             "stock": 0}}

        async def get_ad_items(self, ad_id, cursor=None):
            if TheStockDiagnosticShowsWhatTheMarketplaceSays.Api.boom:
                raise RuntimeError("HTTP 500: сервер лёг")
            return {"data": list(
                TheStockDiagnosticShowsWhatTheMarketplaceSays.Api.rows)}

        async def add_ad_items(self, ad_id, items):
            TheStockDiagnosticShowsWhatTheMarketplaceSays.Api.added.append(
                (str(ad_id), list(items)))
            TheStockDiagnosticShowsWhatTheMarketplaceSays.Api.rows += [
                {"status": "available", "value": r} for r in items]
            return {"data": {"added": len(items)}}

    def setUp(self):
        self.Api.rows, self.Api.added, self.Api.boom = [], [], False

    def ask(self, text):
        said = []

        class Sent:
            async def edit_text(s, t, **kw):
                said.append(t)
                return s

        class Msg:
            def __init__(s):
                s.text = text
                s.from_user = type("U", (), {"id": 7})()

            async def answer(s, t, **kw):
                said.append(t)
                return Sent()

        run(PI.stock_debug(Msg(), self.Api()))
        return said[-1]

    def test_without_a_number_it_says_how_to_call_it(self):
        said = self.ask("/stock_debug")
        self.assertIn("/stock_debug 250845", said)
        self.assertEqual(self.Api.added, [], "спрашивать — не значит слать")

    def test_reading_alone_sends_nothing(self):
        """Диагностика, меняющая товар без спроса, — не диагностика."""
        self.Api.rows = [{"status": "available", "value": "KEY-1"}]
        said = self.ask("/stock_debug 250845")
        self.assertEqual(self.Api.added, [])
        self.assertIn("позиций: <b>1</b>", said)
        self.assertIn("свободных: <b>1</b>", said)

    def test_it_names_the_kind_and_the_status_of_the_item(self):
        """Вид решает, что вообще значит «остаток»."""
        said = self.ask("/stock_debug 250845")
        self.assertIn("auto-delivery", said)
        self.assertIn("unpublish", said)

    def test_an_empty_answer_is_printed_as_it_came(self):
        """Ровно тот случай, ради которого команда и написана: позиций
        нет, и единственное, что может объяснить почему, — сырой ответ."""
        said = self.ask("/stock_debug 250845")
        self.assertIn("позиций: <b>0</b>", said)
        self.assertIn("сырой ответ", said)

    def test_the_probe_sends_exactly_the_one_line(self):
        said = self.ask("/stock_debug 250845 KEY-ПРОБА")
        self.assertEqual(self.Api.added, [("250845", ["KEY-ПРОБА"])])
        self.assertIn("отправляю", said)
        self.assertIn("KEY-ПРОБА", said)
        self.assertIn("added", said, "сырой ответ маркетплейса")

    def test_and_it_re_reads_after_the_probe(self):
        """Ответ на отправку и то, что он отдаёт обратно, — разные вещи,
        и вся загадка живёт ровно между ними."""
        said = self.ask("/stock_debug 250845 KEY-ПРОБА")
        before, after = said.split("📮", 1)
        self.assertIn("позиций: <b>0</b>", before, "до отправки — пусто")
        self.assertIn("позиций: <b>1</b>", after, "после — та самая строка")
        self.assertIn("осталась у товара", said,
                      "строка живая — продавцу надо знать")

    def test_a_refusal_to_read_is_said_out_loud(self):
        self.Api.boom = True
        said = self.ask("/stock_debug 250845")
        self.assertIn("не прочиталось", said)
        self.assertIn("HTTP 500", said)


if __name__ == "__main__":
    unittest.main()
