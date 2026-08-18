"""Robux: узнавание заказа, подбор номинала, безопасность повтора.

Раздел был скопирован со звёзд, и от этого нёс чужие понятия: ник
покупателя, произвольное количество, ручная выдача «кому». У Robux
выдаётся код, ник не нужен, а количество бывает только то, что лежит в
каталоге поставщика.

Самое дорогое здесь — **молчаливая подмена номинала**. Выдать 800 Robux за
заказ на 500 значит подарить триста; выдать 200 — недодать оплаченное. Оба
исхода бот обязан отказаться совершать и сказать почему.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from automation import robux as R          # noqa: E402


def catalog(*rows):
    """Каталог в том виде, в каком его отдаёт живой `GET /services`."""
    services: dict = {}
    for region, name, price, stock, ident in rows:
        key = f"Roblox Wallet Code | {region}"
        services.setdefault(key, {
            "id": f"svc-{region}", "name": key, "type": "voucher",
            "fields": None, "items": []})
        services[key]["items"].append(
            {"id": ident, "name": name, "price": price,
             "currency": "USD", "inStock": stock})
    return {"items": list(services.values())}


LIVE = catalog(
    ("GL", "200 Robux", 3.57, 1948, "gl-200"),
    ("GL", "800 Robux", 9.078, 2329, "gl-800"),
    ("GL", "1000 Robux", 11.22, 994, "gl-1000"),
    ("GL", "10000 Robux", 98.8339, 199, "gl-10000"),
    ("RU", "Roblox code | 200 Robux RU", 2.8968, 443, "ru-200"),
    ("RU", "Roblox code | 1000 Robux RU", 13.9563, 20, "ru-1000"),
)


class AnOrderIsRecognisedByItsTitle(unittest.TestCase):
    """Продавцы называют товар как хотят, и ловить одно написание значит
    пропускать половину заказов."""

    def test_the_usual_spellings_all_count(self):
        for title in ("1000 Robux", "1000 робукс", "Roblox 1000",
                      "🎮 РОБЛОКС 1000 робуксов"):
            self.assertTrue(R.is_robux_order(title), title)

    def test_something_else_entirely_does_not(self):
        for title in ("100 звёзд Telegram", "Steam 500 ₽", ""):
            self.assertFalse(R.is_robux_order(title), title)

    def test_a_sellers_keyword_is_a_demand_not_a_hint(self):
        """Иначе «Roblox аккаунт» уедет в выдачу Robux — а это другой товар,
        и покупатель получит код вместо аккаунта."""
        self.assertFalse(R.is_robux_order("Roblox аккаунт", "робукс"))
        self.assertTrue(R.is_robux_order("500 робукс", "робукс"))


class TheQuantityComesFromTheOrderNotFromASetting(unittest.TestCase):
    """Число, заданное заранее, означало бы «выдать столько, сколько у нас
    записано» — то есть выдать не то, что оплачено."""

    def test_the_number_next_to_the_word_wins(self):
        """«1000 Robux за 350 ₽» — однажды продали бы номинал на 350."""
        self.assertEqual(R.robux_quantity("Roblox 1000 Robux за 350 руб"), 1000)

    def test_a_year_is_not_a_quantity(self):
        self.assertEqual(R.robux_quantity("Robux 2024 акция 800 робукс"), 800)

    def test_no_number_means_no_guess(self):
        """Подставить запасное значение значит выдать наугад."""
        self.assertEqual(R.robux_quantity("Robux по акции"), 0)


class ADenominationIsMatchedExactlyOrNotAtAll(unittest.TestCase):
    """Самое дорогое место раздела.

    Подмена ближайшим номиналом — это либо подарок за наш счёт, либо
    недоданное оплаченное. Бот обязан отказаться и назвать причину, а не
    выдать другую сумму молча.
    """

    def test_an_exact_denomination_is_found_with_its_id(self):
        row, why = R.match_denomination(LIVE, 1000, "GL")
        self.assertEqual(why, "")
        self.assertEqual(row["denomination_id"], "gl-1000")
        self.assertEqual(row["robux"], 1000)

    def test_a_missing_denomination_is_refused_not_rounded(self):
        row, why = R.match_denomination(LIVE, 500, "GL")
        self.assertIsNone(row)
        self.assertIn("500", why)
        self.assertIn("200", why)          # перечислили, что есть
        self.assertIn("складывать", why.lower())

    def test_a_zero_stock_denomination_is_refused_plainly(self):
        thin = catalog(("GL", "800 Robux", 9.078, 0, "gl-800"))
        row, why = R.match_denomination(thin, 800, "GL")
        self.assertIsNone(row)
        self.assertIn("остаток", why)

    def test_the_region_is_honoured_because_codes_are_not_interchangeable(self):
        """Российский код глобальному аккаунту не подойдёт, и наоборот."""
        row, _ = R.match_denomination(LIVE, 1000, "RU")
        self.assertEqual(row["denomination_id"], "ru-1000")
        row, _ = R.match_denomination(LIVE, 1000, "GL")
        self.assertEqual(row["denomination_id"], "gl-1000")

    def test_an_unknown_quantity_is_refused(self):
        row, why = R.match_denomination(LIVE, 0, "GL")
        self.assertIsNone(row)
        self.assertIn("не видно", why)

    def test_an_empty_catalogue_is_called_empty(self):
        row, why = R.match_denomination({"items": []}, 1000, "GL")
        self.assertIsNone(row)
        self.assertIn("нет номиналов", why)


class TheDenominationIdIsTheNestedOneNotTheService(unittest.TestCase):
    """Перепутать их значит получить отказ по схеме: сервер ждёт
    `denominationId`, то есть `id` номинала внутри услуги."""

    def test_it_takes_the_nested_item_id(self):
        rows = R.denominations(LIVE, "GL")
        self.assertIn("gl-1000", [r["denomination_id"] for r in rows])
        self.assertNotIn("svc-GL", [r["denomination_id"] for r in rows])

    def test_the_service_id_is_kept_separately(self):
        rows = R.denominations(LIVE, "GL")
        self.assertTrue(all(r["service_id"] == "svc-GL" for r in rows))


class ARepeatMustNotBuyTwice(unittest.TestCase):
    """Обрыв связи после отправки запроса — обычное дело. Ссылка, собранная
    из времени, при повторе стала бы другой, и поставщик купил бы второй
    раз вместо того, чтобы вернуть прежний результат."""

    def test_the_reference_is_the_same_for_the_same_order(self):
        first = R.order_reference("1225296", 1000)
        second = R.order_reference("1225296", 1000)
        self.assertEqual(first, second)

    def test_different_orders_get_different_references(self):
        self.assertNotEqual(R.order_reference("1", 1000),
                            R.order_reference("2", 1000))


class AnEmptyResultIsARefusalNotADelivery(unittest.TestCase):
    """Доложить о выдаче, не получив кода, — худшее, что может случиться:
    покупатель заплатил, продавец уверен, что всё прошло."""

    def test_a_pin_is_read_out_of_the_answer(self):
        data = {"result": {"vouchers": [{"pin": "AAAA-BBBB"}]}}
        self.assertEqual(R.codes_from_result(data), ["AAAA-BBBB"])

    def test_no_vouchers_means_no_codes(self):
        for data in ({"result": {"vouchers": []}}, {"result": {}}, {}, None):
            self.assertEqual(R.codes_from_result(data), [])


class TheScreenDoesNotPromiseWhatItCannotDo(unittest.TestCase):
    """Шесть кнопок раздела отвечали «функция появится в следующем
    обновлении», а «Ручная выдача» спрашивала @username — понятие звёзд.
    Кнопка, которая заведомо не сработает, — обещание невозможного."""

    def keyboard(self):
        from handlers import plugins as P
        kb = P._roblox_keyboard({"plugins": {"auto_roblox": {}}})
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_the_stub_buttons_are_gone(self):
        cbs = self.keyboard()
        for dead in ("plugins:roblox:accumulated", "plugins:roblox:profit",
                     "plugins:roblox:balance", "plugins:roblox:notifs",
                     "plugins:roblox:replies"):
            self.assertNotIn(dead, cbs)

    def test_manual_delivery_asks_for_an_order_not_for_a_buyer(self):
        """Кнопка вернулась, но означает другое. Прежняя спрашивала
        @username — понятие звёзд; код уходит в чат заказа, и аккаунт
        покупателя к делу не относится."""
        import inspect
        from handlers import plugins as P
        self.assertIn("plugins:roblox:manual", self.keyboard())
        # Смотрим на то, что увидит продавец, а не на докстринг: в нём
        # @username упомянут нарочно — там объяснено, почему его больше нет.
        shown = inspect.getsource(P.roblox_manual_prompt).split('"""')[-1]
        self.assertIn("номер заказа", shown)
        self.assertNotIn("@username", shown)

    def test_making_an_ad_starts_from_a_denomination(self):
        """Витрина и каталог обязаны сойтись: объявление на номинал, которого
        у поставщика нет, автовыдаче недоступно."""
        self.assertIn("plugins:roblox:make_ads", self.keyboard())

    def test_the_plugin_does_not_create_ads_behind_the_wizards_back(self):
        """Своего создания товаров тут быть не должно: мастер уже умеет
        категории панели, обязательные поля, фото и публикацию."""
        import inspect
        from handlers import plugins as P
        self.assertNotIn("panel_create_product_sync", inspect.getsource(P))

    def test_the_other_supplier_is_not_offered_here(self):
        """ns.gifts убран: поставщик выбран, а сравнение цены — не то, ради
        чего открывают раздел выдачи."""
        self.assertNotIn("ns:creds", self.keyboard())

    def test_the_supplier_and_the_catalogue_are_reachable(self):
        cbs = self.keyboard()
        self.assertIn("apr:creds", cbs)
        self.assertIn("apr:stock", cbs)

    def test_the_screen_says_a_code_is_what_the_buyer_gets(self):
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {}}})
        self.assertIn("код", said.lower())
        self.assertIn("ник", said.lower())      # и что он не нужен

    def test_the_toggle_now_means_what_it_says(self):
        """Выдача написана, и тумблер её включает — обещание выполнимо."""
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {"enabled": True}}})
        self.assertIn("автовыдача включена", said.lower())
        off = P._roblox_text({"plugins": {"auto_roblox": {"enabled": False}}})
        self.assertIn("выключена", off.lower())

    def test_but_it_admits_it_has_never_run_on_a_real_order(self):
        """«Работает» и «проверено» — разные утверждения. Выдавать второе за
        первое здесь уже стоило дней разбирательств."""
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {"enabled": True}}})
        self.assertIn("не проверялась", said.lower())


