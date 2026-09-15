"""Экран поддержки — один на всё.

Их было два: свой у `/help` и свой у кнопки «🧡 Поддержка». Тексты
разъехались (один звал прислать `/version`, второй нет), и заметить это
было нечем — экраны никто не сверял. Теперь он собирается здесь, а команда
и кнопка только показывают его.

Что на нём есть и почему:

* **Telegram ID продавца.** Первое, что спрашивает поддержка, и первое,
  чего человек про себя не знает. Без него разговор начинается с «а какой
  у вас номер», а найти его негде.
* **До какого числа подписка.** Половина обращений — «а у меня ещё
  работает?». Ответ на экране дешевле ответа в переписке.
* **Код сборки — только админу.** Продавцу он ничего не объясняет, а
  рассказывает о боте то, чего тот не спрашивал; прячется по тому же
  правилу, что и `/version`. Владельцу метка остаётся: он воспроизводит
  беду у себя и видит её на том же месте (`ui.build_mark`).
"""
from __future__ import annotations

import ui


def support_text(user_id: int) -> str:
    from storage import (get_settings, get_subscription, get_support_contact,
                         is_lifetime, subscription_days_left)
    import localtime as _lt

    contact = get_support_contact()
    body = [
        "🚀 Я на связи — пиши, если что-то пошло не так или наоборот "
        "придумалось.",
        "",
        "🐞 <b>Нашёл ошибку?</b> Расскажи — за находку добавлю дней "
        "подписки.",
        "💡 <b>Есть идея?</b> Предлагай: что окажется полезным всем — "
        "сделаю.",
        # Контакт печатается ТЕКСТОМ, а не только кнопкой: кнопка-ссылка
        # рисуется лишь для контакта вида `@ник`, а владелец может вписать
        # что угодно. Тогда экран поддержки остался бы без поддержки.
        f"🆘 <b>Нужна помощь?</b> Напиши {ui.esc(contact)} — разберёмся.",
        "",
    ]

    sub = get_subscription(user_id)
    expires = float((sub or {}).get("expires") or 0)
    if is_lifetime(user_id):
        # Дата через сто лет читается как сбой, а не как «навсегда».
        line = "📅 Подписка: <b>навсегда</b>"
    elif expires > 0 and subscription_days_left(user_id) >= 0 and sub:
        when = _lt.fmt(expires, get_settings(user_id), "%d.%m.%Y %H:%M")
        left = subscription_days_left(user_id)
        line = (f"📅 Подписка активна до: <b>{when}</b> "
                f"<i>({left} дн.)</i>" if left > 0 else
                # Ноль дней — это не «активна», а «кончается сегодня».
                # Написать «активна до» и дать нулевой остаток значит
                # предложить человеку самому догадаться, что это значит.
                f"📅 Подписка кончается сегодня: <b>{when}</b>")
    else:
        line = "📅 Подписки нет — открыть можно на экране «🚀 Получить доступ»."
    body += [f"🆔 Твой Telegram ID: <code>{int(user_id)}</code>", line]

    mark = ui.build_mark(user_id)
    return ui.screen(
        "💡 <b>Центр помощи YooMarket</b>", body,
        footer=f"<i>Код сборки:</i>{mark}" if mark else "")


def support_kb(user_id: int = 0):
    """Кнопки экрана. «Назад» ведёт туда, откуда сюда попадают: у кого
    магазин подключён — в меню, у остальных — на витрину."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from storage import get_support_contact, get_token
    contact = get_support_contact()
    b = InlineKeyboardBuilder()
    if contact.startswith("@"):
        b.button(text="🆘 Поддержка", url=f"https://t.me/{contact[1:]}")
    b.button(text="📄 Документы", callback_data="menu:policy:help")
    b.button(text="⬅️ Назад",
             callback_data="menu:main" if get_token(user_id) else "start:hello")
    return ui.lay(b, solo={"menu:policy:help"}).as_markup()
