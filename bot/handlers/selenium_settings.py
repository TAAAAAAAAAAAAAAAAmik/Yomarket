"""Auto-bump, auto-restore, auto-withdraw via YooMarket Integration API."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
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
    waiting_restore_interval = State()
    waiting_pos_url = State()
    waiting_pos_threshold = State()
    waiting_pos_interval = State()
    waiting_pos_price = State()
    waiting_pos_guard = State()
    waiting_pos_cooldown = State()
    waiting_pos_limit = State()
    waiting_sched_times = State()
    waiting_sched_ceiling = State()


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


def promo_only_ids(s: dict) -> list:
    """Listings the seller picked for promotion. Empty = every listing."""
    return list((s.get("auto_bump", {}) or {}).get("only_items") or [])


def promo_limit(s: dict) -> int:
    """How many listings the daily ceiling allows at the chosen tariff.
    0 = no ceiling set, so no cap."""
    ceiling = s.get("bump_stats", {}).get("daily_ceiling", 0)
    price = promo_price(s)
    if not ceiling or not price:
        return 0
    return max(0, int(ceiling // price))


async def promo_afford(api, s: dict) -> tuple[bool, int, str]:
    """What a promotion will cost, and what the shop balance is.

    Returns (ok_to_run, listings_covered, human_text). Note what this does NOT
    do: block the run when the balance is low. Yoomarket does not let «Премиум»
    be paid from the shop balance — the panel's «Оплата» field offers only СБП,
    cards, crypto and the like — so refusing on the balance would be refusing on
    the wrong pot of money. The balance is shown because it is worth knowing;
    whether the payment goes through is decided on the payment page.
    """
    price = promo_price(s)
    if not api:
        return True, 0, ""
    try:
        _balance, shown = await api.get_balance()
    except Exception:
        return True, 0, ""
    if shown == "—":
        return True, 0, ""
    if not price:
        return True, 0, f"баланс магазина: {shown}"
    return True, 0, (f"одно поднятие — <b>{price} ₽</b>, оплата отдельно "
                     f"(баланс магазина {shown} для этого не используется)")


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
    picked = promo_only_ids(s)
    price = promo_price(s)
    if picked:
        scope = f"Товары: <b>выбрано {len(picked)}</b>"
        if price:
            scope += f"  ·  за прогон ≈ {len(picked) * price} ₽"
    else:
        scope = "Товары: <b>все</b> — платится за каждый"
    return "\n".join([
        "⭐ <b>Премиум продвижение</b>\n",
        f"Статус: {_st(on)}",
        f"Интервал: каждые {interval} ч",
        f"Последний запуск: {last_run}",
        f"Потолок трат: {limit}",
        scope,
        tariff_line,
        "",
        "⚠️ <b>Платно.</b> На Юмаркете поднятие — это «Премиум». Оплата "
        "идёт не с баланса магазина, а выбранным способом (СБП, карта, "
        "криптовалюта…) — бот пришлёт ссылку на оплату по каждому "
        "объявлению.",
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
    picked = promo_only_ids(s)
    b.button(text=f"🎯 Товары: {f'выбрано {len(picked)}' if picked else 'все'}",
             callback_data="promo:items")
    bs = s.get("bump_schedule", {})
    times = bs.get("times") or []
    b.button(text=f"🕐 Авто-продвижение: "
                  f"{('по времени · ' + ', '.join(times[:3])) if (bs.get('enabled') and times) else 'выкл'}",
             callback_data="sched:menu")
    pp = s.get("promo_position", {})
    n_watch = len(pp.get("watches") or ([1] if pp.get("url") else []))
    b.button(text=f"📍 По позиции: "
                  f"{f'следит за {n_watch}' if pp.get('enabled') and n_watch else 'выкл'}",
             callback_data="pos:menu")
    b.button(text=f"⏱ Интервал: {interval} ч", callback_data="selenium:bump:set_interval")
    b.button(text="⬅️ К объявлениям", callback_data="menu:ads")
    b.adjust(2, 1, 1, 1, 1, 1, 1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Scheduled promotion — the same slots every day
#
# The scheduler already fires `bump_schedule` from the fast loop; this is the
# screen that sets it, next to the promotion it drives rather than in a
# different menu.
# ---------------------------------------------------------------------------

def _sched_text(s: dict) -> str:
    bs = s.get("bump_schedule", {})
    times = bs.get("times") or []
    price = promo_price(s) or float(bs.get("price_per_bump", 0) or 0)
    ceiling = float(bs.get("daily_ceiling", 0) or 0)
    spent = float(bs.get("spent_today", 0) or 0) if (
        bs.get("spent_day") == datetime.now().strftime("%Y-%m-%d")) else 0.0
    lines = [
        "🕐 <b>Авто-продвижение по времени</b>\n",
        f"Статус: {_st(bs.get('enabled', False))}",
        f"Время запуска: <b>{', '.join(times) if times else 'не задано'}</b>",
    ]
    picked = promo_only_ids(s)
    lines.append(f"Товары: <b>{f'выбрано {len(picked)}' if picked else 'все'}</b>")
    if price and times:
        per_run = (len(picked) or 0) * price
        lines.append(f"Цена поднятия: <b>{price:g} ₽</b>"
                     + (f"  ·  за прогон ≈ {per_run:g} ₽" if per_run else ""))
    lines.append(f"Потолок в день: "
                 f"<b>{f'{ceiling:.0f} ₽' if ceiling else 'не задан'}</b>"
                 + (f"  ·  сегодня {spent:.0f} ₽" if spent else ""))
    if bs.get("bumps_total"):
        lines.append(f"Всего поднятий: <b>{bs['bumps_total']}</b>  ·  "
                     f"потрачено {float(bs.get('spent_total', 0) or 0):.0f} ₽")
    lines += [
        "",
        "Запускает продвижение каждый день в указанные часы — без вашего "
        "участия.",
        "<i>Время по серверу. Слот отрабатывает один раз в сутки; если бот был "
        "выключен дольше 35 минут после слота, он пропускается, а не "
        "срабатывает задним числом.</i>",
    ]
    if bs.get("enabled") and not promo_params(s):
        lines.append("⚠️ <b>Не выбран тариф</b> — расписание не сможет поднять.")
    return "\n".join(lines)


def _sched_kb(s: dict) -> InlineKeyboardMarkup:
    bs = s.get("bump_schedule", {})
    on = bs.get("enabled", False)
    b = InlineKeyboardBuilder()
    b.button(text="🔴 Выключить" if on else "🟢 Включить",
             callback_data="sched:toggle")
    b.button(text="🕐 Задать время", callback_data="sched:times")
    b.button(text="💰 Потолок в день", callback_data="sched:ceiling")
    if bs.get("times"):
        b.button(text="🧹 Убрать расписание", callback_data="sched:clear")
    b.button(text="⬅️ К продвижению", callback_data="selenium:bump:menu")
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "sched:menu")
async def sched_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_sched_text(s), reply_markup=_sched_kb(s))
    await callback.answer()


@router.callback_query(F.data == "sched:toggle")
async def sched_toggle(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    bs = s.setdefault("bump_schedule", {"enabled": False, "times": [],
                                        "last_runs": {}})
    turning_on = not bs.get("enabled", False)
    if turning_on and not (bs.get("times") or []):
        await callback.answer("Сначала задайте время", show_alert=True)
        return
    if turning_on and not promo_params(s):
        await callback.answer(
            "Сначала выберите тариф «Премиум» — иначе поднять не получится",
            show_alert=True)
        return
    bs["enabled"] = turning_on
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_sched_text(s), reply_markup=_sched_kb(s))
    await callback.answer("Включено" if turning_on else "Выключено")


@router.callback_query(F.data == "sched:times")
async def sched_times_start(callback: CallbackQuery, state: FSMContext) -> None:
    cur = ", ".join(get_settings(callback.from_user.id)
                    .get("bump_schedule", {}).get("times") or [])
    await state.set_state(SeleniumState.waiting_sched_times)
    await callback.message.edit_text(
        f"🕐 <b>Время авто-продвижения</b>\n\n"
        f"Сейчас: {cur or 'не задано'}\n\n"
        f"Пришлите время через запятую, например:\n"
        f"<code>09:00, 15:00, 21:00</code>\n\n"
        f"<i>Каждый день в эти часы бот поднимет выбранные товары.</i>",
        reply_markup=_cancel_kb("sched:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_sched_times)
async def sched_times_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(";", ",")
    valid, bad = [], []
    for part in raw.split(","):
        t = part.strip()
        if not t:
            continue
        # Accept 9:00 and 09.00 alike, then store one canonical HH:MM — the
        # scheduler compares slots as strings.
        t = t.replace(".", ":")
        try:
            hh, mm = t.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            valid.append(f"{h:02d}:{m:02d}")
        except ValueError:
            bad.append(t)
    if not valid:
        await message.answer("❌ Не разобрал время. Пример: <code>09:00, 21:00</code>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    bs = s.setdefault("bump_schedule", {})
    bs["times"] = sorted(set(valid))
    # Slots already marked as run today would otherwise stay marked under the
    # new schedule and skip their first day.
    bs["last_runs"] = {}
    save_settings(message.from_user.id, s)
    note = f"\n⚠️ Не понял: {', '.join(bad)}" if bad else ""
    await message.answer(f"✅ Время: <b>{', '.join(bs['times'])}</b>{note}")
    await message.answer(_sched_text(s), reply_markup=_sched_kb(s))


@router.callback_query(F.data == "sched:clear")
async def sched_clear(callback: CallbackQuery) -> None:
    s = get_settings(callback.from_user.id)
    bs = s.setdefault("bump_schedule", {})
    bs["times"] = []
    bs["last_runs"] = {}
    bs["enabled"] = False           # a schedule with no slots is not a schedule
    save_settings(callback.from_user.id, s)
    await callback.answer("Расписание убрано", show_alert=True)
    await callback.message.edit_text(_sched_text(s), reply_markup=_sched_kb(s))


@router.callback_query(F.data == "sched:ceiling")
async def sched_ceiling_start(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get(
        "bump_schedule", {}).get("daily_ceiling", 0)
    await state.set_state(SeleniumState.waiting_sched_ceiling)
    await callback.message.edit_text(
        f"💰 <b>Потолок трат в день</b>\n\n"
        f"Сейчас: {f'{float(cur):.0f} ₽' if cur else 'не задан'}\n\n"
        f"Пришлите сумму в рублях. Когда за сутки потрачено больше — "
        f"расписание пропускает запуск.\n\n"
        f"<i>0 — без ограничения.</i>",
        reply_markup=_cancel_kb("sched:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_sched_ceiling)
async def sched_ceiling_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число, например: <b>300</b>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    bs = s.setdefault("bump_schedule", {})
    bs["daily_ceiling"] = amount
    # The ceiling is enforced against price_per_bump; keep it in step with the
    # tariff so the cap counts real money rather than a stale figure.
    if promo_price(s):
        bs["price_per_bump"] = promo_price(s)
    save_settings(message.from_user.id, s)
    await message.answer(_sched_text(s), reply_markup=_sched_kb(s))


# ---------------------------------------------------------------------------
# Positional trigger: promote when a listing slips down the offers list
#
# Position is not in the seller API — it exists only on the public storefront,
# so the page a buyer sees is read and our row located in it. One watch per
# listing: a shop with thirty items does not have one position, it has thirty,
# and a single page could never speak for all of them.
#
# The rules themselves live in automation/position.py, away from Telegram, and
# are covered by bot/tests/test_position.py. This module is the screen.
# ---------------------------------------------------------------------------

def _pos_settings(uid: int) -> tuple[dict, dict, list]:
    """(settings, promo_position, watches) with the legacy config migrated."""
    from automation.position import watches as _watches
    s = get_settings(uid)
    pp = s.setdefault("promo_position", {})
    return s, pp, _watches(pp)


def _watch_label(w: dict, idx: int) -> str:
    name = (w.get("title") or "").strip() or _short_url(w.get("url", ""))
    pos = int(w.get("last_pos") or 0)
    where = f"{pos} место" if pos else "—"
    return f"{idx + 1}. {name[:22]} · {where}"


def _short_url(url: str) -> str:
    u = str(url or "").split("://")[-1]
    return (u[:34] + "…") if len(u) > 35 else u


def _pos_text(s: dict) -> str:
    from automation.position import daily_limit, cooldown_hours, watches
    pp = s.get("promo_position", {})
    ws = watches(pp)
    lines = [
        "📍 <b>Поднятие по позиции</b>\n",
        f"Статус: {_st(pp.get('enabled', False))}",
        f"Проверка: каждые {pp.get('interval_hours', 1)} ч",
        f"Товаров под наблюдением: <b>{len(ws)}</b>",
        f"Режим: {'⭐ поднимать автоматически' if pp.get('auto_promote') else '🔔 только предупреждать'}",
    ]
    if pp.get("auto_promote"):
        price = promo_price(s)
        lines.append(f"Пауза между поднятиями: {cooldown_hours(pp):.0f} ч")
        cap = daily_limit(pp)
        lines.append("Лимит в сутки на товар: "
                     + (f"<b>{cap}</b>" + (f" (до {cap * price} ₽)" if price else "")
                        if cap else "без лимита"))
        if not promo_params(s):
            lines.append("⚠️ <b>Тариф «Премиум» не выбран</b> — поднять не смогу")
    if ws:
        lines.append("")
        for i, w in enumerate(ws):
            thr = int(w.get("max_position") or 3)
            pos = int(w.get("last_pos") or 0)
            if not pos:
                mark = "⚪"
            elif pos <= thr:
                mark = "🟢"
            else:
                mark = "🔴"
            name = _esc((w.get("title") or "").strip() or _short_url(w.get("url", "")))
            row = f"{mark} <b>{name[:30]}</b> — "
            row += f"{pos} место" if pos else "ещё не проверял"
            row += f", порог {thr}"
            if w.get("alarmed"):
                row += " ⚠️ не нахожу на странице"
            lines.append(row)
    else:
        lines.append("")
        lines.append("Пока ничего не отслеживается. Добавьте товар: пришлите "
                     "адрес страницы, где виден <b>список предложений</b> — "
                     "именно по нему считается место.")
    lines.append("")
    lines.append("<i>Позиции нет в API — бот читает публичную витрину и ищет "
                 "в списке ваш магазин.</i>")
    return "\n".join(lines)


def _pos_kb(s: dict) -> InlineKeyboardMarkup:
    from automation.position import MAX_WATCHES, watches
    pp = s.get("promo_position", {})
    ws = watches(pp)
    on = pp.get("enabled", False)
    b = InlineKeyboardBuilder()
    for i, w in enumerate(ws[:MAX_WATCHES]):
        b.button(text=_watch_label(w, i), callback_data=f"pos:w:{i}")
    if len(ws) < MAX_WATCHES:
        b.button(text="➕ Добавить товар", callback_data="pos:add")
    b.button(text="🔴 Выключить" if on else "🟢 Включить", callback_data="pos:toggle")
    b.button(text=("⭐ Режим: поднимать" if pp.get("auto_promote")
                   else "🔔 Режим: сигнал"), callback_data="pos:auto")
    b.button(text=f"⏱ Проверка: {pp.get('interval_hours', 1)} ч",
             callback_data="pos:interval")
    b.button(text=("💰 Дешевле: сообщать" if pp.get("undercut_notify", True)
                   else "💰 Дешевле: молчать"), callback_data="pos:undercut")
    if pp.get("auto_promote"):
        b.button(text=f"⏸ Пауза: {pp.get('cooldown_hours', 6):.0f} ч",
                 callback_data="pos:cooldown")
        b.button(text=f"🧾 Лимит/сутки: {pp.get('daily_limit', 3) or '∞'}",
                 callback_data="pos:limit")
    if ws:
        b.button(text="🔍 Проверить все", callback_data="pos:checkall")
    b.button(text="⬅️ К продвижению", callback_data="selenium:bump:menu")
    # One watch per row so its name and place stay readable; the settings pair
    # up below them.
    rows = [1] * len(ws[:MAX_WATCHES])
    rows += [1] if len(ws) < MAX_WATCHES else []
    rows += [2, 2] + ([2] if pp.get("auto_promote") else [])
    rows += ([1] if ws else []) + [1]
    b.adjust(*rows)
    return b.as_markup()


async def _pos_screen(callback: CallbackQuery) -> None:
    s, _pp, _ws = _pos_settings(callback.from_user.id)
    save_settings(callback.from_user.id, s)      # persist a migration, if any
    await _pos_edit(callback.message, _pos_text(s), _pos_kb(s))


async def _pos_edit(message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit, and fall back to a fresh message when Telegram refuses.

    "message is not modified" comes back for a screen redrawn unchanged — the
    normal case for a check that found nothing new — and an unhandled one used
    to look like the button did nothing.
    """
    try:
        await message.edit_text(text[:4000], reply_markup=kb)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await message.answer(text[:4000], reply_markup=kb)
        except Exception:
            logger.warning("position screen: %s", e)


