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
# Второе семейство: подарочные карты по странам. Их 23 против трёх
# кошельковых, и мерятся они **другим** — номинал у них в местной валюте
# («$10 Roblox gift card», «EUR 25»), а не в Robux. Сколько Robux даст карта
# на $10, решает курс самого Roblox: мы его не знаем и выдумывать не станем.
# Поэтому подбор у семейств разный, и семейство определяется по региону.
_CARD_SERVICE = "roblox gift card"

# Регионы кошельковых кодов — там номинал в Robux. Всё остальное, что
# встретится в каталоге, это страна подарочной карты.
WALLET_REGIONS = ("GL", "RU", "XBOX")

# Строка, которой регион записывается в описание товара. У Юмаркета выбора
# региона нет вовсе — 19.08 проверено: категория 14 отдаёт ноль фильтров, —
# поэтому единственное место, где регион переживёт создание объявления, это
# само описание. Его же читает выдача, чтобы знать, какой код покупать.
REGION_PREFIX = "Регион кода:"

# Как регион может быть назван в чужом или старом описании. Читается только
# если строки `REGION_PREFIX` нет: точная запись всегда главнее догадки.
_REGION_WORDS = {
    "GL": ("глобальн", "global", "worldwide", "любой регион"),
    "RU": ("росси", "russia", " ru ", "рф"),
    "XBOX": ("xbox", "хбокс"),
    "US": ("сша", "usa", " us "),
    "EU": ("европ", "europe", " eu "),
    "UK": ("великобритан", "британ", " uk "),
    "TR": ("турц", "turkey"),
    "BR": ("бразил", "brazil"),
}

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


def is_wallet_region(region: str) -> bool:
    """Кошельковый код (номинал в Robux) или подарочная карта (в валюте).

    Семейство определяется по региону, а не по названию заказа: у карт в
    названии стоит номинал вроде «$10», и принять его за десять Robux —
    ошибка в сто раз, притом молчаливая.
    """
    return str(region or "").strip().upper() in WALLET_REGIONS


def face_value(name: str) -> tuple[float, str]:
    """Номинал подарочной карты: «$10 Roblox gift card» → (10.0, «USD»).

    У карт число само по себе ничего не значит без валюты: «10» бывает и
    долларами, и евро, и реалами. Пара возвращается целиком, а не одно
    число, чтобы сравнить их случайно было нельзя.
    """
    text = str(name or "")
    # Код валюты — отдельное слово. Без границ слова `\b` три буквы брались
    # из середины: «1000 Robux» читалось как тысяча валюты «ROB», то есть
    # номинал в Robux притворялся номиналом карты.
    m = re.search(r"(\b[A-Za-z]{3}\b|[$€£₽])\s*([\d]+(?:[.,]\d+)?)", text)
    if not m:
        m2 = re.search(r"([\d]+(?:[.,]\d+)?)\s*(\b[A-Za-z]{3}\b|[$€£₽])", text)
        if not m2:
            return 0.0, ""
        amount, sign = m2.group(1), m2.group(2)
    else:
        sign, amount = m.group(1), m.group(2)
    try:
        value = float(str(amount).replace(",", "."))
    except ValueError:
        return 0.0, ""
    signs = {"$": "USD", "€": "EUR", "£": "GBP", "₽": "RUB"}
    return value, signs.get(sign, str(sign).upper())


def catalog_regions(catalog) -> list[dict]:
    """Регионы Roblox из живого каталога — как они есть, а не по памяти.

    19.08 их 26: три кошельковых (GL, RU, XBOX) и 23 страны подарочных карт.
    Список читается из ответа поставщика, потому что он меняется: зашитый
    перечень однажды промолчит про новый регион или предложит исчезнувший.

    Строки: `{region, kind, service_id, count, in_stock}`.
    """
    items = (catalog or {}).get("items") if isinstance(catalog, dict) else catalog
    out: list[dict] = []
    for service in (items or []):
        if not isinstance(service, dict):
            continue
        low = str(service.get("name") or "").lower()
        kind = ("wallet" if _WALLET_SERVICE in low else
                "card" if _CARD_SERVICE in low else "")
        if not kind:
            continue
        rows = [r for r in (service.get("items") or []) if isinstance(r, dict)]
        out.append({
            "region": region_of(service.get("name") or "") or "—",
            "kind": kind,
            "service_id": str(service.get("id") or ""),
            "count": len(rows),
            "in_stock": sum(1 for r in rows if int(r.get("inStock") or 0) > 0),
        })
    out.sort(key=lambda r: (r["kind"] != "wallet", r["region"]))
    return out


