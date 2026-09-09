"""Отзывы читаются по одному, с ником, оценкой, текстом и листанием.

Экран показывал десять отзывов подряд куском текста: понять, кто и что
написал, можно было только вглядываясь, а до одиннадцатого не добраться
вовсе. И счётчик «из N» обязан говорить правду: панель отдаёт отзывы
постранично, и прочитанное — не то же самое, что всё, что есть.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

import handlers.stats as ST          # noqa: E402


def review(n: int, rating=5, text="Всё быстро", author="Иван"):
    return {"id": str(n), "rating": rating,
            "text": f"{text} {n}" if text else "",
            "author": f"{author}{n}", "ts": time.time() - n * 60,
            "title": f"Товар {n}"}


class Screen(unittest.TestCase):
    def setUp(self):
        ST._REVIEWS.clear()
        self._get = ST.get_settings
        ST.get_settings = lambda uid: {}

    def tearDown(self):
        ST._REVIEWS.clear()
        ST.get_settings = self._get

    class CB:
        def __init__(self):
            self.text = ""
            self.kb = None
            self.message = self
            self.data = ""

        async def edit_text(self, text, reply_markup=None, **kw):
            self.text, self.kb = text, reply_markup

        async def answer(self, *a, **kw):
            pass

        from_user = type("U", (), {"id": 1})()

    def show(self, rows, i=0, more=False, err=""):
        async def load(uid, refresh):
            return rows, more, err

        ST._load_reviews = load
        cb = self.CB()
        asyncio.run(ST._show_review(cb, i))
        return cb

    @staticmethod
    def presses(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row
                if b.callback_data]


class OneReviewAtATime(Screen):
    def test_the_buyer_the_rating_and_the_text_are_all_there(self):
        cb = self.show([review(1)])
        self.assertIn("Иван1", cb.text)
        self.assertIn("⭐", cb.text)
        self.assertIn("Всё быстро 1", cb.text)

    def test_the_newest_is_first(self):
        """Панель отдаёт новые первыми — порядок не переставляем."""
        cb = self.show([review(1), review(2)])
        self.assertIn("Иван1", cb.text)
        self.assertNotIn("Иван2", cb.text)

    def test_the_counter_shows_where_we_are(self):
        cb = self.show([review(1), review(2), review(3)], i=1)
        self.assertIn("2 из 3", cb.text)

    def test_a_missing_rating_is_said_plainly(self):
        cb = self.show([review(1, rating=None)])
        self.assertIn("оценки нет", cb.text)

    def test_a_review_without_text_is_not_an_empty_screen(self):
        cb = self.show([review(1, text="")])
        self.assertIn("без текста", cb.text)

    def test_the_rating_is_shown_as_a_number_too(self):
        cb = self.show([review(1, rating=3)])
        self.assertIn("3/5", cb.text)


class TheCounterDoesNotLie(Screen):
    """Панель отдаёт отзывы постранично. «1 из 100» на магазине с тысячей —
    вранье: столько мы прочитали, а не столько их есть."""

    def test_unread_pages_are_marked_with_a_plus(self):
        cb = self.show([review(1), review(2)], more=True)
        self.assertIn("из 2+", cb.text)

    def test_everything_read_shows_a_plain_number(self):
        cb = self.show([review(1), review(2)], more=False)
        self.assertIn("из 2", cb.text)
        self.assertNotIn("2+", cb.text)

    def test_at_the_last_loaded_one_it_says_older_are_not_loaded(self):
        cb = self.show([review(1), review(2)], i=1, more=True)
        self.assertIn("не загружены", cb.text)

    def test_it_does_not_nag_about_that_in_the_middle(self):
        cb = self.show([review(1), review(2)], i=0, more=True)
        self.assertNotIn("не загружены", cb.text)


class TurningThePages(Screen):
    def test_the_first_one_offers_only_forward(self):
        cb = self.show([review(1), review(2)], i=0)
        press = self.presses(cb.kb)
        self.assertIn("stats:rev:1", press)
        self.assertNotIn("stats:rev:0", press)

    def test_the_last_one_offers_only_back(self):
        cb = self.show([review(1), review(2)], i=1)
        press = self.presses(cb.kb)
        self.assertIn("stats:rev:0", press)
        self.assertNotIn("stats:rev:2", press)

    def test_a_jump_to_the_newest_appears_once_you_move(self):
        cb = self.show([review(n) for n in range(5)], i=3)
        self.assertIn("stats:rev:0", self.presses(cb.kb))

    def test_a_single_review_has_no_paging_at_all(self):
        cb = self.show([review(1)])
        self.assertFalse([p for p in self.presses(cb.kb)
                          if p.startswith("stats:rev:")])

    def test_an_index_past_the_end_lands_on_the_last(self):
        """Кнопка из старого сообщения не должна ронять экран."""
        cb = self.show([review(1), review(2)], i=99)
        self.assertIn("2 из 2", cb.text)

    def test_a_negative_index_lands_on_the_first(self):
        cb = self.show([review(1), review(2)], i=-5)
        self.assertIn("1 из 2", cb.text)


class WhenThereIsNothingToShow(Screen):
    def test_no_reviews_says_so_and_still_offers_a_way_back(self):
        cb = self.show([])
        self.assertIn("Отзывов пока нет", cb.text)
        self.assertIn("menu:stats", self.presses(cb.kb))

    def test_a_panel_error_is_shown_not_swallowed(self):
        cb = self.show([], err="401: сессия панели истекла")
        self.assertIn("сессия панели истекла", cb.text)

    def test_an_error_is_not_dressed_up_as_an_empty_shop(self):
        cb = self.show([], err="панель не ответила")
        self.assertNotIn("Отзывов пока нет", cb.text)


class FindingTheTextWhateverTheFieldIsCalled(unittest.TestCase):
    """Отзывы пришли без текста, хотя текст в строке был.

    Имена полей у панели свои, и список ключей их не покрыл. Хуже того,
    поле `user_comment` подходило сразу под «автора» и под «текст», а
    автор проверялся первым — и съедал отзыв молча.
    """

    def parse(self, fields, rating=5):
        from automation.panel import _parse_review
        rows = [{"attribute": "rating", "value": rating}] + [
            {"attribute": a, "value": v} for a, v in fields]
        return _parse_review({"id": 1, "fields": rows})

    def test_a_plain_comment_field_is_the_text(self):
        got = self.parse([("comment", "Всё пришло быстро")])
        self.assertEqual(got["text"], "Всё пришло быстро")

    def test_a_field_that_looks_like_both_goes_to_the_text(self):
        """`user_comment` подходит и под автора, и под текст."""
        got = self.parse([("user_comment", "Спасибо, всё ок")])
        self.assertEqual(got["text"], "Спасибо, всё ок")

    def test_the_author_still_lands_in_the_author(self):
        got = self.parse([("user_name", "Иван"), ("comment", "Хорошо")])
        self.assertEqual(got["author"], "Иван")
        self.assertEqual(got["text"], "Хорошо")

    def test_an_unknown_field_name_is_taken_as_the_text(self):
        """Отзыв — самый длинный текст в своей записи."""
        got = self.parse([("otzyv_ot_pokupatelya",
                           "Продавец молодец, рекомендую")])
        self.assertEqual(got["text"], "Продавец молодец, рекомендую")

    def test_the_longest_one_wins_not_the_first(self):
        got = self.parse([("zzz", "ок"), ("yyy", "Долго ждал, но всё дошло")])
        self.assertEqual(got["text"], "Долго ждал, но всё дошло")

    def test_numbers_are_not_mistaken_for_a_review(self):
        got = self.parse([("some_id", "348211")])
        self.assertEqual(got["text"], "")

    def test_a_row_without_a_rating_gets_no_invented_text(self):
        """Иначе категория с полем `name` начинает считаться отзывом — та
        самая ошибка, из-за которой раньше принимали за отзывы что попало."""
        from automation.panel import _parse_review
        got = _parse_review({"id": 1, "fields": [
            {"attribute": "name", "value": "категория"}]})
        self.assertEqual(got["text"], "")


class ALinkToAnotherRecordIsNotTheReviewText(unittest.TestCase):
    """Продавцу пришло уведомление:

        ⭐ НОВЫЙ ОТЗЫВ
        👤 Покупатель ⭐⭐⭐⭐⭐
        «Andrej Prokopjew»

    Имя покупателя, выданное за текст отзыва. Отзыв был без текста, а
    запасной вариант взял самое длинное поле строки — им оказалась ссылка
    на автора. По имени поля этого не поймать: оно у панели своё. Зато у
    Nova есть `component`, и он говорит прямо: `belongs-to` — ссылка на
    другую запись, `textarea` — свободный текст.
    """

    def parse(self, fields, rating=5):
        from automation.panel import _parse_review
        rows = [{"attribute": "rating", "value": rating}] + [
            dict(f) for f in fields]
        return _parse_review({"id": 1, "fields": rows})

    def test_a_relation_never_becomes_the_review_text(self):
        got = self.parse([{"attribute": "sender", "component": "belongs-to-field",
                           "value": "Andrej Prokopjew"}])
        self.assertEqual(got["text"], "")

    def test_and_it_is_shown_as_the_author_instead(self):
        got = self.parse([{"attribute": "sender", "component": "belongs-to-field",
                           "value": "Andrej Prokopjew"}])
        self.assertEqual(got["author"], "Andrej Prokopjew")

    def test_a_textarea_is_the_text_whatever_it_is_called(self):
        """Короткий отзыв рядом с длинным чужим полем: «самое длинное поле»
        выбрало бы не его."""
        got = self.parse([
            {"attribute": "zzz", "component": "textarea-field", "value": "Ок"},
            {"attribute": "qqq", "component": "text-field",
             "value": "техническая пометка панели подлиннее"}])
        self.assertEqual(got["text"], "Ок")

    def test_the_relation_does_not_steal_a_real_text(self):
        got = self.parse([
            {"attribute": "sender", "component": "belongs-to-field",
             "value": "Andrej Prokopjew"},
            {"attribute": "comment", "component": "textarea-field",
             "value": "Быстро и без вопросов"}])
        self.assertEqual(got["author"], "Andrej Prokopjew")
        self.assertEqual(got["text"], "Быстро и без вопросов")

    def test_a_named_product_relation_stays_the_product(self):
        got = self.parse([
            {"attribute": "item", "component": "belongs-to-field",
             "value": "1000 Stars"},
            {"attribute": "buyer", "component": "belongs-to-field",
             "value": "Andrej Prokopjew"}])
        self.assertEqual(got["title"], "1000 Stars")
        self.assertEqual(got["author"], "Andrej Prokopjew")

    def test_the_notification_says_a_review_has_no_text(self):
        """Уведомление, оборванное на нике, выглядит как сбой бота."""
        import inspect
        from tasks import manager as M
        self.assertIn("без текста", inspect.getsource(M.TaskManager._check_reviews))


class TheReaderReportsWhatItDidNotRead(unittest.TestCase):
    def test_the_panel_helper_returns_the_more_flag(self):
        """Без него экран не может честно посчитать «из N»."""
        import inspect
        from automation import panel as P
        src = inspect.getsource(P.panel_reviews_sync)
        self.assertIn('"more": more', src)


if __name__ == "__main__":
    unittest.main()