class TheSettingsAskForWhatRobuxActuallyNeed(unittest.TestCase):
    """«Кол-во Robux» настройкой быть не может: количество диктует заказ."""

    def test_there_is_no_fixed_amount_setting(self):
        from handlers import plugins as P
        kb = P._roblox_settings_keyboard({"plugins": {"auto_roblox": {}}})
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertNotIn("plugins:roblox:set_amount", cbs)

    def test_the_region_can_be_switched(self):
        from handlers import plugins as P
        kb = P._roblox_settings_keyboard({"plugins": {"auto_roblox": {"region": "GL"}}})
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("plugins:roblox:region:RU", cbs)

    def test_without_a_keyword_the_screen_warns_about_account_listings(self):
        from handlers import plugins as P
        said = P._roblox_settings_text({"plugins": {"auto_roblox": {}}})
        self.assertIn("Roblox аккаунт", said)


class MakingAnAdHandsOverToTheWizardWhereTheWizardStillAsks(unittest.TestCase):
    """Плагин подставляет то, что знает каталог, — и не больше.

    Первая версия отдавала управление сразу на предпросмотр, зашив
    количество единицей. С предпросмотра количество не правится: там есть
    название, цена, описание и фото, а количества нет. Товар уходил бы с
    витрины после первой же продажи, и продавец узнал бы об этом не от
    бота, а по исчезнувшему объявлению.
    """

    def setUp(self):
        import storage
        self._creds = storage.get_ar_creds

    def tearDown(self):
        import storage
        storage.get_ar_creds = self._creds

    class Msg:
        def __init__(self, text):
            self.text = text
            self.said: list = []
            self.from_user = type("U", (), {"id": 1})()

        async def answer(self, text="", **kw):
            self.said.append(text)

    class FSM:
        def __init__(self, **data):
            self.data = dict(data)
            self.state = None
            self.cleared = 0

        async def set_state(self, s):
            self.state = s

        async def clear(self):
            self.state = None
            self.data = {}
            self.cleared += 1

        async def update_data(self, **kw):
            self.data.update(kw)

        async def get_data(self):
            return dict(self.data)

    def test_the_wizard_is_entered_at_the_quantity_step(self):
        import asyncio
        from handlers import plugins as P
        from handlers.create_ad import CreateAdState

        msg, fsm = self.Msg("990"), self.FSM(robux=1000)
        asyncio.run(P.roblox_den_price(msg, fsm))

        self.assertEqual(fsm.state, CreateAdState.quantity)
        self.assertEqual(fsm.data.get("title"), "1000 Robux")
        self.assertEqual(fsm.data.get("price"), 990)

    def test_the_quantity_is_not_pinned_behind_the_sellers_back(self):
        """Зашитое количество продавец не увидит и не поправит."""
        import asyncio
        from handlers import plugins as P

        msg, fsm = self.Msg("990"), self.FSM(robux=1000)
        asyncio.run(P.roblox_den_price(msg, fsm))

        self.assertNotIn("quantity", fsm.data)

    def test_going_back_from_the_price_step_closes_the_form(self):
        """Брошенная форма ловит следующее сообщение и отвечает «нужно одно
        число» на что угодно, включая команду."""
        import asyncio
        import storage
        from handlers import plugins as P

        storage.get_ar_creds = lambda uid: None
        cb = type("CB", (), {
            "data": "plugins:roblox:make_ads",
            "from_user": type("U", (), {"id": 1})(),
            "message": None,
            "answer": lambda self, *a, **kw: _done(),
        })()
        fsm = self.FSM(robux=1000)
        asyncio.run(P.roblox_make_ads_prompt(cb, fsm))

        self.assertEqual(fsm.state, None)
        self.assertNotIn("robux", fsm.data)


