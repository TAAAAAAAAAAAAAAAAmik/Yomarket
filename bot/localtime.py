"""Время глазами продавца, а не сервера.

Контейнер живёт по UTC, продавец — нет. Пока это не учитывалось, каждая дата
в боте была смещена на его часовой пояс: время писем в чатах, отметки в
журнале, сроки. Хуже того, смещались и решения — ночной режим «22:00—09:00»
применялся к часам сервера, то есть у продавца из UTC+5 работал с 03:00 до
14:00. Сам маркетплейс пишет сроки «по МСК», поэтому по умолчанию берём
Москву: это ближе к правде, чем UTC, для всех, кому бот продаётся.

Фиксированное смещение, а не именованная зона: в образе python-slim нет
tzdata, а в России и большей части СНГ перевода часов нет. Где он есть,
продавец поправит смещение сам — это честнее, чем молча ошибаться на час.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Москва: маркетплейс сам объявляет сроки «по МСК».
DEFAULT_OFFSET_MIN = 180

# Крайние настоящие зоны: от UTC−12 до UTC+14.
MIN_OFFSET_MIN = -12 * 60
MAX_OFFSET_MIN = 14 * 60


def offset_minutes(settings: dict | None) -> int:
    """Смещение продавца в минутах от UTC."""
    raw = (settings or {}).get("tz_offset_min", DEFAULT_OFFSET_MIN)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_OFFSET_MIN
    return max(MIN_OFFSET_MIN, min(MAX_OFFSET_MIN, value))


def offset_label(settings: dict | None) -> str:
    """«UTC+3 (МСК)» — то, что видит продавец в настройках."""
    total = offset_minutes(settings)
    sign = "+" if total >= 0 else "−"
    hours, minutes = divmod(abs(total), 60)
    body = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return body + (" (МСК)" if total == DEFAULT_OFFSET_MIN else "")


def now(settings: dict | None) -> datetime:
    """Текущее время продавца, наивное — для strftime и сравнений часов."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        minutes=offset_minutes(settings))


def to_local(ts: float, settings: dict | None) -> datetime:
    """Момент эпохи → время продавца."""
    return datetime.fromtimestamp(float(ts or 0), tz=timezone.utc).replace(
        tzinfo=None) + timedelta(minutes=offset_minutes(settings))


def fmt(ts: float, settings: dict | None, pattern: str = "%d.%m %H:%M") -> str:
    """Момент эпохи → строка во времени продавца. Пустой ts → «»."""
    if not ts:
        return ""
    try:
        return to_local(ts, settings).strftime(pattern)
    except (ValueError, OverflowError, OSError):
        return ""


def today_str(settings: dict | None) -> str:
    """Сегодняшняя дата продавца, «2026-08-09».

    По ней считаются дневные лимиты и «уже отчитывались сегодня». По часам
    сервера сутки у продавца из UTC+5 кончались в пять утра.
    """
    return now(settings).strftime("%Y-%m-%d")


def hour(settings: dict | None) -> int:
    """Который сейчас час у продавца — для расписаний и ночного режима."""
    return now(settings).hour
