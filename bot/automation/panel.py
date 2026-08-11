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
        # A Bearer token the login response may carry — the panel SPA stores it
        # in localStorage and uses it for the chat API, which cookies cannot
        # reach. Captured so support replies can be sent.
        self.chat_token: str = ""

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
                        # Capture a Bearer token from the body regardless of
                        # cookies — the chat API needs it, cookies do not reach
                        # it. Look through nested objects too (token often sits
                        # under "data"/"user").
                        self.chat_token = _token_from_body(text)
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



def _token_from_body(text: str) -> str:
    """A Bearer token from a login response — top level or nested under the
    usual keys. Returns '' when there is none."""
    try:
        data = _json.loads(text)
    except Exception:
        return ""

    def _walk(node, depth=0):
        if depth > 4 or not isinstance(node, dict):
            return ""
        for key in ("token", "access_token", "api_token", "bearer",
                    "auth_token", "accessToken"):
            v = node.get(key)
            if isinstance(v, str) and len(v) >= 20:
                return v
        for v in node.values():
            if isinstance(v, dict):
                found = _walk(v, depth + 1)
                if found:
                    return found
        return ""

    return _walk(data)


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


def _get_update_fields(session, hdrs, item_id: str,
                       resource: str = "items") -> tuple[list[dict], str]:
    """GET update-fields for an item. Returns (fields, error).

    `resource` was previously read from an enclosing scope that does not exist,
    so every call raised NameError and came back as "не получил поля товара".
    """
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/{resource}/{item_id}/update-fields"
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


def _put_item(session, hdrs, item_id: str, form: dict, resource: str = "items"):
    return session.post(
        f"{PANEL_URL}/nova-api/{resource}/{item_id}?editing=true&editMode=update",
        data=form, headers=hdrs, timeout=(6, 15),
    )


def _value_of_attr(fields: list[dict], attr: str):
    """Текущее значение поля по имени атрибута, или None если такого нет."""
    for f in fields:
        if f.get("attribute") == attr:
            return f.get("value")
    return None


def _same_value(want, got) -> bool:
    """Одно ли это значение. Панель возвращает «139.00» на отправленные 139."""
    if got is None:
        return False
    a, b = _num(want), _num(got)
    if a is not None and b is not None:
        return abs(a - b) < 0.005
    return str(want).strip().lower() == str(got).strip().lower()


def _locate_fields(session, hdrs, item_id: str, wanted: set[str]
                   ) -> tuple[str, str, list[dict]]:
    """Найти запись, у которой правда есть нужные поля.

    Позиция в `items` — это единица товара: название, остаток, связь с
    объявлением. Цены среди её полей нет вовсе, и PUT с ценой Nova принимала,
    ничего не меняя. Поэтому сначала ищем, у кого поле есть, и правим уже
    того. Возвращает (ресурс, номер записи, поля); пустой ресурс — не нашли.
    """
    fields, _err = _get_update_fields(session, hdrs, item_id)
    have = {str(f.get("attribute")) for f in fields}
    if fields and wanted <= have:
        return "items", str(item_id), fields

    resources = [r for r in dict.fromkeys(
        list(panel_discover_resources_sync(_cookie_of(session)))
        + list(_ITEM_RESOURCES)) if r not in _NOT_A_LISTING]

    # Сама запись могла найтись в другом ресурсе — «unlimited» товары в
    # `items` отвечают 404.
    for resource in resources[:12]:
        if resource == "items":
            continue
        code, other = _probe_one(session, hdrs, resource, item_id)
        if code == 200 and other and wanted <= {
                str(f.get("attribute")) for f in other}:
            return resource, str(item_id), other

    # Иначе — вверх по связи: цена стоит у объявления, а не у его единицы.
    for f in fields:
        rid = _looks_like_link(f)
        if not rid or rid == str(item_id):
            continue
        for resource in resources[:12]:
            code, parent = _probe_one(session, hdrs, resource, rid)
            if code == 200 and parent and wanted <= {
                    str(pf.get("attribute")) for pf in parent}:
                return resource, rid, parent
    return "", str(item_id), fields


def _cookie_of(session) -> str:
    """Строка cookie этой сессии — для функций, которые берут её, а не сессию."""
    try:
        return "; ".join(f"{c.name}={c.value}" for c in session.cookies)
    except Exception:
        return ""


