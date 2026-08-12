import asyncio
import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from api.yoomarket import YooMarketAPI
from keyboards.main import back_keyboard
import localtime as _lt
from storage import get_settings, save_settings

router = Router()
logger = logging.getLogger(__name__)


class WithdrawState(StatesGroup):
    waiting_amount = State()
    waiting_threshold = State()
    waiting_auto_min = State()
    waiting_req_value = State()      # a requisite field in the panel withdraw wizard
    waiting_panel_amount = State()   # amount for a manual panel withdrawal
    waiting_option_search = State()  # filtering a long option list by name


def _pick(*sources_and_keys) -> object | None:
    """First value whose key is actually present.

    A plain `a.get(x) or a.get(y)` chain drops a real balance of 0, because
    zero is falsy — the value is discarded and the search moves on.
    """
    sources, keys = sources_and_keys[0], sources_and_keys[1:]
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            if key in src and src[key] not in (None, ""):
                return src[key]
    return None


_MONEY_KEYS = ("balance", "wallet", "money", "balance_rub", "amount")
_PENDING_KEYS = ("pending_balance", "pending", "hold", "frozen")


def _money(value):
    """Число из того, во что маркетплейс завернул сумму. None — не число.

    Разбор один на весь бот — в `orderfields.parse_amount`. Здешняя версия
    убирала пробелы и запятые, но не символ валюты: «1 234,56 ₽»
    становилось «1234.56₽», float падал молча, и баланс показывался
    прочерком при том, что панель его прислала.
    """
    from orderfields import parse_amount
    return parse_amount(value)


def _deep_find(node, keys, depth: int = 4):
    """First value under any of `keys`, searched breadth-first.

    A one-level lookup missed a balance nested one dict deeper than expected
    and reported zero, which reads as "the shop has no money" rather than "the
    bot did not look there".
    """
    level = [node]
    for _ in range(depth):
        nxt = []
        for cur in level:
            if not isinstance(cur, dict):
                continue
            for k in keys:
                if k in cur and cur[k] not in (None, ""):
                    got = _money(cur[k]) if keys is _MONEY_KEYS else cur[k]
                    if got is not None:
                        return got
            nxt.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        level = [x for v in nxt for x in (v if isinstance(v, list) else [v])]
        if not level:
            break
    return None


def _shape(data, limit: int = 12) -> str:
    """The keys a response actually has — for saying why nothing was found."""
    if isinstance(data, dict):
        inner = [f"{k}:{_shape(v, 4)}" if isinstance(v, dict) else k
                 for k in list(data)[:limit] for v in [data[k]]]
        return "{" + ", ".join(inner) + "}"
    return type(data).__name__


async def _run_panel(uid, fn, *args):
    """Выполнить блокирующий вызов панели и распаковать его (ok, payload).

    Функция была удалена 05.08 вместе с соседней, а четыре её вызова и
    импорт из `stats_source` остались. Ловилось это только в бою и молча:
    статистика на каждый экран получала ImportError, уходила в запасной
    источник и подписывалась «история бота — cannot import name
    '_run_panel'», а баланс, реквизиты и вывод падали на NameError.
    Из-за этого же цифры не сходились с панелью: их туда просто не спрашивали.

    Все `panel_*` возвращают (ok, data). Отдавать этот кортеж как есть
    нельзя: вызывающий проверял `isinstance(res, list)` против кортежа,
    проверка не срабатывала, и весь сырой ответ уезжал в сообщение длиннее
    телеграмного предела — «загружаю…» так и оставалось на экране.
    """
    import asyncio as _a
    from storage import get_panel_creds
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return None, "нужен вход в панель продавца"
    loop = _a.get_event_loop()
    try:
        result = await _a.wait_for(
            loop.run_in_executor(None, fn, creds["cookies"], *args),
            timeout=60)
    except _a.TimeoutError:
        return None, "панель не ответила за 60 секунд"
    except Exception as e:
        return None, str(e)[:150] or type(e).__name__
    if (isinstance(result, tuple) and len(result) == 2
            and isinstance(result[0], bool)):
        ok, payload = result
        return (payload, "") if ok else (None, str(payload)[:250])
    return result, ""


async def _panel_balance(uid: int) -> tuple[str | None, str]:
    """The balance as the panel reports it → (formatted, error).

    The Integration API has no balance anywhere: /check returns identity only.
    The panel's balances resource is the sole source, and it is the same one
    withdrawal acts on, so the figure shown and the figure withdrawn agree.
    """
    import asyncio as _a
    from storage import get_panel_creds
    creds = get_panel_creds(uid)
    if not creds or not creds.get("cookies"):
        return None, "нужен вход в панель продавца"
    from automation.panel import panel_balances_sync, panel_shop_balance_sync
    loop = _a.get_event_loop()

    # Where the panel actually shows it: the shop's own page. The `balances`
    # resource was the earlier guess and answers nothing on this panel.
    try:
        ok, got = await _a.wait_for(
            loop.run_in_executor(None, panel_shop_balance_sync,
                                 creds["cookies"], ""),
            timeout=45)
    except Exception as e:
        ok, got = False, f"не ответила: {str(e)[:60]}"
    if ok and isinstance(got, dict):
        amt = got.get("amount")
        return f"{amt:.2f}".rstrip("0").rstrip("."), ""
    shop_err = str(got)[:200]

    try:
        ok, rows = await _a.wait_for(
            loop.run_in_executor(None, panel_balances_sync, creds["cookies"]),
            timeout=45)
    except Exception as e:
        return None, f"магазин: {shop_err} · balances: {str(e)[:50]}"
    if not ok or not isinstance(rows, list):
        # И причину второй попытки тоже: «магазин: …» без неё выглядит так,
        # будто balances вообще не спрашивали.
        return None, f"магазин: {shop_err} · balances: {str(rows)[:120]}"

    # Several rows can exist (one per currency); the roubles one is the balance
    # a seller means, and any single row is unambiguous on its own.
    best = None
    for row in rows:
        amt = _money(row.get("amount"))
        if amt is None:
            continue
        cur = str(row.get("currency") or "").lower()
        if "rub" in cur or "руб" in cur or "₽" in cur or not cur:
            return f"{amt:.2f}".rstrip("0").rstrip("."), ""
        best = best if best is not None else amt
    if best is not None:
        return f"{best:.2f}".rstrip("0").rstrip("."), ""
    # Дошли сюда — значит `balances` ответил, но суммы в строках нет. Прежний
    # текст «панель не показала сумму» выбрасывал разбор страницы магазина,
    # где как раз перечислены найденные поля, — то есть ровно то, ради чего
    # эту причину и печатают. Показываем обе попытки и то, что в строках было.
    seen = []
    for row in rows[:3]:
        fields = [str(f.get("name") or f.get("attribute"))
                  for f in ((row.get("raw") or {}).get("fields") or [])
                  if isinstance(f, dict)]
        seen.append(f"строка {row.get('id')}: "
                    + (", ".join(n for n in fields if n) or "без полей"))
    return None, (f"магазин: {shop_err} · balances: строк {len(rows)}, "
                  "суммы ни в одной; " + "; ".join(seen))[:400]


