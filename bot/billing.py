"""Приём оплаты подписки переводом внутри Bybit.

Продавец переводит USDT на UID владельца, бот читает поступления и сам
засчитывает оплату. Разбор поступлений — здесь; сам разговор с Bybit — в
`automation/bybit.py`.

Три правила, каждое про чужие деньги:

1. **Дважды не засчитываем.** `txID` перевода помнится, и перезапуск бота
   не выдаёт подписку по второму разу за те же деньги. Вернуть выданное
   нечем.
2. **Пришедшие деньги не теряются.** Продавец мог заплатить раньше, чем
   назвал свой UID, или прислать меньше самого дешёвого тарифа. Такой
   перевод не выбрасывается: он лежит с причиной, виден владельцу и
   разбирается сам, как только продавец назовёт UID.
3. **Молча не отказываем.** У каждого «ничего не произошло» есть причина,
   и она уходит владельцу в журнал, а не только в лог контейнера.
"""
from __future__ import annotations

import asyncio
import logging

from automation.bybit import STATUS_DONE, BybitError, internal_deposits

logger = logging.getLogger(__name__)

COIN = "USDT"


async def collect(bot=None) -> list[dict]:
    """Прочитать поступления и засчитать опознанные.

    Отдаёт список выданных подписок — чтобы это можно было проверить
    тестом, а не поверить на слово.
    """
    from storage import (bybit_ready, get_bybit, note_payments_checked,
                         payments_since)
    if not bybit_ready():
        return []
    creds = get_bybit()
    try:
        rows = await asyncio.to_thread(
            internal_deposits, creds.get("key", ""), creds.get("secret", ""),
            since_ms=payments_since(), coin=COIN)
    except BybitError as e:
        # Отказ не проглатываем: снаружи «оплата не засчиталась» и «мы не
        # смогли спросить» выглядят одинаково, а лечатся по-разному.
        await _tell_owner(bot, f"₿ Не смог прочитать поступления Bybit.\n{e}")
        return []

    granted: list[dict] = []
    for row in rows:
        got = await _settle(bot, row)
        if got:
            granted.append(got)
    note_payments_checked()
    return granted


async def _settle(bot, row: dict) -> dict | None:
    """Разобрать одно поступление. Отдаёт запись о выдаче либо None."""
    from storage import (add_unresolved, note_payment, payer_by_uid,
                         payment_seen, tier_for_amount)

    tx = str(row.get("txID") or "")
    status = int(row.get("status") or 0)
    if not tx or payment_seen(tx):
        return None
    if status != STATUS_DONE:
        # Единица — «ещё неизвестно», а не «почти успех»: засчитать её
        # значит выдать подписку за перевод, который может не дойти. Её мы
        # просто пропускаем — придёт следующим проходом уже двойкой.
        return None

    uid = str(row.get("fromMemberId") or "").strip()
    coin = str(row.get("coin") or "").upper()
    try:
        amount = float(row.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0

    if coin != COIN:
        add_unresolved(tx, uid, amount, coin, f"прислано в {coin}, а не USDT")
        await _tell_owner(bot, f"₿ Пришло {amount} {coin} от UID {uid or '—'} — "
                               f"это не USDT, подписку не выдал.")
        return None

    who = payer_by_uid(uid)
    days = tier_for_amount(amount)

    if not who:
        add_unresolved(tx, uid, amount, coin, "UID не назван ни одним продавцом")
        await _tell_owner(bot, f"₿ Пришло {amount} USDT от UID {uid or '—'}, "
                               "но такой UID никто из продавцов не называл. "
                               "Засчитается сам, как только назовёт.")
        return None

    if not days:
        add_unresolved(tx, uid, amount, coin, "меньше самого дешёвого тарифа")
        await _notify(bot, who, f"₿ Перевод на {amount} USDT получен, но этого "
                                "не хватает на самый короткий тариф. Напиши в "
                                "поддержку — разберёмся, деньги не потеряны.")
        await _tell_owner(bot, f"₿ {amount} USDT от UID {uid} — меньше "
                               "самого дешёвого тарифа. Продавцу сказал.")
        return None

    return await _grant(bot, tx, who, uid, amount, days)


async def _grant(bot, tx: str, who: int, uid: str, amount: float,
                 days: int) -> dict:
    """Выдать подписку и сказать об этом обоим."""
    from storage import (drop_unresolved, grant_subscription,
                         note_payment, usdt)
    # Отметка ставится ВМЕСТЕ с выдачей и до сообщений: упавшая отправка не
    # должна приводить к повторной выдаче на следующем проходе.
    note_payment(tx, who, amount, days)
    drop_unresolved(tx)
    grant_subscription(who, days)
    await _notify(bot, who, f"✅ Оплата получена: {usdt(amount)} USDT.\n"
                            f"Доступ продлён на <b>{days} дн.</b>")
    await _tell_owner(bot, f"₿ {usdt(amount)} USDT от UID {uid} — "
                           f"выдал {days} дн. "
                           f"продавцу {who}.")
    return {"tx": tx, "user": who, "amount": amount, "days": days}


async def settle_pending(bot, user_id: int, uid: str) -> list[dict]:
    """Разобрать переводы, ждавшие этот UID.

    Зовётся, когда продавец назвал свой UID: он мог заплатить раньше, и
    ждать следующего перевода ему не за что.
    """
    from storage import tier_for_amount, unresolved_for_uid
    out: list[dict] = []
    for tx, row in unresolved_for_uid(uid):
        if str(row.get("coin") or "").upper() != COIN:
            continue
        amount = float(row.get("amount") or 0)
        days = tier_for_amount(amount)
        if days:
            out.append(await _grant(bot, tx, user_id, uid, amount, days))
    return out


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:                          # noqa: BLE001
        logger.warning("не смог сказать продавцу %s об оплате: %s", user_id, e)


async def _tell_owner(bot, text: str) -> None:
    """В журнал владельца, тему «Ордер на оплату».

    Наружу отсюда не летит ничего: запись о полученных деньгах не должна
    мешать их получать.
    """
    if bot is None:
        return
    try:
        from logs import log_event
        await log_event(bot, "payment", [text])
    except Exception as e:                          # noqa: BLE001
        logger.warning("журнал оплаты не записался: %s", e)
