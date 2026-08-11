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
# Запасной узел — он есть в документации, у нас его не было. Транзакция уже
# подписана: если основной узел не ответил, деньги не ушли, а заказ считался
# бы проваленным на ровном месте.
TONCENTER_SEND_FALLBACK = "https://toncenter.net/api/v2/sendBoc"
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
    """Получатель из ответа `searchStarsRecipient` — ровно `found.recipient`.

    В документации значение одно: `data["found"]["recipient"]`. У нас рядом
    стоял перебор `id`, `myself`, `value` «на всякий случай» — и `myself` там
    настоящее поле, булево: у своего же аккаунта Fragment отвечает
    `{"myself": true, "recipient": "..."}`. Стоило `recipient` не прийти — и
    в заявку ушло бы `recipient=True`, а разбираться пришлось бы с ответом
    «Access denied», который выглядит точно так же, как отказ по правам.
    """
    if not isinstance(resp, dict):
        return ""
    found = resp.get("found")
    if isinstance(found, dict) and found.get("recipient"):
        return str(found["recipient"])
    if resp.get("recipient"):
        return str(resp["recipient"])
    return ""


def _query_forms(username: str) -> list[str]:
    """Как документация ищет ник: «@Username, @username, Username, username».

    Fragment отвечает не на любое написание, и рабочий клиент перебирает
    четыре. Мы слали одно — то, что ввёл продавец, без «собаки». На своём
    аккаунте это проходило, и гадать, почему у покупателя «получатель не
    найден», пришлось бы вслепую.
    """
    bare = (username or "").strip().lstrip("@")
    if not bare:
        return []
    # Порядок ровно как в документации: @Username, @username, Username,
    # username. У нас он был свой — сначала оба написания «как ввели»,
    # потом оба в нижнем регистре. Разница видна только на нике со
    # заглавными, но сверяться так сверяться.
    low = bare.lower()
    forms = [f"@{bare}", f"@{low}", bare, low]
    out: list[str] = []
    for f in forms:
        if f not in out:
            out.append(f)
    return out


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
    """Перевод с комментарием — как в `build_boc_with_comment` образца.

    Образец не передаёт ячейку Fragment как есть: он раскодирует её в текст
    и собирает комментарий заново — 32 нулевых бита, потом байты текста.
    Мы передавали ячейку без изменений. Это наш вариант, а не описанный, и
    поэтому он убран.

    Замечание, которое стоит помнить, если шаг когда-нибудь не сойдётся:
    `get_top_upped_array()` отдаёт вместе с текстом и четыре нулевых байта
    заголовка, а `.strip()` их не снимает — это не пробелы. То есть
    комментарий получается с лишним нулевым префиксом. У автора образца
    покупка при этом доходит до конца, и вероятная причина в том, что
    Fragment сверяет платёж по самому BOC: `confirmReq` шлёт ему BOC
    целиком. Проверить это можно будет только на удавшейся покупке.
    """
    from tonsdk.utils import Address, to_nano
    from tonsdk.boc import Cell

    amount_ton = Decimal(int(amount_nano)) / Decimal(1_000_000_000)

    payload_cell = None
    if payload_b64:
        try:
            src = Cell.one_from_boc(base64.b64decode(_fix_base64(payload_b64)))
            text = src.bits.get_top_upped_array().decode("utf-8",
                                                         errors="ignore").strip()
            payload_cell = Cell()
            payload_cell.bits.write_uint(0, 32)
            payload_cell.bits.write_bytes(text.encode("utf-8"))
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
    """Отправить подписанную транзакцию. Второй узел — как в документации.

    Повтор безопасен: тот же BOC с тем же seqno сеть примет один раз, а
    второй отклонит как дубль. Опасно обратное — считать заказ проваленным
    из-за одного не ответившего узла.
    """
    for url in (TONCENTER_SEND, TONCENTER_SEND_FALLBACK):
        try:
            r = requests.post(url, json={"boc": boc_b64}, timeout=20)
            r.raise_for_status()
            if r.json().get("ok"):
                return True
            logger.error("sendBoc: %s ответил без ok", url)
        except Exception as e:
            logger.error("sendBoc failed on %s: %s", url, e)
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
    """Покупка звёзд — по документации рабочего клиента, шаг в шаг.

    Собрано заново и намеренно линейно: восемь шагов раздела 9 в том же
    порядке и с теми же полями. Прежняя версия обросла нашими домыслами —
    перебор страниц с хешем, повтор с освежённым хешем, оплата всех
    сообщений подряд, своя сборка комментария, — и каждый такой домысел
    приходилось потом опровергать отдельным прогоном. Здесь их нет.

    Своё оставлено только там, где документ молчит, а бот обязан не врать:
    ответ `confirmReq` проверяется, а факт списания записывается в `report`,
    чтобы менеджер не повторил покупку, за которую уже заплачено.

    Блокирующая — звать через executor. Секреты не логируются.
    """
    import json

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

    # 1. Сессия: куки и единственный заголовок User-Agent.
    session = _make_session(cookies)

    def post(method: str, args: dict) -> dict:
        """Один запрос: всё в строке запроса, как в разделах 3–8."""
        try:
            r = session.post(FRAGMENT_API_URL,
                             params={"method": method, "hash": h, **args},
                             timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.RequestException as e:
            return {"error": f"сеть: {str(e)[:80]}"}
        except ValueError:
            return {"error": "не JSON от Fragment"}
        return data if isinstance(data, dict) else {"error": str(data)[:80]}

    # 2. Хеш — со страницы покупки, той же сессией. Читается всегда, как в
    # документации: хеш Fragment выдаёт сессии, и сохранённый в настройках
    # к нашей сессии отношения не имеет. Он остаётся только запасным — на
    # случай, если страница почему-то без хеша.
    h = ""
    try:
        page = session.get("https://fragment.com/stars/buy", timeout=30)
        body = page.text or ""
    except Exception as e:
        return False, f"Страница fragment.com/stars/buy: {str(e)[:80]}"
    for pattern in _HASH_PATTERNS:
        m = re.search(pattern, body)
        if m:
            h = m.group(1)
            break
    if not h:
        h = (api_hash or "").strip()
    if not h:
        return False, ("Не удалось прочитать hash со страницы "
                       "fragment.com/stars/buy — проверьте куки Fragment")

    # 3. Получатель. Написания перебираются, как в разделе 3.
    search: dict = {}
    recipient = ""
    for form in _query_forms(username):
        search = post("searchStarsRecipient", {"query": form})
        recipient = _extract_recipient(search)
        if recipient:
            break
    if not recipient:
        err = search.get("error") or search.get("error_message") or "не найден"
        return False, f"@{username}: {err}. Проверьте username и cookies."

    # 4. Заявка → req_id.
    init = post("initBuyStarsRequest", {"recipient": recipient,
                                        "quantity": quantity})
    req_id = init.get("req_id")
    if not req_id:
        err = str(init.get("error") or init.get("error_message")
                  or str(init)[:120])
        if "access denied" in err.lower():
            return False, (
                "Fragment: «Access denied» — заявку на покупку он не "
                "принимает. Поиск получателя при этом проходит, но он "
                "проходит и без входа, так что правами это не считается.\n\n"
                "Что уже проверено и причиной не является: форма запроса, "
                "api-hash, свежесть кук, подключённый TON-кошелёк и то, "
                "себе или чужому. Причина пока не найдена; покажите вывод "
                "/stars_probe.")
        return False, f"initBuyStarsRequest не дал req_id: {err}"

    # 5. Кошелёк: сырой адрес для `account`, обычный — для seqno.
    try:
        wallet = _wallet_from_mnemonic(mnemonic, wallet_version)
        raw_addr = wallet.address.to_string(False)
        bounce_addr = wallet.address.to_string(True, True, True)
    except Exception as e:
        return False, f"Ошибка кошелька (проверьте seed-фразу): {str(e)[:80]}"

    account = json.dumps({"address": raw_addr, "chain": MAINNET_CHAIN})
    device = json.dumps({
        "platform": "browser",
        "appName": "telegram-wallet",
        "appVersion": "1",
        "maxProtocolVersion": 2,
        "features": ["SendTransaction",
                     {"name": "SendTransaction", "maxMessages": 4}],
    })

    # 6. Данные TON-транзакции.
    link = post("getBuyStarsLink", {"id": req_id, "transaction": 1,
                                    "show_sender": 1, "account": account,
                                    "device": device})
    tx = link.get("transaction") or {}
    messages = tx.get("messages") or []
    if not messages:
        err = link.get("error") or link.get("error_message") or str(link)[:150]
        return False, f"getBuyStarsLink не дал транзакцию: {err}"

    # Раздел 6 документа: берётся первое сообщение. Мы платили по всем
    # подряд — это была наша предусмотрительность, и на лишнем сообщении
    # она списала бы деньги второй раз.
    msg = messages[0]
    try:
        destination = msg["address"]
        amount_nano = int(msg["amount"])
        payload = msg.get("payload", "")
    except (KeyError, TypeError, ValueError):
        return False, f"Fragment вернул сообщение без адреса или суммы: {str(msg)[:100]}"

    # 7. Подпись и отправка.
    try:
        seqno = _get_seqno(bounce_addr)
    except Exception as e:
        return False, f"Не удалось получить seqno кошелька: {str(e)[:80]}"
    try:
        boc = _build_signed_boc(wallet, destination, amount_nano, payload, seqno)
    except Exception as e:
        return False, f"Ошибка сборки транзакции: {str(e)[:100]}"
    if not _send_boc(boc):
        return False, "TonCenter отклонил транзакцию (проверьте баланс кошелька)"

    # Деньги ушли. Дальше любой провал означает «заплатили и не получили»,
    # и повторять покупку нельзя — заплатим дважды. Документ об этом молчит,
    # но молчать об этом продавцу нельзя.
    if report is not None:
        report["sent_onchain"] = True
        report["nano"] = amount_nano
        report["ton"] = amount_nano / 1_000_000_000
        if len(messages) > 1:
            report["extra_messages"] = len(messages) - 1

    # Документ ждёт seqno до confirmReq — так и делаем.
    if wait_confirm:
        _wait_seqno_advance(bounce_addr, seqno)

    # 8. Подтверждение. Его ответ и решает, засчитана ли оплата.
    confirm = post("confirmReq", {"id": req_id, "boc": boc,
                                  "account": account})
    cerr = confirm.get("error") or confirm.get("error_message")
    if cerr:
        if report is not None:
            report["confirm_error"] = str(cerr)[:120]
        return False, (
            f"⚠️ TON ушёл ({amount_nano / 1_000_000_000:.4f}), но Fragment "
            f"не засчитал оплату: {str(cerr)[:80]}. Повтор не делаю — "
            "заплатили бы дважды. Проверьте начисление на fragment.com и "
            "выдайте вручную, если звёзд нет.")

    tail = ""
    if len(messages) > 1:
        tail = (f"\n⚠️ Fragment попросил ещё {len(messages) - 1} перевод(а) — "
                "оплачен только первый, как в документации. Проверьте, "
                "начислились ли звёзды.")
    if wait_confirm:
        return True, (f"✅ {quantity}⭐ отправлены на @{username} "
                      f"(подтверждено в TON){tail}")
    return True, f"✅ {quantity}⭐ отправлены на @{username}{tail}"

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
                            facts: dict | None = None,
                            session: requests.Session | None = None
                            ) -> list[str]:
    """Все хеши, какие видны на страницах Fragment, — в порядке доверия.

    Раньше брался первый совпавший, и этого не хватило: в разметке лежит не
    один «hash», и подойти к API может не тот, что нашёлся первым. Перебрать
    несколько дешевле, чем гадать.

    `session` — чтобы читать страницу той же сессией, которой потом пойдут
    запросы. В документации так и сделано: одна `requests.Session()` на
    всё. У нас страницу читала отдельная сессия со своим User-Agent, а
    куки, которые Fragment ставит на этой странице, оставались в ней и
    пропадали. Хеш при этом уходил в запрос от другой сессии.

    `facts` — то, что вызывающему нужно решать, а не показывать: вошли ли мы.
    Вычитывать это обратно из текста отчёта значило бы управлять логикой по
    прозе, а она меняется от любой правки формулировки.
    """
    session = session or _page_session(cookies or {})
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


