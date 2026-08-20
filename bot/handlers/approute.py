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
    "proxy": ("proxy", "прокси", "proxy_url"),
}


class ARState(StatesGroup):
    key = State()
    search = State()
    proxy = State()


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


@router.message(Command("apr_item"))
async def apr_item(message: Message) -> None:
    """Цена и остаток одного номинала — тем же путём, каким их читает выдача.

    Заменяет `/apr_dry_check`, отменённую по существу: сухого прогона для
    магазинных заказов не существует — `checkOnly` разрешён только при
    `ordersType=dtu`, а Robux это `voucher`. Команда, отвечавшая на вопрос
    «сухой ли наш сухой прогон», осталась без предмета.

    Здесь же проверяются права ключа на чтение — одним дешёвым запросом
    (120 в минуту против 2 у полного каталога).
    """
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "🔎 <b>Цена и остаток номинала</b>\n\n"
            "<code>/apr_item ID_УСЛУГИ ID_НОМИНАЛА</code>\n\n"
            "Оба идентификатора показывает <code>/apr_stock robux</code>: "
            "первый — у услуги, второй — у строки с номиналом. Ничего не "
            "покупает.")
        return
    service_id, item_id = parts[1], parts[2]
    status = await message.answer("⏳ Спрашиваю номинал…")
    await status.edit_text(await _item_text(creds, service_id, item_id))


async def _item_text(creds: dict, service_id: str, item_id: str) -> str:
    from automation.approute import item_sync

    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: item_sync(creds, service_id, item_id)),
            timeout=60)
    except Exception as e:
        ok, data = False, str(e)[:300]
    if not ok:
        return f"❌ {html.escape(str(data)[:600])}"
    if not isinstance(data, dict) or not data:
        return ("🤷 Поставщик ответил пусто. Это не «нет такого номинала» и "
                "не «есть» — проверьте идентификаторы по "
                "<code>/apr_stock robux</code>.")
    stock = data.get("inStock")
    lines = [
        "🔎 <b>Номинал у поставщика</b>", "",
        f"Название: {html.escape(str(data.get('name') or '—'))}",
        f"Цена: <b>{data.get('price')}</b> {html.escape(str(data.get('currency') or ''))}",
        f"Остаток: <b>{stock}</b>",
    ]
    if data.get("isLongOrder"):
        lines.append("⏳ Долгий заказ: код приходит не сразу, выдача "
                     "дожидается его опросом.")
    if data.get("minQtyToLongOrder"):
        lines.append(f"   с количества {data.get('minQtyToLongOrder')} заказ "
                     f"становится долгим")
    lines.append("")
    if isinstance(stock, (int, float)) and stock <= 0:
        lines.append("❌ Остаток нулевой — выдача откажет честно, ничего не "
                     "потратив.")
    else:
        lines.append("Это ровно то чтение, которое выдача делает перед "
                     "покупкой вместо сухого прогона: его для магазинных "
                     "заказов не существует.")
    return "\n".join(lines)


@router.message(Command("apr_order_probe"))
async def apr_order_probe(message: Message) -> None:
    """Какую форму тела `POST /orders` принимает поставщик. Денег не тратит.

    SDK и `openapi.yaml` описывают тело заказа по-разному, и до первой
    покупки надо знать, кто прав: ошибка здесь — это оплаченный заказ, ушедший
    в никуда. Проба спрашивает обе формы сухим прогоном про несуществующий
    товар.

    Свой `itemId` можно передать аргументом — тогда останется одна защита
    (`checkOnly`), а её мы в работе ещё не видели.
    """
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    parts = (message.text or "").split()
    item_id = parts[1] if len(parts) > 1 else ""
    service_id = parts[2] if len(parts) > 2 else ""
    status = await message.answer("⏳ Пробую обе формы тела заказа…")
    await _order_probe_report(status, creds, item_id, service_id)


