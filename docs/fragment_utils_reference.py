import json
import base64
import requests
import traceback
import binascii
from decimal import Decimal
from pathlib import Path
import logging
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import Address, to_nano
from tonsdk.boc import Cell
from tonsdk.utils import to_nano
from tonsdk.boc import Cell
from tonsdk.utils import Address
FRAGMENT_API_URL = "https://fragment.com/api"
TONCENTER_URL = "https://toncenter.com/api/v2/sendBoc"
SEQNO_CHECK_INTERVAL = 3


def load_config(path="config.json"):
    """Загружает конфигурационный файл."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Файл {path} не найден!")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def make_session(cookies_dict):
    """Создаёт сессию с куками."""
    s = requests.Session()
    s.cookies.update(cookies_dict)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

import base64
import binascii
from tonsdk.boc import Cell

def fix_base64(s: str) -> str:
    """
    Исправляет Base64 строку:
    - Меняет '-' → '+' и '_' → '/'
    - Добавляет '=' для устранения ошибки 'Incorrect padding'.
    """
    s = s.replace('-', '+').replace('_', '/')
    return s + ('=' * (-len(s) % 4))



def send_boc_to_toncenter(boc_b64):
    """Отправляет BOC в TonCenter."""
    headers = {"Content-Type": "application/json"}
    data = {"boc": boc_b64}

    try:
        response = requests.post(TONCENTER_URL, headers=headers, json=data)
        response.raise_for_status()
        print("✅ Успешная отправка в TonCenter!", response.json())
        return response.json()

    except requests.exceptions.HTTPError as e:
        print(f"❌ Ошибка при отправке BOC: {e}")
        
        # 🔥 Логируем ответ сервера (если есть)
        if response.content:
            try:
                error_data = response.json()
                print("❌ Детали ошибки:", json.dumps(error_data, indent=2, ensure_ascii=False))
            except Exception:
                print("❌ Ошибка сервера (не JSON):", response.text)

        return None


def confirm_req(session, req_id, boc_b64):
    """
    Подтверждает транзакцию в Fragment API.
    """
    account_data = json.dumps({
        "address": "0:a3f4f00afedd68f48f1fe68e614d5ae2fd2455d953fc475992b1fd0964d6545d",
        "chain": "-239"
    })
    
    params = {
        "method": "confirmReq",
        "hash": "af142ec36cafbbfa89",
        "id": req_id,
        "boc": boc_b64,
        "account": account_data
    }
    
    try:
        response = session.post(FRAGMENT_API_URL, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка при confirmReq: {e}")
        return None



def extract_comment_from_payload(payload_b64):
    """
    Расшифровывает комментарий из payload, пытаясь декодировать как BOC.
    """
    try:
        # Фиксим Base64 перед декодированием
        payload_b64_fixed = fix_base64(payload_b64)
        payload_bytes = base64.b64decode(payload_b64_fixed)  # Декодируем Base64

        try:
            # Попытка декодировать как BOC (TON Cell)
            payload_cell = Cell.one_from_boc(payload_bytes)
            payload_text = payload_cell.bits.get_top_upped_array().decode("utf-8")  # Извлекаем текст
            payload_text = payload_text.lstrip("\x00")  # Убираем незначащие байты
            payload_text = " ".join(payload_text.split())  # Убираем лишние пробелы
            print(f"✅ Найден комментарий в BOC: {repr(payload_text)}")
            return payload_text

        except Exception as e:
            print(f"⚠️ Не удалось декодировать BOC: {e}. Пробуем просто как текст...")

            # Если BOC-декодирование не удалось, пробуем обычный UTF-8
            payload_text = payload_bytes.decode("utf-8").strip()
            return payload_text

    except (binascii.Error, UnicodeDecodeError) as e:
        print(f"❌ Ошибка при расшифровке payload: {e}")
        return None



from tonsdk.boc import Cell

def build_boc_with_comment(recipient, amount_nano, payload_b64, seqno, wallet_obj):
    amount_ton = Decimal(amount_nano) / Decimal(1e9)

    payload_bytes = base64.b64decode(fix_base64(payload_b64))
    payload_cell = Cell.one_from_boc(payload_bytes)
    payload_text = payload_cell.bits.get_top_upped_array().decode("utf-8", errors="ignore").strip()

    comment_cell = Cell()
    comment_cell.bits.write_uint(0, 32)
    comment_cell.bits.write_bytes(payload_text.encode("utf-8"))

    transfer_msg = wallet_obj.create_transfer_message(
        to_addr=Address(recipient),
        amount=to_nano(str(amount_ton), "ton"),
        seqno=seqno,
        payload=comment_cell,
        send_mode=3
    )

    boc_bytes = transfer_msg["message"].to_boc(False)
    boc_b64 = base64.b64encode(boc_bytes).decode()

    return boc_b64

def search_stars_recipient(session, username):
    """
    Ищет получателя Stars по Telegram username через Fragment API.
    """
    try:
        r = session.post(
            FRAGMENT_API_URL,
            params={
                "method": "searchStarsRecipient",
                "hash": "af142ec36cafbbfa89",
                "query": username
            }
        )
        r.raise_for_status()
        return r.json()
    
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка при запросе searchStarsRecipient: {e}")
        return {"ok": False, "error": "Ошибка сети"}

def init_buy_stars_request(session, recipient, quantity):
    """Инициирует покупку Stars для указанного получателя через Fragment API."""
    try:
        # Логирование данных, которые отправляются в запрос
        logging.info(f"Инициализация запроса на покупку {quantity} Stars для {recipient}")
        
        r = session.post(
            FRAGMENT_API_URL,
            params={
                "method": "initBuyStarsRequest",
                "hash": "af142ec36cafbbfa89",
                "recipient": recipient,
                "quantity": quantity
            }
        )
        r.raise_for_status()
        response = r.json()

        # Логирование ответа API
        logging.info(f"Ответ от API: {response}")

        return response

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Ошибка при запросе initBuyStarsRequest: {e}")
        return {"ok": False, "error": "Ошибка сети"}


def get_buy_stars_link(session, req_id, raw_hex_addr, chain_str):
    """
    Получает ссылку для покупки Stars через Fragment API.
    """
    try:
        account_data = json.dumps({"address": raw_hex_addr, "chain": chain_str})
        device_data  = json.dumps({
            "platform": "browser",
            "appName": "telegram-wallet",
            "appVersion": "1",
            "maxProtocolVersion": 2,
            "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}]
        })

        r = session.post(
            FRAGMENT_API_URL,
            params={
                "method": "getBuyStarsLink",
                "hash": "af142ec36cafbbfa89",
                "id": req_id,
                "transaction": 1,
                "show_sender": 1,
                "account": account_data,
                "device": device_data
            }
        )
        r.raise_for_status()
        return r.json()
    
    except requests.exceptions.RequestException as e:
        print(f"❌ Ошибка при запросе getBuyStarsLink: {e}")
        return {"ok": False, "error": "Ошибка сети"}

def get_seqno(wallet_address):
    url = "https://toncenter.com/api/v2/runGetMethod"
    data = {"address": wallet_address, "method": "seqno", "stack": []}

    response = requests.post(url, json=data)
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise Exception(f"Ошибка при запросе seqno: {result}")

    seqno_hex = result["result"]["stack"][0][1]
    return int(seqno_hex, 16)

def wait_for_seqno(wallet_address, current_seqno):
    """Ожидание пока seqno увеличится (пока транзакция не подтвердится)."""
    while True:
        new_seqno = get_seqno(wallet_address)
        if new_seqno > current_seqno:
            print(f"✅ Транзакция подтверждена! Новый seqno: {new_seqno}")
            return new_seqno
        print("⌛ Ожидание подтверждения транзакции...")
        time.sleep(SEQNO_CHECK_INTERVAL)

import time

import queue
import time

order_queue = queue.Queue()  # Очередь заказов