async def _done():
    return None


class TheScreenExplainsWhyOldOrdersSitStill(unittest.TestCase):
    """Продавец включает выдачу, оплаченный заказ лежит, ничего не
    происходит — и это выглядит поломкой. Автовыдача срабатывает на
    перемену: новый заказ или приход оплаты. Молчать об этом нельзя."""

    def test_the_screen_says_old_orders_need_the_manual_button(self):
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {}}})
        self.assertIn("до включения", said)
        self.assertIn("вручную", said)


class TheSellersOwnWordsDoNotBreakTheScreen(unittest.TestCase):
    """Одиночный «<» в заметке или названии магазина роняет отправку всего
    сообщения — а роняет он тот самый экран, через который заметку и
    правят. Продавец остался бы с разделом, в который не войти.
    """

    def test_a_note_with_a_bracket_is_escaped_on_the_main_screen(self):
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {"note": "цена <3$"}}})
        self.assertNotIn("<3$", said)
        self.assertIn("&lt;3$", said)

    def test_a_shop_name_with_a_bracket_is_escaped_too(self):
        from handlers import plugins as P
        said = P._roblox_text({"plugins": {"auto_roblox": {}}}, "Shop <A>")
        self.assertNotIn("Shop <A>", said)

    def test_a_keyword_with_a_bracket_is_escaped_in_the_settings(self):
        from handlers import plugins as P
        said = P._roblox_settings_text(
            {"plugins": {"auto_roblox": {"keyword": "robux<x"}}})
        self.assertNotIn("robux<x", said)

    def test_a_note_with_a_bracket_is_escaped_in_the_settings(self):
        from handlers import plugins as P
        said = P._roblox_settings_text(
            {"plugins": {"auto_roblox": {"note": "цена <3$"}}})
        self.assertIn("&lt;3$", said)


