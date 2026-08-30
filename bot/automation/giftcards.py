"""Шаблон гифт-карт: один движок, много плагинов.

Замысел продавца дословно: **«одинаковые менюшки и т.д, просто ид покупки
меняется»**. Здесь это принято буквально.

* **Одинаково всё поведение** — подбор номинала, покупка, опрос, выдача,
  журнал, экран. Ни одной ветки «если это Apple, то иначе».
* **Меняется то, что покупаем** — подкатегория у поставщика, а внутри неё
  пара «регион + номинал» и даёт `denominationId`, то есть тот самый ид.

Добавить карту = дописать одну декларацию в `CARDS`. Ни строки движка.

Почему не двадцать пять копий кода, раз плагины отдельные. Дословно из
`automation/robux.py`: «Плагин был скопирован со звёзд, и от этого на экране
оказались чужие понятия». Одна копия уже принесла чужие понятия; двадцать
пять принесли бы двадцать пять расхождений в **пути к деньгам**, и пять
правок AppRoute пришлось бы держать в каждой.

---

**Одинаковыми не могут быть две вещи, и обе — текст для живого покупателя:**

1. `activation` — код Apple гасится на `appstore.com/redeem`, Xbox на
   `xbox.com/redeem`. Одна инструкция на всех означала бы, что бот уверенно
   отправляет неверный адрес.
2. `words` — по каким словам узнать заказ. Иначе не понять, какой заказ чей.

---

**Номинал меряется тремя разными вещами** — это выяснено на живом каталоге
20.08, и это главная трудность шаблона:

| вид | как выглядит у поставщика |
|---|---|
| `MONEY` | `AED 50 Apple gift card`, `$10 PlayStation®Store Wallet gift card` |
| `UNITS` | `VP 240 Valorant`, `TikTok: 1010 Coins`, `Free Fire: 100+10 Diamonds` |
| `PERIOD` | `1 Month Tinder Plus`, `Nintendo Switch Online: 3 month subscription` |

Вид — часть декларации, а не догадка движка: подставить «ну наверное деньги»
значит однажды продать `VP 240` как двести сорок долларов. И одна
подкатегория поставщика может дать **два** плагина: у Nintendo лежат и карты
в валюте, и подписки, а для покупателя это разные товары.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Чем меряется номинал
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """Вид номинала. `name` — как единица зовётся у поставщика («VP»)."""

    kind: str            # "money" | "units" | "period"
    name: str = ""

    def __str__(self) -> str:                      # pragma: no cover - для отчётов
        return self.name or self.kind


MONEY = Unit("money")
PERIOD = Unit("period")


def UNITS(name: str) -> Unit:
    """Игровая единица: `UNITS("VP")`, `UNITS("Coins")`, `UNITS("Diamonds")`."""
    return Unit("units", str(name or "").strip())


# ---------------------------------------------------------------------------
# Разбор номинала из названия
# ---------------------------------------------------------------------------

_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "₽": "RUB"}

# Запятая перед ровно тремя цифрами — разделитель тысяч, а не десятичная
# точка. Проверено на живом каталоге 20.08: таких названий 92, а запятой в
# роли десятичной там **ноль**. Без этого «Roblox: 4,500 Robux for Xbox»
# читалось как 4.5 Robux, «IDR 500,000» как 500 идр, а «$1,000 Amazon» как
# один доллар: номинал уменьшался в тысячу раз и переставал находиться.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _as_number(raw: str) -> float:
    """Число из названия поставщика. Пустое или непонятное — ноль."""
    text = _THOUSANDS.sub("", str(raw or "").strip())
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return 0.0
# Срок подписки приводится к единственному числу: «3 months» и «3 Month» —
# одно и то же, а сравнивать их как разные значило бы не найти номинал,
# который есть.
_PERIODS = ("day", "week", "month", "year")


def face_value(name: str) -> tuple[float, str]:
    """Номинал в деньгах: «$10 Xbox gift card» → `(10.0, "USD")`.

    Число само по себе ничего не значит без валюты: «10» бывает и долларами,
    и евро, и реалами. Пара возвращается целиком, чтобы сравнить их случайно
    было нельзя.

    Код валюты обязан быть **отдельным словом**. Без границ `\\b` три буквы
    брались из середины: «1000 Robux» читалось как тысяча валюты «ROB», то
    есть номинал в Robux притворялся номиналом карты.

    Разбор нарочно осторожный. На живом каталоге он даёт 492 из 492 у Apple
    и **0 из 796 у Tinder** — и второе тоже правильно: `1 Month Tinder Plus`
    деньгами не меряется, и притворяться, что меряется, здесь нельзя.
    """
    text = str(name or "")
    m = re.search(r"(\b[A-Za-z]{3}\b|[$€£₽])\s*([\d]+(?:[.,]\d+)*)", text)
    if not m:
        m2 = re.search(r"([\d]+(?:[.,]\d+)*)\s*(\b[A-Za-z]{3}\b|[$€£₽])", text)
        if not m2:
            return 0.0, ""
        amount, sign = m2.group(1), m2.group(2)
    else:
        sign, amount = m.group(1), m.group(2)
    value = _as_number(amount)
    if value <= 0:
        return 0.0, ""
    return value, _SIGNS.get(sign, str(sign).upper())


def units_value(name: str, unit_name: str) -> tuple[float, str]:
    """Номинал в игровой единице: «VP 240 Valorant» → `(240.0, "VP")`.

    Единица ищется по имени из декларации, а не «какое-нибудь слово рядом с
    числом»: в названии их несколько («EA SPORTS FC™ 24: 2800 FC Points» —
    здесь и 24, и 2800), и взять не то значит продать не то.

    `«Free Fire: 100+10 Diamonds»` — это «сто и десять сверху бонусом».
    Берётся **первое** число: покупатель заказывал сто, и сложить их в сто
    десять значит подменить номинал. Полное название поставщика всё равно
    уходит покупателю целиком.
    """
    text = str(name or "")
    unit = str(unit_name or "").strip()
    if not unit:
        return 0.0, ""
    u = re.escape(unit)
    # Единица перед числом («VP 240») или после него («2800 FC Points»).
    m = (re.search(rf"\b{u}\b\s*:?\s*([\d]+(?:[.,]\d+)*)", text, re.I)
         or re.search(rf"([\d]+(?:[.,]\d+)*)\s*\+?\s*[\d]*\s*\b{u}\b", text, re.I))
    if not m:
        return 0.0, ""
    value = _as_number(m.group(1))
    return (value, unit) if value > 0 else (0.0, "")


def period_value(name: str) -> tuple[float, str]:
    """Срок подписки: «Nintendo Switch Online: 3 month subscription» → `(3, "month")`."""
    text = str(name or "")
    m = re.search(rf"([\d]+)\s*({'|'.join(_PERIODS)})s?\b", text, re.I)
    if not m:
        return 0.0, ""
    try:
        return float(m.group(1)), m.group(2).lower()
    except ValueError:                              # pragma: no cover
        return 0.0, ""


def nominal_of(name: str, unit: Unit) -> tuple[float, str]:
    """Номинал из названия по виду из декларации → `(значение, мера)`.

    Пустая мера означает «не разобрали». Это не повод угадать: номинал,
    которого мы не поняли, в подбор не попадает вовсе.
    """
    if unit.kind == "money":
        return face_value(name)
    if unit.kind == "units":
        return units_value(name, unit.name)
    if unit.kind == "period":
        return period_value(name)
    return 0.0, ""                                  # pragma: no cover


def nominal_text(value: float, measure: str) -> str:
    """Номинал одной строкой: `(50.0, "AED")` → «AED 50».

    Из этой же строки его потом читает `nominal_from_title`, поэтому вид
    у неё ровно тот, который разбирается обратно.
    """
    if not measure:
        return ""
    amount = f"{value:g}"
    if measure in _PERIODS:
        return f"{amount} {measure}"
    return f"{measure} {amount}"


# ---------------------------------------------------------------------------
# Регион: он живёт в описании товара, потому что больше ему негде
# ---------------------------------------------------------------------------

# Строка, которой регион записывается в описание товара. У Юмаркета выбора
# региона нет вовсе — 19.08 проверено: категория отдаёт ноль фильтров, — и
# описание остаётся единственным местом, где регион переживает создание
# объявления. Для карт это важнее, чем для Robux: карта US не активируется
# на аккаунте RU вообще никак.
REGION_PREFIX = "Регион кода:"

# Как регион может быть назван в чужом или старом описании. Читается только
# если строки `REGION_PREFIX` нет: точная запись всегда главнее догадки.
REGION_WORDS = {
    "GL": ("глобальн", "global", "worldwide", "любой регион"),
    "RU": ("росси", "russia", " ru ", "рф"),
    "US": ("сша", "usa", " us "),
    "EU": ("европ", "europe", " eu "),
    "UK": ("великобритан", "британ", " uk "),
    "TR": ("турц", "turkey"),
    "BR": ("бразил", "brazil"),
    "DE": ("герман", "germany"),
    "FR": ("франц", "france"),
    "PL": ("польш", "poland"),
    "JP": ("япон", "japan"),
    "IN": ("инди", "india"),
    "AE": ("оаэ", "эмират", "uae"),
    "KZ": ("казахст", "kazakh"),
    "UA": ("украин", "ukrain"),
}


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
    """Регион товара из описания → `(регион, чем узнали)`.

    Порядок: сначала своя строка, потом слова. Точная запись главнее
    догадки — иначе описание «глобальный аккаунт не нужен, код RU»
    прочтётся как глобальный код.

    Вторым значением возвращается способ (`строка`/`слова`/пусто), чтобы
    отчёт мог сказать, откуда взялся регион: догадка, поданная как факт,
    в этом проекте уже стоила дней.
    """
    text = str(description or "")
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith(REGION_PREFIX.lower()):
            value = stripped[len(REGION_PREFIX):].strip().upper()
            value = re.split(r"[\s,(/]+", value)[0] if value else ""
            if value:
                return value, "строка"
    low = f" {text.lower()} "
    hits = {code for code, words in REGION_WORDS.items()
            if any(w in low for w in words)}
    if len(hits) == 1:
        return hits.pop(), "слова"
    # Ни одного или несколько — это «не знаем». Выбрать из двух наугад
    # значит купить код не того региона: покупатель его не активирует.
    return "", ""


# ---------------------------------------------------------------------------
# Коды из ответа поставщика
# ---------------------------------------------------------------------------


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

    Замазанные коды отбрасываются. Если после этого не осталось ни одного —
    это «кода нет», а не «выдали»: доложить об успехе, не получив кода, —
    худшее, что может сделать этот модуль.
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


# ---------------------------------------------------------------------------
# Декларация карты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GiftCard:
    """Одна гифт-карта. Всё, что отличает Apple от Xbox, — здесь.

    `subcategory` — **точное** имя подкатегории у поставщика. Выбирать по
    нему, а не по слову в названии: на живом каталоге слово «xbox» находит
    47 услуг, а гифт-карт среди них 16. Остальное — подписки Game Pass,
    ключи отдельных игр, аккаунты и `Roblox Wallet Code | XBox`, то есть
    **товар чужого плагина**. Подбор по слову увёл бы его, и покупатель
    получил бы код Roblox вместо карты Xbox.
    """

    slug: str
    title: str
    emoji: str
    subcategory: str
    unit: Unit
    words: tuple[str, ...]
    activation: str
    ad_title: str = ""
    ad_text: str = ""
    # Название услуги может ещё и уточняться словом — на случай, когда одна
    # подкатегория даёт два плагина (Nintendo: карты и подписки).
    name_must_have: str = ""
    name_must_not_have: str = ""
    # Слова для подстановки раздела витрины при создании товара, от узкого к
    # широкому. Мастер возьмёт вариант, только если под слово подошёл ровно
    # один; несколько или ни одного — спросит продавца, как обычно.
    #
    # Панель 20.08: категории есть у Steam (4655), Xbox (4732) и PlayStation
    # (4676), а у Apple, Amazon и Razer их **нет вовсе** — там подстановке
    # не за что зацепиться, и мастер честно спросит. Подкатегория у
    # пополнений называется «Пополнение баланса», у Robux — «Игровая
    # валюта»: подкатегории «Робуксы» в панели не существует.
    autopick: tuple[str, ...] = ()
    # Дата замера на живом каталоге — или пусто, если семейство брали по
    # записанной таблице, не померив разбор номинала. Разница видна продавцу
    # на экране карты: «мерено» значит, что все названия семейства
    # разобрались, а не «похоже, что разберутся».
    measured: str = ""
    # Одна строка о том, что это за товар — для экрана карты и для списка,
    # где карту выбирают. Пишется про товар, а не про будущие доходы:
    # «самый ходовой номинал» и «покупают чаще всего» проверить нельзя, а
    # «код пополняет Apple ID» покупатель проверит сам.
    pitch: str = ""

    def matches_service(self, service: dict) -> bool:
        """Эта ли услуга относится к карте."""
        if not isinstance(service, dict):
            return False
        if str(service.get("subcategoryName") or "") != self.subcategory:
            return False
        low = str(service.get("name") or "").lower()
        if self.name_must_have and self.name_must_have.lower() not in low:
            return False
        if self.name_must_not_have and self.name_must_not_have.lower() in low:
            return False
        return True


APPLE = GiftCard(
    slug="apple", title="Apple", emoji="🍎",
    pitch="Пополняет Apple ID: App Store, iCloud, подписки. Логин "
          "покупателя не нужен — код он вводит сам.",
    subcategory="Apple Gift Cards", unit=MONEY, measured="20.08",
    words=("apple", "эпл", "эппл", "айтюнс", "itunes", "app store", "апстор"),
    activation="Активировать: appstore.com/redeem либо Настройки → ваш Apple ID "
               "→ «Погасить подарочную карту».",
    ad_title="Apple Gift Card {номинал} ({регион})",
    ad_text="Код пополнения Apple ID. Выдача сразу после оплаты.",
)

PSN = GiftCard(
    slug="psn", title="PlayStation Store", emoji="🎮",
    pitch="Кошелёк PlayStation Store. Игры, подписки и внутриигровые "
          "покупки покупатель берёт сам.",
    subcategory="PlayStation Gift Cards", unit=MONEY, measured="20.08",
    words=("playstation", "плейстейшен", "плейстешн", "плейстейшн", "psn",
           "пс стор", "ps store"),
    autopick=("playstation", "пополнение баланса"),
    activation="Активировать: PlayStation Store → «Активировать код».",
    ad_title="PlayStation Store {номинал} ({регион})",
    ad_text="Код пополнения кошелька PlayStation. Выдача сразу после оплаты.",
)


# Roblox. Все 26 услуг лежат в одной подкатегории поставщика — проверено
# живьём 20.08, — а кошельковые коды отличаются словом «Wallet Code» в
# названии. Какое-то время под второе семейство, номинированное деньгами,
# стояла отдельная карта `roblox_card`; **21.08 продавец её снял**: в его
# магазине это один и тот же товар, и два плагина на него только путали.
#
# `name_must_have` при этом остался. Сняли карту, а не различение: если
# денежные услуги в каталоге всё-таки есть, без фильтра они попали бы сюда
# и **молча исчезли** из списка номиналов — мера у них другая, разобрать её
# как Robux нельзя. Молча исчезнувший товар в этом проекте дороже лишнего
# фильтра. Что там на самом деле, показывает «📦 Наличие и баланс».
ROBUX = GiftCard(
    slug="robux", title="Roblox Gift Cards", emoji="🎮",
    pitch="Robux на аккаунт по коду. Ник не нужен: покупатель "
          "активирует код в самом Roblox.",
    subcategory="Roblox Gift Cards", unit=UNITS("Robux"), measured="20.08",
    name_must_have="Wallet Code",
    # Слова узкие нарочно, и порядок в реестре тоже: «roblox» подходит и
    # картам, поэтому здесь его нет. Заказ «Roblox Gift Card $10» пройдёт
    # мимо этой карты и достанется следующей — а «1000 Robux» заберётся
    # здесь, потому что слово «Robux» стоит в названии всякого такого
    # товара. Продавцу, у которого товар назван одним словом «Роблокс»,
    # экран советует задать своё слово-опознаватель.
    words=("robux", "робукс", "робуксы", "робуксов"),
    autopick=("robux", "игровая валюта", "roblox"),
    activation="Активировать: в самом Roblox → Пополнить → Использовать код. "
               "Robux зачислятся сразу на ваш аккаунт.",
    ad_title="Roblox {номинал} ({регион})",
    ad_text="Код на пополнение Robux. Выдача сразу после оплаты.",
)


# Следующие четыре объявлены после замера на живом каталоге 20.08: у всех
# номинал разбирается **на 100%**, все в наличии и все дешёвые. Семейства,
# где разбор неполный, сюда не берутся — Tinder меряется сроком подписки
# (0% денег из 796), Valorant смешан с VP (57%), и обоим нужна своя мера.
XBOX = GiftCard(
    slug="xbox", title="Xbox", emoji="🟩",
    pitch="Кошелёк Xbox: игры, Game Pass, дополнения. Код работает в "
          "своём регионе.",
    subcategory="Xbox Gift Cards", unit=MONEY, measured="20.08",
    words=("xbox", "иксбокс", "хбокс", "хбох", "иксбох"),
    autopick=("xbox", "пополнение баланса"),
    activation="Активировать: xbox.com/redeem либо на консоли → «Использовать код».",
    ad_title="Xbox Gift Card {номинал} ({регион})",
    ad_text="Код пополнения Xbox. Выдача сразу после оплаты.",
)

STEAM = GiftCard(
    slug="steam", title="Steam", emoji="🎲",
    pitch="Пополнение кошелька Steam — деньги на игры и внутриигровые "
          "покупки.",
    subcategory="Steam Wallet Gift Cards", unit=MONEY, measured="20.08",
    words=("steam", "стим"),
    autopick=("steam", "пополнение баланса"),
    activation="Активировать: Steam → «Добавить средства» → «Активировать код "
               "пополнения кошелька».",
    ad_title="Steam Wallet {номинал} ({регион})",
    ad_text="Код пополнения кошелька Steam. Выдача сразу после оплаты.",
)

AMAZON = GiftCard(
    slug="amazon", title="Amazon", emoji="📦",
    pitch="Оплата покупок на Amazon. Номиналов много, начиная с совсем "
          "мелких.",
    subcategory="Amazon Gift Cards", unit=MONEY, measured="20.08",
    words=("amazon", "амазон"),
    activation="Активировать: в аккаунте Amazon → «Подарочные карты» → "
               "«Активировать код». Карта действует в магазине своей страны.",
    ad_title="Amazon Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Amazon. Выдача сразу после оплаты.",
)

RAZER = GiftCard(
    slug="razer", title="Razer Gold", emoji="🐍",
    pitch="Razer Gold — единая валюта для оплаты игр без банковской карты.",
    subcategory="Razer Gold Gift Cards", unit=MONEY, measured="20.08",
    words=("razer", "рейзер", "разер", "razer gold"),
    activation="Активировать: Razer Gold → «Пополнить» → ввести PIN.",
    ad_title="Razer Gold {номинал} ({регион})",
    ad_text="Код пополнения Razer Gold. Выдача сразу после оплаты.",
)


# Шесть карт по решению продавца 21.08: «делай те, что схожи с Xbox, Apple и
# Roblox». Схожесть здесь означает одно — **номинал меряется деньгами**.
#
# Строки подкатегорий взяты из живого каталога, снятого 20.08
# (`docs/plan_giftcards_template.md`, §2), с точностью до символа: у этих
# шести слово «Card» стоит в единственном числе, в отличие от `Apple Gift
# Cards`. Пересказ по памяти стоил бы карты, которая не находит ни одной
# услуги и молчит.
#
# **Чего у них нет и о чём сказано вслух:** разбор номинала на живом
# каталоге у этих шести не мерян — отсюда пустое `measured`. У прежних
# семи он мерян и дал 100 %; здесь основание слабее: семейства выбраны по
# тому, что каталог показывает их номиналы деньгами (`AED 50`, `$10`), а не
# игровой единицей или сроком. Проверяется это кнопкой «🔬 Что ещё можно
# завести» за один запрос.
#
# Ошибка в эту сторону не продаёт лишнего: номинал подбирается **точным**
# совпадением значения и меры в своём регионе, и непонятый номинал
# оборачивается отказом, а не выдачей не того.
#
# Смешанные семейства сюда не взяты намеренно. У Nintendo рядом с картами
# лежат подписки, у EA — FC Points, у TikTok — Coins, у Valorant и Riot —
# VP и Riot Cash, у Free Fire — Diamonds, у Tinder всё меряется сроком.
# Каждому нужна своя мера, и объявить их деньгами значит завести товар,
# который бот не поймёт.
AIRBNB = GiftCard(
    slug="airbnb", title="Airbnb", emoji="🏠",
    pitch="Сертификат Airbnb — оплата жилья и впечатлений.",
    subcategory="Airbnb Gift Card", unit=MONEY,
    words=("airbnb", "эйрбиэнби", "эйр би эн би", "аирбнб"),
    activation="Активировать: в аккаунте Airbnb — раздел подарочных карт. "
               "Карта работает в валюте своего региона.",
    ad_title="Airbnb Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Airbnb. Выдача сразу после оплаты.",
)

ENEBA = GiftCard(
    slug="eneba", title="Eneba", emoji="🛒",
    pitch="Баланс Eneba — площадки ключей, карт и подписок.",
    subcategory="Eneba Gift Card", unit=MONEY,
    words=("eneba", "энеба"),
    activation="Активировать: в аккаунте Eneba — пополнение баланса кодом.",
    ad_title="Eneba Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Eneba. Выдача сразу после оплаты.",
)

TWITCH = GiftCard(
    slug="twitch", title="Twitch", emoji="💜",
    pitch="Кошелёк Twitch: подписки на стримеров и биты.",
    subcategory="Twitch Gift Card", unit=MONEY,
    words=("twitch", "твич"),
    activation="Активировать: в аккаунте Twitch — раздел кошелька или "
               "подарочных карт. Карта в валюте своего региона.",
    ad_title="Twitch Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Twitch. Выдача сразу после оплаты.",
)

META_QUEST = GiftCard(
    slug="meta_quest", title="Meta Quest", emoji="🥽",
    pitch="Оплата игр и приложений в Meta Quest Store.",
    # «Квест» одним словом не берём: так зовут и игры, и услуги прохождения,
    # и карта забирала бы чужие заказы. Первая признавшая забирает заказ —
    # ошибка здесь тихая.
    subcategory="Meta Quest Gift Card", unit=MONEY,
    words=("meta quest", "мета квест", "oculus", "окулус"),
    activation="Активировать: в аккаунте Meta — раздел платежей или "
               "подарочных карт. Карта в валюте своего региона.",
    ad_title="Meta Quest Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Meta Quest. Выдача сразу после оплаты.",
)

BATTLENET = GiftCard(
    slug="battlenet", title="Battle.net", emoji="⚔️",
    pitch="Баланс Battle.net — игры Blizzard и внутриигровые покупки.",
    subcategory="Battle.net Gift Card", unit=MONEY,
    words=("battle.net", "battlenet", "battle net", "баттлнет", "батлнет",
           "близзард", "blizzard"),
    activation="Активировать: в аккаунте Battle.net — пополнение баланса "
               "кодом. Баланс в валюте своего региона.",
    ad_title="Battle.net Gift Card {номинал} ({регион})",
    ad_text="Подарочная карта Battle.net. Выдача сразу после оплаты.",
)

OZON = GiftCard(
    slug="ozon", title="OZON.ru", emoji="🔵",
    pitch="Сертификат OZON — оплата любых покупок на маркетплейсе.",
    subcategory="OZON.ru Gift Card", unit=MONEY,
    words=("ozon", "озон"),
    activation="Активировать: на OZON — раздел сертификатов.",
    ad_title="OZON.ru {номинал} ({регион})",
    ad_text="Подарочный сертификат OZON. Выдача сразу после оплаты.",
)


# Реестр. Из него строится меню, по нему же идут общие тесты — один тест на
# все карты сразу, и это главный выигрыш шаблона.
#
# **Порядок значим.** Заказ забирает первая признавшая карта, поэтому узкие
# объявления стоят раньше широких. Пока карт Roblox было две, `ROBUX` со
# словом «robux» стоял перед картой со словом «roblox» — иначе вторая
# забирала бы заказы на Robux. Карту сняли, правило осталось: оно понадобится
# первой же паре, где одно слово входит в другое.
CARDS: tuple[GiftCard, ...] = (ROBUX, APPLE, PSN,
                               XBOX, STEAM, AMAZON, RAZER,
                               AIRBNB, ENEBA, TWITCH, META_QUEST,
                               BATTLENET, OZON)


def cards() -> tuple[GiftCard, ...]:
    return CARDS


def card(slug: str) -> GiftCard | None:
    want = str(slug or "").strip().lower()
    return next((c for c in CARDS if c.slug == want), None)


# ---------------------------------------------------------------------------
# Каталог поставщика
# ---------------------------------------------------------------------------


def _catalog_items(catalog) -> list:
    items = (catalog or {}).get("items") if isinstance(catalog, dict) else catalog
    return [s for s in (items or []) if isinstance(s, dict)]


def services(catalog, gift: GiftCard) -> list[dict]:
    """Услуги поставщика, относящиеся к этой карте."""
    return [s for s in _catalog_items(catalog) if gift.matches_service(s)]


# Суффикс «| XX» из названия услуги. Короткий и без пробелов — иначе это
# не регион, а часть имени товара.
_SUFFIX = re.compile(r"^[A-Z0-9]{2,5}$")


def region_of(service: dict) -> str:
    """Регион услуги. Сначала суффикс «| XX» из названия, потом `countryCode`.

    Прежде здесь стоял `countryCode` — «совпал с суффиксом во всех 90
    услугах трёх семейств». Для Apple, PlayStation и подарочных карт Roblox
    это правда, а для кошельковых кодов Roblox — нет, и цена ошибки та
    самая: `Roblox Wallet Code | XBox` и `| GL` оба помечены `GLOB`, то
    есть сливаются в один регион. Номинал 4500 есть в обоих, и покупатель
    с ПК мог получить код, который работает только на Xbox.

    По всему каталогу 20.08 суффикс и `countryCode` расходятся у 242 услуг
    из 849. Из объявленных карт слив был ровно один — тот самый Robux.

    Суффикс главнее ещё и потому, что его пишет поставщик именно чтобы
    различать товары, а `countryCode` у него — грубая пометка «глобальный».
    """
    name = str((service or {}).get("name") or "")
    if "|" in name:
        suffix = name.split("|")[-1].strip().upper()
        if _SUFFIX.match(suffix):
            return suffix
    return str((service or {}).get("countryCode") or "").strip().upper()


def denominations(catalog, gift: GiftCard, region: str = "") -> list[dict]:
    """Номиналы карты из живого ответа `GET /services`.

    `denomination_id` — это `id` **номинала**, а не услуги: именно он уходит
    в `orders[].denominationId`, и перепутать их значит получить отказ по
    схеме.

    Номиналы, которые разобрать не удалось, сюда не попадают вовсе: продать
    то, чего мы не поняли, нельзя.
    """
    want = str(region or "").strip().upper()
    out: list[dict] = []
    for service in services(catalog, gift):
        reg = region_of(service)
        if want and reg != want:
            continue
        for row in (service.get("items") or []):
            if not isinstance(row, dict):
                continue
            title = str(row.get("name") or "")
            value, measure = nominal_of(title, gift.unit)
            if value <= 0 or not measure:
                continue
            out.append({
                "value": value,
                "measure": measure,
                "nominal": nominal_text(value, measure),
                "denomination_id": str(row.get("id") or ""),
                "price": float(row.get("price") or 0),
                "currency": str(row.get("currency") or ""),
                "in_stock": int(row.get("inStock") or 0),
                "region": reg,
                "service_id": str(service.get("id") or ""),
                "name": title,
            })
    # Дешевле за единицу — выше. При равном номинале выбирать наугад нельзя,
    # а по цене — можно объяснить.
    out.sort(key=lambda r: (r["measure"], r["value"],
                            r["price"] / (r["value"] or 1)))
    return out


def regions(catalog, gift: GiftCard) -> list[dict]:
    """Регионы карты как они есть в каталоге, а не по памяти.

    Зашитый перечень однажды промолчит про новый регион или предложит
    исчезнувший: у поставщика их от одного (OZON) до 63 (Tinder).
    """
    out: dict[str, dict] = {}
    for row in denominations(catalog, gift):
        cell = out.setdefault(row["region"], {"region": row["region"],
                                              "count": 0, "in_stock": 0})
        cell["count"] += 1
        if row["in_stock"] > 0:
            cell["in_stock"] += 1
    return sorted(out.values(), key=lambda r: r["region"])


def currencies_of(catalog, gift: GiftCard, region: str) -> list[str]:
    """Какими мерами меряются номиналы этого региона.

    Нужна, чтобы понять, однозначно ли «10» в названии заказа: если мера в
    регионе одна, догадываться не приходится, а если их две — это отказ,
    а не выбор наугад.
    """
    return sorted({r["measure"] for r in denominations(catalog, gift, region)})


# ---------------------------------------------------------------------------
# Заказ на витрине
# ---------------------------------------------------------------------------

# Числа, которые в названии товара номиналом не являются: год, «24/7»,
# проценты и тому подобное соседство.
_NOT_A_NOMINAL = re.compile(r"(20\d\d|24/7|\d+\s*%)")


# Слова, которые стоят рядом с числом, но единицей товара не являются.
# Без этого «10 USD» дало бы единицу «USD», а «3 month» — «month», и замер
# объявил бы готовым раздел, который на самом деле меряется деньгами или
# сроком: обе меры у нас уже есть, и подменять их выдуманной третьей значит
# завести товар, который выдача потом не поймёт.
_NOT_A_UNIT = frozenset(
    ("usd", "eur", "gbp", "rub", "try", "brl", "inr", "aud", "cad", "pln",
     "sar", "aed", "jpy", "mxn", "ars", "clp", "cop", "idr", "myr", "php",
     "sgd", "thb", "vnd", "zar", "chf", "nok", "sek", "dkk", "czk", "huf",
     "ils", "nzd", "hkd", "twd", "krw", "cny", "kzt", "uah")
    + tuple(_PERIODS) + ("gift", "card", "code", "key", "usdt", "for", "and"))

# Слово вплотную к числу — с любой стороны. «2800 FC Points» пишет единицу
# после числа, «VP 240 Valorant» — перед ним, и смотреть только в одну
# сторону значит для половины разделов угадать не единицу, а название игры.
_AFTER_NUMBER = re.compile(r"[\d][\d.,]*\s*\+?\s*[\d]*\s*([A-Za-z][A-Za-z-]{1,19})")
_BEFORE_NUMBER = re.compile(r"([A-Za-z][A-Za-z-]{1,19})\s*:?\s*[\d][\d.,]*")


def guess_unit(names) -> str:
    """Чем, похоже, меряется номинал в этом разделе — по самому частому
    слову вплотную к числу.

    Это **догадка для замера**, а не для выдачи. Нужна она затем, что мера
    задаётся в декларации карты, а декларацию ещё только предстоит написать:
    померить раздел, не зная его меры, иначе нельзя. Валюты и сроки из
    кандидатов выброшены — для них у нас свои меры.

    При равной частоте побеждает короткое слово: в «VP 240 Valorant» и то и
    другое стоит рядом с числом, но единица здесь `VP`, а `Valorant` —
    название игры. Написание возвращается то, каким его пишет поставщик:
    оно уйдёт в название товара, и «240 vp» там читается как небрежность.
    """
    counts: dict[str, int] = {}
    spelling: dict[str, dict[str, int]] = {}
    for name in names or []:
        text = str(name or "")
        for pattern in (_AFTER_NUMBER, _BEFORE_NUMBER):
            for word in pattern.findall(text):
                low = word.lower()
                if low in _NOT_A_UNIT or len(low) < 2:
                    continue
                counts[low] = counts.get(low, 0) + 1
                seen = spelling.setdefault(low, {})
                seen[word] = seen.get(word, 0) + 1
    if not counts:
        return ""
    best = max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return max(spelling[best].items(), key=lambda kv: kv[1])[0]


def survey(catalog) -> list[dict]:
    """Замер каталога по разделам поставщика: чем меряется номинал и
    разбирается ли он вообще.

    Отвечает на единственный вопрос, с которого начинается новая карта:
    **можно ли её заводить**. Раздел, у которого номинал разбирается не у
    всех услуг, — это товар, который бот однажды не поймёт и не выдаст, уже
    после оплаты покупателем. Прошлый заход по этому замеру взял четыре
    карты и отверг Tinder (0 % деньгами из 796 названий) и Valorant (57 %,
    смешан с VP) — и отверг правильно.

    Меры перебираются все три: деньги, штуки (по угаданной единице) и срок.
    Побеждает та, что разобрала больше. Ноль по всем трём — не «плохой
    раздел», а «мера ещё не написана».

    Возвращает по строке на раздел: имя (его же надо положить в
    `subcategory` декларации), сколько услуг и номиналов, сколько в
    наличии, лучшая мера с долей разбора, самая дешёвая цена в наличии и
    заведена ли уже карта.
    """
    taken = {c.subcategory: c for c in CARDS}
    groups: dict[str, list[dict]] = {}
    for service in _catalog_items(catalog):
        name = str(service.get("subcategoryName") or "").strip()
        if not name:
            continue
        groups.setdefault(name, []).append(service)

    out: list[dict] = []
    for name, rows in groups.items():
        titles = [str(s.get("name") or "") for s in rows]
        nominals = [i for s in rows for i in (s.get("items") or [])
                    if isinstance(i, dict)]
        in_stock = [i for i in nominals if int(i.get("inStock") or 0) > 0]
        prices = [float(i.get("price") or 0) for i in in_stock
                  if float(i.get("price") or 0) > 0]

        unit_name = guess_unit(titles)
        candidates = [("деньги", MONEY), ("срок", PERIOD)]
        if unit_name:
            candidates.insert(1, (unit_name, UNITS(unit_name)))
        best_measure, best_share = "", 0.0
        for label, unit in candidates:
            got = sum(1 for t in titles if nominal_of(t, unit)[1])
            share = got / len(titles) if titles else 0.0
            if share > best_share:
                best_measure, best_share = label, share

        out.append({
            "subcategory": name,
            "services": len(rows),
            "nominals": len(nominals),
            "in_stock": len(in_stock),
            "measure": best_measure,
            "share": best_share,
            "cheapest": min(prices) if prices else 0.0,
            "card": taken.get(name).title if name in taken else "",
            # Готов — значит номинал разбирается у всех до одной услуги и
            # есть что купить. «Почти всё» здесь не годится: неразобранная
            # услуга это оплаченный заказ, который бот не выдаст.
            "ready": best_share >= 1.0 and bool(in_stock) and name not in taken,
        })
    out.sort(key=lambda r: (not r["ready"], bool(r["card"]), -r["share"],
                            -r["in_stock"]))
    return out


def is_card_order(gift: GiftCard, title: str, keyword: str = "") -> bool:
    """Заказ ли это на эту карту.

    `keyword` — слово продавца. Заданное, оно **требование**, а не подсказка:
    товар «Xbox аккаунт» не должен уходить в выдачу карт только потому, что
    в названии есть «xbox».
    """
    low = str(title or "").lower()
    if not low:
        return False
    kw = str(keyword or "").strip().lower()
    if kw:
        return kw in low
    return any(w in low for w in gift.words)


def nominal_from_title(gift: GiftCard, title: str) -> tuple[float, str]:
    """Что заказал покупатель → `(значение, мера)`.

    Сначала пробуем разобрать так же, как номинал у поставщика: названия
    товаров бот сам и составляет по заготовке, где номинал напечатан
    `nominal_text`.

    Если меры в названии нет, возвращается голое число с пустой мерой —
    решать, годится ли оно, будет `match_denomination`, который знает, одна
    мера в регионе или несколько. Догадка о мере не делается здесь никогда.
    """
    text = str(title or "")
    value, measure = nominal_of(text, gift.unit)
    if value > 0 and measure:
        return value, measure
    # Голое число рядом со словом карты: «Apple Gift Card 50 (AE)».
    low = text.lower()
    spots = [m.start() for w in gift.words for m in re.finditer(re.escape(w), low)]
    best, best_dist = None, None
    for m in re.finditer(r"\d[\d\s.,]*", low):
        raw = m.group(0)
        if _NOT_A_NOMINAL.search(raw.strip()):
            continue
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue
        number = float(digits)
        if number <= 0:
            continue
        dist = min((abs(m.start() - s) for s in spots), default=0)
        if best_dist is None or dist < best_dist:
            best, best_dist = number, dist
    return (best, "") if best is not None else (0.0, "")


def match_denomination(catalog, gift: GiftCard, region: str, value: float,
                       measure: str = "") -> tuple[dict | None, str, str]:
    """Номинал ровно на столько → `(строка каталога, причина отказа, чем узнали)`.

    **Только точное совпадение.** Ближайший сверху означал бы подарок за наш
    счёт, ближайший снизу — недоданное оплаченное. Молча подменять номинал
    здесь нельзя.

    **Номиналы разных регионов не сравниваются никогда:** `$10` в US и `$10`
    в AE — разные товары, и регион применяется до сравнения.

    Мера в заказе может быть не названа («Apple Gift Card 50»). Тогда она
    берётся из региона, но **только если она там одна**. Две — это отказ:
    выбрать наугад значит купить не тот номинал.
    """
    want = float(value or 0)
    if want <= 0:
        return None, "в названии заказа не видно номинала", ""
    if not region:
        return None, "не сказано, какой это регион", ""
    rows = denominations(catalog, gift, region)
    if not rows:
        return None, (f"у поставщика нет номиналов «{gift.title}» "
                      f"в регионе {region}"), ""

    how = "мера из названия"
    unit = str(measure or "").strip()
    if not unit:
        have = sorted({r["measure"] for r in rows})
        if len(have) != 1:
            return None, (f"в названии заказа номинал «{want:g}» без меры, "
                          f"а в регионе {region} их несколько: "
                          f"{', '.join(have)}. Допиши меру в название"), ""
        unit = have[0]
        how = f"мера одна в регионе {region}"

    exact = [r for r in rows
             if r["measure"] == unit and abs(r["value"] - want) < 0.001]
    if not exact:
        have = ", ".join(f"{v:g}" for v in sorted(
            {r["value"] for r in rows if r["measure"] == unit})) or "ничего"
        return None, (f"номинала {nominal_text(want, unit)} у поставщика нет "
                      f"в регионе {region}. Есть: {have}. Складывать "
                      f"несколько кодов бот не умеет"), how
    in_stock = [r for r in exact if r["in_stock"] > 0]
    if not in_stock:
        return None, (f"номинал {nominal_text(want, unit)} есть, "
                      f"но остаток нулевой"), how
    return in_stock[0], "", how


# ---------------------------------------------------------------------------
# Ссылка покупки, заготовки, баланс
# ---------------------------------------------------------------------------


# Состояния записи журнала. Выдачей считается ровно одно — то, при котором
# код ушёл покупателю; всё остальное либо ещё в пути, либо не состоялось.
# Считать «последней выдачей» последнюю запись любого вида значит однажды
# отчитаться о выдаче там, где был отказ.
DELIVERED_STATE = "выдан"
FAILED_STATES = frozenset((
    "не выдан", "нет в наличии", "поставщик отказал", "ответ без кода",
    "номинал не перечитан", "куплен, отправить не смогли",
))


def ago(when: float, now: float = 0.0) -> str:
    """Сколько прошло — словами и без часовых поясов.

    Абсолютное время («сегодня в 13:40») требует знать пояс продавца, а мы
    его не знаем: бот живёт на сервере, продавец — где угодно. Соврать на
    три часа в строке «последняя выдача» — мелочь, из которой складывается
    недоверие ко всему остальному.
    """
    now = now or time.time()
    gap = max(0.0, now - float(when or 0))
    if not when:
        return ""
    if gap < 90:
        return "только что"
    if gap < 3600:
        return f"{int(gap // 60)} мин назад"
    if gap < 86400:
        return f"{int(gap // 3600)} ч назад"
    days = int(gap // 86400)
    if days == 1:
        return "вчера"
    return f"{days} дн назад"


def stats(settings: dict) -> dict:
    """Что автовыдача уже сделала — по журналам карт.

    Цифры, а не прилагательные. «Работает надёжно» продавец читает как
    рекламу, «выдано 12 кодов, последний 20 минут назад» — как факт, и
    второе он может проверить в журнале.
    """
    out = {"cards_on": 0, "delivered": 0, "failed": 0, "last_at": 0.0,
           "last_card": "", "per_card": {}}
    for card in CARDS:
        conf = card_conf(settings, card.slug)
        done = len(conf.get("delivered") or [])
        log = [e for e in (conf.get("log") or []) if isinstance(e, dict)]
        failed = sum(1 for e in log if str(e.get("state")) in FAILED_STATES)
        # «Последняя выдача» — по записям о выдаче, а не по последней записи
        # вообще. Иначе несостоявшаяся попытка, случившаяся минуту назад,
        # отчитывалась бы как выдача: строка на экране бодрая, а кода
        # покупатель не получил.
        last = max((float(e.get("at") or 0) for e in log
                    if str(e.get("state")) == DELIVERED_STATE), default=0.0)
        if conf.get("enabled"):
            out["cards_on"] += 1
        out["delivered"] += done
        out["failed"] += failed
        out["per_card"][card.slug] = {"delivered": done, "last_at": last,
                                      "failed": failed}
        if last > out["last_at"]:
            out["last_at"], out["last_card"] = last, card.title
    return out


def dry_run(titles, catalog, settings: dict | None = None) -> list[dict]:
    """Сухой прогон по названиям товаров продавца — без единой покупки.

    Отвечает на вопрос, который иначе стоит денег: **узнает ли бот мои
    товары**. Движок у карт общий и проверен живой выдачей, а вот `words`
    (по ним узнаётся заказ) и разбор номинала у каждой карты свои — и
    ошибка в них видна не отказом, а тишиной: заказ оплачен, а выдача его
    просто не заметила.

    Регион здесь **не проверяется**: он лежит в описании товара, а не в
    названии, и читается при выдаче. Поэтому проверка отвечает «узнаю и
    номинал такой у поставщика есть», а не «выдам наверняка».

    По строке на товар: какая карта его забирает, что прочитано номиналом,
    и есть ли такой номинал у поставщика хоть в одном регионе.
    """
    conf_of = {}
    if isinstance(settings, dict):
        conf_of = {c.slug: card_conf(settings, c.slug) for c in CARDS}

    out: list[dict] = []
    for title in titles or []:
        text = str(title or "")
        row = {"title": text, "card": "", "nominal": "", "regions": [],
               "why": ""}
        gift = next(
            (c for c in CARDS
             if is_card_order(c, text, (conf_of.get(c.slug) or {}).get("keyword") or "")),
            None)
        if gift is None:
            row["why"] = "ни одна карта не узнала товар по названию"
            out.append(row)
            continue
        row["card"] = gift.title

        value, measure = nominal_from_title(gift, text)
        if value <= 0:
            row["why"] = "в названии не видно номинала"
            out.append(row)
            continue
        row["nominal"] = nominal_text(value, measure) if measure else f"{value:g}"

        rows = [d for d in denominations(catalog, gift)
                if abs(d["value"] - value) < 0.001
                and (not measure or d["measure"] == measure)
                and d["in_stock"] > 0]
        if not rows:
            row["why"] = "такого номинала у поставщика нет в наличии"
        else:
            row["regions"] = sorted({d["region"] for d in rows})
        out.append(row)
    return out


def order_reference(slug: str, order_id: str) -> str:
    """Ссылка покупки — придумывается ДО вызова поставщика.

    По ней заказ находится у поставщика (`GET /orders?referenceId=…`), если
    связь оборвалась, и она же делает повтор безопасным: на ту же ссылку
    приходит `IDEMPOTENCY_REPLAY` — прежний результат, а не второе списание.

    Поэтому она собирается из номера заказа, а не из времени, и **не
    зависит от номинала**: правка названия товара на витрине не должна
    менять ссылку, иначе повтор уйдёт к поставщику как новая покупка.
    """
    return f"gc-{str(slug or '').strip()}-{str(order_id or '').strip()}"[:40]


# Что можно подставить в заготовку. Список короткий нарочно: обещать в
# подсказке больше, чем подставляется, хуже, чем не обещать вовсе.
TEMPLATE_FIELDS = {
    "{номинал}": "номинал: «AED 50», «USD 10»",
    "{регион}": "регион кода: US, TR, AE…",
    "{цена}": "твоя цена в рублях",
    "{карта}": "название карты: Apple, Xbox…",
}


def fill_template(template: str, *, nominal: str = "", region: str = "",
                  price: int = 0, title: str = "") -> str:
    """Подставить значения в заготовку продавца.

    Неизвестные подстановки не трогаются: продавец увидит `{чтото}` в
    предпросмотре и поймёт, что опечатался. Молча стереть их значило бы
    выпустить товар с дырой в названии, и заметил бы он это на витрине.
    """
    out = str(template or "")
    for mark, value in (("{номинал}", nominal),
                        ("{регион}", str(region or "").upper()),
                        ("{цена}", price), ("{карта}", title)):
        out = out.replace(mark, str(value))
    return out.strip()


def affordable(catalog, gift: GiftCard, balance_usd: float,
               region: str = "") -> dict:
    """Что можно купить на этот баланс — фактами, а не прозой.

    «Не ноль» и «хватит» — разные вещи, и разница стоит оплаченного заказа:
    при 1.43 $ на счету и самом дешёвом номинале в 2.74 $ экран молчал бы,
    а первая же выдача отвалилась бы с «не хватает средств».
    """
    rows = [r for r in denominations(catalog, gift, region) if r["in_stock"] > 0]
    out = {"cheapest": 0.0, "cheapest_name": "", "cheapest_region": "",
           "count": 0, "total": 0}
    if not rows:
        return out
    cheap = min(rows, key=lambda r: r["price"])
    out["cheapest"] = float(cheap["price"])
    out["cheapest_name"] = cheap["name"]
    out["cheapest_region"] = cheap["region"]
    money = float(balance_usd or 0)
    if out["cheapest"] > 0:
        out["count"] = int(money // out["cheapest"])
    out["total"] = sum(1 for r in rows if r["price"] <= money)
    return out


# ---------------------------------------------------------------------------
# Настройки карты
# ---------------------------------------------------------------------------

# Умолчания настроек одной карты. **Не объявляются в `_DEFAULT_SETTINGS`**,
# и это не лень: `_merge_defaults` в `storage.py` сливает раздел `plugins`
# на один уровень (`result["plugins"][pkey].update(pval)`), поэтому
# сохранённый `gift_cards` целиком заменил бы умолчания — и продавец,
# включивший карту вчера, не увидел бы умолчаний карты, добавленной сегодня.
# Ровно так 20.08 уже ловили `KeyError` у продавца со старыми настройками.
DEFAULT_CARD_CONF: dict = {
    "enabled": False,
    # Автоответ покупателю — уходит в чат заказа сразу, как заказ взят в
    # работу, ДО кода. Пустой означает «молчим»: слать «принял заказ» и
    # следом код через секунду — шум, а вот когда поставщик отвечает
    # «принято, код будет позже», ожидание бывает минутами, и тогда
    # молчание выглядит как поломка.
    "greeting": "",
    "keyword": "",
    "note": "",
    "region": "",
    "ad_title": "",
    "ad_text": "",
    "delivered": [],
    "log": [],
    "force": [],
}


def _take_over_robux(plugins: dict, holder: dict) -> None:
    """Забрать настройки старого раздела AutoRoblox под карту `robux`.

    Robux переехал на общий движок, и у продавца всё нажитое лежит в
    `plugins["auto_roblox"]`. Самое важное там — **`delivered`**: это
    список заказов, по которым код уже куплен и отправлен. Потерять его
    значит выдать их заново, то есть купить второй код за деньги продавца
    по каждому. `log` и `force` теряются дешевле, но и они переносятся:
    в журнале лежат коды, не ушедшие в закрытый чат.

    Старый раздел не удаляется. Он остаётся как есть — на случай, если
    переезд надо будет разобрать: стереть чужие настройки, не спросив,
    здесь не принято.

    Вызывается лениво и ровно один раз: признак — что карты `robux` в
    новом хранилище ещё нет.
    """
    old = plugins.get("auto_roblox")
    if not isinstance(old, dict):
        return
    fresh = copy.deepcopy(DEFAULT_CARD_CONF)
    for key in fresh:
        if key in old:
            fresh[key] = copy.deepcopy(old[key])
    holder[ROBUX.slug] = fresh


# Слаг снятой карты. Само имя пережило её нарочно: в чужих настройках под
# ним осталось нажитое, и разобрать его надо, а не забыть.
RETIRED_ROBLOX_CARD = "roblox_card"


def _take_over_roblox_card(holder: dict) -> None:
    """Забрать под `robux` то, что осталось от снятой карты `roblox_card`.

    Карту сняли 21.08 по решению продавца. Настройки её при этом остались в
    хранилище — и остались бы **недостижимыми**: экрана нет, значит нет и
    журнала. А в журнале лежат коды, которые не ушли в закрытый чат, и
    список `delivered` — заказы, по которым код уже куплен.

    Переносится ровно два списка:

    * `delivered` — чтобы выданное не выдалось второй раз. Ошибка в эту
      сторону стоит денег продавца по каждому заказу.
    * `log` — журнал: в нём коды, за которые уже заплачено.

    **`force` не переносится намеренно.** Это очередь ручной выдачи, и
    заказ в ней — на *денежную* карту. Перенести его сюда значит купить по
    нему код на Robux: не тот товар за настоящие деньги. Пусть лучше не
    выдастся ничего — это чинится, а покупка не того чинится возвратом.

    Старый ключ не стирается: убирать чужие настройки, не спросив, здесь не
    принято.
    """
    old = holder.get(RETIRED_ROBLOX_CARD)
    if not isinstance(old, dict):
        return
    fresh = holder.setdefault(ROBUX.slug, {})
    for key in ("delivered", "log"):
        moved = old.get(key)
        if not isinstance(moved, list) or not moved:
            continue
        mine = fresh.setdefault(key, [])
        if not isinstance(mine, list):              # pragma: no cover
            continue
        known = {_entry_order(e) for e in mine}
        for entry in moved:
            if _entry_order(entry) not in known:
                mine.append(copy.deepcopy(entry))


def _entry_order(entry) -> str:
    """Номер заказа из записи журнала или из списка выданных.

    В `delivered` лежат номера строками, в `log` — записи словарями. Одна
    мерка на оба списка нужна затем, чтобы перенос не задваивал записи.
    """
    if isinstance(entry, dict):
        return str(entry.get("order") or "")
    return str(entry or "")


def card_conf(settings: dict, slug: str) -> dict:
    """Настройки одной карты, заводятся лениво.

    Изменяемые умолчания копируются: общий список на все карты означал бы,
    что журнал Apple пополняется выдачами Xbox.
    """
    plugins = settings.setdefault("plugins", {})
    holder = plugins.setdefault("gift_cards", {})
    if not isinstance(holder, dict):                # pragma: no cover
        holder = {}
        plugins["gift_cards"] = holder
    if str(slug) == ROBUX.slug and ROBUX.slug not in holder:
        _take_over_robux(plugins, holder)
    if str(slug) == ROBUX.slug:
        _take_over_roblox_card(holder)
    conf = holder.setdefault(str(slug), {})
    for key, value in DEFAULT_CARD_CONF.items():
        if key not in conf:
            conf[key] = copy.deepcopy(value)
    return conf


def enabled_cards(settings: dict) -> list[GiftCard]:
    """Карты, включённые продавцом. Порядок — как в реестре."""
    holder = (settings or {}).get("plugins", {}).get("gift_cards", {})
    holder = holder if isinstance(holder, dict) else {}
    return [c for c in CARDS
            if (holder.get(c.slug) or {}).get("enabled")]