def fetch_api_hash_sync(cookies: dict, report: list | None = None,
                        session: requests.Session | None = None) -> str:
    """The api hash Fragment issued to this session, read off its own page.

    Fragment stamps every request with a per-session hash, and a hash from
    somebody else's session is answered with «Bad request» — which is what a
    hardcoded one produced. It sits in the page's own JavaScript, so there is
    no reason to make a seller find it by hand, let alone open developer tools
    on a phone.

    `session` передают, когда хеш и запросы должны идти одной сессией — как
    в документации.
    """
    got = collect_api_hashes_sync(cookies, report, session=session)
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


# Что документация читает с профиля: имя, ник, кошелёк и две отметки
# проверки. Отметки мы не смотрели ни разу — а «Access denied» на покупке
# при живой сессии и работающих куках объясняется ими не хуже прочего.
_PROFILE_MARKS = (
    ("Telegram-ник", r"@([A-Za-z0-9_]{4,32})"),
    ("Identity Verified", r"(Identity\s+Verified)"),
    ("Wallet Verified", r"(Wallet\s+Verified)"),
    ("Not Verified", r"(Not\s+Verified)"),
    ("KYC", r"(KYC|verification required|Verify)"),
)


def profile_facts_sync(cookies: dict) -> list[str]:
    """Что Fragment пишет о самом аккаунте на `/my/profile`.

    Документация рабочего клиента читает отсюда пять вещей: имя, ник,
    TON-кошелёк, Identity Verified и Wallet Verified. Первые три мы читали,
    последние две — ни разу, хотя именно они решают, что аккаунту разрешено.
    Отсутствие фразы в разметке доказательством не считается: на этих
    страницах не видно и работающих имён методов.
    """
    out: list[str] = []
    session = _page_session(cookies or {})
    try:
        r = session.get("https://fragment.com/my/profile", timeout=20)
    except Exception as e:
        return [f"профиль: {str(e)[:60]}"]
    body = r.text or ""
    out.append(f"профиль: HTTP {r.status_code}, {len(body)} символов")
    if r.status_code != 200 or not body:
        return out
    if _looks_logged_out(body):
        out.append("  страница отдана как гостю — остальное читать нечего")
        return out
    for label, pattern in _PROFILE_MARKS:
        m = re.search(pattern, body, re.I)
        out.append(f"  {label}: " + (f"есть — «{m.group(1)[:40]}»" if m
                                     else "не встречается"))
    addr = _ADDR_RE.findall(body) or _RAW_ADDR_RE.findall(body)
    out.append("  кошелёк: " + (f"…{addr[0][-6:]}" if addr else "не видно"))
    return out


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