def panel_update_item_sync(
    cookie_string: str, item_id: str, overrides: dict, uid: int | None = None,
) -> tuple[bool, str]:
    """Blocking: change item fields (e.g. {'price': 199, 'title': ...}),
    preserving everything else including photos.

    «Обновлено» здесь значит «в панели теперь так», а не «запрос принят».
    Nova отвечает 200 и на форму, часть которой молча выбросила, — продавцу
    приходило «✅ Цена обновлена», а на сайте оставалась старая цена, и
    поверить боту после такого нельзя ни в чём.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    wanted = {str(k) for k in overrides}
    resource, rec_id, fields = _locate_fields(session, hdrs, item_id, wanted)
    if not fields:
        return False, "не получил поля товара"
    if not resource:
        have = ", ".join(sorted({str(f.get("attribute")) for f in fields})[:10])
        return False, (f"панель не хранит {', '.join(sorted(wanted))} у этого "
                       f"товара. Есть поля: {have}")
    where = "" if (resource == "items" and rec_id == str(item_id)) \
        else f" ({resource} #{rec_id})"

    form = _build_update_form(fields, overrides)
    try:
        resp = _put_item(session, hdrs, rec_id, form, resource)
    except Exception as e:
        return False, f"ошибка запроса: {str(e)[:60]}"
    _save_refreshed_cookies(uid, cookie_string, session)
    if resp.status_code not in (200, 201, 204):
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

    after, read_err = _get_update_fields(session, hdrs, rec_id, resource)
    if not after:
        # Перечитать не вышло — не выдаём это за успех и не выдаём за провал.
        return True, f"панель приняла запрос (проверить не удалось: {read_err})"

    unknown, stuck = [], []
    for attr, want in overrides.items():
        got = _value_of_attr(after, attr)
        if got is None and not any(f.get("attribute") == attr for f in after):
            unknown.append(attr)
        elif not _same_value(want, got):
            stuck.append(f"{attr}: осталось «{_strip_html(got)}», просили «{want}»")

    if not unknown and not stuck:
        return True, f"обновлено{where}"

    why = ["панель приняла запрос, но значение не изменилось."]
    if stuck:
        why.append("; ".join(stuck))
    if unknown:
        near = [f.get("attribute") for f in after
                if any(k in str(f.get("attribute", "")).lower()
                       for k in ("price", "cost", "sum", "amount", "цен"))]
        why.append(f"поля {', '.join(unknown)} у товара нет"
                   + (f"; похожие: {', '.join(str(n) for n in near[:6])}"
                      if near else ""))
    return False, " ".join(why)


_MONEY_HINTS = ("price", "cost", "sum", "amount", "цен")


def _looks_like_link(f: dict) -> str:
    """Номер связанной записи, если это поле — ссылка на другую сущность.

    Позиция товара обычно принадлежит объявлению, а цена стоит у объявления.
    Пока связь не видна, «в items цены нет» — тупик: неизвестно, где искать.
    """
    comp = str(f.get("component") or "").lower()
    val = f.get("value")
    if isinstance(val, dict):
        rid = val.get("id") or val.get("value")
        if rid is not None:
            return str(rid)
    if "belongs-to" in comp or "belongsto" in comp:
        rid = f.get("belongsToId") or f.get("value")
        if rid is not None and not isinstance(rid, (dict, list)):
            return str(rid)
    return ""


def _probe_one(session, hdrs, resource: str, rec_id: str) -> tuple[int, list[dict]]:
    """update-fields одной записи: (код ответа, поля)."""
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/{resource}/{rec_id}/update-fields"
            f"?editing=true&editMode=update",
            headers=hdrs, timeout=(6, 10), allow_redirects=False)
    except Exception:
        return 0, []
    if r.status_code != 200:
        return r.status_code, []
    try:
        return 200, _parse_nova_fields_payload(r.json())
    except Exception:
        return 200, []


def panel_item_fields_probe_sync(cookie_string: str, item_id: str,
                                 uid: int | None = None) -> list[str]:
    """Где у этого товара лежит цена — только чтение.

    «Цена не меняется» и «в этой сущности цены вообще нет» — разные вещи, а
    выглядят одинаково. Здесь перебираются все ресурсы панели, показываются
    все поля найденной записи и прослеживаются связи: если позиция
    принадлежит объявлению, цена почти наверняка у него.
    """
    out: list[str] = []
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)

    resources = [r for r in dict.fromkeys(
        list(panel_discover_resources_sync(cookie_string)) + list(_ITEM_RESOURCES))
        if r not in _NOT_A_LISTING]

    missing: list[str] = []
    links: list[tuple[str, str]] = []      # (имя поля, номер записи)
    for resource in resources[:20]:
        code, fields = _probe_one(session, hdrs, resource, item_id)
        if code != 200 or not fields:
            missing.append(f"{resource}:{code or 'сеть'}")
            continue
        out.append(f"✔ {resource}: полей {len(fields)}")
        for f in fields:
            attr = str(f.get("attribute") or "")
            if not attr:
                continue
            comp = str(f.get("component") or "").replace("-field", "")
            value = _field_text(f)[:40]
            mark = " 💰" if any(k in attr.lower() for k in _MONEY_HINTS) else ""
            out.append(f"   {attr} [{comp}] = «{value}»{mark}")
            rid = _looks_like_link(f)
            if rid and rid != str(item_id):
                links.append((attr, rid))
        if len(out) > 45:
            break

    if missing:
        out.append("✘ нет записи: " + ", ".join(missing[:12]))

    # По связям — туда, где цена и должна быть.
    for attr, rid in links[:3]:
        out.append(f"↳ связь {attr} → #{rid}")
        for resource in resources[:20]:
            code, fields = _probe_one(session, hdrs, resource, rid)
            if code != 200 or not fields:
                continue
            money = [f for f in fields
                     if any(k in str(f.get("attribute", "")).lower()
                            for k in _MONEY_HINTS)]
            out.append(f"   ✔ {resource}/{rid}: полей {len(fields)}"
                       + ("" if money else " — денежных полей нет"))
            for f in money[:4]:
                out.append(f"      {f.get('attribute')} = "
                           f"«{_field_text(f)[:30]}» 💰")
            break

    return out


# Nova resources a listing might live under. The panel exposes no resource
# index (/nova-api/resources answers with nothing), so the names are probed
# rather than enumerated — `items` holds some listings but not the «unlimited»
# ones, whose ids answer 404 there.
_ITEM_RESOURCES = ("items", "ad-groups", "ad-group", "ads", "goods", "products",
                   "lots", "offers", "unlimiteds", "unlimited-items", "adverts")

# Resources that plainly hold something other than a listing. «ad-groups» is
# deliberately absent: it was assumed to be an organisational bucket and
# excluded, but on this panel `items` turned out to hold the units being sold
# (individual accounts with a balance), which leaves the listing itself
# somewhere else — and a group of identical units is exactly what a listing is.
_NOT_A_LISTING = frozenset({
    "categories", "tags", "users", "roles", "permissions", "settings", "logs",
    "reviews", "orders", "chats", "messages", "notifications", "balances",
    "withdrawals", "transactions", "payments", "nova-notifications",
})

_RESOURCE_CACHE: dict[str, list[str]] = {}


def panel_discover_resources_sync(cookie_string: str) -> list[str]:
    """Every Nova resource this panel exposes, asked rather than guessed.

    Guessing names is how the search kept missing: nine candidates were tried
    and only `items` existed. Nova's index endpoint answers empty on this
    panel, so the names are also read out of the SPA's own page — they appear
    there as uriKeys and router paths. Cached per session cookie: the list does
    not change between passes, and each pass would otherwise refetch it.
    """
    import re as _re
    ck = cookie_string[-32:]
    if ck in _RESOURCE_CACHE:
        return _RESOURCE_CACHE[ck]

    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    names: list[str] = []

    def add(found):
        for r in found:
            r = str(r).strip().lower()
            if r and r not in names and _re.fullmatch(r"[a-z0-9_\-]{3,40}", r):
                names.append(r)

    for path in ("/nova-api/navigation", "/nova-api/resources"):
        try:
            r = session.get(PANEL_URL + path, headers=hdrs,
                            timeout=(6, 10), allow_redirects=False)
            if r.status_code == 200:
                add(_re.findall(r'"uriKey"\s*:\s*"([^"]+)"', r.text))
        except Exception:
            pass
    if not names:
        try:
            html = session.get(PANEL_URL + "/", timeout=(6, 12),
                               allow_redirects=True).text
            add(_re.findall(r'"uriKey"\s*:\s*"([^"]+)"', html))
            add(_re.findall(r'/resources/([a-z0-9_\-]+)', html, _re.I))
        except Exception:
            pass

    names = [n for n in names if n not in _NOT_A_LISTING]
    _RESOURCE_CACHE[ck] = names
    return names


def _row_title(res: dict) -> str:
    """The display name of a Nova row, wherever this panel keeps it."""
    title = res.get("title") or res.get("display") or ""
    if isinstance(title, dict):
        title = title.get("value") or title.get("title") or ""
    if title:
        return str(title)
    fields = res.get("fields")
    if isinstance(fields, dict):
        fields = list(fields.values())
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        fa = str(f.get("attribute", "")).lower()
        fn = str(f.get("name", "")).lower()
        if "title" in fa or "name" in fa or "назван" in fn or "наимен" in fn:
            v = f.get("value")
            if isinstance(v, dict):
                v = v.get("title") or v.get("name") or v.get("display")
            if v:
                return str(v)
    return ""


def _row_haystack(res: dict) -> str:
    """Everything textual in a Nova row, for matching a listing by name.

    The row's display title is not always the ad's own name — this panel titles
    a row by its parent product, keeping the ad name in a field — so the whole
    row is searched rather than one attribute.
    """
    parts = [_row_title(res)]
    fields = res.get("fields")
    if isinstance(fields, dict):
        fields = list(fields.values())
    for f in (fields or []):
        if not isinstance(f, dict):
            continue
        v = f.get("value")
        if isinstance(v, dict):
            v = v.get("title") or v.get("name") or v.get("display") or ""
        if isinstance(v, str) and v:
            parts.append(_strip_html(v))
    return " ".join(str(p) for p in parts if p)


def _nova_global_search(session, hdrs, query: str) -> list[dict]:
    """Nova's own cross-resource search: [{resource, id, title}].

    Guessing resource names is how the last search failed — every candidate
    but `items` answered 404, while the listing sat in a resource nobody
    guessed. Nova already knows where its records live, so it is asked.
    """
    out = []
    for path in ("/nova-api/search", "/nova-api/global-search"):
        try:
            r = session.get(PANEL_URL + path, params={"search": query[:60]},
                            headers=hdrs, timeout=(6, 12), allow_redirects=False)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        rows = data if isinstance(data, list) else (
            data.get("resources") or data.get("data") or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            res = (row.get("resourceName") or row.get("resource")
                   or row.get("resourceUriKey") or "")
            rid = row.get("resourceId") or row.get("id")
            if isinstance(rid, dict):
                rid = rid.get("value")
            title = str(row.get("title") or row.get("resourceTitle")
                        or row.get("display") or "")
            if res and rid:
                out.append({"resource": str(res), "id": str(rid),
                            "title": title})
        if out:
            break
    return out


def panel_find_listing_sync(
    cookie_string: str, titles: list, resources: tuple = _ITEM_RESOURCES,
) -> tuple[dict, str]:
    """Blocking: locate listings by name across the panel's own resources.

    Returns ({title_key: (resource, id)}, trace). The Integration API and the
    panel number the same listing differently, and the panel does not
    necessarily title a row by the ad's name, so every text in the row is
    searched — matched on letters and digits only, since the two sides decorate
    and truncate differently.
    """
    import re as _re

    def key(t) -> str:
        return _re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", str(t or "")).lower()

    wanted = {key(t) for t in titles if len(key(t)) >= 8}
    found: dict = {}
    trace: list[str] = []
    samples: list[str] = []
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)

    # Ask Nova where each listing lives before walking resources by name: it
    # knows, and the names were the part that kept being wrong.
    for t in titles:
        wk = key(t)
        if len(wk) < 8 or wk in found:
            continue
        for hit in _nova_global_search(session, hdrs, str(t)):
            hk = key(hit["title"])
            if hk and (wk in hk or hk in wk or hk[:24] in wk or wk[:24] in hk):
                found[wk] = (hit["resource"], hit["id"])
                trace.append(f"поиск: {hit['resource']}#{hit['id']}")
                break
    if len(found) == len(wanted):
        return found, "; ".join(trace)[:400]

    # Beyond the guessed names, walk what the panel says it actually has. Nine
    # guesses found only `items`, and `items` turned out to hold the units being
    # sold rather than the listings — so the resource that holds them is one
    # nobody named.
    try:
        discovered = [r for r in panel_discover_resources_sync(cookie_string)
                      if r not in resources]
    except Exception:
        discovered = []
    if discovered:
        trace.append(f"ещё есть: {', '.join(discovered[:12])}")

    for res_name in tuple(resources) + tuple(discovered):
        if len(found) == len(wanted):
            break
        rows = []
        # Walk pages: a listing missing from page one is not a listing absent
        # from the panel.
        for page in (1, 2, 3):
            try:
                r = session.get(f"{PANEL_URL}/nova-api/{res_name}",
                                params={"perPage": "100", "page": str(page)},
                                headers=hdrs, timeout=(6, 12),
                                allow_redirects=False)
            except Exception as e:
                trace.append(f"{res_name}: {str(e)[:30]}")
                break
            if r.status_code != 200:
                if page == 1:
                    trace.append(f"{res_name}: {r.status_code}")
                break
            try:
                chunk = (r.json() or {}).get("resources") or []
            except Exception:
                break
            rows += [x for x in chunk if isinstance(x, dict)]
            if len(chunk) < 100:
                break
        if not rows:
            continue
        trace.append(f"{res_name}: {len(rows)} шт.")
        for row in rows:
            rid = row.get("id")
            if isinstance(rid, dict):
                rid = rid.get("value")
            if not rid:
                continue
            hay = key(_row_haystack(row))
            if len(samples) < 6:
                samples.append(_row_title(row)[:28] or f"#{rid}")
            for wk in wanted:
                if wk in found:
                    continue
                # Containment both ways: the panel may hold the fuller name or
                # the shorter one. The 8-character floor above keeps a short
                # name from matching half the catalogue.
                if wk in hay or (len(hay) >= 8 and hay[:24] in wk):
                    found[wk] = (res_name, str(rid))
                    break
    if len(found) < len(wanted) and samples:
        trace.append("названия в панели: " + ", ".join(samples))
    return found, "; ".join(trace)[:400]


# Поля, по которым видно, опубликован товар или нет.
_STATUS_ATTRS = ("status", "state", "public", "is_public", "visible",
                 "is_active", "active", "published", "moderation")


def _publish_state(fields: list[dict]) -> str:
    """Состояние публикации одной строкой, или «» — если поля нет.

    Сравнивать «до и после» надо по чему-то устойчивому: имена полей у панели
    свои, поэтому берём все похожие на статус сразу.
    """
    if not fields:
        return ""
    parts: list[str] = []
    for f in fields:
        attr = str(f.get("attribute") or "").lower()
        if attr in _STATUS_ATTRS:
            parts.append(f"{attr}={_field_text(f)[:24]}")
    return ", ".join(parts)


def _nova_refusal(resp) -> str:
    """Отказ, спрятанный в теле успешного ответа Nova.

    Действие может вернуть 200 и при этом ничего не сделать: причина лежит в
    поле `danger`. По коду ответа это неотличимо от успеха.
    """
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("danger", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def panel_publish_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
    public: bool = True, resource: str = "items",
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
            f"{PANEL_URL}/nova-api/{resource}/actions",
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

    # Снимок состояния до действия. Nova отвечает 200 и на отказ — сообщение
    # об отказе лежит в теле, а бот рапортовал «Отправлен на модерацию» по
    # одному коду ответа. Сравнение «до и после» — единственное доказательство.
    _code_before, _fields_before = _probe_one(session, hdrs, resource, item_id)
    before = _publish_state(_fields_before)

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
                    f"{PANEL_URL}/nova-api/{resource}/action?action={key}",
                    data={"resources": str(item_id)},
                    headers=hdrs, timeout=(6, 15),
                )
                trace.append(f"action {key}: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    refused = _nova_refusal(resp)
                    if refused:
                        trace.append(f"отказ: {refused[:60]}")
                        continue
                    _save_refreshed_cookies(uid, cookie_string, session)
                    _code, fields_after = _probe_one(session, hdrs, resource,
                                                     item_id)
                    after = _publish_state(fields_after)
                    if after and before and after == before:
                        # Панель приняла запрос и ничего не изменила. Самый
                        # частый случай — у нового товара нет остатков, и
                        # публиковать нечего.
                        return False, (
                            f"панель приняла «{a.get('name') or key}», но "
                            f"статус не изменился ({after})")
                    what = f" — статус: {after}" if after else ""
                    return True, f"через действие «{a.get('name') or key}»{what}"
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


def _find_promo_action(actions: list[dict]) -> dict | None:
    """The «Премиум» action among a resource's Nova actions."""
    for a in actions:
        if not isinstance(a, dict):
            continue
        key = str(a.get("uriKey") or "")
        if not key:
            continue
        blob = str(a.get("name") or "").lower() + " " + key.lower()
        if any(kw in blob for kw in _DANGEROUS_KWS + _UNPUBLISH_KWS):
            continue
        if any(kw in blob for kw in _BUMP_KWS):
            return a
    return None


_PRICE_RE = re.compile(r"(\d[\d\s ]*)\s*₽")


def _option_price(label: str) -> int:
    """'7 дней - 49 ₽' -> 49. The tariff labels carry the price, which is the
    only place the cost of a promotion is stated before buying it."""
    m = _PRICE_RE.search(str(label))
    if not m:
        return 0
    try:
        return int(m.group(1).replace(" ", "").replace(" ", ""))
    except ValueError:
        return 0


def _nova_rows_to_options(data) -> list[dict]:
    """Turn a Nova index/associatable payload into [{label, value}]."""
    if not isinstance(data, dict):
        return []
    rows = data.get("resources")
    if not isinstance(rows, list):
        return []
    opts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        val = row.get("value", row.get("id"))
        if isinstance(val, dict):                # index rows nest {value: ...}
            val = val.get("value", val.get("id"))
        label = row.get("display") or row.get("title")
        if not label:
            # index rows carry their columns in `fields`
            for fl in (row.get("fields") or []):
                if isinstance(fl, dict) and fl.get("value") not in (None, ""):
                    if str(fl.get("attribute")) not in ("id",):
                        label = str(fl.get("value"))
                        break
        if val in (None, ""):
            continue
        opts.append({"label": str(label or val), "value": val})
    return opts