@router.callback_query(F.data == "pos:menu")
async def pos_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _pos_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "pos:toggle")
async def pos_toggle(callback: CallbackQuery) -> None:
    s, pp, ws = _pos_settings(callback.from_user.id)
    turning_on = not pp.get("enabled", False)
    if turning_on and not ws:
        await callback.answer("Сначала добавьте товар для наблюдения",
                              show_alert=True)
        return
    pp["enabled"] = turning_on
    if turning_on:
        for w in ws:
            w["last_check"] = 0          # check on the next tick, not in an hour
    save_settings(callback.from_user.id, s)
    await _pos_edit(callback.message, _pos_text(s), _pos_kb(s))
    await callback.answer("Включено" if turning_on else "Выключено")


@router.callback_query(F.data == "pos:auto")
async def pos_auto(callback: CallbackQuery) -> None:
    s, pp, _ws = _pos_settings(callback.from_user.id)
    pp["auto_promote"] = not pp.get("auto_promote", False)
    save_settings(callback.from_user.id, s)
    if pp["auto_promote"] and not promo_params(s):
        await callback.answer(
            "Включено, но тариф «Премиум» не выбран — поднять не смогу. "
            "Настройте тариф в «Премиум продвижении».", show_alert=True)
    else:
        await callback.answer(
            "Буду поднимать автоматически — каждое поднятие платное"
            if pp["auto_promote"] else "Только предупреждаю, деньги не трачу",
            show_alert=True)
    await _pos_edit(callback.message, _pos_text(s), _pos_kb(s))