def _parse_check(data: dict) -> tuple[str, str, str | None]:
    """Parse /check response → (name, balance, pending)."""
    logger.info("CHECK raw response: %s", data)
    shop = data.get("shop") or data.get("data") or data
    if not isinstance(shop, dict):
        shop = data
    srcs = (shop, data)

    # The shop name sits as deep as the balance does — a one-level lookup
    # reported «—» for a shop the response plainly names.
    name = (_pick(srcs, "name", "shop_name", "title")
            or _deep_find(data, ("name", "shop_name", "title")) or "—")
    balance = _deep_find(data, _MONEY_KEYS)
    pending = _deep_find(data, _PENDING_KEYS)

    # Empty, not "0": a balance the response never carried is not a balance of
    # zero, and showing zero for it reads as «денег нет» instead of «не нашёл».
    bal_str = f"{balance:.2f}".rstrip("0").rstrip(".") if balance is not None else ""
    pend = _money(pending) if pending is not None else None
    pend_str = (f"{pend:.2f}".rstrip("0").rstrip(".")) if pend else None
    return str(name), bal_str, pend_str


from orderfields import DONE as _DONE
from orderfields import money as _RUB       # 1234 → «1 234», 1234.5 → «1 234,5»


def _shown(value) -> str:
    """Сумму на экран — с разрядами. «586226 ₽» читается пересчётом цифр.

    Разбор и показ разведены нарочно: наружу из `_panel_balance` уходит
    голое число, которое ещё будут превращать во `float` перед выводом
    средств. Пробел в разрядах там сломал бы `float`, и баланс стал бы
    нулём — «выводить нечего» при полном счёте.
    """
    from orderfields import parse_amount
    num = parse_amount(value)
    return _RUB(num) if num is not None else str(value)


def _earned(details: dict, known: dict, since: float) -> tuple[int, int]:
    """(completed sales, revenue ₽) over orders first seen at/after `since`.

    Reads what the order poller already stores, so the balance screen can show
    what came in today and this week without another round-trip to the API.
    """
    sales = revenue = 0
    for oid, det in details.items():
        seen = det.get("seen_at", 0)
        if not seen or seen < since:
            continue
        st = str(known.get(oid) or known.get(str(oid)) or "")
        if st not in _DONE:
            continue
        sales += 1
        try:
            revenue += int(float(str(det.get("price", 0))))
        except (ValueError, TypeError):
            pass
    return sales, revenue


def _bump_spent(s: dict, since: float) -> float:
    """Promotion spend to subtract from revenue. Only today's is tracked
    precisely; for the week it is the best figure available."""
    bs = s.get("bump_schedule", {})
    if bs.get("spent_day") == _lt.today_str(s):
        return float(bs.get("spent_today", 0) or 0)
    return 0.0


def _activity_block(uid: int) -> str:
    s = get_settings(uid)
    details = s.get("known_order_details", {})
    known = s.get("known_orders", {})
    now = time.time()
    from stats_source import day_start as _day_start
    day_start = _day_start(now, s)
    week_start = now - 7 * 86400

    d_sales, d_rev = _earned(details, known, day_start)
    w_sales, w_rev = _earned(details, known, week_start)
    d_spent = _bump_spent(s, day_start)

    lines = ["", "📊 <b>Продажи</b>"]
    net = d_rev - d_spent
    today = f"├ Сегодня: <b>{_RUB(d_rev)} ₽</b> ({d_sales})"
    if d_spent:
        today += f"  ·  чистыми {_RUB(net)} ₽"
    lines.append(today)
    lines.append(f"└ 7 дней: <b>{_RUB(w_rev)} ₽</b> ({w_sales})")
    return "\n".join(lines)


def _kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💸 Вывести средства", callback_data="balance:withdraw")
    b.button(text="📋 Активные выводы", callback_data="balance:active")
    b.button(text="📜 История выводов", callback_data="balance:history")
    b.button(text="📊 Статистика", callback_data="menu:stats")
    b.button(text="🔔 Порог уведомления", callback_data="balance:threshold")
    b.button(text="💳 Реквизиты вывода", callback_data="balance:auto")
    b.button(text="🔄 Обновить", callback_data="menu:balance")
    b.button(text="⬅️ Главное меню", callback_data="menu:main")
    b.adjust(2, 2, 1, 2)
    return b.as_markup()


