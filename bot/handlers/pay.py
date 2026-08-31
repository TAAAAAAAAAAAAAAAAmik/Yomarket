"""Оплата подписки переводом внутри Bybit — со стороны продавца.

Он переводит USDT на UID владельца, бот видит поступление и засчитывает
оплату сам. Разбор поступлений — в `billing.py`, разговор с Bybit — в
`automation/bybit.py`.

Голос здесь — продавцу, то есть на «ты»: это экран бота, а не магазина.

Два места, где легко соврать, и оба закрыты:

* **UID продавца спрашивается до перевода, а не после.** В записи о
  поступлении Bybit называет UID отправителя — и это единственное, чем
  перевод привязывается к человеку. Не спросив, мы взяли бы деньги и не
  знали, кому выдавать.
* **Чужой UID занять нельзя.** Иначе вписавший его забирал бы чужие
  оплаты, а пострадавший видел бы только, что деньги ушли, а подписки нет.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

router = Router()


class PayState(StatesGroup):
    uid = State()


def _kb(has_uid: bool):
    b = InlineKeyboardBuilder()
    b.button(text=("✏️ Изменить мой UID" if has_uid else "🆔 Указать мой UID"),
             callback_data="pay:uid")
    b.button(text="🔄 Я оплатил", callback_data="pay:check")
    b.button(text="⬅️ Назад", callback_data="access:menu")
    return b


def _screen(user_id: int):
    """Экран оплаты: куда переводить, сколько и от кого мы ждём."""
    from storage import (PRICE_TIERS, get_bybit, get_payer_uid,
                         get_usdt_prices, usdt)
    to_uid = str(get_bybit().get("uid") or "")
    mine = get_payer_uid(user_id)
    prices = get_usdt_prices()

    body = [f"Переведи USDT на UID <code>{ui.esc(to_uid)}</code> внутри "
            "Bybit — переводы между кошельками Bybit мгновенные и без "
            "комиссии.", "", "<b>Сколько и за что</b>"]
    for days, label in PRICE_TIERS:
        price = prices.get(days)
        if price:
            body.append(f"• {label} — <b>{usdt(price)} USDT</b>")
    body += ["", "<i>Пришлёшь больше — засчитаю самый длинный срок, который "
                 "сумма покрывает.</i>", ""]
    if mine:
        body.append(f"🆔 Жду перевод с твоего UID <code>{ui.esc(mine)}</code>.")
    else:
        # Без этого перевод не к кому приписать — и сказать об этом надо
        # ДО перевода, а не после того, как деньги ушли.
        body.append("⚠️ <b>Сначала укажи свой UID на Bybit</b> — иначе я не "
                    "пойму, что перевод от тебя, и подписка не включится "
                    "сама.")
    return ui.screen("₿ <b>Оплата через Bybit</b>", body,
                     footer="<i>Доступ включится сам, обычно в течение "
                            "минуты после перевода.</i>"), _kb(bool(mine))


@router.callback_query(F.data == "pay:menu")
async def pay_menu(callback: CallbackQuery, state: FSMContext) -> None:
    from storage import bybit_ready
    await state.clear()
    if not bybit_ready():
        # Экран, предлагающий заплатить туда, куда сейчас нельзя, — обещание
        # невозможного. Причину продавцу знать незачем, а деться ему есть куда.
        await callback.answer("Оплата — через владельца, жми «Прошу счёт».",
                              show_alert=True)
        return
    text, kb = _screen(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "pay:uid")
async def pay_uid_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PayState.uid)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="pay:menu")
    await callback.message.edit_text(ui.screen(
        "🆔 <b>Твой UID на Bybit</b>",
        ["Пришли его числом.", "",
         "Где взять: приложение Bybit → «Профиль», UID стоит под именем. "
         "Это не адрес кошелька и не почта — только цифры.", "",
         "<i>Он нужен, чтобы я узнал твой перевод среди прочих. Ничего, "
         "кроме этого, я по нему не вижу.</i>"]),
        reply_markup=ui.lay(b).as_markup())
    await callback.answer()


@router.message(PayState.uid)
async def pay_uid_save(message: Message, state: FSMContext) -> None:
    from automation.bybit import parse_uid
    from storage import set_payer_uid
    uid = parse_uid(message.text or "")
    if not uid:
        await message.answer("Это не похоже на UID — нужны только цифры. "
                             "Пришли ещё раз.")
        return
    if not set_payer_uid(message.from_user.id, uid):
        # Не «ошибка», а прямое объяснение: чаще всего человек ошибся
        # цифрой, реже — вписывает чужой намеренно.
        await message.answer(
            "Этот UID уже закреплён за другим продавцом. Проверь цифры — "
            "если всё верно, напиши в поддержку.")
        return
    await state.clear()
    # Он мог заплатить раньше, чем назвал UID. Ждать следующего перевода
    # ему не за что.
    from billing import settle_pending
    got = await settle_pending(message.bot, message.from_user.id, uid)
    if got:
        return                                     # billing уже всё сказал
    await message.answer(f"✅ Запомнил: <code>{uid}</code>.\n"
                         "Теперь переводи — доступ включится сам.")
    text, kb = _screen(message.from_user.id)
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "pay:check")
async def pay_check(callback: CallbackQuery, state: FSMContext) -> None:
    """«Я оплатил» — посмотреть прямо сейчас, не дожидаясь прохода.

    Кнопка не обещает найти перевод: если его ещё нет, так и говорим, а не
    отвечаем бодрым «проверяю» и тишиной.
    """
    from billing import collect
    from storage import get_payer_uid, subscription_days_left
    if not get_payer_uid(callback.from_user.id):
        await callback.answer("Сначала укажи свой UID на Bybit",
                              show_alert=True)
        return
    await callback.answer("Смотрю поступления…")
    got = await collect(callback.bot)
    if [g for g in got if g.get("user") == callback.from_user.id]:
        return                                     # billing уже сказал
    left = subscription_days_left(callback.from_user.id)
    await callback.answer(
        ("Нового перевода пока не вижу. Bybit иногда думает пару минут — "
         "загляни ещё раз."
         if not left else
         f"Нового перевода пока не вижу. Доступ у тебя есть, осталось "
         f"{left} дн."), show_alert=True)
