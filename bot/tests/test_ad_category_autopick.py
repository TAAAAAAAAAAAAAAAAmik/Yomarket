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
        # И КАК выбрано — у каждой строки своя причина в скобках. Слово
        # проверять нельзя: причин несколько, и «найден в тексте товара»
        # такая же законная, как «подобран по названию».
        self.assertTrue(all(p.rstrip().endswith(")") for p in picked), picked)

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


class TheSectionIsLookedForInTheItemsOwnText(unittest.TestCase):
    """Поиск идёт В ОБРАТНУЮ СТОРОНУ, и в этом всё дело.

    Раньше бот брал свои слова («Аккаунт», «Баланс») и искал их среди
    названий разделов. Так находится «Аккаунты с виртами» и НИКОГДА не
    находится «Black Russia»: это два слова, и ни одного из них в названии
    товара нет. А панель прислала все 825 названий сама — значит искать
    надо ИХ в тексте товара.

    Живой случай 07.09: продавец получил подраздел «Аккаунты с виртами»
    автоматически, а раздел «Black Russia» бот взять не мог, хотя игра
    названа в описании открытым текстом.
    """

    GAMES = [{"label": n, "value": i} for i, n in enumerate(
        ["Black Desert", "Black Russia", "Black Russia Mobile", "Steam",
         "ARK: Survival Evolved", "AI LIMIT", "Ace Racer", "Роблокс"],
        start=1)]

    def find(self, title, description=""):
        got = C._match_by_text(self.GAMES, title, description)
        return None if got is None else got["label"]

    def test_the_game_is_found_in_the_description(self):
        self.assertEqual(
            self.find("💖Аккаунт 💖Баланс: 3.000.000 ₽",
                      "Аккаунт Black Russia, 4 уровень, вирты в банке."),
            "Black Russia")

    def test_the_title_beats_the_description(self):
        """Название товара говорит о нём самом, описание — о чём угодно:
        «переход с Black Russia не нужен» стоит в описании аккаунта Steam.

        Проверка нарочно такая, где длинное имя лежит в ОПИСАНИИ, а верное
        короткое — в названии: иначе побеждало бы длинное, и порядок ничего
        бы не решал."""
        self.assertEqual(
            self.find("Аккаунт Steam", "Переход с Black Russia не нужен."),
            "Steam")

    def test_the_longest_name_wins(self):
        """«Black Russia Mobile» содержит «Black Russia» — при обоих
        совпадениях верное длинное."""
        self.assertEqual(self.find("", "аккаунт Black Russia Mobile"),
                         "Black Russia Mobile")

    def test_two_names_of_equal_length_decide_nothing(self):
        """Раздел решает, где покупатель увидит товар. Ошибиться можно один
        раз: панель менять раздел после создания не даёт."""
        games = [{"label": "Раст", "value": 1}, {"label": "Тарк", "value": 2}]
        self.assertIsNone(
            C._match_by_text(games, "", "продаю Раст и Тарк одним лотом"))

    def test_only_whole_words_count(self):
        """Название внутри чужого слова положило бы товар в чужой раздел.
        Границы нужны ОБЕ: справа — «Роблоксовый», слева — «МикроРоблокс»."""
        self.assertIsNone(self.find("", "это Роблоксовый аккаунт"))
        self.assertIsNone(self.find("", "продаю МикроРоблокс задёшево"))
        self.assertIsNone(self.find("", "оплата через SuperSteam"))

    def test_it_works_for_cyrillic_names_too(self):
        """`\b` перед кириллицей ведёт себя не так, как ждут, — граница
        считается по самому слову."""
        self.assertEqual(self.find("", "продаю аккаунт Роблокс дёшево"),
                         "Роблокс")
        self.assertIsNone(self.find("", "это Роблоксовый аккаунт"))

    def test_short_names_are_not_hunted(self):
        """Двухбуквенное название найдётся в любом тексте."""
        games = [{"label": "AI", "value": 1}, {"label": "ARK", "value": 2}]
        self.assertIsNone(C._match_by_text(games, "", "аккаунт AI и ARK"))

    def test_empty_text_finds_nothing(self):
        self.assertIsNone(self.find("", ""))
        self.assertIsNone(self.find(None, None))

    def test_a_name_that_is_not_there_is_not_invented(self):
        self.assertIsNone(self.find("Аккаунт с виртами", "Вход по почте."))


class WhatDecidesWhichOptionIsTheRightOne(unittest.TestCase):
    """Порядок доводов в `_pick_option` — не вкусовщина.

    Форма создания и карточка товара — одна панель, один раздел `items` и
    одно поле, значит и нумерация одна: номер образца сильнее всего
    остального. Надпись — второй довод, на случай если номера в списке нет.
    Слово — третий и самый слабый: им товар кладут не на ту полку.
    """

    def pick(self, options, value=None, label="", words=None):
        got, how = C._pick_option(options, value, label, words or [])
        return (None if got is None else got["value"]), how

    def test_the_number_wins_over_a_label_pointing_elsewhere(self):
        """Надпись на карточке бывает старой или иначе оформленной. Номер —
        это то, чем товар связан с разделом на самом деле."""
        options = [{"label": "Игры", "value": 12},
                   {"label": "Аккаунты", "value": 30}]
        self.assertEqual(self.pick(options, 12, "Аккаунты"),
                         (12, "номер образца"))

    def test_the_label_decides_when_the_number_is_absent(self):
        options = [{"label": "Аккаунты", "value": 30}]
        self.assertEqual(self.pick(options, 12, "Аккаунты"),
                         (30, "надпись образца"))

    def test_a_word_is_the_last_resort(self):
        options = [{"label": "Standoff 2", "value": 44},
                   {"label": "Roblox", "value": 45}]
        self.assertEqual(self.pick(options, None, "", ["standoff"]),
                         (44, "подобран по названию"))

    def test_two_word_matches_decide_nothing(self):
        """Раздел решает, где покупатель увидит товар."""
        options = [{"label": "Standoff 2 GL", "value": 44},
                   {"label": "Standoff 2 RU", "value": 45}]
        self.assertEqual(self.pick(options, None, "", ["standoff"]),
                         (None, ""))

    def test_two_options_with_the_same_label_decide_nothing_either(self):
        options = [{"label": "Аккаунты", "value": 30},
                   {"label": "аккаунты", "value": 31}]
        self.assertEqual(self.pick(options, None, "Аккаунты"), (None, ""))


