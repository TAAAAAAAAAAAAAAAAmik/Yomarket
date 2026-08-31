"""Покупка подписки: срок → способ оплаты → «я оплатил».

Приёма денег внутри бота нет и не обещается. Продавец платит владельцу
напрямую, бот показывает, куда именно, и передаёт владельцу заявку.

Прежний экран предлагал «Прошу счёт» — то есть просил человека подождать,
пока с ним свяжутся. Половина не дожидалась. Здесь он сам выбирает срок,
сам видит реквизиты и платит, не выходя из бота.

Два места, где легко соврать:

* **Срок без цены не показывается.** «1 месяц — 0 ₽» читается как
  «бесплатно», а это обещание, за которое спросят.
* **Способ оплаты без реквизитов не показывается.** Кнопка, за которой
  пусто, — обещание невозможного: нажмёт и увидит ничего.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

router = Router()


def _tier_label(days: int) -> str:
    from storage import PRICE_TIERS
    return dict(PRICE_TIERS).get(int(days), f"{days} дн.")


@router.callback_query(F.data == "sub:buy")
async def choose_term(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1 — за какой срок платим."""
    await state.clear()
    from storage import PRICE_TIERS, get_prices, get_support_contact
    prices = get_prices()

    b = InlineKeyboardBuilder()
    for days, label in PRICE_TIERS:
        price = prices.get(days)
        if price:
            b.button(text=f"{label} — {price} ₽",
                     callback_data=f"sub:buy:{days}")
    b.button(text="⬅️ Назад", callback_data="access:menu")

    body = (["Выбери срок — дальше покажу, куда платить."] if prices else
            ["<i>Цены пока не назначены. Напиши "
             f"{ui.esc(get_support_contact())} — договоримся.</i>"])
    await callback.message.edit_text(
        ui.screen("💳 <b>Оплатить подписку</b>", body),
        # Сроки — каждый своей строкой: «2 недели» и «12 месяцев» рядом
        # читаются как один тариф, а промах пальцем стоит денег.
        reply_markup=ui.lay(b, solo={f"sub:buy:{d}"
                                     for d, _l in PRICE_TIERS}).as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("sub:buy:"))
async def choose_method(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 2 — чем платим. Сразу после срока, без промежуточных экранов."""
    await state.clear()
    from storage import get_pay_methods, get_prices, get_support_contact
    try:
        days = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return
    price = get_prices().get(days)
    if not price:
        # Тариф сняли, пока человек смотрел на кнопку. Молча вернуть его на
        # шаг назад — значит оставить с ощущением, что бот сломался.
        await callback.answer("Этот срок больше не продаётся — выбери другой",
                              show_alert=True)
        await choose_term(callback, state)
        return

    methods = [m for m in get_pay_methods() if m.get("details")]
    b = InlineKeyboardBuilder()
    for m in methods:
        b.button(text=m["title"], callback_data=f"sub:m:{days}:{m['id']}")
    b.button(text="⬅️ Другой срок", callback_data="sub:buy")

    body = [f"<b>{_tier_label(days)}</b> — <b>{price} ₽</b>", ""]
    body += (["Чем платишь?"] if methods else
             ["<i>Способы оплаты пока не настроены. Напиши "
              f"{ui.esc(get_support_contact())} — договоримся.</i>"])
    await callback.message.edit_text(
        ui.screen("💳 <b>Способ оплаты</b>", body),
        reply_markup=ui.lay(b, solo={f"sub:m:{days}:{m['id']}"
                                     for m in methods}).as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("sub:m:"))
async def show_details(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 3 — реквизиты и кнопка «я оплатил»."""
    await state.clear()
    from storage import get_prices, pay_method
    parts = callback.data.split(":")
    try:
        days, mid = int(parts[2]), parts[3]
    except (IndexError, ValueError):
        await callback.answer()
        return
    method = pay_method(mid)
    price = get_prices().get(days)
    if not method or not price:
        await callback.answer("Этот способ больше не доступен — выбери другой",
                              show_alert=True)
        await choose_term(callback, state)
        return

    b = InlineKeyboardBuilder()
    b.button(text="✅ Я оплатил", callback_data=f"pay:paid:{days}:{mid}")
    b.button(text="⬅️ Другой способ", callback_data=f"sub:buy:{days}")
    await callback.message.edit_text(ui.screen(
        f"💳 <b>{ui.esc(method['title'])}</b>",
        [f"К оплате: <b>{price} ₽</b> за {_tier_label(days)}", "",
         ui.esc(method["details"]), "",
         "Оплатил — жми кнопку ниже, и владелец включит доступ."],
        footer="<i>Доступ включает владелец вручную: проверять оплату бот "
               "не умеет и делать вид не будет.</i>"),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.callback_query(F.data == "trial:menu")
async def free_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Бесплатные дни — обе пробы на одном экране.

    Пробы независимы: взявший три дня всё ещё может добрать семь за
    подписку. Кнопка пробы тому, кто её уже брал, — обещание невозможного.
    """
    await state.clear()
    from storage import (get_trial_channel, get_trial_days,
                         get_trial_free_days, trial_used)
    uid = callback.from_user.id
    free_days, long_days = get_trial_free_days(), get_trial_days()
    channel = get_trial_channel()

    b = InlineKeyboardBuilder()
    body: list[str] = []
    if free_days > 0 and not trial_used(uid, "free"):
        body.append(f"🎁 <b>{free_days} дня</b> — просто так, без условий")
        b.button(text=f"🎁 Взять {free_days} дня", callback_data="trial:free")
    if long_days > 0 and channel and not trial_used(uid, "channel"):
        body.append(f"📣 <b>+{long_days} дней</b> — за подписку на канал")
        b.button(text=f"📣 Взять +{long_days} дней", callback_data="trial:offer")
    if len(body) > 1:
        body += ["", "<i>Берётся и то, и другое — по одному разу.</i>"]
    b.button(text="⬅️ Назад", callback_data="access:menu")

    await callback.message.edit_text(
        ui.screen("🎁 <b>Получить бесплатно</b>",
                  body or ["Бесплатные дни ты уже брал — оба раза.", "",
                           "Дальше только по подписке."]),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()