def denominations(catalog, region: str = "") -> list[dict]:
    """Номиналы Roblox из живого ответа `GET /services`.

    Оба семейства сразу. У кошельковых кодов заполнено `robux`, у
    подарочных карт — `face` и `face_currency`, а `robux` там ноль: сколько
    Robux даст карта на $10, решает курс самого Roblox, и подставлять сюда
    догадку значит однажды выдать не то, что оплачено.

    `denomination_id` — это `id` **номинала**, а не услуги: именно он уходит
    в `orders[].denominationId`, и перепутать их значит получить отказ по
    схеме.
    """
    items = (catalog or {}).get("items") if isinstance(catalog, dict) else catalog
    want = str(region or "").strip().upper()
    out: list[dict] = []
    for service in (items or []):
        if not isinstance(service, dict):
            continue
        name = str(service.get("name") or "")
        low = name.lower()
        kind = ("wallet" if _WALLET_SERVICE in low else
                "card" if _CARD_SERVICE in low else "")
        if not kind:
            continue
        reg = region_of(name)
        if want and reg != want:
            continue
        for row in (service.get("items") or []):
            if not isinstance(row, dict):
                continue
            title = str(row.get("name") or "")
            qty = robux_quantity(title) if kind == "wallet" else 0
            face, face_cur = face_value(title) if kind == "card" else (0.0, "")
            if kind == "wallet" and qty <= 0:
                continue
            if kind == "card" and face <= 0:
                continue
            out.append({
                "robux": qty,
                "face": face,
                "face_currency": face_cur,
                "kind": kind,
                "denomination_id": str(row.get("id") or ""),
                "price": float(row.get("price") or 0),
                "currency": str(row.get("currency") or ""),
                "in_stock": int(row.get("inStock") or 0),
                "region": reg,
                "service_id": str(service.get("id") or ""),
                "name": title,
            })
    # Дешевле за единицу — выше. При равном номинале в разных регионах
    # выбирать наугад нельзя, а по цене — можно объяснить.
    def _key(r):
        unit = r["robux"] or r["face"] or 1
        return (r["kind"] != "wallet", r["robux"], r["face"], r["price"] / unit)
    out.sort(key=_key)
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
    rows = denominations(catalog, region)
    if not rows:
        where = f" в регионе {region}" if region else ""
        return None, f"в каталоге поставщика нет номиналов Roblox{where}"

    wallet = is_wallet_region(region) if region else True
    if wallet:
        want = int(robux or 0)
        if want <= 0:
            return None, "в названии заказа не видно, сколько Robux нужно"
        exact = [r for r in rows if r["kind"] == "wallet" and r["robux"] == want]
        unit = f"{want} Robux"
        have = ", ".join(str(q) for q in sorted(
            {r["robux"] for r in rows if r["kind"] == "wallet"})) or "ничего"
    else:
        # Подарочная карта: сравнивается номинал в её валюте, а не Robux.
        # Число из названия заказа здесь — это доллары или евро, и принять
        # их за Robux значит ошибиться в сотню раз, притом молча.
        want_face = float(robux or 0)
        if want_face <= 0:
            return None, ("в названии заказа не видно номинала карты — "
                          "например «$10» или «EUR 25»")
        exact = [r for r in rows
                 if r["kind"] == "card" and abs(r["face"] - want_face) < 0.001]
        unit = f"номинал {want_face:g}"
        have = ", ".join(
            f"{q:g}" for q in sorted({r["face"] for r in rows
                                      if r["kind"] == "card"})) or "ничего"

    if not exact:
        return None, (f"{unit} у поставщика нет"
                      + (f" в регионе {region}" if region else "")
                      + f". Есть: {have}. Складывать несколько кодов бот "
                        f"не умеет")
    in_stock = [r for r in exact if r["in_stock"] > 0]
    if not in_stock:
        return None, f"{unit} есть, но остаток нулевой"
    return in_stock[0], ""