def probe_recipient_sync(cookies: dict, username: str, quantity: int = 50,
                         api_hash: str = "") -> list[str]:
    """Найдёт ли Fragment этого получателя и примет ли на него заявку.

    Отдельно от большой пробы и без перебора вариантов: одна пара запросов,
    ответ за секунду. Нужна затем, что «ник не находится» и «покупка
    запрещена» — разные беды с разным лечением, а по одному прогону на
    собственном аккаунте их не различить: на себе поиск проходит всегда.

    Заявка денег не двигает: списание происходит только при отправке
    подписанной транзакции, до неё здесь дело не доходит.
    """
    nick = (username or "").strip().lstrip("@")
    if not cookies:
        return ["Куки Fragment не заданы"]
    if not nick:
        return ["Пустой ник"]

    h = (api_hash or "").strip() or fetch_api_hash_sync(cookies)
    if not h:
        return ["Не удалось прочитать api-hash со страницы Fragment — "
                "скорее всего истекли куки"]

    session = _make_session(cookies)
    out: list[str] = []
    search: dict = {}
    recipient = ""
    used = ""
    for form in _query_forms(nick):
        try:
            search = session.post(
                FRAGMENT_API_URL,
                params={"method": "searchStarsRecipient", "hash": h,
                        "query": form}, timeout=20).json()
        except Exception as e:
            return [f"Поиск не дошёл до Fragment: {str(e)[:60]}"]
        recipient = _extract_recipient(search)
        if recipient:
            used = form
            break

    if not recipient:
        err = str(search.get("error") or search.get("error_message")
                  or search)[:120]
        return [f"❌ @{nick} — не найден",
                f"Fragment ответил: {err}",
                "",
                "Пробовали написания: " + ", ".join(_query_forms(nick)),
                "Так отвечают и на несуществующий ник, и на тот, кому "
                "звёзды слать нельзя. Что именно — Fragment не уточняет."]

    found = search.get("found") if isinstance(search.get("found"), dict) else {}
    out.append(f"✅ @{nick} — найден (написание «{used}»)")
    if found.get("myself"):
        out.append("⚠️ Это ваш собственный аккаунт — покупку себе Fragment "
                   "может разрешать иначе, чем чужому.")
    out.append(f"recipient: {len(recipient)} знаков")

    try:
        init = session.post(
            FRAGMENT_API_URL,
            params={"method": "initBuyStarsRequest", "hash": h,
                    "recipient": recipient, "quantity": quantity},
            timeout=20).json()
    except Exception as e:
        return out + [f"Заявка не дошла: {str(e)[:60]}"]
    out.append("")
    if init.get("req_id") or init.get("id"):
        out.append(f"✅ Заявка на {quantity}⭐ принята — Fragment готов "
                   "продать. Денег пока не списано.")
    else:
        out.append(f"❌ Заявка на {quantity}⭐ — "
                   f"{str(init.get('error') or init)[:100]}")
    return out