class TheSwitchDoesNotTurnOnWhatCannotWork(unittest.TestCase):
    """«🟢 Автовыдача включена — бот сам купит код» без ключа поставщика —
    обещание невыполнимого: покупать не на что.

    Раньше это выяснялось на живом оплаченном заказе, то есть в самый
    неподходящий момент и уже при ждущем покупателе.
    """

    def setUp(self):
        import storage
        from handlers import plugins as P
        self._undo = []
        self.settings = {"plugins": {"auto_roblox": {"enabled": False}}}
        self.saved: list = []
        for mod, name, val in (
            (P, "get_settings", lambda uid: self.settings),
            (P, "save_settings", lambda uid, s: self.saved.append(s)),
            (P, "get_shop_name", lambda uid: ""),
        ):
            self._undo.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)
        self._undo.append((storage, "get_ar_creds", storage.get_ar_creds))
        self.key = None
        storage.get_ar_creds = lambda uid: self.key

    def tearDown(self):
        for mod, name, old in reversed(self._undo):
            setattr(mod, name, old)

    def toggle(self):
        import asyncio
        from handlers import plugins as P

        class Screen:
            def __init__(self):
                self.texts: list = []

            async def edit_text(self, text="", reply_markup=None, **kw):
                self.texts.append(text)

        cb = type("CB", (), {})()
        cb.data = "plugins:roblox:toggle"
        cb.from_user = type("U", (), {"id": 1})()
        cb.message = Screen()
        cb.alerts = []

        async def answer(text="", **kw):
            cb.alerts.append(text)

        cb.answer = answer
        asyncio.run(P.roblox_toggle(cb))
        return cb

    def test_without_a_supplier_key_it_stays_off(self):
        cb = self.toggle()
        self.assertFalse(self.settings["plugins"]["auto_roblox"]["enabled"])
        self.assertEqual(self.saved, [])

    def test_and_says_what_is_missing(self):
        cb = self.toggle()
        self.assertTrue(any("AppRoute" in a for a in cb.alerts))

    def test_with_a_key_it_turns_on(self):
        self.key = {"api_key": "k"}
        self.toggle()
        self.assertTrue(self.settings["plugins"]["auto_roblox"]["enabled"])

    def test_turning_it_off_never_needs_a_key(self):
        """Запрещать выключение нельзя ни по какой причине."""
        self.settings["plugins"]["auto_roblox"]["enabled"] = True
        self.toggle()
        self.assertFalse(self.settings["plugins"]["auto_roblox"]["enabled"])


