"""Состояние выдачи. Пережить перезапуск — обязательное требование.

Обрыв случается чаще всего не от сбоя, а от обычного выката: контейнер
перезапускается между «покупаем» и «выдан». После этого статус заказа
больше не меняется никогда — значит по нему уже ничего не сработает само:
деньги потрачены, покупатель без кода, продавец без единого слова.

Поэтому намерение записывается ДО вызова поставщика, а не после.

Запись атомарна (`.tmp` + `os.replace`): обычный `open(path, "w")` обнуляет
файл до записи, и процесс, убитый посередине, оставляет обрезанный JSON —
то есть не «часть журнала», а ноль.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Protocol


# Состояния записи. Терминальные — «выдан» и любой отказ; всё остальное
# подбирается возобновлением.
STATE_NEW = "собираемся покупать"
STATE_BUYING = "покупаем"
STATE_WAIT_CODE = "куплен, ждём код"
STATE_SENDING = "куплен, отправляем"
STATE_SEND_FAILED = "куплен, отправить не смогли"
STATE_DONE = "выдан"

# Что подбирает `resume_unfinished`. Ключевое: сюда входит «куплен, отправить
# не смогли» — код уже оплачен, и бросить его нельзя.
UNFINISHED = (STATE_NEW, STATE_BUYING, STATE_WAIT_CODE, STATE_SENDING,
              STATE_SEND_FAILED)

LOG_MAX = 200          # записей в журнале
DELIVERED_MAX = 500    # номеров выданных заказов


class Store(Protocol):
    """Что движку нужно от хранилища."""

    def conf(self, card_slug: str) -> dict: ...
    def save(self) -> None: ...


class JsonStore:
    """Простейшее хранилище: один JSON-файл на продавца.

    Для боевой работы замените на свою базу — движку важен только
    интерфейс. Но атомарную запись сохраните в любом случае.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (ValueError, OSError):
                # Битый файл не роняет выдачу, но и не выдаётся за пустой:
                # молча начав с нуля, мы бы купили всё заново.
                raise RuntimeError(
                    f"{path} не читается. Это журнал выдач: в нём номера "
                    f"уже купленных заказов. Начав с пустого, бот купит их "
                    f"второй раз. Восстановите файл из копии.")

    def conf(self, card_slug: str) -> dict:
        """Настройки и журнал одного вида товара."""
        cards = self.data.setdefault("cards", {})
        return cards.setdefault(card_slug, {
            "enabled": False,
            "keyword": "",
            "region": "",          # запасной, если в описании нет
            "greeting": "",        # что написать покупателю до кода
            "note": "",            # что дописать к коду
            "delivered": [],       # номера заказов, по которым код ушёл
            "log": [],             # записи выдач, новые в начале
            "force": [],           # ручная очередь: выдать эти заказы
        })

    def save(self) -> None:
        with self._lock:
            for conf in self.data.get("cards", {}).values():
                del conf.setdefault("log", [])[LOG_MAX:]
                del conf.setdefault("delivered", [])[:-DELIVERED_MAX]
            folder = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise


def find_entry(conf: dict, order_id: str) -> dict | None:
    """Запись журнала по номеру заказа.

    Заказ уже в журнале — значит вызов поставщика МОГ уйти. Повторять его
    можно только той же ссылкой, а заводить вторую запись нельзя: по ней
    потом не понять, что покупали.
    """
    for entry in conf.get("log") or []:
        if isinstance(entry, dict) and str(entry.get("order")) == str(order_id):
            return entry
    return None