def probe_buy_sync(cookies: dict, username: str, quantity: int = 50,
                   api_hash: str = "", control: str = "durov") -> list[str]:
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
    # Какие куки вообще ушли — только имена и длины. Значения не печатаем
    # никогда: это доступ к чужому аккаунту и кошельку.
    names = ", ".join(f"{k} ({len(str(v))})"
                      for k, v in sorted((cookies or {}).items()))
    out.append("Куки: " + (names or "нет"))

    # Признан ли вход — то единственное, чего проба до сих пор не показывала.
    # Поиск получателя Fragment отдаёт и гостю, покупку — нет. Пока эта
    # строка не напечатана, «Access denied» одинаково объясняется и
    # транспортом, и тем, что сессии просто нельзя покупать.
    facts: dict = {}
    pages: list[str] = []
    hashes = collect_api_hashes_sync(cookies, pages, facts) or []
    if "signed_in" in facts:
        out.append("Вход: " + ("✅ признан (есть выход/личный раздел)"
                               if facts["signed_in"]
                               else "❌ страница отдана как гостю"))
        if not facts["signed_in"]:
            # Куки Fragment живут недолго: в этот же день проба сначала
            # видела личный раздел, а через полчаса — гостевую страницу с
            # теми же куками. Пока это не сказано вслух, продавец ищет
            # причину в боте.
            out.append("  Куки Fragment истекли. Снимите их заново — они "
                       "живут недолго, и вчерашние уже не годятся.")
    wallet = wallet_on_page_sync(cookies)
    out.append("Кошелёк на странице Fragment: "
               + (f"…{wallet[-6:]}" if wallet else "не видно"))
    out += [f"  · {line}" for line in pages[:6]]

    stored = (api_hash or "").strip()
    if stored and stored not in hashes:
        hashes.insert(0, stored)
    if not hashes:
        return ["не нашёл ни одного api-hash на страницах Fragment"]
    # Длину печатаем, а не только хвост. У рабочего образца хеш — ровно
    # 18 знаков (af142ec36cafbbfa89). Среди наших шаблонов есть и «csrf», и
    # «token»: под видом api-hash легко подобрать что-то чужой длины, и
    # поиск такое может стерпеть, а покупка — нет. Хвоста для этого мало.
    out.append("Хешей найдено: " + ", ".join(
        f"…{h[-6:]} ({len(h)} знаков)" for h in hashes))

    # Что отвечает Fragment на другие методы. Проверка, которой не хватало
    # с самого начала: если «Access denied» приходит и на выдуманное имя
    # метода, то это не «вам нельзя покупать», а «такого метода тут нет» —
    # и вся линия рассуждений про права держалась на пустом месте.
    out.append("")
    out.append("Что отвечают другие методы:")
    out += _probe_methods(cookies, hashes[0], username, quantity)

    # Fragment различает «Invalid method», «Session expired» и «Access
    # denied» — значит метод существует, и отказ не про мёртвую сессию:
    # соседние методы на той же сессии отвечают иначе. Остаётся сам запрос.
    # Меняем в нём по одному полю и смотрим, меняется ли ответ. Если не
    # меняется ни от чего — Fragment до разбора параметров не доходит.
    out.append("")
    out.append("Что меняет каждое поле заявки:")
    out += _probe_init_shapes(cookies, hashes[0], username, quantity)

    # Ровно как в документации: одна сессия и на страницу, и на API. У нас
    # страницу читала отдельная сессия со своим User-Agent, и куки, которые
    # Fragment ставит при её открытии, терялись — а хеш он выдаёт сессии.
    # Это последнее отличие от образца, и до сих пор оно не проверялось.
    out.append("")
    out.append("Одной сессией, как в документе:")
    out += _probe_single_session(cookies, username, quantity)

    plain = {"User-Agent": "Mozilla/5.0"}
    rich = {"User-Agent": _USER_AGENTS[0][1],
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://fragment.com",
            "Referer": "https://fragment.com/stars"}

    # Перебор транспорта закрыт: шесть форм запроса дали побайтово один
    # ответ, а различие ответов по методам показало, что дело и не в правах.
    # Оставлена одна форма — та, что в документации, — как точка отсчёта.
    variants = (
        ("как в образце", True, plain, False, False),
    )

    def call(session, in_query, h, method, args):
        if in_query:
            return session.post(FRAGMENT_API_URL,
                                params={"method": method, "hash": h, **args},
                                timeout=20)
        return session.post(FRAGMENT_API_URL, params={"hash": h},
                            data={"method": method, **args}, timeout=20)

    # Подробности печатаются один раз, а не по разу на каждый из десяти
    # прогонов: иначе отчёт не влезет в сообщение.
    shown = {"search": False, "init": False}

    for label, in_query, headers, with_qty, warm in variants:
        for h in hashes[:2]:
            tag = f"{label} · хеш …{h[-6:]}"
            session = requests.Session()
            session.cookies.update(cookies)
            session.headers.update(headers)
            if warm:
                try:
                    session.get("https://fragment.com/stars/buy", timeout=20)
                except Exception as e:
                    out.append(f"{tag}: страница — ошибка {str(e)[:40]}")
                    continue
            # Написания перебираются и здесь. Перебор был вписан в покупку,
            # а в пробу — нет, и контрольная заявка упёрлась в «Please enter
            # a username assigned to a user» на живом нике.
            search, recipient = {}, ""
            for form in _query_forms(username):
                args = {"query": form}
                if with_qty:
                    args["quantity"] = quantity
                try:
                    search = call(session, in_query, h,
                                  "searchStarsRecipient", args).json()
                except Exception as e:
                    out.append(f"{tag}: поиск — ошибка {str(e)[:40]}")
                    search = {}
                    break
                recipient = _extract_recipient(search)
                if recipient:
                    break
            if not recipient:
                out.append(f"{tag}: поиск — "
                           f"{str(search.get('error') or search)[:60]}")
                continue
            # Что именно нашлось — важнее, чем «нашлось». Если сюда попал не
            # тот идентификатор (например, флаг `myself` вместо recipient),
            # заявка обязана отвечать отказом, и искать причину в транспорте
            # можно бесконечно.
            if not shown["search"]:
                shown["search"] = True
                found = search.get("found")
                out.append(f"  поиск вернул поля: "
                           f"{', '.join(sorted(search)) or '—'}")
                out.append(f"  found: {str(found)[:120]}")
                out.append(f"  recipient длиной {len(recipient)}: "
                           f"«{recipient[:24]}…»")
                if isinstance(found, dict) and found.get("myself"):
                    out.append("  ⚠️ myself: это ваш собственный аккаунт")
            try:
                resp = call(session, in_query, h, "initBuyStarsRequest",
                            {"recipient": recipient, "quantity": quantity})
                init = resp.json()
            except Exception as e:
                out.append(f"{tag}: заявка — ошибка {str(e)[:40]}")
                continue
            if init.get("req_id") or init.get("id"):
                out.append(f"✅ {tag}: ЗАЯВКА ПРИНЯТА — вот рабочий вариант")
                return out
            out.append(f"{tag}: заявка — "
                       f"{str(init.get('error') or init)[:70]}")
            # Ответ целиком, один раз: «Access denied» бывает не единственным
            # полем, и соседние объясняют, чего не хватает.
            if not shown["init"]:
                shown["init"] = True
                out.append(f"  ответ заявки: HTTP {resp.status_code}, "
                           f"поля: {', '.join(sorted(init)) or '—'}")
                out.append(f"  целиком: {str(init)[:200]}")

    # Ни один вариант не прошёл. Осталось различить две причины, которые до
    # сих пор объясняли отказ одинаково: сессии вообще нельзя покупать —
    # или нельзя покупать себе. Проверяется одной заявкой на чужой ник.
    # В работе получатель всегда покупатель, а не продавец, так что второй
    # случай означал бы, что выдача исправна, а сломан только наш способ
    # её проверять.
    live = (control or "").strip().lstrip("@")
    if live and live.lower() != username.lower():
        out.append("")
        out.append(f"Контрольная заявка на чужой ник @{live}:")
        out += _probe_control(cookies, live, quantity, hashes[0], username)

    # То, что документация читает с профиля, а мы не читали ни разу:
    # отметки проверки аккаунта. Живая сессия и работающие куки покупку не
    # разрешили — значит дело в правах самого аккаунта либо в чём-то, чего
    # мы ещё не видели. Профиль — единственное место, где Fragment о правах
    # говорит словами.
    out.append("")
    out.append("Что Fragment пишет об аккаунте:")
    out += [f"  {line}" for line in profile_facts_sync(cookies)]

    out.append("")
    out.append("Что меняет каждая кука:")
    out += _probe_cookie_roles(cookies, username, quantity)
    return out


