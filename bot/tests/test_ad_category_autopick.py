"""Раздел витрины, выбранный ботом за продавца.

Товары по номиналам Robux заводятся из плагина пачкой, и раздел у них один
и тот же. Выбирать «Roblox» руками по каждому номиналу — работа, которую
бот может сделать сам.

Опасность здесь ровно одна и она не про удобство: **раздел решает, где
покупатель увидит товар**. Молча ошибиться значит выставить код Robux среди
аккаунтов и узнать об этом по отсутствию продаж — то есть получить тихую
поломку вместо отказа. Поэтому автовыбор срабатывает только при
единственном подходящем варианте, а выбранное показывается продавцу.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers import create_ad as C          # noqa: E402


def opts(*labels) -> list[dict]:
    return [{"label": l, "value": i} for i, l in enumerate(labels, start=1)]


class OnlyAnUnambiguousSectionIsChosenWithoutAsking(unittest.TestCase):
    def pick(self, options, words):
        got = C._autopick_match(options, words)
        return None if got is None else got["label"]

    def test_the_only_exact_match_is_taken(self):
        self.assertEqual(self.pick(opts("Аккаунты", "Robux"), ["robux"]),
                         "Robux")

    def test_a_single_partial_match_is_taken_too(self):
        """В панели раздел зовётся длиннее, чем слово: «Roblox — Robux»."""
        self.assertEqual(self.pick(opts("Аккаунты", "Roblox — Robux"),
                                   ["robux"]), "Roblox — Robux")

    def test_case_does_not_matter(self):
        self.assertEqual(self.pick(opts("ROBUX"), ["robux"]), "ROBUX")

    def test_two_matches_are_not_resolved_by_taking_the_first(self):
        """Раздел решает, где покупатель увидит товар. Догадка здесь — это
        товар не на своей полке, и узнаётся это по тишине в продажах."""
        self.assertIsNone(self.pick(opts("Robux GL", "Robux RU"), ["robux"]))

    def test_nothing_matching_asks_instead_of_inventing(self):
        self.assertIsNone(self.pick(opts("Аккаунты", "Ключи"), ["robux"]))

    def test_the_narrow_word_wins_over_the_wide_one(self):
        """«roblox» подошло бы и аккаунтам, и подарочным картам. Порядок слов
        не декоративный: он и отличает валюту от всего остального."""
        got = self.pick(opts("Roblox аккаунты", "Roblox Robux", "Roblox карты"),
                        ["robux", "roblox"])
        self.assertEqual(got, "Roblox Robux")

    def test_the_wide_word_still_helps_when_the_narrow_one_misses(self):
        """Отдельного раздела под валюту может не быть вовсе."""
        self.assertEqual(self.pick(opts("Steam", "Roblox"), ["robux", "roblox"]),
                         "Roblox")

    def test_an_exact_name_beats_a_longer_one_containing_it(self):
        self.assertEqual(self.pick(opts("Robux", "Robux оптом"), ["robux"]),
                         "Robux")

    def test_no_words_at_all_changes_nothing(self):
        """Обычное создание товара идёт без подсказки — и должно спрашивать."""
        self.assertIsNone(self.pick(opts("Robux"), []))
        self.assertIsNone(self.pick(opts("Robux"), None))


class FSM:
    def __init__(self, data):
        self.data = dict(data)
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.cleared = True

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


class TheWizardStopsAskingWhatItAlreadyKnows(unittest.TestCase):
    """Проводка: подсказка может быть верной, а до мастера не доезжать."""

    def setUp(self):
        self.created: list = []
        self.asked: list = []
        self._create, self._render = (C._panel_create_and_report,
                                      C._render_select)

        async def create(msg, uid, values, extra=None, picked=None,
                         state=None, api=None):
            self.created.append({"values": values, "extra": extra,
                                 "picked": picked, "state": state,
                                 "api": api})

        async def render(msg, state, edit=True):
            data = await state.get_data()
            self.asked.append(data.get("current_attr"))

        C._panel_create_and_report = create
        C._render_select = render

    def tearDown(self):
        C._panel_create_and_report, C._render_select = self._create, self._render

    def run_wizard(self, autopick):
        fields = [{"attribute": "category", "label": "Категория",
                   "options": opts("Аккаунты", "Roblox", "Steam")},
                  {"attribute": "subcategory", "label": "Подкатегория",
                   "options": opts("Robux", "Предметы")}]
        state = FSM({"select_queue": ["category", "subcategory"],
                     "form_fields": fields, "chosen": {},
                     "pending": {"title": "1000 Robux"},
                     "autopick": autopick})
        asyncio.run(C._ask_next_select(Msg(), state, 1))
        return state

    def test_both_sections_are_picked_and_nothing_is_asked(self):
        self.run_wizard(["robux", "roblox"])
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0]["extra"],
                         {"category": 2, "subcategory": 1})

    def test_the_seller_is_told_what_was_chosen_for_him(self):
        """Иначе он узнает, где лежит товар, только с витрины.

        Рядом с названием сказано и КАК оно выбрано: подобранное по словам
        может оказаться не тем, а взятое у образца — то же самое, что у
        товара, с которого копировали. Разница эта продавцу и нужна."""
        self.run_wizard(["robux", "roblox"])
        picked = self.created[0]["picked"]
        self.assertTrue(any(p.startswith("Категория: Roblox") for p in picked),
                        picked)
        self.assertTrue(any(p.startswith("Подкатегория: Robux") for p in picked),
                        picked)
        self.assertTrue(all("подобран" in p for p in picked), picked)

    def test_without_a_hint_the_wizard_asks_as_before(self):
        """Обычное создание товара этой правкой не меняется."""
        self.run_wizard([])
        self.assertEqual(self.asked, ["category"])
        self.assertEqual(self.created, [])

    def test_an_ambiguous_section_is_asked_even_with_a_hint(self):
        fields = [{"attribute": "category", "label": "Категория",
                   "options": opts("Robux GL", "Robux RU")}]
        state = FSM({"select_queue": ["category"], "form_fields": fields,
                     "chosen": {}, "pending": {}, "autopick": ["robux"]})
        asyncio.run(C._ask_next_select(Msg(), state, 1))
        self.assertEqual(self.asked, ["category"])
        self.assertEqual(self.created, [])

    def test_the_rest_is_still_asked_when_only_one_section_matched(self):
        fields = [{"attribute": "category", "label": "Категория",
                   "options": opts("Аккаунты", "Roblox")},
                  {"attribute": "type", "label": "Тип",
                   "options": opts("Ключ", "Аккаунт")}]
        state = FSM({"select_queue": ["category", "type"],
                     "form_fields": fields, "chosen": {}, "pending": {},
                     "autopick": ["robux", "roblox"]})
        asyncio.run(C._ask_next_select(Msg(), state, 1))
        self.assertEqual(self.asked, ["type"])
        self.assertEqual(state.data["chosen"], {"category": 2})


class TheChoiceIsVisibleInTheReport(unittest.TestCase):
    def test_the_note_names_the_field_and_the_value(self):
        got = C._picked_note(["Категория: Roblox", "Подкатегория: Robux"])
        self.assertIn("Категория: Roblox", got)
        self.assertIn("Подкатегория: Robux", got)

    def test_nothing_is_said_when_the_seller_chose_himself(self):
        self.assertEqual(C._picked_note([]), "")
        self.assertEqual(C._picked_note(None), "")

    def test_a_section_name_with_a_tag_does_not_break_the_message(self):
        """Названия разделов приходят из панели. Одиночный «<» роняет
        отправку целиком — и роняет как раз отчёт о созданном товаре."""
        self.assertNotIn("<b>", C._picked_note(["Категория: <b>Roblox"]))


class ThePluginTellsTheWizardWhichSectionToUse(unittest.TestCase):
    """Без этого автовыбор написан, но не работает нигде."""

    def test_the_robux_plugin_passes_the_words(self):
        import inspect

        from handlers import plugins as P
        src = inspect.getsource(P.roblox_den_price)
        self.assertIn("autopick", src)
        self.assertIn("robux", src)
        self.assertIn("roblox", src)

    def test_the_narrow_word_goes_first(self):
        """Проверяется порядок, а не точный список.

        Раньше здесь стояло `words[:2] == ["robux", "roblox"]`. Утверждение
        класса — «узкое слово первым», а проверка запрещала вставить между
        ними третье. Между ними и понадобилось: подкатегории «Робуксы» в
        панели нет, и без «игровая валюта» подбор не срабатывал вовсе
        (см. `test_robux_subcategory.py`).
        """
        import inspect
        import re

        from handlers import plugins as P
        got = re.search(r"autopick=\[(.+?)\]",
                        inspect.getsource(P.roblox_den_price), re.S)
        self.assertIsNotNone(got, "autopick передаётся не списком")
        words = [w.strip().strip('"\'') for w in got.group(1).split(",")
                 if w.strip()]
        self.assertEqual(words[0], "robux", "узкое слово должно идти первым")
        self.assertIn("roblox", words)
        self.assertLess(words.index("robux"), words.index("roblox"),
                        "широкое слово перебило бы узкое")


def many(n: int) -> list[dict]:
    """Список, обрезанный панелью: ровно столько, сколько она показывает."""
    return [{"label": f"Игра {i:03d}", "value": 1000 + i} for i in range(n)]


class ACutOffListDoesNotDenyTheSectionTheItemAlreadyHas(unittest.TestCase):
    """Живой экран 07.09: «Выбери Категория (всего: 500)» и пятьсот чужих
    игр по алфавиту.

    Панель отдаёт список ОБРЕЗАННЫМ: без слова для поиска приходят первые
    несколько сотен. «Нет в списке» поэтому не значит «нет вовсе» — значит
    «дальше не показали», и выбрасывать из-за этого номер, взятый с карточки
    ТОЙ ЖЕ панели, нельзя: продавец получает список чужих игр вместо
    раздела, который у товара уже стоит.

    Обратное тоже обязано работать: полный список номер именно опровергает,
    и такой не отправляется — товар лёг бы в чужой раздел молча.
    """

    def setUp(self):
        self.created: list = []
        self.asked: list = []
        self.searched: list = []
        self._create, self._render = C._panel_create_and_report, C._render_select
        self._search = C._search_options

        async def create(msg, uid, values, extra=None, picked=None,
                         state=None, api=None):
            self.created.append({"extra": dict(extra or {}), "picked": picked})

        async def render(msg, state, edit=True):
            data = await state.get_data()
            self.asked.append(data.get("current_attr"))

        async def search(uid, data, attr, terms):
            self.searched.append(list(terms))
            return self.found, (self.found and terms[0] or "")

        self.found: list = []
        C._panel_create_and_report, C._render_select = create, render
        C._search_options = search

    def tearDown(self):
        C._panel_create_and_report, C._render_select = self._create, self._render
        C._search_options = self._search

    def wizard(self, options, chosen, labels=None, words=None):
        fields = [{"attribute": "category", "label": "Категория",
                   "options": options}]
        state = FSM({"select_queue": ["category"], "form_fields": fields,
                     "chosen": dict(chosen), "pending": {"title": "товар"},
                     "source_labels": dict(labels or {}),
                     "autopick": list(words or [])})
        asyncio.run(C._ask_next_select(Msg(), state, 1))
        return state

    def test_the_number_from_the_card_survives_a_cut_off_list(self):
        self.wizard(many(C._OPTIONS_SHOWN), {"category": 12},
                    {"category": "Standoff 2"})
        self.assertEqual(self.asked, [], "спросил то, что у товара уже стоит")
        self.assertEqual(self.created[0]["extra"], {"category": 12})

    def test_a_full_list_that_denies_the_number_is_believed(self):
        """Тот же номер, но список полон — значит номера правда нет."""
        self.wizard(many(C._OPTIONS_SHOWN - 1), {"category": 12},
                    {"category": "Standoff 2"})
        self.assertEqual(self.asked, ["category"])
        self.assertEqual(self.created, [])

    def test_it_asks_the_panel_by_name_before_giving_up(self):
        """Листать за продавца обрезок бессмысленно — нужного в нём не было.
        Тем же адресом, которым ищет он словом, бот спрашивает сам."""
        self.found = [{"label": "Standoff 2", "value": 44}]
        self.wizard(many(C._OPTIONS_SHOWN), {}, {"category": "Standoff 2"},
                    ["аккаунт"])
        self.assertEqual(self.asked, [])
        self.assertEqual(self.created[0]["extra"], {"category": 44})
        self.assertEqual(self.searched[0][0], "Standoff 2",
                         "искать надо сперва по надписи образца")

    def test_what_the_search_found_is_shown_instead_of_the_cut_off_list(self):
        """Выбрать не вышло — но показать найденное лучше, чем первые
        пятьсот по алфавиту: нужного среди них и не было."""
        self.found = [{"label": "Standoff 2 GL", "value": 44},
                      {"label": "Standoff 2 RU", "value": 45}]
        state = self.wizard(many(C._OPTIONS_SHOWN), {},
                            {"category": "Standoff 2"})
        self.assertEqual(self.asked, ["category"])
        self.assertEqual(state.data["current_options"], self.found)



if __name__ == "__main__":
    unittest.main()
