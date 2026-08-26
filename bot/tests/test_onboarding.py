"""Регистрация клиента: первый экран, ввод токена, отказ.

Это единственная часть бота, которую видит человек, ещё ничего не
настроивший, — и до сих пор на неё не было ни одного теста. Отсюда две
беды, которые здесь и ловятся.

Первая: на отказ печаталось `HTTP 401: {"message":"Unauthenticated."}`.
Английский код ошибки на экране продавца — отписка; хуже того, из него не
следует, что делать, а делать в разных случаях надо противоположное: при
лежащем маркетплейсе — подождать, при отозванном токене — создать новый.

Вторая: у первого экрана не было ни одной кнопки, и токен оставался
висеть в переписке навсегда.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                            # noqa: E402
import handlers.start as S                                # noqa: E402
from api.yoomarket import auth_trouble                    # noqa: E402


def run(coro):
    """Свой цикл на каждый вызов.

    `get_event_loop()` берёт общий на весь прогон, и соседний модуль тестов,
    закрывший свой, роняет здесь шестнадцать проверок сообщением «нет
    текущего цикла» — то есть падает не то, что сломано.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Подделки: настоящие Message/CallbackQuery требуют живого бота.
# ---------------------------------------------------------------------------

class Sent:
    """Отправленное сообщение — его же потом правят на месте."""

    def __init__(self, text, markup=None):
        self.text, self.markup = text, markup
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))
        self.text, self.markup = text, reply_markup

    @property
    def now(self) -> str:
        return self.text


class FakeMessage:
    def __init__(self, text="", uid=7, deletable=True):
        self.text = text
        self.from_user = type("U", (), {"id": uid})()
        self.sent: list[Sent] = []
        self.deleted = False
        self._deletable = deletable

    async def answer(self, text, reply_markup=None, **kw):
        s = Sent(text, reply_markup)
        self.sent.append(s)
        return s

    async def delete(self):
        if not self._deletable:
            raise RuntimeError("message can't be deleted")
        self.deleted = True


class FakeState:
    def __init__(self):
        self.state = None
        self.cleared = 0
        self.data: dict = {}

    async def set_state(self, st):
        self.state = st

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared += 1
        self.state = None
        self.data = {}


class FakeAPI:
    """Ответ маркетплейса на /check — или отказ."""

    def __init__(self, token):
        self.token = token

    async def start(self):
        pass

    async def close(self):
        pass

    async def check(self):
        if FakeAPI.refusal:
            raise RuntimeError(FakeAPI.refusal)
        return {"shop": {"title": FakeAPI.shop}}

    refusal: str = ""
    shop: str = "Мой магазин"


def texts(markup) -> list[str]:
    return [] if markup is None else [b.text for row in markup.inline_keyboard
                                      for b in row]


# ---------------------------------------------------------------------------


