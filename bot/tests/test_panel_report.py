"""Отчёт о создании товара обязан дойти — даже с чужой разметкой внутри.

19.08 экран замер на «⏳ Товар создан, делаю публичным…» и остался так
навсегда. Товар был создан, публикация не прошла, ответ панели попал в
отчёт сырым — а панель отвечает своим HTML, и обрезанный по длине он
оставил в сообщении половину тега. Telegram отказал сообщению целиком
(«can't parse entities: Unsupported start tag "co</i"»), обработчик упал
на этой строке, и продавец не узнал ни что товар создан, ни что случилось.

Рассказ о сбое не дошёл тоже: он вставлял текст той же ошибки — с тем же
обрывком тега — и падал на нём же.

Проверяется следствие: дошло ли сообщение и что в нём.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from aiogram.exceptions import TelegramBadRequest      # noqa: E402

from handlers import create_ad as C                    # noqa: E402

# Ответ панели, на котором всё и сломалось: чужая разметка, обрезанная по
# длине посреди тега.
PANEL_SAID = ('Отказ: <code class="err">нет остатков</code> — '
              + "x" * 200)


class Screen:
    """Сообщение бота. Помним каждую попытку отправки и чем она кончилась."""

    def __init__(self, reject: int = 0):
        self.tries: list = []          # (текст, parse_mode)
        self.reject = reject           # сколько первых попыток отвергнуть

    async def edit_text(self, text="", reply_markup=None, parse_mode=...,
                        **kw):
        self.tries.append((text, parse_mode))
        if len(self.tries) <= self.reject:
            raise TelegramBadRequest(
                method="editMessageText",
                message='can\'t parse entities: Unsupported start tag "co</i"')
        return self

    async def answer(self, text="", reply_markup=None, **kw):
        self.tries.append((text, ...))
        return self

    @property
    def last(self) -> str:
        return self.tries[-1][0] if self.tries else ""


class TheReportGetsThroughEvenWhenTheMarkupIsRefused(unittest.TestCase):

    def test_a_refused_message_is_repeated_without_markup(self):
        screen = Screen(reject=1)
        asyncio.run(C._edit_safely(screen, "<b>отчёт</b>"))
        self.assertEqual(len(screen.tries), 2)
        self.assertIsNone(screen.tries[1][1])

    def test_the_seller_sees_the_text_either_way(self):
        screen = Screen(reject=1)
        asyncio.run(C._edit_safely(screen, "<b>отчёт</b>"))
        self.assertIn("отчёт", screen.last)

    def test_an_unchanged_message_is_not_resent(self):
        """«message is not modified» — не отказ, а «показывать нечего»."""

        class Same(Screen):
            async def edit_text(self, text="", reply_markup=None,
                                parse_mode=..., **kw):
                self.tries.append((text, parse_mode))
                raise TelegramBadRequest(
                    method="editMessageText",
                    message="message is not modified")

        screen = Same()
        asyncio.run(C._edit_safely(screen, "<b>то же самое</b>"))
        self.assertEqual(len(screen.tries), 1)

    def test_a_good_message_goes_out_once(self):
        screen = Screen()
        asyncio.run(C._edit_safely(screen, "<b>отчёт</b>"))
        self.assertEqual(len(screen.tries), 1)


class ThePanelsOwnAnswerCannotBreakTheReport(unittest.TestCase):
    """Тот самый заказ: товар создан, публикация отказала, ответ панели
    ушёл в отчёт."""

    def setUp(self):
        import storage
        from automation import panel
        self._undo = []
        for mod, name, val in (
            (storage, "get_panel_creds", lambda uid: {"cookies": "c=1"}),
            (panel, "panel_create_product_sync",
             lambda *a, **kw: (True, "12345")),
            (panel, "panel_publish_item_sync",
             lambda *a, **kw: (False, PANEL_SAID)),
        ):
            self._undo.append((mod, name, getattr(mod, name, None)))
            setattr(mod, name, val)

    def tearDown(self):
        for mod, name, old in reversed(self._undo):
            setattr(mod, name, old)

    def report(self, title="1000 Robux"):
        screen = Screen()
        values = {"title": title, "price": 990, "description": "код",
                  "quantity": 1, "category": "", "photo_path": None}
        asyncio.run(C._panel_create_and_report(screen, 1, values, extra=None))
        return screen

    def test_the_report_is_delivered_on_the_first_try(self):
        """То есть разметка в нём верная — Telegram её принял."""
        screen = self.report()
        self.assertEqual([t[1] for t in screen.tries].count(None), 0)

    def test_the_panels_tags_arrive_as_text_not_as_markup(self):
        screen = self.report()
        self.assertNotIn('<code class="err">', screen.last)
        self.assertIn("&lt;code", screen.last)

    def test_the_seller_learns_the_item_was_created(self):
        """Главное, чего не хватало: товар-то создан."""
        screen = self.report()
        self.assertIn("12345", screen.last)

    def test_a_title_with_a_bracket_does_not_break_it_either(self):
        screen = self.report(title="Robux <1000>")
        self.assertNotIn("Robux <1000>", screen.last)
        self.assertIn("&lt;1000&gt;", screen.last)



# Отчёт панели в том виде, в каком он приходит: наша разметка вперемешку с
# чужим текстом, и всё это одной строкой.
REFUSED = (
    "✅ Ресурс <b>items</b> найден!\n"
    "Поля: <code>['title', 'category', 'filter__8']</code>\n"
    "Обязательные: <code>[]</code>\n\n"
    'Ошибка 422:\n<code>{"filter__8": '
    '["Поле Регион обязательно для заполнения."]}</code>\n\n'
    'Отправлено: <code>{"title": "Apple Gift Card TRY 10 (TR)", '
    '"price": 60, "content": "Код пополнения", "category": 4718}</code>'
)


class TheRefusalReadsAsAnAnswerNotAsADump(unittest.TestCase):
    """Продавец присылал этот экран дважды, и оба раза в нём было видно
    одно: «Ресурс <b>items</b> найден» — угловыми скобками, буквами.

    Всё сообщение экранировалось целиком, вместе с нашей собственной
    разметкой. Плюс двадцать строк дампа поверх двух строк по делу: ответ на
    вопрос «что не так» лежал в самом низу.
    """

    def setUp(self):
        import storage
        from automation import panel
        self._undo = []
        for mod, name, val in (
            (storage, "get_panel_creds", lambda uid: {"cookies": "c=1"}),
            (panel, "panel_create_product_sync",
             lambda *a, **kw: (False, REFUSED)),
        ):
            self._undo.append((mod, name, getattr(mod, name, None)))
            setattr(mod, name, val)

    def tearDown(self):
        for mod, name, old in reversed(self._undo):
            setattr(mod, name, old)

    def report(self, description="Код пополнения"):
        screen = Screen()
        values = {"title": "Apple Gift Card TRY 10 (TR)", "price": 60,
                  "description": description, "quantity": 1, "category": "",
                  "photo_path": None}
        asyncio.run(C._panel_create_and_report(screen, 1, values, extra=None))
        return screen.last

    def test_our_own_tags_are_not_shown_as_letters(self):
        got = self.report()
        self.assertNotIn("&lt;b&gt;", got)
        self.assertNotIn("&lt;code&gt;", got)

    def test_the_reason_comes_before_the_diagnostics(self):
        """Ради него экран и открывают."""
        got = self.report()
        self.assertIn("Регион", got)
        self.assertLess(got.index("Регион"), got.index("tg-spoiler"))

    def test_the_diagnostics_are_folded_away(self):
        """Нужны они раз в сто отказов, а место занимали всегда."""
        got = self.report()
        self.assertIn("<tg-spoiler>", got)
        self.assertIn("filter__8", got)          # не потеряны, просто свёрнуты

    def test_the_field_name_is_not_said_twice(self):
        """Панель уже назвала поле по-русски: «filter__8: Поле Регион
        обязательно» читается как заикание. Различает случаи сама панель —
        своё имя поля она пишет словом «Поле» в начале жалобы, и там, где
        не пишет, техническое имя остаётся (`test_panel_links`)."""
        got = self.report()
        head = got.split("<tg-spoiler>")[0]
        self.assertNotIn("filter__8", head)
        self.assertIn("Поле Регион", head)

    def test_no_advice_is_given_where_we_have_none(self):
        """Совет наугад хуже молчания: он отправляет продавца делать
        бессмысленное. Про регион мастер и так спросит сам."""
        got = self.report()
        self.assertNotIn("Что делать", got.split("<tg-spoiler>")[0])

    def test_a_forbidden_link_is_named_where_it_is(self):
        """Панель говорит «ссылки запрещены», а какая именно — знаем мы."""
        from automation import panel
        old = panel.panel_create_product_sync
        panel.panel_create_product_sync = lambda *a, **kw: (
            False, '{"content": ["Ссылки запрещены."]}')
        try:
            got = self.report(description="Купить тут: roblox.com/redeem")
        finally:
            panel.panel_create_product_sync = old
        self.assertIn("Что делать", got)
        self.assertIn("roblox.com", got)

    def test_the_report_still_arrives_in_one_piece(self):
        """Разметка своя, и Telegram обязан её принять с первой попытки."""
        screen = Screen()
        values = {"title": "Apple <10>", "price": 60, "description": "код",
                  "quantity": 1, "category": "", "photo_path": None}
        asyncio.run(C._panel_create_and_report(screen, 1, values, extra=None))
        self.assertEqual([t[1] for t in screen.tries].count(None), 0)


class ATruncatedReportBreaksOnAWord(unittest.TestCase):
    """«…"subcategor» — так обрывался дамп в сообщении продавца. Пересылая
    его в поддержку, он отправлял огрызок."""

    def test_the_cut_lands_on_a_boundary(self):
        from automation.panel import as_plain
        got = as_plain("раз два три четыре пять шесть семь восемь", 25)
        self.assertTrue(got.endswith("…"))
        self.assertFalse(got[:-1].endswith(" "))
        self.assertIn("раз два три", got)

    def test_a_short_text_is_left_alone(self):
        from automation.panel import as_plain
        self.assertEqual(as_plain("коротко", 100), "коротко")

    def test_our_markup_is_stripped_not_escaped(self):
        from automation.panel import as_plain
        self.assertEqual(as_plain("<b>Ресурс</b> <code>items</code>"),
                         "Ресурс items")

if __name__ == "__main__":
    unittest.main()
