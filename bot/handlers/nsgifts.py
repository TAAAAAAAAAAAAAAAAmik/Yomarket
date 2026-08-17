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

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from storage import delete_ns_creds, get_ns_creds, ns_fields, save_ns_creds

router = Router()
logger = logging.getLogger(__name__)

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
    """Каталог поставщика. Только чтение, ничего не покупает.

    С этой команды начинается автовыдача Robux: в документации поставщика
    Роблокса нет ни в одном примере, и под каким `service_id` он лежит,
    какие поля требует и сколько стоит — знает только живой ответ `/stock`.
    """
    from automation.nsgifts import find_services, stock_sync

    creds, hint = _creds_or_hint(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    parts = (message.text or "").split(maxsplit=1)
    needle = parts[1].strip() if len(parts) > 1 else "robux"

    status = await message.answer(f"⏳ Читаю каталог поставщика "
                                  f"(ищу «{html.escape(needle)}»)…")
    loop = asyncio.get_event_loop()
    try:
        ok, data = await asyncio.wait_for(
            loop.run_in_executor(None, stock_sync, creds), timeout=90)
    except Exception as e:
        await status.edit_text(f"❌ {html.escape(str(e)[:200])}")
        return
    if not ok:
        await status.edit_text(f"❌ {html.escape(str(data)[:400])}")
        return

    cats = (data or {}).get("categories") or []
    found = find_services(data, needle)
    lines = [f"📦 <b>Каталог ns.gifts</b>",
             f"Категорий: <b>{len(cats)}</b> · "
             f"услуг всего: <b>{len(find_services(data, ''))}</b>",
             ""]
    if not found:
        lines += [f"❌ По слову «{html.escape(needle)}» ничего не нашлось.",
                  "",
                  "Попробуйте другое слово: <code>/ns_stock roblox</code>, "
                  "<code>/ns_stock gift</code>. Пустое слово покажет всё, но "
                  "список длинный."]
        await status.edit_text("\n".join(lines))
        return

    lines.append(f"Нашёл по «{html.escape(needle)}»: <b>{len(found)}</b>")
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
    await status.edit_text("\n".join(lines)[:4000])
