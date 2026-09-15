"""Что и на сколько покупать: номинал из названия товара, регион из описания.

Тут живут две задачи, обе — чистые функции без сети. Именно поэтому они
покрываются тестами напрямую, и именно поэтому ошибка в них ловится до
того, как спишутся деньги.

Главное правило раздела: **не угадывать**. Не нашли номинал — отказ с
причиной, а не «возьмём похожий». Купленный не тот номинал — это деньги
продавца и недовольный покупатель, а не мелкая неточность.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Card:
    """Вид товара, который умеем выдавать."""
    slug: str                       # "robux", "steam"
    title: str                      # "Roblox Gift Cards"
    emoji: str = "🎁"
    # По каким словам узнаём заказ этого вида в названии товара.
    keywords: tuple[str, ...] = ()
    # Как называется единица номинала: "робуксов", "₽", "$".
    measure: str = ""
    # Что написать покупателю вместе с кодом.
    activation: str = "Активируйте код на официальном сайте."
    # Услуги поставщика, где искать номиналы: {регион: service_id}
    services: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Узнавание заказа
# ---------------------------------------------------------------------------

def is_card_order(card: Card, title: str, keyword: str = "") -> bool:
    """Наш ли это заказ.

    Своё слово продавца (`keyword`) означает «только оно»: он задал его,
    чтобы отделить свои товары от чужих, и подмешивать к нему наши догадки
    значит отменять его решение.
    """
    text = " ".join(str(title or "").lower().split())
    if keyword.strip():
        return keyword.strip().lower() in text
    return any(k.lower() in text for k in card.keywords)


def pick_card(cards: list[Card], title: str, conf_of) -> Card | None:
    """Кому достанется заказ. ПЕРВАЯ признавшая забирает.

    Название вида «Apple или Xbox» признали бы обе, и без остановки на
    первой бот купил бы два кода на один оплаченный заказ.
    """
    for card in cards:
        conf = conf_of(card.slug) or {}
        if not conf.get("enabled"):
            continue
        if is_card_order(card, title, str(conf.get("keyword") or "")):
            return card
    return None


# ---------------------------------------------------------------------------
# Номинал из названия
# ---------------------------------------------------------------------------

_NUM = re.compile(r"(\d[\d\s  ]*(?:[.,]\d+)?)")


def nominal_from_title(title: str) -> float | None:
    """Число из названия товара: «Roblox 1000 Robux» → 1000.

    Берётся САМОЕ КРУПНОЕ число, а не первое. В названиях попадаются
    «Roblox Gift Card 10 USD (1000 Robux)» и «Steam 500 ₽ — скидка 5%»:
    первое число там бывает и годом, и процентом, и версией.
    """
    best: float | None = None
    for raw in _NUM.findall(str(title or "")):
        clean = raw.replace(" ", "").replace(" ", "").replace(
            " ", "").replace(",", ".")
        try:
            val = float(clean)
        except ValueError:
            continue
        if best is None or val > best:
            best = val
    return best


# ---------------------------------------------------------------------------
# Регион из описания
# ---------------------------------------------------------------------------

_REGION = re.compile(
    r"(?:регион|region)[^\wа-яё]{0,4}(?:кода|code)?[^\wа-яё]{0,4}"
    r"([A-Za-zА-Яа-яЁё]{2,12})", re.I)

# Глобальный и российский коды невзаимозаменяемы: выдать не тот регион —
# это возврат, а не мелочь.
_ALIASES = {
    "GLOBAL": "GL", "ГЛОБАЛ": "GL", "ГЛОБАЛЬНЫЙ": "GL", "GL": "GL",
    "РОССИЯ": "RU", "РФ": "RU", "RU": "RU", "RUS": "RU",
    "США": "US", "US": "US", "USA": "US",
    "ТУРЦИЯ": "TR", "TR": "TR", "ЕВРОПА": "EU", "EU": "EU",
}


def region_from_description(text: str) -> str:
    """Регион кода из описания товара: «Регион кода: US» → "US".

    Читается ИЗ ОПИСАНИЯ, а не из настроек плагина, потому что у продавца
    товаров много и регионы у них разные. Настройка остаётся запасным
    вариантом — для товаров, заведённых до того, как регион стали писать.
    """
    m = _REGION.search(str(text or ""))
    if not m:
        return ""
    word = m.group(1).strip().upper()
    return _ALIASES.get(word, word if word.isascii() and len(word) <= 3 else "")


# ---------------------------------------------------------------------------
# Подбор номинала в каталоге поставщика
# ---------------------------------------------------------------------------

@dataclass
class Denomination:
    """Номинал у поставщика."""
    service_id: str
    item_id: str            # он же denominationId при покупке
    value: float            # 1000
    title: str              # "1000 Robux (Global)"
    price: float | None = None
    in_stock: int = 0
    region: str = ""


def match_denomination(rows: list[Denomination], region: str,
                       want: float | None) -> tuple[Denomination | None, str]:
    """Найти ТОЧНЫЙ номинал → (номинал, причина отказа).

    Три правила, каждое против потери денег:

    * **регион сначала.** Правильный номинал чужого региона хуже, чем
      отказ: покупатель не активирует код и откроет спор;
    * **точное совпадение.** «Ближайший» номинал — это либо недодать, либо
      переплатить за продавца. Ни то ни другое он не просил;
    * **остаток проверяется здесь и ещё раз перед покупкой.** Каталог
      кешируется, и «есть в наличии» в нём может быть вчерашним.
    """
    if want is None:
        return None, ("в названии товара нет числа — непонятно, какой "
                      "номинал покупать")
    same_region = [r for r in rows if not region or not r.region
                   or r.region.upper() == region.upper()]
    if not same_region:
        return None, f"у поставщика нет номиналов региона {region}"

    exact = [r for r in same_region if abs(r.value - want) < 1e-9]
    if not exact:
        near = ", ".join(str(int(r.value)) for r in sorted(
            same_region, key=lambda r: r.value)[:8])
        return None, (f"номинала {want:g} у поставщика нет. Есть: {near}. "
                      f"Подбирать похожий бот не станет — это чужие деньги")

    live = [r for r in exact if r.in_stock > 0]
    if not live:
        return None, f"номинал {want:g} есть в каталоге, но его нет в наличии"

    # Дешевле — лучше: номинал один и тот же, разница только в закупке.
    live.sort(key=lambda r: (r.price if r.price is not None else 1e9))
    return live[0], ""


def order_reference(prefix: str, card_slug: str, order_id: str) -> str:
    """Ссылка покупки — вся защита от двойного списания.

    Считается ОДИН раз и хранится в записи журнала. Пересчитывать её при
    повторе нельзя: изменится хоть один символ — поставщик не узнает заказ,
    ответит не `IDEMPOTENCY_REPLAY`, а новой покупкой, и деньги спишутся
    второй раз.

    Префикс отделяет площадки друг от друга: ссылка уникальна в пределах
    кабинета поставщика, а кабинет один на все площадки продавца.
    """
    return f"{prefix}-{card_slug}-{order_id}"[:40]