def region_line(region: str) -> str:
    """Строка региона для описания товара — та, которую бот потом и читает."""
    return f"{REGION_PREFIX} {str(region or '').strip().upper()}"


def with_region(description: str, region: str) -> str:
    """Дописать регион в описание, не задвоив его при повторном создании."""
    text = str(description or "").rstrip()
    kept = [ln for ln in text.split("\n")
            if not ln.strip().lower().startswith(REGION_PREFIX.lower())]
    body = "\n".join(kept).rstrip()
    line = region_line(region)
    return f"{body}\n\n{line}" if body else line


def region_from_description(description: str) -> tuple[str, str]:
    """Регион товара из его описания → (регион, чем узнали).

    Выбора региона у Юмаркета нет: 19.08 проверено — категория 14 отдаёт
    ноль фильтров, а в заказе от объявления приходит только `ad_id`.
    Описание остаётся единственным местом, где регион переживает создание
    товара, поэтому туда его пишет бот и оттуда же читает.

    Порядок: сначала своя строка, потом слова. Точная запись главнее
    догадки — иначе описание «глобальный аккаунт не нужен, код RU» прочтётся
    как глобальный код.

    Вторым значением возвращается способ (`строка`/`слова`/пусто), чтобы
    отчёт мог сказать, откуда взялся регион: догадка, поданная как факт,
    в этом проекте уже стоила дней.
    """
    text = str(description or "")
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith(REGION_PREFIX.lower()):
            value = stripped[len(REGION_PREFIX):].strip().upper()
            # Берём первое слово: «GL (глобальный)» — это GL.
            value = re.split(r"[\s,(/]+", value)[0] if value else ""
            if value:
                return value, "строка"
    low = f" {text.lower()} "
    hits = {code for code, words in _REGION_WORDS.items()
            if any(w in low for w in words)}
    if len(hits) == 1:
        return hits.pop(), "слова"
    # Ни одного или несколько — это «не знаем». Выбрать из двух наугад
    # значит купить код не того региона: покупатель его не активирует.
    return "", ""


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


def is_masked_code(code: str) -> bool:
    """`****9012` — это замазанный код, а не код.

    `GET /orders` без `unhide=true` отдаёт коды скрытыми: звёздочки и
    последние четыре символа. Отправить такое покупателю значит отчитаться
    о выдаче, которой не было, — притом ответ приходит успешный, поле на
    месте, и заметить это со стороны нечем. Проверка стоит здесь не потому,
    что мы забудем `unhide`, а потому что забыть его — единственный способ
    ошибиться незаметно.
    """
    return str(code or "").lstrip().startswith("*")


def codes_from_result(data) -> list[str]:
    """Коды из ответа поставщика. `pin` — это и есть код гифт-карты.

    Форм ответа две, и они разные:

    * покупка — `data.result.vouchers[].pin`;
    * список заказов (`GET /orders`) — `data.page.items[].vouchers[].pin`.

    Второй путь появился после того, как выяснилось, что после обрыва связи
    мы не нашли бы код даже там, где он есть: искали не по тому пути.

    Замазанные коды отбрасываются. Если после этого не осталось ни одного —
    это «кода нет», а не «выдали»: пустой список означает отказ, и доложить
    об успехе, не получив кода, — худшее, что может сделать этот модуль.
    """
    out: list[str] = []
    if not isinstance(data, dict):
        return out

    def take(vouchers) -> None:
        for voucher in (vouchers or []):
            if not isinstance(voucher, dict):
                continue
            pin = voucher.get("pin")
            if pin and not is_masked_code(pin):
                out.append(str(pin))

    result = data.get("result")
    if isinstance(result, dict):
        take(result.get("vouchers"))
    take(data.get("vouchers"))

    page = data.get("page")
    if isinstance(page, dict):
        for row in (page.get("items") or []):
            if isinstance(row, dict):
                out.extend(codes_from_result(row))
    for row in (data.get("items") or []):
        if isinstance(row, dict) and row.get("vouchers"):
            take(row.get("vouchers"))
    for order in (data.get("orders") or []):
        if isinstance(order, dict):
            out.extend(codes_from_result(order))
    return out
