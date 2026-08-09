"""Where the shop's numbers come from.

Statistics used to be assembled purely from `known_orders` — the orders the
poller happened to observe while it was running. Everything sold before the bot
was started, or while the container was down, did not exist as far as the
figures were concerned, so a report could arrive saying the day was empty when
it was not. The panel keeps the shop's real ledger; these helpers read it and
fall back to the local history only when the panel cannot be reached.

Both the «Статистика» screen and the daily report go through here, so the two
can never disagree about what a day earned.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# The ledger is one HTTP round-trip per screen; a seller clicking through the
# statistics sub-screens would otherwise re-read it on every tap.
_TTL = 120
_CACHE: dict[int, tuple[float, list, str, str]] = {}

# «success» — так этот маркетплейс называет выполненный заказ; без него ни одна
# продажа не попадала в выручку.
from orderfields import BACK as _BACK, DONE as _DONE

PANEL = "panel"
LOCAL = "local"


def invalidate(uid: int) -> None:
    _CACHE.pop(uid, None)


async def _panel_events(uid: int) -> tuple[list[dict], str]:
    """The panel's ledger as events, or ([], reason).

    Which resource holds the ledger is discovered once and remembered: probing
    a list of candidate names on every read costs a request each.
    """
    from automation.panel import panel_find_ledger_sync, panel_operations_sync
    from handlers.balance import _run_panel
    from storage import get_settings, save_settings

    res = str(get_settings(uid).get("panel_ledger") or "")
    ops = None
    err = ""
    if res:
        ops, err = await _run_panel(uid, panel_operations_sync, res, 6, 100, 0.0)
    if not isinstance(ops, list):
        found, err2 = await _run_panel(uid, panel_find_ledger_sync)
        if (isinstance(found, (list, tuple)) and len(found) == 2
                and isinstance(found[1], list)):
            res = str(found[0])
            s = get_settings(uid)
            s["panel_ledger"] = res
            save_settings(uid, s)
            ops, err = await _run_panel(uid, panel_operations_sync, res, 6, 100, 0.0)
        else:
            err = err2 or err
    if not isinstance(ops, list):
        return [], (err or "панель не отдала операции")[:200]

    # A row with no date cannot be placed in any window, and counting it into
    # "today" would inflate the day with the shop's whole history.
    dated = [o for o in ops if o.get("ts")]
    if not dated:
        # Reached the panel, read nothing usable — a different thing from not
        # reaching it, and the seller should be told which happened.
        return [], "панель ответила, но операций в журнале нет"
    return dated, ""


def local_events(settings: dict) -> list[dict]:
    """The bot's own order history in the same event shape."""
    known: dict = settings.get("known_orders", {})
    details: dict = settings.get("known_order_details", {})
    out: list[dict] = []
    for oid, det in details.items():
        if not isinstance(det, dict):
            continue
        seen = det.get("seen_at") or 0
        st = str(known.get(oid) or known.get(str(oid)) or "")
        try:
            price = float(str(det.get("price", 0)) or 0)
        except (TypeError, ValueError):
            price = 0.0
        kind = "sale" if st in _DONE else ("refund" if st in _BACK else "order")
        out.append({"id": oid, "ts": float(seen or 0), "amount": price,
                    "title": str(det.get("title") or "—"),
                    "buyer": str(det.get("buyer") or ""),
                    "kind": kind, "status": st,
                    "direction": "in" if kind == "sale" else "?",
                    "type": ""})
    return out


async def events_for(uid: int, settings: dict | None = None,
                     force: bool = False) -> tuple[list[dict], str, str]:
    """(events, source, panel error). `source` is PANEL or LOCAL."""
    now = time.time()
    hit = _CACHE.get(uid)
    if hit and not force and now - hit[0] < _TTL:
        return hit[1], hit[2], hit[3]

    if settings is None:
        from storage import get_settings
        settings = get_settings(uid)

    try:
        events, err = await _panel_events(uid)
    except Exception as e:                      # never let stats crash a report
        logger.warning("panel ledger for %s: %s", uid, e)
        events, err = [], str(e)[:150]

    source = PANEL
    if not events:
        events, source = local_events(settings), LOCAL

    _CACHE[uid] = (now, events, source, err)
    return events, source, err


def summarize(events: list[dict], since: float = 0.0,
              until: float | None = None) -> dict:
    """Totals over a time window. Refunds are not revenue; bumps are spend."""
    res = {"sales": 0, "revenue": 0.0, "orders": 0, "refunds": 0,
           "spend": 0.0, "payouts": 0.0, "payout_count": 0, "other_in": 0.0}
    for e in events:
        ts = float(e.get("ts") or 0)
        if ts < since or (until is not None and ts >= until):
            continue
        kind = e.get("kind")
        amount = float(e.get("amount") or 0)
        if kind == "sale":
            res["sales"] += 1
            res["orders"] += 1
            res["revenue"] += amount
        elif kind == "refund":
            res["refunds"] += 1
            res["orders"] += 1
        elif kind == "order":
            res["orders"] += 1
        elif kind == "bump":
            res["spend"] += amount
        elif kind == "payout":
            res["payouts"] += amount
            res["payout_count"] += 1
        elif kind == "out":
            res["spend"] += amount
        elif kind == "in":
            res["other_in"] += amount
    res["net"] = res["revenue"] - res["spend"]
    return res


def day_start(now: float | None = None,
              settings: dict | None = None) -> float:
    """Midnight local time, as epoch seconds.

    `now - now % 86400` is midnight UTC, which for a Moscow seller cuts the day
    three hours late — sales made between 00:00 and 03:00 were counted into the
    previous day's report.
    """
    from datetime import timedelta, timezone
    import localtime as _lt
    ts = now if now is not None else time.time()
    # Полночь продавца, а не сервера: у продавца из UTC+5 «сегодня» начиналось
    # в пять утра, и утренние продажи попадали во вчерашний отчёт.
    off = timedelta(minutes=_lt.offset_minutes(settings))
    local = _lt.to_local(ts, settings).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (local - off).replace(tzinfo=timezone.utc).timestamp()


def source_note(source: str, err: str = "") -> str:
    if source == PANEL:
        return "<i>Источник: панель продавца</i>"
    if err:
        return f"<i>Источник: история бота — {err}.</i>"
    return ("<i>Источник: история бота. Точные цифры за всё время появятся "
            "после входа в панель продавца.</i>")
