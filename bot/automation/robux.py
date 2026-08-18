"""Robux — подбор номинала под заказ. Покупок здесь нет.

**Robux — не звёзды, и путать их дорого.** Плагин был скопирован со звёзд, и
от этого на экране оказались чужие понятия: ник покупателя, произвольное
количество, ручная выдача «кому». Разница вот в чём:

| | Звёзды (Fragment) | Robux (AppRoute) |
|---|---|---|
| что выдаём | зачисление на аккаунт | **код**, покупатель активирует сам |
| ник покупателя | обязателен | **не нужен**: `fields` у всех услуг пусты |
| чем платим | свой кошелёк TON | баланс кабинета поставщика |
| количество | любое | **только номиналы из каталога** |
| повторная выдача | звёзды уйдут дважды | код спишется со склада дважды |
| регион | нет | есть: GL, RU и страновые — коды не взаимозаменяемы |

Отсюда главное правило этого модуля: **номинал подбирается точно или не
подбирается вовсе**. Выдать 800 Robux за заказ на 500 значит либо подарить
триста, либо, если наоборот, недодать оплаченное. Оба исхода — враньё
покупателю, поэтому «примерно подходящий» номинал здесь не возвращается
никогда, а несовпадение называется вслух.
"""
from __future__ import annotations

import re

# Написания, по которым узнаётся заказ на Robux. Продавцы называют товар
# по-разному, и ловить только латиницу значит пропускать половину.
ROBUX_WORDS = ("robux", "робукс", "roblox", "роблокс", "робл")

# Услуги AppRoute, в которых лежат номиналы Robux. Название приходит вида
# «Roblox Wallet Code | GL» — регион отделён вертикальной чертой.
_WALLET_SERVICE = "roblox wallet code"

# Числа, которые в названии товара количеством не являются: год, «24/7»,
# проценты и тому подобное соседство.
_NOT_A_QUANTITY = re.compile(r"(20\d\d|24/7|\d+\s*%)")


def is_robux_order(title: str, keyword: str = "") -> bool:
    """Заказ ли это на Robux.

    `keyword` — слово продавца. Заданное, оно требование, а не подсказка:
    товар «Roblox аккаунт» не должен уходить в выдачу Robux только потому,
    что в названии есть «roblox».
    """
    low = str(title or "").lower()
    if not low:
        return False
    kw = str(keyword or "").strip().lower()
    if kw:
        return kw in low
    return any(w in low for w in ROBUX_WORDS)


def robux_quantity(title: str, default: int = 0) -> int:
    """Сколько Robux просят — число, стоящее ближе всего к слову «robux».

    В названии обычно несколько чисел («Roblox 1000 Robux за 350 ₽»), и
    брать первое попавшееся значит однажды продать номинал на 350.
    Расстояние до слова решает вернее.
    """
    low = str(title or "").lower()
    if not low:
        return int(default or 0)
    spots = [m.start() for w in ROBUX_WORDS
             for m in re.finditer(re.escape(w), low)]
    if not spots:
        return int(default or 0)
    best, best_dist = None, None
    for m in re.finditer(r"\d[\d\s.,]*", low):
        raw = m.group(0)
        if _NOT_A_QUANTITY.search(raw.strip()):
            continue
        digits = re.sub(r"\D", "", raw)
        if not digits:
            continue
        value = int(digits)
        if value <= 0:
            continue
        dist = min(abs(m.start() - s) for s in spots)
        if best_dist is None or dist < best_dist:
            best, best_dist = value, dist
    return best if best is not None else int(default or 0)


def region_of(service_name: str) -> str:
    """Регион услуги: «Roblox Wallet Code | GL» → «GL».

    Регион важен: российский код не подойдёт покупателю с глобальным
    аккаунтом и наоборот. Свести их в одну кучу значит выдать негодный код и
    узнать об этом от покупателя.
    """
    name = str(service_name or "")
    return name.split("|")[-1].strip().upper() if "|" in name else ""


