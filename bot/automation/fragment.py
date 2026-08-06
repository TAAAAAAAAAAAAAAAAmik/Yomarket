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
import re
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


def _api_call(session: requests.Session, api_hash: str, method: str,
              extra: dict):
    """Один запрос к Fragment API.

    Хеш — в строке запроса (`/api?hash=…`), метод и аргументы — в теле,
    form-urlencoded. Fragment читает их именно оттуда, и это не мелочь: пока
    всё уходило в строку запроса, хеш он видел, а `query` — нет, и на живой
    ник отвечал «Please enter a username assigned to a user». Поиск получателя
    — первый шаг покупки, так что дальше него дело не доходило никогда.
    """
    return session.post(FRAGMENT_API_URL,
                        params={"hash": api_hash},
                        data={"method": method, **extra},
                        timeout=20)


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
    report: dict | None = None,
) -> tuple[bool, str]:
    """
    Buy `quantity` Telegram Stars for `username`. Returns (ok, human_message).
    Blocking — run in an executor. Secrets are never logged.

    `report` собирает то, что иначе теряется в тексте: сколько TON реально
    ушло. Без этого «прибыль» считалась бы по курсу из головы, а не по
    сумме, которую подписал кошелёк.
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
    state = {"hash": api_hash or DEFAULT_HASH, "refreshed": False}

    def _raw(method: str, extra: dict) -> dict:
        try:
            r = _api_call(session, state["hash"], method, extra)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"сеть: {str(e)[:80]}"}
        except ValueError:
            return {"ok": False, "error": "не JSON от Fragment"}

    def _post(method: str, extra: dict) -> dict:
        """One API call, retried once with a fresh hash if the old one is stale.

        Fragment issues a hash per session and answers a foreign one with «Bad
        request». Left unhandled, that turned every delivery into a failure the
        seller could do nothing about — the hash is not something they were
        ever asked for.
        """
        out = _raw(method, extra)
        said = str(out.get("error") or "").lower()
        if "bad request" in said and not state["refreshed"]:
            state["refreshed"] = True
            fresh = fetch_api_hash_sync(cookies)
            if fresh and fresh != state["hash"]:
                state["hash"] = fresh
                logger.info("Fragment: refreshed api hash mid-flight")
                return _raw(method, extra)
        return out

    # 1. find recipient
    # quantity здесь не лишний: форма поиска на Fragment отправляет его вместе
    # с ником, и без него ответ приходит не тот.
    search = _post("searchStarsRecipient", {"query": username,
                                            "quantity": quantity})
    recipient = _extract_recipient(search)
    if not recipient:
        err = search.get("error") or search.get("error_message") or "получатель не найден"
        return False, f"@{username}: {err}. Проверьте username и cookies."

    # 2. init request
    init = _post("initBuyStarsRequest", {"recipient": recipient,
                                         "quantity": quantity})
    req_id = init.get("req_id") or init.get("id")
    if not req_id:
        err = str(init.get("error") or init.get("error_message")
                  or str(init)[:120])
        if "access denied" in err.lower():
            # Получателя Fragment нашёл — значит сессия жива. Отказ именно на
            # покупке: её разрешает не вход через Telegram, а привязанный
            # TON-кошелёк. Голое «Access denied» продавцу ничего не говорит.
            return False, ("Fragment: «Access denied» — сессии не разрешена "
                           "покупка. Обычно это значит, что к аккаунту "
                           "Fragment не подключён TON-кошелёк, либо подключён "
                           "не тот, чья seed-фраза задана боту. Проверьте "
                           "сверку кошельков: AutoStars → 🔑 Данные Fragment "
                           "→ 🧪 Проверить вход.")
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
        if report is not None:
            try:
                report["nano"] = int(report.get("nano", 0)) + int(msg["amount"])
                report["ton"] = report["nano"] / 1_000_000_000
            except (TypeError, ValueError):
                pass
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


def _page_session(cookies: dict) -> requests.Session:
    """Сессия для обычной страницы, а не для API.

    У API-сессии стоит `X-Requested-With: XMLHttpRequest` — с ним Fragment
    отвечает как на XHR, и разметки со скриптами в ответе может не оказаться.
    А хеш лежит именно в скриптах страницы.
    """
    s = requests.Session()
    s.cookies.update(cookies or {})
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
    })
    return s


_HASH_PATTERNS = (
    r'ajInit\(\s*\{[^{}]*?"hash"\s*:\s*"([0-9a-zA-Z]{8,64})"',
    r'api\?hash=([0-9a-zA-Z]{8,64})',
    r'"apiHash"\s*:\s*"([0-9a-zA-Z]{8,64})"',
    r'"hash"\s*:\s*"([0-9a-zA-Z]{8,64})"',
    r"hash['\"]?\s*[:=]\s*['\"]([0-9a-f]{12,64})['\"]",
)

_HASH_PAGES = ("https://fragment.com/stars",
               "https://fragment.com/stars/buy",
               "https://fragment.com/",
               "https://fragment.com/premium")


def collect_api_hashes_sync(cookies: dict, report: list | None = None,
                            facts: dict | None = None) -> list[str]:
    """Все хеши, какие видны на страницах Fragment, — в порядке доверия.

    Раньше брался первый совпавший, и этого не хватило: в разметке лежит не
    один «hash», и подойти к API может не тот, что нашёлся первым. Перебрать
    несколько дешевле, чем гадать.

    `facts` — то, что вызывающему нужно решать, а не показывать: вошли ли мы.
    Вычитывать это обратно из текста отчёта значило бы управлять логикой по
    прозе, а она меняется от любой правки формулировки.
    """
    session = _page_session(cookies or {})
    found: list[str] = []
    for url in _HASH_PAGES:
        try:
            r = session.get(url, timeout=20)
        except Exception as e:
            if report is not None:
                report.append(f"{url}: {str(e)[:60]}")
            continue
        body = r.text or ""
        if report is not None:
            report.append(f"{url}: HTTP {r.status_code}, {len(body)} символов")
            for line in page_signals(body):
                report.append(f"  · {line}")
        if facts is not None and "signed_in" not in facts:
            facts["signed_in"] = not _looks_logged_out(body)
        if r.status_code != 200:
            continue
        for pattern in _HASH_PATTERNS:
            for m in re.finditer(pattern, body):
                h = m.group(1)
                if h and h not in found:
                    found.append(h)
        if found and report is not None:
            report.append(f"кандидатов в хеш на этой странице: {len(found)}")
        if found:
            break
    return found


def fetch_api_hash_sync(cookies: dict, report: list | None = None) -> str:
    """The api hash Fragment issued to this session, read off its own page.

    Fragment stamps every request with a per-session hash, and a hash from
    somebody else's session is answered with «Bad request» — which is what a
    hardcoded one produced. It sits in the page's own JavaScript, so there is
    no reason to make a seller find it by hand, let alone open developer tools
    on a phone.
    """
    got = collect_api_hashes_sync(cookies, report)
    return got[0] if got else ""


# Адрес TON-кошелька на странице Fragment. Покупку разрешает не вход через
# Telegram, а привязанный кошелёк, и его адрес виден в разметке. Границу
# слова в конце не ставим: адрес может заканчиваться на «-» или «_», и \b
# после них не срабатывает.
_ADDR_RE = re.compile(r"(?<![A-Za-z0-9_-])([EU]Q[A-Za-z0-9_-]{46})")
_RAW_ADDR_RE = re.compile(r"(?<![0-9a-fA-F:])(0:[0-9a-fA-F]{64})")


def wallet_on_page_sync(cookies: dict, report: list | None = None) -> str:
    """Какой TON-кошелёк Fragment считает привязанным к этой сессии.

    «Access denied» на покупке означает не «куки протухли», а «этой сессии
    покупать нельзя» — чаще всего потому, что кошелёк не подключён или
    подключён другой. Пока оба адреса не видно рядом, различить нечем.
    """
    session = _page_session(cookies or {})
    for url in ("https://fragment.com/stars/buy", "https://fragment.com/stars",
                "https://fragment.com/"):
        try:
            r = session.get(url, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        body = r.text or ""
        found = _ADDR_RE.findall(body) or _RAW_ADDR_RE.findall(body)
        if report is not None:
            report.append(f"{url}: адресов в разметке {len(found)}")
        if found:
            return found[0]
    return ""


def wallet_address_sync(mnemonic: str, wallet_version: str = "v4r2") -> str:
    """Адрес кошелька бота — того, с которого он собирается платить."""
    try:
        wallet = _wallet_from_mnemonic(mnemonic, wallet_version)
        return wallet.address.to_string(True, True, True)
    except Exception:
        return ""


def _same_wallet(a: str, b: str) -> bool:
    """Один ли это кошелёк. EQ… и UQ… — две записи одного адреса."""
    if not a or not b:
        return False
    return a[2:] == b[2:] if a[:1] in "EU" and b[:1] in "EU" else a == b


def page_signals(html: str) -> list[str]:
    """Что именно видно на странице — фактами, а не выводом.

    Прежний детектор считал признаком входа строку «ton-auth», хотя это
    кнопка «Connect TON», то есть признак ровно обратного: бот докладывал
    «вход есть» на гостевой странице. Списку найденных маркеров соврать
    труднее, чем одному слову.
    """
    text = html or ""
    low = text.lower()
    out: list[str] = []
    # Заголовок — из оригинала: в нижнем регистре он читается как чужой.
    m = re.search(r"<title[^>]*>(.{0,80}?)</title>", text, re.S | re.I)
    if m:
        out.append(f"заголовок: «{m.group(1).strip()}»")
    checks = (
        ("ссылка выхода", "/logout" in low),
        ("кнопка «Log in»", "log in" in low or "sign in" in low),
        ("кнопка «Connect TON»", "connect ton" in low or "ton-auth" in low),
        ("раздел «My assets»", "my assets" in low),
        ("форма покупки звёзд", "buystars" in low or "stars-form" in low),
    )
    for name, present in checks:
        out.append(f"{name}: {'есть' if present else 'нет'}")
    return out


def _looks_logged_out(html: str) -> bool:
    """Похоже ли, что страница отдана гостю.

    Вход подтверждает только то, что бывает лишь у вошедшего: ссылка выхода
    или личный раздел. Кнопка подключения кошелька входом не является — на
    этом прежняя версия и ошибалась.
    """
    low = (html or "").lower()
    if not low:
        return True
    signed_in = "/logout" in low or "my assets" in low
    return not signed_in


def check_fragment_session_sync(cookies: dict,
                                api_hash: str = "") -> tuple[bool, object]:
    """Light check that the Fragment session is alive.

    Returns (ok, message) or, when a fresh hash was discovered along the way,
    (True, {"message":…, "api_hash":…}) so the caller can keep it: the hash is
    what the previous version got wrong, and finding it is most of the fix.
    """
    if not cookies:
        return False, "cookies не заданы"
    session = _make_session(cookies)

    def _try(h: str):
        try:
            r = _api_call(session, h, "searchStarsRecipient",
                          {"query": "durov", "quantity": 50})
        except Exception as e:
            return None, f"ошибка сети: {str(e)[:80]}"
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        try:
            return r.json(), ""
        except ValueError:
            return None, "Fragment ответил не JSON"

    data, err = _try(api_hash or DEFAULT_HASH)
    if data is not None and (data.get("ok") or data.get("found")):
        return True, "сессия работает"

    # «Bad request» — это ответ на чужой хеш. Собираем все, какие видны на
    # странице, и пробуем каждый: в разметке их несколько, и подойти может не
    # первый.
    report: list[str] = []
    facts: dict = {}
    tried = {(api_hash or DEFAULT_HASH)}
    fresh = ""
    for candidate in collect_api_hashes_sync(cookies, report, facts):
        if candidate in tried:
            continue
        tried.add(candidate)
        data2, err2 = _try(candidate)
        if data2 is not None and (data2.get("ok") or data2.get("found")):
            return True, {"message": "сессия работает (обновил api-hash)",
                          "api_hash": candidate}
        data, err = data2, err2
        fresh = candidate
    checked = len(tried) - 1

    if err:
        return False, err
    said = str((data or {}).get("error") or data)[:120]
    if "bad request" in said.lower():
        if facts.get("signed_in") is False:
            why = "Куки Fragment больше не действуют — страница отдаётся как гостю."
        elif checked:
            # Хеши нашлись, но ни один не принят. Говорить «прочитать не
            # удалось» здесь — прямая ложь, и она уводит от настоящей причины.
            why = (f"Со страницы прочитано хешей: {checked}, и Fragment не "
                   f"принял ни один. Обычно так отвечает сессия, которую "
                   f"Fragment считает чужой: куки скопированы из другого "
                   f"браузера или устарели.")
        else:
            why = "Хеш сессии со страницы Fragment прочитать не удалось."
        return False, {
            "message": f"Fragment: «Bad request». {why}",
            "how": ("Хеш вводить руками не нужно — бот читает его сам. "
                    "Нужно обновить куки: «🔑 Данные Fragment» → "
                    "«🍪 Cookies» — там написано, как достать их с телефона."),
            "report": report,
        }
    # Fragment ответил по существу — значит и хеш принят, и запрос дошёл
    # целиком. Ругаться на такой ответ нельзя: пробный ник тут ни при чём, а
    # продавец видел «⚠️» там, где всё в порядке. Осталось понять, от чьего
    # имени с нами говорят — гостю Fragment отвечает так же охотно.
    if any("не видно входа" in line for line in report):
        return False, {
            "message": "Куки Fragment больше не действуют — страница "
                       "отдаётся как гостю.",
            "how": ("Обновите куки: «🔑 Данные Fragment» → «🍪 Cookies» — "
                    "там написано, как достать их с телефона."),
            "report": report,
        }
    out: dict = {"message": f"сессия работает (Fragment на пробный запрос "
                            f"ответил «{said}» — это нормально)"}
    if fresh and fresh != (api_hash or DEFAULT_HASH):
        out["api_hash"] = fresh
    return True, out
