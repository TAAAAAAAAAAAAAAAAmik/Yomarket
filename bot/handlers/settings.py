from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import localtime as _lt
from storage import get_settings, save_settings

router = Router()


@router.callback_query(F.data == "settings:menu")
async def settings_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Автопилот", callback_data="ap:menu")
    b.button(text="🔔 Уведомления", callback_data="notif:menu")
    b.button(text="⚙️ Авто-функции", callback_data="auto:menu")
    b.button(text="👥 Аккаунты", callback_data="acc:menu")
    b.button(text="🌐 Панель продавца", callback_data="panel:menu")
    b.button(text=f"🕐 Часовой пояс · {_lt.offset_label(s)}",
             callback_data="tz:menu")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(1, 2, 2, 1, 1)
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        "⚡ <b>Автопилот</b> — включить все автоматизации в один тап.\n"
        "Остальные разделы — для тонкой настройки.",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


# Часовые пояса, в которых живут продавцы этого маркетплейса. Список, а не
# ввод числа: «UTC+5» знают не все, а «Екатеринбург» — все.
_ZONES: tuple[tuple[int, str], ...] = (
    (120, "Калининград"),
    (180, "Москва, Минск"),
    (240, "Самара, Баку"),
    (300, "Екатеринбург"),
    (360, "Омск, Астана"),
    (420, "Красноярск, Бишкек"),
    (480, "Иркутск"),
    (540, "Якутск"),
    (600, "Владивосток"),
    (0, "UTC (Лондон зимой)"),
)


def _tz_text(s: dict) -> str:
    now = _lt.now(s)
    return "\n".join([
        "🕐 <b>Часовой пояс</b>", "",
        f"Сейчас у вас: <b>{now.strftime('%H:%M')}</b>  ·  "
        f"{_lt.offset_label(s)}", "",
        "По нему считается всё, что связано со временем: время писем в чатах, "
        "отметки в журналах, «сегодня» в статистике, час итогов дня и окно "
        "ночного режима у автоответов.",
        "",
        "<i>Если время выше не совпадает с вашими часами — выберите город. "
        "Перевода часов нет: где он есть, поправьте пояс вручную.</i>",
    ])


def _tz_kb(s: dict) -> object:
    current = _lt.offset_minutes(s)
    b = InlineKeyboardBuilder()
    for minutes, name in _ZONES:
        mark = "✅ " if minutes == current else ""
        b.button(text=f"{mark}{name}", callback_data=f"tz:set:{minutes}")
    b.button(text="⬅️ Настройки", callback_data="settings:menu")
    b.adjust(2, 2, 2, 2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data == "tz:menu")
async def tz_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_tz_text(s), reply_markup=_tz_kb(s))
    await callback.answer()


@router.callback_query(F.data.startswith("tz:set:"))
async def tz_set(callback: CallbackQuery) -> None:
    try:
        minutes = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Не разобрал пояс", show_alert=True)
        return
    if not (_lt.MIN_OFFSET_MIN <= minutes <= _lt.MAX_OFFSET_MIN):
        await callback.answer("❌ Такого пояса не бывает", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    s["tz_offset_min"] = minutes
    save_settings(callback.from_user.id, s)
    await callback.answer(f"🕐 Теперь {_lt.now(s).strftime('%H:%M')}",
                          show_alert=False)
    await callback.message.edit_text(_tz_text(s), reply_markup=_tz_kb(s))
