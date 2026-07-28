"""YooMarket panel automation: email OTP login."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

PANEL_URL = "https://panel.yoomarket.net"
# The seller panel has no login of its own for sellers: you sign in on the
# marketplace itself (email → code from the letter) and the panel picks that
# session up, because the cookies are issued for the shared .yoomarket.net
# domain. So the code is requested here, not from PANEL_URL.
MAIN_URL = "https://yoomarket.net"
# The storefront is a Next.js app, so its login form talks to a separate API
# host rather than posting to itself — this is the one the bot already uses.
API_URL = "https://api.yoo.market"


def _esc(value) -> str:
    """Escape text that came from a web page before it goes into a Telegram
    HTML-parsed message. Raw page dumps contain tags like <!doctype html>,
    which Telegram rejects — the send then fails and the user is left staring
    at the previous "in progress" message."""
    import html as _html
    return _html.escape(str(value), quote=False)


async def try_token_login(api_token: str) -> tuple[bool, str]:
    """
    Try to log into the panel using the existing API token (token exchange / SSO).
    Returns (True, cookie_string) on success, (False, error) on failure.
    """
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=12)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    }

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        # Attempt 1: POST token to common auth endpoints
        token_payloads = [
            (PANEL_URL + "/api/auth/token",       {"token": api_token}),
            (PANEL_URL + "/api/auth/api-token",   {"api_token": api_token}),
            (PANEL_URL + "/api/login/token",      {"token": api_token}),
            (PANEL_URL + "/api/auth",             {"token": api_token, "type": "api"}),
            (PANEL_URL + "/api/v1/auth/token",    {"token": api_token}),
        ]
        for url, payload in token_payloads:
            try:
                async with session.post(url, json=payload, timeout=timeout, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    if resp.status in (200, 201):
                        not_login = all(k not in final_url for k in ("/login", "/auth", "/signin"))
                        cookies = _extract_cookies(session, PANEL_URL)
                        if cookies and not_login:
                            logger.info("Token login succeeded via POST %s", url)
                            return True, cookies
                        # JSON token in response body
                        try:
                            import json as _json_mod
                            data = _json_mod.loads(await resp.text())
                            for key in ("token", "access_token", "session_token", "api_token"):
                                if data.get(key):
                                    return True, cookies or f"{key}={data[key]}"
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Token auth attempt %s: %s", url, e)

        # Attempt 2: GET redirect (SSO-style)
        sso_urls = [
            PANEL_URL + f"/auth/token?token={api_token}",
            PANEL_URL + f"/login?api_token={api_token}",
            PANEL_URL + f"/sso?token={api_token}",
        ]
        for url in sso_urls:
            try:
                async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    not_login = all(k not in final_url for k in ("/login", "/auth", "/signin"))
                    if resp.status == 200 and not_login:
                        cookies = _extract_cookies(session, PANEL_URL)
                        if cookies:
                            logger.info("Token login (SSO) succeeded via GET %s", url)
                            return True, cookies
            except Exception as e:
                logger.debug("SSO attempt %s: %s", url, e)

        # Attempt 3: Bearer header on panel root
        try:
            async with session.get(
                PANEL_URL + "/",
                headers={**headers, "Authorization": f"Bearer {api_token}"},
                timeout=timeout,
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                not_login = all(k not in final_url for k in ("/login", "/auth", "/signin"))
                if resp.status == 200 and not_login:
                    cookies = _extract_cookies(session, PANEL_URL)
                    if cookies:
                        return True, cookies
        except Exception as e:
            logger.debug("Bearer panel attempt: %s", e)

    return False, ""


class YooMarketPanelHTTP:
    """
    Email OTP login for panel.yoomarket.net.

    Flow (exactly as seen on the login page):
      1. send_code(email)   → "Код для авторизации отправлен на почту"
      2. verify_code(code)  → returns (True, cookie_string) or (False, error)
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._email: str = ""
        self._csrf: str = ""
        # The send-code path that actually worked — used to derive the matching
        # verify path (same base) in verify_code().
        self._send_path: str = ""
        # A path that answered "code required" during the scan — that is the
        # verify endpoint itself, so verify_code() should try it first.
        self._verify_path: str = ""
        # Host that actually handles the login (the marketplace, normally).
        self._auth_host: str = MAIN_URL

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False, limit=16)
        # Origin/Referer must match the host being called — pinning them to the
        # panel made every request to the marketplace look cross-origin, which
        # Laravel rejects. They are set per request in _host_headers() instead.
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def _host_headers(self, base: str) -> dict:
        """Origin/Referer for the host actually being called."""
        return {"Origin": base, "Referer": base + "/login"}

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def _xsrf_token(self) -> str:
        """Extract XSRF-TOKEN cookie value (URL-decoded) for X-XSRF-TOKEN header."""
        import urllib.parse
        jar = self._session.cookie_jar.filter_cookies(PANEL_URL)
        for name, cookie in jar.items():
            if name.upper() in ("XSRF-TOKEN", "CSRF-TOKEN", "_TOKEN"):
                return urllib.parse.unquote(cookie.value)
        return self._csrf

    async def _prepare(self) -> dict:
        """CSRF handshake against the panel; returns headers for the auth POSTs."""
        timeout = aiohttp.ClientTimeout(total=15, connect=8)
        for path in ("/sanctum/csrf-cookie", "/csrf-cookie"):
            try:
                async with self._session.get(
                    PANEL_URL + path, timeout=timeout,
                    headers=self._host_headers(PANEL_URL),
                ) as resp:
                    if resp.status < 400:
                        break
            except Exception:
                continue
        try:
            async with self._session.get(
                PANEL_URL + "/login", timeout=timeout,
                headers=self._host_headers(PANEL_URL),
            ) as resp:
                html = await resp.text()
            for pattern in (
                r'"csrfToken"\s*:\s*"([^"]+)"',
                r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
            ):
                m = re.search(pattern, html)
                if m:
                    self._csrf = m.group(1)
                    break
        except Exception as e:
            logger.warning("GET /login failed: %s", e)

        hdrs = dict(self._host_headers(PANEL_URL))
        xsrf = self._xsrf_token()
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf
        return hdrs

    async def send_code(self, email: str) -> tuple[bool, str]:
        """
        Ask the panel to mail a login code.

        Endpoint and payload were captured from the site itself: it posts
        {"email": ..., "code": ""} to /token — an empty code field is what
        marks the request as "send me one". No amount of path guessing would
        have found that shape.

        Returns (True, '') or (False, error).
        """
        if not self._session:
            return False, "Сессия не запущена"

        self._email = email.strip()
        self._auth_host = PANEL_URL
        self._verify_path = "/code"
        timeout = aiohttp.ClientTimeout(total=25, connect=10)
        hdrs = await self._prepare()

        attempts = [
            ("/token", {"email": self._email, "code": ""}),
            ("/token", {"email": self._email}),
        ]
        detail = []
        for path, payload in attempts:
            try:
                async with self._session.post(
                    PANEL_URL + path, json=payload, headers=hdrs,
                    timeout=timeout, allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    logger.info("send_code POST %s → %s: %s",
                                path, resp.status, text[:200])
                    if resp.status in (200, 201, 204, 302):
                        self._send_path = path
                        return True, ""
                    if resp.status == 422:
                        try:
                            msg = (_json.loads(text) or {}).get("message") or ""
                        except Exception:
                            msg = ""
                        detail.append(f"{path}: {_esc(msg or text[:120])}")
                        continue
                    detail.append(f"{path}: HTTP {resp.status} {_esc(text[:100])}")
            except Exception as e:
                detail.append(f"{path}: {_esc(str(e)[:80])}")

        return False, "Панель не приняла запрос кода.\n" + "\n".join(detail[:4])




    async def verify_code(self, code: str) -> tuple[bool, str]:
        """
        Submit the emailed code to /code and return the session cookies.

        The site sends the code as a NUMBER, not a string — captured from its
        own request — so that shape is tried first.
        Returns (True, cookie_string) or (False, error).
        """
        if not self._session:
            return False, "Сессия не запущена"

        timeout = aiohttp.ClientTimeout(total=25, connect=10)
        hdrs = dict(self._host_headers(PANEL_URL))
        xsrf = self._xsrf_token()
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf

        raw = code.strip()
        payloads = []
        if raw.isdigit():
            payloads.append({"email": self._email, "code": int(raw)})
        payloads.append({"email": self._email, "code": raw})

        last = ""
        for payload in payloads:
            try:
                async with self._session.post(
                    PANEL_URL + (self._verify_path or "/code"),
                    json=payload, headers=hdrs,
                    timeout=timeout, allow_redirects=True,
                ) as resp:
                    text = await resp.text()
                    logger.info("verify_code → %s: %s", resp.status, text[:200])
                    if resp.status in (200, 201, 204, 302):
                        cookies = _extract_all_cookies(self._session)
                        if cookies and await self._session_ok():
                            return True, cookies
                        if cookies:
                            # Cookies issued but the panel API did not accept
                            # them yet — still worth storing, the caller checks.
                            return True, cookies
                        try:
                            data = _json.loads(text)
                            for key in ("token", "access_token", "api_token"):
                                if data.get(key):
                                    return True, f"{key}={data[key]}"
                        except Exception:
                            pass
                        last = "вход прошёл, но сессия не выдана"
                    elif resp.status == 422:
                        try:
                            last = (_json.loads(text) or {}).get("message") or ""
                        except Exception:
                            last = _esc(text[:150])
                    else:
                        last = f"HTTP {resp.status}: {_esc(text[:120])}"
            except Exception as e:
                last = _esc(str(e)[:100])

        return False, last or "Неверный код или срок действия истёк."

    async def _session_ok(self) -> bool:
        """True if the current cookie jar is an authenticated panel session."""
        timeout = aiohttp.ClientTimeout(total=10)
        hdrs = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        for path in ("/nova-api/navigation", "/api/user"):
            try:
                async with self._session.get(
                    PANEL_URL + path, headers=hdrs, timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status in (401, 403):
                        return False
            except Exception:
                continue
        try:
            async with self._session.get(
                PANEL_URL + "/", timeout=timeout, allow_redirects=True,
            ) as resp:
                final = str(resp.url)
                return not any(k in final for k in ("/login", "/code", "/auth"))
        except Exception:
            return False



def _extract_all_cookies(session: aiohttp.ClientSession) -> str:
    """Merge cookies visible to both hosts.

    Signing in on the marketplace sets cookies for the shared .yoomarket.net
    domain, which is exactly why the panel accepts that session — so collect
    from both and let the panel-scoped ones win.
    """
    merged: dict[str, str] = {}
    for url in (MAIN_URL, PANEL_URL):
        try:
            for name, cookie in session.cookie_jar.filter_cookies(url).items():
                if cookie.value:
                    merged[name] = cookie.value
        except Exception:
            continue
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def _extract_cookies(session: aiohttp.ClientSession, url: str) -> str:
    """Pull all cookies matching the given URL from an aiohttp session."""
    jar = session.cookie_jar.filter_cookies(url)
    parts = [f"{name}={cookie.value}" for name, cookie in jar.items()]
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Synchronous panel product creation (runs in a thread via run_in_executor)
# Uses `requests` with real socket timeouts so it never blocks the event loop.
# ---------------------------------------------------------------------------

def _make_panel_requests_session(cookie_string: str):
    """Build a `requests` session pre-loaded with panel cookies and headers."""
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    session = requests.Session()
    session.verify = False
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                session.cookies.set(k.strip(), v.strip(), domain="panel.yoomarket.net")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PANEL_URL + "/",
    })
    return session