async def _order_probe_report(target, creds: dict, item_id: str = "",
                              service_id: str = "") -> None:
    """Отчёт пробы форм тела — факты поставщика и осторожное чтение."""
    from automation.approute import order_shape_probe_sync

    say = target.edit_text if hasattr(target, "edit_text") else target.answer
    loop = asyncio.get_event_loop()
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: order_shape_probe_sync(creds, item_id, service_id)),
            timeout=120)
    except Exception as e:
        await say(f"❌ {html.escape(str(e)[:300])}")
        return

    lines = ["🧾 <b>Форма тела заказа</b>",
             "<i>Сухой прогон. Денег не тратит, заказ не создаётся.</i>", ""]
    invented = any(r.get("invented_item") for r in rows)
    if invented:
        lines.append("Товар спрошен заведомо несуществующий — "
                     "покупать было нечего.")
    else:
        lines.append("⚠️ Спрошен <b>ваш</b> товар: защита осталась одна — "
                     "<code>checkOnly</code>, а её поведение на живом сервере "
                     "здесь ещё не проверялось.")
    lines.append("")

    for row in rows:
        name = ("SDK — <code>ordersType</code> + <code>referenceId</code>"
                if row["shape"] == "sdk" else
                "openapi.yaml — <code>clientTime</code> + <code>reference</code>")
        lines.append(f"<b>{name}</b>")
        lines.append(f"   отправили: <code>{html.escape(', '.join(row['sent']))}</code>")
        if row["error"]:
            lines.append(f"   не достучались: <code>{html.escape(row['error'])}</code>")
        else:
            lines.append(f"   HTTP <b>{row['http']}</b>, конверт: "
                         f"<b>{html.escape(row['envelope'])}</b>")
            for field, value in (row.get("fields") or {}).items():
                lines.append(f"   {html.escape(field)}: "
                             f"<code>{html.escape(str(value))}</code>")
            if row.get("data"):
                lines.append(f"   data: <code>{html.escape(str(row['data']))}</code>")
            # Ради этих строк проба и затевалась: сервер называет поля сам.
            for complaint in (row.get("complaints") or [])[:8]:
                lines.append(f"   • <code>{html.escape(str(complaint))}</code>")
            if row["trace"]:
                lines.append(f"   traceId: <code>{html.escape(row['trace'])}</code>")
        lines.append(f"   ↳ {html.escape(row['reading'])}")
        lines.append("")

    lines.append("Строка после «↳» — <b>чтение отказа, а не факт</b>: она "
                 "выведена из кода ошибки, а какую форму поставщик принимает "
                 "на самом деле, документация не говорит. Выдачу писать "
                 "только по форме, на которой он дошёл до товара.")
    await say("\n".join(lines)[:4000])

# ---------------------------------------------------------------------------
# Отчёты — общие для команд и кнопок
# ---------------------------------------------------------------------------

async def _whoami_text(creds: dict) -> str:
    """`/whoami` словами: он же показывает IP, который видит поставщик.

    У AppRoute белый список адресов, а бот живёт на Railway, где адрес
    меняется при каждом деплое. Когда ключ «не сработал», это первое, что
    надо посмотреть, — и узнать это можно только отсюда, с самого сервера.
    """
    from automation.approute import outbound_ip, proxy_label, whoami_sync

    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, whoami_sync, creds), timeout=60)
    except Exception as e:
        ok, data = False, str(e)[:300]
    if not ok:
        # Адрес нужен именно тогда, когда ключ ещё не приняли: его и
        # вписывают в белый список. Спрашивать его у AppRoute бесполезно —
        # он отвечает только принятому ключу, — поэтому берём у стороннего
        # сервиса, тем же маршрутом (через прокси, если он задан).
        mine = await asyncio.wait_for(
            loop.run_in_executor(None, outbound_ip, creds), timeout=40)
        route = ("через прокси " + proxy_label(creds) if creds.get("proxy")
                 else "напрямую с сервера")
        return (f"❌ {html.escape(str(data)[:600])}\n\n"
                f"📍 <b>Наш адрес наружу: <code>{html.escape(mine)}</code></b>\n"
                f"   маршрут: {html.escape(route)}\n\n"
                f"<i>Это тот адрес, который нужно вписать в белый список "
                f"кабинета. Если бот живёт на Railway, он меняется при каждом "
                f"выкате — тогда нужен прокси с постоянным адресом, кнопка "
                f"«🌐 Прокси».</i>")
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



# Знак валюты вместо кода: «1 250,00 ₽» читается с одного взгляда, «RUB:
# 1250.0» — нет. Незнакомая валюта остаётся своим кодом: придумывать ей знак
# значит показать не те деньги.
_CURRENCY_SIGNS = {"USD": "$", "EUR": "€", "RUB": "₽", "USDT": "₮",
                   "GBP": "£", "KZT": "₸", "UAH": "₴"}


