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

_DEFAULT_SETTINGS = {
    "shop_name": "",
    "auto_reply": {"enabled": False, "message": "Спасибо за заказ! Скоро свяжемся с вами."},
    "auto_events": {
        "on_confirmed": {"enabled": False, "message": "✅ Ваш заказ подтверждён! Спасибо."},
        "on_refunded": {"enabled": False, "message": "↩️ Возврат оформлен. Ожидайте 1-3 дня."},
    },
    "auto_rules": [],  # [{"keyword": "Roblox", "message": "🎮 Робуксы отправим в течение 15 минут!"}]
    "auto_restore": {"enabled": False},
    "auto_bump": {"enabled": False, "interval_hours": 24},
    "auto_withdraw": {"enabled": False, "min_amount": 500},
    "responders": {},  # {"GameName": "message text"} - keyed by ad title/name
    "known_orders": {},  # {order_id: status}
    "known_order_ids": [],
    "known_order_details": {},  # {order_id: {title, buyer, price, chat_id, seen_at}}
    "known_messages": {},  # {order_id: last_msg_id}
    "blacklist": [],  # list of buyer names to suppress notifications for
    "reminders": {"enabled": False, "hours": 24},
    "reminded_orders": [],  # order IDs already reminded (reset on status change)
    "auto_confirm": {"enabled": False, "hours": 24},
    "balance_notify": {"enabled": False, "threshold": 1000, "last_notified_balance": 0.0},
    "daily_report": {"enabled": False, "hour": 20, "last_report_day": ""},
    "quick_replies": ["Спасибо за заказ!", "Отправлю в течение часа.", "Уточните, пожалуйста."],
    "buyer_notes": {},
    "bump_schedule": {"enabled": False, "times": [], "last_runs": {}},
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


def _load() -> dict:
    if os.path.exists(_FILE):
        with open(_FILE) as f:
            return json.load(f)
    return {}


def _save_tokens(data: dict) -> None:
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w") as f:
        json.dump(data, f)


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
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w") as f:
        json.dump(data, f)


def _load_settings() -> dict:
    if os.path.exists(_SETTINGS_FILE):
        with open(_SETTINGS_FILE) as f:
            return json.load(f)
    return {}


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
        os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(all_settings, f)
    raw = all_settings.get(key, {})
    return _merge_defaults(raw)


def save_settings(user_id: int, settings: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    all_settings = _load_settings()
    all_settings[_account_key(user_id)] = settings
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(all_settings, f)


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
    if os.path.exists(_PANEL_FILE):
        with open(_PANEL_FILE) as f:
            return json.load(f)
    return {}


def get_panel_creds(user_id: int) -> dict | None:
    """Panel cookies for the ACTIVE account (per-account, legacy migrated)."""
    data = _load_panel_creds()
    key = _account_key(user_id)
    if key not in data and str(user_id) in data:
        data[key] = data.pop(str(user_id))
        os.makedirs(os.path.dirname(_PANEL_FILE), exist_ok=True)
        with open(_PANEL_FILE, "w") as f:
            json.dump(data, f)
    return data.get(key)


def save_panel_creds(user_id: int, creds: dict) -> None:
    """Save panel credentials for the user's active account."""
    os.makedirs(os.path.dirname(_PANEL_FILE), exist_ok=True)
    data = _load_panel_creds()
    data[_account_key(user_id)] = creds
    with open(_PANEL_FILE, "w") as f:
        json.dump(data, f)


def delete_panel_creds(user_id: int) -> None:
    """Remove panel credentials for the user's active account."""
    data = _load_panel_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    os.makedirs(os.path.dirname(_PANEL_FILE), exist_ok=True)
    with open(_PANEL_FILE, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Fragment credentials (Telegram Stars auto-delivery) — SENSITIVE
# {cookies: {...}, mnemonic: "24 words", wallet_version: "v4r2", api_hash: "..."}
# Stored per active account. Never log the mnemonic or cookie values.
# ---------------------------------------------------------------------------

def _load_fragment_creds() -> dict:
    if os.path.exists(_FRAGMENT_FILE):
        with open(_FRAGMENT_FILE) as f:
            return json.load(f)
    return {}


def get_fragment_creds(user_id: int) -> dict | None:
    data = _load_fragment_creds()
    key = _account_key(user_id)
    # migrate legacy per-uid entry to the active-account key (like settings/panel)
    if key not in data and str(user_id) in data:
        data[key] = data.pop(str(user_id))
        os.makedirs(os.path.dirname(_FRAGMENT_FILE), exist_ok=True)
        with open(_FRAGMENT_FILE, "w") as f:
            json.dump(data, f)
        try:
            os.chmod(_FRAGMENT_FILE, 0o600)
        except OSError:
            pass
    return data.get(key)


def save_fragment_creds(user_id: int, creds: dict) -> None:
    os.makedirs(os.path.dirname(_FRAGMENT_FILE), exist_ok=True)
    data = _load_fragment_creds()
    existing = data.get(_account_key(user_id)) or {}
    existing.update(creds)
    data[_account_key(user_id)] = existing
    with open(_FRAGMENT_FILE, "w") as f:
        json.dump(data, f)
    try:  # tighten file perms — it holds a seed phrase
        os.chmod(_FRAGMENT_FILE, 0o600)
    except OSError:
        pass


def delete_fragment_creds(user_id: int) -> None:
    data = _load_fragment_creds()
    data.pop(_account_key(user_id), None)
    data.pop(str(user_id), None)
    os.makedirs(os.path.dirname(_FRAGMENT_FILE), exist_ok=True)
    with open(_FRAGMENT_FILE, "w") as f:
        json.dump(data, f)
