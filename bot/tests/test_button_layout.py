"""Раскладка кнопок: короткие по две в ряд, длинные — по одной.

Экраны бота собирались с `adjust(1)` в ста одном месте — в столбик всё
подряд. На экране заказов это восемь рядов, из которых пять заняты
надписями в треть строки («🔍 Поиск», «⏳ Активные»), и до кнопки «⬅️
Главное меню» приходится прокручивать. Здесь проверяется, что раскладка
считает ширину надписи, а не длину строки Python, и что на экранах, где
промах пальцем стоит денег, кнопки остаются в столбик.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ui  # noqa: E402
from aiogram.utils.keyboard import InlineKeyboardBuilder  # noqa: E402


def rows(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


class WidthCountsWhatIsDrawn(unittest.TestCase):
    """Считали `len` — и «↩️ Возвраты» весило одиннадцать, потому что в нём
    невидимый селектор начертания. Ширина должна мерить нарисованное."""

    def test_an_invisible_variation_selector_does_not_add_width(self):
        self.assertEqual(ui.width("⬅️ Назад"), ui.width("⬅ Назад"))

    def test_a_zero_width_joiner_does_not_add_width(self):
        self.assertEqual(ui.width("👨‍💻"), ui.width("👨💻"))

    def test_an_emoji_is_wider_than_a_letter(self):
        self.assertGreater(ui.width("✅"), ui.width("X"))

    def test_plain_letters_are_counted_one_for_one(self):
        self.assertEqual(ui.width("Активные"), 8)

    def test_nothing_at_all_has_no_width(self):
        self.assertEqual(ui.width(""), 0)
        self.assertEqual(ui.width(None), 0)


class ShortLabelsShareARow(unittest.TestCase):
    """Ради чего всё затевалось."""

    def test_two_short_labels_stand_side_by_side(self):
        self.assertEqual(ui.sizes(["⏳ Активные", "🔍 Поиск"]), [2])

    def test_the_orders_footer_takes_three_rows_instead_of_five(self):
        got = ui.sizes(["🔍 Поиск", "✅ Выполненные", "⏳ Активные",
                        "↩️ Возвраты", "⬅️ Главное меню"])
        self.assertEqual(got, [2, 2, 1])

    def test_a_long_label_keeps_the_whole_row(self):
        self.assertEqual(
            ui.sizes(["🎮 Xbox Game Pass Ultimate 1 месяц", "⬅️ Назад"]),
            [1, 1],
        )

    def test_a_short_label_next_to_a_long_one_also_waits_its_turn(self):
        # Пара рвётся об любую половину, не только об первую.
        self.assertEqual(
            ui.sizes(["⬅️ Назад", "🎮 Xbox Game Pass Ultimate 1 месяц"]),
            [1, 1],
        )

    def test_a_list_of_goods_stays_in_a_column(self):
        goods = [f"🎁 Apple Gift Card {n} USD · 990 ₽" for n in (10, 25, 50)]
        self.assertEqual(ui.sizes(goods), [1, 1, 1])

    def test_an_odd_short_button_at_the_end_gets_its_own_row(self):
        self.assertEqual(ui.sizes(["а", "б", "в"]), [2, 1])

    def test_an_empty_keyboard_asks_for_one_row_not_for_none(self):
        # `adjust()` без размеров раскладывает по `max_width`, то есть в один
        # ряд целиком. Пустой список размеров тихо вернул бы это поведение.
        self.assertEqual(ui.sizes([]), [1])

    def test_a_button_marked_alone_never_shares_a_row(self):
        # «🧹 Очистить всё» стирает список покупателей без вопроса.
        self.assertEqual(ui.sizes(["а", "б", "в", "г"], alone=[1]), [1, 1, 2])

    def test_marking_a_button_alone_does_not_lose_its_neighbours(self):
        got = ui.sizes(["а", "б", "в", "г", "д"], alone=[2])
        self.assertEqual(got, [2, 1, 2])
        self.assertEqual(sum(got), 5)

    def test_the_threshold_is_a_parameter_not_a_constant_in_the_body(self):
        self.assertEqual(ui.sizes(["ааааа", "ббббб"], wide=3), [1, 1])
        self.assertEqual(ui.sizes(["ааааа", "ббббб"], wide=30), [2])


class LayReflowsARealKeyboard(unittest.TestCase):
    """`sizes` считает список надписей, `lay` — настоящую клавиатуру."""

    def build(self, *labels):
        b = InlineKeyboardBuilder()
        for i, text in enumerate(labels):
            b.button(text=text, callback_data=f"c{i}")
        return b

    def test_the_orders_footer_becomes_three_rows(self):
        b = self.build("🔍 Поиск", "✅ Выполненные", "⏳ Активные",
                       "↩️ Возвраты", "⬅️ Главное меню")
        self.assertEqual(rows(ui.lay(b).as_markup()), [
            ["🔍 Поиск", "✅ Выполненные"],
            ["⏳ Активные", "↩️ Возвраты"],
            ["⬅️ Главное меню"],
        ])

    def test_goods_stay_in_a_column_and_only_the_footer_pairs_up(self):
        b = self.build("🎁 Apple Gift Card 10 USD · 990 ₽",
                       "🎁 Apple Gift Card 25 USD · 2 400 ₽",
                       "🔄 Обновить", "⬅️ Назад")
        self.assertEqual(rows(b := ui.lay(b).as_markup())[:2],
                         [["🎁 Apple Gift Card 10 USD · 990 ₽"],
                          ["🎁 Apple Gift Card 25 USD · 2 400 ₽"]])
        self.assertEqual(rows(b)[2], ["🔄 Обновить", "⬅️ Назад"])

    def test_no_button_is_lost_or_duplicated(self):
        labels = ["а", "бббббббббббббббббббб", "в", "г", "д"]
        b = ui.lay(self.build(*labels))
        flat = [t for row in rows(b.as_markup()) for t in row]
        self.assertEqual(flat, labels)

    def test_a_single_button_keeps_its_row(self):
        self.assertEqual(rows(ui.lay(self.build("⬅️ Назад")).as_markup()),
                         [["⬅️ Назад"]])

    def test_a_solo_callback_keeps_its_row_to_itself(self):
        b = self.build("а", "стереть", "в")
        # Кнопки нумеруются c0, c1, c2 — «стереть» это c1.
        self.assertEqual(rows(ui.lay(b, solo={"c1"}).as_markup()),
                         [["а"], ["стереть"], ["в"]])

    def test_solo_matches_the_callback_and_not_the_caption(self):
        b = self.build("а", "стереть", "в")
        self.assertEqual(rows(ui.lay(b, solo={"стереть"}).as_markup()),
                         [["а", "стереть"], ["в"]])

    def test_lay_returns_the_builder_so_it_can_be_chained(self):
        b = self.build("а", "б")
        self.assertIs(ui.lay(b), b)


class MoneyButtonsStayInAColumn(unittest.TestCase):
    """«✅ Подтвердить» и «↩️ Возврат» стояли в одном ряду.

    Обе короткие, обе денежные, и промах пальцем на телефоне означает либо
    закрытую сделку вместо возврата, либо отданные деньги вместо закрытой
    сделки. Раскладка по ширине здесь не применяется намеренно — проверяем,
    что её и не применили.
    """

    def test_confirm_and_refund_are_not_neighbours(self):
        from keyboards.main import order_actions_keyboard
        got = rows(order_actions_keyboard("77", chat_id="5"))
        for row in got:
            self.assertLessEqual(
                len(row), 1,
                f"кнопки заказа встали в ряд: {row}")

    def test_the_order_screen_still_offers_everything_it_did(self):
        from keyboards.main import order_actions_keyboard
        flat = [t for row in rows(order_actions_keyboard("77", chat_id="5"))
                for t in row]
        self.assertEqual(len(flat), 4)
        self.assertTrue(any("Подтвердить" in t for t in flat))
        self.assertTrue(any("Возврат" in t for t in flat))


class ClearingTheBlacklistIsNotNextToBack(unittest.TestCase):
    """`bl:clear` стирает чёрный список сразу, без подтверждения.

    Обе кнопки коротки, и раскладка по ширине поставила бы «🧹 Очистить всё»
    вплотную к «⬅️ Уведомления» — под тот же палец, которым выходят назад.
    """

    def test_the_clear_button_stands_on_its_own_row(self):
        from handlers.notifications import _bl_kb
        got = _bl_kb({"blacklist": ["ivan", "petr"]})
        for row in rows(got):
            self.assertFalse(
                len(row) > 1 and any("Очистить" in t for t in row),
                f"«Очистить всё» встало в ряд с соседом: {row}")

    def test_the_clear_button_is_offered_at_all_when_there_is_something_to_clear(self):
        from handlers.notifications import _bl_kb
        flat = [t for row in rows(_bl_kb({"blacklist": ["ivan"]})) for t in row]
        self.assertTrue(any("Очистить" in t for t in flat))


class ThePreviewScreenGroupsTheThreeEdits(unittest.TestCase):
    """«✏️ Изменить название», «✏️ Изменить цену», «✏️ Изменить описание».

    Слово «Изменить» повторялось в каждой надписи, съедало половину ширины
    и занимало три строки под то, что читается как один набор. Экран
    предпросмотра из-за этого не помещался на телефон целиком вместе с
    описанием товара.
    """

    def rows_of(self, has_photo: bool):
        from handlers.create_ad import _confirm_kb
        return rows(_confirm_kb(has_photo))

    def test_the_three_edits_share_one_row(self):
        for has_photo in (True, False):
            edits = [r for r in self.rows_of(has_photo) if len(r) == 3]
            self.assertEqual(len(edits), 1, f"фото={has_photo}: {self.rows_of(has_photo)}")
            self.assertEqual(edits[0], ["✏️ Название", "✏️ Цена", "✏️ Описание"])

    def test_cancel_is_not_next_to_an_edit_button(self):
        # «Отмена» бросает всё набранное, и промах по ней не отменить.
        for has_photo in (True, False):
            for row in self.rows_of(has_photo):
                if any("Отмена" in t for t in row):
                    self.assertEqual(len(row), 1, row)

    def test_the_edits_still_lead_where_they_led(self):
        from handlers.create_ad import _confirm_kb
        data = {b.text: b.callback_data
                for row in _confirm_kb(True).inline_keyboard for b in row}
        self.assertEqual(data["✏️ Название"], "create_ad:edit:title")
        self.assertEqual(data["✏️ Цена"], "create_ad:edit:price")
        self.assertEqual(data["✏️ Описание"], "create_ad:edit:description")

    def test_without_a_photo_there_is_no_create_button(self):
        # Панель без картинки товар не принимает — обещать нечего.
        flat = [t for row in self.rows_of(False) for t in row]
        self.assertFalse(any("Создать товар" in t for t in flat))
        self.assertTrue(any("Добавить фото" in t for t in flat))


class TheAdminButtonDoesNotGlueToAMenuItem(unittest.TestCase):
    """`adjust(2)` вызывался до того, как добавлялась кнопка админа.

    `add()` дописывает новую кнопку в последний ряд, если там есть место, —
    и «👑 Админ-панель» вставала рядом со случайным пунктом меню, если
    пунктов было нечётное число, и отдельной строкой, если чётное. То есть
    вёрстка зависела от того, сколько пунктов продавец включил.
    """

    def test_the_admin_button_owns_its_row_whatever_the_menu_size(self):
        from keyboards.main import main_menu_keyboard
        import storage
        saved = storage.MENU_BUTTONS
        try:
            for count in (3, 4, 5):
                storage.MENU_BUTTONS = tuple(
                    (f"k{i}", f"Пункт {i}", f"menu:{i}") for i in range(count))
                got = rows(main_menu_keyboard(is_admin_user=True))
                self.assertEqual(
                    got[-1], ["👑 Админ-панель"],
                    f"при {count} пунктах меню админ-кнопка встала так: {got}")
        finally:
            storage.MENU_BUTTONS = saved

    def test_without_admin_rights_there_is_no_admin_button(self):
        from keyboards.main import main_menu_keyboard
        flat = [t for row in rows(main_menu_keyboard(is_admin_user=False))
                for t in row]
        self.assertFalse(any("Админ" in t for t in flat))


if __name__ == "__main__":
    unittest.main()