def _money(value) -> str:
    """Сумма деньгами: разряды пробелом, копейки запятой.

    Нечисловое значение печатается как пришло. Подставить вместо него
    «0,00» значило бы сказать «денег нет» там, где на самом деле «мы не
    разобрали ответ» — а это противоположные советы продавцу.
    """
    try:
        num = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return html.escape(str(value))
    whole, _, frac = f"{num:,.2f}".partition(".")
    return f"{whole.replace(',', ' ')},{frac}"


def _accounts(data) -> list[dict]:
    """Счета структурно: валюта, сумма числом, доступно, овердрафт.

    Отдельно от печати нарочно. Здесь когда-то жила `_positive_amount`,
    которая искала числа регулярным выражением **в собственном же готовом
    отчёте**, — тот самый разбор своей прозы вместо данных, из-за которого
    в `CLAUDE.md` записано «факты собирайте в dict». Стоило поменять формат
    строки, и «есть ли деньги» начало отвечать наугад.

    `amount` равен `None`, когда сумму не разобрать: это «неизвестно», а не
    ноль, и ни одно предупреждение на нём не строится.
    """
    out: list[dict] = []
    for acc in (data or {}).get("items") or []:
        if not isinstance(acc, dict):
            continue
        raw = acc.get("balance")
        try:
            amount = float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            amount = None
        out.append({"currency": str(acc.get("currency") or "?").upper(),
                    "raw": raw, "amount": amount,
                    "available": acc.get("available"),
                    "overdraft": acc.get("overdraftLimit")})
    return out


def _balance_body(data) -> str:
    """Счета в вид, который читается с одного взгляда.

    Порядок — не тот, в котором их прислал поставщик: сверху непонятые
    суммы, потом деньги по убыванию, пустые счета вниз. Продавец открывает
    этот экран с вопросом «есть ли на что выдавать», и ответ не должен
    зависеть от того, каким по счёту кабинет вернул нужный счёт.
    """
    rows = _accounts(data)
    rows.sort(key=lambda r: (r["amount"] is not None, -(r["amount"] or 0)))
    lines: list[str] = []
    for r in rows:
        sign = _CURRENCY_SIGNS.get(r["currency"], r["currency"])
        # Неразобранную сумму печатаем в виде «USD: недоступно»: приставлять
        # знак валюты к слову получается «недоступно $» — вид числа у того,
        # что числом не является.
        money = (f"{_money(r['raw'])} {sign}" if r["amount"] is not None
                 else f"{r['currency']}: {html.escape(str(r['raw']))}")
        # Жирным — только то, чем можно платить. Ноль, набранный жирным,
        # выглядит как сумма, и глаз спотыкается именно там, где не надо.
        lines.append(f"<b>{money}</b>" if (r["amount"] or 0) > 0 else money)
        extra = []
        if r["available"] is not None and r["available"] != r["raw"]:
            extra.append(f"доступно {_money(r['available'])}")
        if r["overdraft"]:
            extra.append(f"овердрафт {_money(r['overdraft'])}")
        if extra:
            lines.append(f"   <i>{' · '.join(extra)}</i>")
    return "\n".join(lines)


def _nothing_to_spend(data) -> bool:
    """Все счета известны и все пусты.

    Считается по числам, а не по тексту. Один неразобранный счёт снимает
    утверждение целиком: «не поняли» — не повод объявлять, что денег нет.
    """
    rows = _accounts(data)
    return bool(rows) and all(r["amount"] == 0 for r in rows)


