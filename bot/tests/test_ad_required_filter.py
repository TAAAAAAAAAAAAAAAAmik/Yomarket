"""Поле, о котором панель молчит до самого отказа.

20.08, живой отказ на Apple Gift Card TRY 10 (TR):

    ⚠️ Ресурс найден, но есть ошибка валидации
    Панель не приняла:
    • filter__8: Поле Регион обязательно для заполнения.
    Обязательные: []

Два факта в этом отказе важнее самого отказа.

**У панели есть своё поле «Регион».** В `CLAUDE.md` записано обратное — «у
Юмаркета своего поля под регион нет, и выдача читает его из описания», — и
описание мы честно писали строкой «Регион кода: TR». Панель на это не
смотрит: у товара есть `filter__8`, и он обязательный.

**Обязательность зависит от раздела.** Форма создания отдаёт
`Обязательные: []` — по её правилам не обязательно ничего. Требование
появляется только после выбора категории, и узнать о нём заранее неоткуда.
Зато отказ называет поле прямым текстом — значит его можно спросить и
отправить товар заново. Прежний мастер вместо этого показывал простыню и
предлагал «создать вручную в панели»: тупик при известном выходе.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P              # noqa: E402
from handlers import create_ad as C            # noqa: E402

# Отчёт в том виде, в каком его собирает `panel_create_product_sync`: сначала
# отказ панели, следом наше «Отправлено». Разбирать надо первый объект.
REFUSAL = (
    "✅ Ресурс <b>items</b> найден!\n"
    "Поля: <code>['title', 'category', 'filter__8', 'price']</code>\n"
    "Обязательные: <code>[]</code>\n\n"
    'Ошибка 422:\n<code>{"filter__8": '
    '["Поле Регион обязательно для заполнения."]}</code>\n\n'
    'Отправлено: <code>{"title": "Apple Gift Card TRY 10 (TR)", '
    '"price": 60, "category": 4718}</code>'
)

FIELDS = [
    {"attribute": "category", "label": "Категория", "options": []},
    {"attribute": "filter__8", "label": "Регион", "options": [
        {"label": "TR", "value": 7}, {"label": "US", "value": 8}]},
]


class FSM:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.cleared = True
        self.data = {}

    async def set_state(self, s):
        self.data["_state"] = s


class Msg:
    def __init__(self):
        self.texts: list[str] = []
        self.chat = type("Chat", (), {"id": 1})()
        self.message_id = 1

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return self

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return self

    @property
    def last(self):
        return self.texts[-1] if self.texts else ""


class TheRefusalNamesTheFieldAndWeReadIt(unittest.TestCase):
    """Продавцу нужен текст, мастеру — имя поля. Ответы разные, и разбор
    поэтому тоже свой."""

    def test_the_field_the_panel_asked_for_is_found(self):
        self.assertEqual(P.validation_fields(REFUSAL), ["filter__8"])

    def test_our_own_payload_is_not_mistaken_for_a_refusal(self):
        """«Отправлено: {…}» лежит в том же отчёте, и по нему тоже можно
        пройтись словарём — получив «поле title» на ровном месте."""
        sent = ('Отправлено: <code>{"title": "Apple Gift Card", '
                '"price": 60}</code>')
        self.assertEqual(P.validation_fields(sent), [])

    def test_a_report_without_a_refusal_names_nothing(self):
        self.assertEqual(P.validation_fields("✅ Товар создан"), [])
        self.assertEqual(P.validation_fields(""), [])

    def test_several_fields_are_all_named(self):
        raw = ('{"filter__8": ["Поле Регион обязательно."], '
               '"content": ["Ссылки запрещены."]}')
        self.assertEqual(sorted(P.validation_fields(raw)),
                         ["content", "filter__8"])

    def test_the_explanation_still_reads_as_before(self):
        """Разбор на имена не должен был тронуть объяснение продавцу."""
        self.assertIn("Регион обязательно", P.explain_validation(REFUSAL))


class ARefusedFieldIsAskedNotSurrenderedTo(unittest.TestCase):
    """Панель назвала недостающее поле — значит выход известен. Прежний
    мастер на этом месте предлагал завести товар руками: 476 номиналов
    Apple по 31 региону руками не заводятся."""

    def setUp(self):
        import storage
        self.created: list = []
        self.asked: list = []
        self._undo: list = []

        def create(cookies, title, price, description, quantity=1,
                   category="", uid=None, extra=None, photo_path=None):
            self.created.append(dict(extra or {}))
            # Первый заход — отказ по региону; со спрошенным регионом — успех.
            if extra and "filter__8" in extra:
                return True, "12345"
            return False, REFUSAL

        async def render(msg, state, edit=True):
            data = await state.get_data()
            self.asked.append(data.get("current_attr"))

        for mod, name, val in (
            (storage, "get_panel_creds", lambda uid: {"cookies": "c=1"}),
            (P, "panel_create_product_sync", create),
            (P, "panel_publish_item_sync", lambda *a, **kw: (True, "ok")),
            (P, "panel_get_item_form_sync",
             lambda cookies: (True, {"resource": "items", "fields": FIELDS})),
            (C, "_render_select", render),
        ):
            self._undo.append((mod, name, getattr(mod, name, None)))
            setattr(mod, name, val)

    def tearDown(self):
        for mod, name, old in reversed(self._undo):
            setattr(mod, name, old)

    def values(self):
        return {"title": "Apple Gift Card TRY 10 (TR)", "price": 60,
                "description": "Код пополнения Apple ID", "quantity": 1,
                "category": "", "photo_path": None}

    def run_create(self, state, extra=None):
        msg = Msg()
        asyncio.run(C._panel_create_and_report(msg, 1, self.values(),
                                               extra=extra, state=state))
        return msg

    def test_the_seller_is_asked_for_the_region(self):
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        self.run_create(state, extra={"category": 4718})
        self.assertEqual(self.asked, ["filter__8"])

    def test_the_question_is_called_by_its_human_name(self):
        """`filter__8` продавцу не говорит ничего, «Регион» — говорит."""
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        msg = self.run_create(state, extra={"category": 4718})
        self.assertIn("Регион", msg.texts[-1])

    def test_what_was_already_chosen_is_not_asked_again(self):
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        self.run_create(state, extra={"category": 4718})
        self.assertEqual(state.data["chosen"], {"category": 4718})
        self.assertNotIn("category", state.data["select_queue"])

    def test_the_form_is_not_closed_while_the_question_stands(self):
        """Закрытая форма — это вопрос в пустоту: ответ уйдёт в никуда."""
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        self.run_create(state, extra={"category": 4718})
        self.assertFalse(state.cleared)

    def test_the_answer_goes_back_to_the_panel_and_the_item_is_created(self):
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        self.run_create(state, extra={"category": 4718})
        # Продавец выбрал регион — мастер досылает товар.
        asyncio.run(C._panel_create_and_report(
            Msg(), 1, self.values(),
            extra={"category": 4718, "filter__8": 7}, state=state))
        self.assertEqual(self.created[-1],
                         {"category": 4718, "filter__8": 7})

    def test_it_asks_once_and_does_not_loop(self):
        """Если и с ответом панель откажет, продавец получит отчёт, а не
        второй круг вопросов."""
        state = FSM({"form_fields": FIELDS, "form_resource": "items",
                     "refused_asked": True})
        msg = self.run_create(state, extra={"category": 4718})
        self.assertEqual(self.asked, [])
        self.assertIn("Регион", msg.last)          # отчёт, с объяснением
        self.assertTrue(state.cleared)

    def test_a_field_the_form_does_not_know_is_not_asked(self):
        """Спросить поле, которого нет в форме, значит спросить пустоту."""
        state = FSM({"form_fields": [FIELDS[0]], "form_resource": "items"})
        msg = self.run_create(state, extra={"category": 4718})
        self.assertEqual(self.asked, [])
        self.assertTrue(state.cleared)

    def test_without_a_form_it_is_read_before_asking(self):
        """Создание из плагина идёт мимо мастера — формы в состоянии нет."""
        state = FSM({})
        self.run_create(state, extra={"category": 4718})
        self.assertEqual(self.asked, ["filter__8"])

    def test_a_created_item_closes_the_form(self):
        state = FSM({"form_fields": FIELDS, "form_resource": "items"})
        self.run_create(state, extra={"category": 4718, "filter__8": 7})
        self.assertTrue(state.cleared)


class TheGiftCardTemplateHintsTheRegionItAlreadyKnows(unittest.TestCase):
    """Регион продавец выбрал на первом же шаге создания товара. Спрашивать
    его второй раз, уже под именем «Регион», — работа на ровном месте."""

    def source(self):
        import pathlib
        here = pathlib.Path(__file__).resolve().parent.parent
        return (here / "handlers" / "plugins.py").read_text()

    def test_the_region_travels_with_the_section_hints(self):
        self.assertIn("list(gift.autopick) + [region.lower()]", self.source())

    def test_an_ambiguous_region_is_still_asked(self):
        """Подсказка не решает за продавца: два подходящих варианта — вопрос.
        Регион здесь решает, какой код купит выдача."""
        options = [{"label": "TR (Турция)", "value": 7},
                   {"label": "TR (Кипр)", "value": 8}]
        self.assertIsNone(C._autopick_match(options, ["tr"]))

    def test_the_matching_region_is_taken(self):
        options = [{"label": "TR", "value": 7}, {"label": "US", "value": 8}]
        got = C._autopick_match(options, ["tr"])
        self.assertEqual(got["value"], 7)

    def test_an_exact_label_wins_over_a_longer_one(self):
        """«TR» и «TR (Турция)» — не двусмысленность: точное совпадение
        отвечает на вопрос, а частичное только похоже на ответ."""
        options = [{"label": "TR", "value": 7},
                   {"label": "TR (Турция)", "value": 8}]
        self.assertEqual(C._autopick_match(options, ["tr"])["value"], 7)


if __name__ == "__main__":
    unittest.main()
