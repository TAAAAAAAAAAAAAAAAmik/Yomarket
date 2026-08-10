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
# Зашитого хеша больше нет. Он был чужой, из образца, и это первая из пяти
# причин, по которым выдача не работала: Fragment отвечал «Bad request», а
# продавцу предлагалось лезть в F12. Хеш всегда читается со страницы покупки.
DEFAULT_HASH = ""
SEQNO_POLL_SECS = 3
SEQNO_MAX_WAIT_SECS = 120


def _fix_base64(s: str) -> str:
    s = s.replace("-", "+").replace("_", "/")
    return s + ("=" * (-len(s) % 4))


def _make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.cookies.update(cookies or {})
    # Заголовки — как в документации рабочего клиента: только User-Agent.
    # Свои X-Requested-With / Origin / Referer убраны: они добавлялись по
    # догадке, а сверять поведение надо с тем, что заведомо работает.
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _api_call(session: requests.Session, api_hash: str, method: str,
              extra: dict):
    """Один запрос к Fragment API — ровно как в документации рабочего клиента.

    Всё уходит в строку запроса: и `method`, и `hash`, и аргументы. У нас
    было по-своему — метод и аргументы в теле, — и на этом стояла выдача:
    поиск получателя проходил, а покупка отвечала «Access denied». Своих
    вариантов здесь больше нет: документация описывает клиента, который
    доводит покупку до конца, и расхождения с ней мы не сочиняем.
    """
    return session.post(FRAGMENT_API_URL,
                        params={"method": method, "hash": api_hash, **extra},
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
    api_hash: str = "",
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
    state = {"hash": api_hash or "", "refreshed": False}
    if not state["hash"]:
        # Хеш всегда берётся со страницы покупки, как в документации.
        # Зашитого значения здесь больше нет: чужой хеш — первая из пяти
        # причин, по которым выдача не работала.
        state["hash"] = fetch_api_hash_sync(cookies)
        if not state["hash"]:
            return False, ("Не удалось прочитать hash со страницы "
                           "fragment.com/stars/buy — проверьте куки Fragment")

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
    # Только `query` — так в документации. У нас сюда добавлялся ещё
    # `quantity` по наблюдению за формой на сайте; документ его не шлёт.
    search = _post("searchStarsRecipient", {"query": username})
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
            return False, ("Fragment: «Access denied» — сессию он признаёт "
                           "(получателя нашёл), но покупку ей не разрешает. "
                           "Проверьте сверку кошельков: AutoStars → "
                           "🔑 Данные Fragment → 🧪 Проверить вход. Если там "
                           "«это один кошелёк», причина в другом — покажет "
                           "команда /stars_probe с ником покупателя.")
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
    # Документация рабочего клиента ищет не только «hash»: у Fragment тот же
    # ключ встречается под другими именами.
    r"['\"](?:csrf|token|nonce|signature|sig)['\"]\s*:\s*['\"]([0-9a-f]{12,64})['\"]",
)

# Первой — страница покупки: именно её называет документация рабочего
# клиента, и хеш, выданный на ней, точно годится для покупки. Хеш со
# страницы витрины годился для поиска получателя, а дальше шёл «Access
# denied» — возможно, ровно поэтому.
_HASH_PAGES = ("https://fragment.com/stars/buy",
               "https://fragment.com/stars",
               "https://fragment.com/",
               "https://fragment.com/premium")


