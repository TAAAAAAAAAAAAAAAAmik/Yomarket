import json
import os

_FILE = os.path.join(os.path.dirname(__file__), "data", "tokens.json")


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
