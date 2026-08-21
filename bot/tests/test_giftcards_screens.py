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



class TheScreensSellWithNumbersNotAdjectives(unittest.TestCase):
    """«Упакуй красиво и продажно» — 21.08.

    Продажно здесь не значит «побольше прилагательных». Продавец платит за
    подписку и вправе видеть, что она сделала: не «автоматическая доставка
    цифровых товаров», а сколько кодов ушло и когда ушёл последний. Цифры
    берутся из его собственных журналов — значит это отчёт, а не обещание,
    и проверить он может там же.

    Отдельно проверяется, что несостоявшиеся выдачи не спрятаны за
    состоявшимися: «12 выдано» рядом с тремя молча не выданными — это тот
    самый бодрый отчёт, от которого здесь уходят.
    """

    def settings(self, delivered=2, failed=0, enabled=True, when=None):
        import time as _t
        log = [{"at": when or (_t.time() - 600), "state": "выдан"}
               for _ in range(delivered)]
        log += [{"at": _t.time() - 60, "state": "не выдан", "why": "нет кода"}
                for _ in range(failed)]
        return {"plugins": {"gift_cards": {"apple": {
            "enabled": enabled, "delivered": [str(i) for i in range(delivered)],
            "log": log}}}}

    def menu(self, settings):
        from handlers import plugins as P
        return P._plugins_menu_text(settings)

    def shelf(self, settings):
        from handlers import plugins as P
        return P._gift_cards_text(settings)

    def test_the_menu_states_what_was_delivered(self):
        got = self.menu(self.settings(delivered=12))
        self.assertIn("12", got)
        self.assertIn("мин назад", got)

    def test_a_failure_is_not_hidden_behind_the_successes(self):
        got = self.menu(self.settings(delivered=12, failed=3))
        self.assertIn("Не выдано", got)
        self.assertIn("3", got)

    def test_an_empty_shop_is_invited_not_scolded(self):
        """Ноль выдач — это первый день, а не поломка."""
        got = self.menu(self.settings(delivered=0, enabled=False))
        self.assertIn("Пока не выдано", got)
        self.assertNotIn("Не выдано:", got)

    def test_the_shelf_counts_each_card(self):
        got = self.shelf(self.settings(delivered=5))
        self.assertIn("Apple", got)
        self.assertIn("выдано 5", got)

    def test_the_shelf_no_longer_claims_delivery_is_unproven(self):
        """Эта строка была правдой ровно до первой выдачи, а потом
        превратилась в неправду, которую продавец видел каждый день."""
        got = self.shelf(self.settings(delivered=5))
        self.assertNotIn("ещё не проверялась", got)

    def test_the_shelf_says_how_many_cards_are_left_to_turn_on(self):
        got = self.shelf(self.settings(delivered=0, enabled=False))
        self.assertIn("Доступно ещё", got)

    def test_the_screen_says_what_the_plugins_do_for_the_seller(self):
        """Цифры доказывают, что работает; текст объясняет, зачем это нужно.
        Одних цифр мало: «выдано 3» ничего не говорит тому, кто ещё думает,
        включать ли плагин."""
        got = self.menu(self.settings(delivered=3))
        self.assertIn("Что делают плагины", got)
        self.assertIn("пока вы спите", got)

    def test_the_pitch_stays_when_there_is_nothing_to_show_yet(self):
        got = self.menu(self.settings(delivered=0, enabled=False))
        self.assertIn("Что делают плагины", got)

    def test_the_numbers_come_before_the_pitch(self):
        """Доказательство вперёд обещания: продавец видит свой счёт, а
        потом уже читает, почему это хорошо."""
        got = self.menu(self.settings(delivered=3))
        self.assertLess(got.index("Выдано кодов"), got.index("Что делают"))

    def test_nothing_is_promised_that_cannot_be_checked(self):
        """«Продажи вырастут в разы» проверить нельзя, а «заказ в четыре утра
        не ждёт до утра» продавец проверит в первую же ночь. Обещание,
        которого нельзя сдержать, здесь дороже любой недосказанности."""
        got = self.menu(self.settings(delivered=3)).lower()
        for promise in ("в разы", "гарантир", "вырастут", "заработаете",
                        "прибыль вырастет", "х2", "в два раза больше"):
            self.assertNotIn(promise, got)

    def test_the_number_of_cards_is_counted_not_written_by_hand(self):
        """Число в тексте — из реестра. Дописанное руками разошлось бы с ним
        на первой же новой карте, и экран начал бы врать по мелочи."""
        from automation.giftcards import cards
        self.assertIn(str(len(cards())), self.menu(self.settings(delivered=1)))

    def test_a_failed_attempt_is_not_reported_as_a_delivery(self):
        """Самая дорогая мелочь на этом экране. Последняя запись журнала —
        не то же, что последняя выдача: несостоявшаяся попытка минуту назад
        отчитывалась как выдача, и строка выходила бодрая, а кода покупатель
        не получил."""
        import time as _t
        settings = {"plugins": {"gift_cards": {"apple": {
            "enabled": True, "delivered": ["1"],
            "log": [{"at": _t.time() - 7200, "state": "выдан"},
                    {"at": _t.time() - 60, "state": "не выдан"}]}}}}
        got = self.menu(settings)
        self.assertIn("2 ч назад", got)
        self.assertNotIn("1 мин назад", got)

    def test_the_cards_offered_are_the_ones_still_off(self):
        """Список «доступно ещё» перечислял включённые карты — предлагал
        завести то, что уже заведено."""
        got = self.shelf(self.settings(delivered=1))
        tail = got.split("Доступно ещё")[1]
        self.assertNotIn("Apple", tail)

    def test_the_card_screen_puts_the_result_above_the_settings(self):
        from automation.giftcards import card
        from handlers import plugins as P
        got = P._gift_card_text(self.settings(delivered=4), card("apple"))
        self.assertIn("Выдано кодов", got)
        self.assertLess(got.index("Выдано кодов"), got.index("Регион"))


class TimeIsToldWithoutInventingATimezone(unittest.TestCase):
    """Абсолютное время требует знать пояс продавца, а мы его не знаем: бот
    живёт на сервере, продавец — где угодно. Соврать на три часа в строке
    «последняя выдача» — мелочь, из которой складывается недоверие ко
    всему остальному."""

    def test_minutes_hours_and_days(self):
        from automation.giftcards import ago
        now = 1_000_000.0
        self.assertEqual(ago(now - 30, now), "только что")
        self.assertEqual(ago(now - 600, now), "10 мин назад")
        self.assertEqual(ago(now - 7200, now), "2 ч назад")
        self.assertEqual(ago(now - 86400 * 1.2, now), "вчера")
        self.assertEqual(ago(now - 86400 * 5, now), "5 дн назад")

    def test_nothing_is_said_when_nothing_happened(self):
        from automation.giftcards import ago
        self.assertEqual(ago(0), "")

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
        """Выдача читает регион из описания, а не из панели. Своё поле
        «Регион» у панели есть (`filter__8`, обязательное), но оно её
        собственное требование к карточке и описания не заменяет."""
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