class TheManualQueueRefusesWhatItCannotBuy(unittest.TestCase):
    """«Куплю на ближайшем проходе» при незаданном ключе — обещание
    отчёта, который придёт отказом."""

    def setUp(self):
        import storage
        from handlers import plugins as P
        self._undo = []
        self.settings = {"plugins": {"auto_roblox": {"enabled": True}}}
        self.saved: list = []
        for mod, name, val in (
            (P, "get_settings", lambda uid: self.settings),
            (P, "save_settings", lambda uid, s: self.saved.append(s)),
        ):
            self._undo.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)
        self._undo.append((storage, "get_ar_creds", storage.get_ar_creds))
        self.key = None
        storage.get_ar_creds = lambda uid: self.key

    def tearDown(self):
        for mod, name, old in reversed(self._undo):
            setattr(mod, name, old)

    def queue(self, text="777"):
        import asyncio
        from handlers import plugins as P

        said: list = []

        class Msg:
            def __init__(self):
                self.text = text
                self.from_user = type("U", (), {"id": 1})()

            async def answer(self_, t="", **kw):
                said.append(t)

        class FSM:
            async def clear(self):
                return None

        asyncio.run(P.roblox_manual_order_input(Msg(), FSM()))
        return said

    def test_without_a_key_the_order_is_not_queued(self):
        self.queue()
        self.assertEqual(self.settings["plugins"]["auto_roblox"].get("force"),
                         None)

    def test_without_a_key_the_seller_is_told_why(self):
        said = self.queue()
        self.assertTrue(any("AppRoute" in t for t in said))

    def test_with_a_key_the_order_is_queued(self):
        self.key = {"api_key": "k"}
        self.queue()
        self.assertEqual(self.settings["plugins"]["auto_roblox"]["force"],
                         ["777"])


if __name__ == "__main__":
    unittest.main()
