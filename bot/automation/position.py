"""Deciding what to do about a listing's place in the offers list.

Kept apart from the handlers and the scheduler on purpose: this is the part
that spends money, and it is the part worth testing. Everything here is a pure
function over a watch dict plus the offers read off the storefront — no network,
no Telegram, no settings I/O — so the rules can be exercised directly
(`bot/tests/test_position.py`).

A "watch" is one listing being followed on one storefront page:

    {"url":…, "title":…,                       # what is being watched
     "market_id":…,                            # its id on the storefront
     "item_id":…,                              # its id in the seller panel
     "max_position": 3, "undercut_guard": 0,   # thresholds
     "min_price": 0,
     "last_pos":…, "last_alert_pos":…,         # state
     "last_check":…, "last_promo":…,
     "misses":…, "fails":…, "promo_day":…, "promos_today":…}

The two ids are deliberately separate and must not be conflated. `market_id`
comes off the public page and is what recognises our row among the offers;
`item_id` is the panel's own record id and is what the paid «Премиум» action
addresses. They are different numbers for the same listing — promoting by the
storefront id answers 404, which is the mistake auto-restore already made once.

Three thresholds, deliberately separate, because they answer different
questions:

  • position   — when to act at all (we slipped below the place worth holding);
  • undercut   — when NOT to pay (someone is cheaper by more than this, so a
                 paid position buys a click that goes to them anyway);
  • min_price  — when to shout (the market price fell below what makes sense).
"""
from __future__ import annotations

import time as _time

DEFAULT_MAX_POSITION = 3
DEFAULT_INTERVAL_HOURS = 1.0
# Каждое поднятие платное. Между съезжающим товаром и пустой картой стоят
# две защиты: пауза между поднятиями и счёт за сутки.
DEFAULT_COOLDOWN_HOURS = 6.0
DEFAULT_DAILY_LIMIT = 3
# Предел был 10, когда интервал был один на всех: чаще проверять — значит
# чаще обходить витрину за каждым наблюдением. Теперь интервал у товара свой,
# и спокойные позиции можно проверять раз в несколько часов, поэтому список
# может быть длиннее.
MAX_WATCHES = 30
# Сколько молчаливых сбоев подряд до того, как сказать продавцу, что
# слежение перестало работать. Один — это шум (страница икнула), три
# подряд — это сломанное слежение.
FAILS_BEFORE_ALARM = 3


def new_watch(url: str, *, title: str = "", item_id: str = "",
              market_id: str = "", category_id=None,
              max_position: int = DEFAULT_MAX_POSITION,
              min_price: float = 0, undercut_guard: float = 0,
              interval_hours: float | None = None,
              auto_promote: bool | None = None) -> dict:
    return {
        "url": str(url or "").strip(),
        "item_id": str(item_id or ""),          # panel record — what gets paid
        "market_id": str(market_id or ""),      # storefront row — what gets found
        # Номер раздела, если каталог назвал его прямо. Он ценнее адреса: на
        # /categories/<игра>/<раздел> это API отвечает ИГРОЙ, поэтому раздел,
        # выведенный из адреса, тихо превращается во всю игру — 638 предложений
        # вместо 161, и позиция считается в
        # wrong one.
        "category_id": category_id,
        "title": str(title or ""),
        "max_position": int(max_position),
        "min_price": float(min_price or 0),
        "undercut_guard": float(undercut_guard or 0),
        # None/пусто = как у магазина. Не 0 и не False: «как у магазина» и
        # «выключено этому товару» — разные вещи, и путать их нельзя.
        "interval_hours": interval_hours,
        "auto_promote": auto_promote,
        "last_pos": 0,
        "last_alert_pos": 0,
        "last_check": 0.0,
        "last_promo": 0.0,
        "misses": 0,
        "fails": 0,
        "alarmed": False,
        "promo_day": "",
        "promos_today": 0,
    }


def watches(pp: dict) -> list[dict]:
    """Список слежений — с переносом настроек той версии, из которой он вырос.

    Первая версия следила за одной страницей на весь магазин. Её настройки
    превращаются в первое слежение, а не выбрасываются: продавец, который
    всё настроил, не должен после обновления увидеть пустой экран.
    """
    got = pp.get("watches")
    if isinstance(got, list) and got:
        return got
    legacy_url = str(pp.get("url") or "").strip()
    if not legacy_url:
        return []
    w = new_watch(
        legacy_url,
        max_position=int(pp.get("max_position") or DEFAULT_MAX_POSITION),
        min_price=float(pp.get("min_price") or 0),
    )
    w["last_pos"] = int(pp.get("last_pos") or 0)
    w["last_alert_pos"] = int(pp.get("last_alert_pos") or 0)
    w["last_check"] = float(pp.get("last_check") or 0)
    pp["watches"] = [w]
    return pp["watches"]


