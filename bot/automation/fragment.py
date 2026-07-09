"""Telegram Stars auto-delivery via Fragment (fragment.com) + TON wallet.

Flow (all blocking — call through loop.run_in_executor):
  1. searchStarsRecipient(username)   → recipient id
  2. initBuyStarsRequest(recipient, quantity) → req_id
  3. derive TON wallet from mnemonic  → address
  4. getBuyStarsLink(req_id, address) → transaction messages
  5. for each message: build signed BOC, send to TonCenter, confirmReq
  6. wait for the wallet seqno to advance = on-chain confirmation

Secrets (Fragment cookies, wallet mnemonic) are passed in by the caller and
never logged. Based on the seller-provided fragment_utils API sample.
"""
from __future__ import annotations

import base64
import logging
import time
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)

FRAGMENT_API_URL = "https://fragment.com/api"
TONCENTER_SEND = "https://toncenter.com/api/v2/sendBoc"
TONCENTER_RUN = "https://toncenter.com/api/v2/runGetMethod"
MAINNET_CHAIN = "-239"
DEFAULT_HASH = "af142ec36cafbbfa89"
SEQNO_POLL_SECS = 3
SEQNO_MAX_WAIT_SECS = 120


def _fix_base64(s: str) -> str:
    s = s.replace("-", "+").replace("_", "/")
    return s + ("=" * (-len(s) % 4))


def _make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.cookies.update(cookies or {})
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://fragment.com",
        "Referer": "https://fragment.com/stars",
    })
    return s


def _extract_recipient(resp: dict) -> str:
    """Pull the recipient id out of a searchStarsRecipient response."""
    if not isinstance(resp, dict):
        return ""
    found = resp.get("found")
    if isinstance(found, dict):
        for k in ("recipient", "id", "myself", "value"):
            if found.get(k):
                return str(found[k])
    for k in ("recipient", "id"):
        if resp.get(k):
            return str(resp[k])
    return ""


def _wallet_from_mnemonic(mnemonic: str, version: str):
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    words = mnemonic.split()
    try:
        ver = WalletVersionEnum(version)
    except ValueError:
        ver = WalletVersionEnum.v4r2
    _m, _pub, _priv, wallet = Wallets.from_mnemonics(words, ver, workchain=0)
    return wallet


def _build_signed_boc(wallet, to_addr: str, amount_nano, payload_b64: str, seqno: int) -> str:
    """Build and sign a transfer carrying Fragment's payload cell."""
    from tonsdk.utils import Address, to_nano
    from tonsdk.boc import Cell

    amount_ton = Decimal(int(amount_nano)) / Decimal(1_000_000_000)

    payload_cell = None
    if payload_b64:
        try:
            payload_cell = Cell.one_from_boc(base64.b64decode(_fix_base64(payload_b64)))
        except Exception as e:
            logger.warning("payload decode failed, sending without payload: %s", e)
            payload_cell = None

    transfer = wallet.create_transfer_message(
        to_addr=Address(to_addr),
        amount=to_nano(str(amount_ton), "ton"),
        seqno=seqno,
        payload=payload_cell,
        send_mode=3,
    )
    boc_bytes = transfer["message"].to_boc(False)
    return base64.b64encode(boc_bytes).decode()