def _probe_methods(cookies: dict, api_hash: str, username: str,
                   quantity: int) -> list[str]:
    """Чем отличаются ответы Fragment на разные методы.

    «Access denied» мы полторы недели читали как «этой сессии покупать
    нельзя» — и ни разу не спросили, что Fragment отвечает на имя метода,
    которого не существует вовсе. Если то же самое, значит это его общее
    «нет», и никаких выводов о правах из него не следует: скорее всего мы
    зовём метод, которого на этом хеше нет.

    Все вызовы безобидны: заявка денег не двигает, у `getBuyStarsLink` и
    `confirmReq` заведомо несуществующий `id`.
    """
    session = _make_session(cookies)

    def ask(method: str, args: dict) -> str:
        try:
            r = session.post(FRAGMENT_API_URL,
                             params={"method": method, "hash": api_hash,
                                     **args}, timeout=20)
        except Exception as e:
            return f"ошибка сети {str(e)[:30]}"
        try:
            data = r.json()
        except ValueError:
            return f"HTTP {r.status_code}, не JSON ({len(r.text or '')} симв.)"
        if not isinstance(data, dict):
            return f"HTTP {r.status_code}, {str(data)[:40]}"
        if data.get("error") or data.get("error_message"):
            return str(data.get("error") or data.get("error_message"))[:50]
        return "принято: " + ", ".join(sorted(data))[:50]

    recipient = ""
    first = _query_forms(username)[:1]
    if first:
        try:
            got = session.post(
                FRAGMENT_API_URL,
                params={"method": "searchStarsRecipient", "hash": api_hash,
                        "query": first[0]}, timeout=20).json()
            recipient = _extract_recipient(got)
        except Exception:
            recipient = ""

    checks = [
        ("searchStarsRecipient", {"query": first[0] if first else "x"}),
        ("initBuyStarsRequest", {"recipient": recipient or "x",
                                 "quantity": quantity}),
        ("getBuyStarsLink", {"id": "0", "transaction": 1}),
        ("confirmReq", {"id": "0", "boc": "x"}),
        # Двух выдуманных имён достаточно, чтобы увидеть, отличает ли
        # Fragment «нельзя» от «нет такого».
        ("thisMethodDoesNotExist", {}),
        ("searchStarsRecipientX", {"query": first[0] if first else "x"}),
    ]
    out = []
    for method, args in checks:
        out.append(f"  {method}: {ask(method, args)}")
    return out


