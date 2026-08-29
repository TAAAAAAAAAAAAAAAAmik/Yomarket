import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> str:
    """Выбрать папку данных, которая переживёт пересборку контейнера.

    По порядку:
      1. переменная $DATA_DIR — явное указание, например точка тома Railway;
      2. /app/data — том, объявленный в docker-compose.yml
         (`bot_data:/app/data`); переживает `docker compose up --build`;
      3. ~/.yomarket — запасной вариант для запуска без Docker.

    Прежняя версия выбирала ~/.yomarket даже внутри Docker, а эта папка томом
    НЕ накрыта — данные стирались при каждом выкате.
    """
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    # В образе Docker рабочая папка — /app, и том compose ведёт в /app/data.
    if os.path.isdir("/app"):
        return "/app/data"
    return os.path.join(os.path.expanduser("~"), ".yomarket")


_DATA_DIR = _resolve_data_dir()
# Куда прежние версии бота могли складывать файлы хранилища.
_LEGACY_DIRS = [
    os.path.join(os.path.dirname(__file__), "data"),     # bot/data/
    os.path.join(os.path.expanduser("~"), ".yomarket"),  # previous (broken) default
]


def _migrate_legacy() -> None:
    """Один раз перенести *.json из всех прежних мест в нынешнюю папку данных."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    for legacy in _LEGACY_DIRS:
        if not os.path.isdir(legacy) or os.path.abspath(legacy) == os.path.abspath(_DATA_DIR):
            continue
        for fname in ("tokens.json", "settings.json", "panel_creds.json"):
            src = os.path.join(legacy, fname)
            dst = os.path.join(_DATA_DIR, fname)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                except Exception:
                    pass


_migrate_legacy()

_FILE = os.path.join(_DATA_DIR, "tokens.json")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")
_PANEL_FILE = os.path.join(_DATA_DIR, "panel_creds.json")
# Секретное: куки Fragment и seed-фраза кошелька TON. Ни в логи, ни в
# репозиторий не попадают никогда — это доступ к чужому кошельку.
_FRAGMENT_FILE = os.path.join(_DATA_DIR, "fragment_creds.json")
_NS_FILE = os.path.join(_DATA_DIR, "ns_creds.json")
_AR_FILE = os.path.join(_DATA_DIR, "approute_creds.json")
_ADMIN_FILE = os.path.join(_DATA_DIR, "admin.json")

_DEFAULT_SETTINGS = {
    "shop_name": "",
    "auto_reply": {"enabled": False, "message": "Спасибо за заказ! Скоро свяжемся с вами."},
    "auto_events": {
        "on_confirmed": {"enabled": False, "message": "✅ Ваш заказ подтверждён! Спасибо."},
        "on_refunded": {"enabled": False, "message": "↩️ Возврат оформлен. Ожидайте 1-3 дня."},
    },
    "auto_rules": [],  # [{"keyword": "Roblox", "message": "🎮 Робуксы отправим в течение 15 минут!"}]
    "auto_restore": {
        "enabled": False,
        "interval_hours": 1,     # раньше крутилось каждые 30 минут без паузы
        # Вернуть товар в продажу сразу после покупки, не дожидаясь
        # планового прохода: продажа — это и есть момент, когда товар мог уйти
        # с витрины.
        "instant": True,
        "require_stock": True,   # не публиковать распроданное
        "last_restore_run": 0,
        "restored_total": 0,
        # {ad_id: {"tries": n, "until": ts, "reason": str}} — объявление,
        # которое маркетплейс отверг, не долбится каждый час
        "failures": {},
        "last_result": "",
    },
    "auto_bump": {"enabled": False, "interval_hours": 24},
    # Поднятие по позиции: следим за местом товара в списке предложений на
    # витрине и поднимаем, когда он опустился ниже порога. Позиции нет в API
    # продавца — она есть только на публичной витрине, оттуда и читается.
    "promo_position": {
        "enabled": False,
        # Список наблюдений: по одному на товар. Каждое — {url, item_id, title,
        # max_position, min_price, undercut_guard, ...}; см. automation/position.py.
        # Старые настройки с одной страницей на магазин переносятся сюда
        # автоматически при первом чтении.
        "watches": [],
        "interval_hours": 1,
        # По умолчанию только предупреждаем: поднятие тратит деньги, и решение
        # тратить их автоматически должно быть осознанным.
        "auto_promote": False,
        "undercut_notify": True,   # сообщать, когда кто-то дешевле
        # Предохранители для платного поднятия: пауза между поднятиями одного
        # товара, потолок поднятий в сутки и денежный потолок на всё
        # продвижение по позиции. Без них падение позиции на весь день
        # означало бы оплату на каждой проверке.
        "cooldown_hours": 6,
        "daily_limit": 3,
        "daily_budget": 0,      # ₽ в сутки на все наблюдения; 0 — без потолка
        "spent_today": 0,
        "spent_day": "",
        # Наследие однопозиционной версии — читается только при миграции.
        "url": "",
        "max_position": 3,
        "min_price": 0,
        "last_check": 0,
        "last_pos": 0,
        "last_alert_pos": 0,
    },
    # Реквизиты вывода. Автовывода нет: деньги переводит человек, а не
    # расписание — ключ остался прежним, чтобы уже настроенные реквизиты не
    # пропали у тех, кто их заполнил.
    "auto_withdraw": {
        # Только панель: вывода через Integration API у Юмаркета нет.
        "method": "panel",
        # для вывода через панель: действие «Вывести» на ресурсе balances —
        # id баланса, ключ действия и значения полей (сумма/способ/реквизиты),
        # прочитанные из панели, а не угаданные
        "panel_balance_id": "",
        "panel_action_key": "",
        "panel_values": {},
        "last_result": "",
    },
    "responders": {},  # {"GameName": "message text"} - keyed by ad title/name
    # Ответы на сообщения покупателя. Полный набор полей и значения по
    # умолчанию живут в autoreply.DEFAULTS — здесь ключ заведён, чтобы он был
    # виден среди настроек; autoreply.cfg() дополняет недостающее.
    "autoreplies": {"enabled": False, "rules": [], "log": [], "state": {}},
    "known_orders": {},  # {order_id: status}
    "known_order_ids": [],
    "known_order_details": {},  # {order_id: {title, buyer, price, chat_id, seen_at}}
    "known_messages": {},  # {order_id: last_msg_id}
    "deleted_ads": [],  # ids удалённых товаров — API отдаёт их ещё какое-то время
    # Отслеживаемые чаты, не привязанные ни к одному заказу, — поддержка и
    # модерация. Найти их сами мы не можем: списка чатов в API нет вовсе,
    # поэтому номера добавляются руками.
    "watched_chats": {},   # {chat_id: {"label": str, "last_msg": str}}
    "blacklist": [],  # list of buyer names to suppress notifications for
    "reminders": {"enabled": False, "hours": 24},
    "reminded_orders": [],  # order IDs already reminded (reset on status change)
    "auto_accept": {"enabled": False},  # авто «начать заказ» при поступлении
    "auto_confirm": {"enabled": False, "hours": 24},
    # Автовозврат «зависших» заказов. Единственная автоматика в боте, которая
    # ОТДАЁТ деньги, поэтому по умолчанию выключена и работает только там, где
    # бот сам знает, что товар не выдан.
    #   scope="stars" — только заказы, которых AutoStars ждёт (ник не прислан);
    #   scope="any"   — любой застрявший в работе, включая выданные вручную.
    "auto_refund": {"enabled": False, "hours": 48, "scope": "stars",
                    "max_per_day": 3, "day": "", "count": 0, "done": []},
    "withdrawal_history": [],  # [{amount, ts, type: manual/auto, status}]
    "balance_notify": {"enabled": False, "threshold": 1000, "last_notified_balance": 0.0},
    # Уведомления о новых заказах и о сообщениях в чатах. По умолчанию включены:
    # раньше они не имели выключателя вообще, и выключение не должно менять
    # поведение для тех, кто ничего не настраивал.
    "notify_orders": {"enabled": True},
    "notify_messages": {"enabled": True},
    "daily_report": {"enabled": False, "hour": 20, "last_report_day": ""},
    "quick_replies": ["Спасибо за заказ!", "Отправлю в течение часа.", "Уточните, пожалуйста."],
    "bump_schedule": {
        "enabled": False, "times": [], "last_runs": {},
        "price_per_bump": 0,   # ₽ за одно поднятие (0 = бесплатно)
        "daily_ceiling": 0,    # потолок трат на поднятия в день (0 = без лимита)
        "spent_today": 0.0,
        "spent_day": "",
        "spent_total": 0.0,    # накопленные расходы на поднятия
        "bumps_total": 0,      # всего поднятий сделано
    },
    "ad_packs": {},  # {"Пак имя": [ad_id, ...]} — группы объявлений
    "complaint_notify": {"enabled": True, "seen": []},  # уведомления о жалобах
    "reviews_monitor": {"enabled": False, "known_review_ids": []},
    "ad_templates": [],
    "plugins": {
        "auto_stars": {
            "enabled": False, "amount": 50, "note": "",
            # Пусто — узнаём звёздные заказы по всем обычным написаниям
            # («звёзд», «звезд», «stars», «⭐»); своё слово здесь означает
            # «только оно».
            "keyword": "",
            "ask_username": True,      # спрашивать @username в чате заказа
            "pending": {},             # {order_id: {quantity, asked_at}} — ждём username
            "delivered": [],           # order_id, по которым звёзды уже выданы
            "wallet_version": "v4r2",
            # Предупреждать, пока пополнить кошелёк ещё есть время: закончиться
            # TON посреди оплаченного заказа — худший исход.
            "low_balance_warn": True,
            "low_balance_deliveries": 2,
            "balance_checked_at": 0,
            "balance_low": False,
            "log": [],                 # журнал выдач: что, кому, почём
            # Что сообщать продавцу. Раньше слалось всё и всегда: удачная
            # выдача в потоке из тридцати заказов — это шум, а провал — нет.
            "notify": {"done": True, "failed": True, "low_balance": True},
            # Тексты покупателю. Пустая строка = взять стандартный.
            "texts": {"ask": "", "remind": "", "sending": "", "done": "",
                      "failed": ""},
        },
        # Robux выдаются кодом, а не зачислением на аккаунт, поэтому здесь
        # нет ни ника покупателя, ни «сколько выдавать»: количество диктует
        # заказ, а номинал берётся из каталога поставщика. Регион важен —
        # глобальный и российский коды невзаимозаменяемы.
        # `region` остаётся запасным вариантом: у товаров, созданных до
        # того, как регион стали писать в описание, взять его больше
        # неоткуда. Основной источник — само описание объявления.
        #
        # `ad_title` / `ad_text` — заготовки продавца для создания товара.
        # Пустые означают «взять наши»; подстановки описаны в
        # `automation.robux.fill_template`.
        "auto_roblox": {"enabled": False, "region": "GL", "keyword": "",
                        "note": "", "ad_title": "", "ad_text": "",
                        "delivered": [], "log": []},
        "auto_gifts": {"enabled": False, "gift_type": "", "note": ""},
    },
}


_DEFAULT_ACCOUNT = "Основной"


# ---------------------------------------------------------------------------
# Где лежат данные: PostgreSQL, если задан DATABASE_URL, иначе JSON-файлы.
# Остальной модуль (и весь бот) от этого не меняется — различаются только
# `_read_blob` и `_write_blob`. Каждый прежний JSON-файл становится строкой
# в kv_store, а имеющиеся файлы переезжают в базу при первом чтении.
# ---------------------------------------------------------------------------

import threading as _threading


def _resolve_database_url() -> str:
    """Найти адрес Postgres среди имён переменных, принятых у Railway и Heroku,
    либо собрать его из частей PG*. Пусто — значит база не настроена.
    """
    for var in ("DATABASE_URL", "DATABASE_PRIVATE_URL", "POSTGRES_URL",
                "POSTGRESQL_URL", "PG_URL", "DATABASE_PUBLIC_URL"):
        url = os.environ.get(var, "").strip()
        if url:
            # psycopg2 принимает и postgres://, но приводим к postgresql://
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            return url
    # Если есть отдельные переменные PG*, собираем адрес из них
    host = os.environ.get("PGHOST", "").strip()
    if host:
        user = os.environ.get("PGUSER", "postgres")
        pwd = os.environ.get("PGPASSWORD", "")
        port = os.environ.get("PGPORT", "5432")
        db = os.environ.get("PGDATABASE", "railway")
        auth = f"{user}:{pwd}@" if pwd else f"{user}@"
        return f"postgresql://{auth}{host}:{port}/{db}"
    return ""


_DATABASE_URL = _resolve_database_url()
_USE_DB = bool(_DATABASE_URL)
_db_lock = _threading.Lock()
_db_pool = None
_cache: dict[str, str] = {}

# ключ хранилища → прежний путь файла: для переезда и для работы без базы
_BLOBS = {
    "tokens": _FILE,
    "settings": _SETTINGS_FILE,
    "panel_creds": _PANEL_FILE,
    "fragment_creds": _FRAGMENT_FILE,
    "ns_creds": _NS_FILE,
    "approute_creds": _AR_FILE,
    "admin": _ADMIN_FILE,
}


def _init_pool():
    global _db_pool
    if _db_pool is not None:
        return
    import psycopg2.pool
    _db_pool = psycopg2.pool.ThreadedConnectionPool(1, 8, _DATABASE_URL)
    conn = _db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kv_store "
                "(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.commit()
    finally:
        _db_pool.putconn(conn)


def _db_read_raw(key: str) -> str | None:
    _init_pool()
    conn = _db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM kv_store WHERE k=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _db_pool.putconn(conn)


def _db_write_raw(key: str, raw: str) -> None:
    _init_pool()
    conn = _db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv_store (k, v) VALUES (%s, %s) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (key, raw))
        conn.commit()
    finally:
        _db_pool.putconn(conn)


def _read_blob(key: str) -> dict:
    """Прочитать словарь по ключу хранилища — из базы или из файла."""
    if not _USE_DB:
        path = _BLOBS[key]
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}
    with _db_lock:
        if key not in _cache:
            raw = _db_read_raw(key)
            if raw is None:
                # разовый переезд из прежнего JSON-файла, если он ещё лежит
                path = _BLOBS[key]
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            raw = f.read()
                        json.loads(raw)  # validate
                        _db_write_raw(key, raw)
                    except Exception:
                        raw = "{}"
                else:
                    raw = "{}"
            _cache[key] = raw
        try:
            return json.loads(_cache[key])
        except Exception:
            return {}


def _write_blob(key: str, data: dict) -> None:
    """Записать словарь по ключу хранилища — в базу или в файл."""
    if not _USE_DB:
        path = _BLOBS[key]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        return
    raw = json.dumps(data, ensure_ascii=False)
    with _db_lock:
        _cache[key] = raw
        _db_write_raw(key, raw)


def _load() -> dict:
    return _read_blob("tokens")


def _save_tokens(data: dict) -> None:
    _write_blob("tokens", data)


def _user_entry(data: dict, user_id: int) -> dict | None:
    """Запись аккаунтов продавца, с переносом старого формата «просто токен»."""
    raw = data.get(str(user_id))
    if raw is None:
        return None
    if isinstance(raw, str):  # legacy single-token format
        raw = {"accounts": {_DEFAULT_ACCOUNT: {"token": raw}},
               "active": _DEFAULT_ACCOUNT}
        data[str(user_id)] = raw
        _save_tokens(data)
    return raw


# ---------------------------------------------------------------------------
# Несколько аккаунтов
# ---------------------------------------------------------------------------

def get_accounts(user_id: int) -> dict:
    """Аккаунты продавца: {имя: {"token": …}}. Пусто — ни одного нет."""
    entry = _user_entry(_load(), user_id)
    return (entry or {}).get("accounts", {})


def get_active_account(user_id: int) -> str:
    entry = _user_entry(_load(), user_id)
    if not entry:
        return ""
    active = entry.get("active", "")
    if active not in entry.get("accounts", {}):
        accounts = entry.get("accounts", {})
        return next(iter(accounts), "")
    return active


def set_active_account(user_id: int, name: str) -> bool:
    data = _load()
    entry = _user_entry(data, user_id)
    if not entry or name not in entry.get("accounts", {}):
        return False
    entry["active"] = name
    _save_tokens(data)
    return True


def add_account(user_id: int, name: str, token: str, make_active: bool = True) -> None:
    data = _load()
    entry = _user_entry(data, user_id)
    if entry is None:
        entry = {"accounts": {}, "active": name}
        data[str(user_id)] = entry
    entry.setdefault("accounts", {})[name] = {"token": token}
    if make_active:
        entry["active"] = name
    _save_tokens(data)


def remove_account(user_id: int, name: str) -> bool:
    data = _load()
    entry = _user_entry(data, user_id)
    if not entry or name not in entry.get("accounts", {}):
        return False
    entry["accounts"].pop(name)
    if entry.get("active") == name:
        entry["active"] = next(iter(entry["accounts"]), "")
    if not entry["accounts"]:
        data.pop(str(user_id), None)
    _save_tokens(data)
    return True


def get_token(user_id: int) -> str | None:
    """Токен АКТИВНОГО аккаунта — вход, совместимый со старым кодом."""
    entry = _user_entry(_load(), user_id)
    if not entry:
        return None
    active = entry.get("active") or next(iter(entry.get("accounts", {})), "")
    acc = entry.get("accounts", {}).get(active)
    return acc.get("token") if acc else None


def save_token(user_id: int, token: str) -> None:
    """Записать токен активному аккаунту; если аккаунтов нет — создать первый."""
    data = _load()
    entry = _user_entry(data, user_id)
    if entry is None:
        data[str(user_id)] = {
            "accounts": {_DEFAULT_ACCOUNT: {"token": token}},
            "active": _DEFAULT_ACCOUNT,
        }
    else:
        active = entry.get("active") or _DEFAULT_ACCOUNT
        entry.setdefault("accounts", {}).setdefault(active, {})["token"] = token
        entry["active"] = active
    _save_tokens(data)


def delete_token(user_id: int) -> None:
    """Убрать активный аккаунт (выход). Остальные остаются на месте."""
    data = _load()
    entry = _user_entry(data, user_id)
    if not entry:
        return
    active = entry.get("active", "")
    entry.get("accounts", {}).pop(active, None)
    if entry.get("accounts"):
        entry["active"] = next(iter(entry["accounts"]))
    else:
        data.pop(str(user_id), None)
    _save_tokens(data)


def _load_settings() -> dict:
    return _read_blob("settings")


def _save_all_settings(all_settings: dict) -> None:
    _write_blob("settings", all_settings)


def _merge_defaults(settings: dict) -> dict:
    import copy
    result = copy.deepcopy(_DEFAULT_SETTINGS)
    for key, val in settings.items():
        if key == "plugins" and isinstance(val, dict):
            for pkey, pval in val.items():
                if pkey in result["plugins"] and isinstance(pval, dict):
                    result["plugins"][pkey].update(pval)
                else:
                    result["plugins"][pkey] = pval
        elif key == "auto_events" and isinstance(val, dict):
            for ekey, eval_ in val.items():
                if ekey in result["auto_events"] and isinstance(eval_, dict):
                    result["auto_events"][ekey].update(eval_)
                else:
                    result["auto_events"][ekey] = eval_
        elif isinstance(val, dict) and key in result and isinstance(result[key], dict):
            result[key].update(val)
        else:
            result[key] = val
    return result


def _account_key(user_id: int) -> str:
    """Ключ хранилища по аккаунту: «{продавец}::{аккаунт}», иначе просто номер."""
    account = get_active_account(user_id)
    return f"{user_id}::{account}" if account else str(user_id)


def _first_account_key(user_id: int) -> str:
    """Ключ ПЕРВОГО аккаунта — того, кому принадлежат данные до аккаунтов.

    Хранилища трёх видов (настройки, куки панели, данные Fragment) заведены
    ещё до многомагазинности, под голым `uid`. Перенос этих записей шёл
    **в активный аккаунт** — и в этом была ошибка: `add_account` делает
    новый аккаунт активным сразу, поэтому «первое чтение после переноса»
    наступало уже под вторым магазином. Второму доставались чужое название
    магазина, чужие куки панели и **чужая seed-фраза TON**.

    Так и вышло: продавец добавил второй магазин, а в меню осталось имя
    первого. Название — самое безобидное из трёх.

    Первый аккаунт — первый ключ в словаре: `save_token` заводит его до
    того, как появится второй, а порядок словаря сохраняется и в JSON,
    и в PostgreSQL.
    """
    first = next(iter(get_accounts(user_id)), "")
    return f"{user_id}::{first}" if first else str(user_id)


def _take_legacy(data: dict, user_id: int) -> bool:
    """Перенести долистовую запись первому аккаунту. True — если переносили.

    Данные не перетираются: если у первого аккаунта уже что-то есть,
    старая запись просто убирается.
    """
    legacy = str(user_id)
    if legacy not in data:
        return False
    target = _first_account_key(user_id)
    if target == legacy:
        return False
    moved = data.pop(legacy)
    if target not in data:
        data[target] = moved
    return True


def get_settings(user_id: int) -> dict:
    all_settings = _load_settings()
    if _take_legacy(all_settings, user_id):
        _save_all_settings(all_settings)
    return _merge_defaults(all_settings.get(_account_key(user_id), {}))


def save_settings(user_id: int, settings: dict) -> None:
    all_settings = _load_settings()
    all_settings[_account_key(user_id)] = settings
    _save_all_settings(all_settings)


def get_all_users() -> list[int]:
    return [int(uid) for uid in _load().keys()]


def get_shop_name(user_id: int) -> str:
    return get_settings(user_id).get("shop_name", "")


def save_shop_name(user_id: int, name: str) -> None:
    s = get_settings(user_id)
    s["shop_name"] = name
    save_settings(user_id, s)


# ---------------------------------------------------------------------------
# Доступ к панели продавца Юмаркета
# ---------------------------------------------------------------------------

def _load_panel_creds() -> dict:
    return _read_blob("panel_creds")


def _save_panel_data(data: dict) -> None:
    _write_blob("panel_creds", data)


def get_panel_creds(user_id: int) -> dict | None:
    """Куки панели активного аккаунта; старый общий формат переносится сюда же."""
    data = _load_panel_creds()
    if _take_legacy(data, user_id):
        _save_panel_data(data)
    return data.get(_account_key(user_id))


def accounts_with_panel(user_id: int) -> list[str]:
    """Аккаунты, у которых есть вход в панель.

    Вход в панель — свой у каждого магазина, и «баланс не показывается»
    после переключения обычно значит именно это. Назвать аккаунт, где вход
    есть, дешевле, чем оставить продавца гадать.
    """
    data = _load_panel_creds()
    out = []
    for name in get_accounts(user_id):
        if (data.get(f"{user_id}::{name}") or {}).get("cookies"):
            out.append(name)
    return out


def save_panel_creds(user_id: int, creds: dict) -> None:
    """Сохранить доступ к панели для активного аккаунта продавца."""
    data = _load_panel_creds()
    data[_account_key(user_id)] = creds
    _save_panel_data(data)


def delete_panel_creds(user_id: int) -> None:
    """Убрать доступ к панели у активного аккаунта продавца."""
    data = _load_panel_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _save_panel_data(data)


# ---------------------------------------------------------------------------
# Доступ к Fragment (автовыдача звёзд Telegram) — СЕКРЕТНОЕ
# {cookies: {…}, mnemonic: «24 слова», wallet_version: "v4r2", api_hash: "…"}
# Хранится по активному аккаунту. Ни фраза, ни значения кук в логи не идут.
# ---------------------------------------------------------------------------

# Ключ шифрования seed-фразы. Берётся из окружения и в репозиторий не
# попадает; на Railway задаётся переменной SECRET_KEY (или FRAGMENT_KEY).
#
# Подстраховки «нет ключа — придумаем свой» здесь нет намеренно. Ключ,
# выведенный из чего-то, что лежит рядом с данными, шифрованием не является:
# он создаёт ощущение защиты, а seed-фраза — это чужой кошелёк. Нет ключа —
# храним как раньше и говорим об этом вслух в /version.
_SECRET_KEY = (os.environ.get("FRAGMENT_KEY")
               or os.environ.get("SECRET_KEY") or "").strip()
_ENC_PREFIX = "enc:v1:"


def _fernet():
    """Шифровальщик или None, если ключа нет либо библиотека недоступна."""
    if not _SECRET_KEY:
        return None
    try:
        import base64
        import hashlib

        from cryptography.fernet import Fernet
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # Не `Exception`: битая сборка cryptography роняет импорт
        # `pyo3_runtime.PanicException`, а он наследуется от BaseException и
        # мимо обычного перехвата проходит насквозь. Поймано на этой самой
        # машине: падал не только вход в настройки, но и /version, то есть
        # ровно та команда, которой выясняют, что происходит.
        logger.exception("Шифрование недоступно — cryptography не загрузилась")
        return None
    # Ключ Fernet — ровно 32 байта в base64. Продавец задаёт произвольную
    # строку, поэтому она приводится к нужной длине хешем, а не обрезанием:
    # обрезание молча ослабило бы длинный ключ до первых символов.
    digest = hashlib.sha256(_SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encryption_on() -> bool:
    """Шифруется ли seed-фраза на самом деле. Показывается в /version."""
    return _fernet() is not None


def _seal(value: str) -> str:
    f = _fernet()
    if not f or not value or str(value).startswith(_ENC_PREFIX):
        return value
    try:
        return _ENC_PREFIX + f.encrypt(str(value).encode("utf-8")).decode()
    except Exception:                              # pragma: no cover
        return value


def _unseal(value: str) -> str:
    """Расшифровать. Записи, сделанные до шифрования, читаются как есть."""
    text = str(value or "")
    if not text.startswith(_ENC_PREFIX):
        return text
    f = _fernet()
    if not f:
        # Ключ потеряли или сменили. Отдать зашифрованную строку как
        # seed-фразу нельзя: кошелёк из неё не соберётся, а сообщение об
        # ошибке будет про «неверную seed-фразу» вместо «нет ключа».
        return ""
    try:
        return f.decrypt(text[len(_ENC_PREFIX):].encode()).decode("utf-8")
    except Exception:
        return ""


def _load_fragment_creds() -> dict:
    return _read_blob("fragment_creds")


def _save_fragment_data(data: dict) -> None:
    _write_blob("fragment_creds", data)


def get_fragment_creds(user_id: int) -> dict | None:
    data = _load_fragment_creds()
    # Перенос — первому аккаунту, а не активному. Здесь лежит seed-фраза
    # TON: отдать её другому магазину значит отдать доступ к кошельку.
    if _take_legacy(data, user_id):
        _save_fragment_data(data)
    creds = data.get(_account_key(user_id))
    if not creds:
        return creds
    if creds.get("mnemonic"):
        creds = {**creds, "mnemonic": _unseal(creds["mnemonic"])}
    return creds


def save_fragment_creds(user_id: int, creds: dict) -> None:
    data = _load_fragment_creds()
    existing = data.get(_account_key(user_id)) or {}
    existing.update(creds)
    # Шифруется в единственном месте — на записи. Старые записи в открытом
    # виде переезжают сюда же при первом же сохранении.
    if existing.get("mnemonic"):
        existing["mnemonic"] = _seal(_unseal(existing["mnemonic"]))
    data[_account_key(user_id)] = existing
    _save_fragment_data(data)
    if not _USE_DB:
        try:  # tighten file perms — it holds a seed phrase
            os.chmod(_FRAGMENT_FILE, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Доступ к поставщику ns.gifts: {user_id, login, password, api_secret, proxy}.
# Шифруются пароль и секрет — вместе они дают право тратить баланс кабинета.
# ---------------------------------------------------------------------------

# Что именно прячем. Логин и user_id не секреты сами по себе, но без пароля и
# ключа они бесполезны, поэтому шифруем ровно те два поля, ради которых стоит
# заводить шифрование.
_NS_SECRET_FIELDS = ("api_secret", "password")


def ns_fields() -> tuple[str, ...]:
    """Какие поля нужны для входа к поставщику — один список на бота."""
    return ("user_id", "login", "password", "api_secret")


def get_ns_creds(user_id: int) -> dict:
    data = _read_blob("ns_creds")
    creds = dict(data.get(_account_key(user_id)) or {})
    for field in _NS_SECRET_FIELDS:
        if creds.get(field):
            creds[field] = _unseal(creds[field])
    return creds


def save_ns_creds(user_id: int, creds: dict) -> None:
    data = _read_blob("ns_creds")
    existing = dict(data.get(_account_key(user_id)) or {})
    existing.update(creds)
    for field in _NS_SECRET_FIELDS:
        if existing.get(field):
            # Как и у seed-фразы: шифруем на записи и только один раз —
            # `_unseal` перед `_seal` не даёт нарастить второй слой.
            existing[field] = _seal(_unseal(existing[field]))
    data[_account_key(user_id)] = existing
    _write_blob("ns_creds", data)
    if not _USE_DB:
        try:
            os.chmod(_NS_FILE, 0o600)
        except OSError:
            pass


def delete_ns_creds(user_id: int) -> None:
    data = _read_blob("ns_creds")
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _write_blob("ns_creds", data)


# ---------------------------------------------------------------------------
# Доступ к поставщику AppRoute: {api_key, region, proxy}. Шифруются ключ и
# прокси: первый даёт право тратить баланс кабинета целиком, во втором лежат
# логин с паролем от платного адреса.
# ---------------------------------------------------------------------------

# Прокси шифруется наравне с ключом: в его строке обычно логин и пароль от
# платного адреса, то есть чужой доступ ровно так же.
_AR_SECRET_FIELDS = ("api_key", "proxy")


def ar_fields() -> tuple[str, ...]:
    """Что нужно для входа к AppRoute — один список на бота.

    Регион сюда не входит: у него есть значение по умолчанию, и требовать
    его выбора значит держать продавца перед экраном, который и так знает
    ответ.
    """
    return ("api_key",)


def get_ar_creds(user_id: int) -> dict:
    data = _read_blob("approute_creds")
    creds = dict(data.get(_account_key(user_id)) or {})
    for field in _AR_SECRET_FIELDS:
        if creds.get(field):
            creds[field] = _unseal(creds[field])
    return creds


def save_ar_creds(user_id: int, creds: dict) -> None:
    data = _read_blob("approute_creds")
    existing = dict(data.get(_account_key(user_id)) or {})
    existing.update(creds)
    for field in _AR_SECRET_FIELDS:
        if existing.get(field):
            # Как у seed-фразы: шифруем на записи и только один раз —
            # `_unseal` перед `_seal` не даёт нарастить второй слой.
            existing[field] = _seal(_unseal(existing[field]))
    data[_account_key(user_id)] = existing
    _write_blob("approute_creds", data)
    if not _USE_DB:
        try:
            os.chmod(_AR_FILE, 0o600)
        except OSError:
            pass


def delete_ar_creds(user_id: int) -> None:
    data = _read_blob("approute_creds")
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _write_blob("approute_creds", data)


def delete_fragment_creds(user_id: int) -> None:
    data = _load_fragment_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _save_fragment_data(data)


# ---------------------------------------------------------------------------
# Общее на весь бот: владелец и админы, подписки, цена, заблокированные.
# Не по аккаунтам.
# ---------------------------------------------------------------------------

import time as _time

OWNER_ID = 6887373040


def _load_admin() -> dict:
    return _read_blob("admin")


def _save_admin(data: dict) -> None:
    _write_blob("admin", data)


def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    return int(user_id) in [int(x) for x in _load_admin().get("admins", [])]


def add_admin(user_id: int) -> None:
    data = _load_admin()
    admins = [int(x) for x in data.get("admins", [])]
    if int(user_id) not in admins:
        admins.append(int(user_id))
    data["admins"] = admins
    _save_admin(data)


def remove_admin(user_id: int) -> bool:
    data = _load_admin()
    admins = [int(x) for x in data.get("admins", [])]
    if int(user_id) in admins:
        admins.remove(int(user_id))
        data["admins"] = admins
        _save_admin(data)
        return True
    return False


def list_admins() -> list[int]:
    return [OWNER_ID] + [int(x) for x in _load_admin().get("admins", [])
                         if int(x) != OWNER_ID]


# --- Subscriptions ---------------------------------------------------------

def grant_subscription(user_id: int, days: int, by: int = 0) -> float:
    """Добавить продавцу `days` подписки — от сегодня либо к уже имеющейся.

    Отдаёт новый момент окончания.
    """
    data = _load_admin()
    subs = data.setdefault("subscriptions", {})
    now = _time.time()
    cur = subs.get(str(user_id), {})
    base = max(now, float(cur.get("expires", 0)))
    expires = base + days * 86400
    subs[str(user_id)] = {"expires": expires, "by": int(by)}
    _save_admin(data)
    return expires


def revoke_subscription(user_id: int) -> bool:
    data = _load_admin()
    subs = data.get("subscriptions", {})
    if str(user_id) in subs:
        subs.pop(str(user_id))
        _save_admin(data)
        return True
    return False


def get_subscription(user_id: int) -> dict | None:
    return _load_admin().get("subscriptions", {}).get(str(user_id))


def has_active_subscription(user_id: int) -> bool:
    sub = get_subscription(user_id)
    return bool(sub and float(sub.get("expires", 0)) > _time.time())


def subscription_days_left(user_id: int) -> int:
    sub = get_subscription(user_id)
    if not sub:
        return 0
    left = float(sub.get("expires", 0)) - _time.time()
    return max(0, int(left // 86400))


def count_subscribers() -> int:
    subs = _load_admin().get("subscriptions", {})
    now = _time.time()
    return sum(1 for s in subs.values() if float(s.get("expires", 0)) > now)


# --- Bot price -------------------------------------------------------------

def get_bot_price() -> int:
    return int(_load_admin().get("bot_price", 0))


def set_bot_price(price: int) -> None:
    data = _load_admin()
    data["bot_price"] = int(price)
    _save_admin(data)


# --- Заблокированные, на весь бот -------------------------------------------

def block_user(user_id: int) -> None:
    data = _load_admin()
    blocked = [int(x) for x in data.get("blocked", [])]
    if int(user_id) not in blocked:
        blocked.append(int(user_id))
    data["blocked"] = blocked
    _save_admin(data)


def unblock_user(user_id: int) -> bool:
    data = _load_admin()
    blocked = [int(x) for x in data.get("blocked", [])]
    if int(user_id) in blocked:
        blocked.remove(int(user_id))
        data["blocked"] = blocked
        _save_admin(data)
        return True
    return False


def is_blocked(user_id: int) -> bool:
    return int(user_id) in [int(x) for x in _load_admin().get("blocked", [])]


def list_blocked() -> list[int]:
    return [int(x) for x in _load_admin().get("blocked", [])]


def count_users() -> int:
    return len(get_all_users())


def require_subscription_enabled() -> bool:
    return bool(_load_admin().get("require_subscription", False))


def set_require_subscription(enabled: bool) -> None:
    data = _load_admin()
    data["require_subscription"] = bool(enabled)
    _save_admin(data)


# ---------------------------------------------------------------------------
# Оформление, на весь бот: свои надписи кнопок главного меню и, если задано,
# кастомное эмодзи в заголовке. По-настоящему покрасить кнопку Telegram не
# даёт, поэтому «раскраска» — это цветное эмодзи в надписи.
# ---------------------------------------------------------------------------

# ключ → (стандартная надпись, кнопка) для главного меню
MENU_BUTTONS = [
    ("ads",      "🚀 Объявления", "menu:ads"),
    ("orders",   "🛒 Заказы",     "menu:orders"),
    ("chats",    "💬 Чаты",       "menu:chats"),
    ("balance",  "💰 Баланс",     "menu:balance"),
    ("stats",    "📊 Статистика", "menu:stats"),
    ("plugins",  "🧩 Плагины",    "plugins:menu"),
    ("autopilot", "⚡ Автопилот",  "ap:menu"),
    ("settings", "⚙️ Настройки",  "settings:menu"),
]


def get_appearance() -> dict:
    return _load_admin().get("appearance", {})


# --- Правовые документы, на весь бот ----------------------------------------
#
# Ссылки на документы принадлежат владельцу бота, а не продавцу-подписчику,
# поэтому лежат в общем хранилище рядом с ценой и подписками, а не в
# настройках каждого магазина.
#
# Объявлением, а не тремя парами функций: документ добавляется одной строкой
# здесь, и экран с админкой подхватывают его сами.
POLICY_DOCS: tuple[tuple[str, str], ...] = (
    ("terms", "📜 Пользовательское соглашение"),
    ("offer", "📄 Публичная оферта"),
    ("privacy", "🔒 Политика конфиденциальности"),
)


def get_policy_links() -> dict:
    """{ключ документа: ссылка}. Незаданные ключи отсутствуют, а не пусты.

    Отсутствие и пустая строка — разные вещи для того, кто рисует кнопки:
    кнопка с пустым адресом не «ведёт никуда», а роняет отправку целиком —
    Telegram отвергает такую клавиатуру, и экран не приходит вовсе.
    """
    saved = _load_admin().get("policy_links", {})
    return {k: str(saved[k]).strip() for k, _title in POLICY_DOCS
            if str(saved.get(k) or "").strip()}


def set_policy_link(key: str, url: str) -> None:
    data = _load_admin()
    links = data.setdefault("policy_links", {})
    url = str(url or "").strip()
    if url:
        links[key] = url
    else:
        links.pop(key, None)
    _save_admin(data)


def clear_policy_link(key: str) -> None:
    set_policy_link(key, "")


def get_menu_labels() -> dict:
    """Надписи пунктов меню: {ключ: надпись}, поверх стандартных — правки админа."""
    overrides = get_appearance().get("menu_labels", {})
    return {key: overrides.get(key, default) for key, default, _cb in MENU_BUTTONS}


def set_menu_label(key: str, label: str) -> None:
    data = _load_admin()
    ap = data.setdefault("appearance", {})
    labels = ap.setdefault("menu_labels", {})
    labels[key] = label
    _save_admin(data)


def reset_menu_labels() -> None:
    data = _load_admin()
    data.setdefault("appearance", {})["menu_labels"] = {}
    _save_admin(data)


def get_header_emoji() -> dict | None:
    """{'id': номер кастомного эмодзи, 'fallback': '🏠'} либо None."""
    return get_appearance().get("header_emoji")


def set_header_emoji(emoji_id: str, fallback: str) -> None:
    data = _load_admin()
    ap = data.setdefault("appearance", {})
    ap["header_emoji"] = {"id": str(emoji_id), "fallback": fallback or "🏠"}
    _save_admin(data)


def clear_header_emoji() -> None:
    data = _load_admin()
    data.setdefault("appearance", {}).pop("header_emoji", None)
    _save_admin(data)


def menu_header_html() -> str:
    """Значок в заголовке главного меню: кастомное эмодзи, если задано; иначе
    обычное, если админ его выбрал; иначе 🏠.
    """
    he = get_header_emoji()
    if he:
        fb = he.get("fallback") or "🏠"
        if he.get("id"):
            return f'<tg-emoji emoji-id="{he["id"]}">{fb}</tg-emoji>'
        return fb  # plain emoji chosen by the admin
    return "🏠"


# ---------------------------------------------------------------------------
# Редактируемые тексты бота, общие на весь бот, с кастомными эмодзи.
# Хранятся готовым к отправке HTML (из message.html_text). Подстановки в
# {фигурных} скобках заполняются при сборке сообщения.
# ---------------------------------------------------------------------------

# Первый экран заканчивается тем, что человек должен сделать прямо сейчас,
# и кнопка под ним ведёт ровно туда. Раньше шаги упирались в ссылку внутри
# текста: её надо было заметить, нажать, вернуться — и всё это до того, как
# бот доказал хоть одну свою пользу.
_TOKEN_HOWTO = (
    "🔑 <b>Шаг 1 из 2</b>  ·  Подключаем магазин\n\n"
    "Нужен API-токен — ключ, которым я вижу ваши заказы и чаты.\n\n"
    "1️⃣ Откройте панель кнопкой ниже\n"
    "2️⃣ <b>Мой магазин</b> → вкладка <b>Интеграции</b>\n"
    "3️⃣ <b>Создать токен</b> → скопировать\n"
    "4️⃣ Прислать сюда одним сообщением\n\n"
    "<i>🔒 Токен виден только вашему боту, а сообщение с ним я сразу удалю "
    "из переписки.</i>"
)

_WHAT_I_DO = (
    "⚡️ <b>Принимаю заказ за секунды</b> — в 4 утра тоже\n"
    "💬 <b>Отвечаю покупателю мгновенно</b> — пока он не ушёл к другому\n"
    "🔄 <b>Возвращаю распроданное в продажу</b> — витрина не простаивает\n"
    "⭐️ <b>Выдаю звёзды автоматически</b> — без вашего участия\n"
    "🔔 <b>Ловлю жалобы и споры первым</b> — до арбитража, а не после\n"
    "📊 <b>Считаю выручку и чистыми</b> — каждый вечер, без таблиц"
)

CUSTOM_TEXTS = {
    "welcome": {
        "title": "Приветствие /start",
        "vars": [],
        "default": (
            "👋 Вы в двух минутах от того, чтобы магазин работал без вас.\n\n"
            "Каждый неотвеченный час — это заказ, который ушёл к тому, кто "
            "ответил быстрее. Я закрываю эти часы.\n\n"
            + _WHAT_I_DO + "\n\n"
            "<b>Рутина — на мне. Продажи — ваши.</b>\n\n"
            "━━━━━━━━━━━━━━\n"
            + _TOKEN_HOWTO
        ),
    },
    "token_ok": {
        "title": "После ввода токена (шаг 2 — панель)",
        "vars": ["{name}", "{balance}"],
        "default": (
            "🔥 <b>Магазин на связи!</b>\n\n"
            "🏪 {name}   ·   💰 {balance} ₽\n\n"
            "Заказы и чаты уже под автоматикой — с этой секунды ни один "
            "покупатель не ждёт вас впустую.\n\n"
            "━━━━━━━━━━━━━━\n"
            "🚀 <b>Шаг 2 из 2 — включить полную автоматизацию</b>\n\n"
            "Токен открыл заказы и чаты. Вход в панель отдаёт мне ещё пачку "
            "функций, которые Юмаркет не даёт через API, — и все их я беру "
            "на себя:\n\n"
            "🆕 <b>Выкладываю товары</b> — вы даёте название и цену, остальное я\n"
            "⭐️ <b>Продвигаю сам</b> — по расписанию, в рамках вашего бюджета\n"
            "🔄 <b>Возвращаю снятое в продажу</b> — сам, с проверкой остатков\n"
            "💸 <b>Вывожу деньги</b> — по порогу, на карту, СБП или крипту\n"
            "🛟 <b>Отвечаю поддержке</b> — из этого же чата\n\n"
            "Дальше вы просто нажимаете кнопки, а рутина крутится без вас.\n\n"
            "Это 30 секунд: почта → код из письма → готово.\n"
            "<i>Пароль не нужен и не запрашивается.</i>"
        ),
    },
    "token_help": {
        "title": "Подсказка «Не нахожу токен»",
        "vars": [],
        "default": (
            "❓ <b>Где взять токен</b>\n"
            "━━━━━━━━━━━━━━\n"
            "Токен выдаёт сам Юмаркет — в боте его взять негде.\n\n"
            "1️⃣ <a href=\"https://panel.yoomarket.net\">panel.yoomarket.net</a> "
            "— вход тот же, что на сайте\n"
            "2️⃣ Слева <b>Мой магазин</b>\n"
            "3️⃣ Вкладка <b>Интеграции</b>\n"
            "4️⃣ Кнопка <b>Создать токен</b>\n"
            "5️⃣ Скопировать строку целиком и прислать сюда\n\n"
            "<b>Где обычно застревает</b>\n\n"
            "• <b>Скопировалась половина.</b> Токен — одна длинная строка без "
            "пробелов. Берите его кнопкой «копировать» рядом с полем, а не "
            "выделением пальцем.\n"
            "• <b>Вкладки «Интеграции» нет.</b> Она открыта не всем магазинам "
            "— это к поддержке Юмаркета, из бота тут ничего не сделать.\n"
            "• <b>Токен создавали раньше.</b> Второй раз панель его не "
            "покажет: создайте новый, прежний перестанет работать.\n\n"
            "━━━━━━━━━━━━━━\n"
            "<i>🔒 Токен открывает заказы и чаты — и только их. Вывести "
            "деньги через него нельзя: у Юмаркета в API такой возможности "
            "нет вовсе.</i>"
        ),
    },
    "policy": {
        "title": "Экран /policy — правовые документы",
        "vars": [],
        "default": (
            "Используя данный бот, вы подтверждаете, что ознакомились и "
            "соглашаетесь с условиями Пользовательского соглашения, "
            "Публичной оферты и Политики конфиденциальности."
        ),
    },
    "subscription": {
        "title": "Сообщение «нужна подписка»",
        "vars": ["{price}"],
        "default": (
            "🔒 <b>Требуется подписка</b>\n\n"
            "Для доступа к боту нужна активная подписка.{price}\n\n"
            "Обратитесь к владельцу бота для покупки."
        ),
    },
    "sub_granted": {
        "title": "Уведомление о выданной подписке",
        "vars": ["{days}", "{left}"],
        "default": (
            "🎉 <b>Доступ открыт — на {days} дн.</b>\n"
            "<i>Осталось: {left} дн.</i>\n\n"
            "Спасибо за доверие. Теперь по делу — что изменится уже сегодня.\n\n"
            "Сейчас ваш магазин зарабатывает только когда вы у телефона. "
            "Ночью заказы висят непринятыми, распроданное молчит, а покупатель "
            "уходит к тому, кто ответил первым.\n\n"
            "С этой минуты за вас работаю я:\n\n"
            + _WHAT_I_DO + "\n\n"
            "<b>Вы занимаетесь ростом. Рутину закрываю я.</b>\n\n"
            "━━━━━━━━━━━━━━\n"
            + _TOKEN_HOWTO
        ),
    },
}


def get_custom_text(key: str) -> str:
    """Сохранённый HTML по ключу текста — либо стандартный, если своего нет."""
    saved = get_appearance().get("texts", {}).get(key)
    if saved is not None:
        return saved
    return CUSTOM_TEXTS.get(key, {}).get("default", "")


def set_custom_text(key: str, html: str) -> None:
    data = _load_admin()
    ap = data.setdefault("appearance", {})
    texts = ap.setdefault("texts", {})
    texts[key] = html
    _save_admin(data)


def clear_custom_text(key: str) -> None:
    data = _load_admin()
    texts = data.setdefault("appearance", {}).setdefault("texts", {})
    texts.pop(key, None)
    _save_admin(data)


def is_custom_text_set(key: str) -> bool:
    return key in get_appearance().get("texts", {})


def render_custom_text(key: str, **subs) -> str:
    """Собрать текст с подстановками вида {имя} — не спотыкаясь о чужие скобки."""
    tmpl = get_custom_text(key)
    for k, v in subs.items():
        tmpl = tmpl.replace("{" + k + "}", str(v))
    return tmpl


# ---------------------------------------------------------------------------
# Удаление данных продавца
# ---------------------------------------------------------------------------
#
# Политика конфиденциальности обещает две вещи: удаление по запросу и
# удаление через три дня после окончания подписки. Обещание, которого код не
# выполняет, — это не недоделка, а ложь в публичном документе, на который
# сошлются в споре.
#
# Хранилищ семь, и данные продавца лежат в шести из них под ключом `{uid}`
# либо `{uid}::{аккаунт}` — по одному на каждый его магазин. Удаление,
# прошедшее мимо одного хранилища, хуже отсутствующего: оно ЗАЯВЛЯЕТ
# полноту. Поэтому список хранилищ здесь один и перечислен явно, а функция
# возвращает отчёт о том, что действительно стёрла.

# Хранилища, где данные разложены по ключу продавца.
_USER_BLOBS: tuple[str, ...] = (
    "tokens", "settings", "panel_creds", "fragment_creds",
    "approute_creds", "ns_creds",
)


def _user_keys(blob: dict, user_id: int) -> list[str]:
    """Ключи одного продавца в хранилище — свой и по каждому его магазину."""
    uid = str(int(user_id))
    return [k for k in blob if k == uid or str(k).startswith(f"{uid}::")]


def purge_user(user_id: int) -> dict:
    """Стереть все данные продавца. Отдаёт отчёт: {хранилище: сколько записей}.

    Чего НЕ трогает и почему:

    * **чёрный список.** Иначе «удалить мои данные» становится способом
      снять блокировку: заблокировали — стёр — вернулся;
    * **отметку о выданном пробном периоде.** По той же причине: иначе
      `/forget_me` превращается в способ брать бесплатные дни сколько
      угодно раз. Обе оговорки записаны в политике конфиденциальности —
      данные, тайно оставленные после «полного удаления», это ложь в
      опубликованном документе;
    * **владельца бота.** Стереть его данные значит выключить бота себе же;
      функция на владельце отказывает и говорит об этом отчётом.

    Отчёт возвращается не для красоты: «✅ удалено» без перечня — то самое
    бодрое сообщение об успехе, по которому нельзя понять, случилось ли
    что-нибудь. По нему же видно, что удалять было нечего.
    """
    uid = int(user_id)
    if is_owner(uid):
        return {"отказ": "владелец бота — его данные держат сам бот"}

    report: dict[str, int] = {}
    for name in _USER_BLOBS:
        blob = _read_blob(name)
        keys = _user_keys(blob, uid)
        if not keys:
            continue
        for k in keys:
            blob.pop(k, None)
        _write_blob(name, blob)
        report[name] = len(keys)

    # Подписка и права админа — тоже данные о человеке. Блокировка — нет:
    # она про защиту бота, а не про удобство того, кого заблокировали.
    data = _load_admin()
    dirty = False
    if str(uid) in (data.get("subscriptions") or {}):
        data["subscriptions"].pop(str(uid), None)
        report["subscriptions"] = 1
        dirty = True
    admins = [int(x) for x in data.get("admins", [])]
    if uid in admins:
        data["admins"] = [x for x in admins if x != uid]
        report["admins"] = 1
        dirty = True
    if dirty:
        _save_admin(data)
    return report


def expired_before(cutoff: float) -> list[int]:
    """Продавцы, чья подписка кончилась раньше `cutoff`.

    Только те, у кого запись о подписке ЕСТЬ. Продавец без записи — это не
    «подписка кончилась бесконечно давно», а человек, работающий в боте с
    выключенной проверкой подписки; стереть его данные значит снести
    работающий магазин.
    """
    subs = _load_admin().get("subscriptions", {}) or {}
    out: list[int] = []
    for uid, row in subs.items():
        try:
            expires = float((row or {}).get("expires", 0))
        except (TypeError, ValueError):
            continue
        if expires and expires < cutoff:
            out.append(int(uid))
    return out


# ---------------------------------------------------------------------------
# Тарифы по срокам и пробный период
# ---------------------------------------------------------------------------
#
# До этого цена была одним числом `bot_price`, и экран «нужна подписка»
# показывал его же. Документы обещают градацию по срокам со скидкой за
# длинный срок — значит она должна быть в интерфейсе, а не только в тексте
# оферты: цена, которой клиент нигде не видит, обещанием не является.
#
# `bot_price` оставлен и продолжает работать: это цена месяца и запасной
# ответ, когда тарифы не заданы.

# Сроки, под которые заводятся тарифы. Дни, а не «месяцы»: `grant_subscription`
# считает днями, и перевод месяцев в дни в двух местах разошёлся бы.
PRICE_TIERS: tuple[tuple[int, str], ...] = (
    (30, "1 месяц"),
    (90, "3 месяца"),
    (180, "6 месяцев"),
    (365, "12 месяцев"),
)

# Пробный период. Ноль — выключен: тогда бот про него не заикается, а не
# обещает «3 дня» и молчит в ответ.
TRIAL_DAYS_DEFAULT = 3


def get_prices() -> dict[int, int]:
    """{дней: цена ₽}. Незаданные тарифы отсутствуют, а не равны нулю.

    Ноль и «не задано» — разные вещи: «0 ₽» на экране читается как
    «бесплатно», а это обещание, за которое спросят.
    """
    saved = _load_admin().get("prices", {}) or {}
    out: dict[int, int] = {}
    for days, _label in PRICE_TIERS:
        try:
            value = int(saved.get(str(days), 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[days] = value
    return out


def set_price(days: int, price: int) -> None:
    data = _load_admin()
    prices = data.setdefault("prices", {})
    if int(price) > 0:
        prices[str(int(days))] = int(price)
    else:
        prices.pop(str(int(days)), None)
    _save_admin(data)


def price_lines() -> list[str]:
    """Тарифы строками для экрана «нужна подписка».

    Скидка считается от цены месяца и показывается только там, где она
    действительно есть: приписка «выгоднее» к тарифу без выгоды — враньё,
    которое клиент проверит за десять секунд.
    """
    prices = get_prices()
    if not prices:
        base = get_bot_price()
        return [f"💰 Стоимость: <b>{base} ₽</b>"] if base else []

    month = prices.get(30) or get_bot_price()
    lines: list[str] = []
    for days, label in PRICE_TIERS:
        price = prices.get(days)
        if not price:
            continue
        row = f"• {label} — <b>{price} ₽</b>"
        if month and days > 30:
            full = month * days / 30
            saved = round((1 - price / full) * 100)
            if saved >= 1:
                row += f"  <i>(−{saved}%)</i>"
        lines.append(row)
    return lines


# --- Пробный период ---------------------------------------------------------
#
# Кому пробный период уже выдавался. Список ПЕРЕЖИВАЕТ удаление данных —
# иначе `/forget_me` превращается в способ брать три дня бесплатно сколько
# угодно раз. Это ровно та же оговорка, что и у чёрного списка, и она
# записана в политике конфиденциальности: тайно оставленные после «полного
# удаления» данные — это ложь в опубликованном документе.


def get_trial_days() -> int:
    return int(_load_admin().get("trial_days", TRIAL_DAYS_DEFAULT))


def set_trial_days(days: int) -> None:
    data = _load_admin()
    data["trial_days"] = max(0, int(days))
    _save_admin(data)


def trial_used(user_id: int) -> bool:
    return int(user_id) in [int(x) for x in _load_admin().get("trials", [])]


def note_trial(user_id: int) -> None:
    data = _load_admin()
    used = [int(x) for x in data.get("trials", [])]
    if int(user_id) not in used:
        used.append(int(user_id))
        data["trials"] = used
        _save_admin(data)


def start_trial(user_id: int) -> int:
    """Выдать пробный период → сколько дней выдано (0 — не положено).

    Ноль возвращается в трёх случаях: пробный период выключен владельцем,
    он уже брался этим человеком, либо подписка и так действует. Вызывающий
    по нулю ничего не обещает — молчит.
    """
    days = get_trial_days()
    if days <= 0 or trial_used(user_id) or has_active_subscription(user_id):
        return 0
    note_trial(user_id)
    grant_subscription(user_id, days)
    return days