# Сколько вариантов панель отдаёт на самом деле — живой ответ 07.09.# Сколько вариантов панель отдаёт на самом деле — живой ответ 07.09.
_PANEL_GIVES = 825


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

    def test_a_number_no_list_confirms_goes_out_and_says_so(self):
        """Тупик хуже отказа. Номер взят с карточки ТОЙ ЖЕ панели, у ТОГО
        ЖЕ товара, в том же поле того же раздела `items` — значит нумерация
        та же. Если он не сошёлся ни со списком, ни с поиском, отправляем
        его и говорим об этом: панель, если номер не тот, ответит отказом
        по полю, а отказ мастер превращает в вопрос.

        Выброшенный номер вопросом не становится — он становится списком из
        сотен чужих строк, в котором нужного нет. Ровно в это копия и
        упиралась."""
        self.wizard(many(20), {"category": 12}, {"category": "Standoff 2"})
        self.assertEqual(self.asked, [])
        self.assertEqual(self.created[0]["extra"], {"category": 12})
        self.assertTrue(
            any("не сверился" in p for p in self.created[0]["picked"]),
            self.created[0]["picked"])

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

    def test_a_field_the_item_never_has_takes_the_forms_own_default(self):
        """В форме создания есть поля, которых у товара не бывает вовсе —
        живой ответ 07.09: `has_chat`, `created_order`, `wait_order`,
        `confirmed_order`. Спросить о них значит спросить о том, чего
        копировать неоткуда: при обычном создании уходит то, что предлагает
        сама форма."""
        fields = [{"attribute": "has_chat", "label": "Чат",
                   "options": opts("Да", "Нет"), "value": 1}]
        state = FSM({"select_queue": ["has_chat"], "form_fields": fields,
                     "chosen": {}, "pending": {}, "autopick": [],
                     "source_labels": {}})
        asyncio.run(C._ask_next_select(Msg(), state, 1))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.created[0]["extra"], {"has_chat": 1})

    def test_but_the_section_never_takes_a_default(self):
        """Раздел решает, где покупатель увидит товар, а тип — как заказ
        будет выдан. Тихо подставить сюда «что предлагает форма» значит
        поставить продавца перед фактом на витрине."""
        for attr in C._SECTION_TRIPLE:
            with self.subTest(attr):
                self.created.clear()
                self.asked.clear()
                fields = [{"attribute": attr, "label": attr,
                           "options": opts("Первый", "Второй"), "value": 1}]
                state = FSM({"select_queue": [attr], "form_fields": fields,
                             "chosen": {}, "pending": {}, "autopick": [],
                             "source_labels": {}})
                asyncio.run(C._ask_next_select(Msg(), state, 1))
                self.assertEqual(self.asked, [attr])
                self.assertEqual(self.created, [])

    def test_a_section_past_the_shown_part_is_still_matched(self):
        """Живой /copy_debug 07.09: панель отдала 825 вариантов, а сверка
        шла по первым пятистам. Триста двадцать пять разделов обрезались, и
        «Standoff 2» — буква S — в остаток не попадал: копия не находила
        номер, КОТОРЫЙ ПАНЕЛЬ ЖЕ И ПРИСЛАЛА.

        Сверяться надо со всем, что пришло; обрезать — только показ."""
        options = many(_PANEL_GIVES) + [{"label": "Standoff 2", "value": 44}]
        self.wizard(options, {"category": 44}, {"category": "Standoff 2"})
        self.assertEqual(self.asked, [], "не нашёл присланного панелью")
        self.assertEqual(self.created[0]["extra"], {"category": 44})
        # Именно НАШЁЛ, а не отправил не глядя: «не сверился» здесь значит,
        # что сверка снова идёт по обрезку, просто беду прикрывает запасной
        # ход. Отличать эти два случая и есть смысл проверки.
        self.assertTrue(
            any("номер образца" in p for p in self.created[0]["picked"]),
            self.created[0]["picked"])

    def test_the_shown_part_stays_bounded(self):
        """Показ обрезается по-прежнему: восемьсот кнопок в состоянии — это
        не выбор, а склад."""
        options = many(_PANEL_GIVES) + [{"label": "Standoff 2", "value": 44}]
        state = self.wizard(options, {}, {}, ["ничего не подойдёт"])
        self.assertEqual(self.asked, ["category"])
        self.assertLessEqual(len(state.data["current_options"]),
                             C._OPTIONS_SHOWN)

    def test_a_search_that_returns_the_same_stub_does_not_break_the_number(self):
        """Панель, не понявшая слова, присылает тот же обрезок. Записанное
        по такому ответу «список полон» выбрасывало номер, взятый с её же
        карточки, — то есть поиск ломал то, что без него работало."""
        self.found = many(C._OPTIONS_SHOWN)
        self.wizard(many(C._OPTIONS_SHOWN), {"category": 12},
                    {"category": "Standoff 2"})
        self.assertEqual(self.asked, [], "спросил то, что у товара уже стоит")
        self.assertEqual(self.created[0]["extra"], {"category": 12})

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
