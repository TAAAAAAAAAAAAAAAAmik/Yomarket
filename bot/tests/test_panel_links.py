"""Ссылки в описании товара: панель их не принимает.

Живой отказ 19.08, при создании товара из плагина Robux:

    {"content": ["Ссылки запрещены. Разрешены только: Warface, YouTube,
     Rutube, VKVideo, Google Диск, Яндекс Диск, Майл Диск"]}

Отправляли мы своё же описание — «Активируется на roblox.com → …». То есть
товар, заведённый ботом, панель не приняла бы никогда, и выяснялось это на
**последнем** шаге мастера, когда продавец уже ввёл всё.

Здесь три вещи. Наше описание больше не содержит домена. Ссылка продавца
ловится на шаге описания, а не в конце. И отказ панели читается словами, а
не разбирается в JSON внутри отладочной простыни: сообщение на экране было и
раньше, а прочитать в нём, что делать, было нельзя.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import panel as P                  # noqa: E402

REFUSAL = ('Ошибка 422:\n{"content": ["Ссылки запрещены. Разрешены только: '
           'Warface, YouTube, Rutube, VKVideo, Google Диск, Яндекс Диск, '
           'Майл Диск"]}\nОтправлено: {"title": "200 Robux", "price": 349}')


class ALinkIsFoundBeforeThePanelRefusesIt(unittest.TestCase):
    def test_a_bare_domain_is_a_link(self):
        self.assertEqual(P.link_trouble("Активируется на roblox.com → далее"),
                         "roblox.com")

    def test_an_http_address_too(self):
        self.assertIn("http", P.link_trouble("Инструкция https://example.org/x"))

    def test_www_without_a_scheme_too(self):
        self.assertIn("www.", P.link_trouble("см. www.example.org"))

    def test_an_allowed_service_passes(self):
        """Белый список панели — её решение, не наше."""
        self.assertEqual(P.link_trouble("Видео: youtube.com/watch?v=1"), "")
        self.assertEqual(P.link_trouble("Файл на disk.yandex.ru/i/1"), "")

    def test_a_price_with_a_dot_is_not_a_link(self):
        """Ложная тревога здесь дороже пропуска: она не даст продавцу
        сохранить нормальное описание, а панель бы его приняла."""
        self.assertEqual(P.link_trouble("Цена 1.500 руб, ответ за 24 часа"), "")

    def test_ordinary_russian_text_is_not_a_link(self):
        self.assertEqual(
            P.link_trouble("Код активируется в самом Roblox. Выдача сразу."), "")

    def test_the_found_link_is_returned_not_just_a_flag(self):
        """Продавцу надо показать, что править, иначе он ищет её глазами."""
        self.assertEqual(P.link_trouble("текст bad-site.xyz текст"),
                         "bad-site.xyz")


class OurOwnDescriptionPassesThePanel(unittest.TestCase):
    """Товар, заведённый ботом, панель обязана принять: продавец не писал это
    описание и исправить его не может."""

    def description(self) -> str:
        import inspect

        from handlers import plugins as PL
        src = inspect.getsource(PL)
        start = src.index('description=("Код на пополнение Robux')
        return src[start:src.index("),", start)]

    def test_it_has_no_link_at_all(self):
        self.assertEqual(P.link_trouble(self.description()), "")

    def test_and_no_domain_slipped_back_in(self):
        self.assertNotIn("roblox.com", self.description())

    def test_it_still_says_where_to_activate(self):
        """Убрать домен — не значит убрать смысл: покупателю нужно знать,
        куда идти с кодом."""
        self.assertIn("Roblox", self.description())
        self.assertIn("Использовать код", self.description())


class TheRefusalIsReadableNotJson(unittest.TestCase):
    def test_the_field_is_named_as_the_seller_calls_it(self):
        said = P.explain_validation(REFUSAL)
        self.assertIn("описание", said)
        self.assertNotIn("content", said)

    def test_the_reason_survives_word_for_word(self):
        """Правило придумано не нами — пересказывать его своими словами
        значит однажды пересказать неверно."""
        self.assertIn("Ссылки запрещены", P.explain_validation(REFUSAL))

    def test_the_second_json_in_the_report_does_not_break_it(self):
        """В отчёте их два: отказ панели и наше «Отправлено: {…}». Жадный
        захват от первой скобки до последней давал невалидный JSON — то есть
        молчание вместо объяснения."""
        self.assertTrue(P.explain_validation(REFUSAL))

    def test_several_fields_are_all_named(self):
        said = P.explain_validation('{"title": ["Слишком длинное"], '
                                    '"price": ["Обязательное поле"]}')
        self.assertIn("название", said)
        self.assertIn("цена", said)

    def test_an_unknown_field_is_shown_as_it_came(self):
        """Незнакомое поле лучше показать по-английски, чем промолчать."""
        self.assertIn("filter__7",
                      P.explain_validation('{"filter__7": ["Обязательное"]}'))

    def test_text_without_json_is_left_alone(self):
        self.assertEqual(P.explain_validation("HTTP 500: сервер лёг"), "")

    def test_nothing_is_invented_for_an_empty_answer(self):
        self.assertEqual(P.explain_validation(""), "")


class Msg:
    def __init__(self, text):
        self.text = text
        self.said: list[str] = []

    async def answer(self, text, **kw):
        self.said.append(text)
        return self


class FSM:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class TheSellerIsStoppedAtTheDescriptionStep(unittest.TestCase):
    """Отказ на шаге описания дешевле отказа на последнем шаге: править одно
    поле, а не проходить мастер заново. Это то же правило, по которому фото
    стало обязательным на своём шаге."""

    def enter(self, text):
        from handlers import create_ad as C
        msg, fsm = Msg(text), FSM()
        asyncio.run(C.ad_description(msg, fsm))
        return msg, fsm

    def test_a_link_is_refused_right_away(self):
        msg, fsm = self.enter("Активируется на roblox.com → Пополнить")
        self.assertIn("ссылк", "\n".join(msg.said).lower())
        self.assertIsNone(fsm.data.get("description"))

    def test_the_offending_link_is_shown(self):
        msg, _ = self.enter("см. bad-site.xyz")
        self.assertIn("bad-site.xyz", "\n".join(msg.said))

    def test_and_the_step_is_not_left(self):
        """Иначе продавец уедет на следующий шаг с непринятым описанием."""
        _msg, fsm = self.enter("см. bad-site.xyz")
        self.assertIsNone(fsm.state)

    def test_a_clean_description_goes_through(self):
        _msg, fsm = self.enter("Код активируется в самом Roblox. Выдача сразу.")
        self.assertIn("Roblox", fsm.data.get("description") or "")

    def test_an_allowed_service_goes_through_too(self):
        _msg, fsm = self.enter("Инструкция: youtube.com/watch?v=1")
        self.assertTrue(fsm.data.get("description"))


if __name__ == "__main__":
    unittest.main()