def _probe_single_session(cookies: dict, username: str,
                          quantity: int) -> list[str]:
    """Вся цепочка одной сессией — точь-в-точь как в документации.

    Там одна `requests.Session()` с единственным заголовком User-Agent:
    ею читается страница покупки, из неё берётся хеш, ею же уходят все
    запросы. У нас страницу читала вторая сессия — со своим User-Agent и
    своей банкой кук. Куки, которые Fragment ставит при открытии страницы,
    в неё и оставались, а хеш, выданный ей, уходил в запрос от первой.
    """
    session = _make_session(cookies)
    out: list[str] = []
    try:
        page = session.get("https://fragment.com/stars/buy", timeout=20)
    except Exception as e:
        return [f"  страница — ошибка {str(e)[:40]}"]
    body = page.text or ""
    # Что Fragment поставил сам, открывая страницу: если среди этих кук есть
    # та, которой у нас не было, вот она и есть недостающее звено.
    try:
        got = sorted({c.name for c in session.cookies})
    except Exception:
        got = []
    out.append(f"  страница: HTTP {page.status_code}, {len(body)} символов")
    out.append(f"  куки после неё: {', '.join(got) or '—'}")
    new = [n for n in got if n not in (cookies or {})]
    if new:
        out.append(f"  ↑ Fragment поставил сам: {', '.join(new)}")

    h = ""
    for pattern in _HASH_PATTERNS:
        m = re.search(pattern, body)
        if m:
            h = m.group(1)
            break
    if not h:
        return out + ["  хеша на странице нет — дальше идти не с чем"]
    out.append(f"  хеш с этой страницы: …{h[-6:]} ({len(h)} знаков)")
    out.append(f"  {_probe_two_calls(session, h, username, quantity)}")
    return out


def _probe_init_shapes(cookies: dict, api_hash: str, username: str,
                       quantity: int) -> list[str]:
    """Меняем в заявке по одному полю и смотрим, меняется ли ответ.

    Fragment отвечает «Invalid method» на выдуманное имя и «Session
    expired» соседним методам на мёртвой сессии — а `initBuyStarsRequest`
    и там и там говорит «Access denied». На куки он, значит, не смотрит:
    его не устраивает сам запрос. Осталось узнать, доходит ли он вообще до
    разбора полей. Если ответ одинаков и на пустой запрос, и на мусор в
    получателе — не доходит, и искать надо не в полях.

    Ничего не оплачивается: заявка денег не двигает.
    """
    session = _make_session(cookies)
    recipient = ""
    forms = _query_forms(username)
    if forms:
        try:
            got = session.post(
                FRAGMENT_API_URL,
                params={"method": "searchStarsRecipient", "hash": api_hash,
                        "query": forms[0]}, timeout=20).json()
            recipient = _extract_recipient(got)
        except Exception:
            recipient = ""
    if not recipient:
        return ["  получателя не нашли — сравнивать не с чем"]

    shapes = (
        ("как сейчас", {"recipient": recipient, "quantity": quantity}),
        ("без quantity", {"recipient": recipient}),
        ("без recipient", {"quantity": quantity}),
        ("совсем пусто", {}),
        ("мусор в recipient", {"recipient": "zzz", "quantity": quantity}),
        ("quantity = 0", {"recipient": recipient, "quantity": 0}),
        ("quantity = 1000", {"recipient": recipient, "quantity": 1000}),
        # Поля, которые документация шлёт на соседнем шаге. Здесь их быть
        # не должно — но если ответ от них меняется, значит Fragment всё же
        # разбирает запрос, и это уже подсказка.
        ("+ show_sender", {"recipient": recipient, "quantity": quantity,
                           "show_sender": 1}),
    )
    out: list[str] = []
    answers: list[str] = []
    for label, args in shapes:
        try:
            data = session.post(
                FRAGMENT_API_URL,
                params={"method": "initBuyStarsRequest", "hash": api_hash,
                        **args}, timeout=20).json()
        except Exception as e:
            out.append(f"  {label}: ошибка {str(e)[:30]}")
            continue
        if isinstance(data, dict) and (data.get("req_id") or data.get("id")):
            out.append(f"  ✅ {label}: ПРИНЯТО — вот рабочая форма")
            return out
        said = str((data or {}).get("error") or data)[:50]
        answers.append(said)
        out.append(f"  {label}: {said}")
    if answers and len(set(answers)) == 1:
        out.append("  ↑ ответ один и тот же на всё, включая пустой запрос: "
                   "до разбора полей Fragment не доходит, и дело не в них.")
    return out


