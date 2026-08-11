"""Что именно приехало в заказе: товар, покупатель, сумма, статус.

Экран заказов доставал поля по угаданным именам — `title`, `buyer_name`,
`price` на верхнем уровне, — и когда маркетплейс кладёт их иначе, список
превращался в «#1184186 — — 👤 — 💰 — ₽ created»: заказы есть, а данных нет.

Здесь разбор один на весь бот: список заказов, подписи чатов, уведомления и
статистика читают заказ отсюда, поэтому чинится это в одном месте. Поиск идёт
по вложенным объектам, но не вслепую: имя покупателя не должно оказаться
названием товара только потому, что и там и там поле называется `name`.
"""
from __future__ import annotations

import re

from collections import deque

# Ветки, в которых лежит покупатель. Их не трогаем, когда ищем товар и сумму.
_PERSON_KEYS = ("buyer", "user", "customer", "client", "seller", "shop",
                "owner", "author")
# Ветки товара — их не трогаем, когда ищем покупателя.
_ITEM_KEYS = ("ad", "item", "product", "offer", "lot", "goods", "position")


def _clean(value) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("", "none", "null", "—", "-") else text


def _deep(node, keys: tuple[str, ...], skip: tuple[str, ...] = (),
          depth: int = 3):
    """Первое непустое значение под одним из `keys`, обходя дерево вширь.

    Вширь, а не вглубь: поле верхнего уровня должно побеждать одноимённое
    поле, закопанное в подобъект.
    """
    q: deque = deque([(node, 0)])
    while q:
        cur, d = q.popleft()
        if isinstance(cur, dict):
            for k in keys:
                got = _clean(cur.get(k))
                if got:
                    return got
            if d < depth:
                for key, value in cur.items():
                    if str(key).lower() in skip:
                        continue
                    if isinstance(value, (dict, list)):
                        q.append((value, d + 1))
        elif isinstance(cur, list) and d < depth:
            for value in cur[:5]:
                if isinstance(value, (dict, list)):
                    q.append((value, d + 1))
    return ""


def order_id(order: dict) -> str:
    return _clean(order.get("id")) or _clean(_deep(order, ("id", "order_id", "number"), depth=1))


def order_title(order: dict) -> str:
    """Название товара из заказа."""
    return _deep(order,
                 ("title", "ad_title", "product_name", "item_title",
                  "lot_title", "offer_title", "ad_name", "product_title",
                  "name"),
                 skip=_PERSON_KEYS)


def order_buyer(order: dict) -> str:
    """Имя покупателя. Сначала прямые поля, потом ветка покупателя."""
    direct = _deep(order, ("buyer_name", "customer_name", "user_name",
                           "buyer_login", "buyer_username"), depth=1)
    if direct:
        return direct
    for key in ("buyer", "customer", "client", "user"):
        node = order.get(key)
        if isinstance(node, dict):
            got = _deep(node, ("name", "title", "username", "login",
                               "nickname", "display_name", "full_name"),
                        depth=2)
            if got:
                return got
        elif _clean(node):
            return _clean(node)
    return ""


def order_username(order: dict) -> str:
    """@username покупателя, если маркетплейс его отдаёт."""
    raw = _deep(order, ("username", "telegram", "tg", "login", "nickname"),
                skip=_ITEM_KEYS)
    if not raw:
        return ""
    raw = raw.lstrip("@")
    return f"@{raw}" if raw else ""


def order_price(order: dict) -> float | None:
    """Сумма заказа числом, или None — если её в ответе нет.

    Ноль и «нет данных» — разные вещи: «0 ₽» на экране читается как бесплатный
    заказ, а не как «сумму не прислали».
    """
    for key in ("price", "total", "sum", "amount", "cost", "total_price",
                "price_total", "paid", "paid_amount", "summ"):
        node = order.get(key)
        if isinstance(node, dict):                # {"amount": …, "currency": …}
            node = node.get("amount", node.get("value", node.get("sum")))
        got = _to_number(node)
        if got is not None:
            return got
    got = _deep(order, ("price", "total", "sum", "amount", "cost", "value"),
                skip=_PERSON_KEYS)
    return _to_number(got)


def ad_price(ad: dict) -> float | None:
    """Цена объявления числом, или None — если её в ответе нет.

    Маркетплейс отдаёт её объектом: {"amount": 129, "base_amount": 129,
    "currency": "RUB"}. Прочитанная как скаляр, она превращалась в 0 или в
    «—»: экраны показывали «цена не указана», а ночное расписание молча
    пропускало каждый товар — на dict его int(float(str(…))) спотыкался.
    """
    return order_price(ad)