@router.callback_query(F.data == "pos:undercut")
async def pos_undercut(callback: CallbackQuery) -> None:
    s, pp, _ws = _pos_settings(callback.from_user.id)
    pp["undercut_notify"] = not pp.get("undercut_notify", True)
    save_settings(callback.from_user.id, s)
    await _pos_edit(callback.message, _pos_text(s), _pos_kb(s))
    await callback.answer()


# --- adding a watch --------------------------------------------------------

@router.callback_query(F.data == "pos:add")
async def pos_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SeleniumState.waiting_pos_url)
    await callback.message.edit_text(
        "🔗 <b>Какую страницу смотреть</b>\n\n"
        "Откройте свой товар так, как его видит покупатель — на странице, где "
        "виден <b>список предложений</b> от разных продавцов — и пришлите её "
        "адрес.\n\n"
        "<i>Место считается именно по этому списку. Для каждого товара нужна "
        "своя страница.</i>",
        reply_markup=_cancel_kb("pos:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_pos_url)
async def pos_url_save(message: Message, state: FSMContext) -> None:
    from automation.position import MAX_WATCHES, new_watch, watches

    url = (message.text or "").strip()
    if not url.lower().startswith("http"):
        await message.answer("❌ Пришлите полный адрес, начиная с https://")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    pp = s.setdefault("promo_position", {})
    ws = watches(pp)
    if len(ws) >= MAX_WATCHES:
        await message.answer(f"❌ Больше {MAX_WATCHES} товаров не отслеживаю")
        return
    if any((w.get("url") or "").strip() == url for w in ws):
        await message.answer("ℹ️ Эта страница уже отслеживается",
                             reply_markup=_pos_kb(s))
        return
    ws.append(new_watch(url))
    pp["watches"] = ws
    save_settings(message.from_user.id, s)

    idx = len(ws) - 1
    note = await _pos_probe(message.from_user.id, idx, message)
    s = get_settings(message.from_user.id)
    await message.answer(_watch_text(s, idx, note), reply_markup=_watch_kb(s, idx))


async def _pos_probe(uid: int, idx: int, message=None) -> str:
    """Read the page once and try to recognise our own row.

    Done at the moment the page is added, because a watch that cannot find its
    listing is useless and the seller should learn that now — not from silence
    a week later. The matched title is remembered: it is what tells two
    listings of the same shop apart on later checks.
    """
    import asyncio

    from automation.market import fetch_listing, find_position
    from automation.position import watches
    from storage import get_shop_name

    s = get_settings(uid)
    ws = watches(s.setdefault("promo_position", {}))
    if idx >= len(ws):
        return ""
    w = ws[idx]
    shop = get_shop_name(uid) or ""
    if message:
        try:
            await message.answer("⏳ Открываю страницу...")
        except Exception:
            pass
    loop = asyncio.get_event_loop()
    try:
        ok, res = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_listing, w["url"], shop),
            timeout=120)
    except Exception as e:
        return f"⚠️ Страница не открылась: {_esc(str(e)[:120])}"
    if not ok:
        return f"⚠️ {_esc(str(res)[:200])}"

    offers = res["offers"]
    mine = find_position(offers, seller=shop) if shop else None
    if not mine:
        return (f"⚠️ Нашёл {len(offers)} предложений, но вашего магазина "
                f"«{_esc(shop) or '—'}» среди них нет. Проверьте адрес — "
                f"нужна страница, где ваш товар есть в списке.")
    w["title"] = mine["title"] or w.get("title") or ""
    # The storefront id, kept for finding this row again — NOT the panel id the
    # paid action needs. Those are different numbers, and binding the listing
    # in the panel is a separate, deliberate step.
    if mine.get("id"):
        w["market_id"] = str(mine["id"])
    w["last_pos"] = int(mine["pos"])
    save_settings(uid, s)
    return (f"✅ Нашёл вас на <b>{mine['pos']}-м месте</b> из {len(offers)}"
            + (f", цена {float(mine['price']):.0f} ₽" if mine.get("price") else ""))


# --- one watch -------------------------------------------------------------

def _watch_text(s: dict, idx: int, note: str = "") -> str:
    from automation.position import watches
    pp = s.get("promo_position", {})
    ws = watches(pp)
    if idx >= len(ws):
        return "Наблюдение удалено."
    w = ws[idx]
    thr = int(w.get("max_position") or 3)
    lines = [
        f"📍 <b>{_esc((w.get('title') or '').strip() or 'Товар без названия')}</b>\n",
        f"Страница: <code>{_esc(_short_url(w.get('url', '')))}</code>",
        f"🎯 Поднимать, если место ниже <b>{thr}</b>",
    ]
    pos = int(w.get("last_pos") or 0)
    when = _fmt_ts(w.get("last_check"))
    lines.append("Последняя проверка: "
                 + (f"<b>{pos} место</b>" + (f" · {when}" if when != "—" else "")
                    if pos else "ещё не проверял"))
    guard = float(w.get("undercut_guard") or 0)
    lines.append("⛔ Не поднимать, если конкурент дешевле больше чем на: "
                 + (f"<b>{guard:.0f} ₽</b>" if guard else "не ограничено"))
    floor = float(w.get("min_price") or 0)
    lines.append("⚠️ Сигнал, если цена на витрине упала ниже: "
                 + (f"<b>{floor:.0f} ₽</b>" if floor else "не задан"))
    if w.get("item_id"):
        lines.append(f"Товар в панели: <code>{_esc(w['item_id'])}</code>")
    else:
        lines.append("Товар в панели: <b>не привязан</b> — "
                     "поднимать нечего, привяжите")
    if w.get("promos_today"):
        lines.append(f"Поднятий сегодня: <b>{w['promos_today']}</b>")
    if w.get("alarmed"):
        lines.append("")
        lines.append("⚠️ Товар не находится на странице несколько проверок "
                     "подряд — проверьте адрес.")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _watch_kb(s: dict, idx: int) -> InlineKeyboardMarkup:
    from automation.position import watches
    ws = watches(s.get("promo_position", {}))
    b = InlineKeyboardBuilder()
    if idx >= len(ws):
        b.button(text="⬅️ Назад", callback_data="pos:menu")
        return b.as_markup()
    w = ws[idx]
    b.button(text=f"🎯 Порог места: {int(w.get('max_position') or 3)}",
             callback_data=f"pos:wt:{idx}")
    guard = float(w.get("undercut_guard") or 0)
    b.button(text=f"⛔ Порог цены: {f'{guard:.0f} ₽' if guard else 'нет'}",
             callback_data=f"pos:wg:{idx}")
    floor = float(w.get("min_price") or 0)
    b.button(text=f"⚠️ Сигнал цены: {f'{floor:.0f} ₽' if floor else 'нет'}",
             callback_data=f"pos:wp:{idx}")
    b.button(text=("🔗 Товар: привязан" if w.get("item_id") else "🔗 Привязать товар"),
             callback_data=f"pos:wi:{idx}")
    b.button(text="🔍 Проверить сейчас", callback_data=f"pos:wc:{idx}")
    b.button(text="🗑 Удалить", callback_data=f"pos:wd:{idx}")
    b.button(text="⬅️ К списку", callback_data="pos:menu")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


def _watch_at(uid: int, data: str) -> tuple[dict, dict, list, int] | None:
    """(settings, pp, watches, idx) for a `pos:xx:<idx>` callback, or None."""
    from automation.position import watches
    try:
        idx = int(str(data).split(":")[-1])
    except ValueError:
        return None
    s = get_settings(uid)
    pp = s.setdefault("promo_position", {})
    ws = watches(pp)
    if not 0 <= idx < len(ws):
        return None
    return s, pp, ws, idx


@router.callback_query(F.data.startswith("pos:w:"))
async def pos_watch_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        await _pos_screen(callback)
        return
    s, _pp, _ws, idx = got
    await _pos_edit(callback.message, _watch_text(s, idx), _watch_kb(s, idx))
    await callback.answer()


