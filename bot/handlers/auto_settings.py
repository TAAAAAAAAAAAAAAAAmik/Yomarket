from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ui

from storage import get_settings, save_settings

router = Router()


class AutoState(StatesGroup):
    waiting_reply_msg = State()
    waiting_confirmed_msg = State()
    waiting_refunded_msg = State()
    waiting_rule_keyword = State()
    waiting_rule_message = State()
    waiting_confirm_hours = State()
    waiting_refund_hours = State()
    waiting_refund_cap = State()
    waiting_bump_times = State()
    waiting_bump_price = State()
    waiting_bump_ceiling = State()


def _st(on: bool) -> str:
    return "🟢 ВКЛ" if on else "🔴 ВЫКЛ"


def _auto_text(s: dict) -> str:
    ar = s.get("auto_reply", {})
    ae = s.get("auto_events", {})

    lines = ["⚙️ <b>Авто-функции</b>\n"]

    # Ответы на сообщения покупателя — то, ради чего продавцы включают бота
    # ночью; показываем первыми и с числом правил, чтобы включённый, но пустой
    # автоответчик не выглядел работающим.
    conf = s.get("autoreplies", {})
    ans_on = conf.get("enabled", False)
    live = [r for r in (conf.get("rules") or []) if r.get("on", True)]
    lines.append(f"💬 <b>Ответы на сообщения</b> — {_st(ans_on)}")
    if ans_on:
        if live or (conf.get("fallback") or {}).get("on"):
            lines.append(f"   Правил: <b>{len(live)}</b>")
        else:
            lines.append("   ⚠️ <i>правил нет — бот молчит</i>")

    on = ar.get("enabled", False)
    lines.append(f"\n📩 <b>Автоответчик (новый заказ)</b> — {_st(on)}")
    if on:
        lines.append(f'   <i>"{ar.get("message","")[:50]}"</i>')

    ev_ref = ae.get("on_refunded", {})
    on3 = ev_ref.get("enabled", False)
    lines.append(f"\n↩️ <b>Автоответ при возврате</b> — {_st(on3)}")
    if on3:
        lines.append(f'   <i>"{ev_ref.get("message","")[:50]}"</i>')

    rst_on = s.get("auto_restore", {}).get("enabled", False)
    lines.append(f"\n🔄 <b>Авто-восстановление товаров</b> — {_st(rst_on)}")
    bump_on = s.get("auto_bump", {}).get("enabled", False)
    lines.append(f"⭐ <b>Премиум продвижение</b> — {_st(bump_on)}")

    ac = s.get("auto_confirm", {})
    ac_on = ac.get("enabled", False)
    ac_h = ac.get("hours", 24)
    lines.append(f"\n✅ <b>Авто-подтверждение заказов</b> — {_st(ac_on)}")
    if ac_on:
        lines.append(f"   Через <b>{ac_h} ч</b> после взятия в работу")

    rf = s.get("auto_refund", {})
    rf_on = rf.get("enabled", False)
    lines.append(f"\n↩️ <b>Автовозврат зависших</b> — {_st(rf_on)}")
    if rf_on:
        lines.append(f"   Через <b>{rf.get('hours', 48)} ч</b>, не больше "
                     f"<b>{rf.get('max_per_day', 3)}</b> в сутки")
        lines.append("   " + ("<b>любой</b> застрявший заказ"
                              if rf.get("scope") == "any"
                              else "только те, что ждут ник для звёзд"))

    bs = s.get("bump_schedule", {})
    bs_on = bs.get("enabled", False)
    bs_times = bs.get("times", [])
    lines.append(f"\n⏰ <b>Планировщик поднятия</b> — {_st(bs_on)}")
    if bs_on and bs_times:
        lines.append(f"   Время: <b>{', '.join(bs_times)}</b>")
    elif bs_on:
        lines.append("   Время не задано")

    return "\n".join(lines)