class ARefusalIsExplainedInRussian(unittest.TestCase):
    """`auth_trouble` — перевод отказа и совет, что делать."""

    def test_an_unrecognised_token_is_not_shown_as_an_english_code(self):
        why, what, _ours = auth_trouble('HTTP 401: {"message":"Unauthenticated."}', "abc")
        self.assertNotIn("Unauthenticated", why)
        self.assertIn("не признал", why.lower())
        self.assertTrue(what.strip())

    def test_a_marketplace_that_is_down_is_not_blamed_on_the_token(self):
        # Совет «создайте токен заново» здесь отправит искать поломку не там.
        _why, what, ours = auth_trouble("timeout: Юмаркет не ответил вовремя", "abc")
        self.assertNotIn("создайте", what.lower())
        self.assertIn("ни при чём", what.lower())
        self.assertFalse(ours, "поломка маркетплейса записана в вину токену")

    def test_a_revoked_token_is_blamed_on_the_token(self):
        *_rest, ours = auth_trouble("HTTP 401", "abc")
        self.assertTrue(ours)

    def test_a_rate_limit_is_not_blamed_on_the_token_either(self):
        *_rest, ours = auth_trouble("HTTP 429", "abc")
        self.assertFalse(ours)

    def test_a_revoked_token_is_advised_to_be_recreated(self):
        _why, what, _ours = auth_trouble("HTTP 401", "abc")
        self.assertIn("нов", what.lower())

    def test_missing_rights_and_a_wrong_token_get_different_advice(self):
        _w1, a1, _ours = auth_trouble("HTTP 403 forbidden", "abc")
        _w2, a2, _ours = auth_trouble("HTTP 401", "abc")
        self.assertNotEqual(a1, a2)

    def test_a_rate_limit_does_not_advise_re_creating_the_token(self):
        # Токен исправен, его просто спросили слишком часто.
        _why, what, ours = auth_trouble("HTTP 429 too_many_requests", "abc")
        self.assertNotIn("создайте", what.lower())
        self.assertFalse(ours)

    def test_an_unknown_refusal_is_passed_through_and_not_swallowed(self):
        # Молчаливое «что-то пошло не так» — ровно то, из-за чего пишут в
        # поддержку. Непонятый ответ показывается как есть.
        _why, what, _ours = auth_trouble("HTTP 418: I am a teapot", "abc")
        self.assertIn("teapot", what)

    def test_a_pasted_link_is_noticed_but_the_marketplace_still_decides(self):
        why, what, _ours = auth_trouble("HTTP 401", "https://panel.yoomarket.net")
        self.assertIn("ссылка", what.lower())
        self.assertIn("не признал", why.lower())

    def test_several_words_are_noticed(self):
        _why, what, _ours = auth_trouble("HTTP 401", "мой токен abc123")
        self.assertIn("несколько слов", what.lower())

    def test_a_plain_token_earns_no_remark_about_its_shape(self):
        # Какой формы бывают токены Юмаркета, мы не знаем. Замечание «это не
        # похоже на токен» на настоящем токене — отказ по догадке.
        _why, what, _ours = auth_trouble("HTTP 401", "wli-QQ1")
        self.assertNotIn("❗️", what)


class TheFirstScreenHasSomethingToPress(unittest.TestCase):
    """Приветствие было текстом без единой кнопки: ссылку на панель надо
    было выцепить из абзаца, а застрявшему — некуда деться."""

    def test_the_welcome_offers_the_panel_and_a_way_out_of_being_stuck(self):
        kb = texts(S._welcome_kb())
        self.assertTrue(any("панел" in t.lower() for t in kb), kb)
        self.assertTrue(any("токен" in t.lower() for t in kb), kb)

    def test_the_panel_button_is_a_link_and_not_a_dead_callback(self):
        row = S._welcome_kb().inline_keyboard[0]
        panel = [b for b in row if "панел" in b.text.lower()][0]
        self.assertEqual(panel.url, S.PANEL_URL)

    def test_the_help_screen_leads_back_and_not_into_itself(self):
        kb = texts(S._welcome_kb(back=True))
        self.assertFalse(any("Не нахожу" in t for t in kb), kb)
        self.assertTrue(any("подключению" in t.lower() for t in kb), kb)


class ReadingTheHelpDoesNotCancelTheRegistration(unittest.TestCase):
    """Экран помощи открывается посреди ожидания токена.

    Если при этом сбросить состояние, вставленный после чтения токен уйдёт
    в никуда: ловить его будет уже не эта форма, а общий обработчик, и
    человек получит молчание в ответ на правильный токен.
    """

    def setUp(self):
        self.cb = type("C", (), {})()
        self.cb.message = FakeMessage()
        self.cb.message.edit_text = self.cb.message.answer

        async def answer(*a, **kw):
            pass
        self.cb.answer = answer
        self.state = FakeState()

    def test_the_help_screen_keeps_waiting_for_the_token(self):
        run(S.token_help(self.cb, self.state))
        self.assertEqual(self.state.state, S.AuthState.waiting_for_token)

    def test_going_back_keeps_waiting_too(self):
        run(S.back_to_welcome(self.cb, self.state))
        self.assertEqual(self.state.state, S.AuthState.waiting_for_token)