@router.callback_query(F.data == "menu:balance")
async def show_balance(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.message.edit_text("⏳ Загружаю баланс...")
    if not api:
        await callback.message.edit_text(
            "❌ Нужен API-токен. Отправьте /start и введите токен.",
            reply_markup=back_keyboard())
        await callback.answer()
        return
    name = "Магазин"
    balance = None
    pending = None
    raw = None
    err = None
    try:
        data = await api.check()
        raw = data
        name, balance, pending = _parse_check(data)
    except Exception as e:
        err = str(e)[:200]
        logger.error("Balance check error: %s", e)

    # If /check didn't give a usable number, try the dedicated balance endpoints
    if balance in (None, "", "0", "—"):
        try:
            amount, bal_str = await api.get_balance()
            if bal_str not in ("—", None):
                balance = bal_str.replace(" ₽", "")
        except Exception as e:
            if err is None:
                err = str(e)[:200]

    # /check answers {status, shop:{id,title}, integration:{…}, ts} — an auth
    # probe with no money in it, which is why the balance always read zero.
    # The panel is where the figure lives, and where withdrawal already reads
    # it from.
    panel_err = ""
    if balance in (None, "", "—"):
        panel_bal, panel_err = await _panel_balance(callback.from_user.id)
        if panel_bal is not None:
            balance = panel_bal

    if balance in (None, "", "—"):
        # Neither source answered. Report both attempts, not just the API's
        # shape: the previous version hid the panel's reason behind a message
        # about /check, so two rounds went by without knowing what the panel
        # said. The build stamp is here for the same reason it is on the
        # restore screen — output from a stale container looks identical.
        from handlers.start import BOT_VERSION
        from storage import get_panel_creds
        has_panel = bool((get_panel_creds(callback.from_user.id) or {}).get("cookies"))
        lines = [f"💰 <b>Баланс</b>  <code>{BOT_VERSION}</code>", ""]
        lines.append("<b>API:</b> в ответе нет баланса — это только проверка "
                     "доступа:")
        lines.append(f"<code>{_esc(_shape(raw))[:400]}</code>" if raw
                     else f"<code>{_esc(err or 'нет ответа')[:200]}</code>")
        lines.append("")
        lines.append(f"<b>Панель:</b> {'вход есть' if has_panel else '❌ вход не выполнен'}"
                     + (f" — {_esc(panel_err)[:200]}" if panel_err else ""))
        if not has_panel:
            lines.append("<i>Баланс есть только в панели. Войдите: "
                         "«Настройки» → «Панель продавца».</i>")
        await callback.message.edit_text("\n".join(lines), reply_markup=_kb())
        await callback.answer()
        return

    if balance not in (None, "", "—"):
        text = (
            f"🏪 <b>{name}</b>\n\n"
            f"💰 Баланс: <b>{_shown(balance)} ₽</b>\n"
        )
        if pending:
            text += f"⏳ В ожидании: <b>{_shown(pending)} ₽</b>\n"
        # Tie the number to what produced it — a balance with no sales context
        # is just a figure
        text += _activity_block(callback.from_user.id)
        threshold = get_settings(callback.from_user.id).get(
            "balance_notify", {})
        if threshold.get("enabled"):
            text += (f"\n\n🔔 Уведомлю при достижении "
                     f"<b>{_RUB(float(threshold.get('threshold', 0) or 0))} ₽</b>")
        kb = _kb()
    else:
        # Nothing usable — show diagnostics so the real response shape is visible
        text = "❌ <b>Не удалось получить баланс</b>\n\n"
        if err:
            text += f"Ошибка: <code>{err}</code>\n\n"
        if raw is not None:
            text += f"Ответ API <code>/check</code>:\n<code>{str(raw)[:400]}</code>"
        else:
            text += "API не ответил. Проверьте токен и попробуйте снова."
        kb = _kb()

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ── Вывод средств ───────────────────────────────────────────────────────────

def _payout_ready(uid: int) -> bool:
    """Настроен ли способ выплаты. Без него выводить нечем."""
    aw = get_settings(uid).get("auto_withdraw", {})
    return bool(aw.get("panel_balance_id") and aw.get("panel_values"))


def _setup_first_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⚙️ Настроить вывод", callback_data="balance:auto")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(1)
    return b.as_markup()


async def _ask_amount(message, uid: int, api: YooMarketAPI,
                      state: FSMContext) -> None:
    """Экран «сколько вывести». Деньги уходят через панель — в API вывода нет.

    Прежние кнопки ходили в /withdraw, /wallet/withdraw и ещё три пути,
    которых у Юмаркета не существует: вывод всегда заканчивался ошибкой,
    хотя рядом, в мастере, тот же вывод работал через панель.
    """
    from tasks.manager import shop_balance
    if not _payout_ready(uid):
        await message.edit_text(
            "💸 <b>Вывод средств</b>\n\n"
            "Сначала нужно указать, <b>куда</b> выводить: способ выплаты и "
            "реквизиты. Бот прочитает форму вывода прямо из панели.",
            reply_markup=_setup_first_kb())
        return
    _amount, bal_str = await shop_balance(uid, api)
    await state.set_state(WithdrawState.waiting_panel_amount)
    b = InlineKeyboardBuilder()
    b.button(text="💸 Вывести всё", callback_data="balance:withdraw_all")
    b.button(text="❌ Отмена", callback_data="menu:balance")
    b.adjust(1)
    await message.edit_text(
        # `shop_balance` отдаёт строку уже со знаком рубля. Второй здесь
        # давал «586226 ₽ ₽».
        f"💸 <b>Вывод средств</b>\n\nДоступно: <b>{bal_str}</b>\n\n"
        "Пришлите сумму в ₽ или нажмите «Вывести всё»:",
        reply_markup=b.as_markup())


@router.callback_query(F.data == "balance:withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext,
                         api: YooMarketAPI) -> None:
    await _ask_amount(callback.message, callback.from_user.id, api, state)
    await callback.answer()


def _log_withdrawal(uid: int, amount: float, wtype: str, ok: bool) -> None:
    s = get_settings(uid)
    hist = s.setdefault("withdrawal_history", [])
    hist.insert(0, {
        "amount": amount, "ts": time.time(), "type": wtype,
        "status": "requested" if ok else "failed",
    })
    del hist[100:]  # keep last 100
    save_settings(uid, s)


@router.callback_query(F.data == "balance:withdraw_all")
async def withdraw_all(callback: CallbackQuery, state: FSMContext,
                       api: YooMarketAPI) -> None:
    """«Всё» — это столько, сколько панель показывает на счету."""
    from tasks.manager import shop_balance
    await state.clear()
    uid = callback.from_user.id
    if not _payout_ready(uid):
        await callback.message.edit_text(
            "💸 <b>Вывод средств</b>\n\nСначала настройте способ выплаты.",
            reply_markup=_setup_first_kb())
        await callback.answer()
        return
    await callback.answer("⏳ Смотрю баланс…")
    amount, bal_str = await shop_balance(uid, api)
    if not amount or amount < 1:
        # Ноль на экране подтверждения — заявка в никуда.
        await callback.message.edit_text(
            f"💸 <b>Вывод средств</b>\n\nНа счету <b>{bal_str}</b> — "
            "выводить нечего.",
            reply_markup=_setup_first_kb())
        return
    await _confirm_payout(callback.message, uid, int(amount))


# ── Порог уведомления о балансе ─# ── Порог уведомления о балансе ──────────────────────────────────────────────

@router.callback_query(F.data == "balance:threshold")
async def threshold_start(callback: CallbackQuery, state: FSMContext) -> None:
    s = get_settings(callback.from_user.id)
    bn = s.get("balance_notify", {})
    cur = bn.get("threshold", 1000)
    on = bn.get("enabled", False)
    await state.set_state(WithdrawState.waiting_threshold)
    b = InlineKeyboardBuilder()
    if on:
        b.button(text="🔕 Выключить уведомление", callback_data="balance:threshold_off")
    b.button(text="❌ Отмена", callback_data="menu:balance")
    b.adjust(1)
    await callback.message.edit_text(
        f"🔔 <b>Порог уведомления о балансе</b>\n\n"
        f"Сейчас: {'включено, ' + str(cur) + ' ₽' if on else 'выключено'}\n\n"
        f"Пришлите сумму — бот напишет, когда баланс её достигнет, "
        f"чтобы вовремя вывести средства.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "balance:threshold_off")
async def threshold_off(callback: CallbackQuery, state: FSMContext, api: YooMarketAPI) -> None:
    await state.clear()
    s = get_settings(callback.from_user.id)
    s.setdefault("balance_notify", {})["enabled"] = False
    save_settings(callback.from_user.id, s)
    await callback.answer("Уведомление выключено", show_alert=True)
    await show_balance(callback, api)


@router.message(WithdrawState.waiting_threshold)
async def threshold_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите сумму числом, например: <b>1000</b>")
        return
    await state.clear()
    s = get_settings(message.from_user.id)
    bn = s.setdefault("balance_notify", {})
    bn["threshold"] = amount
    bn["enabled"] = True
    # Reset the arm so an already-high balance still triggers once next tick,
    # instead of being treated as "already notified".
    bn["last_notified_balance"] = 0.0
    save_settings(message.from_user.id, s)
    b = InlineKeyboardBuilder()
    b.button(text="💰 К балансу", callback_data="menu:balance")
    await message.answer(
        f"✅ Уведомлю, когда баланс достигнет <b>{_RUB(amount)} ₽</b>.",
        reply_markup=b.as_markup())


# ── Реквизиты вывода ─────────────────────────────────────────────────────────
#
# Автовывод убран: деньги переводит человек, а не расписание. Здесь остаётся
# то, без чего ручной вывод невозможен, — способ выплаты и реквизиты,
# прочитанные из формы самой панели.

def _auto_text(s: dict) -> str:
    aw = s.get("auto_withdraw", {})
    vals = aw.get("panel_values") or {}
    ready = bool(aw.get("panel_balance_id") and vals)
    lines = ["💳 <b>Реквизиты вывода</b>\n"]
    if ready:
        masked = ", ".join(
            f"{k}={_mask(v) if k in ('card', 'wallet', 'phone', 'steam_wallet') else v}"
            for k, v in vals.items() if k != "system")
        lines.append("Статус: 🟢 настроены")
        lines.append(f"Куда: {_esc(masked) or '—'}")
    else:
        lines.append("Статус: 🔴 не настроены")
        lines.append("")
        lines.append("Вывод идёт через панель — у Юмаркета нет вывода через "
                     "API. Бот прочитает форму выплаты прямо из панели и "
                     "спросит только то, что в ней есть.")
    lim = aw.get("limits") or {}
    if lim.get("max"):
        lines.append(f"Потолок одной выплаты: {_RUB(float(lim['max']))} ₽")
    if aw.get("last_result"):
        lines.append(f"\nПоследнее: {_esc(aw['last_result'])}")
    lines.append("\n<i>Автоматического вывода нет: заявку отправляете вы.</i>")
    return "\n".join(lines)


def _mask(value) -> str:
    """Показать реквизит, не показывая его целиком.

    Экран подтверждения выплаты нужен, чтобы заметить чужую карту, а не
    чтобы светить свою в переписке.
    """
    s = str(value or "")
    if len(s) <= 6:
        return s
    return f"{s[:4]}…{s[-4:]}"


def _esc(t) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _auto_kb(s: dict) -> InlineKeyboardMarkup:
    aw = s.get("auto_withdraw", {})
    ready = bool(aw.get("panel_balance_id") and aw.get("panel_values"))
    b = InlineKeyboardBuilder()
    b.button(text="⚙️ Настроить вывод через панель",
             callback_data="balance:auto_setup")
    if ready:
        b.button(text="💸 Вывести сейчас", callback_data="wd:manual")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data == "balance:auto")
async def auto_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    s = get_settings(callback.from_user.id)
    await callback.message.edit_text(_auto_text(s), reply_markup=_auto_kb(s))
    await callback.answer()


@router.callback_query(F.data == "balance:auto_setup")
async def auto_setup(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the withdrawal wizard: pick a balance to withdraw from."""
    from automation.panel import panel_balances_sync
    await state.clear()
    await callback.answer("⏳")
    await callback.message.edit_text("⏳ Читаю счета из панели...")
    res, err = await _run_panel(callback.from_user.id, panel_balances_sync)
    ok = isinstance(res, list)
    if not ok:
        b = InlineKeyboardBuilder()
        b.button(text="⬅️ Назад", callback_data="balance:auto")
        await callback.message.edit_text(
            f"❌ Не удалось прочитать счета: {_esc(err or res)}",
            reply_markup=b.as_markup())
        return
    # Keep only what the next step needs — the full Nova rows in "raw" are large
    # and would bloat the FSM state for no reason.
    slim = [{"id": bl["id"], "currency": bl.get("currency", ""),
             "amount": bl.get("amount", "")} for bl in res]
    await state.update_data(wd_balances=slim)
    b = InlineKeyboardBuilder()
    for i, bl in enumerate(slim):
        cur = (bl.get("currency") or "").split("  ")[0][:28]
        b.button(text=f"💳 {cur} · {bl.get('amount') or '—'}",
                 callback_data=f"wd:bal:{i}")
    b.button(text="⬅️ Назад", callback_data="balance:auto")
    b.adjust(1)
    await callback.message.edit_text(
        "⚙️ <b>Вывод через панель</b>\n\n"
        "С какого счёта выводить? Обычно это <b>Баланс Магазина</b> "
        "(счёт для продаж).",
        reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("wd:bal:"))
async def wd_pick_balance(callback: CallbackQuery, state: FSMContext) -> None:
    from automation.panel import panel_withdraw_fields_sync
    data = await state.get_data()
    balances = data.get("wd_balances") or []
    try:
        bl = balances[int(callback.data.split(":")[-1])]
    except (ValueError, IndexError):
        await callback.answer("Список устарел, начните заново", show_alert=True)
        return
    await callback.answer("⏳")
    await callback.message.edit_text("⏳ Читаю форму вывода...")
    res, err = await _run_panel(callback.from_user.id,
                                panel_withdraw_fields_sync, bl["id"])
    if not isinstance(res, dict):
        await callback.message.edit_text(
            f"❌ {_esc(err or res)}",
            reply_markup=_one_btn("⬅️ Назад", "balance:auto"))
        return
    sys_field = next((f for f in res["fields"] if f["attribute"] == "system"), None)
    if not sys_field or not sys_field["options"]:
        await callback.message.edit_text(
            "❌ Панель не отдала список способов вывода. Возможно, вывод "
            "закрыт, пока профиль не прошёл проверку.",
            reply_markup=_one_btn("⬅️ Назад", "balance:auto"))
        return
    await state.update_data(wd_balance_id=bl["id"], wd_action=res["key"],
                            wd_systems=sys_field["options"], wd_values={})
    b = InlineKeyboardBuilder()
    for i, o in enumerate(sys_field["options"][:20]):
        b.button(text=str(o["label"])[:40], callback_data=f"wd:sys:{i}")
    b.button(text="❌ Отмена", callback_data="balance:auto")
    b.adjust(1)
    await callback.message.edit_text(
        "💸 <b>Способ вывода</b>\n\n"
        "Выберите, куда выводить средства:",
        reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("wd:sys:"))
async def wd_pick_system(callback: CallbackQuery, state: FSMContext) -> None:
    from automation.panel import panel_withdraw_fields_sync
    data = await state.get_data()
    systems = data.get("wd_systems") or []
    try:
        sysopt = systems[int(callback.data.split(":")[-1])]
    except (ValueError, IndexError):
        await callback.answer("Начните заново", show_alert=True)
        return
    await callback.answer(str(sysopt["label"])[:60])
    await callback.message.edit_text("⏳ Уточняю, какие нужны реквизиты...")
    # Re-read the form with the system chosen — the panel then marks the
    # requisites that method needs as visible.
    res, err = await _run_panel(callback.from_user.id,
                                panel_withdraw_fields_sync,
                                data["wd_balance_id"], {"system": sysopt["value"]})
    reqs = []
    if isinstance(res, dict):
        for f in res["fields"]:
            if (f["visible"] and not f["skip"] and not f["is_amount"]
                    and f["attribute"] != "system"):
                reqs.append(f)
    if not reqs:
        # Fallback map if the re-read did not reshape visibility
        reqs = _fallback_reqs(str(sysopt["label"]), res)
    values = {"system": sysopt["value"]}
    # Carried in state: the option lookup below needs the seller's panel
    # session, and the step that performs it only has the message to hand.
    await state.update_data(wd_values=values, wd_system_label=str(sysopt["label"]),
                            wd_queue=reqs, wd_qi=0, wd_uid=callback.from_user.id)
    await _wd_ask_next(callback.message, state)


def _fallback_reqs(system_label: str, res) -> list:
    """If Nova did not reshape the form, ask for the requisites the chosen
    method obviously needs, by its own field labels."""
    lab = system_label.lower()
    fields = {f["attribute"]: f for f in (res.get("fields") if isinstance(res, dict) else [])}
    want: list[str] = []
    if "карт" in lab or "card" in lab:
        want = ["card"]
    elif "steam" in lab:
        want = ["steam_wallet"]
    elif "сбп" in lab or "sbp" in lab:
        want = ["bank_id", "phone"]
    elif any(t in lab for t in ("usdt", "trc", "крипт", "crypto")):
        want = ["wallet"]
    return [fields[a] for a in want if a in fields]


async def _wd_ask_next(message, state: FSMContext) -> None:
    data = await state.get_data()
    queue = data.get("wd_queue") or []
    qi = data.get("wd_qi", 0)
    # Skip past any field already answered
    while qi < len(queue) and queue[qi]["attribute"] in (data.get("wd_values") or {}):
        qi += 1
    if qi >= len(queue):
        await _wd_finish(message, state)
        return
    f = queue[qi]
    await state.update_data(wd_qi=qi)
    label = _esc(f["label"])
    hint = f"\n<i>{_esc(f['help'])}</i>" if f.get("help") else ""

    # A dependent select ships with `options: []` until the choices it depends
    # on are known — «Банк» arrives empty and only fills in once the payment
    # system is picked. Without asking for them the wizard fell through to a
    # text prompt and made the seller type a bank name by hand. The promotion
    # flow already resolves dependent options this way; withdrawal now does too.
    if (not f["options"] and f.get("component", "").endswith("select-field")
            and not f.get("_looked_up")):
        f["_looked_up"] = True
        from storage import get_panel_creds
        from automation.panel import panel_action_field_options_sync
        creds = get_panel_creds(data.get("wd_uid") or message.chat.id) or {}
        if creds.get("cookies"):
            await message.edit_text(f"⏳ Узнаю список: {label}…")
            try:
                opts, _trace = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, panel_action_field_options_sync,
                        creds["cookies"], str(data.get("wd_balance_id") or ""),
                        str(data.get("wd_action") or ""), f["attribute"],
                        dict(data.get("wd_values") or {}),
                        f.get("component_key") or "", f.get("depends_on") or {}),
                    timeout=45)
                f["options"] = opts or []
            except Exception as e:
                logger.info("Withdraw options for %s: %s", f["attribute"], e)
        queue[qi] = f
        await state.update_data(wd_queue=queue)

    if f["options"]:
        # Indices point into the full list, never into a filtered view: the
        # answer handler resolves them against `f["options"]`.
        query = (data.get("wd_filter") or "").strip().lower()
        shown = [(i, o) for i, o in enumerate(f["options"])
                 if not query or query in str(o["label"]).lower()]
        total = len(f["options"])
        # The SBP bank list runs well past thirty entries, and cutting it there
        # silently hid every bank after the thirtieth — «Яндекс Банк» among
        # them. What does not fit is reachable by name instead of lost.
        clipped = shown[:_WD_MAX_BUTTONS]

        b = InlineKeyboardBuilder()
        for i, o in clipped:
            b.button(text=str(o["label"])[:40], callback_data=f"wd:opt:{i}")
        rows = [1] * len(clipped)
        if total > len(clipped) or query:
            b.button(text="🔎 Найти по названию", callback_data="wd:find")
            rows.append(1)
        if query:
            b.button(text="↩️ Весь список", callback_data="wd:findoff")
            rows.append(1)
        if not f["required"]:
            b.button(text="⏭ Пропустить", callback_data="wd:skip")
            rows.append(1)
        b.button(text="❌ Отмена", callback_data="balance:auto")
        rows.append(1)
        b.adjust(*rows)

        head = f"💳 <b>{label}</b>{hint}\n\n"
        if query:
            head += (f"По запросу «{_esc(query)}»: {len(shown)} из {total}.\n"
                     if shown else
                     f"По запросу «{_esc(query)}» ничего нет ({total} всего).\n")
        elif total > len(clipped):
            head += f"Показаны первые {len(clipped)} из {total}.\n"
        await message.edit_text(head + "Выберите:", reply_markup=b.as_markup())
    else:
        await state.set_state(WithdrawState.waiting_req_value)
        b = InlineKeyboardBuilder()
        if not f["required"]:
            b.button(text="⏭ Пропустить", callback_data="wd:skip")
        b.button(text="❌ Отмена", callback_data="balance:auto")
        b.adjust(1)
        ph = f.get("placeholder")
        await message.edit_text(
            f"✍️ <b>{label}</b>{hint}\n\n"
            f"Пришлите значение{f' ({_esc(ph)})' if ph else ''}:",
            reply_markup=b.as_markup())


_WD_MAX_BUTTONS = 30


@router.callback_query(F.data == "wd:find")
async def wd_find(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for a few letters, so a long list is reachable by name."""
    await state.set_state(WithdrawState.waiting_option_search)
    b = InlineKeyboardBuilder()
    b.button(text="↩️ Весь список", callback_data="wd:findoff")
    await callback.message.edit_text(
        "🔎 Введите часть названия — например «яндекс»:",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(WithdrawState.waiting_option_search)
async def wd_find_apply(message: Message, state: FSMContext) -> None:
    await state.update_data(wd_filter=(message.text or "").strip())
    await state.set_state(None)
    sent = await message.answer("⏳")
    await _wd_ask_next(sent, state)


@router.callback_query(F.data == "wd:findoff")
async def wd_find_off(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(wd_filter="")
    await state.set_state(None)
    await _wd_ask_next(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("wd:opt:"))
async def wd_pick_option(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    queue = data.get("wd_queue") or []
    qi = data.get("wd_qi", 0)
    if qi >= len(queue):
        await callback.answer("Начните заново", show_alert=True)
        return
    f = queue[qi]
    try:
        o = f["options"][int(callback.data.split(":")[-1])]
    except (ValueError, IndexError):
        await callback.answer("Ошибка выбора", show_alert=True)
        return
    values = dict(data.get("wd_values") or {})
    values[f["attribute"]] = o["value"]
    await callback.answer(str(o["label"])[:40])
    # Cleared with the step: a filter typed for the bank list must not carry
    # over and hide most of the next field's choices.
    await state.update_data(wd_values=values, wd_qi=qi + 1, wd_filter="")
    await _wd_ask_next(callback.message, state)


@router.callback_query(F.data == "wd:skip")
async def wd_skip(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(None)
    await callback.answer("Пропущено")
    await state.update_data(wd_qi=data.get("wd_qi", 0) + 1)
    await _wd_ask_next(callback.message, state)


@router.message(WithdrawState.waiting_req_value)
async def wd_req_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    queue = data.get("wd_queue") or []
    qi = data.get("wd_qi", 0)
    if qi >= len(queue):
        await state.clear()
        await message.answer("Начните заново: «Настроить вывод».")
        return
    f = queue[qi]
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Пришлите значение или нажмите «Пропустить».")
        return
    # Number fields carry only digits; keep text ones as typed
    if str(f.get("component", "")).startswith("number") or f.get("attribute") in (
            "card", "phone", "link_id"):
        cleaned = "".join(ch for ch in raw if ch.isdigit())
        val = cleaned or raw
    else:
        val = raw
    values = dict(data.get("wd_values") or {})
    values[f["attribute"]] = val
    await state.set_state(None)
    await state.update_data(wd_values=values, wd_qi=qi + 1)
    await _wd_ask_next(message, state)


async def _wd_finish(message, state: FSMContext) -> None:
    """Requisites gathered — save the method and show what was configured."""
    data = await state.get_data()
    uid = message.chat.id
    s = get_settings(uid)
    aw = s.setdefault("auto_withdraw", {})
    aw["method"] = "panel"
    aw["panel_balance_id"] = data.get("wd_balance_id", "")
    aw["panel_action_key"] = data.get("wd_action", "")
    aw["panel_values"] = data.get("wd_values", {})   # system + requisites, no amount
    save_settings(uid, s)
    await state.clear()

    lines = ["✅ <b>Способ вывода сохранён</b>\n",
             f"Способ: <b>{_esc(data.get('wd_system_label') or '—')}</b>"]
    for k, v in (data.get("wd_values") or {}).items():
        if k == "system":
            continue
        shown = _mask(v) if k in ("card", "wallet", "phone", "steam_wallet") else _esc(str(v))
        lines.append(f"• {_esc(k)}: {shown}")
    lines.append("\nТеперь можно вывести сумму вручную или включить автовывод.")
    b = InlineKeyboardBuilder()
    b.button(text="💸 Вывести сейчас", callback_data="wd:manual")
    b.button(text="🤖 К автовыводу", callback_data="balance:auto")
    b.button(text="💰 Баланс", callback_data="menu:balance")
    b.adjust(1, 2)
    await message.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data == "wd:manual")
async def wd_manual_start(callback: CallbackQuery, state: FSMContext) -> None:
    s = get_settings(callback.from_user.id)
    aw = s.get("auto_withdraw", {})
    if not (aw.get("panel_balance_id") and aw.get("panel_values")):
        await callback.answer("Сначала настройте способ вывода", show_alert=True)
        return
    await state.set_state(WithdrawState.waiting_panel_amount)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data="balance:auto")
    await callback.message.edit_text(
        "💸 <b>Сумма вывода</b>\n\nПришлите сумму в ₽:",
        reply_markup=b.as_markup())
    await callback.answer()


async def _confirm_payout(message, uid: int, amount: int) -> None:
    """Последний экран перед реальными деньгами: сумма и куда."""
    aw = get_settings(uid).get("auto_withdraw", {})
    vals = aw.get("panel_values") or {}
    masked = ", ".join(
        f"{k}={_mask(v) if k in ('card', 'wallet', 'phone', 'steam_wallet') else v}"
        for k, v in vals.items() if k != "system")
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Вывести {_RUB(amount)} ₽", callback_data=f"wd:go:{amount}")
    b.button(text="❌ Отмена", callback_data="menu:balance")
    b.adjust(1)
    send = getattr(message, "edit_text", None) or message.answer
    await send(
        f"⚠️ <b>Подтвердите вывод</b>\n\n"
        # Разряды именно здесь важнее всего: экран последний перед деньгами,
        # и «5862 ₽» от «58620 ₽» без пробела отличаются одним взглядом.
        f"Сумма: <b>{_RUB(amount)} ₽</b>\n"
        f"Реквизиты: {_esc(masked) or '—'}\n\n"
        f"Заявка уйдёт в панель. Проверьте реквизиты — ошибку не отменить.",
        reply_markup=b.as_markup())


@router.message(WithdrawState.waiting_panel_amount)
async def wd_manual_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = int(float(raw))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите сумму числом, например: <b>500</b>")
        return
    await state.clear()
    await _confirm_payout(message, message.from_user.id, amount)


@router.callback_query(F.data.startswith("wd:go:"))
async def wd_execute(callback: CallbackQuery, api: YooMarketAPI) -> None:
    from automation.panel import panel_withdraw_sync
    try:
        amount = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    uid = callback.from_user.id
    s = get_settings(uid)
    aw = s.get("auto_withdraw", {})
    values = {**(aw.get("panel_values") or {}), "amount": amount}
    # Отметка времени до запроса: по ней потом отличается свежая заявка от
    # старой, уже лежавшей в журнале.
    started = time.time()
    await callback.answer("⏳ Оформляю вывод...")
    await callback.message.edit_text("⏳ Отправляю заявку на вывод...")
    res, err = await _run_panel(uid, panel_withdraw_sync,
                                aw.get("panel_balance_id"),
                                aw.get("panel_action_key"), values, uid, True)
    # _run_panel already unpacks (ok, payload): success gives (payload, ""),
    # failure gives (None, reason). Re-testing for a tuple here made every
    # outcome read as failure — a payout that went through was reported «❌ не
    # удалось» with no reason, inviting a second attempt at real money.
    ok = err == "" and res is not None
    msg = str(res) if ok else (err or "панель не ответила")
    _log_withdrawal(uid, float(amount), "manual", bool(ok))
    b = InlineKeyboardBuilder()
    b.button(text="📜 История", callback_data="balance:history")
    b.button(text="💰 Баланс", callback_data="menu:balance")
    b.adjust(2)
    tail = await _payout_readback(uid, started) if ok else ""
    await callback.message.edit_text(
        (f"✅ {_esc(msg)}{tail}" if ok else f"❌ {_esc(msg)}"),
        reply_markup=b.as_markup())


async def _payout_readback(uid: int, since: float) -> str:
    """Видно ли заявку в журнале панели — перечитыванием, а не по коду ответа.

    Первое правило проекта: HTTP 200 не доказательство. Здесь оно дороже
    всего — «бот сказал «отправлено», а в панели пусто» это чужие деньги.
    Но и обратное враньё запрещено: заявка на выплату баланс сразу не
    уменьшает, она ждёт модерации. Поэтому три исхода названы по-разному,
    и «не вижу» не выдаётся за «не создана».
    """
    from stats_source import events_for, invalidate
    invalidate(uid)
    try:
        events, source, perr = await events_for(uid, force=True)
    except Exception as e:
        return ("\n\n<i>Журнал панели перечитать не удалось "
                f"({_esc(str(e)[:60])}) — проверьте раздел выплат сами.</i>")
    if source != "panel":
        return ("\n\n<i>Журнал панели недоступен"
                + (f" ({_esc(perr[:60])})" if perr else "")
                + " — заявку проверьте в панели сами.</i>")
    fresh = [e for e in events
             if e.get("kind") == "payout" and float(e.get("ts") or 0) >= since]
    if fresh:
        return "\n\n✅ Заявка видна в журнале панели."
    return ("\n\n<i>В журнале панели заявка пока не видна. Так бывает: она "
            "появляется не сразу. Если через несколько минут её там нет — "
            "вывод не создан, и это надо решать в панели.</i>")


def _one_btn(text: str, cb: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=cb)
    return b.as_markup()


async def _safe_edit(message, text: str, reply_markup=None) -> None:
    """Edit a message, surviving both Telegram's 4096 limit and a rejected
    edit — a message that fails to update is what makes the bot look hung."""
    text = text[:4000]
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            await message.answer(text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("safe_edit failed: %s", e)


# ── Активные выводы ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "balance:active")
async def active_withdrawals(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer("⏳")
    items = []
    if api:
        try:
            items = await api.get_withdrawals()
        except Exception as e:
            logger.warning("get_withdrawals: %s", e)
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="balance:active")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(2)
    active_st = ("pending", "processing", "new", "in_progress", "wait", "created")
    active = [w for w in items
              if str(w.get("status", "")).lower() in active_st]
    if active:
        lines = [f"📋 <b>Активные выводы</b> ({len(active)})\n"]
        for w in active[:20]:
            amt = w.get("amount") or w.get("sum") or "—"
            st = w.get("status", "—")
            lines.append(f"• <b>{amt} ₽</b> — {st}")
        text = "\n".join(lines)
    elif items:
        text = "📋 <b>Активные выводы</b>\n\nНет выводов в обработке."
    else:
        text = ("📋 <b>Активные выводы</b>\n\n"
                "API не отдаёт список выводов. Смотрите статус в панели "
                "или в «Истории выводов» бота.")
    await callback.message.edit_text(text, reply_markup=b.as_markup())


# ── История выводов ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "balance:history")
async def withdrawal_history(callback: CallbackQuery, api: YooMarketAPI) -> None:
    await callback.answer()
    uid = callback.from_user.id
    # prefer the API history; fall back to the local log
    api_items = []
    if api:
        try:
            api_items = await api.get_withdrawals()
        except Exception:
            pass
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="balance:history")
    b.button(text="⬅️ Баланс", callback_data="menu:balance")
    b.adjust(2)
    if api_items:
        lines = [f"📜 <b>История выводов</b> ({len(api_items)})\n"]
        for w in api_items[:25]:
            amt = w.get("amount") or w.get("sum") or "—"
            st = w.get("status", "—")
            date = w.get("created_at") or w.get("date") or ""
            date = str(date)[:16]
            lines.append(f"• <b>{amt} ₽</b> — {st}  {date}")
        text = "\n".join(lines)
    else:
        hist = get_settings(uid).get("withdrawal_history", [])
        if not hist:
            text = "📜 <b>История выводов</b>\n\nВыводов пока не было."
        else:
            lines = [f"📜 <b>История выводов</b> ({len(hist)})\n"
                     "<i>(локальный журнал бота)</i>\n"]
            s = get_settings(callback.from_user.id)
            for w in hist[:25]:
                amt = w.get("amount", 0)
                amt_str = f"{_RUB(amt)} ₽" if amt else "всё"
                wtype = "🤖" if w.get("type") == "auto" else "✋"
                st = "✅" if w.get("status") == "requested" else "❌"
                try:
                    date = _lt.fmt(w.get("ts", 0), s)
                except Exception:
                    date = ""
                lines.append(f"{st} {wtype} <b>{amt_str}</b>  {date}")
            text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=b.as_markup())