def panel_action_field_options_sync(
    cookie_string: str, item_id: str, action_key: str, attr: str,
    chosen: dict | None = None, component_key: str = "",
    depends_on: dict | None = None,
) -> tuple[list[dict], str]:
    """Blocking: resolve the options of a dependent action field.

    «Оплата» ships as `visible: false, options: [], dependsOn: {up_id,
    parameter_id}` — it has nothing to offer until the term is chosen, and the
    panel then asks the server to re-resolve it. That request is what is
    reproduced here: PATCH, the dependency values as the body, and the field's
    own `dependentComponentKey` as the `component` parameter, exactly as Nova's
    form does. Several route shapes are tried because the action variant is not
    the same across Nova versions; whatever each answered is reported.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = dict(_panel_xsrf_headers(session, cookie_string))
    hdrs.setdefault("Accept", "application/json")
    trace: list[str] = []

    # The body carries the current state of every field the target depends on.
    values: dict = {}
    for k in (depends_on or {}):
        values[k] = (depends_on or {}).get(k)
    values.update({k: v for k, v in (chosen or {}).items()})
    values = {k: ("" if v is None else v) for k, v in values.items()}

    params = {
        "editing": "true", "editMode": "update",
        "resources": str(item_id), "action": action_key,
        "field": attr,
    }
    if component_key:
        params["component"] = component_key

    def _field_options(data) -> list[dict]:
        """Nova answers a sync with the single field, or with a field list."""
        if not isinstance(data, dict):
            return []
        candidates = []
        if data.get("attribute"):
            candidates.append(data)
        candidates.extend(_parse_nova_fields_payload(data))
        for a in (data.get("actions") or []):
            if isinstance(a, dict) and (not action_key
                                        or a.get("uriKey") == action_key):
                candidates.extend([f for f in (a.get("fields") or [])
                                   if isinstance(f, dict)])
        for f in candidates:
            if isinstance(f, dict) and f.get("attribute") == attr:
                opts = _normalize_options(f)
                if opts:
                    return opts
        return _nova_rows_to_options(data)

    # This panel resolves dependent action fields on the action list itself,
    # once the values they depend on are passed — measured, not assumed. The
    # dedicated sync routes 404/405 here, and are kept only as a fallback for
    # a differently-versioned panel.
    routes = [
        ("GET", "/nova-api/items/actions"),
        ("PATCH", f"/nova-api/items/action-fields/{action_key}"),
        ("PATCH", "/nova-api/items/action-fields"),
        ("PATCH", f"/nova-api/items/actions/{action_key}/fields"),
        ("GET", f"/nova-api/items/actions/{action_key}/fields"),
    ]
    for method, path in routes:
        try:
            if method == "PATCH":
                r = session.patch(f"{PANEL_URL}{path}", params=params,
                                  json=values, headers=hdrs,
                                  timeout=(6, 12), allow_redirects=False)
            else:
                r = session.get(f"{PANEL_URL}{path}",
                                params={**params, **{k: str(v) for k, v
                                                     in values.items()}},
                                headers=hdrs, timeout=(6, 12),
                                allow_redirects=False)
        except Exception as e:
            trace.append(f"{method} {path}: {str(e)[:30]}")
            continue
        if r.status_code in (404, 405):
            trace.append(f"{method} {path}: {r.status_code}")
            continue
        trace.append(f"{method} {path}: {r.status_code}")
        if r.status_code != 200:
            trace.append(f"  {r.text[:80]}")
            continue
        try:
            found = _field_options(r.json())
        except Exception:
            continue
        if found:
            return found, "; ".join(trace)

    # Last resort: a resource the payment systems might live in as rows
    stem = attr[:-3] if attr.endswith("_id") else attr
    for guess in (f"{stem}s", "payment-systems", "payments", "wallets"):
        try:
            r = session.get(f"{PANEL_URL}/nova-api/{guess}",
                            params={"perPage": "100"}, headers=hdrs,
                            timeout=(5, 10), allow_redirects=False)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        trace.append(f"/nova-api/{guess}: 200")
        try:
            found = _nova_rows_to_options(r.json())
        except Exception:
            continue
        if found:
            return found, "; ".join(trace)

    return [], "; ".join(trace)[:400]


def panel_promo_fields_sync(
    cookie_string: str, item_id: str,
) -> tuple[bool, object]:
    """Blocking: describe the «Премиум» action so a tariff can be chosen.

    Returns (True, {"key", "name", "fields": [{attribute, label, options,
    required, price}]}) or (False, error). Options that Nova does not inline
    («Оплата» is a relation, not a plain select) are fetched separately.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items/actions",
            params={"resources": str(item_id)},
            headers=hdrs, timeout=(6, 10), allow_redirects=False,
        )
    except Exception as e:
        return False, f"не получил список действий: {str(e)[:60]}"
    if r.status_code == 401:
        return False, "401: сессия панели истекла — войдите снова"
    if r.status_code != 200:
        return False, f"actions: {r.status_code}"
    try:
        actions = [a for a in ((r.json() or {}).get("actions") or [])
                   if isinstance(a, dict)]
    except Exception:
        return False, "не разобрал ответ панели"

    a = _find_promo_action(actions)
    if not a:
        names = [(x.get("name") or x.get("uriKey") or "?") for x in actions]
        return False, f"действие продвижения не найдено. Доступные: {names}"

    out_fields = []
    for f in (a.get("fields") or []):
        if not isinstance(f, dict):
            continue
        attr = f.get("attribute")
        if not attr:
            continue
        opts = _normalize_options(f)
        for o in opts:
            o["price"] = _option_price(o["label"])
        out_fields.append({
            "attribute": attr,
            "label": f.get("name") or attr,
            "options": opts,
            # «Оплата» carries no required rule — the panel only enforces it
            # server-side — so an empty field cannot be treated as optional:
            # skipping it is exactly what produced the 422.
            "required": "required" in str(f.get("rules", "")) or not opts,
            "value": f.get("value"),
            # Fetched lazily, once earlier choices are known: a dependent field
            # has no options until the selection it depends on is made.
            "lookup": not opts,
            # What Nova needs in order to resolve this field later: which
            # fields it watches, and the key its own form sends as `component`.
            "depends_on": f.get("dependsOn") or {},
            "component_key": f.get("dependentComponentKey") or "",
            "shape": (f"component={f.get('component')} "
                      f"rel={f.get('relationshipType') or f.get('belongsToRelationship') or '—'} "
                      f"keys={sorted(f.keys())[:14]}")[:250] if not opts else "",
        })
    return True, {"key": a.get("uriKey"), "name": a.get("name") or "Премиум",
                  "item_id": str(item_id), "fields": out_fields}


_URL_RE = re.compile(r"https?://[^\s\"'<>\\]{6,300}")


def _find_url(node) -> str:
    """First http(s) link anywhere in a decoded response.

    The payment link was looked for under Nova's own keys (redirect,
    openInNewTab, download) and was not there, so the shape of this panel's
    answer is not Nova's default. Rather than guess the next key name, the
    whole structure is searched.
    """
    if isinstance(node, str):
        m = _URL_RE.search(node)
        return m.group(0) if m else ""
    if isinstance(node, dict):
        # Keys that name a link win over one merely mentioned in prose
        for k, v in node.items():
            if isinstance(v, str) and any(
                    t in str(k).lower() for t in
                    ("url", "link", "redirect", "pay", "invoice", "href")):
                m = _URL_RE.search(v)
                if m:
                    return m.group(0)
        for v in node.values():
            found = _find_url(v)
            if found:
                return found
    if isinstance(node, list):
        for v in node:
            found = _find_url(v)
            if found:
                return found
    return ""


# Nova answers a refused action with HTTP 200 and the reason in the body, so
# the status code says nothing about whether anything happened.
_REFUSAL_KWS = (
    "нет прав", "не авторизов", "запрещ", "недоступ", "не удалось",
    "невозможно", "ошибк", "not authorized", "not allowed", "sorry",
)


def _action_result(resp, fallback: str) -> tuple[bool, str]:
    """Whether a Nova action actually did anything, and what it said.

    «Премиум» is paid through an external payment system, so a real success
    means an invoice was created and the answer carries a link to it. A refusal
    arrives as 200 too — under `danger`, or as a message that plainly says no —
    and must not be reported as a promotion that went through.
    """
    raw = (resp.text or "").strip()
    try:
        data = _json.loads(raw or "{}")
    except Exception:
        data = None

    url = _find_url(data) if data is not None else ""
    if not url:
        m = _URL_RE.search(raw)
        url = m.group(0) if m else ""

    text, refused = "", False
    if isinstance(data, dict):
        if data.get("danger"):
            text, refused = str(data["danger"]), True
        else:
            text = str(data.get("message") or "")
    if not refused and text and any(k in text.lower() for k in _REFUSAL_KWS):
        refused = True

    if refused:
        return False, text
    if url:
        return True, f"{text or fallback}\n🔗 Оплата: {url}"
    if text:
        return True, text
    # No link, no message: report what came back, otherwise a promotion that
    # quietly did nothing looks identical to one that worked.
    body = raw[:250] if raw else f"пустой ответ, код {resp.status_code}"
    return False, f"панель ничего не вернула. Ответ: {body}"