def _script_urls(html: str) -> list[str]:
    """Адреса подключённых скриптов, абсолютные."""
    urls: list[str] = []
    for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html or ""):
        src = m.group(1)
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://fragment.com" + src
        elif not src.startswith("http"):
            continue
        if src not in urls:
            urls.append(src)
    return urls


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
        if not found:
            # Документация ищет хеш и в подключённых JS-файлах: в разметке его
            # может не быть вовсе. Только когда в самой странице пусто —
            # лишние загрузки на каждый заказ ни к чему.
            for src in _script_urls(body)[:6]:
                try:
                    js = session.get(src, timeout=15).text or ""
                except Exception:
                    continue
                for pattern in _HASH_PATTERNS:
                    for m in re.finditer(pattern, js):
                        h = m.group(1)
                        if h and h not in found:
                            found.append(h)
                if found:
                    if report is not None:
                        report.append(f"  · хеш найден в {src[:60]}")
                    break
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
    # Первой — страница профиля: документация рабочего клиента проверяет
    # аккаунт и привязанный кошелёк именно на ней. Витрина показывает адрес
    # не всегда, и «кошелёк не вижу» получалось на живой привязке.
    for url in ("https://fragment.com/my/profile",
                "https://fragment.com/stars/buy", "https://fragment.com/stars",
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


def wallet_hash(addr: str) -> str:
    """Внутренний хеш адреса — то, чем один кошелёк отличается от другого.

    У одного адреса три записи: EQ… и UQ… (base64url, различаются только
    флагом bounceable) и сырая 0:hex. Сравнение строк объявляло их разными
    кошельками — и бот уверенно сообщал «Fragment примет оплату только со
    своего», глядя на два написания одного и того же.
    """
    v = str(addr or "").strip()
    if not v:
        return ""
    if ":" in v:                                  # сырая форма 0:hex
        tail = v.split(":", 1)[1].lower()
        return tail if re.fullmatch(r"[0-9a-f]{64}", tail) else ""
    try:
        raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
    except Exception:
        return ""
    # tag(1) + workchain(1) + hash(32) + crc(2)
    return raw[2:34].hex() if len(raw) == 36 else ""


def _same_wallet(a: str, b: str) -> bool:
    """Один ли это кошелёк — по хешу, а не по написанию."""
    ha, hb = wallet_hash(a), wallet_hash(b)
    if ha and hb:
        return ha == hb
    return bool(a) and bool(b) and str(a).strip() == str(b).strip()


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


# Чем бот может представиться. Сессию Fragment выдаёт браузеру, и она
# бывает привязана не только к куке: куки, снятые с телефона, десктопному
# User-Agent могут не подойти. Перебрать три строки дешевле, чем гадать.
_USER_AGENTS = (
    ("телефон Android", "Mozilla/5.0 (Linux; Android 13; SM-S911B) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Mobile Safari/537.36"),
    ("iPhone", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
               "Mobile/15E148 Safari/604.1"),
    ("компьютер", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36"),
)


def guess_cookie_name(value: str) -> str:
    """На какую куку Fragment похоже это значение. Пусто — не похоже ни на что.

    Куки вводятся по одной, и перепутать поля легко: у stel_ssid и
    stel_token значения выглядят одинаково «технически». Отличаются они
    надёжно — у ssid есть подчёркивание и он короткий, token — сплошной
    hex, ton_token заметно длиннее и в base64url.
    """
    v = str(value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"[0-9a-f]{8,32}_\d{6,30}", v):
        return "stel_ssid"
    if re.fullmatch(r"[0-9a-f]{40,400}", v):
        return "stel_token"
    if len(v) > 100 and re.fullmatch(r"[A-Za-z0-9_\-=]+", v):
        return "stel_ton_token"
    return ""


def probe_session_sync(cookies: dict) -> list[str]:
    """Почему Fragment не признаёт эту сессию — перебором, а не рассуждением.

    Отвечает страница гостя: значит куки до Fragment либо не доходят, либо
    им не подходят. Проверяем три вещи, которые можно проверить сами:
    сколько кук задано и какой они длины, отвечает ли Fragment иначе на
    другой User-Agent, и меняет ли что-то отсутствие XHR-заголовков.
    """
    out: list[str] = []
    cookies = cookies or {}
    out.append(f"Кук задано: {len(cookies)}")
    # Типичные длины. Не проверка подлинности, а проверка правдоподобия:
    # stel_token длиннее stel_ssid в разы, и обратное соотношение почти
    # всегда значит, что значения попали не в свои поля.
    typical = {"stel_token": (60, 400), "stel_ssid": (10, 40),
               "stel_ton_token": (100, 2000)}
    for name in ("stel_token", "stel_ssid", "stel_ton_token"):
        value = str(cookies.get(name) or "")
        # Значения не печатаем — это доступ к аккаунту. Длина скажет
        # достаточно: обрезанная при копировании кука видна сразу.
        if not value:
            out.append(f"  · {name}: нет")
            continue
        low, high = typical[name]
        mark = "" if low <= len(value) <= high else "  ⚠️ необычная длина"
        out.append(f"  · {name}: {len(value)} символов "
                   f"(обычно {low}–{high}){mark}")
    # Не только по длине: у значений есть узнаваемый вид, и перепутанные
    # поля видно наверняка, а не по подозрению.
    wrong = []
    for name in ("stel_token", "stel_ssid", "stel_ton_token"):
        value = str(cookies.get(name) or "")
        looks = guess_cookie_name(value)
        if value and looks and looks != name:
            wrong.append(f"в {name} лежит значение от {looks}")
    if wrong:
        out.append("⚠️ Значения попали не в свои поля: " + "; ".join(wrong)
                   + ". Введите их заново по одному.")
    else:
        # Формат опознаётся не всегда — например, у значения непривычного
        # вида. Соотношение длин остаётся вторым, более грубым признаком.
        token, ssid = (str(cookies.get("stel_token") or ""),
                       str(cookies.get("stel_ssid") or ""))
        if token and ssid and len(ssid) > len(token):
            out.append("⚠️ stel_ssid длиннее stel_token — обычно наоборот. "
                       "Похоже, значения перепутаны местами.")
    extra = [k for k in cookies if k not in
             ("stel_token", "stel_ssid", "stel_ton_token")]
    if extra:
        out.append(f"  · ещё кук: {', '.join(sorted(extra)[:6])}")

    for label, ua in _USER_AGENTS:
        session = requests.Session()
        session.cookies.update(cookies)
        session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
        })
        try:
            r = session.get("https://fragment.com/stars", timeout=20)
        except Exception as e:
            out.append(f"{label}: ошибка сети {str(e)[:50]}")
            continue
        body = r.text or ""
        signed = not _looks_logged_out(body)
        out.append(f"{label}: HTTP {r.status_code}, {len(body)} символов, "
                   + ("✅ вошли" if signed else "гость"))
        if signed:
            out.append("  ↑ вот с этим User-Agent сессия признаётся")
        # Что Fragment ставит сам: имена кук, которые он на самом деле
        # использует. Если среди них есть та, которой у нас нет, гадать о
        # «неполном наборе» больше не придётся.
        try:
            names = sorted({c.name for c in session.cookies})
        except Exception:
            names = []
        if names:
            out.append(f"  куки после ответа: {', '.join(names[:8])}")
    return out


