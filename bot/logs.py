"""Журнал событий в группу владельца — по темам форума.

Владелец завёл группу с темами: новые пользователи, подключения магазинов,
пробные периоды, просьбы о счёте, оплаты. Бот пишет туда сам, каждое
событие в свою тему.

Три правила, из-за которых этот модуль устроен именно так:

1. **Журнал не мешает работе.** Ни одна ошибка отправки не должна ронять
   то, ради чего продавец нажал кнопку: подключение магазина важнее записи
   о подключении. Поэтому наружу отсюда не летит ничего.

2. **Молчание объясняется.** Не настроенная группа, отобранные права,
   удалённая тема — всё это выглядит одинаково: «логов нет». Причина
   записывается и показывается в `/log_here`, а владельцу уходит одно
   сообщение — одно, а не на каждое событие.

3. **Секреты сюда не попадают.** В журнал идут только те поля, которые
   собраны на месте вызова. Токенов, кук и seed-фраз среди них нет и быть
   не может: у функций этого модуля нет доступа к хранилищу продавца.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Виды событий и темы, под которые они заведены. Ключ — то, что пишется в
# настройках; заголовок — то, что видно в группе.
KINDS: tuple[tuple[str, str, str], ...] = (
    ("users",   "🔔 Новые пользователи",
     "первый заход в бота"),
    ("account", "🧾 Добавление аккаунта",
     "продавец подключил магазин"),
    ("trial",   "🎁 Активация пробного периода",
     "выдан бесплатный пробный период"),
    ("order",   "❗ Ордер на оплату",
     "продавец просит выставить счёт"),
    ("payment", "💲 Оплата подписки",
     "продавец сообщил об оплате, владелец выдал подписку"),
)

KIND_TITLES = {k: t for k, t, _ in KINDS}

# Об одной и той же беде с журналом владельцу пишем не чаще раза в час:
# сломанный журнал не должен превращаться в рассылку.
_COMPLAINED: dict[str, float] = {}
_COMPLAIN_EVERY = 3600.0


def _person(user) -> str:
    """Как назвать человека в записи: имя, @ник и номер.

    Принимает и объект пользователя, и просто номер: в половине мест, где
    случается событие, объекта уже нет — есть только `uid`, по которому
    работает вся остальная логика. Номер обязателен в любом случае: имена
    меняются и повторяются, а выдавать подписку владельцу придётся именно
    по номеру.
    """
    import ui
    if user is None:
        return "неизвестно кто"
    if isinstance(user, int):
        return f"<code>{user}</code>"
    name = ui.esc(getattr(user, "full_name", "") or "без имени")
    nick = getattr(user, "username", "") or ""
    at = f" @{ui.esc(nick)}" if nick else ""
    return f"{name}{at} · <code>{getattr(user, 'id', '?')}</code>"


async def log_event(bot, kind: str, lines: list[str], user=None) -> bool:
    """Записать событие в свою тему. Отвечает, дошло ли.

    Ответ нужен не вызывающему — ему всё равно, — а `/log_here`, который
    проверяет журнал на живой отправке, а не по наличию настроек.
    """
    from storage import get_log_target, note_log_error

    target = get_log_target()
    chat = target.get("chat")
    if not chat:
        return False                       # группа не задана — это не беда

    head = KIND_TITLES.get(kind, kind)
    body = [f"<b>{head}</b>"]
    if user is not None:
        body.append(_person(user))
    body += [str(x) for x in lines]
    import localtime as _lt
    body.append(f"<i>{_lt.fmt(time.time(), {}, '%d.%m %H:%M')}</i>")

    kwargs = {"disable_web_page_preview": True}
    thread = (target.get("topics") or {}).get(kind)
    if thread:
        kwargs["message_thread_id"] = int(thread)
    try:
        await bot.send_message(chat, "\n".join(body), **kwargs)
        note_log_error("")
        return True
    except Exception as e:                                  # noqa: BLE001
        # Тема удалена, бота выкинули из группы, права отобрали — снаружи
        # всё это одинаково, поэтому причину записываем дословно.
        why = str(e)[:200]
        note_log_error(f"{kind}: {why}")
        logger.warning("журнал (%s) не записался: %s", kind, why)
        await _tell_owner(bot, kind, why)
        return False


async def _tell_owner(bot, kind: str, why: str) -> None:
    """Сказать владельцу — один раз в час на вид события.

    Сломанный журнал сам о себе сообщить не может: он и есть то, что
    сломалось. Поэтому сообщение уходит в личку владельцу.
    """
    from storage import OWNER_ID
    now = time.time()
    if now - _COMPLAINED.get(kind, 0.0) < _COMPLAIN_EVERY:
        return
    _COMPLAINED[kind] = now
    try:
        import ui
        await bot.send_message(OWNER_ID, ui.screen(
            "📋 <b>Журнал не пишется</b>",
            [f"Событие «{KIND_TITLES.get(kind, kind)}» в группу не ушло.",
             "",
             f"Причина: <code>{ui.esc(why)}</code>"],
            footer="<i>Проверить и перепривязать: <code>/log_here</code> "
                   "в нужной теме группы.</i>"))
    except Exception:                                       # noqa: BLE001
        pass                                # владелец недостижим — не наша беда
