import json
import os
import shutil


def _resolve_data_dir() -> str:
    """
    Pick a data directory that survives container re-deploys.

    Priority:
      1. $DATA_DIR env var (explicit override — e.g. a Railway volume mount path)
      2. /app/data — the volume mount point declared in docker-compose.yml
         (`bot_data:/app/data`). Persistent across `docker compose up --build`.
      3. ~/.yomarket — fallback for bare-metal / non-Docker runs.

    The previous version defaulted to ~/.yomarket even inside Docker, which is
    NOT covered by the docker-compose volume, so data was wiped on every redeploy.
    """
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    # In the Docker image WORKDIR is /app and the compose volume maps /app/data.
    if os.path.isdir("/app"):
        return "/app/data"
    return os.path.join(os.path.expanduser("~"), ".yomarket")


_DATA_DIR = _resolve_data_dir()
# Where storage files might have been written by older versions of the bot.
_LEGACY_DIRS = [
    os.path.join(os.path.dirname(__file__), "data"),     # bot/data/
    os.path.join(os.path.expanduser("~"), ".yomarket"),  # previous (broken) default
]


def _migrate_legacy() -> None:
    """Move *.json from any known legacy location into the active data dir, once."""
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
# Sensitive: Fragment cookies + TON wallet seed phrase. Never logged/committed.
_FRAGMENT_FILE = os.path.join(_DATA_DIR, "fragment_creds.json")
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
        "require_stock": True,   # не публиковать распроданное
        "last_restore_run": 0,
        "restored_total": 0,
        # {ad_id: {"tries": n, "until": ts, "reason": str}} — объявление,
        # которое маркетплейс отверг, не долбится каждый час
        "failures": {},
        "last_result": "",
    },
    "auto_bump": {"enabled": False, "interval_hours": 24},
    "auto_withdraw": {"enabled": False, "min_amount": 500},
    "responders": {},  # {"GameName": "message text"} - keyed by ad title/name
    "known_orders": {},  # {order_id: status}
    "known_order_ids": [],
    "known_order_details": {},  # {order_id: {title, buyer, price, chat_id, seen_at}}
    "known_messages": {},  # {order_id: last_msg_id}
    # Chats to watch that belong to no order — support, moderation. They cannot
    # be discovered (the API has no chat list), so their ids are added by hand.
    "watched_chats": {},   # {chat_id: {"label": str, "last_msg": str}}
    "blacklist": [],  # list of buyer names to suppress notifications for
    "reminders": {"enabled": False, "hours": 24},
    "reminded_orders": [],  # order IDs already reminded (reset on status change)
    "auto_accept": {"enabled": False},  # авто «начать заказ» при поступлении
    "auto_confirm": {"enabled": False, "hours": 24},
    "withdrawal_history": [],  # [{amount, ts, type: manual/auto, status}]
    "balance_notify": {"enabled": False, "threshold": 1000, "last_notified_balance": 0.0},
    "daily_report": {"enabled": False, "hour": 20, "last_report_day": ""},
    "quick_replies": ["Спасибо за заказ!", "Отправлю в течение часа.", "Уточните, пожалуйста."],
    "buyer_notes": {},
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
    "price_schedule": {
        "enabled": False,
        "from_hour": 22,   # начало окна (напр. ночь с 22:00)
        "to_hour": 8,      # конец окна (до 8:00)
        "percent": -10.0,  # изменение цены в окне
        "night_active": False,
        "base_prices": {},  # {ad_id: базовая цена} для восстановления
    },
    "reviews_monitor": {"enabled": False, "known_review_ids": []},
    "ad_templates": [],
    "plugins": {
        "auto_stars": {
            "enabled": False, "amount": 50, "note": "",
            "keyword": "звёзд",       # заголовок заказа должен содержать это слово
            "ask_username": True,      # спрашивать @username в чате заказа
            "pending": {},             # {order_id: {quantity, asked_at}} — ждём username
            "delivered": [],           # order_id, по которым звёзды уже выданы
            "wallet_version": "v4r2",
        },
        "auto_roblox": {"enabled": False, "robux": 0, "note": ""},
        "auto_gifts": {"enabled": False, "gift_type": "", "note": ""},
    },
}