def _parse_nova_fields_payload(cf: dict) -> list[dict]:
    """Extract the field list from a Nova creation-fields response.
    Handles list, numeric-keyed dict, and panel-nested shapes."""
    raw = cf.get("fields")
    if isinstance(raw, dict):
        raw_fields = list(raw.values())
    elif isinstance(raw, list):
        raw_fields = list(raw)
    else:
        raw_fields = []
    if not raw_fields and isinstance(cf.get("panels"), list):
        for p in cf["panels"]:
            pf = p.get("fields") if isinstance(p, dict) else None
            if isinstance(pf, dict):
                raw_fields.extend(pf.values())
            elif isinstance(pf, list):
                raw_fields.extend(pf)
    return [f for f in raw_fields if isinstance(f, dict)]


def _normalize_options(field: dict) -> list[dict]:
    """Return [{'label': str, 'value': any}, ...] from a Nova select field."""
    options = field.get("options")
    if not options:
        options = (field.get("meta") or {}).get("options")
    if not options:
        return []
    result = []
    if isinstance(options, dict):
        options = [{"label": str(v), "value": k} for k, v in options.items()]
    if isinstance(options, list):
        for o in options:
            if isinstance(o, dict):
                val = o.get("value", o.get("id"))
                label = o.get("label") or o.get("display") or o.get("name") or str(val)
                result.append({"label": str(label), "value": val})
            else:
                result.append({"label": str(o), "value": o})
    return result


def panel_get_item_form_sync(cookie_string: str) -> tuple[bool, object]:
    """
    Blocking: fetch the product creation form (fields + select options).
    Returns (True, {"resource": str, "fields": [{attribute,label,options,required,dependsOn}]})
    or (False, error_message).
    """
    session = _make_panel_requests_session(cookie_string)
    last_err = "форма не найдена"
    for res in ("items", "ads", "goods", "products"):
        try:
            r = session.get(
                f"{PANEL_URL}/nova-api/{res}/creation-fields?editing=true&editMode=create",
                timeout=(6, 10), allow_redirects=False,
            )
        except Exception as e:
            last_err = str(e)[:60]
            continue
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code != 200:
            continue
        try:
            cf = r.json()
        except Exception:
            continue
        if not isinstance(cf, dict):
            continue
        fields = _parse_nova_fields_payload(cf)
        if not fields:
            continue
        form_fields = []
        for f in fields:
            attr = f.get("attribute")
            if not attr:
                continue
            form_fields.append({
                "attribute": attr,
                "label": f.get("name") or f.get("indexName") or attr,
                "options": _normalize_options(f),
                "required": "required" in str(f.get("rules", [])),
                "dependsOn": f.get("dependsOn"),
                "component": f.get("component", ""),
                "relationship": f.get("relationshipType") or f.get("belongsToRelationship") or "",
            })
        return True, {"resource": res, "fields": form_fields}
    return False, last_err


def panel_sync_field_options_sync(
    cookie_string: str, resource: str, field_attr: str, form_values: dict,
    search: str = "",
) -> tuple[list[dict], str]:
    """
    Blocking: fetch options for a select that has none inline.
    Tries, in order:
      1. /nova-api/{res}/associatable/{attr} — BelongsTo relation options
      2. /nova-api/{res}/creation-fields?field={attr}&... — dependsOn sync
    `search` narrows associatable results (the endpoint caps unfiltered lists).
    Returns (options, debug_trace). options=[] if nothing worked.
    """
    session = _make_panel_requests_session(cookie_string)
    trace: list[str] = []

    # 1. BelongsTo options endpoint (Nova "associatable")
    assoc_params = {
        "search": search, "first": "false", "withTrashed": "false",
        "editing": "true", "editMode": "create",
    }
    for k, v in form_values.items():
        assoc_params[k] = str(v)
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/{resource}/associatable/{field_attr}",
            params=assoc_params, timeout=(6, 10), allow_redirects=False,
        )
        trace.append(f"associatable/{field_attr}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            rows = data.get("resources") if isinstance(data, dict) else None
            if isinstance(rows, list) and rows:
                opts = []
                for row in rows:
                    if isinstance(row, dict):
                        val = row.get("value", row.get("id"))
                        label = row.get("display") or row.get("title") or str(val)
                        opts.append({"label": str(label), "value": val})
                if opts:
                    return opts, "; ".join(trace)
            trace.append(f"body={r.text[:120]}")
    except Exception as e:
        trace.append(f"associatable: {str(e)[:40]}")

    # 2. dependsOn field sync via creation-fields
    params = {"editing": "true", "editMode": "create", "field": field_attr}
    for k, v in form_values.items():
        params[k] = str(v)
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/{resource}/creation-fields",
            params=params, timeout=(6, 10), allow_redirects=False,
        )
        trace.append(f"creation-fields?field: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                candidates = _parse_nova_fields_payload(data) or (
                    [data] if data.get("attribute") else []
                )
                for f in candidates:
                    if f.get("attribute") == field_attr:
                        opts = _normalize_options(f)
                        if opts:
                            return opts, "; ".join(trace)
                trace.append(f"body={r.text[:120]}")
    except Exception as e:
        trace.append(f"creation-fields: {str(e)[:40]}")

    return [], "; ".join(trace)


