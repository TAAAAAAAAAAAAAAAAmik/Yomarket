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


@router.message(Command("apr_dry_check"))
async def apr_dry_check(message: Message) -> None:
    """Сухой ли наш сухой прогон. Перечитывает результат, а не верит ответу.

    В документации поставщика `POST /orders` перечислен четырежды: Shop, DTU,
    eSIM и отдельно **«Проверка DTU»**. То есть проверка описана только для
    пополнений, а мы шлём `checkOnly` вместе с `ordersType: "shop"`. Если для
    shop он игнорируется, то «сухой прогон» перед каждой покупкой — это сама
    покупка, и защиты, которую обещает `docs/robux_delivery.md`, нет вовсе.

    Проверяется чтением обратно: сделали прогон со своей ссылкой — спросили
    `GET /orders?referenceId=…`. Появился заказ — прогон не сухой.

    **Пока баланс кабинета нулевой, проверка ничего не стоит:** даже если
    прогон окажется покупкой, она упрётся в нехватку средств. При ненулевом
    балансе команда требует подтверждения словом — иначе она сама может
    оказаться той тратой, которую проверяет.
    """
    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    parts = (message.text or "").split()
    denomination = parts[1] if len(parts) > 1 else ""
    confirmed = len(parts) > 2 and parts[2].lower() in ("точно", "да", "yes")
    if not denomination:
        await message.answer(
            "🧪 <b>Проверка сухого прогона</b>\n\n"
            "<code>/apr_dry_check ID_НОМИНАЛА</code>\n\n"
            "ID номинала — из «📦 Каталог» (строка <code>itemId=…</code>).\n\n"
            "Проверка делает прогон и тут же спрашивает поставщика, "
            "появился ли заказ. Пока баланс нулевой, это ничего не стоит.")
        return
    await _dry_check_report(message, creds, denomination, confirmed)


async def _dry_check_report(target, creds: dict, denomination: str,
                            confirmed: bool) -> None:
    from automation.approute import balance_lines, balance_sync, dry_run_check_sync

    say = target.edit_text if hasattr(target, "edit_text") else target.answer
    loop = asyncio.get_event_loop()

    # Сначала баланс: он решает, безопасна ли проверка вообще.
    try:
        ok, money = await asyncio.wait_for(
            loop.run_in_executor(None, balance_sync, creds), timeout=60)
    except Exception as e:
        ok, money = False, str(e)[:200]
    if not ok:
        await say(f"❌ Баланс не прочитан: {html.escape(str(money)[:300])}\n\n"
                  f"<i>Без него неизвестно, во что обойдётся проверка, — "
                  f"поэтому не делаю.</i>")
        return
    lines = balance_lines(money)
    has_money = any(_positive_amount(l) for l in lines)
    if has_money and not confirmed:
        await say(
            "⚠️ <b>На кабинете есть деньги</b>\n\n"
            + "\n".join(f"• {html.escape(l)}" for l in lines)
            + "\n\nПроверка потому и нужна, что мы не знаем, сухой ли "
              "прогон. Если он не сухой — это будет настоящая покупка, и "
              "деньги спишутся.\n\n"
              "Если согласны — повторите с подтверждением:\n"
              f"<code>/apr_dry_check {html.escape(denomination)} точно</code>")
        return

    await say("⏳ Делаю прогон и перечитываю, появился ли заказ…")
    try:
        got = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: dry_run_check_sync(creds, denomination)),
            timeout=150)
    except Exception as e:
        await say(f"❌ {html.escape(str(e)[:300])}")
        return

    out = ["🧪 <b>Сухой ли прогон</b>", "",
           f"Ссылка проверки: <code>{html.escape(got['reference'])}</code>",
           f"Прогон принят: <b>{'да' if got['sent_ok'] else 'нет'}</b>"]
    if not got["sent_ok"]:
        out.append(f"   причина: {html.escape(got['sent_why'])}")
    if got["codes"]:
        out.append(f"   ⚠️ в ответе пришли коды: <b>{got['codes']}</b>")
    out.append(f"Заказ по ссылке найден: "
               f"<b>{'да' if got['found'] else 'нет'}</b>"
               + ("" if got["looked"] else " <i>(список заказов не прочитан)</i>"))
    out.append("")

    if got["found"] or got["codes"]:
        out += ["❌ <b>Прогон НЕ сухой: заказ создан.</b>",
                "Значит `checkOnly` для shop-заказов не работает, и защиты "
                "перед покупкой у нас нет. Выдачу включать нельзя, пока это "
                "не переделано.",
                "",
                "Проверьте кабинет по ссылке выше — заказ там."]
    elif got["sent_ok"] and got["looked"]:
        out += ["✅ <b>Похоже, прогон сухой:</b> поставщик принял запрос, а "
                "заказа по ссылке нет.",
                "",
                "<i>«Похоже» — потому что отсутствие в списке слабее, чем "
                "присутствие: заказ мог не успеть в него попасть. Но это "
                "лучшее, что можно узнать, не тратя денег.</i>"]
    elif got["sent_ok"]:
        out += ["🤷 Прогон принят, но список заказов прочитать не вышло — "
                "сказать, создан заказ или нет, нечем.",
                "Повторите проверку позже."]
    else:
        out += ["🤷 Прогон не принят, и заказа нет.",
                "Это не ответ на вопрос: отказать могли и по другой причине "
                "(нет такого номинала, нет денег, ключ). Смотрите причину "
                "выше."]
    await say("\n".join(out)[:4000])


def _positive_amount(line: str) -> bool:
    """Есть ли в строке баланса число больше нуля.

    Строки приходят вида «USD: 12.5 (доступно 10.0)». Разбирать текст здесь
    приходится, потому что `balance_lines` отдаёт уже готовые строки; зато
    решение принимается в сторону осторожности: не разобрали — считаем, что
    деньги есть, и спросим подтверждение.
    """
    numbers = re.findall(r"\d+[.,]?\d*", str(line or ""))
    if not numbers:
        return True
    try:
        return any(float(n.replace(",", ".")) > 0 for n in numbers)
    except ValueError:                                     # pragma: no cover
        return True


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
