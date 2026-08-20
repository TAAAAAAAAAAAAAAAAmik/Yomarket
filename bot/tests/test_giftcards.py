"""Шаблон гифт-карт: что он обязан не перепутать.

Беда, ради которой написаны эти тесты, одна и та же во всех: **продать не то,
что оплачено, и не заметить этого**. У поставщика в одной куче лежат карты,
подписки, ключи игр и аккаунты, а номинал у них меряется деньгами, игровой
единицей или сроком — и всё это разными словами в одной строке названия.

Каталог в фикстуре — настоящий, снят живьём 20.08 с `GET /services`. Строки
не придуманы: `«Roblox: 4,500 Robux for Xbox»`, `«300 USD account»` и
`«Free Fire: 100+10 Diamonds»` существуют у поставщика ровно в таком виде.
Выдумать такую фикстуру нельзя — можно только списать.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import giftcards as gc                            # noqa: E402


CATALOG = {"items": [
    {'id': 'a4952cd9-aab7-4404-a377-0559c807bb42', 'name': 'Apple Gift Card | TR', 'type': 'voucher', 'countryCode': 'TR', 'categoryName': 'Entertainment', 'subcategoryName': 'Apple Gift Cards', 'fields': None, 'items': [{'id': '0554a36a-99a6-4863-8289-83cdb4a3f009', 'name': 'TRY 10 App Store & iTunes code', 'price': 0.2301, 'currency': 'USD', 'inStock': 130, 'isLongOrder': False}, {'id': '8739cad0-f8a6-43dd-b241-ec6f878818dd', 'name': 'TRY 15 App Store & iTunes code', 'price': 0.36, 'currency': 'USD', 'inStock': 715, 'isLongOrder': False}, {'id': 'a392a487-eecf-4756-9956-b2ca7c8f546e', 'name': 'TRY 20 App Store & iTunes code', 'price': 0.459, 'currency': 'USD', 'inStock': 7325, 'isLongOrder': False}, {'id': '22b893a9-1053-443d-8490-547a571c2c31', 'name': 'TRY 25 App Store & iTunes code', 'price': 0.5455, 'currency': 'USD', 'inStock': 220, 'isLongOrder': False}]},
    {'id': '5b8788df-6073-4009-94b2-773e3928ebee', 'name': 'Apple Gift Card | AE', 'type': 'voucher', 'countryCode': 'AE', 'categoryName': 'Entertainment', 'subcategoryName': 'Apple Gift Cards', 'fields': None, 'items': [{'id': 'd6c6106b-bb25-43f7-9779-6823484ba4a5', 'name': 'AED 50 Apple gift card', 'price': 13.5354, 'currency': 'USD', 'inStock': 99, 'isLongOrder': False}, {'id': 'b02758fc-cd20-475b-988b-4b6e860c99fa', 'name': 'AED 75 Apple gift card', 'price': 20.298, 'currency': 'USD', 'inStock': 10, 'isLongOrder': False}, {'id': '848292b5-48c6-422b-9b81-0484d3f46a05', 'name': 'AED 100 Apple gift card', 'price': 27.0708, 'currency': 'USD', 'inStock': 36, 'isLongOrder': False}]},
    {'id': '98a4e3ec-53a3-4266-9516-a399456c3574', 'name': 'Apple Gift Card | US', 'type': 'voucher', 'countryCode': 'US', 'categoryName': 'Entertainment', 'subcategoryName': 'Apple Gift Cards', 'fields': None, 'items': [{'id': '35f61228-ce9c-4c68-989f-63af12b6e5bb', 'name': '$2 Apple Gift Card', 'price': 1.9094, 'currency': 'USD', 'inStock': 11334, 'isLongOrder': False}, {'id': '2b59c6a5-8b9f-4d53-ac11-5664ea567105', 'name': '$3 Apple Gift Card', 'price': 2.8642, 'currency': 'USD', 'inStock': 5421, 'isLongOrder': False}, {'id': 'a7cf15e9-f3d7-4a90-8c66-b640a26a73e6', 'name': '$4 Apple Gift Card', 'price': 3.8189, 'currency': 'USD', 'inStock': 2565, 'isLongOrder': True}]},
    {'id': '5d479b11-8bf2-4fc1-85a9-ff0ce404e4b4', 'name': 'PlayStation®Store Wallet | US', 'type': 'voucher', 'countryCode': 'US', 'categoryName': 'Gaming', 'subcategoryName': 'PlayStation Gift Cards', 'fields': None, 'items': [{'id': 'e5934816-9f6c-4d2f-ab13-02d07008a9fa', 'name': '$1 PlayStation®Store Wallet gift card', 'price': 0.9293, 'currency': 'USD', 'inStock': 10, 'isLongOrder': False}, {'id': '45be1226-0462-452f-b2ea-3e2280697f3f', 'name': '$2 PlayStation®Store Wallet gift card', 'price': 1.818, 'currency': 'USD', 'inStock': 143, 'isLongOrder': False}, {'id': '7bf575e3-ca40-4159-a1d8-6d64b4b854b5', 'name': '$3 PlayStation®Store Wallet gift card', 'price': 3.0931, 'currency': 'USD', 'inStock': 1299, 'isLongOrder': False}, {'id': 'd9240dd7-3c60-4696-b378-8cb6a408ff1b', 'name': '$4 PlayStation®Store Wallet gift card', 'price': 4.0733, 'currency': 'USD', 'inStock': 1226, 'isLongOrder': False}]},
    {'id': '44f8de6a-0822-4a6d-9578-6c0bd8a70b2a', 'name': 'Xbox Gift Card | US', 'type': 'voucher', 'countryCode': 'US', 'categoryName': 'Gaming', 'subcategoryName': 'Xbox Gift Cards', 'fields': None, 'items': [{'id': '0ae1b98e-932b-437c-95e9-c258e44eb124', 'name': '$1 Xbox gift card', 'price': 0.9384, 'currency': 'USD', 'inStock': 4, 'isLongOrder': False}, {'id': '09022a88-eb23-44d7-b35e-a7e9bc7cd4ad', 'name': '$5 Xbox gift card', 'price': 4.692, 'currency': 'USD', 'inStock': 787, 'isLongOrder': False}, {'id': '33a8cc77-1211-49eb-b472-8afaabbe8dc4', 'name': '$10 Xbox gift card', 'price': 8.823, 'currency': 'USD', 'inStock': 249, 'isLongOrder': False}]},
    {'id': '65a50b25-c544-4b75-82fc-7231bad6ceee', 'name': 'Xbox Gift Card | TR', 'type': 'voucher', 'countryCode': 'TR', 'categoryName': 'Gaming', 'subcategoryName': 'Xbox Gift Cards', 'fields': None, 'items': [{'id': '96e4e3ef-2124-4cac-8e04-ba6bb2eff2a2', 'name': 'TRY 25 Xbox gift card', 'price': 0.5304, 'currency': 'USD', 'inStock': 40, 'isLongOrder': True}, {'id': '8f649f3b-0a3f-438d-8922-26a492d3faec', 'name': 'TRY 50 Xbox gift card', 'price': 1.071, 'currency': 'USD', 'inStock': 40, 'isLongOrder': True}, {'id': 'd7b3dff7-ab11-4eb6-beb2-7a1b6c19f30d', 'name': 'TRY 100 Xbox gift card', 'price': 2.1318, 'currency': 'USD', 'inStock': 40, 'isLongOrder': True}]},
    {'id': '50c96e8a-632f-4f02-90bf-2114155a6e9a', 'name': 'Xbox Game Pass | US', 'type': 'voucher', 'countryCode': 'US', 'categoryName': 'Gaming', 'subcategoryName': 'Xbox Subscribitions', 'fields': None, 'items': [{'id': '8894f37c-2dff-4260-a9b6-0e5d21c68cdf', 'name': 'Xbox Game Pass Ultimate: 1 Month Subscription', 'price': 20.2677, 'currency': 'USD', 'inStock': 3, 'isLongOrder': False}, {'id': '8c4bd067-15cf-4ce8-98cf-60ecdafe21d0', 'name': 'Xbox Game Pass Ultimate: 3 Month Subscription', 'price': 60.8207, 'currency': 'USD', 'inStock': 10, 'isLongOrder': False}]},
    {'id': '003e210e-0ffd-40c5-a4d4-780382781fe6', 'name': 'Xbox Accounts | US', 'type': 'voucher', 'countryCode': 'US', 'categoryName': 'Accounts', 'subcategoryName': 'Xbox Accounts', 'fields': None, 'items': [{'id': '0ef8af86-b27d-4ce8-b0fc-9e2656b13392', 'name': '300 USD account', 'price': 242.5775, 'currency': 'USD', 'inStock': 19, 'isLongOrder': False}, {'id': '41b14254-7ee6-41e7-b683-4745be0e01a7', 'name': '400 USD account', 'price': 327.216, 'currency': 'USD', 'inStock': 1, 'isLongOrder': False}]},
    {'id': '699a8143-5cf1-44d2-bd2c-4f16beab170c', 'name': 'Sea of Thieves | Xbox', 'type': 'voucher', 'countryCode': 'GLOB', 'categoryName': 'Gaming', 'subcategoryName': 'Xbox Games', 'fields': None, 'items': [{'id': 'e0f55d84-1da3-4b7d-8125-658efdd521a0', 'name': 'Sea of Thieves: 550 Ancient Coins', 'price': 5.1933, 'currency': 'USD', 'inStock': 1861, 'isLongOrder': False}, {'id': '4b8c9b03-e0f8-42e3-8961-014695d67121', 'name': 'Sea of Thieves: 1000 Ancient Coins', 'price': 8.6613, 'currency': 'USD', 'inStock': 1170, 'isLongOrder': False}]},
    {'id': '1b2fd927-394b-45a3-bc8a-79c2e4f83e60', 'name': 'Roblox Wallet Code | XBox', 'type': 'voucher', 'countryCode': 'GLOB', 'categoryName': 'Gaming', 'subcategoryName': 'Roblox Gift Cards', 'fields': None, 'items': [{'id': '314c08c2-a99a-43ab-a40d-6c15ebac72f4', 'name': 'Roblox: 4,500 Robux for Xbox', 'price': 43.2787, 'currency': 'USD', 'inStock': 1, 'isLongOrder': False}]},
    {'id': '6fab908e-059f-4766-97c0-05faec93a12f', 'name': 'Roblox Wallet Code | GL', 'type': 'voucher', 'countryCode': 'GLOB', 'categoryName': 'Gaming', 'subcategoryName': 'Roblox Gift Cards', 'fields': None, 'items': [{'id': 'b5528afe-5ba5-40e0-8402-e28a8749a57d', 'name': '200 Robux', 'price': 3.57, 'currency': 'USD', 'inStock': 1881, 'isLongOrder': False}, {'id': '135fe1ce-0003-4443-80a4-d3e550e308d1', 'name': '800 Robux', 'price': 9.078, 'currency': 'USD', 'inStock': 2823, 'isLongOrder': False}]},
    {'id': '6dd21667-842e-4974-bbb8-a70539253711', 'name': 'Valorant Gift Card | RU', 'type': 'voucher', 'countryCode': 'RU', 'categoryName': 'Gaming', 'subcategoryName': 'Valorant Gift Card', 'fields': None, 'items': [{'id': '59d29a82-06e7-44c0-b628-2bd93a1e3dcb', 'name': 'VP 240 Valorant gift card', 'price': 2.55, 'currency': 'USD', 'inStock': 3157, 'isLongOrder': False}, {'id': 'e9b526c7-8c78-48b7-962c-87a3a999631d', 'name': 'VP 325 Valorant gift card', 'price': 3.366, 'currency': 'USD', 'inStock': 290, 'isLongOrder': False}, {'id': '6f0451a7-fdb6-40d2-98d6-1ae387358c32', 'name': 'VP 475 Valorant gift card', 'price': 4.6053, 'currency': 'USD', 'inStock': 71, 'isLongOrder': False}]},
    {'id': 'd7515407-8421-4efb-8701-3d259c2d93a2', 'name': 'Free Fire Gift Card', 'type': 'voucher', 'countryCode': 'GLOB', 'categoryName': 'Mobile Games', 'subcategoryName': 'Free Fire Gift Cards', 'fields': None, 'items': [{'id': '8a1c8205-4c91-4381-89c2-7dee4f496040', 'name': 'Free Fire: 100+10 Diamonds', 'price': 0.9384, 'currency': 'USD', 'inStock': 825, 'isLongOrder': False}, {'id': '69fcaecf-c0ae-4ec3-8e55-6352f06cb377', 'name': 'Free Fire: 210+21 Diamonds', 'price': 1.8948, 'currency': 'USD', 'inStock': 5781, 'isLongOrder': False}, {'id': '3da0e182-cae7-4fad-8adf-7ade1d6a7d18', 'name': 'Free Fire: 530+53 Diamonds', 'price': 4.8195, 'currency': 'USD', 'inStock': 182, 'isLongOrder': False}]},
    {'id': 'ecd0fe60-4e2a-4242-8337-45353b17c29a', 'name': 'Tinder Gift Card | DZ', 'type': 'voucher', 'countryCode': 'DZ', 'categoryName': 'Entertainment', 'subcategoryName': 'Tinder Gift Cards', 'fields': None, 'items': [{'id': '1d2282d3-be9e-4ed6-ae22-867d79fc790e', 'name': '1 Week Tinder Plus', 'price': 5.8446, 'currency': 'USD', 'inStock': 500, 'isLongOrder': False}, {'id': '73eafb9d-299f-448b-99e6-88ea7105413b', 'name': '1 Month Tinder Plus', 'price': 7.7928, 'currency': 'USD', 'inStock': 500, 'isLongOrder': False}, {'id': 'f0c14173-f116-41e6-971b-4662150ad346', 'name': '1 Week  Tinder Gold', 'price': 8.7669, 'currency': 'USD', 'inStock': 500, 'isLongOrder': False}]},
    {'id': '77146585-5bc1-4d52-a279-5ef5b8249a98', 'name': 'Nintendo Gift Card | BR', 'type': 'voucher', 'countryCode': 'BR', 'categoryName': 'Gaming', 'subcategoryName': 'Nintendo Gift Cards', 'fields': None, 'items': [{'id': '90bd7e2b-ce53-4768-8a5c-ea71c07ebba4', 'name': 'BRL 50 Nintendo gift card', 'price': 9.4231, 'currency': 'USD', 'inStock': 81, 'isLongOrder': False}, {'id': '1bd1e6b2-8aef-4015-8be8-91337bf9a8b6', 'name': 'BRL 100 Nintendo gift card', 'price': 18.8462, 'currency': 'USD', 'inStock': 83, 'isLongOrder': False}, {'id': '494f6f2f-8a08-48fd-9407-d63566e3441d', 'name': 'BRL 150 Nintendo gift card', 'price': 28.26, 'currency': 'USD', 'inStock': 76, 'isLongOrder': False}, {'id': 'cc3410eb-6b89-48ea-b5dd-61592ddb9b0e', 'name': 'BRL 250 Nintendo gift card', 'price': 47.1155, 'currency': 'USD', 'inStock': 38, 'isLongOrder': False}]},
]}


# Карта, объявленная ЗДЕСЬ, а не в модуле. Это и есть проверка обещания
# «добавить карту = дописать декларацию»: если шаблон настоящий, движок
# примет её наравне со своими.
XBOX_LOCAL = gc.GiftCard(
    slug="xbox_test", title="Xbox", emoji="🟩",
    subcategory="Xbox Gift Cards", unit=gc.MONEY,
    words=("xbox", "иксбокс"),
    activation="Активировать: xbox.com/redeem.",
)

VALORANT_LOCAL = gc.GiftCard(
    slug="vp_test", title="Valorant", emoji="🔫",
    subcategory="Valorant Gift Card", unit=gc.UNITS("VP"),
    words=("valorant", "валорант"),
    activation="Активировать в клиенте Valorant.",
)

FREEFIRE_LOCAL = gc.GiftCard(
    slug="ff_test", title="Free Fire", emoji="🔥",
    subcategory="Free Fire Gift Cards", unit=gc.UNITS("Diamonds"),
    words=("free fire", "фри фаер"),
    activation="Активировать в Free Fire.",
)

TINDER_LOCAL = gc.GiftCard(
    slug="tin_test", title="Tinder", emoji="💛",
    subcategory="Tinder Gift Cards", unit=gc.PERIOD,
    words=("tinder", "тиндер"),
    activation="Активировать в Tinder.",
)


class WhatTheXboxFamilyMustNotSwallow(unittest.TestCase):
    """Слово «xbox» в каталоге находит 47 услуг, а гифт-карт среди них 16.

    Остальное — подписки, ключи игр, аккаунты и кошельковый код Roblox для
    Xbox, то есть товар **чужого плагина**. Подбор по слову увёл бы его, и
    покупатель получил бы код Roblox вместо карты Xbox.

    Защит здесь две, и проверять надо **каждую на своём слое**. Первая —
    выбор услуги по подкатегории. Вторая — разбор номинала: `«Roblox: 4,500
    Robux for Xbox»`, `«1 Month Subscription»` и `«550 Ancient Coins»`
    деньгами не меряются, и в продажу не попадают даже если услуга выбрана
    неверно.

    Первая версия этих тестов проверяла только вторую защиту — и потому
    **не падала**, когда выбор по подкатегории подменяли выбором по слову.
    Тест, который не падает при откате правки, не проверяет ничего.
    """

    def _service_names(self, gift):
        return [str(s.get("name")) for s in gc.services(CATALOG, gift)]

    def _nominal_names(self, gift):
        return [r["name"] for r in gc.denominations(CATALOG, gift)]

    def test_a_roblox_wallet_code_never_leaks_into_the_xbox_family(self):
        """Главный: это товар AutoRoblox, а не карта Xbox."""
        chosen = self._service_names(XBOX_LOCAL)
        self.assertTrue(chosen, "карты Xbox должны найтись")
        self.assertNotIn("Roblox Wallet Code | XBox", chosen)
        # И на втором слое тоже: код Roblox не должен оказаться в продаже.
        self.assertNotIn("robux", " ".join(self._nominal_names(XBOX_LOCAL)).lower())

    def test_an_xbox_account_is_not_sold_as_a_three_hundred_dollar_card(self):
        """«300 USD account» разбирается как деньги идеально — и потому
        особенно опасен: по номиналу его от карты не отличить, спасает
        только выбор по подкатегории."""
        self.assertNotIn("Xbox Accounts | US", self._service_names(XBOX_LOCAL))
        self.assertNotIn("account", " ".join(self._nominal_names(XBOX_LOCAL)).lower())

    def test_an_xbox_game_pass_is_not_sold_as_a_gift_card(self):
        """Подписка меряется месяцами, а карта деньгами."""
        self.assertNotIn("Xbox Game Pass | US", self._service_names(XBOX_LOCAL))

    def test_a_game_key_is_not_sold_as_a_gift_card(self):
        self.assertNotIn("Sea of Thieves | Xbox", self._service_names(XBOX_LOCAL))

    def test_every_chosen_service_belongs_to_the_declared_subcategory(self):
        """Прямая проверка причины: по слову нашлось бы больше."""
        by_word = [s for s in CATALOG["items"]
                   if "xbox" in str(s.get("name", "")).lower()]
        by_sub = gc.services(CATALOG, XBOX_LOCAL)
        self.assertGreater(len(by_word), len(by_sub))
        for service in by_sub:
            self.assertEqual(service["subcategoryName"], "Xbox Gift Cards")


class HowTheNominalIsMeasured(unittest.TestCase):
    """Номинал меряется тремя разными вещами, и спутать их — денежно.

    Принять `VP 240` за двести сорок долларов значит продать в сотню раз
    дешевле или дороже, притом молча.
    """

    def test_valorant_points_are_not_read_as_dollars(self):
        rows = gc.denominations(CATALOG, VALORANT_LOCAL)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["measure"], "VP")

    def test_free_fire_bonus_diamonds_are_not_added_to_the_nominal(self):
        """«100+10 Diamonds» — это сто и десять сверху, а не сто десять.

        Покупатель заказывал сто. Сложить их значит подменить номинал.
        """
        rows = gc.denominations(CATALOG, FREEFIRE_LOCAL)
        values = {row["value"] for row in rows}
        self.assertIn(100.0, values)
        self.assertNotIn(110.0, values)

    def test_a_tinder_month_is_not_a_nominal_in_money(self):
        self.assertEqual(gc.face_value("1 Month Tinder Plus"), (0.0, ""))
        rows = gc.denominations(CATALOG, TINDER_LOCAL)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["measure"], ("day", "week", "month", "year"))

    def test_a_nominal_that_could_not_be_parsed_is_not_offered_for_sale(self):
        """Разобрать не смогли — значит не продаём. Не «продаём наугад»."""
        money_tinder = gc.GiftCard(
            slug="x", title="x", emoji="x",
            subcategory="Tinder Gift Cards", unit=gc.MONEY,
            words=("tinder",), activation="x")
        self.assertEqual(gc.denominations(CATALOG, money_tinder), [])


class WhichCardIsBought(unittest.TestCase):
    """Подбор номинала. Ошибка здесь — оплаченный заказ с чужим кодом."""

    def test_ten_dollars_in_two_regions_are_two_different_cards(self):
        us, _, _ = gc.match_denomination(CATALOG, XBOX_LOCAL, "US", 10, "USD")
        tr, _, _ = gc.match_denomination(CATALOG, XBOX_LOCAL, "TR", 25, "TRY")
        self.assertIsNotNone(us)
        self.assertIsNotNone(tr)
        self.assertNotEqual(us["denomination_id"], tr["denomination_id"])

    def test_a_nominal_from_another_region_is_never_substituted(self):
        row, why, _ = gc.match_denomination(CATALOG, XBOX_LOCAL, "TR", 10, "USD")
        self.assertIsNone(row)
        self.assertIn("TR", why)

    def test_an_exact_match_or_nothing(self):
        """Ближайший сверху — подарок за наш счёт, снизу — недоданное."""
        row, why, _ = gc.match_denomination(CATALOG, gc.APPLE, "AE", 60, "AED")
        self.assertIsNone(row)
        self.assertIn("50", why)

    def test_a_nominal_without_a_measure_is_taken_when_the_region_has_only_one(self):
        row, why, how = gc.match_denomination(CATALOG, gc.APPLE, "TR", 10)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["measure"], "TRY")
        self.assertIn("одна", how)

    def test_a_zero_stock_nominal_is_refused_with_its_reason(self):
        catalog = {"items": [{
            "id": "s1", "name": "Xbox Gift Card | US", "countryCode": "US",
            "subcategoryName": "Xbox Gift Cards", "type": "voucher",
            "items": [{"id": "d1", "name": "$10 Xbox gift card",
                       "price": 9.0, "currency": "USD", "inStock": 0}]}]}
        row, why, _ = gc.match_denomination(catalog, XBOX_LOCAL, "US", 10, "USD")
        self.assertIsNone(row)
        self.assertIn("остаток", why)

    def test_the_keyword_is_a_requirement_not_a_hint(self):
        """«Xbox аккаунт» не должен уходить в выдачу карт."""
        self.assertTrue(gc.is_card_order(XBOX_LOCAL, "Xbox Gift Card 10 USD"))
        self.assertFalse(gc.is_card_order(XBOX_LOCAL, "Xbox аккаунт",
                                          keyword="gift card"))


class TheReferenceThatPreventsBuyingTwice(unittest.TestCase):
    """Ссылка покупки — единственная защита от второго списания.

    Разойдись она между покупкой и повтором хоть на символ, поставщик не
    узнает заказ, ответит не `IDEMPOTENCY_REPLAY`, а покупкой — и деньги
    уйдут второй раз.
    """

    def test_the_reference_does_not_change_when_the_ad_title_changes(self):
        first = gc.order_reference("apple", "1218314")
        second = gc.order_reference("apple", "1218314")
        self.assertEqual(first, second)

    def test_two_cards_on_the_same_order_do_not_share_a_reference(self):
        self.assertNotEqual(gc.order_reference("apple", "1218314"),
                            gc.order_reference("xbox", "1218314"))

    def test_the_reference_fits_in_forty_chars(self):
        """1..40 символов — требование поставщика; длиннее это отказ 422."""
        long_one = gc.order_reference("playstation_store", "9" * 40)
        self.assertLessEqual(len(long_one), 40)
        self.assertTrue(long_one)


class SettingsOfEachCard(unittest.TestCase):
    """Настройки карты заводятся лениво, и это не лень.

    `_merge_defaults` в `storage.py` сливает раздел `plugins` на один
    уровень, поэтому объявленные умолчания карты, добавленной сегодня, у
    продавца со вчерашними настройками просто не появились бы. Ровно так
    20.08 уже ловили `KeyError`.
    """

    def test_settings_of_a_card_added_later_still_get_their_defaults(self):
        settings = {"plugins": {"gift_cards": {"apple": {"enabled": True}}}}
        conf = gc.card_conf(settings, "newcard")
        self.assertIn("log", conf)
        self.assertIn("delivered", conf)
        self.assertFalse(conf["enabled"])

    def test_each_card_gets_its_own_journal(self):
        """Общий список на все карты означал бы, что журнал Apple
        пополняется выдачами Xbox."""
        settings = {}
        one = gc.card_conf(settings, "apple")
        two = gc.card_conf(settings, "xbox")
        one["log"].append({"order": "1"})
        self.assertEqual(two["log"], [])

    def test_an_existing_conf_is_not_wiped_by_defaults(self):
        settings = {"plugins": {"gift_cards": {"apple": {"keyword": "яблоко"}}}}
        conf = gc.card_conf(settings, "apple")
        self.assertEqual(conf["keyword"], "яблоко")

    def test_only_enabled_cards_are_listed(self):
        settings = {}
        gc.card_conf(settings, "apple")["enabled"] = True
        gc.card_conf(settings, "psn")
        names = [c.slug for c in gc.enabled_cards(settings)]
        self.assertEqual(names, ["apple"])


class TheRegistryItself(unittest.TestCase):
    """Обещание шаблона: «одинаковые менюшки, меняется ид покупки».

    Одинаковым не может быть ровно одно — текст, который читает живой
    покупатель. Одна инструкция активации на всех означала бы, что бот
    уверенно отправляет неверный адрес.
    """

    def test_every_card_has_its_own_activation_text(self):
        texts = [c.activation for c in gc.cards()]
        self.assertEqual(len(texts), len(set(texts)))
        for text in texts:
            self.assertTrue(text.strip())

    def test_every_slug_is_unique_and_short_enough_for_callback_data(self):
        slugs = [c.slug for c in gc.cards()]
        self.assertEqual(len(slugs), len(set(slugs)))
        for slug in slugs:
            # `plugins:gc:<slug>:<действие>` обязан уместиться в 64 байта.
            self.assertLessEqual(len(f"plugins:gc:{slug}:set_keyword".encode()), 64)

    def test_every_card_knows_words_to_recognise_its_order(self):
        for gift in gc.cards():
            self.assertTrue(gift.words, gift.slug)

    def test_a_card_declared_outside_the_module_works_the_same(self):
        """Главное обещание: карта — это данные, а не код."""
        rows = gc.denominations(CATALOG, XBOX_LOCAL, "US")
        self.assertTrue(rows)
        self.assertTrue(all(r["region"] == "US" for r in rows))


class CodesFromTheSupplier(unittest.TestCase):
    """Замазанный код — не код. Отправить его значит отчитаться о выдаче,
    которой не было, притом ответ приходит успешный."""

    def test_masked_codes_are_never_returned(self):
        data = {"result": {"vouchers": [{"pin": "****9012"}]}}
        self.assertEqual(gc.codes_from_result(data), [])

    def test_codes_are_read_from_the_orders_listing_shape(self):
        data = {"page": {"items": [{"vouchers": [{"pin": "AAAA-BBBB"}]}]}}
        self.assertEqual(gc.codes_from_result(data), ["AAAA-BBBB"])

    def test_codes_are_read_from_the_purchase_shape(self):
        data = {"result": {"vouchers": [{"pin": "1234-5678"}]}}
        self.assertEqual(gc.codes_from_result(data), ["1234-5678"])


class WhereTheRegionLives(unittest.TestCase):
    """У Юмаркета поля региона нет вовсе, и описание — единственное место,
    где он переживает создание объявления."""

    def test_the_written_line_beats_a_guess_by_words(self):
        text = "Глобальный аккаунт не нужен.\n\nРегион кода: RU"
        self.assertEqual(gc.region_from_description(text), ("RU", "строка"))

    def test_two_regions_at_once_are_not_guessed(self):
        text = "Подойдёт для России и США"
        self.assertEqual(gc.region_from_description(text), ("", ""))

    def test_the_region_is_not_doubled_when_written_twice(self):
        once = gc.with_region("Описание", "TR")
        twice = gc.with_region(once, "TR")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(gc.REGION_PREFIX), 1)

    def test_the_region_comes_from_the_country_code_field(self):
        service = {"name": "Apple Gift Card | AE", "countryCode": "AE"}
        self.assertEqual(gc.region_of(service), "AE")


if __name__ == "__main__":
    unittest.main()