@router.callback_query(F.data.startswith("pos:wd:"))
async def pos_watch_delete(callback: CallbackQuery) -> None:
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Уже удалено")
        await _pos_screen(callback)
        return
    s, pp, ws, idx = got
    name = (ws[idx].get("title") or "").strip() or _short_url(ws[idx].get("url", ""))
    ws.pop(idx)
    pp["watches"] = ws
    if not ws:
        pp["enabled"] = False
    save_settings(callback.from_user.id, s)
    await callback.answer(f"Удалено: {name[:40]}")
    await _pos_edit(callback.message, _pos_text(s), _pos_kb(s))


@router.callback_query(F.data.startswith("pos:wc:"))
async def pos_watch_check(callback: CallbackQuery) -> None:
    """Read this watch's page right now. Reads only — spends nothing."""
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        return
    _s, _pp, _ws, idx = got
    await callback.answer("⏳ Смотрю витрину...")
    await _pos_edit(callback.message, "⏳ Читаю список предложений...",
                    _cancel_kb("pos:menu"))
    text = await _pos_report(callback.from_user.id, idx)
    s = get_settings(callback.from_user.id)
    await _pos_edit(callback.message, text, _watch_kb(s, idx))


async def _pos_report(uid: int, idx: int) -> str:
    """The standings on one watched page, as the seller sees them."""
    import asyncio

    from automation.market import cheapest, fetch_listing, find_position
    from automation.position import watches
    from storage import get_shop_name

    s = get_settings(uid)
    ws = watches(s.setdefault("promo_position", {}))
    if idx >= len(ws):
        return "Наблюдение удалено."
    w = ws[idx]
    shop = get_shop_name(uid) or ""
    loop = asyncio.get_event_loop()
    try:
        ok, res = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_listing, w["url"], shop),
            timeout=120)
    except Exception as e:
        return f"❌ Страница не открылась: {_esc(str(e)[:200])}"
    if not ok:
        return f"❌ {_esc(str(res)[:400])}"

    offers = res["offers"]
    mine = find_position(offers, ad_id=str(w.get("market_id") or ""),
                         title=str(w.get("title") or ""), seller=shop)
    thr = int(w.get("max_position") or 3)
    lines = [f"📍 <b>{_esc((w.get('title') or 'Позиция на витрине')[:40])}</b>\n",
             f"Предложений на странице: <b>{len(offers)}</b>"]
    if mine:
        w["last_pos"] = int(mine["pos"])
        w["last_check"] = _time.time()
        save_settings(uid, s)
        verdict = "🟢 в пределах порога" if mine["pos"] <= thr else "🔴 ниже порога"
        lines.append(f"Ваше место: <b>{mine['pos']}</b> из {len(offers)} "
                     f"(порог {thr}) — {verdict}")
        if mine["price"]:
            lines.append(f"Ваша цена: <b>{float(mine['price']):.0f} ₽</b>")
    else:
        lines.append(f"❔ Не нашёл ваш товар в списке "
                     f"(магазин «{_esc(shop) or '—'}»)")
    others = [o for o in offers if not mine or o["pos"] != mine["pos"]]
    low = cheapest(others or offers)
    if low is not None:
        lines.append(f"Дешевле всех у конкурентов: <b>{low:.0f} ₽</b>")
    lines.append("")
    lines.append("<b>Топ страницы:</b>")
    for row in offers[:8]:
        mark = "👉" if mine and row["pos"] == mine["pos"] else "  "
        price = f"{float(row['price']):.0f} ₽" if row["price"] is not None else "—"
        who = row["seller"][:16] or row["title"][:20]
        lines.append(f"{mark} {row['pos']}. {price} · {_esc(who)}")
    return "\n".join(lines)[:3800]


@router.callback_query(F.data == "pos:checkall")
async def pos_check_all(callback: CallbackQuery) -> None:
    from automation.position import watches
    s = get_settings(callback.from_user.id)
    ws = watches(s.setdefault("promo_position", {}))
    if not ws:
        await callback.answer("Нечего проверять", show_alert=True)
        return
    await callback.answer("⏳ Проверяю...")
    await _pos_edit(callback.message,
                    f"⏳ Читаю витрину: 0 из {len(ws)}...", _cancel_kb("pos:menu"))
    out: list[str] = []
    for i, w in enumerate(ws):
        rep = await _pos_report(callback.from_user.id, i)
        head = (w.get("title") or "").strip() or _short_url(w.get("url", ""))
        first = next((l for l in rep.split("\n")
                      if "Ваше место" in l or "Не нашёл" in l or l.startswith("❌")),
                     "—")
        out.append(f"{i + 1}. <b>{_esc(head[:28])}</b>\n   {first}")
    s = get_settings(callback.from_user.id)
    await _pos_edit(callback.message,
                    "📍 <b>Позиции сейчас</b>\n\n" + "\n".join(out)[:3600],
                    _pos_kb(s))


# --- binding a watch to a panel listing ------------------------------------

@router.callback_query(F.data.startswith("pos:wi:"))
async def pos_watch_item(callback: CallbackQuery) -> None:
    """Pick which panel listing this watch promotes.

    Without it the trigger can detect a slip and do nothing about it: the panel
    action is per-listing, and the storefront id is not the panel id.
    """
    import asyncio

    from automation.panel import panel_list_items_sync
    from storage import get_panel_creds

    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        return
    _s, _pp, _ws, idx = got
    uid = callback.from_user.id
    items = _ITEMS_CACHE.get(uid)
    if not items:
        creds = get_panel_creds(uid)
        if not creds or not creds.get("cookies"):
            await callback.answer("⚠️ Нужен вход в панель продавца",
                                  show_alert=True)
            return
        await callback.answer("⏳ Загружаю товары...")
        await _pos_edit(callback.message, "⏳ Загружаю товары из панели...",
                        _cancel_kb(f"pos:w:{idx}"))
        loop = asyncio.get_event_loop()
        ok, res = await loop.run_in_executor(
            None, panel_list_items_sync, creds["cookies"])
        if not ok:
            await _pos_edit(callback.message, f"❌ {_esc(str(res)[:200])}",
                            _cancel_kb(f"pos:w:{idx}"))
            return
        items = [{"id": str(i.get("id")), "title": str(i.get("title") or i.get("id"))}
                 for i in res if i.get("id")]
        _ITEMS_CACHE[uid] = items
    else:
        await callback.answer()

    b = InlineKeyboardBuilder()
    for j, it in enumerate(items[:40]):
        b.button(text=it["title"][:36], callback_data=f"pos:wis:{idx}:{j}")
    b.button(text="⬅️ Назад", callback_data=f"pos:w:{idx}")
    b.adjust(1)
    await _pos_edit(
        callback.message,
        "🔗 <b>Какой товар поднимать</b>\n\n"
        "Выберите объявление из панели — именно его бот оплатит и поднимет, "
        "когда позиция упадёт ниже порога.",
        b.as_markup())