async def _balance_text(creds: dict) -> tuple[bool, str]:
    """Баланс словами. Он же проверка ключа: чтение, денег не тратит.

    Причина отказа приходит от поставщика, и он вполне может повторить в ней
    наш ключ — так и делает при 400. Поэтому она проходит через `_redact`:
    ключ один даёт право тратить баланс кабинета целиком, и попасть на экран
    он не должен ни в чьём тексте, включая чужой.
    """
    from automation.approute import _redact, balance_sync

    key = str((creds or {}).get("api_key") or "")
    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, balance_sync, creds), timeout=60)
    except Exception as e:
        return False, f"❌ {html.escape(_redact(str(e)[:200], key))}"
    if not ok:
        return False, f"❌ {html.escape(_redact(str(data)[:500], key))}"
    if not _accounts(data):
        # Пустой список счетов — это не ноль на балансе, а «поставщик не
        # назвал ни одного счёта». Разница видна только если сказать прямо.
        return True, ("✅ Ключ принят, но ни одного счёта поставщик не вернул.\n"
                      "Возможно, кабинет ещё не пополняли.")
    body = f"💰 <b>Баланс у поставщика</b>\n\n{_balance_body(data)}"
    if _nothing_to_spend(data):
        # Ноль на счетах — не косметика: следующая же покупка отвалится с
        # «не хватает средств», причём по каждому оплаченному заказу
        # отдельно. Сказать это здесь дешевле, чем объясняться с
        # покупателями потом.
        body += ("\n\n⚠️ <b>Выдавать не на что.</b> Пока на счетах ноль, "
                 "автовыдача остановится на первом же оплаченном заказе — "
                 "пополните кабинет AppRoute.")
    return True, body


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
        b.button(text="🌐 Прокси", callback_data="apr:proxy")
        b.button(text="🔎 Что отвечает сервер", callback_data="apr:debug")
        b.button(text="🗑 Удалить ключ", callback_data="apr:del")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(1, 1, 2, 2, 1, 1, 1)
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
    from automation.approute import proxy_label, proxy_problem
    mark = "🟢" if creds.get("proxy") else "⚪"
    lines.append(f"{mark} 🌐 Прокси: <code>{html.escape(proxy_label(creds))}</code>")
    trouble = proxy_problem(creds)
    if trouble:
        # Иначе socks5 падает уже внутри запроса, и продавец видит английскую
        # строку про зависимости вместо ответа.
        lines.append(f"   ⚠️ {html.escape(trouble)}")
    lines.append("")
    if key:
        lines.append("«🧪 Проверить ключ» спросит баланс — это только чтение, "
                     "денег не тратит.")
        lines.append("«📦 Каталог» покажет, есть ли Roblox, под каким номером "
                     "номинала и почём.")
        lines.append("«🪪 Наш IP у поставщика» — на случай белого списка. "
                     "Адрес постоянный, пока бот живёт на своём сервере; на "
                     "площадках вроде Railway он менялся при каждом выкате.")
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
    # Проверяем сразу, не дожидаясь отдельного нажатия. «Ключ сохранён» —
    # это про наше хранилище, а продавцу нужно знать другое: примет ли его
    # поставщик. Разделять эти два ответа значит показать зелёную галочку
    # там, где ничего ещё не проверено.
    wait = await message.answer("⏳ Ключ сохранён, спрашиваю поставщика…")
    note = await _login_verdict(get_ar_creds(message.from_user.id))
    try:
        await wait.delete()
    except Exception:
        pass
    await _show_creds(message, message.from_user.id, note)


async def _login_verdict(creds: dict) -> str:
    """Принял ли поставщик ключ — одним ответом сразу после входа.

    Спрашивается и баланс, и `whoami`: у AppRoute белый список IP, и когда
    ключ не проходит, следующий вопрос всегда «а с какого адреса нас видно».
    Отвечать на него отдельным нажатием — заставлять продавца искать то, что
    мы уже знаем.
    """
    from automation.approute import whoami_sync

    ok, text = await _balance_text(creds)
    if ok:
        lines = [f"✅ <b>Ключ принят.</b>\n{text}"]
    else:
        lines = [f"❌ <b>Ключ не сработал.</b>\n{text}"]

    loop = asyncio.get_event_loop()
    try:
        seen_ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, whoami_sync, creds), timeout=60)
    except Exception:
        seen_ok, data = False, ""
    ip = ""
    if seen_ok and isinstance(data, dict):
        ip = str(data.get("clientIp") or "")
    if ip:
        lines.append(f"\n🪪 Поставщик видит нас с адреса <code>{html.escape(ip)}</code>.")
        # Пустой список у них означает «пускаем отовсюду». Сказать про это
        # прямо честнее, чем советовать вписывать адрес туда, где проверки
        # нет вовсе.
        if seen_ok and not (data.get("allowlist") or []):
            lines.append("Белый список у вас пуст — значит доступ не ограничен "
                         "по адресу. Если решите его включить, вписывать надо "
                         "именно этот адрес.")
        elif not data.get("allowlistMatches", True):
            lines.append("⚠️ Этого адреса <b>нет</b> в вашем белом списке — "
                         "добавьте его в кабинете, иначе ключ работать не будет.")
    if not ok:
        lines.append("\nЧто смотреть дальше: «🔎 Что отвечает сервер» — "
                     "сырой ответ обоих кабинетов.")
    return "\n".join(lines)


