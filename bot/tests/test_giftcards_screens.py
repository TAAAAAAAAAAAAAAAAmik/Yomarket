"""Экраны гифт-карт: у каждой кнопки есть обработчик, и он тот самый.

Выигрыш шаблона именно здесь: карт может стать двадцать пять, а проверка
остаётся одна. Раньше кнопку добавляли руками — и проверить, что она
куда-то ведёт, можно было только руками же.

Проверяется не текст экрана, а следствия:

* каждая `callback_data` со всех клавиатур **разбирается** каким-нибудь
  обработчиком — кнопка, ведущая в никуда, в Telegram молча крутит часики;
* каждая укладывается в **64 байта** — предел Telegram, за которым кнопка
  просто не отправится;
* поле, введённое на экране одной карты, ложится **в её** настройки, а не
  в соседние.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import giftcards as gc                      # noqa: E402
from handlers import plugins as P                           # noqa: E402


def _all_routers() -> list:
    """Все роутеры, которые бот подключает на самом деле.

    Перечислять их в тесте руками нельзя: список протух бы на первом же
    новом разделе, и кнопка, чей обработчик живёт в нём, считалась бы
    мёртвой. Имена берутся из `main.py` — оттуда же, откуда их берёт бот.

    **`fallback` исключён намеренно.** У него `@router.callback_query(F.data)`
    — он отвечает на любую нераспознанную кнопку и подключён последним.
    С ним «обработчик есть» верно для чего угодно, включая опечатку в
    `callback_data`, и проверка не проверяла бы ничего: первая версия этого
    теста не заметила кнопку, которую я добавил нарочно.
    """
    SKIP = {"fallback"}
    import importlib
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent
              / "main.py").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"dp\.include_router\((\w+)\.router\)",
                                  source)) - SKIP)
    out = []
    for name in names:
        try:
            out.append(importlib.import_module(f"handlers.{name}").router)
        except Exception:                       # pragma: no cover
            continue
    return out


class Ev:
    """Событие с одним полем `data` — этого хватает магическим фильтрам."""

    def __init__(self, data):
        self.data = data


def is_handled(data: str) -> bool:
    """Разберёт ли эту кнопку хоть один зарегистрированный обработчик."""
    for router in _all_routers():
        for handler in router.observers["callback_query"].handlers:
            magics = [f.magic for f in handler.filters or [] if f.magic]
            if magics and all(m.resolve(Ev(data)) for m in magics):
                return True
    return False


def all_buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data]


def settings_with_all_cards() -> dict:
    settings: dict = {}
    for gift in gc.cards():
        gc.card_conf(settings, gift.slug)["enabled"] = True
    return settings


class EveryButtonLeadsSomewhere(unittest.TestCase):
    """Кнопка без обработчика в Telegram не ошибается — она молчит.

    Со стороны продавца это выглядит как «бот сломался», и найти такое
    можно только нажав. На двадцати пяти картах — двести кнопок.
    """

    def _every_markup(self):
        settings = settings_with_all_cards()
        yield P._gift_cards_keyboard(settings)
        yield P._plugins_menu_keyboard()
        for gift in gc.cards():
            yield P._gift_card_keyboard(settings, gift)
            yield P._gift_cfg_keyboard(gift)

    def test_every_button_of_every_card_has_a_handler(self):
        for markup in self._every_markup():
            for data in all_buttons(markup):
                self.assertTrue(is_handled(data),
                                f"кнопка «{data}» никуда не ведёт")

    def test_every_callback_fits_the_telegram_limit(self):
        for markup in self._every_markup():
            for data in all_buttons(markup):
                self.assertLessEqual(len(data.encode()), 64, data)

    def test_the_menu_no_longer_offers_the_telegram_gifts_stub(self):
        """Заглушка обещала «функция появится в следующем обновлении».

        Обещание, которого бот не сдержит, — то же враньё, только вежливое.
        """
        data = all_buttons(P._plugins_menu_keyboard())
        self.assertNotIn("plugins:auto_gifts", data)
        self.assertIn("plugins:gifts", data)


class FieldsGoToTheRightCard(unittest.TestCase):
    """Поля вводятся одним экраном на все карты — `slug` лежит в данных
    состояния. Спутать их значит настроить не ту карту, и продавец увидел
    бы это только по несостоявшейся выдаче."""

    def setUp(self):
        self.saved: dict = {}
        self._real_get = P.get_settings
        self._real_save = P.save_settings
        P.get_settings = lambda uid: self.saved
        P.save_settings = lambda uid, s: self.saved.update(s)

    def tearDown(self):
        # Подменяете что-то в общем модуле — откатывайте: перемешанный
        # порядок тестов иначе роняет чужие.
        P.get_settings = self._real_get
        P.save_settings = self._real_save

    def test_a_keyword_lands_in_the_card_it_was_typed_for(self):
        target = gc.cards()[0]
        other = gc.cards()[1]

        class FSM:
            async def get_data(_self):
                return {"gc_slug": target.slug, "gc_field": "keyword"}

            async def clear(_self):
                pass

        class Msg:
            text = "моё слово"
            from_user = type("U", (), {"id": 1})()

            async def answer(_self, *a, **kw):
                return None

        asyncio.run(P.gift_field_input(Msg(), FSM()))
        self.assertEqual(gc.card_conf(self.saved, target.slug)["keyword"],
                         "моё слово")
        self.assertEqual(gc.card_conf(self.saved, other.slug)["keyword"], "")

    def test_an_unknown_field_is_refused_rather_than_written(self):
        """Экран мог устареть между нажатием и вводом."""
        class FSM:
            async def get_data(_self):
                return {"gc_slug": gc.cards()[0].slug, "gc_field": "enabled"}

            async def clear(_self):
                pass

        said = []

        class Msg:
            text = "да"
            from_user = type("U", (), {"id": 1})()

            async def answer(_self, text, *a, **kw):
                said.append(text)

        asyncio.run(P.gift_field_input(Msg(), FSM()))
        self.assertTrue(said)
        self.assertNotIn("enabled", str(self.saved))


class TheCardScreenTellsTheTruth(unittest.TestCase):
    """Экран не должен обещать выдачу там, где её не будет."""

    def test_a_card_without_a_region_says_where_it_will_look_for_one(self):
        settings = settings_with_all_cards()
        gift = gc.cards()[0]
        gc.card_conf(settings, gift.slug)["region"] = ""
        text = P._gift_card_text(settings, gift)
        self.assertIn("описания товара", text)

    def test_a_card_that_is_off_does_not_claim_to_deliver(self):
        settings = settings_with_all_cards()
        gift = gc.cards()[0]
        gc.card_conf(settings, gift.slug)["enabled"] = False
        self.assertIn("выключена", P._gift_card_text(settings, gift))


if __name__ == "__main__":
    unittest.main()


class AnEnabledCardGetsItsOwnButton(unittest.TestCase):
    """Карта, которой торгуют каждый день, должна открываться в одно
    нажатие, а не через общий список из восьми.

    Кнопки берутся из реестра по признаку «включена». Перечислять их руками
    значило бы завести восемь строчек вида «если это Apple — покажи Apple»,
    и девятая однажды не появилась бы вовсе — ровно то, от чего уходили,
    когда сводили плагины в один движок.
    """

    def buttons(self, settings):
        from handlers.plugins import _plugins_menu_keyboard
        kb = _plugins_menu_keyboard(settings)
        return [(b.text, b.callback_data)
                for row in kb.inline_keyboard for b in row]

    def test_nothing_enabled_shows_only_the_general_entry(self):
        cbs = [c for _, c in self.buttons({})]
        self.assertIn("plugins:gifts", cbs)
        self.assertFalse([c for c in cbs if c.startswith("plugins:gc:")])

    def test_an_enabled_card_is_pinned_to_the_menu(self):
        got = self.buttons({"plugins": {"gift_cards": {"apple": {"enabled": True}}}})
        self.assertIn("plugins:gc:apple", [c for _, c in got])
        self.assertTrue([t for t, _ in got if "Apple" in t])

    def test_a_disabled_card_is_not(self):
        got = self.buttons({"plugins": {"gift_cards": {"apple": {"enabled": False}}}})
        self.assertNotIn("plugins:gc:apple", [c for _, c in got])

    def test_several_enabled_cards_all_appear(self):
        got = [c for _, c in self.buttons({"plugins": {"gift_cards": {
            "apple": {"enabled": True}, "xbox": {"enabled": True},
            "steam": {"enabled": True}}}})]
        for slug in ("apple", "xbox", "steam"):
            self.assertIn(f"plugins:gc:{slug}", got)

    def test_the_general_list_never_disappears(self):
        """Через него включают карту в первый раз. Без него выключенная
        карта была бы недостижима вовсе."""
        got = [c for _, c in self.buttons(
            {"plugins": {"gift_cards": {"apple": {"enabled": True}}}})]
        self.assertIn("plugins:gifts", got)

    def test_no_card_is_named_in_the_menu_code(self):
        """Признак того, что кнопки штампуются, а не перечисляются."""
        import inspect
        from handlers import plugins as P
        src = inspect.getsource(P._plugins_menu_keyboard)
        for slug in ("apple", "xbox", "steam", "amazon", "razer"):
            self.assertNotIn(f'"{slug}"', src)


class MakingAProductLivesInTheTemplateToo(unittest.TestCase):
    """Создание товара было только у Robux — того плагина, что писался с
    нуля. У карт его не было вовсе, а завести руками 476 номиналов Apple по
    31 региону невозможно.

    Смысл не в удобстве: витрина и каталог обязаны сойтись. Объявление на
    номинал, которого у поставщика нет, автовыдаче недоступно, и узнал бы
    об этом продавец из отказа — когда покупатель уже заплатил.
    """

    def keyboard(self, gift):
        from handlers.plugins import _gift_card_keyboard
        kb = _gift_card_keyboard({}, gift)
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_every_card_can_start_a_product(self):
        import automation.giftcards as G
        for gift in G.cards():
            self.assertIn(f"plugins:gc:{gift.slug}:make", self.keyboard(gift))

    def test_the_flow_hands_over_to_the_usual_wizard(self):
        """Своего создания товаров в шаблоне нет и не нужно: мастер умеет
        разделы панели, обязательные поля, фото и публикацию."""
        import inspect
        from handlers import plugins as P
        src = inspect.getsource(P.gift_make_handoff)
        self.assertIn("CreateAdState.quantity", src)
        self.assertNotIn("panel_create_product_sync", src)

    def test_the_region_goes_into_the_description(self):
        """У Юмаркета поля под регион нет — выдача читает его из описания."""
        import inspect
        from handlers import plugins as P
        self.assertIn("with_region", inspect.getsource(P.gift_make_handoff))

    def test_the_section_hint_comes_from_the_card(self):
        """Подстановка раздела витрины — данные карты, а не ветка в коде."""
        import inspect
        from handlers import plugins as P
        src = inspect.getsource(P.gift_make_handoff)
        self.assertIn("gift.autopick", src)
        for slug in ("apple", "xbox", "steam"):
            self.assertNotIn(f'"{slug}"', src)


class TheGreetingIsSentOnceAndOnlyIfAsked(unittest.TestCase):
    """Автоответ уходит покупателю до кода.

    Нужен не всегда: обычно код приходит через секунды. Но у долгих
    номиналов поставщик отвечает «принято, код будет позже», и тогда
    молчание похоже на поломку.
    """

    def test_silence_is_the_default(self):
        import automation.giftcards as G
        self.assertEqual(G.DEFAULT_CARD_CONF["greeting"], "")

    def test_the_mark_lives_in_the_record_not_in_memory(self):
        """Проход может оборваться между приветом и покупкой — тогда
        возобновление поздоровалось бы второй раз."""
        import inspect
        from tasks import manager as M
        src = inspect.getsource(M.TaskManager._maybe_deliver_gift)
        self.assertIn('entry.get("greeted")', src)
        self.assertIn('entry["greeted"] = True', src)

    def test_it_goes_before_the_purchase(self):
        import inspect
        from tasks import manager as M
        src = inspect.getsource(M.TaskManager._maybe_deliver_gift)
        self.assertLess(src.index("greeting"), src.index("_gift_finish"))


class TheKeywordIsExplainedWhereItIsMisread(unittest.TestCase):
    """«Слово-опознаватель» понимают по-разному: одни думают, что это слово
    для покупателя, другие — что бот ищет его в описании."""

    def test_the_prompt_says_where_the_bot_looks(self):
        from handlers.plugins import _GIFT_FIELDS
        prompt = _GIFT_FIELDS["kw"][0]
        self.assertIn("названи", prompt.lower())
        self.assertIn("витрин", prompt.lower())

    def test_and_shows_an_example_of_both_outcomes(self):
        from handlers.plugins import _GIFT_FIELDS
        prompt = _GIFT_FIELDS["kw"][0]
        self.assertIn("✅", prompt)
        self.assertIn("❌", prompt)

    def test_and_says_when_not_to_set_it(self):
        """Совет «задайте всегда» отправил бы продавца делать лишнее."""
        from handlers.plugins import _GIFT_FIELDS
        self.assertIn("только если", _GIFT_FIELDS["kw"][0].lower())
