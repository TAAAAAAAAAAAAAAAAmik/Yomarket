"""Пробный период за подписку на канал.

Неделя доступа даётся один раз и в обмен на подписку. Проверка одна —
`getChatMember` у Telegram, — но ответов у неё три, и путать их дорого:

* **подписан** — выдаём;
* **не подписан** — отказываем и говорим, что делать;
* **проверить не вышло** — это НЕ отказ.

Третий случай и есть главное здесь. Бот не админ канала, канал переехал,
владелец вписал не тот адрес — всё это ошибки на НАШЕЙ стороне, а выглядят
они как «ты не подписан». Продавец, которого отфутболили за чужую поломку,
второй раз не придёт, и владелец об этом не узнает.

Поэтому при неудавшейся проверке пробный период ВЫДАЁТСЯ, а владельцу
уходит жалоба с причиной. Одна невыданная подписка на канал дешевле
потерянного клиента; невозможность проверить — наша беда, а не его.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Состояния, при которых человек считается подписанным. `restricted` — это
# подписчик с ограничениями (например, в режиме «только чтение»): он в
# канале, значит условие выполнено.
_IN = ("member", "administrator", "creator")


async def check_member(bot, channel: str, user_id: int) -> tuple[bool, str]:
    """Подписан ли. Второе значение — причина, когда проверить не вышло.

    Пустая причина при `False` означает честное «не подписан». Непустая —
    что ответа мы не получили вовсе, и трактовать это как отказ нельзя.
    """
    if not channel:
        return True, ""                     # условия нет — и проверять нечего
    try:
        member = await bot.get_chat_member(channel, user_id)
    except Exception as e:                                  # noqa: BLE001
        why = str(e)[:200]
        logger.warning("проверка подписки на %s не вышла: %s", channel, why)
        return False, why
    status = str(getattr(member, "status", "") or "")
    if status in _IN:
        return True, ""
    if status == "restricted":
        # Ограниченный участник — всё ещё участник. Отдельным полем Telegram
        # говорит, в канале ли он: у покинувшего оно False.
        return bool(getattr(member, "is_member", False)), ""
    return False, ""


async def grant_for_subscription(bot, user_id: int) -> tuple[int, str]:
    """Проверить подписку и выдать неделю. → (дней, причина отказа/сбоя).

    Дней 0 и пустая причина — «не подписан». Дней больше нуля с непустой
    причиной — выдали, хотя проверить не смогли: это и надо показать
    владельцу.
    """
    from storage import get_trial_channel, start_trial

    channel = get_trial_channel()
    ok, why = await check_member(bot, channel, user_id)
    if not ok and not why:
        return 0, ""                        # честно не подписан
    # Вид «channel»: у него своя отметка. Общая с короткой пробой означала
    # бы «взял три дня — семь уже не дадут», а они как раз складываются.
    days = start_trial(user_id, kind="channel")
    return days, why