@router.callback_query(F.data == "apr:proxy")
async def apr_proxy_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Ввод прокси. Он же — единственный способ дать боту постоянный адрес.

    У AppRoute белый список IP, а на Railway адрес меняется при каждом
    выкате. Прокси со своим постоянным адресом решает это: в список
    вписывается он, а не сервер.
    """
    await state.set_state(ARState.proxy)
    b = InlineKeyboardBuilder()
    if get_ar_creds(callback.from_user.id).get("proxy"):
        b.button(text="🧪 Годится ли он", callback_data="apr:proxy_test")
        b.button(text="🗑 Убрать прокси", callback_data="apr:proxy_off")
    b.button(text="⬅️ Отмена", callback_data="apr:creds")
    b.adjust(1)
    await callback.message.edit_text(
        "🌐 <b>Прокси для запросов к AppRoute</b>\n\n"
        "Нужен, если у поставщика включён белый список адресов: туда "
        "вписывается адрес <b>прокси</b>, а не сервера.\n\n"
        "Пришлите одной строкой:\n"
        "<code>http://логин:пароль@адрес:порт</code>\n"
        "<code>socks5://логин:пароль@адрес:порт</code>\n\n"
        "⚠️ Адрес у прокси должен быть <b>постоянным</b>. Дешёвые прокси "
        "часто меняют его на каждый запрос — такой в белый список не "
        "впишешь.\n\n"
        "<i>Ключ прокси-провайдеру не достаётся: соединение с AppRoute "
        "шифруется, и через прокси видно только имя сайта.</i>",
        reply_markup=b.as_markup())
    await callback.answer()


@router.message(ARState.proxy)
async def apr_proxy_input(message: Message, state: FSMContext) -> None:
    from automation.approute import proxy_problem

    value = (message.text or "").strip()
    await state.clear()
    # В строке прокси обычно логин и пароль — удаляем, как и ключ.
    try:
        await message.delete()
    except Exception:
        pass
    if not value:
        await _show_creds(message, message.from_user.id,
                          "⚠️ Пустое значение — ничего не изменил.")
        return
    trouble = proxy_problem({"proxy": value})
    if trouble:
        # Сохранить всё равно сохраним: адрес может быть верным, а мешает
        # сборка. Но сказать надо сразу, а не при первом же запросе.
        await _show_creds(message, message.from_user.id,
                          f"⚠️ Сохранил, но: {html.escape(trouble)}")
        save_ar_creds(message.from_user.id, {"proxy": value})
        return
    save_ar_creds(message.from_user.id, {"proxy": value})
    await _show_creds(message, message.from_user.id,
                      "✅ Прокси сохранён. Проверьте «🪪 Наш IP у поставщика»: "
                      "там должен появиться адрес прокси — его и вписывайте "
                      "в белый список.")


@router.callback_query(F.data == "apr:proxy_test")
async def apr_proxy_test(callback: CallbackQuery, state: FSMContext) -> None:
    """Годится ли этот прокси для белого списка — фактом, а не советом."""
    await state.clear()
    creds = get_ar_creds(callback.from_user.id)
    await callback.answer("⏳ Проверяю…")
    await callback.message.edit_text(
        "⏳ Спрашиваю свой адрес несколько раз подряд…")
    await _show_creds(callback, callback.from_user.id,
                      await _proxy_verdict(creds))


async def _proxy_verdict(creds: dict) -> str:
    """Отчёт о прокси. Собирается из фактов, а не из разбора своей же прозы."""
    from automation.approute import proxy_check_sync

    loop = asyncio.get_event_loop()
    try:
        got = await asyncio.wait_for(
            loop.run_in_executor(None, proxy_check_sync, creds), timeout=120)
    except Exception as e:
        return f"❌ Проверить не вышло: {html.escape(str(e)[:200])}"

    if not got["proxy"]:
        return (f"🌐 Прокси не задан. Наружу выходим напрямую с адреса "
                f"<code>{html.escape(got['ip'])}</code> — его и надо вписывать "
                f"в белый список. На Railway он меняется при каждом выкате.")

    seen = ", ".join(html.escape(a) for a in got["seen"])
    if got["problem"]:
        return f"⚠️ {html.escape(got['problem'])}"
    if not got["ip"]:
        return ("❌ Через этот прокси не удалось выйти наружу ни разу.\n"
                "Проверьте строку: адрес, порт, логин и пароль.")
    if got["same_as_direct"]:
        # «Прокси задан» и «запросы идут через прокси» — разные утверждения.
        return (f"❌ Прокси не применяется: адрес такой же, как без него "
                f"(<code>{html.escape(got['ip'])}</code>).\n"
                f"Обычно это неверный формат строки или отвергнутый пароль.")
    if not got["stable"]:
        return (f"❌ <b>Для белого списка не годится.</b>\n"
                f"Адрес меняется от запроса к запросу: {seen}\n"
                f"В кабинет вписывают один адрес, а этот прокси ротационный. "
                f"Нужен статический (выделенный).")
    return (f"✅ <b>Похоже, годится.</b>\n"
            f"Три запроса подряд вышли с одного адреса: "
            f"<code>{html.escape(got['ip'])}</code>\n"
            f"Без прокси мы выходим с <code>{html.escape(got['direct'])}</code> "
            f"— значит прокси действительно работает.\n\n"
            f"Вписывайте в белый список <code>{html.escape(got['ip'])}</code>.\n"
            f"<i>Три запроса — это выборка, а не доказательство: прокси может "
            f"менять адрес раз в час или на новую сессию. Окончательно "
            f"подтвердит только принятый ключ.</i>")


@router.callback_query(F.data == "apr:proxy_off")
async def apr_proxy_off(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    save_ar_creds(callback.from_user.id, {"proxy": ""})
    await _show_creds(callback, callback.from_user.id, "🗑 Прокси убран.")
    await callback.answer()


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


@router.callback_query(F.data == "apr:balance")
async def apr_balance_button(callback: CallbackQuery) -> None:
    """Баланс кабинета поставщика — из раздела Robux, кнопкой.

    Это те самые деньги, которыми покупаются коды: кончатся — выдача
    остановится на всех заказах сразу, и каждый получит свой отказ «не
    хватает средств». Узнать об этом заранее продавец мог только командой
    `/apr_balance`, а про команду ему неоткуда узнать.

    Спрашивается по нажатию, а не при отрисовке раздела: у AppRoute лимит
    запросов, и читать баланс при каждом открытии экрана значит тратить его
    впустую — да ещё и подвешивать раздел, когда поставщик молчит.

    Отдельно оговаривается, **чей** это баланс. На соседних экранах бота
    живут деньги магазина на Юмаркете; спутать их значит решить, что
    выдавать есть на что, и узнать обратное на оплаченном заказе.
    """
    creds, hint = _creds_or_hint(callback.from_user.id)
    if hint:
        await callback.answer()
        await callback.message.edit_text(hint, reply_markup=_back_to_robux())
        return
    await callback.answer("⏳ Спрашиваю…")
    await callback.message.edit_text("⏳ Спрашиваю баланс у поставщика…")
    ok, text = await _balance_text(creds)
    if ok:
        # Кабинет и время — не украшение. Кабинета два, ключи у них разные,
        # и «баланс ноль» из не того кабинета выглядит точно так же, как
        # настоящий ноль. Время отвечает на «это сейчас или я смотрю на
        # вчерашний экран», который Telegram оставляет в переписке.
        import localtime as _lt
        from storage import get_settings
        cabinet = _REGIONS.get(
            str((creds or {}).get("region") or "io").strip().lower(),
            _REGIONS["io"])[0]
        when = _lt.now(get_settings(callback.from_user.id)).strftime("%H:%M")
        body = (f"{text}\n\n"
                f"<i>Кабинет AppRoute {cabinet} · на {when}. Это деньги, "
                f"которыми бот покупает коды, — не баланс магазина на "
                f"Юмаркете.</i>")
    else:
        # Причину показываем как есть: «баланс не получен» без неё — то же
        # молчание, ради которого этот экран и делался.
        body = (f"❌ <b>Баланс не получен.</b>\n{text}\n\n"
                f"Что смотреть дальше: «🪪 Наш IP у поставщика» — стоит ли "
                f"наш адрес в белом списке; «🔎 Что отвечает сервер» — что "
                f"именно приходит из кабинета. Обе кнопки — в «🔑 Поставщик "
                f"AppRoute».")
    await callback.message.edit_text(body, reply_markup=_back_to_robux())


def _back_to_robux() -> InlineKeyboardMarkup:
    """Возврат туда, откуда пришли, а не на экран ключа.

    Кнопка баланса живёт в разделе Robux; уводить с неё на экран доступа
    значит терять место, где продавец работал.
    """
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="apr:balance")
    b.button(text="⬅️ Назад", callback_data="plugins:auto_roblox")
    b.adjust(1)
    return b.as_markup()


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