def _auto_keyboard(s: dict) -> InlineKeyboardMarkup:
    ar = s.get("auto_reply", {})
    ae = s.get("auto_events", {})
    builder = InlineKeyboardBuilder()

    builder.button(text="⚡ Автопилот (всё сразу)", callback_data="ap:menu")
    builder.button(text="📩 Автоответы", callback_data="ar:menu")

    ar_on = ar.get("enabled", False)
    builder.button(text=f"{'🔴 Выкл' if ar_on else '🟢 Вкл'} новый заказ", callback_data="auto:toggle:reply")
    if ar_on:
        builder.button(text="✏️ Текст нового заказа", callback_data="auto:set:reply_msg")

    ref_on = ae.get("on_refunded", {}).get("enabled", False)
    builder.button(text=f"{'🔴 Выкл' if ref_on else '🟢 Вкл'} автовозврат", callback_data="auto:toggle:refunded")
    if ref_on:
        builder.button(text="✏️ Текст возврата", callback_data="auto:set:refunded_msg")

    # Ad automation (premium promotion, auto-restore) now lives in «Объявления»

    aa = s.get("auto_accept", {})
    aa_on = aa.get("enabled", False)
    builder.button(
        text=f"{'🔴 Выкл' if aa_on else '🟢 Вкл'} авто-принятие заказов",
        callback_data="auto:toggle:accept",
    )

    ac = s.get("auto_confirm", {})
    ac_on = ac.get("enabled", False)
    ac_h = ac.get("hours", 24)
    builder.button(
        text=f"{'🔴 Выкл' if ac_on else '🟢 Вкл'} авто-подтверждение ({ac_h} ч)",
        callback_data="auto:toggle:confirm",
    )
    if ac_on:
        builder.button(text="⏱ Время подтверждения", callback_data="auto:set:confirm_hours")

    rf = s.get("auto_refund", {})
    rf_on = rf.get("enabled", False)
    builder.button(
        text=f"{'🔴 Выкл' if rf_on else '🟢 Вкл'} автовозврат зависших "
             f"({rf.get('hours', 48)} ч)",
        callback_data="auto:toggle:refund",
    )
    if rf_on:
        builder.button(text="⏱ Через сколько возвращать",
                       callback_data="auto:set:refund_hours")
        builder.button(
            text=("🎯 Какие: любой застрявший" if rf.get("scope") == "any"
                  else "🎯 Какие: только ждущие ник"),
            callback_data="auto:set:refund_scope")
        builder.button(text=f"⛔ Не больше {rf.get('max_per_day', 3)} в сутки",
                       callback_data="auto:set:refund_cap")

    bs = s.get("bump_schedule", {})
    bs_on = bs.get("enabled", False)
    bs_times = bs.get("times", [])
    times_str = ", ".join(bs_times) if bs_times else "не задано"
    builder.button(
        text=f"{'🔴 Выкл' if bs_on else '🟢 Вкл'} планировщик поднятия",
        callback_data="auto:toggle:bump_schedule",
    )
    if bs_on:
        builder.button(text=f"⏰ Время: {times_str}", callback_data="auto:set:bump_times")
        ppb = bs.get("price_per_bump", 0)
        ceil = bs.get("daily_ceiling", 0)
        builder.button(text=f"💵 Цена поднятия: {ppb} ₽", callback_data="auto:set:bump_price")
        ceil_str = f"{ceil} ₽/день" if ceil else "без лимита"
        builder.button(text=f"⛔ Потолок трат: {ceil_str}", callback_data="auto:set:bump_ceiling")

    builder.button(text="⬅️ Настройки", callback_data="settings:menu")
    ui.lay(builder)
    return builder.as_markup()


async def _refresh(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_auto_text(s), reply_markup=_auto_keyboard(s))
    await callback.answer()


@router.callback_query(F.data == "auto:menu")
async def auto_menu(callback: CallbackQuery) -> None:
    await _refresh(callback)


@router.callback_query(F.data == "auto:toggle:reply")
async def toggle_reply(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_reply"]["enabled"] = not s["auto_reply"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:toggle:confirmed")
async def toggle_confirmed(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_events"]["on_confirmed"]["enabled"] = not s["auto_events"]["on_confirmed"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:toggle:refunded")
async def toggle_refunded(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s["auto_events"]["on_refunded"]["enabled"] = not s["auto_events"]["on_refunded"].get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:toggle:restore")
async def toggle_restore(callback: CallbackQuery) -> None:
    # Redirect to the restore menu (runs over the Integration API)
    from handlers.selenium_settings import _restore_text, _restore_kb
    from storage import get_panel_creds
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    s = get_settings(uid)
    await callback.message.edit_text(_restore_text(s, creds), reply_markup=_restore_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "auto:toggle:bump")
async def toggle_bump(callback: CallbackQuery) -> None:
    # Redirect to the bump menu (runs over the Integration API)
    from handlers.selenium_settings import _bump_text, _bump_kb
    from storage import get_panel_creds
    uid = callback.from_user.id
    creds = get_panel_creds(uid)
    s = get_settings(uid)
    await callback.message.edit_text(_bump_text(s, creds), reply_markup=_bump_kb(s, creds))
    await callback.answer()


@router.callback_query(F.data == "auto:set:reply_msg")
async def set_reply_msg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_reply_msg)
    cur = get_settings(callback.from_user.id)["auto_reply"].get("message", "")
    await callback.message.edit_text(
        f"✏️ Текст ответа на <b>новый заказ</b>:\nТекущий: <i>{cur}</i>\n\nВведите новый:",
        reply_markup=_cancel_kb()
    )
    await callback.answer()


