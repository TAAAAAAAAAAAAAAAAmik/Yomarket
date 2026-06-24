import json
import os
import shutil

# Data directory: prefer DATA_DIR env var, otherwise ~/.yomarket/ (survives git updates/reclones).
# Falls back to legacy bot/data/ so existing installations keep working until migrated.
_DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.expanduser("~"), ".yomarket")
_LEGACY_DIR = os.path.join(os.path.dirname(__file__), "data")

def _migrate_legacy() -> None:
    """Move bot/data/*.json to ~/.yomarket/ once, silently."""
    if not os.path.isdir(_LEGACY_DIR):
        return
    os.makedirs(_DATA_DIR, exist_ok=True)
    for fname in ("tokens.json", "settings.json", "panel_creds.json"):
        src = os.path.join(_LEGACY_DIR, fname)
        dst = os.path.join(_DATA_DIR, fname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)

_migrate_legacy()

_FILE = os.path.join(_DATA_DIR, "tokens.json")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")
_PANEL_FILE = os.path.join(_DATA_DIR, "panel_creds.json")

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
    "reviews_monitor": {"enabled": False, "known_review_ids": []},
    "ad_templates": [],
    "plugins": {
        "auto_stars": {"enabled": False, "amount": 50, "note": ""},
        "auto_roblox": {"enabled": False, "robux": 0, "note": ""},
        "auto_gifts": {"enabled": False, "gift_type": "", "note": ""},
    },
}


def _load() -> dict:
    if os.path.exists(_FILE):
        with open(_FILE) as f:
            return json.load(f)
    return {}


def get_token(user_id: int) -> str | None:
    return _load().get(str(user_id))


def save_token(user_id: int, token: str) -> None:
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    data = _load()
    data[str(user_id)] = token
    with open(_FILE, "w") as f:
        json.dump(data, f)


def delete_token(user_id: int) -> None:
    data = _load()
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


def get_settings(user_id: int) -> dict:
    all_settings = _load_settings()
    raw = all_settings.get(str(user_id), {})
    return _merge_defaults(raw)


def save_settings(user_id: int, settings: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    all_settings = _load_settings()
    all_settings[str(user_id)] = settings
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
    """Return {"login": "...", "password": "..."} for the user, or None."""
    return _load_panel_creds().get(str(user_id))


def save_panel_creds(user_id: int, creds: dict) -> None:
    """Save panel credentials for the user."""
    os.makedirs(os.path.dirname(_PANEL_FILE), exist_ok=True)
    data = _load_panel_creds()
    data[str(user_id)] = creds
    with open(_PANEL_FILE, "w") as f:
        json.dump(data, f)


def delete_panel_creds(user_id: int) -> None:
    """Remove panel credentials for the user."""
    data = _load_panel_creds()
    data.pop(str(user_id), None)
    os.makedirs(os.path.dirname(_PANEL_FILE), exist_ok=True)
    with open(_PANEL_FILE, "w") as f:
        json.dump(data, f)
