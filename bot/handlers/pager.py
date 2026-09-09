"""Курсор страницы — в памяти, а в кнопке только его номер.

Telegram даёт под `callback_data` **64 байта**, а курсор этого маркетплейса
— это семьдесят с лишним символов base64:

    page:chat_orders:eyJpZCI6MTIyMzUzNSwiX3BvaW50c1RvTmV4dEl0ZW1zIjp0cnVlfQ

Живой отказ 08.09: «Resulted callback data is too long! … > 64». И падало
не одно нажатие, а весь экран: кнопка собирается внутри той же попытки, что
и список, и исключение доезжало до продавца как «Заказы не загрузились».

Раньше это не всплывало по случайности: курсор читали не из того ключа, он
всегда выходил пустым, и кнопка не рисовалась вовсе. Починив чтение, я
открыл давнюю поломку.

Поэтому курсор здесь и остаётся, а кнопка несёт номер — две-три цифры.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Сколько курсоров помним на продавца и вид списка. Листают вперёд и
# помалу; десяток страниц назад никто не отматывает, а расти без предела
# этому словарю незачем — он живёт в памяти процесса.
_KEEP = 30

_CURSORS: dict[tuple[int, str], list[str]] = {}


def remember(uid: int, entity: str, cursor: str) -> str:
    """Запомнить курсор. → короткий номер для кнопки.

    Один и тот же курсор не запоминается дважды: продавец жмёт «Обновить»,
    и список переехал бы на новый номер при том же самом курсоре.
    """
    if not cursor:
        return ""
    rows = _CURSORS.setdefault((int(uid), str(entity)), [])
    if cursor in rows:
        return str(rows.index(cursor))
    rows.append(str(cursor))
    if len(rows) > _KEEP:
        # Выбрасываем старые, но номера НЕ пересчитываем: кнопка в
        # переписке ссылается на номер, и сдвиг превратил бы её в кнопку на
        # чужую страницу — молча.
        for i, old in enumerate(rows):
            if old:
                rows[i] = ""
                break
    return str(len(rows) - 1)


def recall(uid: int, entity: str, number: str) -> str:
    """Курсор по номеру из кнопки. Пусто — номера нет (бот перезапускался).

    Пустое значение здесь — законный ответ, а не поломка: вызывающий
    показывает «список устарел», а не молча открывает первую страницу под
    видом следующей.
    """
    rows = _CURSORS.get((int(uid), str(entity))) or []
    try:
        idx = int(number)
    except (TypeError, ValueError):
        return ""
    if 0 <= idx < len(rows):
        return rows[idx]
    return ""


def forget(uid: int, entity: str | None = None) -> None:
    """Забыть курсоры продавца — на выходе из списка или в тестах."""
    if entity is None:
        for key in [k for k in _CURSORS if k[0] == int(uid)]:
            _CURSORS.pop(key, None)
    else:
        _CURSORS.pop((int(uid), str(entity)), None)