def probe_buy_sync(cookies: dict, username: str, quantity: int = 50,
                   api_hash: str = "") -> list[str]:
    """Где именно ломается покупка — перебором того, чем запросы отличаются.

    Рабочий образец шлёт всё в строке запроса и ставит только User-Agent;
    здесь параметры уехали в тело, а к ним добавились Origin и
    X-Requested-With. Что из этого мешает — вопрос эксперимента, а не
    рассуждения, тем более что поиск получателя проходит в обоих случаях.

    Ничего не оплачивается: заявка денег не двигает, списание происходит
    только при отправке подписанной транзакции после неё.
    """
    username = (username or "").strip().lstrip("@")
    if not cookies or not username:
        return ["нужны куки и ник"]

    out: list[str] = []
    hashes = collect_api_hashes_sync(cookies) or []
    stored = (api_hash or "").strip()
    if stored and stored not in hashes:
        hashes.insert(0, stored)
    if not hashes:
        return ["не нашёл ни одного api-hash на страницах Fragment"]
    out.append(f"Хешей найдено: {len(hashes)}")

    plain = {"User-Agent": "Mozilla/5.0"}
    rich = {"User-Agent": _USER_AGENTS[0][1],
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://fragment.com",
            "Referer": "https://fragment.com/stars"}

    # (название, всё-в-строке-запроса, заголовки, слать ли quantity в поиске)
    variants = (
        ("как в образце", True, plain, False),
        ("строка запроса + наши заголовки", True, rich, False),
        ("тело запроса, без quantity", False, rich, False),
        ("тело запроса, с quantity", False, rich, True),
        ("тело запроса, простые заголовки", False, plain, True),
    )

    def call(session, in_query, h, method, args):
        if in_query:
            return session.post(FRAGMENT_API_URL,
                                params={"method": method, "hash": h, **args},
                                timeout=20)
        return session.post(FRAGMENT_API_URL, params={"hash": h},
                            data={"method": method, **args}, timeout=20)

    for label, in_query, headers, with_qty in variants:
        for h in hashes[:2]:
            tag = f"{label} · хеш …{h[-6:]}"
            session = requests.Session()
            session.cookies.update(cookies)
            session.headers.update(headers)
            args = {"query": username}
            if with_qty:
                args["quantity"] = quantity
            try:
                search = call(session, in_query, h,
                              "searchStarsRecipient", args).json()
            except Exception as e:
                out.append(f"{tag}: поиск — ошибка {str(e)[:40]}")
                continue
            recipient = _extract_recipient(search)
            if not recipient:
                out.append(f"{tag}: поиск — "
                           f"{str(search.get('error') or search)[:60]}")
                continue
            try:
                init = call(session, in_query, h, "initBuyStarsRequest",
                            {"recipient": recipient,
                             "quantity": quantity}).json()
            except Exception as e:
                out.append(f"{tag}: заявка — ошибка {str(e)[:40]}")
                continue
            if init.get("req_id") or init.get("id"):
                out.append(f"✅ {tag}: ЗАЯВКА ПРИНЯТА — вот рабочий вариант")
                return out
            out.append(f"{tag}: заявка — "
                       f"{str(init.get('error') or init)[:70]}")
    return out


