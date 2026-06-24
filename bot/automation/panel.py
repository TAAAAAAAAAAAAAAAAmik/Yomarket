"""YooMarket panel automation: email OTP login."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

PANEL_URL = "https://panel.yoomarket.net"

# Email input selectors (страница входа: "Введите электронную почту")
_EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[placeholder*="почт" i]',
    'input[placeholder*="mail" i]',
    'input[placeholder*="email" i]',
]

# Code input selectors (после отправки кода)
_CODE_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[placeholder*="код" i]',
    'input[placeholder*="code" i]',
    'input[name="code"]',
    'input[name="otp"]',
    'input[name="token"]',
    'input[maxlength="6"]',
    'input[maxlength="4"]',
    'input[type="number"]',
    'input[inputmode="numeric"]',
]

# "Получить код" button
_SEND_CODE_SELECTORS = [
    'button:has-text("Получить код")',
    'button:has-text("Отправить код")',
    'button[type="submit"]',
    'button:has-text("Отправить")',
    'button:has-text("Получить")',
    'button:has-text("Войти")',
]

# "Подтвердить" button
_CONFIRM_SELECTORS = [
    'button:has-text("Подтвердить")',
    'button:has-text("Войти")',
    'button:has-text("Продолжить")',
    'button:has-text("Verify")',
    'button:has-text("Submit")',
    'button[type="submit"]',
]


def _parse_cookies(cookie_string: str) -> list[dict]:
    """Parse 'key=value; key2=value2' string into Playwright cookie list."""
    cookies = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name:
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": "panel.yoomarket.net",
                    "path": "/",
                })
    return cookies


async def _fill_first(page, selectors: list[str], value: str) -> bool:
    """Try selectors in order, fill the first one found. Return True if filled."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors: list[str]) -> bool:
    """Try selectors in order, click the first one found. Return True if clicked."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                return True
        except Exception:
            continue
    return False


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

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False)
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
                "Origin": PANEL_URL,
                "Referer": PANEL_URL + "/login",
            },
        )

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

    async def send_code(self, email: str) -> tuple[bool, str]:
        """
        POST email to trigger OTP code sent to inbox.
        Returns (True, '') on success, (False, error) on failure.
        """
        if not self._session:
            return False, "Сессия не запущена"

        self._email = email.strip()
        timeout = aiohttp.ClientTimeout(total=12)

        # Step 1: Laravel Sanctum CSRF handshake
        for csrf_path in ("/sanctum/csrf-cookie", "/api/csrf-cookie", "/csrf-cookie"):
            try:
                async with self._session.get(
                    PANEL_URL + csrf_path, timeout=timeout
                ) as resp:
                    if resp.status < 400:
                        break
            except Exception:
                continue

        # Step 2: GET /login to pick up any _token in HTML
        try:
            async with self._session.get(PANEL_URL + "/login", timeout=timeout) as resp:
                html = await resp.text()
                for pattern in [
                    r'"csrfToken"\s*:\s*"([^"]+)"',
                    r'name=["\']_token["\']\s+value=["\']([^"\']+)["\']',
                    r'"_token"\s*:\s*"([^"]+)"',
                    r'<meta[^>]+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
                ]:
                    m = re.search(pattern, html)
                    if m:
                        self._csrf = m.group(1)
                        break
        except Exception as e:
            logger.warning("GET /login failed: %s", e)

        xsrf = self._xsrf_token()
        extra_headers = {"X-XSRF-TOKEN": xsrf} if xsrf else {}

        # Step 3: Discover real API paths from the JS bundle
        discovered, disc_debug = await self._discover_api_paths()

        fallback_paths = [
            # web.php routes (no /api/ prefix) — most likely for Laravel SPA
            "/auth/send-code",
            "/auth/email",
            "/auth/login",
            "/auth/code",
            "/send-code",
            "/login/send-code",
            "/login/code",
            "/login/email",
            "/sign-in",
            "/auth/sign-in",
            # api.php routes
            "/api/auth/send-code",
            "/api/auth/email",
            "/api/send-code",
            "/api/login",
            "/api/auth/login",
            "/api/v1/auth/send-code",
            "/api/v1/auth/email",
            "/api/user/send-code",
        ]
        all_paths = list(dict.fromkeys(discovered + fallback_paths))

        diag_lines = []
        for path in all_paths:
            try:
                async with self._session.post(
                    PANEL_URL + path,
                    json={"email": self._email},
                    headers=extra_headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    short = text[:100].replace("\n", " ")
                    diag_lines.append(f"<code>{path}</code> → <b>{resp.status}</b>: {short}")
                    logger.info("POST %s → %s: %s", path, resp.status, text[:300])
                    if resp.status in (200, 201):
                        return True, ""
                    if resp.status == 422:
                        return False, f"422 на <code>{path}</code>:\n<code>{text[:400]}</code>"
            except Exception as e:
                diag_lines.append(f"<code>{path}</code> → {str(e)[:60]}")

        diag = "\n".join(diag_lines)
        return False, f"🔍 <b>Диагностика JS:</b>\n{disc_debug}\n\n<b>Ответы сервера:</b>\n\n{diag}"

    async def _discover_api_paths(self) -> tuple[list[str], str]:
        """Fetch the JS bundle and extract API paths. Returns (paths, debug_info)."""
        discovered = []
        debug = []
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with self._session.get(PANEL_URL + "/login", timeout=timeout) as resp:
                html = await resp.text()
                debug.append(f"HTML {len(html)}б")

            script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
            js_files = [s for s in script_srcs if ".js" in s]
            debug.append(f"Скриптов: {len(js_files)} → {[s[-40:] for s in js_files[:3]]}")

            for src in js_files[:3]:
                url = src if src.startswith("http") else PANEL_URL + src
                try:
                    async with self._session.get(url, timeout=timeout) as resp:
                        js = await resp.text()
                    debug.append(f"JS {url[-30:]}: {len(js)}б")
                    patterns = [
                        r'["\']((?:/api)?/[a-z0-9/_-]{3,60})["\']',
                        r'post\(["\`](/?[a-z0-9/_-]{3,60})["\`]',
                        r'axios\.[a-z]+\(["\`](/?[^"\'`\s]{3,60})["\`]',
                        r'fetch\(["\`](/?[^"\'`\s?]{3,60})["\`]',
                    ]
                    kws = ("auth", "login", "code", "send", "verify", "confirm", "email")
                    for pat in patterns:
                        for match in re.findall(pat, js, re.I):
                            if any(kw in match.lower() for kw in kws):
                                if not match.startswith("http") and match not in discovered:
                                    discovered.append(match)
                    if discovered:
                        break
                except Exception as e:
                    debug.append(f"JS ошибка: {e}")
        except Exception as e:
            debug.append(f"Ошибка: {e}")

        debug.append(f"Найдено путей: {discovered[:5]}")
        return discovered, " | ".join(debug)

    async def verify_code(self, code: str) -> tuple[bool, str]:
        """
        Submit the code from email. Returns (True, cookie_string) or (False, error).
        """
        if not self._session:
            return False, "Сессия не запущена"

        timeout = aiohttp.ClientTimeout(total=12)
        xsrf = self._xsrf_token()
        extra_headers = {"X-XSRF-TOKEN": xsrf} if xsrf else {}

        json_paths = [
            "/api/auth/verify",
            "/api/auth/confirm",
            "/api/verify",
            "/api/auth/check-code",
            "/api/login/verify",
            "/api/v1/auth/verify",
            "/api/v1/auth/confirm",
        ]
        for path in json_paths:
            try:
                async with self._session.post(
                    PANEL_URL + path,
                    json={"email": self._email, "code": code},
                    headers=extra_headers,
                    timeout=timeout,
                    allow_redirects=True,
                ) as resp:
                    text = await resp.text()
                    logger.info("verify POST %s → %s: %s", path, resp.status, text[:200])
                    if resp.status in (200, 201):
                        cookies = _extract_cookies(self._session, PANEL_URL)
                        if cookies:
                            return True, cookies
                        # Token in JSON body
                        try:
                            data = _json.loads(text)
                            for key in ("token", "access_token", "api_token"):
                                if data.get(key):
                                    return True, f"{key}={data[key]}"
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("verify_code %s: %s", path, e)

        return False, "❌ Неверный код или срок действия истёк."


def _extract_cookies(session: aiohttp.ClientSession, url: str) -> str:
    """Pull all cookies matching the given URL from an aiohttp session."""
    jar = session.cookie_jar.filter_cookies(url)
    parts = [f"{name}={cookie.value}" for name, cookie in jar.items()]
    return "; ".join(parts)


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

        timeout = aiohttp.ClientTimeout(total=15)

        # CSRF handshake — GET /sanctum/csrf-cookie sets XSRF-TOKEN in cookie jar
        for csrf_path in ("/sanctum/csrf-cookie", "/csrf-cookie"):
            try:
                async with self._session.get(PANEL_URL + csrf_path, timeout=timeout) as r:
                    if r.status < 400:
                        break
            except Exception:
                pass

        # Warm up session: GET main page so Laravel session cookie is renewed
        try:
            async with self._session.get(
                PANEL_URL + "/goods", timeout=timeout, allow_redirects=True,
            ) as r:
                pass
        except Exception:
            pass

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
        timeout = aiohttp.ClientTimeout(total=20)
        xsrf = self._xsrf()
        hdrs = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        if xsrf:
            hdrs["X-XSRF-TOKEN"] = xsrf

        debug: list[str] = []
        debug.append(f"XSRF: {'✓' if xsrf else '✗ (нет токена)'}")

        # 1. Enumerate Nova resources via navigation endpoint
        resources: list[str] = []
        for nav_path in ("/nova-api/navigation", "/nova-api/resources"):
            try:
                async with self._session.get(
                    PANEL_URL + nav_path, headers=hdrs, timeout=timeout, allow_redirects=False,
                ) as resp:
                    txt = await resp.text()
                    short = txt[:150].replace("\n", " ")
                    if resp.status in (301, 302):
                        loc = resp.headers.get("Location", "?")
                        debug.append(f"{nav_path}: {resp.status} → {loc}")
                    else:
                        debug.append(f"{nav_path}: {resp.status} → {short}")
                    if resp.status == 200:
                        found = re.findall(r'"uriKey"\s*:\s*"([^"]+)"', txt)
                        resources.extend(r for r in found if r not in resources)
                    elif resp.status in (401, 403):
                        return False, (
                            "⚠️ <b>Сессия в панели истекла.</b>\n\n"
                            "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова через email."
                        )
            except Exception as e:
                debug.append(f"{nav_path}: ошибка {str(e)[:50]}")

        debug.append(f"Ресурсы из navigation: {resources or '(не найдено)'}")

        # 2. Scan main page HTML for uriKey hints
        try:
            async with self._session.get(PANEL_URL + "/", headers=hdrs, timeout=timeout) as resp:
                html = await resp.text()
            for r in re.findall(r'"uriKey"\s*:\s*"([^"]+)"', html):
                if r not in resources:
                    resources.append(r)
            for r in re.findall(r'/resources/([a-z0-9_\-]+)', html, re.I):
                if r not in resources:
                    resources.append(r)
        except Exception as e:
            debug.append(f"html scan: {str(e)[:40]}")

        # 3. Hardcoded fallbacks — goods first (SPA route is /goods → uriKey='goods')
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

        # 4. For each candidate: check creation-fields, then POST with JSON
        for res in resources[:15]:
            cf_url = f"{PANEL_URL}/nova-api/{res}/creation-fields"
            try:
                async with self._session.get(
                    cf_url, headers=hdrs, timeout=timeout, allow_redirects=False,
                ) as resp:
                    cf_status = resp.status
                    cf_text = await resp.text()
            except Exception as e:
                debug.append(f"{res}: connection error")
                continue

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
            try:
                async with self._session.post(
                    store_url, json=payload, headers=hdrs, timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status in (200, 201):
                        try:
                            data = _json.loads(text)
                        except Exception:
                            data = {}
                        rid = (data.get("resource") or {}).get("id") or data.get("id") or ""
                        if isinstance(rid, dict):
                            rid = rid.get("value", "")
                        return True, str(rid) if rid else "создан"
                    elif resp.status == 422:
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
                    elif resp.status == 401:
                        return False, (
                            "⚠️ <b>Сессия в панели истекла.</b>\n\n"
                            "Зайдите в <b>Настройки → Панель продавца</b> и войдите снова."
                        )
                    else:
                        debug.append(f"POST {res}: {resp.status} → {text[:80]}")
            except Exception as e:
                debug.append(f"POST {res}: {str(e)[:50]}")

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


class YooMarketPanel:
    """Headless Chromium automation for the YooMarket seller panel."""

    def __init__(self, cookie_string: str = "") -> None:
        self.cookie_string = cookie_string
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.warning("Error closing browser: %s", e)
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("Error stopping playwright: %s", e)

    async def _new_authenticated_page(self):
        """Create a page with session cookies pre-loaded."""
        if not self._browser:
            await self.start()
        context = await self._browser.new_context()
        if self.cookie_string:
            cookies = _parse_cookies(self.cookie_string)
            if cookies:
                await context.add_cookies(cookies)
        return await context.new_page(), context

    # ------------------------------------------------------------------
    # Email OTP login flow (interactive — called step by step from handlers)
    # ------------------------------------------------------------------

    async def open_login_page(self) -> tuple[object, object]:
        """Open a fresh browser context at the login page. Returns (page, context)."""
        if not self._browser:
            await self.start()
        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await page.goto(PANEL_URL + "/login", timeout=20000, wait_until="domcontentloaded")
        except Exception:
            pass
        # Let the SPA render the form
        await asyncio.sleep(3)
        return page, context

    async def submit_email(self, page, email: str) -> tuple[bool, str]:
        """
        Fill the email field and click "Получить код".
        Returns (True, '') on success, (False, error) on failure.
        """
        try:
            # Fill email — first try standard selectors, then JS fallback
            filled = await _fill_first(page, _EMAIL_SELECTORS, email)
            if not filled:
                try:
                    ok = await page.evaluate(
                        """(email) => {
                            const inputs = [...document.querySelectorAll('input')];
                            for (const inp of inputs) {
                                const t = inp.type.toLowerCase();
                                const p = (inp.placeholder || '').toLowerCase();
                                if (t === 'email' || p.includes('почт') || p.includes('mail')) {
                                    inp.focus();
                                    inp.value = email;
                                    inp.dispatchEvent(new Event('input', {bubbles:true}));
                                    inp.dispatchEvent(new Event('change', {bubbles:true}));
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        email,
                    )
                    filled = bool(ok)
                except Exception:
                    pass

            if not filled:
                html = await page.content()
                return False, f"Поле email не найдено.\nHTML: <code>{html[:300]}</code>"

            # Small pause so Vue can register the value before we click
            await asyncio.sleep(0.4)

            # Click button or press Enter
            clicked = await _click_first(page, _SEND_CODE_SELECTORS)
            if not clicked:
                try:
                    await page.keyboard.press("Enter")
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                html = await page.content()
                return False, f"Кнопка «Получить код» не найдена.\nHTML: <code>{html[:300]}</code>"

            # Wait for the browser to finish the form-submission network request,
            # then give the SPA a moment to re-render the code input.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1)

            # If already navigated away from login — logged in without code
            if "/login" not in page.url:
                return True, "__already_logged_in__"

            logger.info("Email submitted OK, url=%s", page.url)
            return True, ""

        except Exception as e:
            logger.error("submit_email error: %s", e)
            return False, str(e)

    async def submit_code(self, page, context, code: str) -> tuple[bool, str]:
        """
        Enter OTP code and submit. Handles both single-input and multi-digit inputs.
        Returns (True, cookie_string) on success, (False, error) otherwise.
        """
        try:
            code = code.strip()
            filled = False

            # Strategy 1: standard selectors (single input field)
            filled = await _fill_first(page, _CODE_SELECTORS, code)

            if not filled:
                # Strategy 2: multi-digit OTP (separate <input maxlength="1"> per digit)
                try:
                    digit_inputs = await page.query_selector_all('input[maxlength="1"]')
                    if len(digit_inputs) >= 4:
                        for i, el in enumerate(digit_inputs):
                            if i < len(code):
                                await el.click()
                                await el.type(code[i])
                        filled = True
                except Exception:
                    pass

            if not filled:
                # Strategy 3: JavaScript — fill any visible code-like input,
                # dispatching Vue/React events so the reactive form updates
                try:
                    ok = await page.evaluate(
                        """(code) => {
                            const inputs = [...document.querySelectorAll(
                                'input:not([type="email"]):not([type="hidden"])')];
                            const visible = inputs.filter(i => i.offsetParent !== null);
                            if (!visible.length) return false;
                            // Prefer inputs that look like OTP
                            let target = visible.find(i => {
                                const p = (i.placeholder || '').toLowerCase();
                                const n = (i.name || '').toLowerCase();
                                return p.includes('код') || p.includes('code') ||
                                       n === 'code' || n === 'otp' || n === 'token';
                            }) || visible[0];
                            target.focus();
                            target.value = code;
                            target.dispatchEvent(new Event('input', {bubbles:true}));
                            target.dispatchEvent(new Event('change', {bubbles:true}));
                            return true;
                        }""",
                        code,
                    )
                    if ok:
                        filled = True
                except Exception:
                    pass

            if not filled:
                # Strategy 4: keyboard type (last resort)
                try:
                    await page.keyboard.type(code)
                    filled = True
                except Exception:
                    pass

            if not filled:
                # Collect diagnostics for the user
                try:
                    inputs_info = await page.evaluate(
                        "() => [...document.querySelectorAll('input')].map(i=>"
                        "({type:i.type,name:i.name,placeholder:i.placeholder,maxlength:i.maxLength}))"
                    )
                except Exception:
                    inputs_info = []
                html = await page.content()
                return False, (
                    f"Поле для кода не найдено.\n"
                    f"Inputs: <code>{str(inputs_info)[:300]}</code>\n"
                    f"HTML: <code>{html[:200]}</code>"
                )

            await asyncio.sleep(0.5)

            # Submit: click button or press Enter
            submitted = await _click_first(page, _CONFIRM_SELECTORS)
            if not submitted:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            # Wait for SPA router to navigate away from /login
            try:
                await page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=12000,
                )
            except Exception:
                pass

            await asyncio.sleep(1.5)
            cur_url = page.url

            if "/login" not in cur_url and "/auth" not in cur_url and "/signin" not in cur_url:
                cookies = await context.cookies()
                cookie_string = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                    if c.get("domain", "").endswith("yoomarket.net")
                )
                if not cookie_string:
                    cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                logger.info("Login OK: %d cookies, url=%s", len(cookies), cur_url)
                return True, cookie_string

            # Still on login — extract error message via JS
            try:
                err_text = await page.evaluate(
                    """() => {
                        const sels = ['.error','.alert','[class*="error" i]',
                                      '.v-alert','[role="alert"]','.notification',
                                      'p[class*="red"]','span[class*="red"]'];
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (el && el.textContent.trim())
                                return el.textContent.trim().slice(0,200);
                        }
                        return null;
                    }"""
                )
            except Exception:
                err_text = None

            if err_text:
                return False, f"❌ {err_text}"

            html = await page.content()
            return False, f"Код не принят (html={len(html)}б, url={cur_url})"

        except Exception as e:
            logger.error("submit_code error: %s", e)
            return False, str(e)

    # ------------------------------------------------------------------
    # Session check
    # ------------------------------------------------------------------

    async def check_session(self) -> bool:
        """Returns True if the session cookies are still valid."""
        page, context = await self._new_authenticated_page()
        try:
            await page.goto(PANEL_URL + "/", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            url = page.url
            if "/login" in url or "/auth" in url or "/signin" in url:
                return False
            login_input = await page.query_selector('input[type="email"], input[name="login"]')
            return login_input is None
        except Exception as e:
            logger.warning("check_session error: %s", e)
            return False
        finally:
            await page.close()
            await context.close()

    # ------------------------------------------------------------------
    # Automation actions
    # ------------------------------------------------------------------

    async def bump_all_ads(self) -> tuple[int, str]:
        page, context = await self._new_authenticated_page()
        try:
            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            if "/login" in page.url or "/auth" in page.url:
                return 0, "❌ Сессия истекла — войди заново через бота"

            bump_buttons = []
            for selector in ['button:has-text("Поднять")', 'a:has-text("Поднять")',
                             '[data-action="bump"]', '.bump-btn']:
                try:
                    buttons = await page.query_selector_all(selector)
                    if buttons:
                        bump_buttons = buttons
                        break
                except Exception:
                    continue

            if not bump_buttons:
                return 0, "ℹ️ Кнопки поднятия не найдены"

            count = 0
            for btn in bump_buttons:
                try:
                    await btn.click()
                    count += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning("Bump click error: %s", e)

            return count, (f"✅ Поднято: {count}" if count else "⚠️ Не удалось поднять")
        except Exception as e:
            logger.error("bump_all_ads error: %s", e)
            return 0, f"❌ Ошибка: {e}"
        finally:
            await page.close()
            await context.close()

    async def restore_sold_ads(self) -> tuple[int, str]:
        page, context = await self._new_authenticated_page()
        try:
            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            if "/login" in page.url or "/auth" in page.url:
                return 0, "❌ Сессия истекла — войди заново через бота"

            restore_buttons = []
            for selector in ['button:has-text("Восстановить")', 'button:has-text("Активировать")',
                             '[data-action="restore"]', 'a:has-text("Восстановить")']:
                try:
                    buttons = await page.query_selector_all(selector)
                    if buttons:
                        restore_buttons = buttons
                        break
                except Exception:
                    continue

            if not restore_buttons:
                return 0, "ℹ️ Нет товаров для восстановления"

            count = 0
            for btn in restore_buttons:
                try:
                    await btn.click()
                    count += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning("Restore click error: %s", e)

            return count, (f"✅ Восстановлено: {count}" if count else "⚠️ Не удалось восстановить")
        except Exception as e:
            logger.error("restore_sold_ads error: %s", e)
            return 0, f"❌ Ошибка: {e}"
        finally:
            await page.close()
            await context.close()

    async def withdraw_balance(self, min_amount: int) -> tuple[bool, str]:
        page, context = await self._new_authenticated_page()
        try:
            current_balance = 0.0
            balance_found = False

            for path in ["/finance", "/wallet", "/balance"]:
                try:
                    await page.goto(PANEL_URL + path, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    if "/login" in page.url or "/auth" in page.url:
                        return False, "❌ Сессия истекла — войди заново через бота"
                    page_text = await page.inner_text("body")
                    for pattern in [r"([\d\s]+[,.]?\d*)\s*₽", r"([\d\s]+[,.]?\d*)\s*RUB",
                                    r"Баланс[:\s]+([\d\s]+[,.]?\d*)"]:
                        matches = re.findall(pattern, page_text)
                        if matches:
                            raw = matches[0].replace(" ", "").replace(",", ".").strip()
                            try:
                                current_balance = float(raw)
                                balance_found = True
                                break
                            except ValueError:
                                continue
                    if balance_found:
                        break
                except Exception as e:
                    logger.warning("Balance load error on %s: %s", path, e)

            if not balance_found:
                return False, "❌ Не удалось найти баланс"
            if current_balance < min_amount:
                return False, f"ℹ️ Баланс {current_balance:.0f} ₽ ниже порога {min_amount} ₽"

            for selector in ['button:has-text("Вывести")', 'button:has-text("Вывод")',
                             'a:has-text("Вывести")', '[data-action="withdraw"]']:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    continue

            try:
                amount_input = await page.query_selector('input[type="number"], input[name="amount"]')
                if amount_input:
                    await amount_input.fill(str(int(current_balance)))
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            for selector in ['button[type="submit"]', 'button:has-text("Подтвердить")', 'button:has-text("OK")']:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.5)
                        return True, f"✅ Вывод {current_balance:.0f} ₽ выполнен"
                except Exception:
                    continue

            return True, f"⚠️ Запрос вывода отправлен ({current_balance:.0f} ₽)"
        except Exception as e:
            logger.error("withdraw_balance error: %s", e)
            return False, f"❌ Ошибка: {e}"
        finally:
            await page.close()
            await context.close()

    async def create_product_browser(
        self,
        title: str,
        price: int,
        description: str,
        quantity: int = 1,
    ) -> tuple[bool, str]:
        """Create a product by navigating the panel UI in Playwright."""
        page, context = await self._new_authenticated_page()
        captured: list[str] = []

        async def on_request(request):
            if request.method in ("POST", "PUT", "PATCH"):
                captured.append(f"{request.method} {request.url}")

        page.on("request", on_request)

        try:
            # 1. Go to goods list page
            await page.goto(PANEL_URL + "/goods", timeout=20000, wait_until="domcontentloaded")

            # Let JS execute and make auth API calls
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            await asyncio.sleep(1)
            cur_url = page.url

            if "/login" in cur_url or "/auth" in cur_url:
                return False, "❌ Сессия панели истекла — обнови cookies в разделе «Панель продавца»"

            # Check if something rendered
            html = await page.content()
            try:
                await page.wait_for_selector("button, a[href], input, [role='button']", timeout=8000)
            except Exception:
                return False, (
                    f"Страница /goods не отрисовала элементы\n"
                    f"URL: {cur_url} | html: {len(html)}б\n"
                    f"Скорее всего cookies истекли — войдите заново в «Панель продавца»"
                )

            # 2. Find and click the "create" / "add" button
            create_clicked = False
            for sel in [
                'button:has-text("Создать")', 'button:has-text("Добавить")',
                'a:has-text("Создать")', 'a:has-text("Добавить")',
                'button:has-text("+ ")', 'a[href*="create"]', 'a[href*="add"]',
                '[data-action="create"]', '.create-btn', '.add-btn',
                'button:has-text("Новый")', 'a:has-text("Новый")',
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    create_clicked = True
                    logger.info("Clicked create button with selector: %s", sel)
                    break

            if not create_clicked:
                # Dump all buttons for diagnostics
                buttons = await page.query_selector_all("button, a")
                texts = []
                for b in buttons[:20]:
                    try:
                        t = (await b.inner_text()).strip()[:30]
                        if t:
                            texts.append(t)
                    except Exception:
                        pass
                return False, f"Кнопка создания не найдена на /goods\nЭлементы: {', '.join(repr(t) for t in texts[:15])}"

            # 3. Wait for form to appear (modal or new page)
            await asyncio.sleep(2)
            try:
                await page.wait_for_selector("input, textarea", timeout=12000)
            except Exception:
                html = await page.content()
                buttons = await page.query_selector_all("button")
                btns = []
                for b in buttons[:10]:
                    try:
                        btns.append((await b.inner_text()).strip()[:30])
                    except Exception:
                        pass
                return False, (
                    f"Форма не появилась после клика (url={page.url}, html={len(html)}б)\n"
                    f"Кнопки: {', '.join(repr(t) for t in btns)}"
                )

            # 4. Fill fields
            for sel in ['input[name="title"]', 'input[name="name"]',
                        'input[placeholder*="азвани"]', 'input[placeholder*="аименовани"]']:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await el.fill(title)
                    break
            else:
                inputs = await page.query_selector_all('input[type="text"], input:not([type])')
                if inputs:
                    await inputs[0].fill(title)

            for sel in ['input[name="price"]', 'input[name="cost"]',
                        'input[placeholder*="цен"]', 'input[type="number"]']:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(str(price))
                    break

            for sel in ['textarea[name="description"]', 'textarea', 'div[contenteditable="true"]']:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(description)
                    break

            if quantity != 1:
                for sel in ['input[name="count"]', 'input[name="quantity"]', 'input[name="amount"]']:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(str(quantity))
                        break

            # 5. Submit
            submitted = False
            for sel in ['button[type="submit"]', 'button:has-text("Создать")',
                        'button:has-text("Добавить")', 'button:has-text("Сохранить")',
                        'button:has-text("Опубликовать")', 'input[type="submit"]']:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    submitted = True
                    break

            if not submitted:
                buttons = await page.query_selector_all("button")
                btns = []
                for b in buttons[:15]:
                    try:
                        t = (await b.inner_text()).strip()[:30]
                        if t:
                            btns.append(t)
                    except Exception:
                        pass
                return False, f"Кнопка отправки не найдена. Кнопки в форме: {', '.join(repr(t) for t in btns)}"

            await asyncio.sleep(4)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

            logger.info("Captured requests: %s", captured)

            # 6. Check success / error
            err_el = await page.query_selector('.error, .alert-danger, [class*="error"]')
            if err_el:
                err_text = (await err_el.inner_text()).strip()[:200]
                return False, f"Ошибка на странице: {err_text}"

            reqs = "; ".join(r.split("/")[-1] for r in captured[-5:]) or "нет"
            return True, f"создан (запросы: {reqs})"

        except Exception as e:
            logger.error("create_product_browser error: %s", e)
            return False, str(e)
        finally:
            await page.close()
            await context.close()