class TheTokenDoesNotStayInTheChat(unittest.TestCase):
    """Токен — ключ от чужого магазина, и в переписке он остаётся навсегда."""

    def setUp(self):
        self.saved = {}
        for name in ("save_token", "save_shop_name"):
            self.saved[name] = getattr(S, name)
            setattr(S, name, lambda *a, **kw: None)
        self.saved["API"] = S.YooMarketAPI
        S.YooMarketAPI = FakeAPI
        self.saved["creds"] = storage.get_panel_creds
        storage.get_panel_creds = lambda uid: {"email": "a@b.c"}
        FakeAPI.refusal = ""
        FakeAPI.shop = "Мой магазин"
        self.saved["menu"] = S._send_menu

        async def no_menu(*a, **kw):
            pass
        S._send_menu = no_menu

    def tearDown(self):
        for name in ("save_token", "save_shop_name"):
            setattr(S, name, self.saved[name])
        S.YooMarketAPI = self.saved["API"]
        S._send_menu = self.saved["menu"]
        storage.get_panel_creds = self.saved["creds"]

    def test_the_message_carrying_the_token_is_deleted(self):
        m = FakeMessage("secret-token")
        run(S.process_token(m, FakeState()))
        self.assertTrue(m.deleted)

    def test_a_failed_deletion_is_admitted_and_not_claimed_as_done(self):
        m = FakeMessage("secret-token", deletable=False)
        run(S.process_token(m, FakeState()))
        said = " ".join(s.now for s in m.sent)
        self.assertIn("сотрите его вручную", said)
        self.assertNotIn("удалено из переписки", said)

    def test_a_successful_deletion_is_reported(self):
        m = FakeMessage("secret-token")
        run(S.process_token(m, FakeState()))
        self.assertIn("удалено из переписки", " ".join(s.now for s in m.sent))

    def test_the_token_is_never_echoed_back_to_the_screen(self):
        m = FakeMessage("secret-token")
        run(S.process_token(m, FakeState()))
        for s in m.sent:
            self.assertNotIn("secret-token", s.now)


class OneScreenInsteadOfAPileOfMessages(unittest.TestCase):
    """«⏳ Проверяю токен…» уходило новым сообщением, отказ — ещё одним, и
    к третьей попытке какой из них последний, приходилось искать глазами."""

    def setUp(self):
        self.saved = {"API": S.YooMarketAPI, "save": S.save_token}
        S.YooMarketAPI = FakeAPI
        S.save_token = lambda *a, **kw: None
        FakeAPI.refusal = "HTTP 401"

    def tearDown(self):
        S.YooMarketAPI = self.saved["API"]
        S.save_token = self.saved["save"]

    def test_a_refusal_rewrites_the_waiting_message(self):
        m = FakeMessage("bad")
        run(S.process_token(m, FakeState()))
        self.assertEqual(len(m.sent), 1, [s.now for s in m.sent])
        self.assertTrue(m.sent[0].edits, "сообщение «проверяю» не переписано")

    def test_a_refusal_that_is_not_the_tokens_fault_says_to_wait(self):
        # «Токен ни при чём» и следующей строкой «пришлите токен ещё раз» —
        # это экран, спорящий сам с собой.
        FakeAPI.refusal = "timeout: Юмаркет не ответил вовремя"
        m = FakeMessage("good-token")
        run(S.process_token(m, FakeState()))
        self.assertIn("через пару минут", m.sent[0].now)

    def test_a_refusal_that_is_the_tokens_fault_says_to_send_it_again_now(self):
        FakeAPI.refusal = "HTTP 401"
        m = FakeMessage("bad")
        run(S.process_token(m, FakeState()))
        self.assertIn("ещё раз — я жду", m.sent[0].now)
        self.assertNotIn("через пару минут", m.sent[0].now)

    def test_the_refusal_screen_offers_a_way_forward(self):
        m = FakeMessage("bad")
        run(S.process_token(m, FakeState()))
        self.assertTrue(texts(m.sent[0].markup))

    def test_the_raw_answer_is_still_shown_for_support(self):
        # Перевод не должен прятать то, что на самом деле ответил сервер:
        # с этой строкой идут в поддержку Юмаркета.
        FakeAPI.refusal = "HTTP 401: Unauthenticated."
        m = FakeMessage("bad")
        run(S.process_token(m, FakeState()))
        self.assertIn("Unauthenticated", m.sent[0].now)


