"""Поставщик ns.gifts: ввод доступа и диагностика каталога.

Сначала диагностика, потом выдача — как со всем непонятным в этом проекте.
Здесь нет ни одной покупки: команды только читают и печатают факты. Пока не
видно, под каким `service_id` у поставщика лежит Roblox и каких полей он
требует, писать выдачу значит гадать.

Секреты в чате не задерживаются: сообщение с доступом удаляется сразу после
разбора, а в отчёты попадает только наличие («задан / не задан»), но не
значения. Пароль и ключ вместе дают право тратить баланс кабинета.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from storage import delete_ns_creds, get_ns_creds, ns_fields, save_ns_creds

router = Router()
logger = logging.getLogger(__name__)


class NSState(StatesGroup):
    one_field = State()
    bulk = State()
    stock_word = State()


# Подпись поля и подсказка, где его взять. Подсказка обязательна: «введите
# user_id» человеку, который первый раз открыл кабинет поставщика, не
# говорит ничего.
_FIELDS = (
    ("user_id", "Номер аккаунта",
     "Числовой <b>user_id</b>. Кабинет ns.gifts → раздел <b>Profile</b>."),
    ("login", "Логин",
     "Логин, который выдал оператор при регистрации."),
    ("password", "Пароль",
     "Пароль от кабинета. По нему бот получает токен на два часа."),
    ("api_secret", "API-ключ",
     "Длинная строка от оператора, выдаётся <b>один раз</b> при регистрации. "
     "Посмотреть повторно нельзя — если потеряли, оператор пересоздаёт."),
)

# Как продавец может назвать каждое поле. Пишут и «user_id», и «айди», и
# «id» — разбирать одно написание значит отправить человека угадывать.
_ALIASES = {
    "user_id": ("user_id", "userid", "user", "id", "айди", "юзер"),
    "login": ("login", "логин", "username"),
    "password": ("password", "pass", "пароль"),
    "api_secret": ("api_secret", "apisecret", "secret", "секрет", "ключ",
                   "api-secret"),
}


def parse_creds(text: str) -> dict:
    """Доступ из вольного текста: `ключ: значение` по строке или через `=`.

    Формат намеренно свободный. Продавец копирует данные из письма оператора,
    где они лежат как попало, и требовать точного порядка значит требовать
    аккуратности в тот момент, когда человек уже устал.
    """
    out: dict = {}
    for line in re.split(r"[\n;]+", str(text or "")):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*([A-Za-zА-Яа-я_\-]+)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        name, value = m.group(1).strip().lower(), m.group(2).strip()
        # Значение могли обернуть в кавычки — снимаем, но только парные.
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'«»":
            value = value[1:-1].strip()
        if not value:
            continue
        for field, names in _ALIASES.items():
            if name in names:
                out[field] = value
                break
    return out


def _have(creds: dict) -> list[str]:
    return [f for f in ns_fields() if not str(creds.get(f) or "").strip()]


@router.message(Command("ns_login"))
async def ns_login(message: Message) -> None:
    """Сохранить доступ к поставщику. Сообщение с ним сразу удаляется."""
    text = (message.text or "")
    _cmd, _, tail = text.partition(" ")
    got = parse_creds(tail)
    # Удаляем в любом случае: даже неразобранное сообщение содержало пароль.
    try:
        await message.delete()
    except Exception:
        pass

    if not got:
        await message.answer(
            "🔑 <b>Доступ к ns.gifts</b>\n\n"
            "Пришлите одной командой, каждое поле со своей строки:\n\n"
            "<code>/ns_login\n"
            "user_id: 1234\n"
            "login: ваш_логин\n"
            "password: ваш_пароль\n"
            "api_secret: ключ-из-письма-оператора</code>\n\n"
            "Порядок и написание любые — понимаю и «секрет», и «пароль».\n"
            "Ваше сообщение удалю сразу после разбора.")
        return

    save_ns_creds(message.from_user.id, got)
    creds = get_ns_creds(message.from_user.id)
    left = _have(creds)
    lines = [f"✅ Принял: {', '.join(sorted(got))}"]
    if left:
        lines.append(f"⚠️ Ещё не задано: <b>{', '.join(left)}</b>")
    else:
        lines.append("Всё на месте. Дальше — <code>/ns_stock robux</code>: "
                     "покажет, что у поставщика есть, ничего не покупая.")
    await message.answer("\n".join(lines))


@router.message(Command("ns_forget"))
async def ns_forget(message: Message) -> None:
    delete_ns_creds(message.from_user.id)
    await message.answer("🗑 Доступ к ns.gifts удалён.")


def _creds_or_hint(uid: int) -> tuple[dict, str]:
    creds = get_ns_creds(uid)
    left = _have(creds)
    if left:
        return {}, (f"⚠️ Не задано: <b>{', '.join(left)}</b>\n"
                    f"Заполните командой <code>/ns_login</code>.")
    return creds, ""


@router.message(Command("ns_balance"))
async def ns_balance(message: Message) -> None:
    """Баланс кабинета у поставщика. Только чтение."""
    from automation.nsgifts import balance_sync

    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    status = await message.answer("⏳ Спрашиваю баланс…")
    loop = asyncio.get_event_loop()
    try:
        ok, value = await asyncio.wait_for(
            loop.run_in_executor(None, balance_sync, creds), timeout=60)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    if not ok:
        await status.edit_text(f"❌ {html.escape(str(value)[:400])}")
        return
    await status.edit_text(f"💰 <b>Баланс у поставщика:</b> {html.escape(str(value))} USD")


@router.message(Command("ns_stock"))
async def ns_stock(message: Message) -> None:
    """Каталог поставщика. Только чтение, ничего не покупает."""
    parts = (message.text or "").split(maxsplit=1)
    needle = parts[1].strip() if len(parts) > 1 else "robux"
    await _stock_report(message, message.from_user.id, needle)


async def _stock_report(target, uid: int, needle: str) -> None:
    """Отчёт по каталогу — один на команду и на кнопку.

    С него начинается автовыдача Robux: в документации поставщика Роблокса
    нет ни в одном примере, и под каким `service_id` он лежит, какие поля
    требует и сколько стоит — знает только живой ответ `/stock`.
    """
    from automation.nsgifts import find_services, stock_sync

    creds, hint = _creds_or_hint(uid)
    say = target.edit_text if hasattr(target, "edit_text") else target.answer
    if hint:
        await say(hint)
        return
    needle = str(needle or "").strip()
    await say(f"⏳ Читаю каталог поставщика"
              + (f" (ищу «{html.escape(needle)}»)…" if needle else "…"))
    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, stock_sync, creds), timeout=90)
    except Exception as e:
        await say(f"❌ {html.escape(str(e)[:200])}")
        return
    if not ok:
        await say(f"❌ {html.escape(str(data)[:400])}")
        return

    cats = (data or {}).get("categories") or []
    found = find_services(data, needle)
    lines = ["📦 <b>Каталог ns.gifts</b>",
             f"Категорий: <b>{len(cats)}</b> · "
             f"услуг всего: <b>{len(find_services(data, ''))}</b>",
             ""]
    if not found:
        lines += [f"❌ По слову «{html.escape(needle)}» ничего не нашлось.",
                  "",
                  "Попробуйте другое слово или посмотрите весь каталог."]
        await say("\n".join(lines))
        return

    lines.append(f"Нашёл{f' по «{html.escape(needle)}»' if needle else ''}: "
                 f"<b>{len(found)}</b>")
    for svc in found[:12]:
        lines.append("")
        lines.append(f"🏷 <b>{html.escape(str(svc.get('service_name') or '—'))}</b>")
        lines.append(f"   категория: {html.escape(str(svc.get('category_name') or '—'))}")
        lines.append(f"   <code>service_id={svc.get('service_id')}</code> · "
                     f"цена {svc.get('price')} {svc.get('currency') or ''} · "
                     f"остаток {svc.get('in_stock')}")
        # Схема полей — то, без чего заказ не составить. Печатаем как есть:
        # угадывать имена полей в этом проекте уже стоило дня.
        fields = svc.get("fields") or []
        if fields:
            names = ", ".join(
                f"{f.get('key')}"
                + (f" ({f.get('type')})" if f.get("type") else "")
                + ("*" if f.get("required") else "")
                for f in fields if isinstance(f, dict))
            lines.append(f"   поля заказа: <code>{html.escape(names)}</code>")
        else:
            lines.append("   поля заказа: <i>категория их не описала</i>")
    if len(found) > 12:
        lines.append("")
        lines.append(f"…и ещё {len(found) - 12}. Уточните слово поиска.")
    await say("\n".join(lines)[:4000])


# ---------------------------------------------------------------------------
# Экран доступа внутри плагина AutoRoblox — то же самое, но кнопками.
# ---------------------------------------------------------------------------

def _creds_kb(creds: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (name, label, _hint) in enumerate(_FIELDS):
        mark = "✅" if str(creds.get(name) or "").strip() else "▫️"
        b.button(text=f"{mark} {label}", callback_data=f"ns:set:{i}")
    b.button(text="📋 Вставить всё строкой", callback_data="ns:bulk")
    ready = not _have(creds)
    if ready:
        b.button(text="🧪 Проверить вход", callback_data="ns:check")
        b.button(text="📦 Каталог", callback_data="ns:stock")
        b.button(text="🗑 Удалить доступ", callback_data="ns:del")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(1, 1, 1, 1, 1, 2, 1, 1)
    return b.as_markup()


def _creds_text(creds: dict) -> str:
    left = _have(creds)
    lines = ["🔑 <b>Доступ к поставщику ns.gifts</b>", ""]
    for name, label, _hint in _FIELDS:
        got = str(creds.get(name) or "").strip()
        if not got:
            lines.append(f"▫️ {label}: <i>не задан</i>")
        elif name in ("password", "api_secret"):
            # Значение не показываем даже частично: это право тратить
            # баланс кабинета. Длина говорит «то самое вставилось» и не
            # выдаёт ничего.
            lines.append(f"✅ {label}: задан, {len(got)} символов")
        else:
            lines.append(f"✅ {label}: <code>{html.escape(got)}</code>")
    lines.append("")
    if left:
        lines.append("Заполните оставшееся — по кнопке на каждое поле или "
                     "всё сразу одной вставкой.")
    else:
        lines.append("Всё на месте. «🧪 Проверить вход» спросит баланс — "
                     "это только чтение, денег не тратит.")
    lines.append("")
    lines.append("<i>Пароль и ключ хранятся зашифрованными. Сообщения с ними "
                 "удаляю сразу после разбора.</i>")
    return "\n".join(lines)


async def _show_creds(target, uid: int, note: str = "") -> None:
    creds = get_ns_creds(uid)
    text = _creds_text(creds)
    if note:
        text = f"{note}\n\n{text}"
    kb = _creds_kb(creds)
    # По наличию экрана, а не по типу: нажатие кнопки правит своё сообщение,
    # ответ на введённое значение приходит новым. Проверка `isinstance` здесь
    # была бы завязана на внутренние классы aiogram — а этот экран должен
    # оставаться проверяемым.
    screen = getattr(target, "message", None)
    if screen is not None and hasattr(screen, "edit_text"):
        await screen.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ns:creds")
async def ns_creds_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_creds(callback, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("ns:set:"))
async def ns_set_field(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        idx = int(callback.data.split(":")[-1])
        name, label, hint = _FIELDS[idx]
    except (ValueError, IndexError):
        await callback.answer()
        return
    await state.set_state(NSState.one_field)
    await state.update_data(ns_field=name)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Отмена", callback_data="ns:creds")
    await callback.message.edit_text(
        f"✏️ <b>{label}</b>\n\n{hint}\n\nПришлите значение одним сообщением.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(NSState.one_field)
async def ns_one_field_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    name = str(data.get("ns_field") or "")
    value = (message.text or "").strip()
    await state.clear()
    # Удаляем всегда: в поле мог быть пароль, и даже неподходящее значение
    # оставлять в переписке незачем.
    try:
        await message.delete()
    except Exception:
        pass
    if not name or not value:
        await _show_creds(message, message.from_user.id,
                          "⚠️ Пустое значение — ничего не изменил.")
        return
    save_ns_creds(message.from_user.id, {name: value})
    label = next((l for n, l, _h in _FIELDS if n == name), name)
    await _show_creds(message, message.from_user.id, f"✅ {label} сохранён.")


@router.callback_query(F.data == "ns:bulk")
async def ns_bulk_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NSState.bulk)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Отмена", callback_data="ns:creds")
    await callback.message.edit_text(
        "📋 <b>Всё сразу</b>\n\n"
        "Пришлите одним сообщением, каждое поле со своей строки:\n\n"
        "<code>user_id: 1234\n"
        "login: ваш_логин\n"
        "password: ваш_пароль\n"
        "api_secret: ключ-от-оператора</code>\n\n"
        "Порядок и написание любые — понимаю и «пароль», и «секрет». "
        "Сообщение удалю сразу после разбора.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(NSState.bulk)
async def ns_bulk_input(message: Message, state: FSMContext) -> None:
    got = parse_creds(message.text or "")
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    if not got:
        await _show_creds(message, message.from_user.id,
                          "❌ Не разобрал ни одного поля. Нужен вид "
                          "<code>поле: значение</code>.")
        return
    save_ns_creds(message.from_user.id, got)
    await _show_creds(message, message.from_user.id,
                      f"✅ Принял полей: {len(got)}.")


@router.callback_query(F.data == "ns:check")
async def ns_check(callback: CallbackQuery) -> None:
    """Проверка входа — спрашиваем баланс. Только чтение, денег не тратит."""
    from automation.nsgifts import balance_sync

    creds, hint = _creds_or_hint(callback.from_user.id)
    if hint:
        await callback.answer("Заполнены не все поля", show_alert=True)
        return
    await callback.answer("⏳ Проверяю…")
    await callback.message.edit_text("⏳ Вхожу к поставщику и спрашиваю баланс…")
    loop = asyncio.get_event_loop()
    try:
        ok, value = await asyncio.wait_for(
            loop.run_in_executor(None, balance_sync, creds), timeout=60)
    except Exception as e:
        ok, value = False, f"не достучались: {str(e)[:120]}"
    note = (f"✅ <b>Вход работает.</b> Баланс: <b>{html.escape(str(value))} USD</b>"
            if ok else f"❌ <b>Вход не прошёл:</b> {html.escape(str(value)[:300])}")
    await _show_creds(callback, callback.from_user.id, note)


@router.callback_query(F.data == "ns:del")
async def ns_delete(callback: CallbackQuery) -> None:
    delete_ns_creds(callback.from_user.id)
    await _show_creds(callback, callback.from_user.id, "🗑 Доступ удалён.")
    await callback.answer()


@router.callback_query(F.data == "ns:stock")
async def ns_stock_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NSState.stock_word)
    b = InlineKeyboardBuilder()
    b.button(text="🎮 robux", callback_data="ns:stock_word:robux")
    b.button(text="🎮 roblox", callback_data="ns:stock_word:roblox")
    b.button(text="📜 Весь каталог", callback_data="ns:stock_word:")
    b.button(text="⬅️ Назад", callback_data="ns:creds")
    b.adjust(2, 1, 1)
    await callback.message.edit_text(
        "📦 <b>Каталог поставщика</b>\n\n"
        "Выберите слово поиска или пришлите своё одним сообщением.\n\n"
        "<i>Только чтение: показывает service_id, цену, остаток и поля, "
        "которые требует заказ. Ничего не покупает.</i>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("ns:stock_word:"))
async def ns_stock_by_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    needle = callback.data.split(":", 2)[-1]
    await callback.answer("⏳ Читаю каталог…")
    await _stock_report(callback.message, callback.from_user.id, needle)


@router.message(NSState.stock_word)
async def ns_stock_by_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _stock_report(message, message.from_user.id,
                        (message.text or "").strip())