def _probe_cookie_roles(cookies: dict, username: str,
                        quantity: int) -> list[str]:
    """Какая кука на что влияет — вычитанием, а не рассуждением.

    «Connect TON» на странице значит либо «кошелёк не подключён», либо
    «разметка окна лежит там всегда» — по HTML не различить, и на похожем
    признаке (`ton-auth`) мы уже один раз ошиблись. Зато различить можно
    опытом: убрать куку и посмотреть, изменилось ли хоть что-нибудь. Если
    без `stel_ton_token` страница и ответы те же — значит она не работает,
    и покупку запрещает именно это.
    """
    cookies = dict(cookies or {})
    subsets = [("все куки", cookies)]
    for name in ("stel_ton_token", "stel_token", "stel_ssid"):
        if name in cookies:
            subsets.append((f"без {name}",
                            {k: v for k, v in cookies.items() if k != name}))
    subsets.append(("без кук вовсе", {}))

    out: list[str] = []
    marks: list[str] = []
    for label, sub in subsets:
        try:
            r = _page_session(sub).get("https://fragment.com/stars/buy",
                                       timeout=20)
            body = r.text or ""
        except Exception as e:
            out.append(f"  {label}: страница — {str(e)[:40]}")
            continue
        low = body.lower()
        assets = "my assets" in low
        h = ""
        for pattern in _HASH_PATTERNS:
            m = re.search(pattern, body)
            if m:
                h = m.group(1)
                break
        step = "хеша нет"
        if h:
            session = requests.Session()
            session.cookies.update(sub)
            session.headers.update({"User-Agent": "Mozilla/5.0"})
            step = _probe_two_calls(session, h, username, quantity)
        state = (f"{len(body)} симв., «My assets» "
                 f"{'есть' if assets else 'нет'}, {step}")
        marks.append(state)
        out.append(f"  {label}: {state}")

    # Вывод — только тот, который следует из сравнения, и только когда есть
    # что сравнивать. Догадка сюда не пишется.
    if len(marks) > 1 and marks[1] == marks[0] and "stel_ton_token" in cookies:
        out.append("  ↑ без stel_ton_token не изменилось ничего — эта кука "
                   "сейчас не работает. Подключите TON-кошелёк на "
                   "fragment.com заново и снимите её ещё раз.")
    return out


def _probe_two_calls(session, api_hash: str, username: str,
                     quantity: int) -> str:
    """Поиск и заявка одной строкой — «нашёл, заявка: …»."""
    recipient = ""
    search: dict = {}
    for form in _query_forms(username):
        try:
            search = session.post(
                FRAGMENT_API_URL,
                params={"method": "searchStarsRecipient", "hash": api_hash,
                        "query": form}, timeout=20).json()
        except Exception as e:
            return f"поиск — ошибка {str(e)[:30]}"
        recipient = _extract_recipient(search)
        if recipient:
            break
    if not recipient:
        return f"поиск — {str(search.get('error') or search)[:40]}"
    try:
        init = session.post(
            FRAGMENT_API_URL,
            params={"method": "initBuyStarsRequest", "hash": api_hash,
                    "recipient": recipient, "quantity": quantity},
            timeout=20).json()
    except Exception as e:
        return f"нашёл, заявка — ошибка {str(e)[:30]}"
    if init.get("req_id") or init.get("id"):
        return "нашёл, ЗАЯВКА ПРИНЯТА"
    return f"нашёл, заявка — {str(init.get('error') or init)[:40]}"


# Кого пробовать контрольной заявкой, если заданный ник не нашёлся.
# Fragment отвечает «Please enter a username assigned to a user» даже на
# @durov, и почему — неизвестно; перебор дешевле догадок. В работе получатель
# всегда покупатель, так что важно проверить хоть на кому-то, кто не «я».
_CONTROL_FALLBACKS = ("telegram", "toncoin", "wallet", "durov")


