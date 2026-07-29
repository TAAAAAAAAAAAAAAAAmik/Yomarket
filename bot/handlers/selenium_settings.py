"""Auto-bump, auto-restore, auto-withdraw via YooMarket Integration API."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class SeleniumState(StatesGroup):
    waiting_bump_interval = State()
    waiting_withdraw_amount = State()
    waiting_promo_value = State()


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts)) if isinstance(ts, (int, float)) else datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return str(ts)[:16]


def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=back)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Auto-bump
# ---------------------------------------------------------------------------

def promo_params(s: dict) -> dict:
    """The tariff the seller picked, as the payload the Nova action expects."""
    return (s.get("auto_bump", {}).get("promo") or {}).get("values") or {}


def promo_price(s: dict) -> int:
    return int((s.get("auto_bump", {}).get("promo") or {}).get("price") or 0)


def promo_limit(s: dict) -> int:
    """How many listings the daily ceiling allows at the chosen tariff.
    0 = no ceiling set, so no cap."""
    ceiling = s.get("bump_stats", {}).get("daily_ceiling", 0)
    price = promo_price(s)
    if not ceiling or not price:
        return 0
    return max(0, int(ceiling // price))


async def promo_afford(api, s: dict) -> tuple[bool, int, str]:
    """Can the shop pay for a promotion right now?

    Returns (ok, how_many_listings_it_covers, human_text). Promotion is paid
    from the shop's balance, so running out of money is a normal outcome that
    deserves a plain answer instead of a panel error.
    """
    price = promo_price(s)
    if not api:
        return True, 0, ""
    try:
        balance, shown = await api.get_balance()
    except Exception as e:
        return True, 0, f"баланс не проверить ({str(e)[:60]})"
    if shown == "—":
        return True, 0, "баланс не проверить"
    if not price:
        return True, 0, f"на балансе {shown}"
    covers = int(balance // price)
    if covers < 1:
        return False, 0, (
            f"на балансе <b>{shown}</b>, а одно поднятие стоит "
            f"<b>{price} ₽</b> — не хватает {price - balance:.0f} ₽")
    return True, covers, f"на балансе <b>{shown}</b> — хватит на {covers} шт."


def _bump_text(s: dict, creds=None) -> str:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    last_run = _fmt_ts(ab.get("last_bump_run"))
    ceiling = s.get("bump_stats", {}).get("daily_ceiling", 0)
    limit = f"{ceiling:.0f} ₽/день" if ceiling else "не задан"
    promo = ab.get("promo") or {}
    if promo.get("values"):
        tariff = promo.get("label") or "выбран"
        price = promo_price(s)
        tariff_line = f"Тариф: <b>{tariff}</b>" + (
            f"  ·  {price} ₽ за объявление" if price else "")
        cap = promo_limit(s)
        if cap:
            tariff_line += f"\nПотолок покрывает: <b>{cap}</b> объявлений за запуск"
    else:
        tariff_line = "Тариф: <b>не выбран</b> — продвижение не запустится"
    return "\n".join([
        "⭐ <b>Премиум продвижение</b>\n",
        f"Статус: {_st(on)}",
        f"Интервал: каждые {interval} ч",
        f"Последний запуск: {last_run}",
        f"Потолок трат: {limit}",
        tariff_line,
        "",
        "⚠️ <b>Платно.</b> На Юмаркете поднятие — это «Премиум», деньги "
        "списываются с баланса магазина за каждое объявление.",
    ])


def _bump_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    ab = s.get("auto_bump", {})
    on = ab.get("enabled", False)
    interval = ab.get("interval_hours", 24)
    has_tariff = bool((ab.get("promo") or {}).get("values"))
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:bump")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:bump:toggle")
    b.button(text=f"⚙️ Тариф{'' if has_tariff else ' — выбрать'}",
             callback_data="promo:setup")
    b.button(text=f"⏱ Интервал: {interval} ч", callback_data="selenium:bump:set_interval")
    b.button(text="⬅️ К объявлениям", callback_data="menu:ads")
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Tariff picker for the paid «Премиум» action
#
# The action refuses to run without up_id (service), parameter_id (duration,
# 19/49/89 ₽) and system_id (payment source). None of that can be guessed: each
# combination spends a different amount of the shop's money, so the options are
# read from the panel and chosen here once, then reused by every promotion.
# ---------------------------------------------------------------------------

async def _load_promo_spec(uid: int) -> tuple[bool, object]:
    import asyncio

    from automation.panel import panel_list_items_sync, panel_promo_fields_sync
    from storage import get_panel_creds

    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return False, "Нужен вход в панель продавца"
    cookies = creds["cookies"]
    loop = asyncio.get_event_loop()
    ok, items = await loop.run_in_executor(None, panel_list_items_sync, cookies)
    if not ok:
        return False, str(items)
    ids = [i.get("id") for i in items if i.get("id")]
    if not ids:
        return False, "В панели нет объявлений — не с чего читать тарифы"
    return await loop.run_in_executor(
        None, panel_promo_fields_sync, cookies, str(ids[0]))


def _promo_step_kb(spec: dict, step: int) -> InlineKeyboardMarkup:
    field = spec["fields"][step]
    b = InlineKeyboardBuilder()
    for i, o in enumerate(field["options"][:12]):
        b.button(text=o["label"][:60], callback_data=f"promo:pick:{step}:{i}")
    b.button(text="❌ Отмена", callback_data="selenium:bump:menu")
    b.adjust(1)
    return b.as_markup()


async def _promo_step(user_id: int, send, state: FSMContext,
                      spec: dict, step: int, picked: dict) -> None:
    """Show the next field that still needs a choice, or save the tariff.

    `send(text, reply_markup=None)` delivers each step, so the same walk works
    whether it was reached by a button or by a typed value.
    """
    import asyncio

    from automation.panel import panel_action_field_options_sync
    from storage import get_panel_creds

    fields = spec["fields"]
    while step < len(fields):
        f = fields[step]
        opts = f["options"]

        if not opts and f.get("lookup"):
            # A dependent field only gets its options once the choices it
            # depends on are known, so this is asked for here, not up front.
            creds = get_panel_creds(user_id) or {}
            chosen = {k: v["value"] for k, v in picked.items()}
            await send(f"⏳ Узнаю варианты для «{f['label']}»...")
            opts, trace = await asyncio.get_event_loop().run_in_executor(
                None, panel_action_field_options_sync, creds.get("cookies", ""),
                str(spec.get("item_id") or ""), spec.get("key") or "",
                f["attribute"], chosen, f.get("component_key") or "",
                f.get("depends_on") or {})
            f["options"] = opts
            f["lookup"] = False
            f["trace"] = trace

        if len(opts) == 1:
            # One possibility is not a choice — take it and move on
            picked[f["attribute"]] = {"value": opts[0]["value"],
                                      "label": opts[0]["label"],
                                      "price": opts[0].get("price", 0)}
            step += 1
            continue

        await state.update_data(promo_spec=spec, promo_picked=picked,
                                promo_step=step)

        if not opts:
            # Never skip an option-less field: the panel validates it anyway,
            # and skipping it is exactly what made promotion fail with 422.
            await state.set_state(SeleniumState.waiting_promo_value)
            info = _esc(f"{f.get('shape') or ''} | {f.get('trace') or ''}")
            b = InlineKeyboardBuilder()
            b.button(text="❌ Отмена", callback_data="selenium:bump:menu")
            await send(
                f"⚠️ Панель не отдала варианты для поля "
                f"<b>{_esc(f['label'])}</b> (<code>{_esc(f['attribute'])}</code>).\n\n"
                f"Пришлите значение сообщением — его видно в панели: откройте "
                f"«Премиум» у объявления и посмотрите, что стоит в этом поле.\n\n"
                f"<code>{info[:300]}</code>",
                b.as_markup())
            return

        await state.set_state(None)
        await send(
            f"⭐ <b>Премиум продвижение</b>\n\n"
            f"Шаг {step + 1} из {len(fields)} — <b>{_esc(f['label'])}</b>\n\n"
            f"<i>Выбор запоминается и применяется ко всем поднятиям.</i>",
            _promo_step_kb(spec, step))
        return

    # Everything chosen
    values = {k: v["value"] for k, v in picked.items()}
    price = max((v.get("price", 0) for v in picked.values()), default=0)
    label = " · ".join(str(v["label"]) for v in picked.values() if v.get("label"))
    s = get_settings(user_id)
    s.setdefault("auto_bump", {})["promo"] = {
        "values": values, "label": label, "price": price,
        "action": spec.get("key"),
    }
    save_settings(user_id, s)
    await state.clear()
    await send(
        f"✅ <b>Тариф сохранён</b>\n\n{_esc(label)}"
        + (f"\n\nСтоимость: <b>{price} ₽</b> за объявление" if price else "")
        + "\n\n" + _bump_text(s),
        _bump_kb(s))


def _cb_send(callback: CallbackQuery):
    async def send(text, reply_markup=None):
        await callback.message.edit_text(text, reply_markup=reply_markup)
    return send


def _msg_send(message: Message):
    async def send(text, reply_markup=None):
        await message.answer(text, reply_markup=reply_markup)
    return send


@router.callback_query(F.data == "promo:setup")
async def promo_setup(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("⏳ Читаю тарифы из панели...")
    await callback.message.edit_text("⏳ Читаю тарифы «Премиум» из панели...")
    ok, spec = await _load_promo_spec(callback.from_user.id)
    if not ok:
        await callback.message.edit_text(
            f"❌ Не удалось прочитать тарифы:\n{str(spec)[:300]}",
            reply_markup=_cancel_kb("selenium:bump:menu"))
        return
    await _promo_step(callback.from_user.id, _cb_send(callback), state,
                      spec, 0, {})


@router.callback_query(F.data.startswith("promo:pick:"))
async def promo_pick(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, step_s, idx_s = callback.data.split(":")
        step, idx = int(step_s), int(idx_s)
    except ValueError:
        await callback.answer("❌ Ошибка выбора", show_alert=True)
        return
    data = await state.get_data()
    spec = data.get("promo_spec")
    if not spec:
        await callback.answer("Начните заново: «⚙️ Тариф»", show_alert=True)
        return
    picked = data.get("promo_picked") or {}
    field = spec["fields"][step]
    o = field["options"][idx]
    picked[field["attribute"]] = {"value": o["value"], "label": o["label"],
                                  "price": o.get("price", 0)}
    await callback.answer(str(o["label"])[:60])
    await _promo_step(callback.from_user.id, _cb_send(callback), state,
                      spec, step + 1, picked)


@router.message(SeleniumState.waiting_promo_value)
async def promo_manual_value(message: Message, state: FSMContext) -> None:
    """Take a value the seller read off the panel for a field Nova would not
    describe, then carry on with the remaining steps."""
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Пришлите значение поля")
        return
    data = await state.get_data()
    spec = data.get("promo_spec")
    step = data.get("promo_step")
    if not spec or step is None:
        await state.clear()
        await message.answer("Начните заново: «⚙️ Тариф»")
        return
    field = spec["fields"][step]
    value = int(raw) if raw.isdigit() else raw
    picked = data.get("promo_picked") or {}
    picked[field["attribute"]] = {"value": value, "label": f"{field['label']}: {raw}",
                                  "price": 0}
    await _promo_step(message.from_user.id, _msg_send(message), state,
                      spec, step + 1, picked)


@router.callback_query(F.data == "selenium:bump:menu")
async def bump_menu(callback: CallbackQuery, state: FSMContext,
                    api: YooMarketAPI) -> None:
    await state.clear()
    s = get_settings(callback.from_user.id)
    # Whether there is money for this is the first thing worth knowing here
    can_pay, _covers, money = await promo_afford(api, s)
    tail = f"\n\n{'💸' if not can_pay else '💰'} {money[0].upper()}{money[1:]}." if money else ""
    await callback.message.edit_text(_bump_text(s) + tail,
                                     reply_markup=_bump_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:toggle")
async def bump_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_bump"]["enabled"] = not s["auto_bump"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_bump_text(s), reply_markup=_bump_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:bump:set_interval")
async def bump_set_interval(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_bump", {}).get("interval_hours", 24)
    await state.set_state(SeleniumState.waiting_bump_interval)
    await callback.message.edit_text(
        f"⏱ <b>Интервал поднятия</b>\n\nТекущий: {cur} ч\n\nВведите количество часов (например: 6, 12, 24):",
        reply_markup=_cancel_kb("selenium:bump:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_bump_interval)
async def bump_save_interval(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if hours < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число часов, например: 24")
        return
    s = get_settings(message.from_user.id)
    s["auto_bump"]["interval_hours"] = hours
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(f"✅ Интервал поднятия: <b>{hours} ч</b>")
    await message.answer(_bump_text(s), reply_markup=_bump_kb(s))


@router.callback_query(F.data == "selenium:run:bump")
async def run_bump(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Ask first — promoting every listing spends money on each one."""
    s = get_settings(callback.from_user.id)
    if not promo_params(s):
        b = InlineKeyboardBuilder()
        b.button(text="⚙️ Выбрать тариф", callback_data="promo:setup")
        b.button(text="⬅️ Назад", callback_data="selenium:bump:menu")
        b.adjust(1)
        await callback.message.edit_text(
            "⚙️ <b>Сначала выберите тариф</b>\n\n"
            "«Премиум» требует услугу, срок и способ оплаты. Каждый срок стоит "
            "по-разному, поэтому я не подставляю их сам.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    price = promo_price(s)
    cap = promo_limit(s)
    label = (s.get("auto_bump", {}).get("promo") or {}).get("label") or "выбран"
    can_pay, covers, money = await promo_afford(api, s)

    if not can_pay:
        b = InlineKeyboardBuilder()
        b.button(text="⚙️ Сменить тариф", callback_data="promo:setup")
        b.button(text="⬅️ Назад", callback_data="selenium:bump:menu")
        b.adjust(1)
        await callback.message.edit_text(
            f"💸 <b>Не хватает денег на поднятие</b>\n\n{money}.\n\n"
            f"Пополните баланс магазина или выберите тариф подешевле — "
            f"день стоит меньше месяца.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, продвинуть все", callback_data="selenium:run:bump_ok")
    b.button(text="⚙️ Сменить тариф", callback_data="promo:setup")
    b.button(text="❌ Отмена", callback_data="selenium:bump:menu")
    b.adjust(1)
    await callback.message.edit_text(
        "⚠️ <b>Продвижение платное</b>\n\n"
        f"Тариф: <b>{label}</b>\n"
        + (f"Списывается <b>{price} ₽</b> за каждое объявление.\n" if price else "")
        + (f"{money[0].upper()}{money[1:]}.\n" if money else "")
        + (f"Потолок трат ограничит запуск {cap} объявлениями.\n" if cap else "")
        + "\nПродвинуть все объявления?",
        reply_markup=b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "selenium:run:bump_ok")
async def run_bump_confirmed(callback: CallbackQuery, api: YooMarketAPI) -> None:
    import asyncio

    from automation.panel import panel_bump_all_sync
    from storage import get_panel_creds

    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        await callback.answer("⚠️ Нужен вход в панель продавца", show_alert=True)
        return

    s = get_settings(uid)
    # Checked again here, not only on the confirmation screen: the balance can
    # have been spent between seeing the warning and pressing the button.
    can_pay, covers, money = await promo_afford(api, s)
    if not can_pay:
        await callback.answer("💸 Не хватает денег", show_alert=True)
        await callback.message.edit_text(
            f"💸 <b>Не хватает денег на поднятие</b>\n\n{money}.\n\n"
            + _bump_text(s), reply_markup=_bump_kb(s))
        return

    await callback.answer("⏳ Продвигаю объявления...", show_alert=False)
    await callback.message.edit_text("⏳ Продвигаю объявления через панель...")
    try:
        # Whichever runs out first — the daily ceiling or the balance — decides
        # how many listings this run pays for.
        caps = [c for c in (promo_limit(s), covers) if c]
        loop = asyncio.get_event_loop()
        count, msg = await asyncio.wait_for(
            loop.run_in_executor(
                None, panel_bump_all_sync, creds["cookies"], uid, True,
                promo_params(s), min(caps) if caps else 0),
            timeout=300,
        )
        s["auto_bump"]["last_bump_run"] = _time.time()
        spent = count * promo_price(s)
        if spent:
            msg += f"\n💸 Потрачено: <b>{spent} ₽</b>"
            try:
                _, left = await api.get_balance()
                msg += f"   ·   Осталось: <b>{left}</b>"
            except Exception:
                pass
        save_settings(uid, s)
        result_text = f"⬆️ <b>Продвижение завершено</b>\n\n{msg}"
    except asyncio.TimeoutError:
        result_text = "⏰ Панель не ответила вовремя"
    except Exception as e:
        logger.error("Manual bump error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    await callback.message.edit_text(
        result_text + "\n\n" + _bump_text(s), reply_markup=_bump_kb(s))


# ---------------------------------------------------------------------------
# Auto-restore
# ---------------------------------------------------------------------------

def _restore_text(s: dict, creds=None) -> str:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    last_run = _fmt_ts(ar.get("last_restore_run"))
    return "\n".join([
        "🔄 <b>Авто-восстановление</b>\n",
        f"Статус: {_st(on)}",
        f"Последний запуск: {last_run}",
        "",
        "Переактивирует проданные или истёкшие объявления через API.",
    ])


def _restore_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    on = s.get("auto_restore", {}).get("enabled", False)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:restore")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:restore:toggle")
    b.button(text="⬅️ К объявлениям", callback_data="menu:ads")
    b.adjust(2, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:restore:menu")
async def restore_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_restore_text(s), reply_markup=_restore_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:restore:toggle")
async def restore_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_restore"]["enabled"] = not s["auto_restore"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_restore_text(s), reply_markup=_restore_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:run:restore")
async def run_restore(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    await callback.answer("⏳ Восстанавливаю объявления...", show_alert=False)
    await callback.message.edit_text("⏳ Восстанавливаю объявления через API...")
    s = get_settings(callback.from_user.id)
    try:
        count, msg = await api.restore_all_ads()
        s["auto_restore"]["last_restore_run"] = _time.time()
        save_settings(callback.from_user.id, s)
        result_text = f"🔄 <b>Восстановление завершено</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual restore error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    await callback.message.edit_text(result_text + "\n\n" + _restore_text(s), reply_markup=_restore_kb(s))


# ---------------------------------------------------------------------------
# Auto-withdraw
# ---------------------------------------------------------------------------

def _withdraw_text(s: dict, creds=None) -> str:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    return "\n".join([
        "💸 <b>Авто-вывод баланса</b>\n",
        f"Статус: {_st(on)}",
        f"Мин. сумма: {min_amount} ₽",
        "",
        "Выводит баланс через API, когда он превышает порог.",
    ])


def _withdraw_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    aw = s.get("auto_withdraw", {})
    on = aw.get("enabled", False)
    min_amount = aw.get("min_amount", 500)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:withdraw")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:withdraw:toggle")
    b.button(text=f"💰 Порог: {min_amount} ₽", callback_data="selenium:withdraw:set_amount")
    b.button(text="⬅️ Назад", callback_data="auto:menu")
    b.adjust(2, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:withdraw:menu")
async def withdraw_menu(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_withdraw_text(s), reply_markup=_withdraw_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:toggle")
async def withdraw_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_withdraw"]["enabled"] = not s["auto_withdraw"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_withdraw_text(s), reply_markup=_withdraw_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:withdraw:set_amount")
async def withdraw_set_amount(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get("auto_withdraw", {}).get("min_amount", 500)
    await state.set_state(SeleniumState.waiting_withdraw_amount)
    await callback.message.edit_text(
        f"💰 <b>Минимальная сумма для вывода</b>\n\nТекущая: {cur} ₽\n\nВведите новую сумму (₽):",
        reply_markup=_cancel_kb("selenium:withdraw:menu"),
    )
    await callback.answer()


@router.message(SeleniumState.waiting_withdraw_amount)
async def withdraw_save_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = int((message.text or "").strip())
        if amount < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число, например: 500")
        return
    s = get_settings(message.from_user.id)
    s["auto_withdraw"]["min_amount"] = amount
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(f"✅ Мин. сумма вывода: <b>{amount} ₽</b>")
    await message.answer(_withdraw_text(s), reply_markup=_withdraw_kb(s))


@router.callback_query(F.data == "selenium:run:withdraw")
async def run_withdraw(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    s = get_settings(callback.from_user.id)
    min_amount = s.get("auto_withdraw", {}).get("min_amount", 500)
    await callback.answer("⏳ Запускаю вывод...", show_alert=False)
    await callback.message.edit_text("⏳ Проверяю баланс и выполняю вывод через API...")
    try:
        success, msg = await api.withdraw_balance(min_amount)
        result_text = f"💸 <b>Авто-вывод</b>\n\n{msg}"
    except Exception as e:
        logger.error("Manual withdraw error: %s", e)
        result_text = f"❌ Ошибка: {e}"
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(result_text + "\n\n" + _withdraw_text(s), reply_markup=_withdraw_kb(s))