def _to_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    for ch in "   ₽":
        text = text.replace(ch, "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def order_ad_id(order: dict) -> str:
    """Номер объявления, по которому сделан заказ.

    Список заказов у этого маркетплейса скупой: номер, статус и ссылка на
    объявление. Название товара приходится дочитывать по этому номеру.
    """
    for key in ("ad_id", "product_id", "item_id", "listing_id", "offer_id",
                "lot_id", "goods_id"):
        got = _clean(order.get(key))
        if got:
            return got
    for key in _ITEM_KEYS:
        node = order.get(key)
        if isinstance(node, dict):
            got = _clean(node.get("id"))
            if got:
                return got
    return ""


def order_chat_id(order: dict) -> str:
    """Номер чата заказа. Пусто — если в ответе его нет.

    Сообщения лежат под чатом: GET /chats/{chat_id}/messages. У части заказов
    номер чата совпадает с номером заказа, у части — нет, и подстановка номера
    заказа «на всякий случай» даёт resource_not_found. Лучше честно вернуть
    пусто и поискать в карточке заказа, чем стучаться не туда.
    """
    for key in ("chat_id", "dialog_id", "conversation_id", "room_id",
                "chatId", "chat"):
        node = order.get(key)
        if isinstance(node, dict):
            node = node.get("id") or node.get("chat_id")
        got = _clean(node)
        if got:
            return got
    return ""


def ad_title(payload) -> str:
    """Название из ответа /ads/{id} — в конверте или без него."""
    node = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        node = payload["data"]
    if not isinstance(node, dict):
        return ""
    return _deep(node, ("title", "name", "product_name"), skip=_PERSON_KEYS,
                 depth=2)


def order_quantity(order: dict) -> str:
    return _deep(order, ("quantity", "count", "qty", "amount_items", "items_count"),
                 skip=_PERSON_KEYS, depth=1)


def order_created(order: dict):
    return (order.get("created_at") or order.get("date") or order.get("created")
            or order.get("createdAt") or order.get("time"))


def order_status(order: dict) -> str:
    """Сырой статус, как его назвал маркетплейс."""
    node = order.get("status")
    if isinstance(node, dict):
        node = (node.get("code") or node.get("name") or node.get("value")
                or node.get("slug"))
    got = _clean(node)
    return got or _clean(_deep(order, ("status", "state", "stage"), depth=2))


# Статусы этого маркетплейса. «created» и «success» показывались как есть —
# продавцу приходилось догадываться, что success это выполненный заказ.
_STATUS_RU = {
    "new": ("🆕", "Новый"),
    "created": ("🆕", "Создан"),
    "pending": ("⏳", "Ожидает"),
    "paid": ("💳", "Оплачен"),
    "hold": ("⏸", "Заморожен"),
    "work": ("🔧", "В работе"),
    "working": ("🔧", "В работе"),
    "processing": ("🔧", "В работе"),
    "delivered": ("📦", "Выдан"),
    "success": ("✅", "Выполнен"),
    "completed": ("✅", "Выполнен"),
    "complete": ("✅", "Выполнен"),
    "done": ("✅", "Выполнен"),
    "confirmed": ("✅", "Подтверждён"),
    "closed": ("✅", "Закрыт"),
    "refunded": ("↩️", "Возврат"),
    "returned": ("↩️", "Возврат"),
    "refund": ("↩️", "Возврат"),
    "cancelled": ("❌", "Отменён"),
    "canceled": ("❌", "Отменён"),
    "rejected": ("❌", "Отклонён"),
    "failed": ("❌", "Не прошёл"),
    "expired": ("⌛", "Истёк"),
    "dispute": ("⚠️", "Спор"),
    "disputed": ("⚠️", "Спор"),
    "arbitration": ("⚠️", "Арбитраж"),
    "complaint": ("⚠️", "Жалоба"),
}


def status_ru(raw: str) -> str:
    """«success» → «✅ Выполнен». Незнакомое показываем как есть — так видно,
    какой статус ещё не переведён, вместо тихой замены на «—»."""
    key = str(raw or "").strip().lower()
    icon, name = _STATUS_RU.get(key, ("", ""))
    if name:
        return f"{icon} {name}"
    return f"📊 {raw}" if raw else "—"


def status_icon(raw: str) -> str:
    return _STATUS_RU.get(str(raw or "").strip().lower(), ("📊", ""))[0]


DONE = ("success", "completed", "complete", "done", "confirmed", "closed",
        "delivered")

# Деньги за заказ уже у маркетплейса — товар выдавать можно.
PAID = ("paid", "work", "working", "processing", "delivered", "success",
        "completed", "complete", "done", "confirmed", "closed")
# Заказ создан, но не оплачен. Панель на такой прямо предупреждает: «Не
# выдавайте товар — дождитесь оплаты».
UNPAID = ("new", "created", "pending", "hold", "cart", "draft", "unpaid",
          "await", "awaiting", "wait")


def is_paid(status) -> bool:
    """Оплачен ли заказ. Незнакомый статус оплаченным не считается.

    Выдавать товар по догадке нельзя: ошибка в эту сторону — подарок за счёт
    продавца, ошибка в другую — всего лишь задержка до следующего прохода.
    """
    key = str(status or "").strip().lower()
    if not key:
        return False
    if key in UNPAID:
        return False
    return key in PAID
BACK = ("refunded", "returned", "refund", "cancelled", "canceled", "rejected",
        "failed", "expired")


def parse_amount(value):
    """Первое число из того, во что панель его завернула. None — числа нет.

    Две поломки подряд, обе на этой строчке.

    Сначала разбор ронял всё: чистка убирала пробелы и запятые по списку, а
    «1 234,56 ₽» превращалось в «1234.56₽», на котором `float` падает молча —
    и баланс показывался прочерком.

    Потом, наоборот, склеивал: пробелы снимались ДО того, как выделено
    число, и поле с двумя суммами — «5 862,26 ₽ · 338 242 ₽» — читалось как
    586226338242. Прочерк хотя бы честен; выдуманный баланс в полтриллиона
    хуже, чем его отсутствие.

    Поэтому число выделяется целиком, вместе со своими разрядами, и только
    потом чистится. Разряды — это группы ровно по три цифры: «5 862 26» на
    второй группе обрывается, и вторая сумма в первую не втягивается.

    Разделители: есть и точка, и запятая — десятичным считается последний.
    Только запятая и после неё одна-две цифры — тоже десятичная, иначе
    разряды.
    """
    if isinstance(value, dict):
        for key in ("amount", "value", "sum", "balance", "total"):
            if value.get(key) not in (None, ""):
                return parse_amount(value[key])
        return None
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    # Неразрывный и узкий пробелы панель ставит в разрядах — приводим к
    # обычному, чтобы дальше был один случай вместо трёх.
    text = re.sub(r"[\s\u00a0\u202f\u2009]", " ", str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    # Сначала форма с разрядами, потом простая: иначе «5 862» прочитается
    # как «5» и остановится.
    m = re.search(r"[-+]?\d{1,3}(?: \d{3})+(?:[.,]\d{1,2})?"      # 1 234,56
                  r"|[-+]?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"       # 1,234.56
                  r"|[-+]?\d+(?:[.,]\d+)?", text)                 # 1234.56
    if not m:
        return None
    token = m.group(0).replace(" ", "")

    if "." in token and "," in token:
        dec = max(token.rfind("."), token.rfind(","))
        token = re.sub(r"[.,]", "", token[:dec]) + "." + token[dec + 1:]
    elif "," in token:
        tail = token.rsplit(",", 1)[1]
        token = (token.replace(",", ".") if len(tail) in (1, 2)
                 else token.replace(",", ""))
    try:
        return float(token)
    except ValueError:
        return None


def money(value: float | None) -> str:
    """1234.0 → «1 234», 1234.5 → «1 234,5», None → «»."""
    if value is None:
        return ""
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value)):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",").rstrip("0").rstrip(",")


def describe(order: dict) -> dict:
    """Всё сразу — одним разбором на заказ."""
    return {
        "id": order_id(order),
        "title": order_title(order),
        "buyer": order_buyer(order),
        "username": order_username(order),
        "price": order_price(order),
        "quantity": order_quantity(order),
        "status": order_status(order),
        "created": order_created(order),
    }


def shape(node, depth: int = 0, limit: int = 40) -> str:
    """Структура ответа: какие вообще поля пришли.

    Нужна для диагностики: угадывать имена полей — это ровно то, из-за чего
    список заказов оказался пустым.
    """
    pad = "  " * depth
    if isinstance(node, dict):
        out = []
        for key, value in list(node.items())[:limit]:
            if isinstance(value, dict):
                out.append(f"{pad}{key}:\n" + shape(value, depth + 1, limit))
            elif isinstance(value, list):
                inner = (shape(value[0], depth + 1, limit)
                         if value and isinstance(value[0], dict) else "")
                out.append(f"{pad}{key}: [{len(value)}]"
                           + (f"\n{inner}" if inner else ""))
            else:
                text = str(value)
                out.append(f"{pad}{key} = {text[:40]}")
        return "\n".join(out)
    return f"{pad}{type(node).__name__}"