def denominations(catalog, region: str = "") -> list[dict]:
    """Номиналы Robux из живого ответа `GET /services`.

    Возвращает строки `{robux, denomination_id, price, currency, in_stock,
    region, service_id}`. `denomination_id` — это `id` **номинала**, а не
    услуги: именно он уходит в `orders[].denominationId`, и перепутать их
    значит получить отказ по схеме.
    """
    items = (catalog or {}).get("items") if isinstance(catalog, dict) else catalog
    want = str(region or "").strip().upper()
    out: list[dict] = []
    for service in (items or []):
        if not isinstance(service, dict):
            continue
        name = str(service.get("name") or "")
        if _WALLET_SERVICE not in name.lower():
            continue
        reg = region_of(name)
        if want and reg != want:
            continue
        for row in (service.get("items") or []):
            if not isinstance(row, dict):
                continue
            qty = robux_quantity(str(row.get("name") or ""))
            if qty <= 0:
                continue
            out.append({
                "robux": qty,
                "denomination_id": str(row.get("id") or ""),
                "price": float(row.get("price") or 0),
                "currency": str(row.get("currency") or ""),
                "in_stock": int(row.get("inStock") or 0),
                "region": reg,
                "service_id": str(service.get("id") or ""),
                "name": str(row.get("name") or ""),
            })
    # Дешевле за Robux — выше. При равном номинале в разных регионах
    # выбирать наугад нельзя, а по цене — можно объяснить.
    out.sort(key=lambda r: (r["robux"], r["price"] / r["robux"]))
    return out


def match_denomination(catalog, robux: int, region: str = "") -> tuple[dict | None, str]:
    """Номинал ровно на столько Robux → (строка каталога, причина отказа).

    **Только точное совпадение.** Ближайший сверху означал бы подарок за наш
    счёт, ближайший снизу — недоданное оплаченное. Молча подменять номинал
    здесь нельзя: это ровно то враньё покупателю, из-за которого в этом
    проекте заведено правило про «✅ Пак поднят · Поднято: 0».

    Складывать несколько кодов в один заказ (500 = 200 + 300) не умеем — и
    так и говорим, вместо того чтобы выдать один код на другую сумму.
    """
    want = int(robux or 0)
    if want <= 0:
        return None, "в названии заказа не видно, сколько Robux нужно"
    rows = denominations(catalog, region)
    if not rows:
        where = f" в регионе {region}" if region else ""
        return None, f"в каталоге поставщика нет номиналов Robux{where}"
    exact = [r for r in rows if r["robux"] == want]
    if not exact:
        have = ", ".join(str(q) for q in sorted({r["robux"] for r in rows})) \
            or "ничего"
        return None, (f"номинала ровно на {want} Robux у поставщика нет. "
                      f"Есть: {have}. Складывать несколько кодов бот не умеет")
    in_stock = [r for r in exact if r["in_stock"] > 0]
    if not in_stock:
        return None, f"номинал на {want} Robux есть, но остаток нулевой"
    return in_stock[0], ""


def order_reference(order_id: str, robux: int) -> str:
    """Наш идентификатор покупки — придумывается ДО вызова поставщика.

    По нему заказ находится у них (`GET /orders?referenceId=…`), если связь
    оборвалась после отправки. И он же делает повтор безопасным: на ту же
    ссылку поставщик отвечает `IDEMPOTENCY_REPLAY` — «такой заказ уже был,
    отдаю прежний результат», то есть успехом, а не отказом. Считать этот
    ответ отказом значит купить второй раз.

    Поэтому ссылка обязана быть **одинаковой** при повторе: она собирается
    из номера заказа, а не из времени.
    """
    return f"yoo-{str(order_id or '').strip()}-{int(robux or 0)}"


def codes_from_result(data) -> list[str]:
    """Коды из ответа поставщика. `pin` — это и есть код гифт-карты.

    Пустой список означает «кода нет», а не «выдача прошла»: доложить об
    успехе, не получив кода, — худшее, что может сделать этот модуль.
    """
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    result = data.get("result")
    if isinstance(result, dict):
        for voucher in (result.get("vouchers") or []):
            if isinstance(voucher, dict) and voucher.get("pin"):
                out.append(str(voucher["pin"]))
    for order in (data.get("orders") or []):
        if isinstance(order, dict):
            out.extend(codes_from_result(order))
    return out