def _save_refreshed_cookies(uid: int | None, cookie_string: str, session) -> None:
    """Merge rotated Laravel session cookies back into storage so the panel
    session keeps extending instead of aging out from the original login."""
    if uid is None:
        return
    try:
        from storage import get_panel_creds, save_panel_creds
        merged: dict[str, str] = {}
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip():
                    merged[k.strip()] = v.strip()
        for c in session.cookies:
            if c.value:
                merged[c.name] = c.value
        creds = get_panel_creds(uid) or {}
        creds["cookies"] = "; ".join(f"{k}={v}" for k, v in merged.items())
        save_panel_creds(uid, creds)
    except Exception:
        logger.debug("cookie refresh save failed", exc_info=True)


def _panel_xsrf_headers(session, cookie_string: str) -> dict:
    """CSRF handshake + build X-XSRF-TOKEN headers for a requests session."""
    import urllib.parse
    try:
        session.get(PANEL_URL + "/sanctum/csrf-cookie",
                    timeout=(6, 10), allow_redirects=False)
    except Exception:
        pass
    raw = ""
    for c in session.cookies:
        if c.name in ("XSRF-TOKEN", "CSRF-TOKEN") and c.value:
            raw = c.value
    if not raw:
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip().upper() in ("XSRF-TOKEN", "CSRF-TOKEN"):
                    raw = v.strip()
                    break
    hdrs = {}
    if raw:
        hdrs["X-XSRF-TOKEN"] = urllib.parse.unquote(raw)
    return hdrs


_PUBLISH_KWS = ("публик", "publish", "актив", "activ", "показ", "вкл",
                "enable", "visib", "выстав", "размест")
_UNPUBLISH_KWS = ("скрыт", "скрыть", "hide", "unpublish", "деактив", "снять",
                  "приостан", "disable", "выключ", "stop")
_DANGEROUS_KWS = ("удал", "delete", "destroy", "force")
# Raising a listing back to the top of the feed. The Integration API has no
# such method at all, but the panel does — it is a Nova action, reachable the
# same way publishing is.
# On this marketplace there is no plain "raise" action — promotion is the paid
# «Премиум» action, so that is what bumping maps to. It SPENDS MONEY, which is
# why callers must pass confirm=True and the daily ceiling is enforced.
_BUMP_KWS = ("премиум", "premium", "подня", "поднят", "bump", "raise",
             "продвин", "буст", "boost")


def _build_update_form(fields: list[dict], overrides: dict) -> dict:
    """Form data for a Nova _method=PUT update: preserve every current value
    (media re-attached by id as __media__[attr][i]) and apply overrides."""
    form: dict = {}
    for f in fields:
        fa = f.get("attribute")
        if not fa:
            continue
        val = f.get("value")
        comp = str(f.get("component") or "")
        if "media" in comp or fa == "images":
            if isinstance(val, list):
                for i, m in enumerate(val):
                    mid = m.get("id") if isinstance(m, dict) else m
                    if mid is not None:
                        form[f"__media__[{fa}][{i}]"] = str(mid)
            continue
        if fa in overrides:
            continue  # заполним из overrides ниже
        if val is None:
            continue
        if isinstance(val, bool):
            form[fa] = "1" if val else "0"
        elif isinstance(val, (dict, list)):
            continue
        else:
            form[fa] = str(val)
    for k, v in overrides.items():
        if v is None:
            continue
        form[k] = ("1" if v else "0") if isinstance(v, bool) else str(v)
    form["_method"] = "PUT"
    return form


def _get_update_fields(session, hdrs, item_id: str) -> tuple[list[dict], str]:
    """GET update-fields for an item. Returns (fields, error)."""
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items/{item_id}/update-fields"
            f"?editing=true&editMode=update",
            headers=hdrs, timeout=(6, 10), allow_redirects=False,
        )
        if r.status_code == 401:
            return [], "401: сессия истекла"
        if r.status_code != 200:
            return [], f"update-fields: {r.status_code}"
        cf = r.json()
        fields = _parse_nova_fields_payload(cf) if isinstance(cf, dict) else []
        return fields, "" if fields else "пустые поля"
    except Exception as e:
        return [], str(e)[:60]


def _put_item(session, hdrs, item_id: str, form: dict):
    return session.post(
        f"{PANEL_URL}/nova-api/items/{item_id}?editing=true&editMode=update",
        data=form, headers=hdrs, timeout=(6, 15),
    )


def panel_update_item_sync(
    cookie_string: str, item_id: str, overrides: dict, uid: int | None = None,
) -> tuple[bool, str]:
    """Blocking: change item fields (e.g. {'price': 199, 'title': ...}),
    preserving everything else including photos."""
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    fields, err = _get_update_fields(session, hdrs, item_id)
    if not fields:
        return False, f"не получил поля товара: {err}"
    form = _build_update_form(fields, overrides)
    try:
        resp = _put_item(session, hdrs, item_id, form)
    except Exception as e:
        return False, f"ошибка запроса: {str(e)[:60]}"
    _save_refreshed_cookies(uid, cookie_string, session)
    if resp.status_code in (200, 201, 204):
        return True, "обновлено"
    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def panel_publish_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
    public: bool = True,
) -> tuple[bool, str]:
    """
    Blocking: make an item public (or hide it with public=False).
    1. Run a matching Nova action (публиковать/скрыть...) if the panel has one.
    2. Fallback: flip a public/visible/active flag via PUT update, preserving
       current values (including media ids as __media__[images][i]).
    Returns (ok, human_message_with_diagnostics).
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    trace: list[str] = []
    want_kws = _PUBLISH_KWS if public else _UNPUBLISH_KWS
    avoid_kws = (_UNPUBLISH_KWS if public else _PUBLISH_KWS) + _DANGEROUS_KWS

    # --- 1. Nova actions -----------------------------------------------------
    actions: list[dict] = []
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items/actions",
            params={"resources": str(item_id)},
            headers=hdrs, timeout=(6, 10), allow_redirects=False,
        )
        trace.append(f"actions: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                actions = [a for a in (data.get("actions") or [])
                           if isinstance(a, dict)]
    except Exception as e:
        trace.append(f"actions: {str(e)[:40]}")

    action_names = [(a.get("name") or a.get("uriKey") or "?") for a in actions]
    for a in actions:
        name = str(a.get("name") or "").lower()
        key = str(a.get("uriKey") or "")
        if not key:
            continue
        blob = name + " " + key.lower()
        if any(kw in blob for kw in avoid_kws):
            continue
        if any(kw in blob for kw in want_kws):
            try:
                resp = session.post(
                    f"{PANEL_URL}/nova-api/items/action?action={key}",
                    data={"resources": str(item_id)},
                    headers=hdrs, timeout=(6, 15),
                )
                trace.append(f"action {key}: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    _save_refreshed_cookies(uid, cookie_string, session)
                    return True, f"через действие «{a.get('name') or key}»"
            except Exception as e:
                trace.append(f"action {key}: {str(e)[:40]}")

    # --- 2. Flag flip via update ---------------------------------------------
    fields, err = _get_update_fields(session, hdrs, item_id)
    if err:
        trace.append(err)
    if fields:
        flag_kws = ("public", "visible", "active", "hidden", "enabled",
                    "status", "публик", "видим", "актив", "показ")
        flag = next(
            (f for f in fields
             if any(kw in str(f.get("attribute", "")).lower()
                    or kw in str(f.get("name", "")).lower()
                    for kw in flag_kws)),
            None,
        )
        if flag is None:
            trace.append(
                f"нет флага публикации среди {[f.get('attribute') for f in fields]}")
        else:
            attr = flag["attribute"]
            inverted = "hidden" in str(attr).lower() or "скрыт" in str(
                flag.get("name", "")).lower()
            on = public != inverted  # обычный флаг: public → 1; hidden: public → 0
            form = _build_update_form(fields, {attr: on})
            try:
                resp = _put_item(session, hdrs, item_id, form)
                trace.append(f"PUT {attr}: {resp.status_code} {resp.text[:80]}")
                if resp.status_code in (200, 201, 204):
                    _save_refreshed_cookies(uid, cookie_string, session)
                    return True, f"через поле «{flag.get('name') or attr}»"
            except Exception as e:
                trace.append(f"PUT: {str(e)[:50]}")

    _save_refreshed_cookies(uid, cookie_string, session)
    verb = "публикации" if public else "скрытия"
    return False, (
        f"не нашёл способ {verb}.\n"
        f"Доступные действия: <code>{action_names or 'нет'}</code>\n"
        f"Лог: <code>{'; '.join(trace)[:350]}</code>"
    )


def panel_bump_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
    confirm: bool = False,
) -> tuple[bool, str]:
    """Blocking: promote one listing via the panel's «Премиум» Nova action.

    This is a PAID action on Yoomarket, so it refuses to run unless the caller
    passes confirm=True. If the action needs parameters, they are reported
    rather than guessed — submitting arbitrary values could buy the wrong
    placement.

    Returns (ok, message_or_diagnostics).
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    trace: list[str] = []

    actions: list[dict] = []
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items/actions",
            params={"resources": str(item_id)},
            headers=hdrs, timeout=(6, 10), allow_redirects=False,
        )
        trace.append(f"actions: {r.status_code}")
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                actions = [a for a in (data.get("actions") or [])
                           if isinstance(a, dict)]
    except Exception as e:
        return False, f"не получил список действий: {str(e)[:60]}"

    names = [(a.get("name") or a.get("uriKey") or "?") for a in actions]
    for a in actions:
        name = str(a.get("name") or "").lower()
        key = str(a.get("uriKey") or "")
        if not key:
            continue
        blob = name + " " + key.lower()
        if any(kw in blob for kw in _DANGEROUS_KWS + _UNPUBLISH_KWS):
            continue
        if not any(kw in blob for kw in _BUMP_KWS):
            continue

        if not confirm:
            return False, (
                f"«{a.get('name') or key}» — платное действие. "
                f"Запуск только с подтверждением."
            )

        # Report required parameters instead of inventing values for a
        # purchase (duration, placement, budget, ...).
        fields = [f for f in (a.get("fields") or []) if isinstance(f, dict)]
        required = [
            f.get("name") or f.get("attribute")
            for f in fields
            if "required" in str(f.get("rules", []))
        ]
        if required:
            return False, (
                f"«{a.get('name') or key}» требует параметры: {required}. "
                f"Запустите его в панели — я не подставляю значения для платной "
                f"операции."
            )

        payload = {"resources": str(item_id)}
        for f in fields:
            attr = f.get("attribute")
            val = f.get("value")
            if attr and val not in (None, ""):
                payload[attr] = val
        try:
            resp = session.post(
                f"{PANEL_URL}/nova-api/items/action?action={key}",
                data=payload, headers=hdrs, timeout=(6, 15),
            )
            trace.append(f"action {key}: {resp.status_code} {resp.text[:80]}")
            if resp.status_code in (200, 201, 204):
                _save_refreshed_cookies(uid, cookie_string, session)
                return True, str(a.get("name") or key)
        except Exception as e:
            trace.append(f"action {key}: {str(e)[:40]}")

    _save_refreshed_cookies(uid, cookie_string, session)
    return False, (f"действие продвижения не найдено. "
                   f"Доступные: {names or 'нет'}; лог: {'; '.join(trace)[:200]}")