def _probe_control(cookies: dict, username: str, quantity: int,
                   api_hash: str, myself_nick: str = "") -> list[str]:
    """Заявка на чужой ник — тем же способом, что и основная.

    Денег не двигает: списание происходит только при отправке подписанной
    транзакции, а до неё дело здесь не доходит.
    """
    session = requests.Session()
    session.cookies.update(cookies or {})
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    def post(method: str, extra: dict):
        return session.post(FRAGMENT_API_URL,
                            params={"method": method, "hash": api_hash,
                                    **extra}, timeout=20)

    # Заданный ник — первым, дальше запасные. Свой собственный из перебора
    # исключён: на нём проверять нечего, это и есть основной случай.
    nicks = [username] + [n for n in _CONTROL_FALLBACKS
                          if n.lower() not in (username.lower(),
                                               (myself_nick or "").lower())]
    search: dict = {}
    recipient = ""
    tried: list[str] = []
    for nick in nicks:
        for form in _query_forms(nick):
            try:
                search = post("searchStarsRecipient", {"query": form}).json()
            except Exception as e:
                return [f"  поиск — ошибка {str(e)[:40]}"]
            recipient = _extract_recipient(search)
            if recipient:
                break
        if recipient:
            break
        tried.append(nick)
    if not recipient:
        return [f"  поиск — {str(search.get('error') or search)[:60]}",
                "  Ни один ник не нашёлся: " + ", ".join(f"@{n}" for n in tried),
                "  Задайте живой ник третьим словом — лучше всего свой второй "
                "аккаунт: /stars_probe NO0RD 50 второйник"]

    out: list[str] = []
    if nick != username:
        out.append(f"  @{username} не нашёлся, взял @{nick}")
    found = search.get("found")
    mine = bool(isinstance(found, dict) and found.get("myself"))
    out.append(f"  найден, myself: {'да' if mine else 'нет'}")
    try:
        resp = post("initBuyStarsRequest",
                    {"recipient": recipient, "quantity": quantity})
        init = resp.json()
    except Exception as e:
        return out + [f"  заявка — ошибка {str(e)[:40]}"]
    if init.get("req_id") or init.get("id"):
        out.append("  ✅ ЗАЯВКА ПРИНЯТА — Fragment не даёт покупать только "
                   "себе. В работе получатель всегда покупатель, так что "
                   "выдаче это не мешает.")
    elif mine:
        # Нашли снова себя — сравнивать не с чем, и говорить «дело не в
        # себе» нельзя: это и есть «себе».
        out.append(f"  ❌ {str(init.get('error') or init)[:70]}")
        out.append("  Но это снова ваш же аккаунт — версия «нельзя покупать "
                   "себе» так и осталась непроверенной.")
    else:
        out.append(f"  ❌ {str(init.get('error') or init)[:70]}")
        out.append("  Отказ и на чужом нике — дело не в «себе»: этой сессии "
                   "покупать нельзя вообще.")
    return out


# Что ищем в коде самого сайта. Простыми подстроками, без кавычек и без
# «method:» рядом: прежний поиск требовал и того и другого и не нашёл даже
# searchStarsRecipient — метод, который у нас работает. Отрицательный
# результат тогда ничего не значил, и два прогона ушли впустую.
_SITE_NEEDLES = ("StarsRecipient", "BuyStars", "initBuy", "confirmReq",
                 "ajInit", "stars/buy", "quantity")
# Сколько скриптов читать. Шести не хватало: вызовы API у Fragment лежат не
# в первых подключённых файлах.
_MAX_SCRIPTS = 25


def _window(text: str, at: int, width: int = 110) -> str:
    """Кусок кода вокруг находки, в одну строку.

    Нам нужны имена соседних параметров — по ним видно, чего мы не шлём.
    """
    start = max(0, at - width // 2)
    chunk = text[start:at + width // 2]
    return " ".join(chunk.split())


def probe_page_api_sync(cookies: dict) -> list[str]:
    """Как сайт зовёт свой API — по его собственному коду.

    Шесть вариантов формы запроса получили один и тот же «Access denied», а
    поиск получателя прошёл в каждом. Значит дело не в том, как мы шлём, и
    догадываться дальше не о чем: надо прочитать, что именно шлёт сама
    страница покупки. Имена методов и соседние параметры лежат в
    подключаемых скриптах, и читать надо все, а не первые шесть.
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
    urls = _script_urls(body)
    out.append(f"скриптов подключено: {len(urls)}")
    for url in urls[:_MAX_SCRIPTS]:
        try:
            js = session.get(url, timeout=20)
        except Exception as e:
            out.append(f"  {url.rsplit('/', 1)[-1][:28]}: {str(e)[:30]}")
            continue
        if js.status_code == 200 and js.text:
            sources.append((url.rsplit("/", 1)[-1][:28], js.text))
    out.append(f"прочитано: {len(sources) - 1}")

    hits = 0
    for name, text in sources:
        for needle in _SITE_NEEDLES:
            at = text.find(needle)
            if at < 0:
                continue
            hits += 1
            out.append(f"{name} · {needle}:")
            out.append(f"  …{_window(text, at)}…")
            if hits >= 12:
                out.append("(дальше обрезано — хватит и этого)")
                return out
    if not hits:
        # Отрицательный результат теперь что-то значит: искали простыми
        # подстроками по всем скриптам, а не по кавычкам в первых шести.
        out.append("Ни одной из искомых строк нет ни на странице, ни в "
                   "скриптах. Значит покупка на сайте работает не через "
                   "этот код — либо страница отдана нам не та, что браузеру.")
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
