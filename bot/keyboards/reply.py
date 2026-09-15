"""Постоянная клавиатура под полем ввода — только основное.

Меню бота живёт в сообщениях, и чтобы вернуться к заказам, надо сначала
найти нужное сообщение в переписке. Клавиатура снизу не уезжает вверх
вместе с историей: четыре ходовых раздела всегда под пальцем.

Кнопок намеренно мало. Постоянная клавиатура занимает треть экрана
телефона, и восемь пунктов главного меню сделали бы её ширмой вместо
подспорья: за ней не видно ни заказов, ни ответов покупателей. Всё
остальное — за «Меню».

Подписи здесь не берутся из `get_menu_labels`: переименованный продавцом
пункт разошёлся бы с обработчиком, который ловит нажатие ПО ТЕКСТУ, и
кнопка молча перестала бы работать. Один список — и подписи, и маршруты.
"""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

# (подпись, вид). Вид совпадает с именем команды: /orders, /chats, …
MAIN_BUTTONS: tuple[tuple[str, str], ...] = (
    ("🛒 Заказы", "orders"),
    ("💬 Чаты", "chats"),
    ("💰 Баланс", "balance"),
    ("📊 Статистика", "stats"),
    ("📋 Меню", "menu"),
)

# Раскладка: два ряда по два и «Меню» во всю ширину. Пятая кнопка в паре с
# четвёртой была бы вдвое уже соседей и читалась бы как обрезанная.
_ROWS = (2, 2, 1)

LABELS = {label for label, _kind in MAIN_BUTTONS}
BY_LABEL = {label: kind for label, kind in MAIN_BUTTONS}


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    rows, rest = [], list(MAIN_BUTTONS)
    for size in _ROWS:
        row, rest = rest[:size], rest[size:]
        if row:
            rows.append([KeyboardButton(text=label) for label, _k in row])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,      # иначе занимает пол-экрана
        is_persistent=True,        # не прячется после нажатия
        input_field_placeholder="Или напиши команду",
    )


def hide_keyboard() -> ReplyKeyboardRemove:
    """Убрать клавиатуру. Постоянная кнопка, которую нельзя убрать, —
    это не удобство, а навязанный интерфейс."""
    return ReplyKeyboardRemove()