def panel_bump_item_sync(
    cookie_string: str, item_id: str, uid: int | None = None,
    confirm: bool = False, params: dict | None = None,
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

        # Required parameters are never invented — this is a purchase. They
        # come from the tariff the seller picked; without them, say so.
        fields = [f for f in (a.get("fields") or []) if isinstance(f, dict)]
        chosen = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        # Not just the fields Nova marks required: «Оплата» carries no such
        # rule yet is enforced server-side. Anything the action asks for and
        # has no default of its own must come from the chosen tariff.
        missing = [
            f.get("name") or f.get("attribute")
            for f in fields
            if f.get("attribute") not in chosen
            and f.get("value") in (None, "")
        ]
        if missing:
            return False, (
                f"«{a.get('name') or key}»: не выбран тариф ({', '.join(map(str, missing))}). "
                f"Откройте «⭐ Премиум продвижение» → «⚙️ Тариф»."
            )

        payload = {"resources": str(item_id)}
        for f in fields:
            attr = f.get("attribute")
            val = f.get("value")
            if attr and val not in (None, ""):
                payload[attr] = val
        payload.update(chosen)
        try:
            resp = session.post(
                f"{PANEL_URL}/nova-api/items/action?action={key}",
                data=payload, headers=hdrs, timeout=(6, 15),
            )
            trace.append(f"action {key}: {resp.status_code}")
            if resp.status_code in (200, 201, 204):
                _save_refreshed_cookies(uid, cookie_string, session)
                return _action_result(resp, str(a.get("name") or key))
            if resp.status_code in (402, 422, 500):
                try:
                    msg = (_json.loads(resp.text) or {}).get("message") or ""
                except Exception:
                    msg = resp.text[:200]
                # Running out of money is an ordinary outcome, not a fault:
                # say so plainly instead of quoting a validation dump.
                if any(k in msg.lower() for k in
                       ("недостаточно", "не хватает", "средств", "баланс",
                        "insufficient", "not enough")):
                    return False, f"💸 не хватает денег на балансе: {msg[:150]}"
            if resp.status_code == 422:
                # The action wants parameters. Describe them instead of
                # inventing values — this one spends money.
                spec = []
                for f in fields:
                    opts = _normalize_options(f)
                    spec.append(
                        f"{f.get('attribute')} | {f.get('name')}"
                        + (f" | варианты: {[o['label'] for o in opts][:6]}"
                           if opts else "")
                        + (" | обязательное" if "required" in str(f.get("rules", ""))
                           else ""))
                return False, (
                    f"«{a.get('name') or key}» требует данные.\n"
                    f"Ответ: {msg[:200]}\n"
                    f"Поля действия: {spec or 'не описаны'}")
        except Exception as e:
            trace.append(f"action {key}: {str(e)[:40]}")

    _save_refreshed_cookies(uid, cookie_string, session)
    return False, (f"действие продвижения не найдено. "
                   f"Доступные: {names or 'нет'}; лог: {'; '.join(trace)[:200]}")


def panel_bump_all_sync(
    cookie_string: str, uid: int | None = None, confirm: bool = False,
    params: dict | None = None, limit: int = 0,
    only_ids: list | None = None,
) -> tuple[int, str]:
    """Blocking: promote listings through the panel. Returns (count, message).

    `limit` caps how many listings are promoted in one run — the caller works it
    out from the daily spending ceiling, since every listing costs money.
    `only_ids` restricts the run to chosen listings; empty means every listing.
    Promoting selectively is the difference between paying for one position and
    paying for the whole shop.
    """
    ok, items = panel_list_items_sync(cookie_string)
    if not ok:
        return 0, f"⚠️ {items}"
    if not items:
        return 0, "ℹ️ Нет объявлений"
    if only_ids:
        wanted = {str(i) for i in only_ids}
        items = [it for it in items if str(it.get("id")) in wanted]
        if not items:
            return 0, ("ℹ️ Выбранных товаров нет в панели — обновите выбор "
                       "в «Премиум продвижении»")

    count = 0
    refused = 0
    last = ""
    links: list[str] = []
    for it in items:
        item_id = it.get("id")
        if not item_id:
            continue
        if limit and count >= limit:
            last = f"остановился на {limit} — упёрся в потолок трат"
            break
        done, msg = panel_bump_item_sync(cookie_string, item_id, uid, confirm,
                                         params)
        if done:
            count += 1
            # Each promotion is paid separately, so every link matters
            for line in str(msg).splitlines():
                if "http" in line:
                    links.append(f"{it.get('title') or item_id}: {line.strip()}")
        else:
            last = msg
            # Nova authorizes an action per record, so a refusal on one listing
            # says nothing about the next — those are counted and skipped. Only
            # a fault that will repeat for every item stops the run.
            if "нет прав" in msg.lower() or "не авторизов" in msg.lower():
                refused += 1
                continue
            if any(k in msg for k in ("не найдено", "401", "подтвержд",
                                      "требует", "тариф", "не хватает")):
                break
    if count:
        out = f"✅ Оформлено: {count}"
        if refused:
            out += f"\n⛔ Панель отказала по {refused} объявлениям"
        if last and not refused:
            out += f"\n{last}"
        if links:
            out += "\n\n" + "\n".join(links[:10])
        return count, out
    if refused:
        return 0, (f"⛔ Панель отказала по всем {refused} объявлениям: {last}\n"
                   f"Обычно так бывает, пока профиль магазина не прошёл "
                   f"проверку или объявления не активны.")
    return 0, f"⚠️ Не удалось поднять: {last}"


# ---------------------------------------------------------------------------
# Withdrawal through the panel
#
# The Integration API has no withdrawal endpoint at all — every /withdraw path
# the bot tried is a guess that 404s. On Yoomarket a payout is a panel
# operation, most likely a Nova resource you create (amount + payment method +
# requisites) the same way a listing is created. Its exact shape is unknown, so
# nothing about it is guessed: the structure is read from the panel first
# (panel_finance_probe_sync / the /withdraw_debug command), and only then is a
# real request built. Withdrawal MOVES MONEY OUT, so panel_withdraw_sync refuses
# without confirm=True and never invents a field value.
# ---------------------------------------------------------------------------

_FINANCE_RES = ("withdrawals", "withdrawal", "withdraws", "withdraw", "payouts",
                "payout", "finances", "finance", "wallet", "wallets", "balance",
                "balances", "transactions", "transaction", "payments",
                "operations", "vyvod", "vyvody", "cashout", "cashouts")
_WITHDRAW_KWS = ("вывод", "вывести", "withdraw", "payout", "cash", "вывода",
                 "снятие", "выплат")
# Which balance resource / action the panel actually uses. Confirmed from the
# panel itself via /withdraw_debug: withdrawal is the «Вывести» (uriKey
# "вывести") Nova action on the `balances` resource — the same kind of action
# «Премиум» is on `items`, not a resource you create.
_BALANCE_RES = "balances"
_WITHDRAW_ACTION_KWS = ("вывести", "вывод", "withdraw", "payout")
_EXCHANGE_KWS = ("обмен", "обменять", "exchange", "convert")


def _find_withdraw_action(actions: list[dict]) -> dict | None:
    """The «Вывести» action, never the «Обменять» one next to it."""
    for a in actions:
        if not isinstance(a, dict):
            continue
        key = str(a.get("uriKey") or "")
        blob = str(a.get("name") or "").lower() + " " + key.lower()
        if any(k in blob for k in _EXCHANGE_KWS):
            continue
        if any(k in blob for k in _WITHDRAW_ACTION_KWS):
            return a
    return None


def panel_balances_sync(cookie_string: str) -> tuple[bool, object]:
    """Blocking: the seller's balance rows — id, currency, amount.

    Withdrawal is an action on one of these rows, so its id is needed to run it,
    and the amounts say which row actually holds money.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.get(f"{PANEL_URL}/nova-api/{_BALANCE_RES}",
                        params={"perPage": "50"}, headers=hdrs,
                        timeout=(6, 12), allow_redirects=False)
    except Exception as e:
        return False, str(e)[:80]
    if r.status_code == 401:
        return False, "401: сессия панели истекла — войдите снова"
    if r.status_code != 200:
        return False, f"balances → {r.status_code}"
    try:
        rows = (r.json() or {}).get("resources") or []
    except Exception:
        return False, "не разобрал ответ панели"

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if isinstance(rid, dict):
            rid = rid.get("value", rid.get("id"))
        currency = amount = ""
        for f in (row.get("fields") or []):
            if not isinstance(f, dict):
                continue
            fn = str(f.get("name") or "").lower()
            fa = str(f.get("attribute") or "").lower()
            val = _strip_html(f.get("value"))
            if any(t in fn + fa for t in ("валют", "currency", "тип", "type")):
                currency = currency or val
            elif any(t in fn + fa for t in ("сумм", "amount", "баланс",
                                            "balance", "остат", "value")):
                amount = amount or val
        out.append({"id": str(rid), "currency": currency, "amount": amount,
                    "raw": row})
    return True, out


def panel_withdraw_fields_sync(
    cookie_string: str, balance_id: str, chosen: dict | None = None,
) -> tuple[bool, object]:
    """Blocking: describe the «Вывести» action so a payout can be filled in.

    Returns (True, {"key", "name", "balance_id", "fields": [...]}) or
    (False, error). `chosen` (e.g. {"system": 56}) is passed to Nova so the
    form reshapes to that choice — after a payment system is picked, the
    requisites it needs (card / wallet / phone / bank) turn visible and
    required, and a dependent select like «Банк» gains its options. Reading the
    form again with the choice made is how those become known, rather than
    hardcoding which method needs what.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    params = {"resources": str(balance_id)}
    for k, v in (chosen or {}).items():
        if v not in (None, ""):
            params[k] = v
    try:
        r = session.get(f"{PANEL_URL}/nova-api/{_BALANCE_RES}/actions",
                        params=params, headers=hdrs,
                        timeout=(6, 10), allow_redirects=False)
    except Exception as e:
        return False, f"не получил действия баланса: {str(e)[:60]}"
    if r.status_code == 401:
        return False, "401: сессия панели истекла — войдите снова"
    if r.status_code != 200:
        return False, f"actions: {r.status_code}"
    try:
        actions = [a for a in ((r.json() or {}).get("actions") or [])
                   if isinstance(a, dict)]
    except Exception:
        return False, "не разобрал ответ панели"

    a = _find_withdraw_action(actions)
    if not a:
        names = [(x.get("name") or x.get("uriKey") or "?") for x in actions]
        return False, f"действие вывода не найдено. Доступные: {names}"

    # Never submit these: the amount is asked for at withdrawal time, «к
    # получению» is computed and read-only.
    _SKIP = ("amount", "к_получению", "к_получения", "to_receive")
    out_fields = []
    for f in (a.get("fields") or []):
        if not isinstance(f, dict):
            continue
        attr = f.get("attribute")
        if not attr:
            continue
        extra = f.get("extraAttributes") or {}
        readonly = bool(f.get("readonly") or extra.get("readonly"))
        out_fields.append({
            "attribute": attr,
            "label": f.get("name") or attr,
            "component": f.get("component", ""),
            "options": _normalize_options(f),
            "required": bool(f.get("required")),
            "visible": bool(f.get("visible")),
            "readonly": readonly,
            "value": f.get("value"),
            "placeholder": f.get("placeholder") or "",
            "help": _strip_html(f.get("helpText")),
            "is_amount": attr == "amount",
            "skip": attr in _SKIP or readonly,
            "depends_on": f.get("dependsOn") or {},
            "component_key": f.get("dependentComponentKey") or "",
        })
    return True, {"key": a.get("uriKey"), "name": a.get("name") or "Вывести",
                  "balance_id": str(balance_id), "fields": out_fields}


def withdraw_limits(fields: list) -> dict:
    """Per-payout limits, read from the form's own help text.

    The «Сумма» field explains itself: «Минимальная сумма: 40 ₽»,
    «Максимальная сумма: 75 000 ₽», «Комиссия: 3% от суммы (мин. 30 ₽)».
    Auto-withdraw was submitting the entire balance, which a shop holding more
    than the per-payout ceiling can never pass — so the ceiling is read rather
    than assumed, and read from the panel so a change to it follows along.

    Returns {"min": float, "max": float, "fee_pct": float, "fee_min": float};
    a value the text does not state comes back 0.
    """
    import re as _re
    text = " ".join(str(f.get("help") or "") + " " + str(f.get("label") or "")
                    for f in (fields or []) if isinstance(f, dict))
    text = text.replace("\u00a0", " ")

    def num(pattern: str) -> float:
        m = _re.search(pattern, text, _re.I)
        if not m:
            return 0.0
        try:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return 0.0

    return {
        "min": num(r"минимальн\w*\s+сумм\w*\s*:?\s*([\d\s.,]+)"),
        "max": num(r"максимальн\w*\s+сумм\w*\s*:?\s*([\d\s.,]+)"),
        "fee_pct": num(r"комисси\w*\s*:?\s*([\d.,]+)\s*%"),
        "fee_min": num(r"мин\.?\s*([\d\s.,]+)\s*₽"),
    }


def panel_withdraw_limits_sync(cookie_string: str, balance_id: str,
                               chosen: dict | None = None) -> tuple[bool, object]:
    """Blocking: the payout limits the panel states for this balance.

    The choice of payment system has to be passed in. Read without it, the
    «Сумма» field comes back `visible: false` with `helpText: null` and
    `dependsOn: {"system": null}` — the limits simply are not in the form yet.
    They appear once the system is chosen, which is also when they can differ
    between СБП, Steam and crypto, so reading them per-system is not merely a
    workaround but the correct question.
    """
    ok, got = panel_withdraw_fields_sync(cookie_string, balance_id, chosen)
    if not ok or not isinstance(got, dict):
        return False, got
    return True, withdraw_limits(got.get("fields") or [])


def _finance_resource_names(session, hdrs) -> tuple[list[str], list[str]]:
    """(all resource uriKeys the panel exposes, the finance-looking ones)."""
    keys: list[str] = []
    for path in ("/nova-api/resources", "/nova-api/navigation"):
        try:
            r = session.get(PANEL_URL + path, headers=hdrs, timeout=(6, 12),
                            allow_redirects=False)
            if r.status_code == 200:
                keys += re.findall(r'"uriKey"\s*:\s*"([^"]+)"', r.text)
        except Exception:
            continue
    keys = sorted(set(keys))
    finance = [k for k in keys
               if any(t in k.lower() for t in
                      ("withdraw", "payout", "finance", "wallet", "balance",
                       "transaction", "payment", "vyvod", "cash"))]
    return keys, finance


def panel_finance_probe_sync(cookie_string: str) -> tuple[bool, str]:
    """Blocking: reveal what the panel exposes for withdrawal.

    A discovery step, not an action — it moves no money. It lists the panel's
    Nova resources, flags the finance-looking ones, and for each tries to read
    the "create withdrawal" form so its fields (amount, method, requisites) are
    seen exactly, not guessed.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    out: list[str] = []

    keys, finance = _finance_resource_names(session, hdrs)
    out.append(f"ресурсов панели: {len(keys)}")
    if keys:
        out.append("все: " + ", ".join(keys)[:400])
    out.append(f"похожие на финансы: {finance or 'нет по названию'}")

    # Probe the likely resource names even if navigation did not name them:
    # Nova resources are often reachable without appearing in the menu.
    seen = []
    for res in list(dict.fromkeys(finance + list(_FINANCE_RES))):
        try:
            r = session.get(f"{PANEL_URL}/nova-api/{res}",
                            params={"perPage": "1"}, headers=hdrs,
                            timeout=(5, 9), allow_redirects=False)
        except Exception as e:
            continue
        if r.status_code in (404,):
            continue
        seen.append(f"{res}→{r.status_code}")
        if r.status_code != 200:
            continue
        # It exists — read its create form and any actions
        out.append(f"\n=== /nova-api/{res} → 200 ===")
        try:
            rows = (r.json() or {}).get("resources") or []
            out.append(f"записей: {len(rows)}")
        except Exception:
            pass
        for cf_path in (f"/nova-api/{res}/creation-fields",):
            try:
                cr = session.get(PANEL_URL + cf_path,
                                 params={"editing": "true", "editMode": "create"},
                                 headers=hdrs, timeout=(6, 10),
                                 allow_redirects=False)
                out.append(f"{cf_path} → {cr.status_code}")
                if cr.status_code == 200:
                    fields = _parse_nova_fields_payload(cr.json())
                    for f in fields:
                        opts = _normalize_options(f)
                        out.append(
                            f"  • {f.get('attribute')} | {f.get('name')} | "
                            f"{f.get('component')}"
                            + (f" | обязат." if "required" in str(f.get('rules', '')) else "")
                            + (f" | варианты: {[o['label'] for o in opts][:6]}" if opts else ""))
            except Exception as e:
                out.append(f"{cf_path}: {str(e)[:50]}")
        # Actions on the resource (withdrawal could be an action, like «Премиум»)
        try:
            ar = session.get(f"{PANEL_URL}/nova-api/{res}/actions",
                             headers=hdrs, timeout=(6, 10), allow_redirects=False)
            if ar.status_code == 200:
                acts = [(a.get("name"), a.get("uriKey"))
                        for a in ((ar.json() or {}).get("actions") or [])
                        if isinstance(a, dict)]
                if acts:
                    out.append(f"  действия: {acts}")
        except Exception:
            pass

    out.append(f"\nпроверено ресурсов: {seen or 'ничего не ответило'}")

    # The «Вывести» action is the actual mechanism — dump its fields raw, the
    # same way /promo_debug does for «Премиум», so the withdrawal form (amount,
    # method, requisites) is seen exactly before anything is wired to it.
    ok_b, balances = panel_balances_sync(cookie_string)
    if ok_b and isinstance(balances, list) and balances:
        out.append("\n=== БАЛАНСЫ ===")
        for bl in balances:
            out.append(f"  id={bl['id']} | {bl['currency']} | {bl['amount']}")
        bid = None
        for bl in balances:                     # prefer a row that holds money
            try:
                if float(str(bl["amount"]).replace(" ", "").replace(",", ".") or 0) > 0:
                    bid = bl["id"]
                    break
            except ValueError:
                continue
        bid = bid or balances[0]["id"]
        try:
            ar = session.get(f"{PANEL_URL}/nova-api/{_BALANCE_RES}/actions",
                             params={"resources": str(bid)}, headers=hdrs,
                             timeout=(6, 10), allow_redirects=False)
            out.append(f"\n=== действие «Вывести» (balance #{bid}) → "
                       f"{ar.status_code} ===")
            if ar.status_code == 200:
                acts = [a for a in ((ar.json() or {}).get("actions") or [])
                        if isinstance(a, dict)]
                a = _find_withdraw_action(acts)
                if not a:
                    out.append(f"не нашёл среди {[x.get('uriKey') for x in acts]}")
                else:
                    out.append(f"uriKey: {a.get('uriKey')}")
                    for f in (a.get("fields") or []):
                        if isinstance(f, dict):
                            out.append("• " + _json.dumps(f, ensure_ascii=False)[:900])
        except Exception as e:
            out.append(f"действие: {str(e)[:60]}")

    return True, "\n".join(out)


def panel_withdraw_sync(
    cookie_string: str, balance_id: str, action_key: str, values: dict,
    uid: int | None = None, confirm: bool = False,
) -> tuple[bool, str]:
    """Blocking: run the «Вывести» action on a balance — MOVES MONEY OUT.

    Refuses without confirm=True, and submits only the field values it was
    given, plus the balance id the action runs on: no money-moving field is
    ever invented. `values` is what the seller filled from
    panel_withdraw_fields_sync (amount, method, requisites).
    """
    if not confirm:
        return False, "вывод не подтверждён"
    if not values:
        return False, "не заданы поля вывода — сначала настройте способ и сумму"
    if not action_key:
        return False, "не известен ключ действия вывода"

    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    payload = {"resources": str(balance_id)}
    payload.update({k: v for k, v in values.items() if v not in (None, "")})
    try:
        r = session.post(
            f"{PANEL_URL}/nova-api/{_BALANCE_RES}/action?action={action_key}",
            data=payload, headers=hdrs, timeout=(6, 20), allow_redirects=False)
    except Exception as e:
        return False, f"запрос не прошёл: {str(e)[:80]}"

    if r.status_code == 401:
        return False, "401: сессия панели истекла — войдите снова"
    if r.status_code in (200, 201, 204):
        # A refused action also returns 200 with the reason in the body — the
        # same trap «Премиум» had, so the answer is inspected, not the code.
        ok, text = _action_result(r, "Заявка на вывод создана")
        if ok:
            _save_refreshed_cookies(uid, cookie_string, session)
        return ok, text
    if r.status_code == 422:
        try:
            body = r.json()
            msg = body.get("message") or ""
            errs = body.get("errors") or {}
        except Exception:
            msg, errs = r.text[:200], {}
        detail = "; ".join(f"{k}: {v[0] if isinstance(v, list) else v}"
                           for k, v in list(errs.items())[:5])
        return False, f"панель не приняла: {msg} {detail}".strip()
    return False, f"панель ответила {r.status_code}: {r.text[:150]}"


# ---------------------------------------------------------------------------
# Support / moderation chat — reply through the panel
#
# The Integration API refuses to send to a chat without an active order
# (no_active_orders_in_chat), which is every support thread. Sending has to go
# through the panel, and an earlier scan got 401 there — likely because the
# chat wants a Bearer token embedded in the page, not the Laravel cookies. The
# probe below finds that token and the real send route without sending
# anything; panel_chat_send_sync then posts the reply.
# ---------------------------------------------------------------------------

def _extract_bearer(html: str) -> str:
    """A chat API token embedded in the panel page, if there is one."""
    for pat in (r'"api_token"\s*:\s*"([^"]{20,})"',
                r'"access_token"\s*:\s*"([^"]{20,})"',
                r'"bearer"\s*:\s*"([^"]{20,})"',
                r'"token"\s*:\s*"([A-Za-z0-9._\-]{30,})"',
                r'Bearer\s+([A-Za-z0-9._\-]{30,})'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def panel_chat_probe_sync(cookie_string: str, chat_id: str,
                          api_token: str = "") -> tuple[bool, str]:
    """Blocking, read-only: find how the panel sends a chat message.

    Sends nothing. Reads the chat page, digs out any Bearer token, tries the
    likely message endpoints with cookies (and with the token if cookies 401),
    and scans the page scripts for the POST route the reply goes to.
    `api_token` is the Integration API bearer that already reads the chat — it
    is tried against the panel chat API, since that is the token the SPA is
    likely storing.
    """
    cid = "".join(ch for ch in str(chat_id) if ch.isdigit()) or str(chat_id)
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    # Sanctum only treats a cookie request as authenticated when it looks like
    # it comes from the SPA — that means an Origin header, not just Referer.
    stateful = {**hdrs, "Origin": PANEL_URL, "Referer": f"{PANEL_URL}/chats/{cid}"}
    out: list[str] = []

    have = sorted({c.name for c in session.cookies})
    out.append("куки: " + (", ".join(have) or "нет"))
    out.append("есть laravel_session: "
               + ("да" if any("session" in c.lower() for c in have) else "НЕТ"))
    out.append("есть XSRF-TOKEN: "
               + ("да" if any("xsrf" in c.lower() for c in have) else "НЕТ"))

    token = ""
    page_js: list[str] = []
    for page in ("/", f"/chats/{cid}"):
        try:
            r = session.get(PANEL_URL + page, timeout=(6, 15))
        except Exception as e:
            out.append(f"{page}: {str(e)[:50]}")
            continue
        if not token:
            token = _extract_bearer(r.text)
        page_js += re.findall(r'src="(/[^"]+\.js[^"]*)"', r.text)
    out.append(f"Bearer на странице: {'найден' if token else 'нет'}")

    def _get(path, extra=None):
        try:
            r = session.get(PANEL_URL + path, headers={**hdrs, **(extra or {})},
                            timeout=(5, 10), allow_redirects=False)
        except Exception as e:
            return f"{path}: {str(e)[:40]}"
        if r.status_code == 404:
            return ""
        return f"{path} → {r.status_code}: {r.text[:80].strip()}"

    out.append("\n— чтение с куками —")
    for p in (f"/api/chats/{cid}/messages", f"/api/chats/{cid}"):
        line = _get(p)
        if line:
            out.append(line)

    # The decisive test: the same call, but marked as a stateful SPA request.
    out.append("\n— то же, но с Origin (Sanctum stateful) —")
    for p in (f"/api/chats/{cid}/messages", f"/api/chats/{cid}"):
        line = _get(p, stateful)
        if line:
            out.append(line)

    # Also try the Nova host and a web-guard route, in case the chat is not an
    # /api/ resource at all.
    out.append("\n— другие хосты/гварды —")
    for p in (f"/nova-api/chats/{cid}", f"/nova-vendor/chat/{cid}/messages",
              f"/chats/{cid}/messages"):
        line = _get(p, stateful)
        if line:
            out.append(line)

    # The Integration bearer that already reads this chat — the likeliest token
    # the SPA keeps in localStorage. Try it against the panel API and against
    # the marketplace API host directly.
    if api_token:
        out.append("\n— с интеграционным Bearer —")
        bearer = {**stateful, "Authorization": f"Bearer {api_token}"}
        for p in (f"/api/chats/{cid}/messages", f"/api/chats/{cid}"):
            line = _get(p, bearer)
            if line:
                out.append(line)
        import requests as _rq
        for base in ("https://api.yoo.market", "https://api.yoo.market/v1",
                     "https://api.yoo.market/app/v1"):
            for p in (f"/chats/{cid}/messages", f"/chats/{cid}"):
                try:
                    rr = _rq.get(base + p, headers={
                        "Authorization": f"Bearer {api_token}",
                        "Accept": "application/json"},
                        timeout=(5, 10), verify=False)
                except Exception as e:
                    continue
                if rr.status_code == 404:
                    continue
                out.append(f"{base}{p} → {rr.status_code}: {rr.text[:80].strip()}")

    # Scan the main bundles for the chat endpoints and how auth is attached.
    out.append("\n— в JS —")
    sends, auth = set(), set()
    for src in list(dict.fromkeys(page_js))[:8]:
        try:
            js = session.get(PANEL_URL + src, timeout=(6, 20)).text
        except Exception:
            continue
        for m in re.findall(r'[`"\']([^`"\']*chats?/[^`"\']*)[`"\']', js, re.I):
            if 4 < len(m) < 70:
                sends.add(m)
        for m in re.findall(r'(Authorization|Bearer|sanctum|localStorage\.\w+'
                            r'|withCredentials)', js, re.I):
            auth.add(m.lower())
    out.append(f"адреса с 'chat': {sorted(sends)[:15] or 'ничего'}")
    out.append(f"признаки авторизации: {sorted(auth)[:10] or 'ничего'}")
    return True, "\n".join(out)


def panel_chat_send_sync(
    cookie_string: str, chat_id: str, text: str,
    chat_token: str = "", endpoint: str = "",
) -> tuple[bool, str]:
    """Blocking: send a message to a support/moderation chat.

    The chat API authenticates by a Bearer token the panel stores in
    localStorage at login (cookies 401 there). `chat_token` is that token,
    captured during login and passed in from stored creds; without it, this
    cannot authenticate and says so plainly instead of guessing.
    """
    if not chat_token:
        return False, ("нет токена чата — войдите в панель заново, чтобы бот "
                       "получил его (кнопка «Войти по email»)")

    import requests as _rq
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    _rq.packages.urllib3.disable_warnings(InsecureRequestWarning)

    cid = "".join(ch for ch in str(chat_id) if ch.isdigit()) or str(chat_id)
    hdrs = {
        "Authorization": f"Bearer {chat_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": PANEL_URL,
        "Referer": f"{PANEL_URL}/chats/{cid}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
    }
    # Both hosts the chat API is served from, panel first.
    targets = []
    if endpoint:
        targets.append(PANEL_URL + (endpoint if endpoint.startswith("/") else "/" + endpoint))
    targets += [
        f"{PANEL_URL}/api/chats/{cid}/messages",
        f"{PANEL_URL}/api/chats/{cid}/send",
        f"https://api.yoo.market/chats/{cid}/messages",
        f"https://api.yoo.market/v1/chats/{cid}/messages",
    ]
    payloads = ({"text": text}, {"message": text}, {"body": text})
    last = ""
    for url in targets:
        for body in payloads:
            try:
                r = _rq.post(url, json=body, headers=hdrs,
                             timeout=(6, 15), verify=False, allow_redirects=False)
            except Exception as e:
                last = str(e)[:80]
                continue
            if r.status_code in (200, 201, 204):
                return True, "✅ Отправлено в поддержку"
            if r.status_code == 404:
                break                      # this url is wrong, next url
            last = f"{r.status_code}: {r.text[:100]}"
            if r.status_code in (401, 403):
                return False, ("токен чата не подошёл (401). Войдите в панель "
                               "заново, чтобы бот обновил токен.")
    return False, f"не удалось отправить: {last or 'адрес не найден'}"


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


def panel_item_actions_sync(cookie_string: str, item_id: str) -> str:
    """Read-only: the Nova actions the panel offers for one listing.

    Used by /restore_debug to establish two things the Integration API cannot
    answer: whether the panel knows this id at all (the API's ad ids and the
    panel's item ids need not be the same space), and which action publishes
    it. Runs no action — it only lists them.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.get(
            f"{PANEL_URL}/nova-api/items/actions",
            params={"resources": str(item_id)},
            headers=hdrs, timeout=(6, 10), allow_redirects=False,
        )
    except Exception as e:
        return f"запрос не удался: {str(e)[:80]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code} — панель не отдала действия"
    try:
        actions = (r.json() or {}).get("actions") or []
    except Exception:
        return "ответ не разобран"
    if not actions:
        return "действий нет (id не знаком панели?)"
    names = [f"{a.get('name') or '?'}[{a.get('uriKey') or '?'}]"
             for a in actions if isinstance(a, dict)]
    return ", ".join(names)[:400]


def panel_resource_census_sync(cookie_string: str, needle: str = "") -> str:
    """Read-only: every panel resource with its row count and a few names.

    The failure trace is capped at a few hundred characters, so a run that
    walked a dozen resources showed the first two and hid the answer. This
    walks the same list and reports all of it, marking any resource whose rows
    mention `needle` — which is what identifies where the listings live.
    """
    import re as _re
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)

    def key(t) -> str:
        return _re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", str(t or "")).lower()

    want = key(needle)
    names = list(_ITEM_RESOURCES)
    for r in panel_discover_resources_sync(cookie_string):
        if r not in names:
            names.append(r)

    lines: list[str] = []
    for res in names:
        try:
            r = session.get(f"{PANEL_URL}/nova-api/{res}",
                            params={"perPage": "100"}, headers=hdrs,
                            timeout=(6, 12), allow_redirects=False)
        except Exception as e:
            lines.append(f"{res}: ошибка {str(e)[:30]}")
            continue
        if r.status_code != 200:
            continue                      # 404 is noise; only what exists matters
        try:
            rows = [x for x in ((r.json() or {}).get("resources") or [])
                    if isinstance(x, dict)]
        except Exception:
            lines.append(f"{res}: ответ не разобран")
            continue
        titles = [_row_title(x) or "?" for x in rows]
        hit = ""
        if want:
            for t in titles:
                if want[:20] in key(t) or key(t)[:20] in want:
                    hit = f"  ⬅️ СОВПАЛО: {t[:40]}"
                    break
        lines.append(f"{res}: {len(rows)} — "
                     + ", ".join(t[:26] for t in titles[:3]) + hit)
    return "\n".join(lines) or "панель не отдала ни одного ресурса"


def panel_shop_balance_sync(cookie_string: str,
                            shop_id: str = "") -> tuple[bool, object]:
    """Blocking: the shop's balance, read where the panel actually shows it.

    The seller's own screenshot settled this: the figure lives on the shop's
    page — /resources/shops/{id} — not in a `balances` resource, which is what
    the code had been asking for. Returns (True, {"amount": float, "field":
    name, "shop": id}) or (False, reason).

    The detail record is fetched rather than the list row: Nova's index omits
    most fields, and the balance is one of the omitted ones on this panel.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)

    ids = [str(shop_id)] if shop_id else []
    if not ids:
        try:
            r = session.get(f"{PANEL_URL}/nova-api/shops",
                            params={"perPage": "50"}, headers=hdrs,
                            timeout=(6, 12), allow_redirects=False)
        except Exception as e:
            return False, f"shops: {str(e)[:60]}"
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code != 200:
            return False, f"shops → {r.status_code}"
        try:
            rows = (r.json() or {}).get("resources") or []
        except Exception:
            return False, "shops: ответ не разобран"
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            if isinstance(rid, dict):
                rid = rid.get("value", rid.get("id"))
            if rid is not None:
                ids.append(str(rid))
        if not ids:
            return False, "в панели нет ни одного магазина"

    tried = []
    for sid in ids[:5]:
        try:
            r = session.get(f"{PANEL_URL}/nova-api/shops/{sid}",
                            headers=hdrs, timeout=(6, 12),
                            allow_redirects=False)
        except Exception as e:
            tried.append(f"{sid}: {str(e)[:30]}")
            continue
        if r.status_code != 200:
            tried.append(f"{sid}: {r.status_code}")
            continue
        try:
            fields = ((r.json() or {}).get("resource") or {}).get("fields") or []
        except Exception:
            tried.append(f"{sid}: ответ не разобран")
            continue
        for f in fields:
            if not isinstance(f, dict):
                continue
            label = (str(f.get("name") or "") + " "
                     + str(f.get("attribute") or "")).lower()
            # «Сумма продаж» and «оплаченные заказы» are metrics on the same
            # page; the balance is the field that says balance.
            if not any(t in label for t in ("баланс", "balance", "средства")):
                continue
            if any(t in label for t in ("продаж", "заказ", "sales", "order")):
                continue
            raw = _strip_html(f.get("value"))
            if raw in (None, ""):
                continue
            # Разбор один на весь бот. Здешний убирал пробелы, «₽» и запятые
            # по списку — и спотыкался обо всё остальное: «руб.», узкий
            # пробел, разряды точкой. Поле «Баланс» находилось, значение не
            # разбиралось, и продавец видел прочерк.
            from orderfields import parse_amount
            amount = parse_amount(raw)
            if amount is None:
                # Найденное, но неразобранное — самое обидное молчание:
                # поле то самое, а сумма потеряна. Называем значение.
                tried.append(f"{sid}: поле «{f.get('name')}» = «{raw[:40]}» "
                             "не разобрано как число")
                continue
            return True, {"amount": amount,
                          "field": str(f.get("name") or f.get("attribute")),
                          "shop": sid}
        # Nothing matched: report what the page does offer, so the field can be
        # named instead of guessed at again.
        names = [str(f.get("name") or f.get("attribute"))
                 for f in fields if isinstance(f, dict)][:14]
        tried.append(f"{sid}: поля — {', '.join(n for n in names if n)}")
    return False, "; ".join(tried)[:400] or "магазин не прочитан"


def panel_withdraw_probe_sync(cookie_string: str) -> str:
    """Read-only: where the «Вывести» action actually lives.

    Withdrawal asks /nova-api/balances/actions — the same resource that turned
    out to hold no balance, which is on the shop's own record instead. This
    reports, for the shop and for `balances`, what each answers and which
    actions each offers, so the payout is aimed from evidence rather than from
    the guess that was inherited. Runs nothing: it lists actions only.
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    out: list[str] = []

    shop_ids: list[str] = []
    try:
        r = session.get(f"{PANEL_URL}/nova-api/shops", params={"perPage": "20"},
                        headers=hdrs, timeout=(6, 12), allow_redirects=False)
        out.append(f"shops → {r.status_code}")
        if r.status_code == 200:
            for row in (r.json() or {}).get("resources") or []:
                rid = row.get("id") if isinstance(row, dict) else None
                if isinstance(rid, dict):
                    rid = rid.get("value", rid.get("id"))
                if rid is not None:
                    shop_ids.append(str(rid))
            out.append(f"магазины: {', '.join(shop_ids) or 'нет'}")
    except Exception as e:
        out.append(f"shops: {str(e)[:50]}")

    for res, ids in (("shops", shop_ids[:2]), (_BALANCE_RES, [""])):
        for rid in ids or [""]:
            params = {"resources": rid} if rid else {}
            try:
                r = session.get(f"{PANEL_URL}/nova-api/{res}/actions",
                                params=params, headers=hdrs,
                                timeout=(6, 10), allow_redirects=False)
            except Exception as e:
                out.append(f"{res}/actions: {str(e)[:50]}")
                continue
            label = f"{res}{'#' + rid if rid else ''}/actions → {r.status_code}"
            if r.status_code != 200:
                out.append(label)
                continue
            try:
                acts = (r.json() or {}).get("actions") or []
            except Exception:
                out.append(f"{label}: ответ не разобран")
                continue
            names = [f"{a.get('name') or '?'}[{a.get('uriKey') or '?'}]"
                     for a in acts if isinstance(a, dict)]
            out.append(f"{label}: {', '.join(names) or 'действий нет'}")
    return "\n".join(out)[:1800]


# ─────────────────────────────── статистика ───────────────────────────────
#
# The bot's statistics used to be built entirely out of `known_orders` — orders
# the poller happened to see while it was running. Anything sold before the bot
# started, or while it was down, simply did not exist, and a restarted container
# reported an empty shop. The panel keeps the real ledger, so the figures are
# read from there and the local history is only a fallback.

_SPACES = "    "


def _num(value) -> float | None:
    """A signed number out of whatever the panel rendered.

    Panel money arrives as text: «1 234,56 ₽», «−300 ₽», «+50». The comma is a
    decimal separator here (the balance «605,226 ₽» is 605 roubles 226 —
    settled with the seller), and the thousands separator is a space.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    text = _strip_html(value)
    if not text:
        return None
    for ch in _SPACES:
        text = text.replace(ch, "")
    text = (text.replace(" ", "").replace("₽", "").replace("руб", "")
            .replace("−", "-").replace("–", "-").replace("+", ""))
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S",
               "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d")


def _ts(value) -> float | None:
    """Epoch seconds out of a panel date, in any of the shapes it uses."""
    from datetime import datetime, timezone
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Milliseconds are also seen; anything past year 5000 is one.
        v = float(value)
        return v / 1000 if v > 1e11 else v
    text = _strip_html(value).strip()
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text[:19], fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _fields_of(row) -> list[dict]:
    """A Nova row's fields as a list, whichever container it used."""
    fields = row.get("fields") if isinstance(row, dict) else None
    if isinstance(fields, dict):
        fields = list(fields.values())
    return [f for f in (fields or []) if isinstance(f, dict)]


def _field_text(f: dict) -> str:
    """The readable value of a field — BelongsTo renders as a nested dict."""
    val = f.get("value")
    if isinstance(val, dict):
        val = (val.get("title") or val.get("name") or val.get("display")
               or val.get("value") or "")
    if isinstance(val, list):
        val = ", ".join(str(x) for x in val if not isinstance(x, (dict, list)))
    if isinstance(val, bool):
        return "да" if val else "нет"
    return _strip_html(val)


def _row_id(row: dict):
    rid = row.get("id")
    if isinstance(rid, dict):
        rid = rid.get("value", rid.get("id"))
    return rid


# What a money row means. Read from the row's own wording, because the panel
# does not label direction: a payout and a sale are both just «операция».
_OUT_KWS = ("вывод", "вывел", "выплат", "списан", "списыв", "withdraw", "payout",
            "премиум", "премк", "поднят", "продвиж", "реклам", "комисс",
            "штраф", "расход", "оплата услуг", "покупк", "debit", "минус")
_IN_KWS = ("продаж", "продан", "заказ", "пополн", "начисл", "зачисл", "доход",
           "приход", "поступ", "sale", "order", "deposit", "credit", "плюс",
           "возврат средств")


def _direction(haystack: str, amount: float | None) -> str:
    """"in" | "out" | "?" — what this operation did to the balance."""
    if amount is not None and amount < 0:
        return "out"
    low = haystack.lower()
    for kw in _OUT_KWS:
        if kw in low:
            return "out"
    for kw in _IN_KWS:
        if kw in low:
            return "in"
    return "?"


_SALE_KWS = ("продаж", "продан", "заказ", "sale", "order")
_BUMP_KWS_OPS = ("премиум", "премк", "поднят", "продвиж", "реклам", "vip",
                 "выделен", "закреп")
_PAYOUT_KWS = ("вывод", "вывел", "выплат", "withdraw", "payout")


def _op_kind(haystack: str, direction: str) -> str:
    """Finer than direction: sale / bump / payout / other."""
    low = haystack.lower()
    if any(k in low for k in _PAYOUT_KWS):
        return "payout"
    if any(k in low for k in _BUMP_KWS_OPS):
        return "bump"
    if any(k in low for k in _SALE_KWS):
        return "sale"
    return "in" if direction == "in" else ("out" if direction == "out" else "other")


def _parse_operation(row: dict) -> dict:
    """One ledger row → {id, ts, amount, kind, direction, title, status, text}."""
    ts = amount = None
    title = status = ""
    kind_bits: list[str] = []
    all_text: list[str] = []

    for f in _fields_of(row):
        label = (str(f.get("name") or "") + " "
                 + str(f.get("attribute") or "")).lower()
        comp = str(f.get("component") or "").lower()
        text = _field_text(f)
        if not text:
            continue
        all_text.append(text)
        if ts is None and ("date" in comp or any(
                t in label for t in ("created", "дата", "date", "время", "time"))):
            got = _ts(f.get("value") if not isinstance(f.get("value"), dict) else text)
            if got:
                ts = got
                continue
        if amount is None and any(t in label for t in (
                "сумм", "amount", "цена", "price", "total", "итог", "money",
                "стоим", "value")):
            got = _num(f.get("value"))
            if got is not None:
                amount = got
                continue
        if any(t in label for t in ("тип", "type", "операц", "operation",
                                    "назнач", "kind", "категор", "commen",
                                    "коммент", "описан", "descr")):
            kind_bits.append(text)
        elif any(t in label for t in ("статус", "status", "состоян", "state")):
            status = status or text
        elif any(t in label for t in ("товар", "наимен", "назван", "title",
                                      "name", "объявл", "лот", "item")):
            title = title or text

    haystack = " ".join(kind_bits + [title, status, _row_title(row)])
    if not haystack.strip():
        haystack = " ".join(all_text)
    direction = _direction(haystack, amount)
    return {
        "id": _row_id(row),
        "ts": ts,
        "amount": abs(amount) if amount is not None else None,
        "signed": amount,
        "direction": direction,
        "kind": _op_kind(haystack, direction),
        "title": title or _row_title(row),
        "status": status,
        "type": " / ".join(b for b in kind_bits if b)[:80],
    }


_OPS_RESOURCES = ("operations", "operation", "transactions", "payments",
                  "orders", "sales", "deals")


def panel_operations_sync(cookie_string: str, resource: str = "operations",
                          pages: int = 6, per_page: int = 100,
                          since: float = 0.0) -> tuple[bool, object]:
    """Blocking: the shop's money ledger from the panel, newest first.

    Returns (True, [operation, …]) or (False, reason). `since` (epoch seconds,
    0 = everything) both filters the result and stops paging once a whole page
    predates it — but the early stop applies only while the rows really are
    coming newest-first, since a panel that sorts the other way would otherwise
    be cut off at its oldest end, which is exactly the data being asked for.
    """
    try:
        session = _make_panel_requests_session(cookie_string)
        hdrs = _panel_xsrf_headers(session, cookie_string)
    except Exception as e:
        return False, f"сессия панели: {str(e)[:80]}"

    ops: list[dict] = []
    descending = True
    last_ts: float | None = None
    reason = ""
    read_a_page = False

    for page in range(1, max(1, pages) + 1):
        params = {"perPage": str(per_page), "page": str(page),
                  "orderBy": "id", "orderByDirection": "desc"}
        try:
            r = session.get(f"{PANEL_URL}/nova-api/{resource}", params=params,
                            headers=hdrs, timeout=(6, 20), allow_redirects=False)
        except Exception as e:
            reason = f"{resource}: {str(e)[:60]}"
            break
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code != 200:
            reason = f"{resource} → {r.status_code}"
            break
        try:
            body = r.json() or {}
        except Exception:
            reason = f"{resource}: ответ не разобран"
            break
        rows = [x for x in (body.get("resources") or []) if isinstance(x, dict)]
        read_a_page = True
        if not rows:
            break

        page_max_ts = 0.0
        for row in rows:
            op = _parse_operation(row)
            if op["ts"]:
                if last_ts is not None and op["ts"] > last_ts + 60:
                    descending = False
                last_ts = op["ts"]
                page_max_ts = max(page_max_ts, op["ts"])
            if since and (op["ts"] or 0) < since:
                continue
            ops.append(op)

        if since and descending and page_max_ts and page_max_ts < since:
            break
        if not body.get("next_page_url"):
            break

    # An empty result after a page really was read is an answer — "no
    # operations in this window" — not a failure to reach the panel. Reporting
    # it as an error is what would silently push the screen onto the local
    # fallback and relabel correct figures as untrustworthy.
    if not ops and not read_a_page:
        return False, reason or f"{resource}: пусто"
    return True, ops


# Где панель держит отзывы. Как и с книгой операций, имя ресурса угадать
# нельзя — оно у каждой панели своё, поэтому кандидаты перебираются.
_REVIEW_RESOURCES = ("reviews", "review", "feedbacks", "feedback",
                     "comments", "ratings", "shop-reviews", "otzyvy")

_RATING_KEYS = ("rating", "stars", "score", "mark", "ball", "оценк")
_TEXT_KEYS = ("text", "comment", "body", "message", "content", "отзыв")
_AUTHOR_KEYS = ("user", "author", "buyer", "customer", "client", "покупател")


def _parse_review(row: dict) -> dict:
    """Одна строка отзыва: оценка, текст, автор, дата, товар.

    Текст ищется по именам полей — а имена у панели свои, и угадать их
    списком не вышло: отзывы приходили без текста при том, что текст в
    строке был. Поэтому две правки. Первая: поле, похожее сразу на автора
    и на текст (`user_comment` подходит под оба списка), больше не
    съедается автором молча — сначала пробуется как текст. Вторая: если по
    именам не нашлось ничего, берётся самое длинное строковое поле строки.
    Отзыв — это самый длинный текст в своей записи; хуже пустоты не будет.
    """
    out = {"id": _row_id(row), "rating": None, "text": "", "author": "",
           "ts": None, "title": ""}
    spare: list[tuple[int, str]] = []
    for f in _fields_of(row):
        attr = str(f.get("attribute") or "").lower()
        value = _field_text(f)
        if out["rating"] is None and any(k in attr for k in _RATING_KEYS):
            num = _num(value)
            # Оценка — это 1..5. Число из поля «rating_count» ею не является.
            if num is not None and 0 <= num <= 5:
                out["rating"] = num
                continue
        if not out["ts"]:
            ts = _ts(value) if any(k in attr for k in
                                   ("created", "date", "time", "дата")) else None
            if ts:
                out["ts"] = ts
                continue
        # Текст — раньше автора: `user_comment` подходит под оба списка, и
        # при прежнем порядке уходил в автора, а отзыв оставался пустым.
        if not out["text"] and any(k in attr for k in _TEXT_KEYS):
            out["text"] = value[:400]
            continue
        if not out["author"] and any(k in attr for k in _AUTHOR_KEYS):
            out["author"] = value[:40]
            continue
        if not out["title"] and any(k in attr for k in ("ad", "item", "product",
                                                        "lot", "товар")):
            out["title"] = value[:60]
            continue
        if value and not _num(value):
            spare.append((len(value), value))
    # Запасной вариант — только для строки, которая уже опознана как отзыв
    # по оценке. Иначе любая строка любого ресурса получает «текст» из
    # своего самого длинного поля, и категория с полем `name` начинает
    # считаться отзывом: ровно та ошибка, из-за которой раньше принимали за
    # отзывы что попало.
    if not out["text"] and out["rating"] is not None and spare:
        out["text"] = max(spare)[1][:400]
    return out


def panel_review_fields_sync(cookie_string: str,
                             resource: str = "") -> tuple[bool, object]:
    """Какие поля панель кладёт в строку отзыва — именами и значениями.

    Отзывы пришли без текста, а имена полей у панели свои. Угадывать их
    списком мы уже попробовали; читать, что там на самом деле, дешевле.
    Только чтение: одна страница, одна строка.
    """
    try:
        session = _make_panel_requests_session(cookie_string)
        hdrs = _panel_xsrf_headers(session, cookie_string)
    except Exception as e:
        return False, f"сессия панели: {str(e)[:80]}"
    tried: list[str] = []
    for res in ([resource] if resource else list(_REVIEW_RESOURCES)):
        try:
            r = session.get(f"{PANEL_URL}/nova-api/{res}",
                            params={"perPage": "1", "page": "1"},
                            headers=hdrs, timeout=(6, 20),
                            allow_redirects=False)
        except Exception as e:
            tried.append(f"{res}: {str(e)[:40]}")
            continue
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code != 200:
            tried.append(f"{res}: HTTP {r.status_code}")
            continue
        try:
            rows = (r.json() or {}).get("resources") or []
        except Exception:
            tried.append(f"{res}: ответ не разобран")
            continue
        if not rows:
            tried.append(f"{res}: пусто")
            continue
        out = []
        for f in _fields_of(rows[0]):
            value = _field_text(f)
            out.append({"attribute": str(f.get("attribute") or ""),
                        "component": str(f.get("component") or "")[:30],
                        "len": len(value), "value": value[:120]})
        return True, {"resource": res, "fields": out}
    return False, "; ".join(tried)[:300] or "ни один ресурс не ответил"


def panel_reviews_sync(cookie_string: str, resource: str = "",
                       pages: int = 2, per_page: int = 50,
                       ) -> tuple[bool, object]:
    """Blocking: отзывы магазина из панели, новые первыми.

    В Integration API отзывов нет — прежний код перебирал /reviews, /feedback
    и /ratings, получал 404 и молча возвращал пустой список. Тумблер
    «Новые отзывы» при этом включался и не делал ничего.

    Returns (True, {"resource": …, "reviews": [...]}) или (False, почему).
    """
    try:
        session = _make_panel_requests_session(cookie_string)
        hdrs = _panel_xsrf_headers(session, cookie_string)
    except Exception as e:
        return False, f"сессия панели: {str(e)[:80]}"

    candidates = [resource] if resource else list(_REVIEW_RESOURCES)
    tried: list[str] = []
    for res in candidates:
        rows: list[dict] = []
        # Дочитали ли до конца. Без этого «1 из 100» на магазине с тысячей
        # отзывов — вранье в чистом виде: столько мы прочитали, а не столько
        # их есть.
        more = False
        for page in range(1, max(1, pages) + 1):
            try:
                r = session.get(
                    f"{PANEL_URL}/nova-api/{res}",
                    params={"perPage": str(per_page), "page": str(page),
                            "orderBy": "id", "orderByDirection": "desc"},
                    headers=hdrs, timeout=(6, 20), allow_redirects=False)
            except Exception as e:
                tried.append(f"{res}: {str(e)[:40]}")
                break
            if r.status_code == 401:
                return False, "401: сессия панели истекла — войдите снова"
            if r.status_code != 200:
                tried.append(f"{res}: HTTP {r.status_code}")
                break
            try:
                chunk = [x for x in ((r.json() or {}).get("resources") or [])
                         if isinstance(x, dict)]
            except Exception:
                tried.append(f"{res}: ответ не разобран")
                break
            if not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < per_page:
                break
            # Страница пришла полной, а страницы кончились — значит дальше
            # ещё есть.
            more = page >= max(1, pages)
        if not rows:
            if not any(res in x for x in tried):
                tried.append(f"{res}: пусто")
            continue
        reviews = [_parse_review(x) for x in rows]
        # Ресурс с оценками или текстами — отзывы. Иначе это что-то другое,
        # что просто откликнулось на похожее имя.
        real = [x for x in reviews if x["rating"] is not None or x["text"]]
        tried.append(f"{res}: {len(rows)} строк, похожих на отзыв {len(real)}")
        if real:
            return True, {"resource": res, "reviews": real, "more": more}
    return False, "; ".join(tried)[:400] or "ни один ресурс не ответил"


def panel_find_ledger_sync(cookie_string: str) -> tuple[bool, object]:
    """Blocking: which panel resource actually holds dated money rows.

    `operations` is the one this panel uses, but naming it in one place and
    guessing elsewhere is what sent the balance hunt around in circles. This
    tries the known candidates and returns the first that answers with rows
    carrying both a date and an amount → (True, (resource, ops)).
    """
    best: tuple[str, list] | None = None
    tried: list[str] = []
    for res in _OPS_RESOURCES:
        ok, got = panel_operations_sync(cookie_string, resource=res, pages=1,
                                        per_page=50)
        if not ok or not isinstance(got, list):
            tried.append(f"{res}: {str(got)[:40]}")
            continue
        dated = [o for o in got if o.get("ts") and o.get("amount") is not None]
        tried.append(f"{res}: {len(got)} строк, с датой и суммой {len(dated)}")
        if dated and (best is None or len(dated) > len(best[1])):
            best = (res, got)
        if best and len(dated) >= 5:
            break
    if best:
        return True, best
    return False, "; ".join(tried)[:400] or "ни один ресурс не ответил"


def panel_shop_metrics_sync(cookie_string: str,
                            shop_id: str = "") -> tuple[bool, object]:
    """Blocking: the numbers the panel prints on the shop's own page.

    «Оплаченные заказы», «Сумма продаж», rating, review count — all-time totals
    the seller can check against with their own eyes. Returns
    (True, {"shop": id, "metrics": {name: {"text","value"}}}) or (False, why).
    """
    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)

    ids = [str(shop_id)] if shop_id else []
    if not ids:
        try:
            r = session.get(f"{PANEL_URL}/nova-api/shops", params={"perPage": "50"},
                            headers=hdrs, timeout=(6, 12), allow_redirects=False)
        except Exception as e:
            return False, f"shops: {str(e)[:60]}"
        if r.status_code == 401:
            return False, "401: сессия панели истекла — войдите снова"
        if r.status_code != 200:
            return False, f"shops → {r.status_code}"
        try:
            for row in (r.json() or {}).get("resources") or []:
                rid = _row_id(row) if isinstance(row, dict) else None
                if rid is not None:
                    ids.append(str(rid))
        except Exception:
            return False, "shops: ответ не разобран"
    if not ids:
        return False, "в панели нет ни одного магазина"

    sid = ids[0]
    try:
        r = session.get(f"{PANEL_URL}/nova-api/shops/{sid}", headers=hdrs,
                        timeout=(6, 15), allow_redirects=False)
    except Exception as e:
        return False, f"shops/{sid}: {str(e)[:60]}"
    if r.status_code != 200:
        return False, f"shops/{sid} → {r.status_code}"
    try:
        fields = ((r.json() or {}).get("resource") or {}).get("fields") or []
    except Exception:
        return False, f"shops/{sid}: ответ не разобран"

    metrics: dict[str, dict] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or f.get("attribute") or "").strip()
        if not name:
            continue
        text = _field_text(f)
        if not text:
            continue
        metrics[name] = {"text": text, "value": _num(f.get("value"))}
    if not metrics:
        return False, f"shops/{sid}: страница без полей"
    return True, {"shop": sid, "metrics": metrics}


