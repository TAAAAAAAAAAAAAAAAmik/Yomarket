"""Walking the screens the way a seller does, with the handlers called for real.

Rules and keyboards are covered elsewhere; what is covered here is the wiring
between them — a handler that saves to the wrong place, or forgets to save at
all, passes every other test in this directory.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage                                     # noqa: E402
from automation import market as M                 # noqa: E402
from automation import position as P               # noqa: E402
import handlers.selenium_settings as S             # noqa: E402


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []
        self.from_user = type("U", (), {"id": 7})()

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = type("U", (), {"id": 7})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.state = None
        self.data = {}

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


PAGE = {"offers": M._normalize([
    {"title": "Аккаунт Steam", "price": 100, "shop": {"name": "Конкурент"}},
    {"title": "Аккаунт Steam", "price": 130, "shop": {"name": "Спайк"},
     "id": 220075},
    {"title": "Аккаунт Steam", "price": 140, "shop": {"name": "Другой"}},
]), "note": "тест"}


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.store = {"promo_position": {}}
        self._undo = []
        self.patch(storage, "get_settings", self._load)
        self.patch(storage, "save_settings", self._save)
        self.patch(storage, "get_shop_name", lambda uid: "Спайк")
        self.patch(S, "get_settings", self._load)
        self.patch(S, "save_settings", self._save)
        self.patch(M, "fetch_listing", lambda url, shop="", category_id=None: (True, PAGE))

    # Copies on the way in and out, like the real store: settings live in the
    # database, so a handler that mutates its dict and forgets to save changes
    # nothing. A shared dict would hide exactly that bug.
    def _load(self, uid):
        return copy.deepcopy(self.store)

    def _save(self, uid, s):
        self.store = copy.deepcopy(s)

    def patch(self, module, name, value):
        self._undo.append((module, name, getattr(module, name, None)))
        setattr(module, name, value)

    def tearDown(self):
        for module, name, old in reversed(self._undo):
            setattr(module, name, old)

    def run_(self, coro):
        return asyncio.run(coro)

    def watches(self):
        return P.watches(self.store.setdefault("promo_position", {}))


class AddingAWatch(FlowCase):
    def test_a_pasted_link_becomes_a_watch_and_finds_us(self):
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["url"], "https://yoomarket.net/p/1")
        # The probe recognises us and remembers what to match on later
        self.assertEqual(ws[0]["title"], "Аккаунт Steam")
        self.assertEqual(ws[0]["market_id"], "220075")
        # The panel id is a different number and must not be invented from it:
        # binding the listing in the panel is a separate, deliberate step.
        self.assertEqual(ws[0]["item_id"], "")
        self.assertEqual(ws[0]["last_pos"], 2)
        self.assertTrue(any("2-м месте" in t for t in msg.sent), msg.sent)

    def test_a_link_we_are_not_on_says_so_instead_of_pretending(self):
        """And says enough to act on: who IS in the list, and what we matched on.

        "не нашёл" alone left the seller with nothing to check.
        """
        self.patch(storage, "get_shop_name", lambda uid: "Другой магазин")
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(len(self.watches()), 1)
        said = "\n".join(msg.sent)
        self.assertIn("вашего товара среди них нет", said)
        self.assertIn("Другой магазин", said)      # what we looked for
        self.assertIn("Спайк", said)               # who was actually there

    def test_we_are_found_by_our_own_ad_id_when_the_listing_names_no_seller(self):
        """The API listing does not have to carry a shop name.

        Our ad ids do identify us, and the marketplace hands them over for the
        asking — a surer match than a name, which can be changed or repeated.
        """
        anonymous = {"offers": M._normalize([
            {"title": "Лот A", "price": 100, "id": 111},
            {"title": "Лот B", "price": 130, "id": 220075},
            {"title": "Лот C", "price": 140, "id": 333},
        ]), "note": "api"}
        self.patch(M, "fetch_listing", lambda url, shop="", category_id=None: (True, anonymous))
        self.patch(storage, "get_token", lambda uid: "tok")

        class FakeApi:
            def __init__(self, token):
                pass

            async def start(self):
                pass

            async def close(self):
                pass

            async def get_ads(self, *a, **kw):
                return {"data": [{"id": 220075}, {"id": 999}]}

        import api.yoomarket as Y
        self.patch(Y, "YooMarketAPI", FakeApi)

        msg = FakeMessage("https://yoomarket.net/p/9")
        self.run_(S.pos_url_save(msg, FakeState()))
        ws = self.watches()
        self.assertEqual(ws[0]["market_id"], "220075")
        self.assertEqual(ws[0]["last_pos"], 2)
        self.assertTrue(any("2-м месте" in t for t in msg.sent), msg.sent)

    def test_an_unreadable_page_still_saves_the_watch_with_the_reason(self):
        self.patch(M, "fetch_listing", lambda url, shop="", category_id=None: (False, "HTTP 503"))
        msg = FakeMessage("https://yoomarket.net/p/1")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(len(self.watches()), 1)
        self.assertTrue(any("503" in t for t in msg.sent), msg.sent)

    def test_junk_is_rejected_without_saving(self):
        msg = FakeMessage("это не ссылка")
        self.run_(S.pos_url_save(msg, FakeState()))
        self.assertEqual(self.watches(), [])
        self.assertIn("https://", msg.sent[0])

    def test_the_same_page_is_not_watched_twice(self):
        for _ in range(2):
            self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                     FakeState()))
        self.assertEqual(len(self.watches()), 1)

    def test_the_list_is_capped(self):
        for i in range(P.MAX_WATCHES + 3):
            self.run_(S.pos_url_save(FakeMessage(f"https://yoomarket.net/p/{i}"),
                                     FakeState()))
        self.assertEqual(len(self.watches()), P.MAX_WATCHES)


class AddingFromOwnListings(FlowCase):
    """Picking a listing instead of copying an address for each of fifteen.

    The bot has to do three things the seller would otherwise do by hand: find
    the page the item is on, check it is really there, and bind the panel
    record a promotion is charged against.
    """

    ADS = {"data": [{"id": 220075, "title": "Аккаунт Steam"},
                    {"id": 330099, "title": "Другой товар"}]}
    # The card names its section and its game, inside-out.
    CARD = {"id": 220075, "slug": "akkaunt-steam",
            "category": {"slug": "virty", "parent": {"slug": "black-russia"}}}

    def setUp(self):
        super().setUp()
        S._MY_ADS_CACHE.clear()
        self.patch(storage, "get_token", lambda uid: "tok")
        self.patch(storage, "get_panel_creds", lambda uid: {"cookies": "c=1"})
        self.fetched: list[str] = []

        ads = self.ADS

        class FakeApi:
            def __init__(self, token):
                pass

            async def start(self):
                pass

            async def close(self):
                pass

            async def get_ads(self, *a, **kw):
                return ads

        import api.yoomarket as Y
        self.patch(Y, "YooMarketAPI", FakeApi)
        self.patch(M, "find_own_listing",
                   lambda mid, title="", seller="": self.CARD)
        self.patch(M, "search_own_listing",
                   lambda mid, title="", seller="": (self.CARD, {}))
        # The catalogue knows «virty» as a section of black-russia,
        # and knows nothing about the reverse.
        self.patch(M, "category_meta",
                   lambda slugs: {"id": 512, "slug": "virty",
                                  "ads_count": 161}
                   if slugs[-1:] == ["virty"] else {})

        # Only the right way round has our listing in it.
        right = "https://yoomarket.net/categories/black-russia/virty"

        def fetch(url, shop="", category_id=None):
            self.fetched.append(url)
            if url == right:
                return True, PAGE
            return True, {"offers": M._normalize(
                [{"title": "Чужое", "price": 10, "shop": {"name": "Кто-то"}},
                 {"title": "Чужое", "price": 20, "shop": {"name": "Ещё"}}]),
                "note": ""}

        self.patch(M, "fetch_listing", fetch)

        import automation.panel as P_
        self.patch(P_, "panel_list_items_sync",
                   lambda cookies: (True, [{"id": 5150,
                                            "title": "Аккаунт Steam"}]))

    def pick(self, i=0):
        cb = FakeCallback(f"pos:addpick:{i}")
        self.run_(S.pos_add_pick(cb))
        return cb

    def test_the_page_is_found_without_the_seller_copying_anything(self):
        cb = self.pick()
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["url"],
                         "https://yoomarket.net/categories/black-russia/virty")
        self.assertEqual(ws[0]["market_id"], "220075")
        self.assertEqual(ws[0]["last_pos"], 2)
        self.assertTrue(any("2-м месте" in t for t in cb.message.sent),
                        cb.message.sent)

    def test_only_the_chosen_address_is_read_in_full(self):
        """Candidates are settled against the catalogue, not by reading each.

        Reading every candidate's listing would be up to forty-five pages
        apiece — minutes of requests to answer a question one lookup settles.
        """
        self.pick()
        self.assertEqual(self.fetched,
                         ["https://yoomarket.net/categories/black-russia/virty"])

    def test_the_slug_order_is_settled_by_the_catalogue(self):
        """Which slug is the game is not stated anywhere on the card.

        Guessing and keeping the guess would count the position in the wrong
        catalogue — the number the seller pays against.
        """
        # Here the catalogue says the *other* order is the real section, and
        # our listing is on whichever page is read.
        self.patch(M, "category_meta",
                   lambda slugs: {"id": 77, "slug": "black-russia",
                                  "ads_count": 638}
                   if slugs[-1:] == ["black-russia"] else {})
        self.patch(M, "fetch_listing", lambda url, shop="", category_id=None: (True, PAGE))
        self.pick()
        self.assertEqual(self.watches()[0]["url"],
                         "https://yoomarket.net/categories/virty/black-russia")

    def test_a_section_that_exists_but_lacks_our_listing_is_not_accepted(self):
        """Existing is not the same as being where we stand."""
        self.patch(M, "fetch_listing",
                   lambda url, shop="", category_id=None: (True, {
                       "offers": M._normalize(
                           [{"title": "Чужое", "price": 10,
                             "shop": {"name": "Кто-то"}}]), "note": ""}))
        cb = self.pick()
        self.assertEqual(self.watches(), [])
        self.assertTrue(any("не показала" in t for t in cb.message.sent),
                        cb.message.sent)

    def test_the_panel_record_is_bound_in_the_same_step(self):
        cb = self.pick()
        self.assertEqual(self.watches()[0]["item_id"], "5150")
        self.assertTrue(any("привязан" in t for t in cb.message.sent))

    def test_a_listing_missing_from_the_panel_is_said_so_not_faked(self):
        import automation.panel as P_
        self.patch(P_, "panel_list_items_sync", lambda c: (True, []))
        cb = self.pick()
        self.assertEqual(self.watches()[0]["item_id"], "")
        self.assertTrue(any("привяжите вручную" in t for t in cb.message.sent))

    def test_when_no_page_has_it_the_manual_route_is_offered(self):
        self.patch(M, "fetch_listing",
                   lambda url, shop="", category_id=None: (True, {"offers": [], "note": ""}))
        cb = self.pick()
        self.assertEqual(self.watches(), [], "nothing unverified is saved")
        said = "\n".join(cb.message.sent)
        self.assertIn("не показала", said)
        kb = cb.message.markups[-1]
        self.assertIn("pos:addurl",
                      [b.callback_data for row in kb.inline_keyboard
                       for b in row])

    def test_a_listing_the_search_cannot_find_says_so_specifically(self):
        """A different failure from «the page did not show it», and it needs a
        different thing from the seller — so it must not read the same."""
        self.patch(M, "find_own_listing", lambda mid, title="", seller="": {})
        self.patch(M, "search_own_listing",
                   lambda mid, title="", seller="": ({}, {}))
        cb = self.pick()
        self.assertEqual(self.watches(), [])
        said = "\n".join(cb.message.sent)
        self.assertIn("Не нашёл этот товар в поиске витрины", said)
        self.assertIn("Пробовал:", said)   # and by what
        self.assertNotIn("не показала", said)

    def test_a_stale_list_is_refused_rather_than_picking_the_wrong_item(self):
        cb = FakeCallback("pos:addpick:99")
        self.run_(S.pos_add_pick(cb))
        self.assertEqual(self.watches(), [])
        self.assertTrue(cb.answers[0][1], "should alert, not silently pass")


class NamingTheSectionByHand(FlowCase):
    """Two taps through the marketplace's own catalogue.

    Automatic detection reads whatever the listing row happens to carry, and on
    a real shop that turned out not to include the section. The catalogue
    always has it — and tapping it beats copying an address, which is what the
    whole screen exists to avoid.
    """

    TREE = [{"id": 77, "slug": "black-russia", "title": "Black Russia",
             "children": [
                 {"id": 512, "slug": "akkaunty-s-virtami",
                  "title": "Аккаунты с виртами", "ads_count": 161},
                 {"id": 513, "slug": "virty", "title": "Вирты",
                  "ads_count": 638}]},
            {"id": 90, "slug": "telegram", "title": "Telegram"}]

    def setUp(self):
        super().setUp()
        S._PENDING_AD[7] = {"id": "220075", "title": "Аккаунт Steam"}
        self.patch(storage, "get_panel_creds", lambda uid: {"cookies": "c=1"})
        import automation.panel as P_
        self.patch(P_, "panel_list_items_sync",
                   lambda c: (True, [{"id": 5150, "title": "Аккаунт Steam"}]))

        def children(parent=""):
            if not parent:
                return [{"id": n["id"], "slug": n["slug"], "title": n["title"],
                         "has_children": bool(n.get("children")),
                         "ads_count": None} for n in self.TREE]
            node = next((n for n in self.TREE if n["slug"] == parent), None)
            return [{"id": c["id"], "slug": c["slug"], "title": c["title"],
                     "has_children": False, "ads_count": c["ads_count"]}
                    for c in (node or {}).get("children", [])]

        self.patch(M, "category_children", children)

        def slugs_for(cat_id):
            for game in self.TREE:
                for c in game.get("children", []):
                    if str(c["id"]) == str(cat_id):
                        return [game["slug"], c["slug"]]
                if str(game["id"]) == str(cat_id):
                    return [game["slug"]]
            return []

        self.patch(M, "category_slugs_for", slugs_for)
        # fetch_listing now takes the section id as a third argument
        self.patch(M, "fetch_listing",
                   lambda url, shop="", category_id=None: (True, PAGE))

    def tearDown(self):
        S._PENDING_AD.pop(7, None)
        super().tearDown()

    def test_the_top_level_offers_games_that_go_deeper(self):
        cb = FakeCallback("pos:cat:")
        self.run_(S.pos_pick_category(cb))
        data = [b.callback_data for row in cb.message.markups[-1].inline_keyboard
                for b in row]
        self.assertIn("pos:cat:black-russia", data, "a game must open its sections")
        self.assertIn("pos:catpick:90", data,
                      "one without sections is already the answer, by its id")

    def test_a_section_is_offered_with_how_much_it_holds(self):
        cb = FakeCallback("pos:cat:black-russia")
        self.run_(S.pos_pick_category(cb))
        kb = cb.message.markups[-1]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Аккаунты с виртами" in l and "161" in l
                            for l in labels), labels)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        # By id, not by slugs: this API answers /categories/<game>/<section>
        # with the game, so an address alone can widen to the whole game.
        self.assertIn("pos:catpick:512", data)

    def test_choosing_a_section_sets_the_watch_up_completely(self):
        cb = FakeCallback("pos:catpick:512")
        self.run_(S.pos_category_chosen(cb))
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(
            ws[0]["url"],
            "https://yoomarket.net/categories/black-russia/akkaunty-s-virtami")
        self.assertEqual(ws[0]["market_id"], "220075")
        self.assertEqual(ws[0]["item_id"], "5150", "panel record not bound")
        self.assertEqual(ws[0]["category_id"], "512",
                         "the section id must be kept — the address alone is "
                         "read loosely by this API")
        self.assertEqual(ws[0]["last_pos"], 2)

    def test_a_section_without_our_listing_is_not_saved(self):
        self.patch(M, "fetch_listing",
                   lambda url, shop="", category_id=None: (True, {
                       "offers": M._normalize(
                           [{"title": "Чужое", "price": 5,
                             "shop": {"name": "Кто-то"}}]), "note": ""}))
        cb = FakeCallback("pos:catpick:513")
        self.run_(S.pos_category_chosen(cb))
        self.assertEqual(self.watches(), [])
        said = "\n".join(cb.message.sent)
        self.assertIn("В этом разделе товара нет", said)
        self.assertIn("Просмотрено предложений: 1", said)

    def test_the_choice_is_refused_when_no_listing_is_pending(self):
        S._PENDING_AD.pop(7, None)
        cb = FakeCallback("pos:catpick:513")
        self.run_(S.pos_category_chosen(cb))
        self.assertEqual(self.watches(), [])
        self.assertTrue(cb.answers[0][1])

    def test_an_unreadable_catalogue_falls_back_to_the_address(self):
        self.patch(M, "category_children", lambda parent="": [])
        cb = FakeCallback("pos:cat:")
        self.run_(S.pos_pick_category(cb))
        data = [b.callback_data for row in cb.message.markups[-1].inline_keyboard
                for b in row]
        self.assertIn("pos:addurl", data)


class EditingAWatch(FlowCase):
    def setUp(self):
        super().setUp()
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))

    def test_threshold_round_trip(self):
        state = FakeState()
        cb = FakeCallback("pos:wt:0")
        self.run_(S.pos_threshold_start(cb, state))
        self.assertEqual(state.data.get("pos_idx"), 0)
        self.run_(S.pos_threshold_save(FakeMessage("5"), state))
        self.assertEqual(self.watches()[0]["max_position"], 5)

    def test_threshold_rejects_nonsense_and_keeps_the_state(self):
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        msg = FakeMessage("сто")
        self.run_(S.pos_threshold_save(msg, state))
        self.assertEqual(self.watches()[0]["max_position"],
                         P.DEFAULT_MAX_POSITION)
        self.assertIn("❌", msg.sent[0])
        self.assertIsNotNone(state.state, "the wizard must not drop out")

    def test_price_guard_round_trip(self):
        state = FakeState()
        self.run_(S.pos_guard_start(FakeCallback("pos:wg:0"), state))
        self.run_(S.pos_guard_save(FakeMessage("25,5"), state))
        self.assertEqual(self.watches()[0]["undercut_guard"], 25.5)

    def test_price_alarm_round_trip(self):
        state = FakeState()
        self.run_(S.pos_price_start(FakeCallback("pos:wp:0"), state))
        self.run_(S.pos_price_save(FakeMessage("90"), state))
        self.assertEqual(self.watches()[0]["min_price"], 90.0)

    def test_a_new_threshold_rearms_the_alert(self):
        self.watches()[0]["last_alert_pos"] = 9
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        self.run_(S.pos_threshold_save(FakeMessage("2"), state))
        self.assertEqual(self.watches()[0]["last_alert_pos"], 0)

    def test_editing_a_deleted_watch_does_not_crash(self):
        state = FakeState()
        self.run_(S.pos_threshold_start(FakeCallback("pos:wt:0"), state))
        self.store["promo_position"]["watches"] = []
        msg = FakeMessage("4")
        self.run_(S.pos_threshold_save(msg, state))
        self.assertIn("удалено", msg.sent[0])

    def test_delete_removes_it_and_turns_the_watcher_off(self):
        self.store["promo_position"]["enabled"] = True
        cb = FakeCallback("pos:wd:0")
        self.run_(S.pos_watch_delete(cb))
        self.assertEqual(self.watches(), [])
        self.assertFalse(self.store["promo_position"]["enabled"])


class Switches(FlowCase):
    def test_cannot_arm_an_empty_watcher(self):
        cb = FakeCallback("pos:toggle")
        self.run_(S.pos_toggle(cb))
        self.assertFalse(self.store["promo_position"].get("enabled"))
        self.assertTrue(cb.answers[0][1], "should be an alert, not a toast")

    def test_arming_schedules_an_immediate_check(self):
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))
        self.watches()[0]["last_check"] = 10 ** 10
        self.run_(S.pos_toggle(FakeCallback("pos:toggle")))
        self.assertTrue(self.store["promo_position"]["enabled"])
        self.assertEqual(self.watches()[0]["last_check"], 0)

    def test_auto_mode_warns_when_no_tariff_is_picked(self):
        self.run_(S.pos_url_save(FakeMessage("https://yoomarket.net/p/1"),
                                 FakeState()))
        cb = FakeCallback("pos:auto")
        self.run_(S.pos_auto(cb))
        self.assertTrue(self.store["promo_position"]["auto_promote"])
        self.assertIn("тариф", cb.answers[0][0])

    def test_shop_wide_numbers_round_trip(self):
        for start, save, text, key, want in (
                (S.pos_interval_start, S.pos_interval_save, "0,5",
                 "interval_hours", 0.5),
                (S.pos_cooldown_start, S.pos_cooldown_save, "12",
                 "cooldown_hours", 12.0),
                (S.pos_limit_start, S.pos_limit_save, "1", "daily_limit", 1),
        ):
            state = FakeState()
            self.run_(start(FakeCallback("x"), state))
            self.run_(save(FakeMessage(text), state))
            self.assertEqual(self.store["promo_position"][key], want)


class LegacyUpgrade(FlowCase):
    def test_the_old_single_page_setup_shows_up_as_a_watch(self):
        self.store = {"promo_position": {
            "enabled": True, "url": "https://yoomarket.net/old",
            "max_position": 4, "min_price": 80, "last_pos": 6}}
        cb = FakeCallback("pos:menu")
        self.run_(S.pos_menu(cb, FakeState()))
        ws = self.watches()
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["max_position"], 4)
        self.assertEqual(ws[0]["min_price"], 80)
        screen = cb.message.sent[-1]
        self.assertIn("Товаров под наблюдением", screen)
        self.assertIn("6 место", screen)


if __name__ == "__main__":
    unittest.main()