def panel_bump_all_sync(
    cookie_string: str, uid: int | None = None, confirm: bool = False,
) -> tuple[int, str]:
    """Blocking: raise every listing the panel shows. Returns (count, message)."""
    ok, items = panel_list_items_sync(cookie_string)
    if not ok:
        return 0, f"⚠️ {items}"
    if not items:
        return 0, "ℹ️ Нет объявлений"

    count = 0
    last = ""
    for it in items:
        item_id = it.get("id")
        if not item_id:
            continue
        done, msg = panel_bump_item_sync(cookie_string, item_id, uid, confirm)
        if done:
            count += 1
        else:
            last = msg
            # A missing action will be missing for every item — stop early
            # instead of repeating the same failure for the whole list.
            if any(k in msg for k in ("не найдено", "401", "подтвержд", "требует")):
                break
    if count:
        return count, f"✅ Поднято: {count}"
    return 0, f"⚠️ Не удалось поднять: {last}"


def panel_list_categories_sync(cookie_string: str) -> tuple[bool, object]:
    """Blocking: the seller's categories, derived from their own listings.
    Returns (True, [{"name": str, "count": int}]) or (False, error)."""
    ok, items = panel_list_items_sync(cookie_string)
    if not ok:
        return False, items
    counts: dict[str, int] = {}
    for it in items:
        cat = (it.get("category") or "").strip() or "Без категории"
        counts[cat] = counts.get(cat, 0) + 1
    cats = [{"name": k, "count": v} for k, v in counts.items()]
    cats.sort(key=lambda c: (-c["count"], c["name"]))
    return True, cats


# Attributes that name a real category; anything else that is a belongsTo is
# accepted as a weaker fallback for grouping.
_CAT_ATTRS = ("category", "subcategory", "categories", "type", "ad", "game",
              "group", "adGroup", "ad_group", "product")


def _strip_html(value) -> str:
    """Plain text out of a Nova ComputedField, which renders raw HTML."""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", " ", value)
    import html as _h
    return " ".join(_h.unescape(text).split())


def _html_badges(value) -> list[str]:
    """Texts of the <span> badges inside a ComputedField.

    The listing rows carry no category field; what a seller reads as the
    category lives in these badges of the «Детали» column.
    """
    if not isinstance(value, str):
        return []
    import html as _h
    out = []
    for chunk in re.findall(r"<span[^>]*>(.*?)</span>", value, re.S | re.I):
        txt = " ".join(_h.unescape(re.sub(r"<[^>]+>", " ", chunk)).split())
        if txt:
            out.append(txt)
    return out


def panel_list_items_sync(cookie_string: str) -> tuple[bool, object]:
    """Blocking: list items from the panel. Returns (True, [{id,title,price}])
    or (False, error)."""
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items",
            params={"perPage": "50"},
            headers=hdrs, timeout=(6, 12), allow_redirects=False,
        )
    except Exception as e:
        return False, f"ошибка запроса: {str(e)[:60]}"
    if r.status_code == 401:
        return False, "401: сессия панели истекла — войдите снова"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:150]}"
    try:
        data = r.json()
    except Exception:
        return False, f"невалидный JSON: {r.text[:120]}"

    items = []
    for res in (data.get("resources") or []):
        if not isinstance(res, dict):
            continue
        rid = res.get("id")
        if isinstance(rid, dict):
            rid = rid.get("value")
        raw_fields = res.get("fields")
        if isinstance(raw_fields, dict):
            raw_fields = list(raw_fields.values())
        # Nova puts the row's display name on the resource itself; relying only
        # on a field called "title" left every item labelled "Товар <id>".
        row_title = res.get("title") or res.get("display") or ""
        if isinstance(row_title, dict):
            row_title = row_title.get("value") or ""
        info = {"id": str(rid), "title": str(row_title or ""), "price": "",
                "public": None, "category": "", "stock": None, "badges": []}
        cat_rank = 99  # lower = better source for the grouping label
        seen_attrs: list[str] = []
        _vis_kws = ("public", "visible", "active", "published", "status",
                    "hidden", "публич", "видим", "актив", "показ", "скрыт")
        for f in raw_fields or []:
            if not isinstance(f, dict):
                continue
            fa = str(f.get("attribute", ""))
            fn = str(f.get("name", ""))
            if fa:
                seen_attrs.append(fa)
            if not info["title"] and (
                    fa in ("title", "name", "naimenovanie")
                    or "title" in fa.lower() or "name" in fa.lower()
                    or "назван" in fn.lower() or "наимен" in fn.lower()):
                v = f.get("value")
                if isinstance(v, dict):
                    v = v.get("title") or v.get("name") or v.get("display")
                if v not in (None, ""):
                    info["title"] = str(v)
            elif not info["price"] and (
                    "price" in fa.lower() or "цен" in fn.lower()
                    or "стоим" in fn.lower()):
                # Rendered as HTML by ComputedField: "<div ...>149 ₽</div>"
                txt = _strip_html(f.get("value")).replace("₽", "").strip()
                if txt:
                    info["price"] = txt
            elif "детал" in fn.lower() or "detail" in fa.lower():
                # Badges hold what the seller reads as the category
                badges = _html_badges(f.get("value"))
                if badges and cat_rank > 1:
                    info["category"] = badges[0]
                    cat_rank = 1
                    info["badges"] = badges[:6]
            elif (fa in _CAT_ATTRS or "категор" in fn.lower()
                  or f.get("belongsToRelationship") or f.get("belongsToId")):
                # The grouping value is a belongsTo. On this panel an item
                # hangs off a parent listing (game/product), not off a field
                # literally called "category", so any belongsTo counts —
                # ranked, so a real category still wins over the parent.
                v = f.get("value")
                label = ""
                if isinstance(v, dict):
                    label = str(v.get("title") or v.get("name")
                                or v.get("display") or v.get("label") or "")
                elif v not in (None, ""):
                    label = str(v)
                if label:
                    rank = 0 if fa in _CAT_ATTRS or "категор" in fn.lower() else 1
                    if rank < cat_rank:
                        info["category"] = label
                        cat_rank = rank
            elif any(k in fa.lower() or k in fn.lower()
                     for k in ("count", "quantity", "stock", "остат", "количеств")):
                try:
                    info["stock"] = int(float(str(f.get("value"))))
                except (TypeError, ValueError):
                    pass
            elif info["public"] is None and any(
                    kw in fa.lower() or kw in fn.lower() for kw in _vis_kws):
                val = f.get("value")
                inverted = "hidden" in fa.lower() or "скрыт" in fn.lower()
                if isinstance(val, bool):
                    info["public"] = (not val) if inverted else val
                elif isinstance(val, str):
                    on = val.lower() in ("1", "true", "да", "active", "published",
                                         "public", "on", "visible")
                    info["public"] = (not on) if inverted else on
                elif isinstance(val, (int, float)):
                    info["public"] = (not bool(val)) if inverted else bool(val)
        if not info["category"] or not info["title"]:
            logger.info(
                "item %s: title=%r category=%r; attrs=%s",
                info["id"], info["title"], info["category"], seen_attrs[:20])
        if info["id"] and info["id"] != "None":
            items.append(info)
    return True, items


