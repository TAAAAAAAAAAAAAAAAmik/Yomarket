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
BACK = ("refunded", "returned", "refund", "cancelled", "canceled", "rejected",
        "failed", "expired")


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