class ARefusedTokenIsNotSaved(unittest.TestCase):
    """Сохранённый нерабочий токен — это бот, который молча ничего не делает."""

    def setUp(self):
        self.saved = {"API": S.YooMarketAPI, "save": S.save_token}
        self.stored: list = []
        S.YooMarketAPI = FakeAPI
        S.save_token = lambda uid, tok: self.stored.append(tok)

    def tearDown(self):
        S.YooMarketAPI = self.saved["API"]
        S.save_token = self.saved["save"]

    def test_a_refused_token_is_not_written_to_storage(self):
        FakeAPI.refusal = "HTTP 401"
        run(S.process_token(FakeMessage("bad"), FakeState()))
        self.assertEqual(self.stored, [])

    def test_the_form_keeps_waiting_after_a_refusal(self):
        FakeAPI.refusal = "HTTP 401"
        st = FakeState()
        run(S.process_token(FakeMessage("bad"), st))
        self.assertEqual(st.cleared, 0, "форма закрылась, токен ввести уже некуда")

    def test_an_empty_message_does_not_go_to_the_marketplace(self):
        FakeAPI.refusal = "HTTP 401"
        m = FakeMessage("   ")
        run(S.process_token(m, FakeState()))
        self.assertFalse(m.deleted)
        self.assertIn("одной строкой", m.sent[0].now)


class AShopNameFromTheMarketplaceCannotBreakTheScreen(unittest.TestCase):
    """Название магазина приходит с маркетплейса и уходит в HTML-сообщение.

    Одиночный `<` в нём роняет отправку целиком — и продавец, только что
    подключившийся, не видит вообще ничего.
    """

    def setUp(self):
        self.saved = {"API": S.YooMarketAPI, "save": S.save_token,
                      "name": S.save_shop_name, "creds": storage.get_panel_creds,
                      "menu": S._send_menu, "get": S.get_shop_name}
        S.YooMarketAPI = FakeAPI
        S.save_token = lambda *a, **kw: None
        S.save_shop_name = lambda *a, **kw: None
        storage.get_panel_creds = lambda uid: {"email": "a@b.c"}
        FakeAPI.refusal = ""
        FakeAPI.shop = "<b>Ломающий</b> магазин & Co"

        async def no_menu(*a, **kw):
            pass
        S._send_menu = no_menu

    def tearDown(self):
        S.YooMarketAPI = self.saved["API"]
        S.save_token = self.saved["save"]
        S.save_shop_name = self.saved["name"]
        S._send_menu = self.saved["menu"]
        S.get_shop_name = self.saved["get"]
        storage.get_panel_creds = self.saved["creds"]

    def test_the_name_is_escaped_before_it_reaches_the_message(self):
        m = FakeMessage("good")
        run(S.process_token(m, FakeState()))
        shown = m.sent[0].now
        self.assertIn("&lt;b&gt;Ломающий&lt;/b&gt;", shown)
        self.assertIn("&amp; Co", shown)

    def test_the_main_menu_escapes_it_too(self):
        S.get_shop_name = lambda uid: "<i>Магазин</i>"
        S._send_menu = self.saved["menu"]
        m = FakeMessage()
        run(S._send_menu(m, 7))
        self.assertIn("&lt;i&gt;Магазин&lt;/i&gt;", m.sent[0].now)


