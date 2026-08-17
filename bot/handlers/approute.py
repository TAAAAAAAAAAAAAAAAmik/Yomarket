"""Поставщик AppRoute: ввод ключа и диагностика каталога.

Поставщика выбрали здесь, и с него теперь начинается автовыдача Robux. Но
порядок тот же, что и всегда в этом проекте: сначала факты, потом выдача. Ни
одна команда в этом файле ничего не покупает — пока не видно, есть ли у
поставщика Roblox, под каким `itemId` он лежит и каких полей требует заказ,
писать выдачу значит гадать. За этим следит отдельный тест.

Ключ — единственный секрет: он один даёт право тратить баланс кабинета.
Сообщение с ним удаляется сразу после разбора, в отчётах показывается длина,
а не значение.

Кабинета два — `approute.io` и `approute.ru`, — и ключ от одного в другом не
работает. Регион переключается кнопкой, потому что иначе «ключ не принят»
означает и «ключ не тот», и «кабинет не тот», а различить их продавцу нечем.
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

from storage import (ar_fields, delete_ar_creds, encryption_on, get_ar_creds,
                     save_ar_creds)

router = Router()
logger = logging.getLogger(__name__)

# Сколько товаров и номиналов печатать. Ограничение Telegram — 4096 символов
# на сообщение, и обрезанный на середине каталог хуже короткого.
_MAX_PRODUCTS = 8
_MAX_ITEMS = 12

_REGIONS = {
    "io": ("🌍 approute.io", "международный кабинет"),
    "ru": ("🇷🇺 approute.ru", "российский кабинет"),
}

# Как продавец может назвать ключ, копируя его из кабинета или из письма.
_ALIASES = {
    "api_key": ("api_key", "apikey", "key", "x-api-key", "ключ", "апиключ",
                "api-key", "token", "токен"),
    "region": ("region", "регион", "кабинет", "домен"),
}


class ARState(StatesGroup):
    key = State()
    search = State()


def parse_creds(text: str) -> dict:
    """Ключ из вольного текста: `ключ: значение`, либо просто сам ключ.

    Ключ у AppRoute один, поэтому вставку без подписи понимаем как ключ:
    требовать `api_key:` перед единственным значением — требовать
    аккуратности там, где угадать нечего.

    По виду ключ не проверяется намеренно. В README у поставщика примеры
    вида `sk_live_…`, а настоящий ключ из кабинета оказался совсем другой
    формы (`wli-…`, 43 символа). Проверка «похоже ли на ключ» отвергала бы
    ровно то, что поставщик и выдал.
    """
    out: dict = {}
    text = str(text or "").strip()
    for line in re.split(r"[\n;]+", text):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*([A-Za-zА-Яа-я_\-]+)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        name, value = m.group(1).strip().lower(), m.group(2).strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'«»":
            value = value[1:-1].strip()
        if not value:
            continue
        for field, names in _ALIASES.items():
            if name in names:
                out[field] = value
                break
    if not out and text and not re.search(r"\s", text):
        # Одно слово без подписи — это ключ и есть.
        out["api_key"] = text
    if out.get("region"):
        region = out["region"].lower()
        out["region"] = "ru" if "ru" in region or "рф" in region or "рос" in region else "io"
    return out


def _missing(creds: dict) -> list[str]:
    return [f for f in ar_fields() if not str(creds.get(f) or "").strip()]


def _creds_or_hint(uid: int) -> tuple[dict, str]:
    creds = get_ar_creds(uid)
    if _missing(creds):
        return {}, ("⚠️ Ключ AppRoute не задан.\n"
                    "Введите его командой <code>/apr_login</code> или кнопкой "
                    "«🔑 Поставщик AppRoute» в разделе AutoRoblox.")
    return creds, ""


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@router.message(Command("apr_login"))
async def apr_login(message: Message) -> None:
    """Сохранить ключ. Сообщение с ним сразу удаляется."""
    _cmd, _, tail = (message.text or "").partition(" ")
    got = parse_creds(tail)
    # Удаляем в любом случае: даже неразобранное сообщение содержало ключ.
    try:
        await message.delete()
    except Exception:
        pass

    if not got:
        await message.answer(
            "🔑 <b>Ключ AppRoute</b>\n\n"
            "Пришлите одной командой:\n\n"
            "<code>/apr_login ваш_ключ</code>\n\n"
            "Ключ берётся в кабинете: approute.io/dashboard — "
            "международный, approute.ru/dashboard — российский. "
            "Если кабинет российский, добавьте второй строкой "
            "<code>регион: ru</code>.\n\n"
            "Ваше сообщение удалю сразу после разбора.")
        return

    save_ar_creds(message.from_user.id, got)
    creds = get_ar_creds(message.from_user.id)
    lines = [f"✅ Принял: {', '.join(sorted(got))}"]
    if _missing(creds):
        lines.append("⚠️ Ключ так и не задан — в присланном его не нашлось.")
    else:
        region = str(creds.get("region") or "io")
        lines.append(f"Кабинет: <b>{_REGIONS.get(region, _REGIONS['io'])[0]}</b>")
        lines.append("Дальше — <code>/apr_stock robux</code>: покажет, что у "
                     "поставщика есть и почём, ничего не покупая.")
    await message.answer("\n".join(lines))


@router.message(Command("apr_forget"))
async def apr_forget(message: Message) -> None:
    delete_ar_creds(message.from_user.id)
    await message.answer("🗑 Ключ AppRoute удалён.")


@router.message(Command("apr_balance"))
async def apr_balance(message: Message) -> None:
    """Баланс кабинета. Только чтение."""
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    status = await message.answer("⏳ Спрашиваю баланс…")
    _ok, text = await _balance_text(creds)
    await status.edit_text(text)


@router.message(Command("apr_stock"))
async def apr_stock(message: Message) -> None:
    """Каталог поставщика. Только чтение, ничего не покупает."""
    parts = (message.text or "").split(maxsplit=1)
    needle = parts[1].strip() if len(parts) > 1 else "roblox"
    await _catalog_report(message, message.from_user.id, needle)


@router.message(Command("apr_debug"))
async def apr_debug(message: Message) -> None:
    """Сырой ответ поставщика по обоим кабинетам. Только чтение."""
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    status = await message.answer("⏳ Спрашиваю оба кабинета…")
    await _probe_report(status, creds)


@router.message(Command("apr_whoami"))
async def apr_whoami(message: Message) -> None:
    """Кем нас видит поставщик и с какого IP. Только чтение."""
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    status = await message.answer("⏳ Спрашиваю, кем нас видит поставщик…")
    await status.edit_text(await _whoami_text(creds))


# ---------------------------------------------------------------------------
# Отчёты — общие для команд и кнопок
# ---------------------------------------------------------------------------

async def _whoami_text(creds: dict) -> str:
    """`/whoami` словами: он же показывает IP, который видит поставщик.

    У AppRoute белый список адресов, а бот живёт на Railway, где адрес
    меняется при каждом деплое. Когда ключ «не сработал», это первое, что
    надо посмотреть, — и узнать это можно только отсюда, с самого сервера.
    """
    from automation.approute import whoami_sync

    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, whoami_sync, creds), timeout=60)
    except Exception as e:
        return f"❌ {html.escape(str(e)[:300])}"
    if not ok:
        return (f"❌ {html.escape(str(data)[:600])}\n\n"
                f"<i>Если здесь сказано про адрес или права — сверьте белый "
                f"список IP в кабинете: командой из кабинета он проверяется "
                f"так же, как этой кнопкой.</i>")
    lines = ["🪪 <b>Поставщик отвечает:</b>"]
    if isinstance(data, dict):
        for k, v in list(data.items())[:15]:
            lines.append(f"• {html.escape(str(k))}: "
                         f"<code>{html.escape(str(v)[:120])}</code>")
        ip = str(data.get("ip") or data.get("clientIp") or
                 data.get("remoteAddr") or "")
        if ip:
            lines.append("")
            lines.append(f"📍 Наш адрес глазами поставщика: <code>{html.escape(ip)}</code>")
            lines.append("Он должен стоять в белом списке кабинета. На Railway "
                         "адрес меняется при деплое — если ключ перестал "
                         "работать после выката, причина обычно здесь.")
    else:
        lines.append(f"<code>{html.escape(str(data)[:500])}</code>")
    return "\n".join(lines)


async def _probe_report(target, creds: dict) -> None:
    """Что именно отвечают оба кабинета — фактами, без нашей трактовки.

    Написана после живого отказа 17.08: «Ключ не сработал · HTTP 200». Тело
    пришло, `traceId` в нём был, а поля `code` не было вовсе — то есть форма
    ответа не та, что описана в SDK. Разбирать такое по догадкам в этом
    проекте уже стоило дней.
    """
    from automation.approute import probe_sync

    say = target.edit_text if hasattr(target, "edit_text") else target.answer
    loop = asyncio.get_event_loop()
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(None, probe_sync, creds), timeout=120)
    except Exception as e:
        await say(f"❌ {html.escape(str(e)[:300])}")
        return
    lines = ["🔎 <b>Что отвечает AppRoute</b>",
             "<i>Только чтение. Ключ в отчёт не попадает.</i>", ""]
    for row in rows:
        head = f"<b>approute.{row['region']}{html.escape(row['path'])}</b>"
        if row["error"]:
            lines.append(f"{head} — не достучались: {html.escape(row['error'])}")
            lines.append("")
            continue
        lines.append(f"{head} — HTTP <b>{row['http']}</b>")
        if not row["json"]:
            lines.append(f"   ответ не JSON: <code>{html.escape(row['excerpt'])}</code>")
        else:
            lines.append(f"   поля тела: <code>{html.escape(', '.join(row['keys']) or 'пусто')}</code>")
            # Значения важнее имён: в `statusMessage` поставщик словами
            # пишет, что не так, и без этого отчёт отвечает «форма не та»,
            # молча о причине.
            for name, value in (row.get("fields") or {}).items():
                lines.append(f"   {html.escape(name)}: "
                             f"<code>{html.escape(str(value))}</code>")
            if row.get("data"):
                lines.append(f"   data: <code>{html.escape(str(row['data']))}</code>")
            if row["trace"]:
                lines.append(f"   traceId: <code>{html.escape(row['trace'])}</code>")
        lines.append("")
    lines.append("Строка <code>statusMessage</code> — это то, что поставщик "
                 "говорит о запросе своими словами; <code>errorCode</code> — "
                 "его же название отказа. С ними и <code>traceId</code> можно "
                 "идти в их поддержку.")
    await say("\n".join(lines)[:4000])



async def _balance_text(creds: dict) -> tuple[bool, str]:
    """Баланс словами. Он же проверка ключа: чтение, денег не тратит."""
    from automation.approute import balance_lines, balance_sync

    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, balance_sync, creds), timeout=60)
    except Exception as e:
        return False, f"❌ {html.escape(str(e)[:200])}"
    if not ok:
        return False, f"❌ {html.escape(str(data)[:500])}"
    lines = balance_lines(data)
    if not lines:
        # Пустой список счетов — это не ноль на балансе, а «поставщик не
        # назвал ни одного счёта». Разница видна только если сказать прямо.
        return True, ("✅ Ключ принят, но ни одного счёта поставщик не вернул.\n"
                      "Возможно, кабинет ещё не пополняли.")
    body = "\n".join(f"• {html.escape(l)}" for l in lines)
    return True, f"💰 <b>Баланс у поставщика:</b>\n{body}"


async def _catalog_report(target, uid: int, needle: str) -> None:
    """Каталог: что есть, под каким id, почём и какие поля требует заказ.

    Это и есть тот самый живой ответ, ради которого всё написано: в SDK и
    OpenAPI списка товаров нет, и на вопрос «есть ли у AppRoute Roblox и
    дешевле ли он, чем у ns.gifts» отвечает только он.
    """
    from automation.approute import find_products, services_sync, truncated

    say = target.edit_text if hasattr(target, "edit_text") else target.answer
    creds, hint = _creds_or_hint(uid)
    if hint:
        await say(hint)
        return
    needle = str(needle or "").strip()
    await say("⏳ Читаю каталог поставщика"
              + (f" (ищу «{html.escape(needle)}»)…" if needle else "…"))
    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, services_sync, creds), timeout=90)
    except Exception as e:
        await say(f"❌ {html.escape(str(e)[:200])}")
        return
    if not ok:
        await say(f"❌ {html.escape(str(data)[:500])}")
        return

    total = len(find_products(data, ""))
    found = find_products(data, needle)
    lines = ["📦 <b>Каталог AppRoute</b>", f"Товаров всего: <b>{total}</b>"]
    if truncated(data):
        # Промолчать нельзя: «Роблокса нет» и «нам прислали не весь список» —
        # разные ответы, а постраничного чтения у поставщика не описано.
        lines.append("⚠️ Поставщик отдал <b>не весь</b> список (hasNext), "
                     "а дочитать его нечем: постраничного чтения каталога "
                     "нет ни в SDK, ни в описании API.")
    lines.append("")

    if not found:
        lines += [f"❌ По слову «{html.escape(needle)}» ничего не нашлось.",
                  "",
                  "Попробуйте другое слово или посмотрите весь каталог."]
        await say("\n".join(lines))
        return

    lines.append(f"Нашёл{f' по «{html.escape(needle)}»' if needle else ''}: "
                 f"<b>{len(found)}</b>")
    for product in found[:_MAX_PRODUCTS]:
        lines += _product_lines(product)
    if len(found) > _MAX_PRODUCTS:
        lines.append("")
        lines.append(f"…и ещё {len(found) - _MAX_PRODUCTS}. Уточните слово поиска.")
    await say("\n".join(lines)[:4000])


def _product_lines(product: dict) -> list[str]:
    """Один товар в отчёте: id, номиналы с ценой и схема полей заказа.

    Печатается как пришло. Угадывание имён полей в этом проекте уже стоило
    дня на Fragment, а здесь за каждый заказ платят деньгами.
    """
    where = " / ".join(str(product.get(k) or "") for k in
                       ("categoryName", "subcategoryName") if product.get(k))
    out = ["",
           f"🏷 <b>{html.escape(str(product.get('name') or '—'))}</b>"]
    if where:
        out.append(f"   раздел: {html.escape(where)}")
    kind = str(product.get("type") or "")
    kind_ru = {"voucher": "код (ваучер)",
               "direct_topup": "прямое пополнение счёта"}.get(kind, kind or "—")
    out.append(f"   вид: {html.escape(kind_ru)}")
    out.append(f"   <code>serviceId={html.escape(str(product.get('id')))}</code>")

    items = [i for i in (product.get("items") or []) if isinstance(i, dict)]
    if not items:
        out.append("   номиналы: <i>поставщик их не назвал</i>")
    for item in items[:_MAX_ITEMS]:
        mark = "✅" if item.get("available") else "⛔"
        stock = item.get("stock")
        stock_part = f" · остаток {stock}" if stock is not None else ""
        out.append(
            f"   {mark} {html.escape(str(item.get('name') or item.get('nominal') or '—'))}"
            f" — {item.get('price')} {html.escape(str(item.get('currency') or ''))}"
            f"{stock_part}")
        out.append(f"      <code>itemId={html.escape(str(item.get('id')))}</code>")
    if len(items) > _MAX_ITEMS:
        out.append(f"   …и ещё {len(items) - _MAX_ITEMS} номиналов")

    fields = [f for f in (product.get("fields") or []) if isinstance(f, dict)]
    if fields:
        names = ", ".join(
            f"{f.get('key')}"
            + (f" ({f.get('type')})" if f.get("type") else "")
            + ("*" if f.get("required") else "")
            for f in fields)
        out.append(f"   поля заказа: <code>{html.escape(names)}</code>")
    else:
        out.append("   поля заказа: <i>товар их не описал</i>")
    return out


# ---------------------------------------------------------------------------
# Экран доступа внутри плагина AutoRoblox — то же самое, но кнопками
# ---------------------------------------------------------------------------

def _creds_kb(creds: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    got = bool(str(creds.get("api_key") or "").strip())
    b.button(text=f"{'✅' if got else '▫️'} 🔑 API-ключ", callback_data="apr:set")
    region = str(creds.get("region") or "io")
    other = "ru" if region == "io" else "io"
    b.button(text=f"Кабинет: {_REGIONS[region][0]} → сменить",
             callback_data=f"apr:region:{other}")
    if got:
        # Кнопка, которая заведомо не сработает, — то же обещание
        # невозможного, что и совет ответить в закрытый чат.
        b.button(text="🧪 Проверить ключ", callback_data="apr:check")
        b.button(text="📦 Каталог", callback_data="apr:stock")
        b.button(text="🪪 Наш IP у поставщика", callback_data="apr:whoami")
        b.button(text="🔎 Что отвечает сервер", callback_data="apr:debug")
        b.button(text="🗑 Удалить ключ", callback_data="apr:del")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(1, 1, 2, 2, 1, 1)
    return b.as_markup()


def _creds_text(creds: dict) -> str:
    key = str(creds.get("api_key") or "").strip()
    region = str(creds.get("region") or "io")
    lines = ["🔑 <b>Доступ к поставщику AppRoute</b>", ""]
    if key:
        # Значение не показываем даже частично: этот ключ — право тратить
        # баланс кабинета целиком. Длина говорит «то самое вставилось» и не
        # выдаёт ничего.
        lines.append(f"✅ Ключ: задан, {len(key)} символов")
    else:
        lines.append("▫️ Ключ: <i>не задан</i>")
    label, about = _REGIONS.get(region, _REGIONS["io"])
    lines.append(f"🌍 Кабинет: <b>{label}</b> — {about}")
    lines.append("")
    if key:
        lines.append("«🧪 Проверить ключ» спросит баланс — это только чтение, "
                     "денег не тратит.")
        lines.append("«📦 Каталог» покажет, есть ли Roblox, под каким itemId "
                     "и почём.")
        lines.append("«🪪 Наш IP у поставщика» — на случай белого списка: "
                     "адрес сервера меняется при каждом выкате.")
        lines.append("")
        lines.append("Если ключ не проходит, причин обычно три: истёк срок "
                     "(временный ключ живёт 48 часов), наш адрес не в белом "
                     "списке или ключ не из того кабинета. Что именно — "
                     "скажет «🔎 Что отвечает сервер».")
    else:
        lines.append("Ключ берётся в кабинете: <b>approute.io/dashboard</b> "
                     "(международный) или <b>approute.ru/dashboard</b> "
                     "(российский). Если не знаете, какой выбрать, — вводите "
                     "ключ и жмите «🔎 Что отвечает сервер»: он спросит оба "
                     "и покажет, где ключ приняли.")
    lines.append("")
    # Обещать шифрование, когда ключа для него нет, — то же враньё, что
    # «✅ Пак поднят · Поднято: 0». На Railway `SECRET_KEY` до сих пор не
    # задан, и продавец должен видеть это здесь, а не узнавать из /version.
    if encryption_on():
        lines.append("<i>Ключ хранится зашифрованным. Сообщение с ним удаляю "
                     "сразу после разбора.</i>")
    else:
        lines.append("⚠️ <i>Ключ ляжет в базу <b>в открытом виде</b>: на "
                     "сервере не задана переменная <code>SECRET_KEY</code>. "
                     "Задайте её — шифрование включится само при следующем "
                     "сохранении. Сообщение с ключом удаляю в любом случае.</i>")
    lines.append("<i>Прежний поставщик ns.gifts никуда не делся: его каталог "
                 "открывается командой /ns_stock — на случай сравнения цены.</i>")
    return "\n".join(lines)


async def _show_creds(target, uid: int, note: str = "") -> None:
    creds = get_ar_creds(uid)
    text = _creds_text(creds)
    if note:
        text = f"{note}\n\n{text}"
    kb = _creds_kb(creds)
    # По наличию экрана, а не по типу: нажатие кнопки правит своё сообщение,
    # ответ на введённое значение приходит новым. `isinstance` завязал бы
    # проверяемый экран на внутренние классы aiogram.
    screen = getattr(target, "message", None)
    if screen is not None and hasattr(screen, "edit_text"):
        await screen.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "apr:creds")
async def apr_creds_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_creds(callback, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("apr:region:"))
async def apr_region(callback: CallbackQuery) -> None:
    region = callback.data.split(":")[-1]
    save_ar_creds(callback.from_user.id,
                  {"region": "ru" if region == "ru" else "io"})
    await _show_creds(callback, callback.from_user.id, "🌍 Кабинет переключён.")
    await callback.answer()


@router.callback_query(F.data == "apr:set")
async def apr_set_key(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ARState.key)
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Отмена", callback_data="apr:creds")
    await callback.message.edit_text(
        "✏️ <b>API-ключ AppRoute</b>\n\n"
        "Кабинет → <b>Dashboard</b> → раздел с ключами. Длинная строка; "
        "в примерах поставщика она выглядит как <code>sk_live_…</code>, "
        "но выдают и другого вида — присылайте как есть.\n\n"
        "⚠️ Временный ключ (на 48 часов) и постоянный — <b>разные</b> ключи. "
        "Если проверка не проходит, стоит попробовать второй: у них разные "
        "правила доступа.\n\n"
        "Пришлите ключ одним сообщением — удалю сразу после разбора.",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(ARState.key)
async def apr_key_input(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    await state.clear()
    # Удаляем всегда: даже не подошедшее значение было ключом.
    try:
        await message.delete()
    except Exception:
        pass
    got = parse_creds(value)
    if not got.get("api_key"):
        await _show_creds(message, message.from_user.id,
                          "⚠️ Пустое значение — ничего не изменил.")
        return
    save_ar_creds(message.from_user.id, {"api_key": got["api_key"]})
    await _show_creds(message, message.from_user.id, "✅ Ключ сохранён.")


@router.callback_query(F.data == "apr:check")
async def apr_check(callback: CallbackQuery) -> None:
    """Проверка ключа — спрашиваем баланс. Только чтение."""
    creds, hint = _creds_or_hint(callback.from_user.id)
    if hint:
        await callback.answer("Ключ ещё не задан", show_alert=True)
        return
    await callback.answer("⏳ Проверяю…")
    await callback.message.edit_text("⏳ Спрашиваю баланс у поставщика…")
    ok, text = await _balance_text(creds)
    note = (f"✅ <b>Ключ принят.</b>\n{text}" if ok
            else (f"❌ <b>Ключ не сработал.</b>\n{text}\n\n"
                  f"Дальше по порядку: «🪪 Наш IP у поставщика» — стоит ли "
                  f"наш адрес в белом списке; «🔎 Что отвечает сервер» — что "
                  f"именно приходит из обоих кабинетов."))
    await _show_creds(callback, callback.from_user.id, note)


@router.callback_query(F.data == "apr:whoami")
async def apr_whoami_button(callback: CallbackQuery) -> None:
    creds, hint = _creds_or_hint(callback.from_user.id)
    if hint:
        await callback.answer("Ключ ещё не задан", show_alert=True)
        return
    await callback.answer("⏳ Спрашиваю…")
    await callback.message.edit_text("⏳ Спрашиваю, кем нас видит поставщик…")
    await _show_creds(callback, callback.from_user.id, await _whoami_text(creds))


@router.callback_query(F.data == "apr:debug")
async def apr_debug_button(callback: CallbackQuery) -> None:
    creds, hint = _creds_or_hint(callback.from_user.id)
    if hint:
        await callback.answer("Ключ ещё не задан", show_alert=True)
        return
    await callback.answer("⏳ Спрашиваю оба кабинета…")
    await callback.message.edit_text("⏳ Спрашиваю оба кабинета…")
    await _probe_report(callback.message, creds)


@router.callback_query(F.data == "apr:del")
async def apr_delete(callback: CallbackQuery) -> None:
    delete_ar_creds(callback.from_user.id)
    await _show_creds(callback, callback.from_user.id, "🗑 Ключ удалён.")
    await callback.answer()


@router.callback_query(F.data == "apr:stock")
async def apr_stock_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ARState.search)
    b = InlineKeyboardBuilder()
    b.button(text="🎮 roblox", callback_data="apr:find:roblox")
    b.button(text="🎮 robux", callback_data="apr:find:robux")
    b.button(text="📜 Весь каталог", callback_data="apr:find:")
    b.button(text="⬅️ Назад", callback_data="apr:creds")
    b.adjust(2, 1, 1)
    await callback.message.edit_text(
        "📦 <b>Каталог поставщика</b>\n\n"
        "Выберите слово поиска или пришлите своё одним сообщением.\n\n"
        "<i>Только чтение: показывает serviceId, itemId, цену и поля, "
        "которые требует заказ. Ничего не покупает.</i>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("apr:find:"))
async def apr_find_by_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    needle = callback.data.split(":", 2)[-1]
    await callback.answer("⏳ Читаю каталог…")
    await _catalog_report(callback.message, callback.from_user.id, needle)


@router.message(ARState.search)
async def apr_find_by_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _catalog_report(message, message.from_user.id,
                          (message.text or "").strip())
