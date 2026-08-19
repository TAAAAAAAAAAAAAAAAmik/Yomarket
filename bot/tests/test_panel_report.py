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


if __name__ == "__main__":
    unittest.main()