class ATransientFailureDoesNotCostTheTokenForever(unittest.TestCase):
    """Сообщение с токеном мы удаляем сразу, а панель показывает токен ровно
    один раз. Если после этого маркетштейс не ответил, продавец остаётся ни
    с чем: скопировать токен заново неоткуда, потому что панель его больше
    не покажет. Поэтому на отказе, в котором токен не виноват, он остаётся
    у формы и предлагается кнопкой.
    """

    def setUp(self):
        self.saved = {"API": S.YooMarketAPI, "save": S.save_token,
                      "name": S.save_shop_name, "creds": storage.get_panel_creds,
                      "menu": S._send_menu}
        S.YooMarketAPI = FakeAPI
        self.stored: list = []
        S.save_token = lambda uid, tok: self.stored.append(tok)
        S.save_shop_name = lambda *a, **kw: None
        storage.get_panel_creds = lambda uid: {"email": "a@b.c"}

        async def no_menu(*a, **kw):
            pass
        S._send_menu = no_menu

    def tearDown(self):
        S.YooMarketAPI = self.saved["API"]
        S.save_token = self.saved["save"]
        S.save_shop_name = self.saved["name"]
        S._send_menu = self.saved["menu"]
        storage.get_panel_creds = self.saved["creds"]

    def test_a_token_the_marketplace_could_not_check_is_kept(self):
        FakeAPI.refusal = "timeout: Юмаркет не ответил вовремя"
        st = FakeState()
        run(S.process_token(FakeMessage("good-token"), st))
        self.assertEqual(st.data.get("token"), "good-token")

    def test_a_refused_token_is_not_kept(self):
        # Предлагать «повторить» с токеном, который уже отвергли,
        # значит обещать, что со второго раза выйдет.
        FakeAPI.refusal = "HTTP 401"
        st = FakeState()
        run(S.process_token(FakeMessage("bad"), st))
        self.assertFalse(st.data.get("token"))

    def test_the_retry_button_is_offered_only_when_it_can_help(self):
        FakeAPI.refusal = "timeout: Юмаркет не ответил вовремя"
        m = FakeMessage("good-token")
        run(S.process_token(m, FakeState()))
        self.assertTrue(any("Повторить" in t for t in texts(m.sent[0].markup)))

        FakeAPI.refusal = "HTTP 401"
        m = FakeMessage("bad")
        run(S.process_token(m, FakeState()))
        self.assertFalse(any("Повторить" in t for t in texts(m.sent[0].markup)))

    def test_pressing_retry_connects_with_the_remembered_token(self):
        FakeAPI.refusal = "timeout: Юмаркет не ответил вовремя"
        st = FakeState()
        run(S.process_token(FakeMessage("good-token"), st))

        FakeAPI.refusal = ""                      # маркетплейс ожил
        cb = _callback()
        run(S.retry_token(cb, st))
        self.assertEqual(self.stored, ["good-token"])

    def test_retry_without_a_remembered_token_says_so_instead_of_pretending(self):
        cb = _callback()
        run(S.retry_token(cb, FakeState()))
        self.assertEqual(self.stored, [])
        self.assertTrue(cb.alerts, "нажатие ушло в тишину")


def _callback():
    c = type("C", (), {})()
    c.from_user = type("U", (), {"id": 7})()
    c.message = FakeMessage()
    c.message.edit_text = _as_edit(c.message)
    c.alerts: list = []

    async def answer(text="", **kw):
        c.alerts.append(text)
    c.answer = answer
    return c


def _as_edit(m: FakeMessage):
    async def edit(text, reply_markup=None, **kw):
        m.sent.append(Sent(text, reply_markup))
    return edit


class TheFormIsStillWiredToTheHandler(unittest.TestCase):
    """Хелперы, вставленные между декоратором и функцией, забирают
    регистрацию себе: `@router.message(...)` привязывается к тому, что идёт
    следом. Снаружи это выглядит как бот, молчащий на правильный токен."""

    def test_the_token_handler_is_the_one_registered_for_the_form(self):
        names = [h.callback.__name__ for h in S.router.message.handlers]
        self.assertIn("process_token", names)

    def test_the_help_and_back_buttons_have_handlers(self):
        names = [h.callback.__name__ for h in S.router.callback_query.handlers]
        self.assertIn("token_help", names)
        self.assertIn("back_to_welcome", names)
        self.assertIn("retry_token", names)


if __name__ == "__main__":
    unittest.main()