def panel_stats_probe_sync(cookie_string: str) -> str:
    """Read-only: what the panel's ledger rows actually look like.

    Every real answer in this project came from printing the server's own
    response instead of guessing at it. This shows which resource holds the
    money rows, the field names on one row, and how the first few rows were
    understood — so a wrong reading is visible rather than silently averaged
    into a total.
    """
    from datetime import datetime, timezone
    out: list[str] = []

    ok, got = panel_find_ledger_sync(cookie_string)
    if not ok:
        out.append(f"журнал операций не найден: {got}")
        res = "operations"
        ops: list[dict] = []
    else:
        res, ops = got
        out.append(f"журнал: {res}, строк на первой странице: {len(ops)}")

    session = _make_panel_requests_session(cookie_string)
    hdrs = _panel_xsrf_headers(session, cookie_string)
    try:
        r = session.get(f"{PANEL_URL}/nova-api/{res}", params={"perPage": "5"},
                        headers=hdrs, timeout=(6, 15), allow_redirects=False)
        body = r.json() or {}
        out.append(f"{res} → {r.status_code}, всего: {body.get('total', '?')}")
        rows = [x for x in (body.get("resources") or []) if isinstance(x, dict)]
        if rows:
            names = [f"{f.get('name') or '?'}[{f.get('attribute') or '?'}"
                     f"·{f.get('component') or '?'}]" for f in _fields_of(rows[0])]
            out.append("поля строки: " + ", ".join(names)[:600])
            out.append("значения 1-й строки: " + ", ".join(
                f"{f.get('name') or f.get('attribute')}={_field_text(f)[:28]}"
                for f in _fields_of(rows[0]))[:600])
    except Exception as e:
        out.append(f"{res}: {str(e)[:80]}")

    for op in ops[:5]:
        import localtime as _lt
        when = _lt.fmt(op.get("ts"), None) or "без даты"
        out.append(f"  · {when} | {op.get('kind')} / {op.get('direction')} | "
                   f"{op.get('amount')} ₽ | {str(op.get('type') or '')[:24]} | "
                   f"{str(op.get('title') or '')[:24]}")

    ok_m, m = panel_shop_metrics_sync(cookie_string)
    if ok_m and isinstance(m, dict):
        out.append("поля магазина: " + ", ".join(
            f"{k}={v['text'][:20]}" for k, v in (m.get("metrics") or {}).items())[:700])
    else:
        out.append(f"магазин: {m}")

    return "\n".join(out)[:3000]
