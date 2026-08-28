"""Экран правовых документов: /policy.

Соглашение, оферта и политика — то, на что ссылаются, когда спорят о
деньгах, поэтому у экрана два требования сверх обычных.

Первое: кнопка есть только у документа, ссылка на который задана. Кнопка,
ведущая в никуда, обещает документ, которого нет, а кнопка с пустым
адресом вдобавок роняет отправку целиком — такую клавиатуру Telegram не
принимает, и клиент не увидит экран вовсе.

Второе: пока владелец не добавил ни одного документа, об этом сказано
вслух. Пустой экран с одной кнопкой «Назад» читается как поломка бота.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "x")

import storage                                             # noqa: E402
import handlers.admin as A                                 # noqa: E402
import handlers.policy as P                                # noqa: E402


def run(coro):
    return asyncio.run(coro)


def rows(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


class Store(unittest.TestCase):
    """Общее хранилище админки подменяется на словарь в памяти.

    Без отката в tearDown соседние тесты падали бы «сами по себе» — это уже
    случалось в этом проекте с `promo_params`.
    """

    def setUp(self):
        self.admin: dict = {}
        self._load, self._save = storage._load_admin, storage._save_admin
        storage._load_admin = lambda: self.admin
        storage._save_admin = lambda d: self.admin.update(d)

    def tearDown(self):
        storage._load_admin, storage._save_admin = self._load, self._save

    def set_all(self):
        for key, _title in storage.POLICY_DOCS:
            storage.set_policy_link(key, f"https://example.org/{key}")


class OnlyDocumentsThatExistGetAButton(Store):

    def test_with_no_links_there_are_no_document_buttons(self):
        self.assertEqual(rows(P.policy_keyboard()), [["⬅️ Назад"]])

    def test_all_three_documents_show_up_when_all_are_set(self):
        self.set_all()
        self.assertEqual(rows(P.policy_keyboard()), [
            ["📜 Пользовательское соглашение"],
            ["📄 Публичная оферта"],
            ["🔒 Политика конфиденциальности"],
            ["⬅️ Назад"],
        ])

    def test_a_document_without_a_link_is_simply_absent(self):
        storage.set_policy_link("offer", "https://example.org/offer")
        got = [t for row in rows(P.policy_keyboard()) for t in row]
        self.assertIn("📄 Публичная оферта", got)
        self.assertNotIn("📜 Пользовательское соглашение", got)

    def test_a_blank_link_is_the_same_as_no_link(self):
        # Пустой адрес в кнопке — не «ведёт никуда», а отказ Telegram
        # принять всю клавиатуру: экран не приходит вовсе.
        storage.set_policy_link("terms", "   ")
        self.assertEqual(rows(P.policy_keyboard()), [["⬅️ Назад"]])

    def test_rubbish_written_straight_into_storage_is_ignored(self):
        # Хранилище — это правимый JSON: значение может попасть туда мимо
        # `set_policy_link` — руками, из прежней версии формата, прямой
        # записью в базу. Пустая строка оттуда означает кнопку с пустым
        # адресом, то есть экран, который не отправится вообще ни у кого.
        self.admin["policy_links"] = {"terms": "  ", "offer": "", "privacy": None}
        self.assertEqual(storage.get_policy_links(), {})
        self.assertEqual(rows(P.policy_keyboard()), [["⬅️ Назад"]])

    def test_a_good_link_survives_a_bad_neighbour_in_storage(self):
        self.admin["policy_links"] = {"terms": "", "offer": "https://ok.example"}
        self.assertEqual(list(storage.get_policy_links()), ["offer"])

    def test_the_buttons_are_links_and_not_callbacks(self):
        self.set_all()
        docs = [b for b in flat(P.policy_keyboard()) if "Назад" not in b.text]
        self.assertEqual(len(docs), 3)
        for b in docs:
            self.assertTrue(b.url, b.text)
            self.assertIsNone(b.callback_data)

    def test_every_button_carries_the_link_that_was_saved(self):
        storage.set_policy_link("privacy", "https://shop.example/privacy.pdf")
        b = [x for x in flat(P.policy_keyboard()) if "Политика" in x.text][0]
        self.assertEqual(b.url, "https://shop.example/privacy.pdf")

    def test_removing_a_link_removes_its_button(self):
        self.set_all()
        storage.clear_policy_link("offer")
        got = [t for row in rows(P.policy_keyboard()) for t in row]
        self.assertNotIn("📄 Публичная оферта", got)
        self.assertIn("📜 Пользовательское соглашение", got)

    def test_back_always_leads_somewhere(self):
        back = [b for b in flat(P.policy_keyboard()) if "Назад" in b.text][0]
        self.assertTrue(back.callback_data)


class AnEmptyScreenExplainsItselfInsteadOfLookingBroken(Store):

    def test_without_documents_the_screen_says_so(self):
        self.assertIn("не добавлены", P.policy_text())

    def test_with_documents_there_is_no_warning(self):
        self.set_all()
        self.assertNotIn("не добавлены", P.policy_text())

    def test_the_consent_wording_is_always_there(self):
        for setup in (lambda: None, self.set_all):
            setup()
            self.assertIn("соглашаетесь", P.policy_text())

    def test_the_owner_is_told_where_to_fix_it(self):
        self.assertIn("Админ-панель", P.policy_text(for_admin=True))

    def test_a_client_is_not_sent_to_a_panel_he_cannot_open(self):
        # Совет открыть админку человеку без доступа — обещание невозможного.
        self.assertNotIn("Админ-панель", P.policy_text(for_admin=False))

    def test_the_wording_is_editable_like_the_other_texts(self):
        self.assertIn("policy", storage.CUSTOM_TEXTS)
        storage.set_custom_text("policy", "Свой текст про документы")
        try:
            self.assertIn("Свой текст", P.policy_text())
        finally:
            storage.clear_custom_text("policy")


class TheCommandIsWiredUp(unittest.TestCase):
    """Команда без обработчика — это тишина, а не ошибка: так молча не
    работали `/chat_debug` и `/withdraw_debug`."""

    def test_the_policy_command_has_a_handler(self):
        names = [h.callback.__name__ for h in P.router.message.handlers]
        self.assertIn("cmd_policy", names)

    def test_the_button_that_opens_it_has_a_handler_too(self):
        names = [h.callback.__name__ for h in P.router.callback_query.handlers]
        self.assertIn("show_policy", names)

    def test_the_router_is_connected_in_main(self):
        # Роутер, который никто не подключил, — это команда, отвечающая
        # молчанием на каждое нажатие.
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        self.assertIn("policy.router", src)


class OwnerSetsTheLinks(Store):
    """Ссылки принадлежат владельцу бота, а не продавцу-подписчику."""

    class Msg:
        def __init__(self, text, uid=1):
            self.text = text
            self.from_user = type("U", (), {"id": uid})()
            self.sent: list[str] = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append(text)
            return self

    class State:
        def __init__(self, key="terms"):
            self.data = {"doc_key": key}
            self.cleared = 0

        async def get_data(self):
            return dict(self.data)

        async def clear(self):
            self.cleared += 1

        async def set_state(self, *a):
            pass

        async def update_data(self, **kw):
            self.data.update(kw)

    def setUp(self):
        super().setUp()
        self._is_admin = A.is_admin
        A.is_admin = lambda uid: uid == 1

    def tearDown(self):
        A.is_admin = self._is_admin
        super().tearDown()

    def test_a_proper_link_is_saved(self):
        m = self.Msg("https://example.org/terms")
        run(A.docs_set_input(m, self.State()))
        self.assertEqual(storage.get_policy_links().get("terms"),
                         "https://example.org/terms")

    def test_a_link_without_a_scheme_is_refused_and_not_saved(self):
        # Telegram не примет такую кнопку, и экран не отправится целиком —
        # то есть сохранить это значит сломать /policy у всех клиентов.
        m = self.Msg("example.org/terms")
        run(A.docs_set_input(m, self.State()))
        self.assertEqual(storage.get_policy_links(), {})
        self.assertIn("http", m.sent[0])

    def test_the_refusal_says_what_is_wrong_rather_than_just_failing(self):
        m = self.Msg("просто текст")
        run(A.docs_set_input(m, self.State()))
        self.assertIn("должна начинаться", m.sent[0])

    def test_a_refusal_keeps_the_form_open_for_another_try(self):
        st = self.State()
        run(A.docs_set_input(self.Msg("не ссылка"), st))
        self.assertEqual(st.cleared, 0, "форма закрылась, вводить уже некуда")

    def test_an_http_link_is_accepted_too(self):
        m = self.Msg("http://example.org/x")
        run(A.docs_set_input(m, self.State("offer")))
        self.assertEqual(storage.get_policy_links().get("offer"),
                         "http://example.org/x")

    def test_someone_who_is_not_an_admin_cannot_set_a_link(self):
        m = self.Msg("https://evil.example/terms", uid=999)
        run(A.docs_set_input(m, self.State()))
        self.assertEqual(storage.get_policy_links(), {})

    def test_an_unknown_document_key_saves_nothing(self):
        m = self.Msg("https://example.org/x")
        run(A.docs_set_input(m, self.State("выдуманный")))
        self.assertEqual(storage.get_policy_links(), {})

    def test_the_admin_panel_offers_a_way_in(self):
        src = (Path(__file__).resolve().parents[1]
               / "handlers" / "admin.py").read_text()
        self.assertIn('callback_data="admin:docs"', src)


if __name__ == "__main__":
    unittest.main()