def _get_seqno(address_str: str) -> int:
    r = requests.post(TONCENTER_RUN,
                      json={"address": address_str, "method": "seqno", "stack": []},
                      timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        # Fresh (undeployed) wallet → seqno 0
        return 0
    try:
        return int(data["result"]["stack"][0][1], 16)
    except (KeyError, IndexError, ValueError):
        return 0


def _send_boc(boc_b64: str) -> bool:
    try:
        r = requests.post(TONCENTER_SEND, json={"boc": boc_b64}, timeout=20)
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception as e:
        logger.error("sendBoc failed: %s", e)
        return False


def get_wallet_balance_sync(
    mnemonic: str, wallet_version: str = "v4r2",
) -> tuple[bool, object]:
    """Return (True, {"ton": float, "nano": int, "address": str}) or (False, err).
    Blocking — run in an executor."""
    if not mnemonic or len(mnemonic.split()) < 12:
        return False, "Не настроена seed-фраза кошелька"
    try:
        wallet = _wallet_from_mnemonic(mnemonic, wallet_version)
        address = wallet.address.to_string(True, True, True)
    except Exception as e:
        return False, f"Ошибка кошелька: {str(e)[:80]}"
    try:
        r = requests.get(
            "https://toncenter.com/api/v2/getAddressBalance",
            params={"address": address}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"TonCenter недоступен: {str(e)[:80]}"
    if not data.get("ok"):
        return False, f"TonCenter: {str(data)[:100]}"
    try:
        nano = int(data["result"])
    except (KeyError, ValueError, TypeError):
        nano = 0
    return True, {"ton": nano / 1_000_000_000, "nano": nano, "address": address}


def buy_stars_sync(
    cookies: dict,
    mnemonic: str,
    username: str,
    quantity: int,
    wallet_version: str = "v4r2",
    api_hash: str = DEFAULT_HASH,
    wait_confirm: bool = True,
) -> tuple[bool, str]:
    """
    Buy `quantity` Telegram Stars for `username`. Returns (ok, human_message).
    Blocking — run in an executor. Secrets are never logged.
    """
    username = (username or "").strip().lstrip("@")
    if not username:
        return False, "Пустой username"
    if not cookies:
        return False, "Не настроены cookies Fragment"
    if not mnemonic or len(mnemonic.split()) < 12:
        return False, "Не настроена seed-фраза TON-кошелька (нужно 24 слова)"
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return False, "Некорректное количество звёзд"
    if quantity < 50:
        return False, "Fragment принимает заказы от 50 звёзд"

    session = _make_session(cookies)

    def _post(method: str, extra: dict) -> dict:
        params = {"method": method, "hash": api_hash, **extra}
        try:
            r = session.post(FRAGMENT_API_URL, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"сеть: {str(e)[:80]}"}
        except ValueError:
            return {"ok": False, "error": "не JSON от Fragment"}

    # 1. find recipient
    search = _post("searchStarsRecipient", {"query": username})
    recipient = _extract_recipient(search)
    if not recipient:
        err = search.get("error") or search.get("error_message") or "получатель не найден"
        return False, f"@{username}: {err}. Проверьте username и cookies."

    # 2. init request
    init = _post("initBuyStarsRequest", {"recipient": recipient, "quantity": quantity})
    req_id = init.get("req_id") or init.get("id")
    if not req_id:
        err = init.get("error") or init.get("error_message") or str(init)[:120]
        return False, f"initBuyStarsRequest не дал req_id: {err}"

    # 3. wallet
    try:
        wallet = _wallet_from_mnemonic(mnemonic, wallet_version)
        raw_addr = wallet.address.to_string(False, False, False)      # 0:hex
        bounce_addr = wallet.address.to_string(True, True, True)      # EQ...
    except Exception as e:
        return False, f"Ошибка кошелька (проверьте seed-фразу): {str(e)[:80]}"

    # 4. transaction link
    link = _post("getBuyStarsLink", {
        "id": req_id, "transaction": 1, "show_sender": 1,
        "account": _json_account(raw_addr),
        "device": _json_device(),
    })
    messages = _extract_messages(link)
    if not messages:
        err = link.get("error") or link.get("error_message") or str(link)[:150]
        return False, f"getBuyStarsLink не дал транзакцию: {err}"

    # 5. sign + send each message, confirm
    try:
        seqno = _get_seqno(bounce_addr)
    except Exception as e:
        return False, f"Не удалось получить seqno кошелька: {str(e)[:80]}"

    valid_msgs = [m for m in messages
                  if m.get("address") and m.get("amount") is not None]
    if not valid_msgs:
        return False, "Fragment не вернул ни одного платёжного сообщения"

    # Each external message needs the previous one confirmed on-chain before the
    # next seqno is accepted, so send sequentially and wait for seqno to advance
    # between messages (a Stars purchase is normally a single message).
    sent = 0
    for i, msg in enumerate(valid_msgs):
        try:
            boc = _build_signed_boc(
                wallet, msg["address"], msg["amount"], msg.get("payload", ""), seqno)
        except Exception as e:
            if sent:
                break
            return False, f"Ошибка сборки транзакции: {str(e)[:100]}"
        if not _send_boc(boc):
            if sent:
                break
            return False, "TonCenter отклонил транзакцию (проверьте баланс кошелька)"
        _post("confirmReq", {"id": req_id, "boc": boc,
                             "account": _json_account(raw_addr)})
        sent += 1
        # Wait for this tx to land (seqno advances) before sending the next
        if wait_confirm or i < len(valid_msgs) - 1:
            confirmed = _wait_seqno_advance(bounce_addr, seqno)
            if confirmed:
                seqno += 1
            elif i < len(valid_msgs) - 1:
                # can't safely send the next message without confirmation
                return True, (f"⏳ {quantity}⭐ для @{username}: часть транзакций "
                              "отправлена, подтверждение в сети ещё идёт")

    if not sent:
        return False, "Не удалось отправить ни одной транзакции"
    if wait_confirm:
        return True, f"✅ {quantity}⭐ отправлены на @{username} (подтверждено в TON)"
    return True, f"✅ {quantity}⭐ отправлены на @{username}"


def _wait_seqno_advance(address: str, from_seqno: int) -> bool:
    """Poll until wallet seqno exceeds from_seqno. Returns True if advanced."""
    deadline = time.time() + SEQNO_MAX_WAIT_SECS
    while time.time() < deadline:
        try:
            if _get_seqno(address) > from_seqno:
                return True
        except Exception:
            pass
        time.sleep(SEQNO_POLL_SECS)
    return False


def _json_account(raw_addr: str) -> str:
    import json
    return json.dumps({"address": raw_addr, "chain": MAINNET_CHAIN})


def _json_device() -> str:
    import json
    return json.dumps({
        "platform": "browser",
        "appName": "telegram-wallet",
        "appVersion": "1",
        "maxProtocolVersion": 2,
        "features": ["SendTransaction",
                     {"name": "SendTransaction", "maxMessages": 4}],
    })


def _extract_messages(link: dict) -> list[dict]:
    """Pull transaction messages out of a getBuyStarsLink response."""
    if not isinstance(link, dict):
        return []
    tx = link.get("transaction")
    if isinstance(tx, dict) and isinstance(tx.get("messages"), list):
        return [m for m in tx["messages"] if isinstance(m, dict)]
    if isinstance(link.get("messages"), list):
        return [m for m in link["messages"] if isinstance(m, dict)]
    return []


def check_fragment_session_sync(cookies: dict) -> tuple[bool, str]:
    """Light check that the Fragment cookies are alive."""
    if not cookies:
        return False, "cookies не заданы"
    session = _make_session(cookies)
    try:
        r = session.post(FRAGMENT_API_URL,
                         params={"method": "searchStarsRecipient",
                                 "hash": DEFAULT_HASH, "query": "durov"},
                         timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok") or data.get("found"):
                return True, "cookies работают"
            return False, f"Fragment ответил: {str(data)[:100]}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"ошибка: {str(e)[:80]}"