@router.callback_query(F.data.startswith("pos:wis:"))
async def pos_watch_item_set(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    try:
        idx, j = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Список устарел", show_alert=True)
        return
    items = _ITEMS_CACHE.get(callback.from_user.id) or []
    got = _watch_at(callback.from_user.id, f"x:{idx}")
    if not got or j >= len(items):
        await callback.answer("Список устарел — откройте заново", show_alert=True)
        return
    s, _pp, ws, idx = got
    ws[idx]["item_id"] = items[j]["id"]
    if not (ws[idx].get("title") or "").strip():
        ws[idx]["title"] = items[j]["title"]
    save_settings(callback.from_user.id, s)
    await callback.answer(f"Привязано: {items[j]['title'][:40]}")
    await _pos_edit(callback.message, _watch_text(s, idx), _watch_kb(s, idx))


# --- thresholds ------------------------------------------------------------

async def _ask_number(callback: CallbackQuery, state: FSMContext,
                      st: State, idx: int, text: str) -> None:
    await state.set_state(st)
    await state.update_data(pos_idx=idx)
    await _pos_edit(callback.message, text, _cancel_kb(f"pos:w:{idx}"))
    await callback.answer()


@router.callback_query(F.data.startswith("pos:wt:"))
async def pos_threshold_start(callback: CallbackQuery, state: FSMContext) -> None:
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        return
    _s, _pp, ws, idx = got
    await _ask_number(
        callback, state, SeleniumState.waiting_pos_threshold, idx,
        f"🎯 <b>Порог места</b>\n\nСейчас: не ниже "
        f"<b>{int(ws[idx].get('max_position') or 3)}</b>-го\n\n"
        f"Пришлите число от 1 до 100. Как только товар окажется ниже этого "
        f"места в списке — бот сработает.")


@router.message(SeleniumState.waiting_pos_threshold)
async def pos_threshold_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        n = int((message.text or "").strip())
        if not 1 <= n <= 100:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно целое число от 1 до 100")
        return
    await state.clear()
    got = _watch_at(message.from_user.id, f"x:{data.get('pos_idx', 0)}")
    if not got:
        await message.answer("Наблюдение уже удалено")
        return
    s, _pp, ws, idx = got
    ws[idx]["max_position"] = n
    ws[idx]["last_alert_pos"] = 0          # a new threshold re-arms the alert
    save_settings(message.from_user.id, s)
    await message.answer(_watch_text(s, idx), reply_markup=_watch_kb(s, idx))


@router.callback_query(F.data.startswith("pos:wg:"))
async def pos_guard_start(callback: CallbackQuery, state: FSMContext) -> None:
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        return
    _s, _pp, ws, idx = got
    cur = float(ws[idx].get("undercut_guard") or 0)
    await _ask_number(
        callback, state, SeleniumState.waiting_pos_guard, idx,
        f"⛔ <b>Порог цены</b>\n\n"
        f"Сейчас: {f'{cur:.0f} ₽' if cur else 'не ограничено'}\n\n"
        f"Поднятие платное, и оно бессмысленно, когда рядом стоит тот же товар "
        f"заметно дешевле: покупатель всё равно уйдёт к нему.\n\n"
        f"Пришлите, на сколько рублей конкурент может быть дешевле, чтобы "
        f"поднятие всё ещё имело смысл. Больше этой разницы — бот не платит, "
        f"а предупредит вас.\n\n"
        f"<i>0 — снять ограничение.</i>")


@router.message(SeleniumState.waiting_pos_guard)
async def pos_guard_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        v = float((message.text or "").replace(",", ".").replace(" ", "").strip())
        if v < 0 or v > 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно число от 0 до 1 000 000")
        return
    await state.clear()
    got = _watch_at(message.from_user.id, f"x:{data.get('pos_idx', 0)}")
    if not got:
        await message.answer("Наблюдение уже удалено")
        return
    s, _pp, ws, idx = got
    ws[idx]["undercut_guard"] = v
    save_settings(message.from_user.id, s)
    await message.answer(_watch_text(s, idx), reply_markup=_watch_kb(s, idx))


@router.callback_query(F.data.startswith("pos:wp:"))
async def pos_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    got = _watch_at(callback.from_user.id, callback.data)
    if not got:
        await callback.answer("Наблюдение не найдено", show_alert=True)
        return
    _s, _pp, ws, idx = got
    cur = float(ws[idx].get("min_price") or 0)
    await _ask_number(
        callback, state, SeleniumState.waiting_pos_price, idx,
        f"⚠️ <b>Сигнал по цене</b>\n\n"
        f"Сейчас: {f'{cur:.0f} ₽' if cur else 'не задан'}\n\n"
        f"Пришлите цену: если на витрине кто-то опустится ниже неё, бот "
        f"сообщит — это повод пересмотреть свою.\n\n"
        f"<i>0 — выключить сигнал.</i>")


@router.message(SeleniumState.waiting_pos_price)
async def pos_price_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        v = float((message.text or "").replace(",", ".").replace(" ", "").strip())
        if v < 0 or v > 10_000_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно число от 0 до 10 000 000")
        return
    await state.clear()
    got = _watch_at(message.from_user.id, f"x:{data.get('pos_idx', 0)}")
    if not got:
        await message.answer("Наблюдение уже удалено")
        return
    s, _pp, ws, idx = got
    ws[idx]["min_price"] = v
    save_settings(message.from_user.id, s)
    await message.answer(_watch_text(s, idx), reply_markup=_watch_kb(s, idx))


# --- shop-wide settings ----------------------------------------------------

@router.callback_query(F.data == "pos:interval")
async def pos_interval_start(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get(
        "promo_position", {}).get("interval_hours", 1)
    await state.set_state(SeleniumState.waiting_pos_interval)
    await _pos_edit(
        callback.message,
        f"⏱ <b>Как часто проверять позицию</b>\n\nСейчас: {cur} ч\n\n"
        f"Пришлите число часов (можно дробное: 0.5 — каждые полчаса).",
        _cancel_kb("pos:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_pos_interval)
async def pos_interval_save(message: Message, state: FSMContext) -> None:
    try:
        h = float((message.text or "").replace(",", ".").strip())
        if not 0.25 <= h <= 168:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно число от 0.25 до 168")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("promo_position", {})["interval_hours"] = h
    save_settings(message.from_user.id, s)
    await message.answer(_pos_text(s), reply_markup=_pos_kb(s))


@router.callback_query(F.data == "pos:cooldown")
async def pos_cooldown_start(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get(
        "promo_position", {}).get("cooldown_hours", 6)
    await state.set_state(SeleniumState.waiting_pos_cooldown)
    await _pos_edit(
        callback.message,
        f"⏸ <b>Пауза между поднятиями</b>\n\nСейчас: {cur} ч\n\n"
        f"Товар, упавший вниз, остаётся внизу — без паузы бот платил бы за "
        f"него на каждой проверке. Пришлите, сколько часов ждать после "
        f"поднятия, прежде чем поднимать этот же товар снова.",
        _cancel_kb("pos:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_pos_cooldown)
async def pos_cooldown_save(message: Message, state: FSMContext) -> None:
    try:
        h = float((message.text or "").replace(",", ".").strip())
        if not 0 <= h <= 168:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно число от 0 до 168")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("promo_position", {})["cooldown_hours"] = h
    save_settings(message.from_user.id, s)
    await message.answer(_pos_text(s), reply_markup=_pos_kb(s))


@router.callback_query(F.data == "pos:limit")
async def pos_limit_start(callback: CallbackQuery, state: FSMContext) -> None:
    s = get_settings(callback.from_user.id)
    cur = s.get("promo_position", {}).get("daily_limit", 3)
    price = promo_price(s)
    await state.set_state(SeleniumState.waiting_pos_limit)
    await _pos_edit(
        callback.message,
        f"🧾 <b>Лимит поднятий в сутки</b>\n\n"
        f"Сейчас: {cur if cur else 'без лимита'}"
        + (f" (до {cur * price} ₽ на товар в день)" if cur and price else "")
        + "\n\nСколько раз за сутки бот может оплатить поднятие <b>одного</b> "
          "товара. Пришлите число.\n\n<i>0 — без лимита (не советую).</i>",
        _cancel_kb("pos:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_pos_limit)
async def pos_limit_save(message: Message, state: FSMContext) -> None:
    try:
        n = int((message.text or "").strip())
        if not 0 <= n <= 50:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно целое число от 0 до 50")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    s.setdefault("promo_position", {})["daily_limit"] = n
    save_settings(message.from_user.id, s)
    await message.answer(_pos_text(s), reply_markup=_pos_kb(s))


# ---------------------------------------------------------------------------
# Tariff picker for the paid «Премиум» action
#
# The action refuses to run without up_id (service), parameter_id (duration,
# 19/49/89 ₽) and system_id (payment method — СБП, cards, crypto, some with a
# commission). None of that can be guessed: each combination costs a different
# amount and charges a different account, so the options are read from the
# panel and chosen here once, then reused by every promotion.
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


# ---------------------------------------------------------------------------
# Which listings to promote
#
# Promotion is charged per listing, so "promote everything" is the expensive
# default. Picking positions is what makes a budget last.
# ---------------------------------------------------------------------------

_ITEMS_CACHE: dict[int, list] = {}


async def _promo_items_screen(callback: CallbackQuery, reload: bool = False) -> None:
    import asyncio

    from automation.panel import panel_list_items_sync
    from storage import get_panel_creds

    uid = callback.from_user.id
    items = _ITEMS_CACHE.get(uid)
    if reload or not items:
        creds = get_panel_creds(uid)
        if not creds or not creds.get("cookies"):
            await callback.answer("⚠️ Нужен вход в панель", show_alert=True)
            return
        await callback.message.edit_text("⏳ Загружаю товары из панели...")
        loop = asyncio.get_event_loop()
        ok, res = await loop.run_in_executor(
            None, panel_list_items_sync, creds["cookies"])
        if not ok:
            await callback.message.edit_text(
                f"❌ {_esc(str(res)[:200])}",
                reply_markup=_cancel_kb("selenium:bump:menu"))
            return
        items = [{"id": str(i.get("id")), "title": str(i.get("title") or i.get("id"))}
                 for i in res if i.get("id")]
        _ITEMS_CACHE[uid] = items

    s = get_settings(uid)
    picked = set(promo_only_ids(s))
    price = promo_price(s)

    b = InlineKeyboardBuilder()
    for i, it in enumerate(items[:40]):
        mark = "✅" if it["id"] in picked else "▫️"
        b.button(text=f"{mark} {it['title'][:34]}", callback_data=f"promo:it:{i}")
    b.adjust(1)
    b.button(text="☑️ Все", callback_data="promo:it_all")
    b.button(text="✖️ Снять все", callback_data="promo:it_none")
    b.button(text="🔄 Обновить список", callback_data="promo:items_reload")
    b.button(text="⬅️ Готово", callback_data="selenium:bump:menu")
    b.adjust(*([1] * len(items[:40])), 2, 1, 1)

    n = len(picked)
    cost = f"\n💸 За один прогон: <b>{n * price} ₽</b>" if (n and price) else ""
    await callback.message.edit_text(
        f"🎯 <b>Что продвигать</b>\n\n"
        f"Выбрано: <b>{n}</b> из {len(items)}"
        + (cost or "")
        + "\n\n<i>Если не выбрано ничего — продвигаются все товары, и платится "
          "за каждый. Отметьте только те позиции, которые нужно держать "
          "наверху.</i>",
        reply_markup=b.as_markup())


@router.callback_query(F.data == "promo:items")
async def promo_items(callback: CallbackQuery) -> None:
    await callback.answer()
    await _promo_items_screen(callback)


@router.callback_query(F.data == "promo:items_reload")
async def promo_items_reload(callback: CallbackQuery) -> None:
    await callback.answer("⏳ Обновляю...")
    await _promo_items_screen(callback, reload=True)


@router.callback_query(F.data.startswith("promo:it:"))
async def promo_item_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    items = _ITEMS_CACHE.get(uid) or []
    try:
        it = items[int(callback.data.split(":")[-1])]
    except (ValueError, IndexError):
        await callback.answer("Список устарел — обновите", show_alert=True)
        return
    s = get_settings(uid)
    ab = s.setdefault("auto_bump", {})
    picked = list(ab.get("only_items") or [])
    if it["id"] in picked:
        picked.remove(it["id"])
        note = "убрано"
    else:
        picked.append(it["id"])
        note = "добавлено"
    ab["only_items"] = picked
    save_settings(uid, s)
    await callback.answer(f"{note}: {it['title'][:40]}")
    await _promo_items_screen(callback)


@router.callback_query(F.data == "promo:it_all")
async def promo_item_all(callback: CallbackQuery) -> None:
    """Empty selection means everything — clearer than storing every id, and it
    keeps working when new listings appear."""
    uid = callback.from_user.id
    s = get_settings(uid)
    s.setdefault("auto_bump", {})["only_items"] = []
    save_settings(uid, s)
    await callback.answer("Продвигаются все товары")
    await _promo_items_screen(callback)


@router.callback_query(F.data == "promo:it_none")
async def promo_item_none(callback: CallbackQuery) -> None:
    """Deselect all: pick nothing, which is 'all' — so this offers the safer
    reading, one item, rather than silently meaning the whole shop."""
    uid = callback.from_user.id
    items = _ITEMS_CACHE.get(uid) or []
    s = get_settings(uid)
    s.setdefault("auto_bump", {})["only_items"] = (
        [items[0]["id"]] if items else [])
    save_settings(uid, s)
    await callback.answer("Оставил один товар — снимите и его, если нужно")
    await _promo_items_screen(callback)


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
    # What this will cost is the first thing worth knowing here
    _can_pay, _covers, money = await promo_afford(api, s)
    tail = f"\n\n💸 {money[0].upper()}{money[1:]}." if money else ""
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
    _can_pay, covers, money = await promo_afford(api, s)

    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, продвинуть все", callback_data="selenium:run:bump_ok")
    b.button(text="⚙️ Сменить тариф", callback_data="promo:setup")
    b.button(text="❌ Отмена", callback_data="selenium:bump:menu")
    b.adjust(1)
    await callback.message.edit_text(
        "⚠️ <b>Продвижение платное</b>\n\n"
        f"Тариф: <b>{label}</b>\n"
        + (f"Стоимость: <b>{price} ₽</b> за каждое объявление.\n" if price else "")
        + (f"Потолок трат ограничит запуск {cap} объявлениями.\n" if cap else "")
        + "\nОплата пройдёт выбранным способом — на каждое объявление "
          "придёт своя ссылка.\n\nПродвинуть все объявления?",
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
    _can_pay, covers, _money = await promo_afford(api, s)

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
                promo_params(s), min(caps) if caps else 0,
                promo_only_ids(s)),
            timeout=300,
        )
        s["auto_bump"]["last_bump_run"] = _time.time()
        spent = count * promo_price(s)
        if spent:
            msg += f"\n\n💸 К оплате: <b>{spent} ₽</b> за {count} шт."
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

def _restore_text(s: dict, creds=None, uid: int | None = None) -> str:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    interval = ar.get("interval_hours", 1)
    last_run = _fmt_ts(ar.get("last_restore_run"))
    total = int(ar.get("restored_total", 0) or 0)
    held = len([f for f in (ar.get("failures") or {}).values()
                if float(f.get("until", 0) or 0) > _time.time()])
    # The build stamp lives here on purpose: several rounds were spent reading
    # output from a container that had not been rebuilt, and a screenshot that
    # names its own build settles that in one glance.
    from handlers.start import BOT_VERSION
    lines = [f"🔄 <b>Авто-восстановление</b>  <code>{BOT_VERSION}</code>\n"]
    if uid is not None:
        # Which shop this acts on. Adding an account is not switching to it,
        # and a token from one shop paired with a panel from another is the
        # failure mode that costs the most time to recognise.
        from storage import get_active_account, get_panel_creds, get_shop_name
        acc = get_active_account(uid)
        # Both sides on one line: restore needs them to be the same shop, and
        # showing only one half is what let them drift apart unnoticed.
        panel_login = (get_panel_creds(uid) or {}).get("login") or "вход не выполнен"
        lines.append(f"🏪 {_esc(get_shop_name(uid) or '—')}"
                     + (f" «{_esc(acc)}»" if acc else "")
                     + f"  ·  🌐 {_esc(panel_login)}")
    lines += [
        f"{_st(on)}  ·  каждые {interval} ч  ·  запуск: {last_run}",
        f"📦 Остатки: {'обязательны' if ar.get('require_stock', True) else 'не важны'}"
        f"  ·  ⚡ Сразу после продажи: "
        f"{'да' if ar.get('instant', True) else 'нет'}",
    ]
    tail = []
    if ar.get("last_result"):
        tail.append(ar["last_result"])
    if total:
        tail.append(f"всего {total}")
    if held:
        tail.append(f"⏸ отложено {held}")
    if tail:
        lines.append("  ·  ".join(tail))
    # A barred status silences restore for every ad in it, so it cannot stay
    # invisible: "поднято 0" with no explanation reads as a broken feature.
    from tasks.manager import _barred_map
    barred = [st for st, until in _barred_map(ar).items() if until > _time.time()]
    if barred:
        lines.append(f"🚫 В бане: <b>{', '.join(barred)}</b> — пропускаются")
    lines += [
        "",
        "<i>Возвращает истёкшие и распроданные — снятые вручную Юмаркет "
        "не отдаёт, их публикуют на сайте. Публикация идёт через "
        "модерацию.</i>",
    ]
    return "\n".join(lines)


def _restore_kb(s: dict, creds=None) -> InlineKeyboardMarkup:
    ar = s.get("auto_restore", {})
    on = ar.get("enabled", False)
    stock = ar.get("require_stock", True)
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить сейчас", callback_data="selenium:run:restore")
    b.button(text=f"{'🔴 Выкл' if on else '🟢 Вкл'}", callback_data="selenium:restore:toggle")
    b.button(text="👁 Что будет восстановлено", callback_data="restore:preview")
    b.button(text=f"⏱ Проверка: {ar.get('interval_hours', 1)} ч",
             callback_data="restore:interval")
    b.button(text=f"📦 Остатки: {'обязательны' if stock else 'не важны'}",
             callback_data="restore:stock")
    b.button(text=f"⚡ Сразу после продажи: "
                  f"{'вкл' if ar.get('instant', True) else 'выкл'}",
             callback_data="restore:instant")
    held = [f for f in (ar.get("failures") or {}).values()
            if float(f.get("until", 0) or 0) > _time.time()]
    # A barred status holds ads back just as a per-ad wait does, and it used to
    # hide the only button that lifts either — leaving no way out of a ban that
    # silences the whole feature.
    from tasks.manager import _barred_map
    barred = [st for st, u in _barred_map(ar).items() if u > _time.time()]
    if held or barred:
        label = f"⏸ Отложенные ({len(held)})" if held else "🚫 Снять баны"
        if held and barred:
            label = f"⏸ Отложенные ({len(held)}) · баны {len(barred)}"
        b.button(text=label, callback_data="restore:held")
    b.button(text="⬅️ К объявлениям", callback_data="menu:ads")
    b.adjust(2, 1, 2, 1, 1, 1)
    return b.as_markup()


@router.callback_query(F.data == "selenium:restore:menu")
async def restore_menu(callback: CallbackQuery, state: FSMContext) -> None:
    uid = callback.from_user.id
    await state.clear()
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))
    await callback.answer()


@router.callback_query(F.data == "selenium:restore:toggle")
async def restore_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(callback.from_user.id)
    s["auto_restore"]["enabled"] = not s["auto_restore"].get("enabled", False)
    # Turning it on should act promptly, not wait out an interval that already
    # elapsed while it was off
    if s["auto_restore"]["enabled"]:
        s["auto_restore"]["last_restore_run"] = 0
    save_settings(callback.from_user.id, s)
    await callback.message.edit_text(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))
    await callback.answer("Включено" if s["auto_restore"]["enabled"] else "Выключено")


@router.callback_query(F.data == "restore:stock")
async def restore_stock_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(callback.from_user.id)
    ar = s.setdefault("auto_restore", {})
    ar["require_stock"] = not ar.get("require_stock", True)
    save_settings(callback.from_user.id, s)
    await callback.answer(
        "Распроданные будут пропускаться" if ar["require_stock"]
        else "Публиковать буду и без остатков — маркетплейс может отказать",
        show_alert=True)
    await callback.message.edit_text(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))


@router.callback_query(F.data == "restore:instant")
async def restore_instant_toggle(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(callback.from_user.id)
    ar = s.setdefault("auto_restore", {})
    ar["instant"] = not ar.get("instant", True)
    save_settings(callback.from_user.id, s)
    await callback.answer(
        "Товар вернётся в продажу сразу после покупки"
        if ar["instant"] else
        "Возврат только по расписанию — до часа ожидания",
        show_alert=True)
    await callback.message.edit_text(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))


@router.callback_query(F.data == "restore:interval")
async def restore_interval(callback: CallbackQuery, state: FSMContext) -> None:
    cur = get_settings(callback.from_user.id).get(
        "auto_restore", {}).get("interval_hours", 1)
    await state.set_state(SeleniumState.waiting_restore_interval)
    await callback.message.edit_text(
        f"⏱ <b>Как часто проверять</b>\n\nСейчас: {cur} ч\n\n"
        f"Пришлите число часов (1, 3, 6, 12, 24). Можно дробное: 0.5 — "
        f"каждые полчаса.",
        reply_markup=_cancel_kb("selenium:restore:menu"))
    await callback.answer()


@router.message(SeleniumState.waiting_restore_interval)
async def restore_save_interval(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    try:
        hours = float((message.text or "").replace(",", ".").strip())
        if not 0.25 <= hours <= 168:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужно число от 0.25 до 168, например: 6")
        return
    s = get_settings(message.from_user.id)
    s.setdefault("auto_restore", {})["interval_hours"] = hours
    save_settings(message.from_user.id, s)
    await state.clear()
    await message.answer(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))


@router.callback_query(F.data == "restore:held")
async def restore_held(callback: CallbackQuery) -> None:
    """Ads put aside after the marketplace refused them, and why."""
    s = get_settings(callback.from_user.id)
    ar = s.get("auto_restore", {})
    now = _time.time()
    held = [(a, f) for a, f in (ar.get("failures") or {}).items()
            if float(f.get("until", 0) or 0) > now]
    from tasks.manager import _barred_map
    barred = [(st, u) for st, u in _barred_map(ar).items() if u > now]

    b = InlineKeyboardBuilder()
    b.button(text="🔁 Снять отсрочку со всех", callback_data="restore:unhold")
    b.button(text="⬅️ Назад", callback_data="selenium:restore:menu")
    b.adjust(1)

    # A barred status quietly skips every ad in it, so it has to be visible —
    # otherwise a seller sees listings that never come back and no reason why.
    barred_block = ""
    if barred:
        barred_block = ("\n\n🚫 <b>Статусы, которые маркетплейс отказался "
                        "публиковать</b>\n"
                        + "\n".join(f"• <code>{_esc(st)}</code> — повторю "
                                   f"{_fmt_ts(u)}" for st, u in sorted(barred))
                        + "\n<i>Объявления в этих статусах пропускаются.</i>")

    if not held:
        await callback.message.edit_text(
            ("Отложенных объявлений нет." + barred_block) or "—",
            reply_markup=b.as_markup())
        await callback.answer()
        return
    lines = []
    for aid, f in held[:15]:
        lines.append(
            f"• <b>{_esc(f.get('title') or aid)[:34]}</b>\n"
            f"   попыток: {f.get('tries')}, следующая: "
            f"{_fmt_ts(f.get('until'))}\n"
            f"   причина: {_esc(f.get('reason'))[:90]}")
    await callback.message.edit_text(
        ("⏸ <b>Отложены после отказа</b>\n\n"
         + "\n\n".join(lines)[:3000] + barred_block),
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "restore:unhold")
async def restore_unhold(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    s = get_settings(callback.from_user.id)
    ar = s.setdefault("auto_restore", {})
    ar["failures"] = {}
    # Barred statuses are the broader half of the same hold, so clearing one
    # without the other leaves the seller wondering why nothing changed.
    ar["barred_until"] = {}
    ar.pop("barred_statuses", None)
    save_settings(callback.from_user.id, s)
    await callback.answer("Отсрочки сняты", show_alert=True)
    await callback.message.edit_text(_restore_text(s, uid=uid), reply_markup=_restore_kb(s))


@router.callback_query(F.data == "restore:preview")
async def restore_preview(callback: CallbackQuery, api: YooMarketAPI) -> None:
    """Show what a run would touch, without touching anything.

    Restoring publishes to a live marketplace, so being able to look before
    acting is worth a button.
    """
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    await callback.answer("⏳ Смотрю...")
    await callback.message.edit_text("⏳ Проверяю, что можно восстановить...")
    s = get_settings(callback.from_user.id)
    from tasks.manager import _barred_map
    try:
        rep = await api.restore_ads(
            require_stock=bool(s.get("auto_restore", {}).get("require_stock", True)),
            dry_run=True,
            skip_statuses=[st for st, u in
                           _barred_map(s.get("auto_restore", {})).items()
                           if u > _time.time()])
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Не удалось посмотреть: {_esc(str(e)[:200])}",
            reply_markup=_restore_kb(s))
        return

    body = [f"Всего объявлений: <b>{rep['total']}</b>",
            f"Подходят под восстановление: <b>{rep['candidates']}</b>", ""]
    if rep["restored"]:
        body.append(f"✅ Будут опубликованы: <b>{len(rep['restored'])}</b>")
        for row in rep["restored"][:12]:
            body.append(f"   • {_esc(row['title'])[:38]} ({_esc(row['status'])})")
    else:
        body.append("Публиковать сейчас нечего.")
    if rep["no_stock"]:
        body.append("")
        body.append(f"📦 Пропущу без остатков: <b>{len(rep['no_stock'])}</b>")
        for row in rep["no_stock"][:8]:
            body.append(f"   • {_esc(row['title'])[:32]} — {_esc(row.get('note'))}")
    if rep.get("unknown"):
        body.append("")
        body.append(f"❔ Незнакомый статус: <b>{len(rep['unknown'])}</b> "
                    f"— не трогаю, пока не ясно, что он значит")
        for row in rep["unknown"][:8]:
            body.append(f"   • {_esc(row['title'])[:32]} — "
                        f"<code>{_esc(row['status'])}</code>")
        body.append("<i>Пришлите мне этот статус, если такие объявления "
                    "должны восстанавливаться.</i>")
    body.append("")
    body.append(f"<i>Статусы в аккаунте: {_esc(', '.join(rep['statuses'][:10]))}</i>")

    b = InlineKeyboardBuilder()
    if rep["restored"]:
        b.button(text=f"▶️ Восстановить ({len(rep['restored'])})",
                 callback_data="selenium:run:restore")
    b.button(text="🔄 Обновить", callback_data="restore:preview")
    b.button(text="⬅️ Назад", callback_data="selenium:restore:menu")
    b.adjust(1, 2)
    await callback.message.edit_text(
        "👁 <b>Предпросмотр восстановления</b>\n\n" + "\n".join(body)[:3500],
        reply_markup=b.as_markup())


@router.callback_query(F.data == "selenium:run:restore")
async def run_restore(callback: CallbackQuery, api: YooMarketAPI) -> None:
    if not api:
        await callback.answer("⚠️ API токен не настроен", show_alert=True)
        return
    await callback.answer("⏳ Восстанавливаю объявления...", show_alert=False)
    await callback.message.edit_text("⏳ Восстанавливаю объявления...")
    uid = callback.from_user.id
    s = get_settings(uid)
    ar = s.setdefault("auto_restore", {})
    try:
        from handlers.panel_items import _deleted_ids
        from tasks.manager import _barred_map, _RESTORE_BARRED_TTL
        now = _time.time()
        # A manual run is a deliberate retry, so a status barred earlier is
        # given another chance here rather than skipped again.
        barred_until = _barred_map(ar)
        rep = await api.restore_ads(
            require_stock=bool(ar.get("require_stock", True)),
            skip_ids=_deleted_ids(uid),
            skip_statuses=[])

        # The same panel fallback the schedule uses. Without it, pressing
        # «Запустить сейчас» reported incorrect_status and stopped there, while
        # the automatic pass quietly recovered the very same listings — the
        # manual button looked broken next to the feature that worked.
        retryable = [r for r in rep["failed"]
                     if "incorrect_status" in str(r.get("reason", ""))]
        done = []
        if retryable:
            from tasks.manager import panel_republish
            done, still = await panel_republish(uid, retryable)
            rep["restored"] += done
            rep["failed"] = [r for r in rep["failed"]
                             if r not in retryable] + still

        # A status the panel just published from is not barred, and a standing
        # ban on it is lifted: the API refusing «unpublish» is exactly what the
        # fallback is for, and barring it would silence restore for the one
        # state it exists to handle.
        # A pass that never got a verdict out of the panel — no login, or the
        # listing not located there — says nothing about any status, so bans
        # standing from one are dropped. Marked on the row rather than matched
        # in its text: the text form let «в панели не нашёл этот товар» through.
        if any(r.get("panel") in ("unreached", "not_found")
               for r in rep["failed"]):
            barred_until.clear()
        recovered = {str(r.get("status") or "").lower() for r in done}
        for st in recovered:
            barred_until.pop(st, None)
        for row in rep["failed"]:
            reason = str(row.get("reason", ""))
            if "incorrect_status" not in reason:
                continue
            # Only the panel actually refusing the action is evidence about
            # the status.
            if row.get("panel") != "refused":
                continue
            st = str(row.get("status") or "").lower()
            if st and st not in recovered:
                barred_until[st] = now + _RESTORE_BARRED_TTL
        ar["barred_until"] = barred_until
        ar.pop("barred_statuses", None)
        ar["last_restore_run"] = _time.time()
        ar["restored_total"] = int(ar.get("restored_total", 0) or 0) + len(rep["restored"])
        # A manual run is a deliberate retry: clear the waits it just resolved
        failures = ar.setdefault("failures", {})
        for row in rep["restored"]:
            failures.pop(str(row["id"]), None)
        ar["last_result"] = (f"поднято {len(rep['restored'])}, "
                             f"без остатков {len(rep['no_stock'])}, "
                             f"отказов {len(rep['failed'])}")
        save_settings(uid, s)

        parts = [f"✅ Опубликовано заново: <b>{len(rep['restored'])}</b>"]
        for row in rep["restored"][:10]:
            parts.append(f"   • {_esc(row['title'])[:40]}")
        if rep["restored"]:
            parts.append("<i>Уходит на модерацию — статус сменится после проверки.</i>")
        if rep["no_stock"]:
            parts.append("")
            parts.append(f"📦 Пропущено без остатков: <b>{len(rep['no_stock'])}</b>")
            for row in rep["no_stock"][:6]:
                parts.append(f"   • {_esc(row['title'])[:32]} — {_esc(row.get('note'))}")
        if rep["failed"]:
            parts.append("")
            parts.append(f"⛔ Отказано: <b>{len(rep['failed'])}</b>")
            for row in rep["failed"][:6]:
                st = _esc(str(row.get("status") or "?"))
                parts.append(f"   • {_esc(row['title'])[:26]}  <code>{st}</code>"
                             f"\n     {_esc(row['reason'])[:400]}")
        if rep.get("unknown"):
            parts.append("")
            parts.append(f"❔ Незнакомый статус, не трогал: "
                         f"<b>{len(rep['unknown'])}</b>")
            for row in rep["unknown"][:5]:
                parts.append(f"   • {_esc(row['title'])[:30]} — "
                             f"<code>{_esc(row['status'])}</code>")
        if not rep["restored"] and not rep["no_stock"] and not rep["failed"]:
            parts = ["Нечего восстанавливать — все объявления на месте.",
                     f"<i>Статусы: {_esc(', '.join(rep['statuses'][:10]))}</i>"]
        result_text = "🔄 <b>Восстановление завершено</b>\n\n" + "\n".join(parts)
    except Exception as e:
        logger.error("Manual restore error: %s", e)
        result_text = f"❌ Ошибка: {_esc(str(e)[:200])}"
    await callback.message.edit_text(
        (result_text + "\n\n" + _restore_text(s, uid=uid))[:4000],
        reply_markup=_restore_kb(s))


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


async def _panel_actions_for(uid: int, item_id) -> str:
    """Read-only: which Nova actions the panel offers for this listing."""
    import asyncio
    from storage import get_panel_creds
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return "нет входа в панель"
    try:
        from automation.panel import panel_item_actions_sync
    except ImportError:
        return "проба недоступна"
    try:
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, panel_item_actions_sync,
                                 creds["cookies"], str(item_id)),
            timeout=30)
    except Exception as e:
        return f"ошибка: {str(e)[:90]}"


@router.message(Command("withdraw_debug"))
async def withdraw_debug(message: Message) -> None:
    """Where the payout action lives — read-only, runs nothing."""
    import asyncio as _a
    import html as _html
    from storage import get_panel_creds
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет входа в панель — «Настройки» → «Панель продавца»")
        return
    status = await message.answer("⏳ Смотрю, где живёт «Вывести»…")
    from automation.panel import panel_withdraw_probe_sync
    try:
        report = await _a.wait_for(
            _a.get_event_loop().run_in_executor(
                None, panel_withdraw_probe_sync, creds["cookies"]),
            timeout=120)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    await status.edit_text("💸 <b>Вывод: что предлагает панель</b>\n\n"
                           f"<code>{_html.escape(report)[:3500]}</code>")


@router.message(Command("panel_map"))
async def panel_map(message: Message) -> None:
    """Every panel resource, its size and sample names — read-only.

    Walking resources blind and reporting a truncated trace is how three runs
    went by without locating the listings. This prints the whole census at
    once, and marks the resource whose rows carry the name being looked for.
    """
    import asyncio as _a
    import html as _html
    from storage import get_panel_creds
    creds = get_panel_creds(message.from_user.id)
    if not creds or not creds.get("cookies"):
        await message.answer("⚠️ Нет входа в панель — «Настройки» → «Панель продавца»")
        return
    needle = (message.text or "").partition(" ")[2].strip()
    status = await message.answer("⏳ Обхожу ресурсы панели…")
    from automation.panel import panel_resource_census_sync
    try:
        report = await _a.wait_for(
            _a.get_event_loop().run_in_executor(
                None, panel_resource_census_sync, creds["cookies"], needle),
            timeout=180)
    except Exception as e:
        report = f"ошибка: {str(e)[:200]}"
    await status.edit_text(
        "🗺 <b>Ресурсы панели</b>"
        + (f"\n<i>ищу: {_html.escape(needle)}</i>" if needle else "")
        + f"\n\n<code>{_html.escape(report)[:3500]}</code>")


@router.message(Command("restore_debug"))
async def restore_debug(message: Message, api: YooMarketAPI) -> None:
    """Print, for the listings restore wants to fix, exactly what the API says.

    A live run refused every candidate with incorrect_status while reporting
    their status as «unpublish» — the state restore was built around. Rather
    than guess what that state really means, this prints the raw list row, the
    raw detail record and the stock verdict for each one, so the classification
    can be corrected from the marketplace's own answer. Read-only: it publishes
    nothing.
    """
    if not api:
        await message.answer("⚠️ Не настроен API-токен")
        return

    import html as _html
    import json as _json

    status = await message.answer("⏳ Читаю объявления...")
    try:
        data = await api.get_ads()
        rows = [a for a in (data.get("data") or data.get("items") or [])
                if isinstance(a, dict)]
        by_state: dict[str, int] = {}
        for a in rows:
            by_state[api._ad_state(a)] = by_state.get(api._ad_state(a), 0) + 1

        lines = [f"всего объявлений: {len(rows)}",
                 f"статусы: {_json.dumps(by_state, ensure_ascii=False)}",
                 f"_DOWN (пробуем публиковать): {list(api._DOWN)}",
                 f"_NEVER (не трогаем): {list(api._NEVER)}", ""]

        targets = [a for a in rows if api._ad_state(a) in api._DOWN][:3]
        if not targets:
            lines.append("Кандидатов на восстановление нет.")
        for a in targets:
            aid = a.get("id")
            lines.append("─" * 30)
            lines.append(f"СТРОКА СПИСКА (id={aid}):")
            for k, v in a.items():
                if isinstance(v, (dict, list)):
                    v = _json.dumps(v, ensure_ascii=False)
                lines.append(f"  • {k} = {str(v)[:90]}")
            try:
                full = await api.get_ad(aid)
                inner = full.get("data") or full
                lines.append("КАРТОЧКА ТОВАРА:")
                for k, v in inner.items():
                    if isinstance(v, (dict, list)):
                        v = _json.dumps(v, ensure_ascii=False)
                    lines.append(f"  • {k} = {str(v)[:90]}")
            except Exception as e:
                lines.append(f"  карточка недоступна: {str(e)[:120]}")
            try:
                has, note = await api.ad_stock(aid)
                lines.append(f"ОСТАТКИ: has={has} {note}")
            except Exception as e:
                lines.append(f"ОСТАТКИ: ошибка {str(e)[:90]}")
            # Does the panel know this id, and what can it do with it? This is
            # what decides whether the panel fallback can work at all.
            lines.append(f"ПАНЕЛЬ: {await _panel_actions_for(message.from_user.id, aid)}")
        report = "\n".join(lines)
    except Exception as e:
        report = f"ошибка: {str(e)[:250]}"

    await status.edit_text(
        f"🔍 <b>Диагностика восстановления</b>\n\n"
        f"<code>{_html.escape(report)[:3500]}</code>")