def panel_delete_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
) -> tuple[bool, str]:
    """Blocking: delete an item via the standard Nova delete endpoint."""
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.delete(
            f"{PANEL_URL}/nova-api/items",
            params={"resources[]": str(item_id)},
            headers=hdrs, timeout=(6, 15),
        )
    except Exception as e:
        return False, f"ошибка запроса: {str(e)[:60]}"
    _save_refreshed_cookies(uid, cookie_string, session)
    if r.status_code in (200, 204):
        return True, "удалён"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def _find_image_url(value) -> str:
    """Dig a usable http image URL out of a media field value."""
    if isinstance(value, list):
        for item in value:
            url = _find_image_url(item)
            if url:
                return url
    elif isinstance(value, dict):
        for key in ("original_url", "url", "preview_url", "full_url"):
            v = value.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in value.values():
            url = _find_image_url(v)
            if url:
                return url
    elif isinstance(value, str) and value.startswith("http") and any(
            ext in value.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
        return value
    return ""


def _field_submit_value(f: dict):
    """Get a resubmittable scalar for a Nova field value.
    BelongsTo/select fields expose the chosen id in `belongsToId` or as a nested
    object in `value` — a plain str(value) would send the label and break the
    create (this is why clone lost category/subcategory/type → 422)."""
    if f.get("belongsToId") is not None:
        return f.get("belongsToId")
    rel = str(f.get("relationshipType") or f.get("belongsToRelationship") or "")
    val = f.get("value")
    if isinstance(val, dict):
        for k in ("id", "value", "key"):
            if val.get(k) is not None:
                return val[k]
        return None
    if rel and isinstance(val, (str, int)) and val != "":
        return val
    return val


def panel_clone_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
) -> tuple[bool, str]:
    """Blocking: create a copy of an item — same fields, photo re-downloaded
    from the panel and re-uploaded to the new item."""
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    fields, err = _get_update_fields(session, hdrs, item_id)
    if not fields:
        return False, f"не получил поля товара: {err}"

    form: dict = {}
    img_bytes = b""
    for f in fields:
        fa = f.get("attribute")
        if not fa:
            continue
        val = f.get("value")
        comp = str(f.get("component") or "")
        if "media" in comp or fa == "images":
            url = _find_image_url(val)
            if url:
                try:
                    ir = session.get(url, timeout=(6, 20))
                    if ir.status_code == 200:
                        img_bytes = ir.content
                except Exception:
                    pass
            continue
        sub = _field_submit_value(f)
        if sub is None or sub == "":
            continue
        if isinstance(sub, bool):
            form[fa] = "1" if sub else "0"
        elif isinstance(sub, (dict, list)):
            continue  # nested structure we can't resubmit
        else:
            form[fa] = str(sub)

    files = {}
    if img_bytes:
        files["__media__[images][0]"] = ("clone.jpg", img_bytes, "image/jpeg")

    try:
        resp = session.post(
            f"{PANEL_URL}/nova-api/items?editing=true&editMode=create",
            data=form, files=files or None, headers=hdrs, timeout=(6, 30),
        )
    except Exception as e:
        return False, f"ошибка запроса: {str(e)[:60]}"
    _save_refreshed_cookies(uid, cookie_string, session)
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except Exception:
            data = {}
        rid = (data.get("resource") or {}).get("id") or data.get("id") or ""
        if isinstance(rid, dict):
            rid = rid.get("value", "")
        return True, str(rid) if rid else "создан"
    if resp.status_code == 422:
        return False, f"422: {resp.text[:300]}"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def panel_create_product_sync(
    cookie_string: str,
    title: str,
    price: int,
    description: str,
    quantity: int = 1,
    category: str = "",
    uid: int | None = None,
    extra: dict | None = None,
    photo_path: str | None = None,
) -> tuple[bool, str]:
    """
    Blocking function — call via loop.run_in_executor().
    Creates a product via the Laravel Nova API using `requests` with socket timeouts.
    If uid is given, refreshed session cookies are saved back to storage.
    `extra` — exact Nova attribute values (e.g. category/subcategory/type ids
    chosen by the user) merged into the payload last, overriding the mapping.
    """
    import urllib.parse
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    CONNECT_TIMEOUT = 6   # seconds to establish TCP connection
    READ_TIMEOUT = 10     # seconds to wait for server response

    session = requests.Session()
    session.verify = False

    # Load cookies from the stored cookie string
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                session.cookies.set(k, v, domain="panel.yoomarket.net")

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PANEL_URL + "/",
    })

    # CSRF handshake
    try:
        session.get(PANEL_URL + "/sanctum/csrf-cookie",
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False)
    except Exception:
        pass

    xsrf = ""
    # Don't use cookies.get(): it raises CookieConflictError when the token
    # exists for several domains (our preset cookie + a fresh server one).
    # Iterate and prefer the last (freshest) match instead.
    raw_xsrf = ""
    for c in session.cookies:
        if c.name in ("XSRF-TOKEN", "CSRF-TOKEN") and c.value:
            raw_xsrf = c.value
    if raw_xsrf:
        xsrf = urllib.parse.unquote(raw_xsrf)
    if not xsrf:
        # fallback: parse from original cookie string
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip().upper() in ("XSRF-TOKEN", "CSRF-TOKEN"):
                    xsrf = urllib.parse.unquote(v.strip())
                    break

    hdrs = {}
    if xsrf:
        hdrs["X-XSRF-TOKEN"] = xsrf

    debug: list[str] = [f"XSRF: {'✓' if xsrf else '✗'}"]

    # Discover Nova resources.
    # NOTE: 401/403 here is recorded but NOT fatal — 403 can mean "no access to
    # this endpoint" while resource endpoints still work fine.
    resources: list[str] = []
    for nav_path in ("/nova-api/navigation", "/nova-api/resources"):
        try:
            resp = session.get(PANEL_URL + nav_path, headers=hdrs,
                               timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                               allow_redirects=False)
            debug.append(f"{nav_path}: {resp.status_code}")
            if resp.status_code == 200:
                found = re.findall(r'"uriKey"\s*:\s*"([^"]+)"', resp.text)
                resources.extend(r for r in found if r not in resources)
        except Exception as e:
            debug.append(f"{nav_path}: {str(e)[:50]}")

    # Scan SPA HTML for resource uriKeys (navigation endpoint 404s on this panel,
    # but resource names leak into the page config / router paths)
    if not resources:
        try:
            html_resp = session.get(
                PANEL_URL + "/", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
            )
            html = html_resp.text
            for r in re.findall(r'"uriKey"\s*:\s*"([^"]+)"', html):
                if r not in resources:
                    resources.append(r)
            for r in re.findall(r'/resources/([a-z0-9_\-]+)', html, re.I):
                if r not in resources:
                    resources.append(r)
            debug.append(f"HTML scan: {resources[:8] or 'ничего'}")
        except Exception as e:
            debug.append(f"HTML scan: {str(e)[:40]}")

    # Candidates to try FIRST: on this panel ad-items belongsTo "ad", so the
    # product entity is almost certainly the "ads" resource
    priority = ["ads", "ad", "items", "goods", "products"]
    for d in reversed(priority):
        if d in resources:
            resources.remove(d)
        resources.insert(0, d)
    for d in ("offers", "lots", "adverts", "listings"):
        if d not in resources:
            resources.append(d)

    # Remove resources that can't be the product form:
    # groups/values/admin panels/events etc.
    def _is_junk(name: str) -> bool:
        n = name.lower()
        if n in ("ad-groups", "ad-group", "categories", "tags", "users",
                 "roles", "permissions", "settings", "logs", "reviews",
                 "orders", "chats", "messages", "notifications"):
            return True
        return any(w in n for w in ("admin", "action-event", "balance", "cabinet"))

    resources = [r for r in resources if not _is_junk(r)]
    debug.append(f"Ресурсы: {resources[:12]}")

    values = {
        "title": title, "price": price,
        "description": description, "quantity": quantity, "category": category,
    }
    _PRICE_KWS = ("price", "cost", "cena", "amount", "sum", "стоим")

    for res in resources[:12]:
        try:
            cf_resp = session.get(
                f"{PANEL_URL}/nova-api/{res}/creation-fields"
                f"?editing=true&editMode=create",
                headers=hdrs, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
            )
        except Exception as e:
            debug.append(f"{res}: connect error {str(e)[:40]}")
            continue

        if cf_resp.status_code == 401:
            return False, (
                "⚠️ <b>Панель не принимает сохранённые куки</b> "
                f"(401 на <code>{res}/creation-fields</code>).\n\n"
                "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова через email."
            )
        if cf_resp.status_code == 403:
            # No permission for THIS resource — not a dead session, keep looking
            debug.append(f"{res}: 403 нет доступа")
            continue
        if cf_resp.status_code == 404:
            debug.append(f"{res}: 404")
            continue
        if cf_resp.status_code != 200:
            debug.append(f"{res}: {cf_resp.status_code}")
            continue

        try:
            cf = cf_resp.json()
        except Exception:
            debug.append(f"{res}: bad JSON: {_esc(cf_resp.text[:100])}")
            continue

        if not isinstance(cf, dict):
            debug.append(f"{res}: not a dict ({type(cf).__name__})")
            continue

        # Nova can return fields as a list, as a numeric-keyed dict
        # ({"fields":{"2":{...}}} — seen on this panel), or nested inside panels
        raw = cf.get("fields")
        if isinstance(raw, dict):
            raw_fields = list(raw.values())
        elif isinstance(raw, list):
            raw_fields = list(raw)
        else:
            raw_fields = []
        if not raw_fields and isinstance(cf.get("panels"), list):
            for p in cf["panels"]:
                pf = p.get("fields") if isinstance(p, dict) else None
                if isinstance(pf, dict):
                    raw_fields.extend(pf.values())
                elif isinstance(pf, list):
                    raw_fields.extend(pf)
        fields = [f for f in raw_fields if isinstance(f, dict)]
        if not fields:
            # Show raw body so we can see the actual response shape
            debug.append(f"{res}: no fields, body={cf_resp.text[:200]}")
            continue

        attrs = [f.get("attribute") for f in fields if f.get("attribute")]

        # Skip resources without a price field — not a product form
        if not any(any(kw in (a or "").lower() for kw in _PRICE_KWS) for a in attrs):
            debug.append(f"⏭ {res}: no price field ({attrs})")
            continue

        debug.append(f"✅ {res}: {attrs}")

        # Build payload respecting max:N rules
        payload: dict = {}
        for f in fields:
            attr = f.get("attribute", "")
            al = attr.lower()
            val = None
            if any(k in al for k in ("title", "name", "header", "naimenov")):
                val = values["title"]
            elif any(k in al for k in ("price", "cost", "cena")):
                val = values["price"]
            elif any(k in al for k in ("desc", "opis", "text", "content")):
                val = values["description"]
            elif any(k in al for k in ("count", "quantity", "qty", "stock", "amount")):
                val = values["quantity"]
            elif any(k in al for k in ("categ", "kategor")):
                val = values["category"] or None
            else:
                dv = f.get("value")
                if dv not in (None, ""):
                    val = dv
            if val is not None and val != "":
                if isinstance(val, str):
                    for rule in (f.get("rules") or []):
                        if isinstance(rule, str) and rule.startswith("max:"):
                            try:
                                val = val[:int(rule.split(":")[1])]
                            except (ValueError, IndexError):
                                pass
                payload[attr] = val
        payload.setdefault("title", values["title"])
        payload.setdefault("price", values["price"])
        # User-chosen select values (category/subcategory/type ids) win over guesses
        if extra:
            for k, v in extra.items():
                if v is not None:
                    payload[k] = v

        store_url = f"{PANEL_URL}/nova-api/{res}?editing=true&editMode=create"
        try:
            import os as _os
            if photo_path and _os.path.exists(photo_path):
                # Multipart: Nova file fields only accept real uploads
                form_data = {}
                for k, v in payload.items():
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        form_data[k] = "1" if v else "0"
                    else:
                        form_data[k] = str(v)
                with open(photo_path, "rb") as fh:
                    img_bytes = fh.read()
                fname = _os.path.basename(photo_path) or "photo.jpg"
                post_resp = None
                # advanced-media-library-field (Spatie Media Library for Nova)
                # expects uploads as __media__[attribute][index]; try that first,
                # then plain Nova file-field key formats as fallbacks
                for file_key in ("__media__[images][0]", "images",
                                 "images[]", "images[0]"):
                    files = {file_key: (fname, img_bytes, "image/jpeg")}
                    post_resp = session.post(
                        store_url, data=form_data, files=files, headers=hdrs,
                        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT + 20),
                    )
                    if post_resp.status_code == 422 and '"images"' in post_resp.text:
                        debug.append(f"POST {res} {file_key}: 422 images")
                        continue
                    break
            else:
                post_resp = session.post(
                    store_url, json=payload, headers=hdrs,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
        except Exception as e:
            debug.append(f"POST {res}: {str(e)[:50]}")
            continue

        if post_resp.status_code in (200, 201):
            try:
                data = post_resp.json()
            except Exception:
                data = {}
            rid = (data.get("resource") or {}).get("id") or data.get("id") or ""
            if isinstance(rid, dict):
                rid = rid.get("value", "")
            _save_refreshed_cookies(uid, cookie_string, session)
            return True, str(rid) if rid else "создан"

        if post_resp.status_code == 422:
            _save_refreshed_cookies(uid, cookie_string, session)
            try:
                err_body = post_resp.json()
                err_fields = err_body.get("errors") or err_body.get("message") or post_resp.text[:300]
            except Exception:
                err_fields = post_resp.text[:300]
            err_str = (
                _json.dumps(err_fields, ensure_ascii=False)[:400]
                if isinstance(err_fields, dict)
                else str(err_fields)[:400]
            )
            required = [
                f.get("attribute") for f in fields
                if f.get("attribute") and "required" in str(f.get("rules", []))
            ]
            # If images failed — show the field definition to identify the component
            img_diag = ""
            if "images" in err_str:
                import os as _os2
                photo_stat = (
                    f"фото: {'✓ ' + str(_os2.path.getsize(photo_path)) + 'б' if photo_path and _os2.path.exists(photo_path) else '✗ не приложено'}"
                )
                img_field = next(
                    (f for f in fields if f.get("attribute") == "images"), None,
                )
                if img_field:
                    compact = {
                        k: img_field.get(k)
                        for k in ("component", "type", "multiple", "meta",
                                  "acceptedTypes", "draftId", "mode")
                        if img_field.get(k) is not None
                    }
                    img_diag = (
                        f"\n📷 {photo_stat}\n"
                        f"images field: <code>"
                        f"{_json.dumps(compact, ensure_ascii=False)[:400]}</code>\n"
                        f"ключи: <code>{list(img_field.keys())[:20]}</code>"
                    )
                else:
                    img_diag = f"\n📷 {photo_stat}\nimages field: не найдено в форме"
            return False, (
                f"✅ Ресурс <b>{res}</b> найден!\n"
                f"Поля: <code>{attrs}</code>\n"
                f"Обязательные: <code>{required}</code>"
                f"{img_diag}\n\n"
                f"Ошибка 422:\n<code>{err_str}</code>\n\n"
                f"Отправлено: <code>{_json.dumps(payload, ensure_ascii=False)[:200]}</code>"
            )

        if post_resp.status_code == 401:
            return False, (
                "⚠️ <b>Панель не принимает сохранённые куки</b> "
                f"(401 на POST <code>{res}</code>).\n\n"
                "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова."
            )
        if post_resp.status_code == 403:
            debug.append(f"POST {res}: 403 нет доступа")
            continue
        if post_resp.status_code == 419:
            debug.append(f"POST {res}: 419 CSRF mismatch")
            continue

        debug.append(f"POST {res}: {post_resp.status_code} → {post_resp.text[:60]}")

    _save_refreshed_cookies(uid, cookie_string, session)
    diag = "\n".join(debug[:20])
    return False, f"🔍 <b>Диагностика</b>:\n{diag}"


def panel_check_session_sync(cookie_string: str) -> tuple[bool, str]:
    """
    Blocking session check via requests — call through run_in_executor.
    Returns (ok, detail) where detail lists each probe and its HTTP status.
    """
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    session = requests.Session()
    session.verify = False
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip():
                session.cookies.set(k.strip(), v.strip(), domain="panel.yoomarket.net")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PANEL_URL + "/",
    })

    details = []
    ok = False
    for path in ("/nova-api/items/creation-fields?editing=true&editMode=create",
                 "/api/user"):
        try:
            r = session.get(PANEL_URL + path, timeout=(6, 10), allow_redirects=False)
            details.append(f"{path.split('?')[0]}: {r.status_code}")
            if r.status_code == 200:
                ok = True
                break
            if r.status_code == 401:
                return False, "\n".join(details)
        except Exception as e:
            details.append(f"{path.split('?')[0]}: {str(e)[:40]}")

    if not ok:
        # Last probe: does the panel root redirect to /login?
        try:
            r = session.get(PANEL_URL + "/", timeout=(6, 10), allow_redirects=True)
            final = r.url or ""
            details.append(f"/: {r.status_code} → {final[-40:]}")
            ok = "/login" not in final and "/auth" not in final
        except Exception as e:
            details.append(f"/: {str(e)[:40]}")

    return ok, "\n".join(details)


