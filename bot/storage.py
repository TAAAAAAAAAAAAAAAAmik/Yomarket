import json
import os

_FILE = os.path.join(os.path.dirname(__file__), "data", "tokens.json")
_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "data", "settings.json")

_DEFAULT_SETTINGS = {
    "auto_reply": {"enabled": False, "message": "Спасибо за заказ! Скоро свяжемся с вами."},
    "auto_restore": {"enabled": False},
    "auto_bump": {"enabled": False, "interval_hours": 24},
    "auto_withdraw": {"enabled": False, "min_amount": 500},
    "known_order_ids": [],
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
    """Deep-merge user settings over defaults so new keys always appear."""
    import copy
    result = copy.deepcopy(_DEFAULT_SETTINGS)
    for key, val in settings.items():
        if key == "plugins" and isinstance(val, dict):
            for pkey, pval in val.items():
                if pkey in result["plugins"] and isinstance(pval, dict):
                    result["plugins"][pkey].update(pval)
                else:
                    result["plugins"][pkey] = pval
        elif isinstance(val, dict) and key in result and isinstance(result[key], dict):
            result[key].update(val)
        else:
            result[key] = val
    return result


def get_settings(user_id: int) -> dict:
    """Returns user settings dict with defaults."""
    all_settings = _load_settings()
    raw = all_settings.get(str(user_id), {})
    return _merge_defaults(raw)


def save_settings(user_id: int, settings: dict) -> None:
    """Saves user settings."""
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    all_settings = _load_settings()
    all_settings[str(user_id)] = settings
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(all_settings, f)


def get_all_users() -> list[int]:
    """Returns all user IDs that have tokens saved."""
    return [int(uid) for uid in _load().keys()]