def probe_page_api_sync(cookies: dict) -> list[str]:
    """Что страница покупки говорит о своих же запросах.

    Пять вариантов формы запроса получили один и тот же «Access denied», а
    поиск получателя прошёл в каждом — значит дело не в том, как мы шлём.
    Образец, по которому написан код, мог устареть: Fragment мог
    переименовать метод или начать требовать поле, которого мы не шлём.

    Имена методов лежат не в разметке, а в подключаемых скриптах, поэтому
    смотреть надо и в них: в самой странице их может не быть вовсе.
    """
    out: list[str] = []
    session = _page_session(cookies or {})
    try:
        r = session.get("https://fragment.com/stars/buy", timeout=20)
    except Exception as e:
        return [f"страница покупки: {str(e)[:60]}"]
    if r.status_code != 200:
        return [f"страница покупки: HTTP {r.status_code}"]
    body = r.text or ""
    out.append(f"страница: {len(body)} символов")

    sources = [("страница", body)]
    # Скрипты страницы — там и живут вызовы API.
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', body)[:6]:
        url = src if src.startswith("http") else (
            "https://fragment.com" + (src if src.startswith("/") else "/" + src))
        try:
            js = session.get(url, timeout=20)
        except Exception:
            continue
        if js.status_code == 200 and js.text:
            sources.append((url.rsplit("/", 1)[-1][:32], js.text))
    out.append(f"скриптов прочитано: {len(sources) - 1}")

    seen_methods: set[str] = set()
    for name, text in sources:
        methods = set(re.findall(
            r"""method['"]?\s*[:=]\s*['"]([A-Za-z][A-Za-z0-9_]{3,40})['"]""",
            text))
        # Fragment зовёт методы и короче — просто по имени в кавычках рядом
        # с ajax-вызовом; отбираем то, что похоже на его словарь.
        methods |= set(re.findall(r"['\"]((?:get|init|search|confirm|update)"
                                  r"[A-Z][A-Za-z0-9]{3,40})['\"]", text))
        fresh = sorted(m for m in methods if m not in seen_methods)
        seen_methods |= methods
        if fresh:
            out.append(f"{name}: " + ", ".join(fresh[:14]))

    stars = sorted(m for m in seen_methods if "star" in m.lower())
    out.append("методы про звёзды: " + (", ".join(stars) if stars
                                        else "не нашёл ни одного"))
    for name in ("initBuyStarsRequest", "getBuyStarsLink"):
        out.append(f"{name}: " + ("есть" if name in seen_methods
                                  else "НЕ упоминается нигде"))
    return out


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