def interval_hours(pp: dict) -> float:
    try:
        return max(MIN_INTERVAL_HOURS,
                   float(pp.get("interval_hours") or DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def cooldown_hours(pp: dict) -> float:
    try:
        return max(0.0, float(pp.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_HOURS


def daily_limit(pp: dict) -> int:
    try:
        return max(0, int(pp.get("daily_limit", DEFAULT_DAILY_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT


# Реже, чем крутится фоновый цикл, проверять нельзя: значение меньше этого
# ничего не ускоряет — проверка всё равно случится на следующем тике, а
# продавец видел бы «каждые 15 минут» и получал полчаса.
MIN_INTERVAL_HOURS = 0.5


def watch_interval_hours(watch: dict, pp: dict) -> float:
    """Как часто проверять именно этот товар.

    Один интервал на все наблюдения заставлял выбирать вслепую: 15 минут ради
    одного горячего товара — это те же 15 минут для всех десяти и вчетверо
    больше обходов витрины; три часа ради экономии — горячий товар висит внизу
    до трёх часов. Пусто у наблюдения = как у магазина.
    """
    own = watch.get("interval_hours")
    if own in (None, "", 0):
        return interval_hours(pp)
    try:
        return max(MIN_INTERVAL_HOURS, float(own))
    except (TypeError, ValueError):
        return interval_hours(pp)


def watch_auto_promote(watch: dict, pp: dict) -> bool:
    """Платить ли за этот товар. None у наблюдения = как у магазина.

    Один переключатель на магазин ставил перед выбором из крайностей: либо бот
    платит за любой просевший товар, либо не платит никогда. А окупается
    поднятие не у всех — у товара с тонкой маржой 19 ₽ съедают продажу.
    """
    own = watch.get("auto_promote")
    if own is None:
        return bool(pp.get("auto_promote"))
    return bool(own)


def is_due(watch: dict, pp: dict, now: float | None = None) -> bool:
    now = _time.time() if now is None else now
    last = float(watch.get("last_check") or 0)
    return (now - last) / 3600.0 >= watch_interval_hours(watch, pp)


def _day(now: float, pp: dict | None = None) -> str:
    """Какие это сутки — по часам продавца, а не сервера.

    Дневной бюджет на продвижение обнулялся в полночь по UTC, то есть
    посреди дня продавца. Смещение кладёт сюда `_check_position` на каждом
    проходе: настройки целиком в эти функции не доходят, а сутки считать
    надо здесь.
    """
    import localtime as _lt
    return _lt.to_local(now, {"tz_offset_min": (pp or {}).get(
        "_tz_min", _lt.DEFAULT_OFFSET_MIN)}).strftime("%Y-%m-%d")


def daily_budget(pp: dict) -> float:
    """Roubles a day this trigger may spend across every watched listing.

    Counted for the whole feature rather than per listing: what a seller wants
    to cap is the money leaving the card, and «три поднятия на товар» says
    nothing about that when there are ten watches. 0 = no cap.
    """
    try:
        return max(0.0, float(pp.get("daily_budget", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def spent_today(pp: dict, now: float) -> float:
    """Сколько это слежение уже потратило сегодня, в рублях."""
    if pp.get("spent_day") != _day(now, pp):
        return 0.0
    try:
        return float(pp.get("spent_today") or 0)
    except (TypeError, ValueError):
        return 0.0


def promos_left(watch: dict, pp: dict, now: float) -> int:
    """Сколько платных поднятий слежению осталось на сегодня. -1 — без счёта."""
    limit = daily_limit(pp)
    if not limit:
        return -1
    used = int(watch.get("promos_today") or 0) if watch.get("promo_day") == _day(now, pp) else 0
    return max(0, limit - used)


def budget_left(pp: dict, now: float) -> float:
    """Сколько рублей ещё можно потратить сегодня. -1 — потолок не задан."""
    budget = daily_budget(pp)
    if not budget:
        return -1.0
    return max(0.0, budget - spent_today(pp, now))


# Сколько поднятий помним для отчёта. Это настройки, а не база данных: расти
# без предела журналу нельзя.
PROMO_LOG_LIMIT = 200


def note_promotion(watch: dict, now: float, price: float = 0,
                   pp: dict | None = None) -> None:
    """Записать поднятие, которое действительно случилось.

    Записывается только прошедшее: отказ — это не поднятие, и зачесть его
    значит потратить дневной бюджет впустую.
    """
    today = _day(now, pp)
    if watch.get("promo_day") != today:
        watch["promo_day"] = today
        watch["promos_today"] = 0
    watch["promos_today"] = int(watch.get("promos_today") or 0) + 1
    watch["last_promo"] = now
    if pp is not None and price:
        if pp.get("spent_day") != today:
            pp["spent_day"] = today
            pp["spent_today"] = 0
        pp["spent_today"] = float(pp.get("spent_today") or 0) + float(price)
    if pp is not None:
        # Позиция «до» — чтобы потом было видно, помогло ли поднятие. Без неё
        # отчёт умеет сказать только «потрачено столько-то», а вопрос у
        # продавца другой: не зря ли.
        log = pp.setdefault("promo_log", [])
        log.append({"ts": now, "title": str(watch.get("title") or "")[:60],
                    "item_id": str(watch.get("item_id") or ""),
                    "price": float(price or 0),
                    "pos_before": int(watch.get("last_pos") or 0)})
        if len(log) > PROMO_LOG_LIMIT:
            del log[:len(log) - PROMO_LOG_LIMIT]


def note_position_after(pp: dict, item_id: str, pos: int, now: float,
                        within_hours: float = 6.0) -> None:
    """Дописать в журнал, какой стала позиция после недавнего поднятия.

    Записывается один раз и только по свежей записи: смысл в паре «было —
    стало», а не в постоянном переписывании последнего значения.
    """
    if not pos:
        return
    for entry in reversed(pp.get("promo_log") or []):
        if str(entry.get("item_id")) != str(item_id):
            continue
        if entry.get("pos_after") is not None:
            return
        if (now - float(entry.get("ts") or 0)) / 3600.0 > within_hours:
            return
        entry["pos_after"] = int(pos)
        return


class Verdict:
    """Что означает одно чтение одной страницы.

    `lines` — что сказать продавцу (пусто — говорить нечего), `promote` —
    тратить ли деньги, `reason` — почему нет, когда `promote` ложно. Проход,
    решивший ничего не делать, обязан уметь объяснить это на экране
    диагностики: «ничего не произошло» без причины и есть та беда, из-за
    которой продавец считает бота сломанным.
    """

    __slots__ = ("found", "pos", "price", "cheapest", "cheapest_above",
                 "lines", "promote", "reason", "slipped", "cost")

    def __init__(self) -> None:
        self.found = False
        self.pos = 0
        self.price: float | None = None
        self.cheapest: float | None = None
        # Сколько стоят предложения, стоящие выше нас: только их и надо
        # обогнать платной позицией.
        self.cheapest_above: float | None = None
        self.cost = 0
        self.lines: list[str] = []
        self.promote = False
        self.reason = ""
        self.slipped = False

    def __repr__(self) -> str:      # tests read a lot better with this
        return (f"<Verdict found={self.found} pos={self.pos} "
                f"slipped={self.slipped} promote={self.promote} "
                f"reason={self.reason!r} lines={self.lines!r}>")


def evaluate(watch: dict, offers: list[dict], *, shop: str = "",
             pp: dict | None = None, now: float | None = None,
             auto_promote: bool | None = None, price: int = 0) -> Verdict:
    """Прочитать позиции на одной странице и решить. Правит состояние слежения.

    Молчит намеренно: проход идёт каждый час, а тревога, повторяемая раз в
    час, — это тревога, которую перестают читать. Про падение говорим один
    раз и повторяем, только если стало хуже; про возвращение — один раз.
    """
    from automation.market import cheapest as _cheapest, find_position

    pp = pp if pp is not None else {}
    now = _time.time() if now is None else now
    if auto_promote is None:
        auto_promote = watch_auto_promote(watch, pp)

    v = Verdict()
    watch["last_check"] = now

    # market_id, а не item_id: строка ищется на витрине, и номер записи из
    # панели там не значит ничего.
    mine = find_position(offers,
                         ad_id=str(watch.get("market_id") or ""),
                         title=str(watch.get("title") or ""),
                         seller=shop)
    if not mine:
        # Не найдя себя, мы не узнали о позиции ничего — значит ничего не
        # поднимаем и ничего не утверждаем. Но слежение, которое ни разу не
        # находит свой товар, сломано, и молчание это скроет.
        watch["misses"] = int(watch.get("misses") or 0) + 1
        v.reason = "не нашёл товар на странице"
        if watch["misses"] >= FAILS_BEFORE_ALARM and not watch.get("alarmed"):
            watch["alarmed"] = True
            v.lines.append(
                f"❔ Не нахожу этот товар в списке уже {watch['misses']} проверок "
                f"подряд — проверьте адрес страницы или название.")
        return v

    if watch.get("misses") or watch.get("alarmed"):
        if watch.get("alarmed"):
            v.lines.append("✅ Товар снова найден на странице")
        watch["misses"] = 0
        watch["alarmed"] = False

    v.found = True
    v.pos = int(mine["pos"])
    v.price = None if mine.get("price") is None else float(mine["price"])
    prev = int(watch.get("last_pos") or 0)
    watch["last_pos"] = v.pos

    threshold = int(watch.get("max_position") or DEFAULT_MAX_POSITION)
    v.slipped = v.pos > threshold
    alerted_at = int(watch.get("last_alert_pos") or 0)
    if v.slipped and (alerted_at == 0 or v.pos > alerted_at):
        watch["last_alert_pos"] = v.pos
        v.lines.append(f"📉 <b>{v.pos}-е место</b>"
                       + (f" (было {prev})" if prev and prev != v.pos else "")
                       + f" — ниже порога {threshold}")
    elif not v.slipped and alerted_at:
        watch["last_alert_pos"] = 0
        v.lines.append(f"📈 Вернулись на <b>{v.pos}-е место</b> — порог соблюдён")

    others = [o for o in offers if o.get("pos") != v.pos]
    v.cheapest = _cheapest(others if others else offers)
    # Стоит ли платить за позицию, решают только предложения ВЫШЕ нас: их
    # покупатель видит первыми. На живом товаре далеко внизу лежал лот за
    # 1 ₽ — цена, с которой никто не соревнуется, и приняв её за «конкурента»,
    # мы заблокировали бы поднятия навсегда.
    v.cheapest_above = _cheapest([o for o in offers
                                  if int(o.get("pos") or 0) < v.pos])

    if pp.get("undercut_notify", True) and v.cheapest is not None and v.price:
        if v.cheapest < v.price:
            line = (f"💰 Дешевле у конкурента: <b>{v.cheapest:.0f} ₽</b> "
                    f"против ваших {v.price:.0f} ₽")
            if (v.cheapest_above is not None
                    and v.cheapest_above != v.cheapest):
                line += f"; выше вас — от {v.cheapest_above:.0f} ₽"
            v.lines.append(line)
    floor = float(watch.get("min_price") or 0)
    if floor and v.cheapest is not None and v.cheapest < floor:
        v.lines.append(f"⚠️ Цена на витрине упала ниже вашего порога "
                       f"{floor:.0f} ₽ — сейчас {v.cheapest:.0f} ₽")

    # --- should we pay? ------------------------------------------------------
    if not v.slipped:
        v.reason = "позиция в пределах порога"
        return v
    if not auto_promote:
        v.reason = "поднятие вручную (авто выключено)"
        return v

    guard = float(watch.get("undercut_guard") or 0)
    rival = v.cheapest_above if v.cheapest_above is not None else v.cheapest
    if guard and rival is not None and v.price is not None \
            and (v.price - rival) > guard:
        # Платить за верх, когда выше стоит кто-то заметно дешевле, — значит
        # купить просмотр, который сработает не нам. Меряем только по стоящим
        # выше: дешёвый лот ниже по списку за эту позицию не борется, и считать
        # его конкурентом значит заблокировать поднятия навсегда.
        v.reason = (f"выше вас есть дешевле на {v.price - rival:.0f} ₽ "
                    f"(порог {guard:.0f}) — поднятие не окупится")
        v.lines.append(f"⛔ Не поднимаю: {v.reason}")
        return v

    cool = cooldown_hours(pp)
    since = (now - float(watch.get("last_promo") or 0)) / 3600.0
    if cool and float(watch.get("last_promo") or 0) and since < cool:
        v.reason = f"поднимал {since:.1f} ч назад, пауза {cool:.0f} ч"
        return v

    left = promos_left(watch, pp, now)
    if left == 0:
        v.reason = f"дневной лимит поднятий исчерпан ({daily_limit(pp)})"
        v.lines.append(f"⛔ Не поднимаю: {v.reason}")
        return v

    budget = daily_budget(pp)
    if budget:
        if not price:
            # Цена поднятия берётся из выбранного тарифа. Без неё проверка
            # бюджета молча пропускалась: на экране стояло «Бюджет: 50 ₽/сут»,
            # бот поднимал сколько влезет по счётчику, а «потрачено» вечно
            # показывало ноль. Предел, который нельзя применить, обязан
            # останавливать, а не притворяться, что он есть.
            v.reason = ("задан дневной бюджет, но цена поднятия неизвестна — "
                        "выберите тариф: Объявления → ⭐ Премиум → ⚙️ Тариф")
            v.lines.append(f"⛔ Не поднимаю: {v.reason}")
            return v
        spent = spent_today(pp, now)
        if spent + price > budget:
            v.reason = (f"дневной бюджет {budget:.0f} ₽ исчерпан "
                        f"(потрачено {spent:.0f} ₽, поднятие стоит {price} ₽)")
            v.lines.append(f"⛔ Не поднимаю: {v.reason}")
            return v

    v.promote = True
    v.cost = int(price or 0)
    v.reason = "ниже порога — поднимаю"
    return v
