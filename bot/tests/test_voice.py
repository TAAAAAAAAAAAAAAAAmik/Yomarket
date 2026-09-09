"""Бот говорит продавцу на «ты», а его покупателю — на «вы».

Это два разных голоса, и путать их дорого в обе стороны.

Продавцу бот — знакомый, который взял на себя рутину; «вы» здесь звучит как
инструкция к бытовой технике. А вот в чат маркетплейса уходит голос МАГАЗИНА,
обращённый к его клиенту: автоответы, просьба прислать ник, сообщение с кодом.
Магазин, который тыкает покупателю, портит продавцу репутацию, и продавец
об этом даже не узнает — он этих сообщений не видит.

Обе беды случились при переводе 30.08 живьём. Массовая замена причесала под
«ты» и шаблоны для покупателя тоже, а заодно — список слов, по которым бот
УЗНАЁТ жалобу: «верните» превратилось в «верни», и спор с покупателем
перестал бы замечаться вовсе. Оформление сломало функцию.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

BOT = pathlib.Path(__file__).resolve().parents[1]

# Обращение на «вы»: местоимения и повелительное наклонение.
FORMAL_PRON = re.compile(r"\b(вы|вас|вам|вами|ваш\w*)\b", re.I)
FORMAL_VERB = re.compile(r"\b[А-ЯЁа-яё]{4,}(?:йте|ите|ьте)\b")

# Голос магазина, обращённый к покупателю. Здесь «вы» обязано остаться.
BUYER_FILES = {"autoreply.py"}
BUYER_MARKS = (
    "STARS_TEXTS",          # просьба прислать ник, отчёт о выдаче звёзд
    "auto_reply",           # автоответ на новый заказ
    "on_confirmed",         # «заказ подтверждён»
    "on_refunded",          # «возврат оформлен»
    "quick_replies",        # быстрые ответы под рукой у продавца
    "_COMPLAINT_KEYWORDS",  # СЛОВА ПОКУПАТЕЛЯ, по которым узнаётся спор
    "activation=",          # как активировать код — уходит вместе с кодом
    "Ваш код",              # сам код в чат заказа
    "_send_chat",           # всё, что напрямую уходит в чат маркетплейса
)


def ui_strings(path: pathlib.Path):
    """Экранные строки файла: без комментариев и докстрингов.

    Докстринги и комментарии пишутся для разработчика — «вы» там ни при чём,
    и проверять их значит проверять прозу.
    """
    src = path.read_text()
    tree = ast.parse(src)
    docs = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))}
    lines = src.split("\n")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value in docs:
            continue
        # Строки рядом с признаком «это покупателю» пропускаем: у них
        # другой адресат, и правило для них обратное.
        around = "\n".join(lines[max(0, node.lineno - 12):node.lineno + 2])
        if any(mark in around for mark in BUYER_MARKS):
            continue
        yield node.lineno, node.value


def sources():
    for path in sorted(BOT.rglob("*.py")):
        if "tests" in path.parts or path.name in BUYER_FILES:
            continue
        yield path


class TheBotTalksToTheSellerAsAFriend(unittest.TestCase):
    """«Вы» на экране продавца — след старого тона, а не выбор."""

    def test_no_screen_addresses_the_seller_formally(self):
        bad = []
        for path in sources():
            for lineno, text in ui_strings(path):
                if FORMAL_PRON.search(text):
                    bad.append(f"{path.name}:{lineno} {text[:70]}")
        self.assertEqual(bad, [], "на «вы» осталось:\n" + "\n".join(bad))

    def test_no_screen_orders_the_seller_in_the_plural(self):
        """«Введите», «Нажмите» — то же «вы», только глаголом."""
        bad = []
        for path in sources():
            for lineno, text in ui_strings(path):
                if FORMAL_VERB.search(text):
                    bad.append(f"{path.name}:{lineno} {text[:70]}")
        self.assertEqual(bad, [], "повелительное на «вы»:\n" + "\n".join(bad))


class TheShopStillTalksToItsBuyerPolitely(unittest.TestCase):
    """Голос магазина к клиенту переводу не подлежит.

    Продавец этих сообщений не видит: они уходят в чат маркетплейса. Значит
    и заметить, что его магазин начал тыкать покупателям, он не сможет.
    """

    def _texts(self):
        import storage
        from tasks import manager
        out = dict(manager.STARS_TEXTS)
        s = storage._DEFAULT_SETTINGS
        out["reply"] = s["auto_reply"]["message"]
        out["confirmed"] = s["auto_events"]["on_confirmed"]["message"]
        out["refunded"] = s["auto_events"]["on_refunded"]["message"]
        out["quick"] = " ".join(s["quick_replies"])
        return out

    def test_the_buyer_is_addressed_formally(self):
        polite = ("ask", "remind", "reply", "confirmed", "refunded", "quick")
        for key in polite:
            with self.subTest(key):
                text = self._texts()[key]
                self.assertTrue(
                    FORMAL_PRON.search(text) or FORMAL_VERB.search(text),
                    f"«{text}» — магазин тыкает покупателю")

    def test_the_stars_request_asks_politely(self):
        """Самое частое из этих сообщений: его видит каждый покупатель
        звёзд, и оно у продавца в чате заказа."""
        from tasks import manager
        self.assertIn("отправьте", manager.STARS_TEXTS["ask"])
        self.assertIn("пришлите", manager.STARS_TEXTS["remind"])

    def test_the_gift_code_activation_stays_polite(self):
        from automation import giftcards
        for card in (giftcards.APPLE,):
            with self.subTest(card.slug):
                self.assertIn("ваш", card.activation.lower())


class ComplaintWordsMatchWhatBuyersActuallyWrite(unittest.TestCase):
    """Список слов для распознавания спора — это данные, а не текст.

    Массовая замена причесала его под «ты» и превратила «верните» в «верни».
    Покупатель пишет «верните деньги»; со сломанным списком бот не заметил
    бы спор вообще — то есть оформление отключило бы функцию.
    """

    def test_the_word_buyers_use_is_still_matched(self):
        from tasks.manager import _COMPLAINT_KEYWORDS
        joined = " ".join(_COMPLAINT_KEYWORDS)
        self.assertIn("верните", joined,
                      "бот перестал узнавать «верните деньги»")

    def test_a_real_complaint_is_recognised(self):
        """Проверяем следствие, а не наличие слова в списке."""
        from tasks.manager import _COMPLAINT_KEYWORDS
        message = "Здравствуйте, верните деньги, товар не пришёл"
        self.assertTrue(any(w in message.lower() for w in _COMPLAINT_KEYWORDS))


if __name__ == "__main__":
    unittest.main()