class PanelSession:
    """
    HTTP client authenticated with panel.yoomarket.net session cookies.
    Uses the panel's internal (non-public) API — no browser needed.
    """

    def __init__(self, cookie_string: str) -> None:
        self.cookie_string = cookie_string
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        cookies: dict[str, str] = {}
        for part in self.cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()

        connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(
            cookies=cookies,
            connector=connector,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": PANEL_URL + "/",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    def _xsrf(self) -> str:
        import urllib.parse
        # From cookie jar (set by server responses, e.g. /sanctum/csrf-cookie)
        try:
            jar = self._session.cookie_jar.filter_cookies(PANEL_URL)
            for name, cookie in jar.items():
                if name.upper() in ("XSRF-TOKEN", "CSRF-TOKEN"):
                    return urllib.parse.unquote(cookie.value)
        except Exception:
            pass
        # Fallback: parse from original Playwright cookie string
        for part in self.cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip().upper() in ("XSRF-TOKEN", "CSRF-TOKEN"):
                    return urllib.parse.unquote(v.strip())
        return ""

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        allow_redirects: bool = False,
        deadline: float = 7.0,
    ) -> tuple[int, str]:
        """
        Make an HTTP request with a hard asyncio deadline so TCP hangs can't block forever.
        Returns (status_code, response_text) or (-1, error_str) on timeout/error.
        """
        async def _do() -> tuple[int, str]:
            kw: dict = {
                "headers": headers or {},
                "allow_redirects": allow_redirects,
                "ssl": False,
            }
            if json is not None:
                kw["json"] = json
            if method == "GET":
                cm = self._session.get(url, **kw)
            else:
                cm = self._session.post(url, **kw)
            async with cm as resp:
                return resp.status, await resp.text()

        try:
            return await asyncio.wait_for(_do(), timeout=deadline)
        except asyncio.TimeoutError:
            return -1, f"timeout after {deadline}s"
        except Exception as e:
            return -1, str(e)[:80]

    async def create_product(
        self,
        title: str,
        price: int,
        description: str,
        quantity: int = 1,
        category: str = "",
    ) -> tuple[bool, str]:
        """
        Create a product via the Laravel Nova API (panel is built on Nova).
        Returns (True, product_id) or (False, error/diagnostic).
        """
        if not self._session:
            return False, "Сессия не запущена"

        # Quick CSRF cookie grab (non-fatal if it times out)
        await self._request("GET", PANEL_URL + "/sanctum/csrf-cookie", deadline=5.0)

        values = {
            "title": title,
            "price": price,
            "description": description,
            "quantity": quantity,
            "category": category,
        }
        return await self._nova_create_product(values)

    async def _nova_create_product(self, values: dict) -> tuple[bool, str]:
        """Discover the Nova product resource and create it via /nova-api."""
        xsrf = self._xsrf()
        hdrs = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf

        debug: list[str] = []
        debug.append(f"XSRF: {'✓' if xsrf else '✗ (нет токена)'}")

        # 1. Enumerate Nova resources via navigation endpoint
        resources: list[str] = []
        for nav_path in ("/nova-api/navigation", "/nova-api/resources"):
            status, txt = await self._request("GET", PANEL_URL + nav_path, headers=hdrs)
            if status == -1:
                debug.append(f"{nav_path}: {txt}")
                continue
            short = txt[:150].replace("\n", " ")
            debug.append(f"{nav_path}: {status} → {short}")
            if status == 200:
                found = re.findall(r'"uriKey"\s*:\s*"([^"]+)"', txt)
                resources.extend(r for r in found if r not in resources)
            elif status in (401, 403):
                return False, (
                    "⚠️ <b>Сессия в панели истекла.</b>\n\n"
                    "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова через email."
                )

        debug.append(f"Ресурсы из navigation: {resources or '(не найдено)'}")

        # 2. Hardcoded fallbacks — goods first (SPA route is /goods → uriKey='goods')
        for d in ("goods", "products", "offers", "items", "lots", "adverts",
                  "listings", "seller-goods", "seller-products", "advertisements"):
            if d not in resources:
                resources.append(d)

        # Remove resources that are definitely not product forms
        _NON_PRODUCT = {"ad-groups", "ad-group", "categories", "tags", "users",
                        "roles", "permissions", "settings", "logs", "reviews",
                        "orders", "chats", "messages", "notifications"}
        resources = [r for r in resources if r not in _NON_PRODUCT]

        debug.append(f"Всего ресурсов для проверки: {len(resources)}")

        # 3. For each candidate: check creation-fields, then POST with JSON
        for res in resources[:6]:
            cf_url = f"{PANEL_URL}/nova-api/{res}/creation-fields"
            cf_status, cf_text = await self._request("GET", cf_url, headers=hdrs)

            if cf_status == 401:
                return False, (
                    "⚠️ <b>Сессия в панели истекла.</b>\n\n"
                    "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова через email."
                )
            if cf_status == 403:
                debug.append(f"{res}: 403 нет доступа")
                continue
            if cf_status == 404:
                debug.append(f"{res}: 404")
                continue
            if cf_status != 200:
                debug.append(f"{res}: {cf_status} → {cf_text[:80]}")
                continue

            try:
                cf = _json.loads(cf_text)
            except Exception:
                debug.append(f"{res}: невалидный JSON")
                continue

            if not isinstance(cf, dict):
                debug.append(f"{res}: ответ не объект ({type(cf).__name__}: {str(cf)[:60]})")
                continue

            fields = cf.get("fields") or []
            # Filter out non-dict entries (some Nova versions return field names as strings)
            fields = [f for f in fields if isinstance(f, dict)]
            if not fields:
                debug.append(f"{res}: пустые поля")
                continue

            attrs = [f.get("attribute") for f in fields if f.get("attribute")]
            required = [
                f.get("attribute") for f in fields
                if f.get("attribute") and "required" in str(f.get("rules", []))
            ]

            # Skip non-product resources: a product form MUST have a price-like field.
            # Resources like ad-groups (title/public_title only) are not product forms.
            _PRICE_KEYWORDS = ("price", "cost", "cena", "amount", "sum", "стоим")
            has_price_field = any(
                any(kw in (a or "").lower() for kw in _PRICE_KEYWORDS)
                for a in attrs
            )
            if not has_price_field:
                debug.append(f"⏭ {res}: нет поля цены, пропуск ({attrs})")
                continue

            debug.append(f"✅ {res}: поля={attrs}")

            payload = self._map_nova_fields(fields, values)

            store_url = f"{PANEL_URL}/nova-api/{res}?editing=true&editMode=create"
            post_status, text = await self._request(
                "POST", store_url, headers=hdrs, json=payload
            )
            if post_status == -1:
                debug.append(f"POST {res}: {text}")
            elif post_status in (200, 201):
                try:
                    data = _json.loads(text)
                except Exception:
                    data = {}
                rid = (data.get("resource") or {}).get("id") or data.get("id") or ""
                if isinstance(rid, dict):
                    rid = rid.get("value", "")
                return True, str(rid) if rid else "создан"
            elif post_status == 422:
                try:
                    err_body = _json.loads(text)
                    err_fields = err_body.get("errors") or err_body.get("message") or text[:300]
                except Exception:
                    err_fields = text[:300]
                err_str = (
                    _json.dumps(err_fields, ensure_ascii=False)[:500]
                    if isinstance(err_fields, dict)
                    else str(err_fields)[:500]
                )
                return False, (
                    f"✅ Ресурс <b>{res}</b> найден!\n"
                    f"Поля Nova: <code>{attrs}</code>\n"
                    f"Обязательные: <code>{required}</code>\n\n"
                    f"Ошибка 422:\n<code>{err_str}</code>\n\n"
                    f"Отправлено: <code>{_json.dumps(payload, ensure_ascii=False)[:200]}</code>"
                )
            elif post_status in (401, 403):
                return False, (
                    "⚠️ <b>Сессия в панели истекла.</b>\n\n"
                    "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова."
                )
            else:
                debug.append(f"POST {res}: {post_status} → {text[:80]}")

        diag = "\n".join(debug[:20])
        return False, f"🔍 <b>Диагностика Nova</b>:\n{diag}"

    @staticmethod
    def _map_nova_fields(fields: list[dict], values: dict) -> dict:
        """Map our title/price/description/quantity onto Nova field attributes."""
        payload: dict = {}
        for f in fields:
            if not isinstance(f, dict):
                continue
            attr = f.get("attribute") or ""
            if not attr:
                continue
            al = attr.lower()
            val = None
            if any(k in al for k in ("title", "name", "header", "naimenov")):
                val = values.get("title")
            elif "price" in al or "cost" in al or "cena" in al:
                val = values.get("price")
            elif "desc" in al or "opis" in al or "text" in al or "content" in al:
                val = values.get("description")
            elif any(k in al for k in ("count", "quantity", "amount", "stock", "kolich", "qty")):
                val = values.get("quantity")
            elif "categ" in al or "kategor" in al:
                val = values.get("category") or None
            else:
                # Pre-fill any existing default value Nova provides
                dv = f.get("value")
                if dv not in (None, ""):
                    val = dv
            if val is not None and val != "":
                # Respect max:N validation rule from Nova field definition
                if isinstance(val, str):
                    for rule in (f.get("rules") or []):
                        if isinstance(rule, str) and rule.startswith("max:"):
                            try:
                                val = val[:int(rule.split(":")[1])]
                            except (ValueError, IndexError):
                                pass
                payload[attr] = val
        # Ensure core fields present even if Nova naming differs (no truncation needed here
        # since these only fire when the field wasn't found by name — Nova will reject anyway)
        if "title" not in payload and values.get("title"):
            payload["title"] = values["title"]
        if "price" not in payload and values.get("price") is not None:
            payload["price"] = values["price"]
        return payload

    async def _discover_product_paths(self) -> tuple[list[str], str]:
        """Download the SPA JS bundles and extract product-creation API paths."""
        timeout = aiohttp.ClientTimeout(total=15)
        discovered: list[str] = []
        debug: list[str] = []
        try:
            # Fetch the SPA shell from the root
            html = ""
            for page_path in ("/", "/goods", "/products"):
                try:
                    async with self._session.get(PANEL_URL + page_path, timeout=timeout) as resp:
                        html = await resp.text()
                    if len(html) > 500:
                        break
                except Exception:
                    continue
            debug.append(f"HTML {len(html)}б")

            # Catch JS refs in <script src>, <link href=*.js>, modulepreload, and bare /assets/*.js
            js_files: list[str] = []
            js_files += re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html, re.I)
            js_files += re.findall(r'<link[^>]+href=["\']([^"\']+\.js[^"\']*)["\']', html, re.I)
            js_files += re.findall(r'["\'](/assets/[^"\']+\.js)["\']', html, re.I)
            js_files += re.findall(r'["\'](/build/[^"\']+\.js)["\']', html, re.I)
            js_files = list(dict.fromkeys(js_files))
            debug.append(f"JS: {len(js_files)} {[s.split('/')[-1][:20] for s in js_files[:3]]}")

            kws = ("product", "goods", "offer", "item", "lot")
            # Capture arguments to .post()/.put()/.patch() AND any product-ish string literal
            post_re = re.compile(r'\.(?:post|put|patch)\(\s*["\'`]([^"\'`]{2,80})["\'`]')
            lit_re = re.compile(r'["\'`]([a-zA-Z0-9/_{}$.\-]*(?:product|goods|offer|item|lot)[a-zA-Z0-9/_{}$.\-]*)["\'`]', re.I)
            base_re = re.compile(r'baseURL\s*[:=]\s*["\'`]([^"\'`]+)["\'`]')

            post_paths: set[str] = set()
            lit_paths: set[str] = set()
            base_url = ""
            total_js_bytes = 0

            for src in js_files[:8]:
                url = src if src.startswith("http") else PANEL_URL + src
                try:
                    async with self._session.get(url, timeout=timeout) as r:
                        js = await r.text()
                    total_js_bytes += len(js)
                except Exception:
                    continue
                if not base_url:
                    bm = base_re.search(js)
                    if bm:
                        base_url = bm.group(1)
                for m in post_re.findall(js):
                    post_paths.add(m)
                for m in lit_re.findall(js):
                    if "/" in m or any(k in m.lower() for k in kws):
                        lit_paths.add(m)

            debug.append(f"JS {total_js_bytes}б base={base_url or '?'}")
            # POST targets that mention a product keyword are the strongest candidates
            prod_posts = [p for p in post_paths if any(k in p.lower() for k in kws)]
            debug.append(f"POST-вызовы: {sorted(post_paths)[:8]}")
            debug.append(f"Товарные литералы: {sorted(lit_paths)[:10]}")

            # Build absolute /api candidate paths
            def _norm(p: str) -> str:
                p = p.strip()
                if p.startswith("http"):
                    return ""
                if p.startswith("/api"):
                    return p
                if p.startswith("/"):
                    return "/api" + p
                return "/api/" + p

            for p in prod_posts + [x for x in lit_paths if "{" not in x and "$" not in x]:
                np = _norm(p)
                if np and np not in discovered:
                    discovered.append(np)
        except Exception as e:
            debug.append(f"Ошибка: {str(e)[:80]}")

        return discovered[:15], " | ".join(debug)

    async def check_session(self) -> bool:
        """Verify cookies are still valid via authenticated API calls."""
        if not self._session:
            return False
        timeout = aiohttp.ClientTimeout(total=10)
        xsrf = self._xsrf()
        hdrs = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf
        try:
            # Nova navigation endpoint — 200 = authenticated
            async with self._session.get(
                PANEL_URL + "/nova-api/navigation",
                headers=hdrs, timeout=timeout, allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (401, 403):
                    return False
            # /api/user — standard Sanctum auth check
            async with self._session.get(
                PANEL_URL + "/api/user",
                headers=hdrs, timeout=timeout, allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    return True
                if resp.status in (401, 403):
                    return False
            # Fallback: redirect check (SPA may give false positive)
            async with self._session.get(
                PANEL_URL + "/", timeout=timeout, allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                return not any(k in final_url for k in ("/login", "/auth", "/signin"))
        except Exception:
            return False