@router.message(AutoState.waiting_reply_msg)
async def save_reply_msg(message: Message, state: FSMContext) -> None:
    s = get_settings(message.from_user.id)
    s["auto_reply"]["message"] = message.text or ""
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(f"✅ Сохранено")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:set:confirmed_msg")
async def set_confirmed_msg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_confirmed_msg)
    cur = get_settings(callback.from_user.id)["auto_events"]["on_confirmed"].get("message", "")
    await callback.message.edit_text(
        f"✏️ Текст ответа при <b>подтверждении заказа</b>:\nТекущий: <i>{cur}</i>\n\nВведите новый:",
        reply_markup=_cancel_kb()
    )
    await callback.answer()


@router.message(AutoState.waiting_confirmed_msg)
async def save_confirmed_msg(message: Message, state: FSMContext) -> None:
    s = get_settings(message.from_user.id)
    s["auto_events"]["on_confirmed"]["message"] = message.text or ""
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer("✅ Сохранено")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:set:refunded_msg")
async def set_refunded_msg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_refunded_msg)
    cur = get_settings(callback.from_user.id)["auto_events"]["on_refunded"].get("message", "")
    await callback.message.edit_text(
        f"✏️ Текст ответа при <b>возврате</b>:\nТекущий: <i>{cur}</i>\n\nВведите новый:",
        reply_markup=_cancel_kb()
    )
    await callback.answer()


@router.message(AutoState.waiting_refunded_msg)
async def save_refunded_msg(message: Message, state: FSMContext) -> None:
    s = get_settings(message.from_user.id)
    s["auto_events"]["on_refunded"]["message"] = message.text or ""
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer("✅ Сохранено")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:rule:add")
async def add_rule_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_rule_keyword)
    await callback.message.edit_text(
        "🎮 <b>Правило по игре/категории</b>\n\n"
        "Шаг 1/2: Введите ключевое слово (название игры или категории):\n"
        "Пример: <code>Roblox</code>, <code>Minecraft</code>, <code>Steam Gift</code>",
        reply_markup=_cancel_kb()
    )
    await callback.answer()


@router.message(AutoState.waiting_rule_keyword)
async def rule_keyword(message: Message, state: FSMContext) -> None:
    await state.update_data(keyword=message.text or "")
    await state.set_state(AutoState.waiting_rule_message)
    await message.answer(
        f"🎮 Правило для: <b>{message.text}</b>\n\n"
        f"Шаг 2/2: Введите текст авто-ответа для этой категории:",
        reply_markup=_cancel_kb()
    )