_DEFAULT_ACCOUNT = "Основной"


# ---------------------------------------------------------------------------
# Storage backend: PostgreSQL when DATABASE_URL is set, else JSON files.
# The rest of the module (and the whole bot) is unchanged — only _read_blob /
# _write_blob differ. Each legacy JSON file becomes one row in kv_store, and
# existing files are auto-migrated into the DB on first read.
# ---------------------------------------------------------------------------

import threading as _threading


def _resolve_database_url() -> str:
    """Find a Postgres URL from the common env-var names Railway/Heroku use,
    or assemble one from PG* parts. Returns '' if none configured."""
    for var in ("DATABASE_URL", "DATABASE_PRIVATE_URL", "POSTGRES_URL",
                "POSTGRESQL_URL", "PG_URL", "DATABASE_PUBLIC_URL"):
        url = os.environ.get(var, "").strip()
        if url:
            # psycopg2 accepts postgres:// but normalize to postgresql://
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            return url
    # Assemble from individual PG* variables if present
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

# blob key -> legacy file path (used for migration + file-mode fallback)
_BLOBS = {
    "tokens": _FILE,
    "settings": _SETTINGS_FILE,
    "panel_creds": _PANEL_FILE,
    "fragment_creds": _FRAGMENT_FILE,
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
    """Load a JSON blob (dict) for a storage key from DB or file."""
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
                # one-time migration from a legacy JSON file, if present
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
    """Persist a JSON blob (dict) for a storage key to DB or file."""
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
    """Return the v2 accounts entry for a user, migrating a bare token string."""
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
# Multi-account API
# ---------------------------------------------------------------------------

def get_accounts(user_id: int) -> dict:
    """{name: {"token": ...}} for the user (empty dict if none)."""
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
    """Token of the ACTIVE account (backward-compatible entry point)."""
    entry = _user_entry(_load(), user_id)
    if not entry:
        return None
    active = entry.get("active") or next(iter(entry.get("accounts", {})), "")
    acc = entry.get("accounts", {}).get(active)
    return acc.get("token") if acc else None


def save_token(user_id: int, token: str) -> None:
    """Set the token on the active account (creates the default account)."""
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
    """Remove the active account (logout). Other accounts stay."""
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
    """Per-account storage key: '{uid}::{account}'. Falls back to plain uid."""
    account = get_active_account(user_id)
    return f"{user_id}::{account}" if account else str(user_id)


def get_settings(user_id: int) -> dict:
    all_settings = _load_settings()
    key = _account_key(user_id)
    if key not in all_settings and str(user_id) in all_settings:
        # migrate legacy per-uid settings to the active account's key
        all_settings[key] = all_settings.pop(str(user_id))
        _save_all_settings(all_settings)
    raw = all_settings.get(key, {})
    return _merge_defaults(raw)


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
# Panel credentials (YooMarket seller panel login/password)
# ---------------------------------------------------------------------------

def _load_panel_creds() -> dict:
    return _read_blob("panel_creds")


def _save_panel_data(data: dict) -> None:
    _write_blob("panel_creds", data)


def get_panel_creds(user_id: int) -> dict | None:
    """Panel cookies for the ACTIVE account (per-account, legacy migrated)."""
    data = _load_panel_creds()
    key = _account_key(user_id)
    if key not in data and str(user_id) in data:
        data[key] = data.pop(str(user_id))
        _save_panel_data(data)
    return data.get(key)


def save_panel_creds(user_id: int, creds: dict) -> None:
    """Save panel credentials for the user's active account."""
    data = _load_panel_creds()
    data[_account_key(user_id)] = creds
    _save_panel_data(data)


def delete_panel_creds(user_id: int) -> None:
    """Remove panel credentials for the user's active account."""
    data = _load_panel_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _save_panel_data(data)


# ---------------------------------------------------------------------------
# Fragment credentials (Telegram Stars auto-delivery) — SENSITIVE
# {cookies: {...}, mnemonic: "24 words", wallet_version: "v4r2", api_hash: "..."}
# Stored per active account. Never log the mnemonic or cookie values.
# ---------------------------------------------------------------------------

def _load_fragment_creds() -> dict:
    return _read_blob("fragment_creds")


def _save_fragment_data(data: dict) -> None:
    _write_blob("fragment_creds", data)


def get_fragment_creds(user_id: int) -> dict | None:
    data = _load_fragment_creds()
    key = _account_key(user_id)
    # migrate legacy per-uid entry to the active-account key (like settings/panel)
    if key not in data and str(user_id) in data:
        data[key] = data.pop(str(user_id))
        _save_fragment_data(data)
    return data.get(key)


def save_fragment_creds(user_id: int, creds: dict) -> None:
    data = _load_fragment_creds()
    existing = data.get(_account_key(user_id)) or {}
    existing.update(creds)
    data[_account_key(user_id)] = existing
    _save_fragment_data(data)
    if not _USE_DB:
        try:  # tighten file perms — it holds a seed phrase
            os.chmod(_FRAGMENT_FILE, 0o600)
        except OSError:
            pass


def delete_fragment_creds(user_id: int) -> None:
    data = _load_fragment_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    _save_fragment_data(data)


# ---------------------------------------------------------------------------
# Global admin / subscription store (owner + admins, subscriptions, price,
# blocked users). Not per-account — bot-wide.
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
    """Add `days` to a user's subscription (from now, or extends existing).
    Returns the new expiry timestamp."""
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


# --- Blocked users (bot-wide) ----------------------------------------------

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
# Appearance / branding (bot-wide): custom main-menu button labels and an
# optional custom-emoji header. Buttons can't be truly colored on Telegram,
# so "coloring" = colored emoji in the label.
# ---------------------------------------------------------------------------

# key -> (default label, callback) for the main menu
MENU_BUTTONS = [
    ("ads",      "🚀 Объявления", "menu:ads"),
    ("orders",   "🛒 Заказы",     "menu:orders"),
    ("chats",    "💬 Чаты",       "menu:chats"),
    ("balance",  "💰 Баланс",     "menu:balance"),
    ("stats",    "📊 Статистика", "menu:stats"),
    ("plugins",  "🧩 Плагины",    "plugins:menu"),
    ("settings", "⚙️ Настройки",  "settings:menu"),
]


def get_appearance() -> dict:
    return _load_admin().get("appearance", {})


def get_menu_labels() -> dict:
    """{key: label} with admin overrides applied over defaults."""
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
    """{'id': custom_emoji_id, 'fallback': '🏠'} or None."""
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
    """Header prefix for the main menu: custom emoji if set, else a plain
    emoji if the admin chose one, else the default 🏠."""
    he = get_header_emoji()
    if he:
        fb = he.get("fallback") or "🏠"
        if he.get("id"):
            return f'<tg-emoji emoji-id="{he["id"]}">{fb}</tg-emoji>'
        return fb  # plain emoji chosen by the admin
    return "🏠"


# ---------------------------------------------------------------------------
# Editable bot message texts (bot-wide), with custom-emoji support.
# Stored as ready-to-send HTML (from message.html_text). Placeholders in
# {curly} are substituted at render time.
# ---------------------------------------------------------------------------

CUSTOM_TEXTS = {
    "welcome": {
        "title": "Приветствие /start",
        "vars": [],
        "default": (
            "👋 Добро пожаловать в <b>YooMarket бот</b>!\n\n"
            "Отправьте ваш <b>API токен</b> из панели YooMarket:\n"
            "<i>Мой магазин → Интеграции → API токен</i>"
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
            "🎉 Вам выдана подписка на бота на <b>{days} дн.</b>!\n"
            "Осталось: <b>{left} дн.</b>"
        ),
    },
}


def get_custom_text(key: str) -> str:
    """Stored HTML for a text key, or its default."""
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
    """Render a text with {placeholder} substitution (brace-safe)."""
    tmpl = get_custom_text(key)
    for k, v in subs.items():
        tmpl = tmpl.replace("{" + k + "}", str(v))
    return tmpl
