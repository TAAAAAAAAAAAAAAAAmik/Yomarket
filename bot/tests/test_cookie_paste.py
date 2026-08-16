"""Куки Fragment вводятся одной вставкой, а не по одной каждый раз.

Продавец: «опять куки истекли, может сделать так, чтобы бот их сам брал».
Сделать так, чтобы они не истекали, нельзя — срок им ставит сервер
Fragment. Проба `/fragment_cookies` вдобавок показала, что Fragment наши
куки и не обновляет: «переписаны: нет, добавлены: нет». Значит подхватывать
на лету нечего.

Зато можно снять повторяющуюся боль. Массовая вставка была, но советовала
`document.cookie` — а он **не показывает** `stel_token` и `stel_ssid`, они
HttpOnly. То есть вставка приносила единственную куку, без которой можно
обойтись, а две главные вбивались руками. Куки живут часами, так что
«руками» — это по нескольку раз в день.

Полный набор лежит в заголовке `Cookie:` любого запроса к fragment.com и в
том, что даёт «Copy as cURL». Разбирается и то, и другое.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from handlers.plugins import _parse_cookies          # noqa: E402

SSID = "a1b2c3d4_1234567890"
TOKEN = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
TON = "AgAAAABxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class TheCookieHeaderIsEnoughOnItsOwn(unittest.TestCase):
    """Ровно то, что копируется из Network → Request Headers → Cookie."""

    HEADER = f"stel_ssid={SSID}; stel_token={TOKEN}; stel_ton_token={TON}"

    def test_all_three_arrive_in_one_paste(self):
        got = _parse_cookies(self.HEADER)
        self.assertEqual(got["stel_ssid"], SSID)
        self.assertEqual(got["stel_token"], TOKEN)
        self.assertEqual(got["stel_ton_token"], TON)

    def test_the_header_name_does_not_have_to_be_stripped_by_hand(self):
        """Люди копируют строку целиком, вместе с «Cookie:»."""
        self.assertEqual(_parse_cookies(f"Cookie: {self.HEADER}"),
                         _parse_cookies(self.HEADER))

    def test_lowercase_too(self):
        self.assertEqual(_parse_cookies(f"cookie: {self.HEADER}"),
                         _parse_cookies(self.HEADER))

    def test_the_old_document_cookie_form_still_works(self):
        """Кто привык — не должен обнаружить, что привычное сломали."""
        got = _parse_cookies(f"stel_ton_token={TON}")
        self.assertEqual(got, {"stel_ton_token": TON})


class CopyAsCurlIsUnderstoodToo(unittest.TestCase):
    """«Copy as cURL» продавец уже знает по прошлым разборам — пусть
    работает и здесь, чтобы не объяснять третий способ."""

    def test_cookies_are_taken_from_the_b_flag(self):
        curl = (f"curl 'https://fragment.com/stars/buy' "
                f"-H 'user-agent: Mozilla/5.0' "
                f"-b 'stel_ssid={SSID}; stel_token={TOKEN}'")
        got = _parse_cookies(curl)
        self.assertEqual(got, {"stel_ssid": SSID, "stel_token": TOKEN})

    def test_and_from_a_cookie_header_flag(self):
        curl = (f"curl 'https://fragment.com/api' "
                f"-H 'cookie: stel_ssid={SSID}; stel_token={TOKEN}' "
                f"-H 'accept: */*'")
        got = _parse_cookies(curl)
        self.assertEqual(got, {"stel_ssid": SSID, "stel_token": TOKEN})

    def test_other_headers_are_not_mistaken_for_cookies(self):
        """В cURL полно строк со знаком «равно» — раньше они разобрались бы
        как куки и легли в хранилище вместе с настоящими."""
        curl = (f"curl 'https://fragment.com/api?method=searchStarsRecipient' "
                f"-H 'sec-ch-ua-platform: \"Android\"' "
                f"-b 'stel_ssid={SSID}' "
                f"--data-raw 'quantity=50'")
        got = _parse_cookies(curl)
        self.assertEqual(list(got), ["stel_ssid"])

    def test_double_quotes_are_fine(self):
        """Windows cmd копирует cURL с двойными кавычками."""
        curl = f'curl "https://fragment.com" -b "stel_token={TOKEN}"'
        self.assertEqual(_parse_cookies(curl), {"stel_token": TOKEN})


class NothingBrokenComesThrough(unittest.TestCase):
    def test_empty_input_gives_nothing(self):
        self.assertEqual(_parse_cookies(""), {})
        self.assertEqual(_parse_cookies("   "), {})

    def test_prose_is_not_a_cookie(self):
        self.assertEqual(_parse_cookies("не знаю где это брать"), {})

    def test_a_name_with_spaces_is_rejected(self):
        """Иначе кусок команды становится «кукой» и уезжает в хранилище."""
        self.assertEqual(_parse_cookies("мой токен = abc"), {})

    def test_a_value_less_cookie_is_skipped(self):
        got = _parse_cookies(f"stel_ssid=; stel_token={TOKEN}")
        self.assertEqual(got, {"stel_token": TOKEN})

    def test_a_value_with_equals_inside_survives(self):
        """base64 кончается на «=», и обрезать по первому знаку нельзя."""
        got = _parse_cookies("stel_ton_token=AgAAA==")
        self.assertEqual(got["stel_ton_token"], "AgAAA==")


class TheScreenTellsThePathThatActuallyWorks(unittest.TestCase):
    """Экран советовал `document.cookie` — единственный источник, где двух
    главных кук нет. Совет, который заведомо не приводит к цели, — это то
    же обещание невозможного, что и кнопка в тупик."""

    def source(self) -> str:
        import inspect

        from handlers import plugins as P
        return inspect.getsource(P.stars_set_cookies_prompt)

    def test_it_names_the_request_header(self):
        self.assertIn("Request Headers", self.source())

    def test_and_mentions_copy_as_curl(self):
        self.assertIn("cURL", self.source())

    def test_it_no_longer_leads_with_document_cookie(self):
        src = self.source()
        self.assertLess(src.index("Request Headers"), src.index("document.cookie"))

    def test_and_still_explains_why_httponly_matters(self):
        self.assertIn("HttpOnly", self.source())

    def test_the_message_with_cookies_is_deleted(self):
        import inspect

        from handlers import plugins as P
        self.assertIn("message.delete()",
                      inspect.getsource(P.stars_set_cookies_input))


if __name__ == "__main__":
    unittest.main()