@router.message(AutoState.waiting_rule_message)
async def rule_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    keyword = data.get("keyword", "")
    await state.clear()
    s = get_settings(message.from_user.id)
    rules = s.get("auto_rules", [])
    rules.append({"keyword": keyword, "message": message.text or ""})
    s["auto_rules"] = rules
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Правило добавлено:\n[{keyword}] → {message.text}")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:rule:del")
async def del_last_rule(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    rules = s.get("auto_rules", [])
    if rules:
        removed = rules.pop()
        s["auto_rules"] = rules
        save_settings(callback.from_user.id, s)
        await callback.answer(f"🗑 Удалено: [{removed.get('keyword','')}]", show_alert=True)
    else:
        await callback.answer("Правил нет", show_alert=True)
    await _refresh(callback)


# ---------------------------------------------------------------------------
# Auto-confirm
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "auto:toggle:accept")
async def toggle_accept(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    aa = s.setdefault("auto_accept", {"enabled": False})
    aa["enabled"] = not aa.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:toggle:confirm")
async def toggle_confirm(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    ac = s.setdefault("auto_confirm", {"enabled": False, "hours": 24})
    ac["enabled"] = not ac.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:set:confirm_hours")
async def set_confirm_hours(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_confirm_hours)
    cur = get_settings(callback.from_user.id).get("auto_confirm", {}).get("hours", 24)
    await callback.message.edit_text(
        f"⏱ Через сколько часов подтверждать заказ?\n\nТекущее: <b>{cur} ч</b>\n\nВведите число (1–72):",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(AutoState.waiting_confirm_hours)
async def save_confirm_hours(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if not 1 <= hours <= 72:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 1 до 72")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("auto_confirm", {})["hours"] = hours
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Подтверждение через <b>{hours} ч</b>")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


# ---------------------------------------------------------------------------
# Автовозврат зависших заказов
#
# Единственная автоматика в боте, которая ОТДАЁТ деньги. Экран поэтому
# устроен не как остальные: включение осторожного режима — обычная кнопка,
# а переход на «любой заказ» проходит через отдельное предупреждение.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "auto:toggle:refund")
async def toggle_refund(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    rf = s.setdefault("auto_refund", {})
    turning_on = not rf.get("enabled", False)
    rf["enabled"] = turning_on
    rf.setdefault("hours", 48)
    rf.setdefault("scope", "stars")
    rf.setdefault("max_per_day", 3)
    save_settings(callback.from_user.id, s)
    if turning_on:
        # Один раз, при включении, — что именно теперь будет происходить с
        # деньгами. Тумблер без этого выглядит как остальные, а он не такой.
        await callback.answer(
            f"Возврат через {rf['hours']} ч, не больше {rf['max_per_day']} "
            f"в сутки. Пока — только заказы, ждущие ник для звёзд.",
            show_alert=True)
    await _refresh(callback)


@router.callback_query(F.data == "auto:set:refund_scope")
async def set_refund_scope(callback: CallbackQuery) -> None:
    """Переключение охвата — через предупреждение, а не одним нажатием.

    «Любой застрявший заказ» означает возврат и за товар, выданный вручную:
    бот о такой выдаче не знает ничего. Продавец теряет и товар, и деньги, а
    узнаёт из отчёта. Такое решение принимают осознанно.
    """
    s = get_settings(callback.from_user.id)
    rf = s.setdefault("auto_refund", {})
    if rf.get("scope") == "any":
        rf["scope"] = "stars"
        save_settings(callback.from_user.id, s)
        await callback.answer("Теперь только заказы, ждущие ник для звёзд",
                              show_alert=True)
        await _refresh(callback)
        return
    b = InlineKeyboardBuilder()
    b.button(text="⚠️ Да, возвращать любой", callback_data="auto:refund_any")
    b.button(text="⬅️ Оставить как есть", callback_data="auto:menu")
    b.adjust(1)
    await callback.message.edit_text(
        "⚠️ <b>Возвращать за любой застрявший заказ?</b>\n\n"
        "Сейчас бот возвращает деньги только за заказы, которые сам не смог "
        "выдать: покупатель не прислал ник для звёзд. Про такой заказ точно "
        "известно, что товар не ушёл.\n\n"
        "Про остальные бот <b>не знает, выдали вы товар или нет</b>. Если "
        "выдали вручную и забыли подтвердить — возврат отдаст покупателю и "
        "товар, и деньги.\n\n"
        "Включайте, если выдаёте только через бота.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "auto:refund_any")
async def set_refund_any(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    s.setdefault("auto_refund", {})["scope"] = "any"
    save_settings(callback.from_user.id, s)
    await callback.answer("Возвращаю за любой застрявший заказ", show_alert=True)
    await _refresh(callback)


@router.callback_query(F.data == "auto:set:refund_hours")
async def set_refund_hours(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_refund_hours)
    cur = get_settings(callback.from_user.id).get("auto_refund", {}).get("hours", 48)
    await callback.message.edit_text(
        f"⏱ Через сколько часов возвращать деньги за зависший заказ?\n\n"
        f"Текущее: <b>{cur} ч</b>\n\n"
        f"Введите число (1–168). Меньше суток ставить не советую: покупатель "
        f"может просто спать.",
        reply_markup=_cancel_kb())
    await callback.answer()


@router.message(AutoState.waiting_refund_hours)
async def save_refund_hours(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        if not 1 <= hours <= 168:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 1 до 168")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("auto_refund", {})["hours"] = hours
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Возврат через <b>{hours} ч</b>")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:set:refund_cap")
async def set_refund_cap(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_refund_cap)
    cur = get_settings(callback.from_user.id).get("auto_refund", {}).get("max_per_day", 3)
    await callback.message.edit_text(
        f"⛔ Сколько возвратов в сутки бот может сделать сам?\n\n"
        f"Сейчас: <b>{cur}</b>\n\n"
        f"Введите число (1–50). Потолок нужен на случай, если что-то пойдёт "
        f"не так: без него одна ошибка вернёт деньги по всем заказам разом.",
        reply_markup=_cancel_kb())
    await callback.answer()


@router.message(AutoState.waiting_refund_cap)
async def save_refund_cap(message: Message, state: FSMContext) -> None:
    try:
        cap = int((message.text or "").strip())
        if not 1 <= cap <= 50:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 1 до 50")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("auto_refund", {})["max_per_day"] = cap
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Не больше <b>{cap}</b> возвратов в сутки")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


# ---------------------------------------------------------------------------
# Bump schedule
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "auto:toggle:bump_schedule")
async def toggle_bump_schedule(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    bs = s.setdefault("bump_schedule", {"enabled": False, "times": [], "last_runs": {}})
    bs["enabled"] = not bs.get("enabled", False)
    save_settings(callback.from_user.id, s)
    await _refresh(callback)


@router.callback_query(F.data == "auto:set:bump_times")
async def set_bump_times(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_bump_times)
    s = get_settings(callback.from_user.id)
    cur = ", ".join(s.get("bump_schedule", {}).get("times", []))
    await callback.message.edit_text(
        f"⏰ <b>Время автоподнятия</b>\n\nТекущее: <b>{cur or 'не задано'}</b>\n\n"
        "Введите время через запятую (формат ЧЧ:ММ):\n"
        "Пример: <code>09:00, 15:00, 21:00</code>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(AutoState.waiting_bump_times)
async def save_bump_times(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    import re
    times = [t.strip() for t in raw.replace(";", ",").split(",")]
    valid = []
    for t in times:
        if re.match(r"^\d{1,2}:\d{2}$", t):
            h, m = map(int, t.split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid.append(f"{h:02d}:{m:02d}")
    if not valid:
        await message.answer("❌ Введите время в формате ЧЧ:ММ, например: <code>09:00, 21:00</code>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("bump_schedule", {})["times"] = valid
    s["bump_schedule"]["last_runs"] = {}
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Время поднятия: <b>{', '.join(valid)}</b>")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:set:bump_price")
async def set_bump_price(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_bump_price)
    cur = get_settings(callback.from_user.id).get("bump_schedule", {}).get("price_per_bump", 0)
    await callback.message.edit_text(
        f"💵 <b>Цена одного поднятия</b>\n\nТекущая: <b>{cur} ₽</b>\n\n"
        "Введите стоимость поднятия в ₽ (0 — бесплатно). "
        "Нужно для учёта потолка трат.",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(AutoState.waiting_bump_price)
async def save_bump_price(message: Message, state: FSMContext) -> None:
    try:
        price = float((message.text or "").strip().replace(",", ".").replace(" ", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число, например: <b>5</b>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("bump_schedule", {})["price_per_bump"] = price
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Цена поднятия: <b>{price:g} ₽</b>")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


@router.callback_query(F.data == "auto:set:bump_ceiling")
async def set_bump_ceiling(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AutoState.waiting_bump_ceiling)
    cur = get_settings(callback.from_user.id).get("bump_schedule", {}).get("daily_ceiling", 0)
    await callback.message.edit_text(
        f"⛔ <b>Потолок трат на поднятия</b>\n\nТекущий: <b>{cur or 'без лимита'} ₽/день</b>\n\n"
        "Введите максимум ₽ в день на поднятия (0 — без лимита). "
        "При достижении бот перестанет поднимать до следующего дня.",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(AutoState.waiting_bump_ceiling)
async def save_bump_ceiling(message: Message, state: FSMContext) -> None:
    try:
        ceil = float((message.text or "").strip().replace(",", ".").replace(" ", ""))
        if ceil < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число, например: <b>100</b>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("bump_schedule", {})["daily_ceiling"] = ceil
    save_settings(message.from_user.id, s)
    await message.answer(f"✅ Потолок: <b>{ceil:g} ₽/день</b>" if ceil else "✅ Лимит снят")
    await message.answer(_auto_text(s), reply_markup=_auto_keyboard(s))


