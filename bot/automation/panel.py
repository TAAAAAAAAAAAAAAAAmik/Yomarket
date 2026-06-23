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
    'input[placeholder*="почту"]',
    'input[placeholder*="электронную"]',
    'input[placeholder*="mail"]',
]

# Code input selectors (после отправки: "Код из письма")
_CODE_SELECTORS = [
    'input[placeholder*="Код из"]',
    'input[placeholder*="код"]',
    'input[name="code"]',
    'input[name="otp"]',
    'input[type="number"]',
    'input[inputmode="numeric"]',
]

# "Получить код" button
_SEND_CODE_SELECTORS = [
    'button:has-text("Получить код")',
    'button[type="submit"]',
    'button:has-text("Отправить")',
    'button:has-text("Получить")',
]

# "Подтвердить" button
_CONFIRM_SELECTORS = [
    'button:has-text("Подтвердить")',
    'button[type="submit"]',
    'button:has-text("Войти")',
    'button:has-text("Продолжить")',
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
        jar = self._session.cookie_jar.filter_cookies(PANEL_URL)
        for name, cookie in jar.items():
            if name.upper() in ("XSRF-TOKEN", "CSRF-TOKEN"):
                return urllib.parse.unquote(cookie.value)
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
        Try to create a product via the panel's internal API.
        Returns (True, product_id_or_url) or (False, error_message).
        """
        if not self._session:
            return False, "Сессия не запущена"

        # Grab CSRF token first
        timeout = aiohttp.ClientTimeout(total=12)
        for csrf_path in ("/sanctum/csrf-cookie", "/csrf-cookie"):
            try:
                async with self._session.get(PANEL_URL + csrf_path, timeout=timeout) as r:
                    if r.status < 400:
                        break
            except Exception:
                pass

        xsrf = self._xsrf()
        extra = {"X-XSRF-TOKEN": xsrf, "X-Requested-With": "XMLHttpRequest"} if xsrf else {"X-Requested-With": "XMLHttpRequest"}

        payload: dict = {
            "title": title,
            "name": title,
            "price": price,
            "description": description,
            "count": quantity,
            "quantity": quantity,
            "amount": quantity,
        }
        if category:
            payload["category"] = category

        # First, try GET /api/products to understand data structure
        try:
            async with self._session.get(PANEL_URL + "/api/products", timeout=timeout) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    logger.info("GET /api/products: %s", text[:500])
                    # Try to POST to same URL with X-HTTP-Method-Override just in case
        except Exception as e:
            logger.debug("GET /api/products: %s", e)

        # Step A: discover product-related API paths from the SPA JS bundle.
        discovered, disc_debug = await self._discover_product_paths()

        # Step B: build the candidate list — discovered paths first, then fallbacks.
        # NOTE: /api/products/<anything> matches the read-only show route → 405.
        # Only try genuinely distinct base paths as fallbacks.
        fallback = [
            "/api/product/store",
            "/api/seller/products",
            "/api/cabinet/products",
            "/api/account/products",
            "/api/v2/products",
            "/api/products-create",
            "/api/create-product",
        ]
        candidates = list(dict.fromkeys(discovered + fallback))

        diag_lines: list[str] = []
        if disc_debug:
            diag_lines.append(disc_debug)

        for path in candidates:
            url = PANEL_URL + path
            try:
                async with self._session.post(url, json=payload, headers=extra, timeout=timeout) as resp:
                    text = await resp.text()
                    short = text[:100].replace("\n", " ")
                    diag_lines.append(f"<code>{path}</code> → {resp.status}: {short}")
                    if resp.status in (200, 201):
                        try:
                            data = _json.loads(text)
                        except Exception:
                            data = {}
                        pid = (
                            data.get("id")
                            or (data.get("data") or {}).get("id")
                            or (data.get("product") or {}).get("id")
                            or ""
                        )
                        return True, str(pid) if pid else "создан"
                    elif resp.status == 401:
                        return False, "❌ Сессия панели истекла — обнови cookies"
                    elif resp.status == 422:
                        # Route exists, validation failed — show what fields it wants
                        try:
                            data = _json.loads(text)
                            errs = data.get("errors") or data.get("message") or text[:300]
                            return False, f"✅ Найден эндпоинт <code>{path}</code>, но нужны поля:\n<code>{errs}</code>"
                        except Exception:
                            return False, f"422 на <code>{path}</code>: {text[:300]}"
            except aiohttp.ClientError as e:
                diag_lines.append(f"<code>{path}</code> → err: {str(e)[:50]}")

        return False, "🔍 Эндпоинты:\n" + "\n".join(diag_lines[:15])

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

            # Capture ALL /api/... paths so we see the full surface
            path_re = re.compile(r'["\'`](/api/[a-zA-Z0-9/_{}.\-]{2,70})["\'`]')
            kws = ("product", "goods", "offer", "item", "lot", "create", "store", "save", "publish", "add")
            all_api: set[str] = set()

            for src in js_files[:8]:
                url = src if src.startswith("http") else PANEL_URL + src
                try:
                    async with self._session.get(url, timeout=timeout) as r:
                        js = await r.text()
                except Exception:
                    continue
                for m in path_re.findall(js):
                    all_api.add(m)

            # Keep product-related, prefer ones without {id} templates
            for p in all_api:
                if any(k in p.lower() for k in kws):
                    discovered.append(p)
            discovered.sort(key=lambda p: ("{" in p or "}" in p, p.count("/")))
            debug.append(f"API всего: {len(all_api)}")
            debug.append(f"Товарные: {discovered[:10]}")
        except Exception as e:
            debug.append(f"Ошибка: {str(e)[:80]}")

        return discovered[:12], " | ".join(debug)

    async def check_session(self) -> bool:
        """Verify cookies are still valid."""
        if not self._session:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._session.get(PANEL_URL + "/", timeout=timeout) as resp:
                final_url = str(resp.url)
                return "/login" not in final_url and "/auth" not in final_url
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
        Returns (True, '') when code input appears, (False, error) otherwise.
        """
        try:
            # Fill email input
            filled = await _fill_first(page, _EMAIL_SELECTORS, email)
            if not filled:
                html = await page.content()
                logger.error("Email input not found. Page snippet: %s", html[:500])
                # Show page content as diagnostic
                return False, f"Поле email не найдено.\nHTML начало: <code>{html[:300]}</code>"

            # Click "Получить код"
            clicked = await _click_first(page, _SEND_CODE_SELECTORS)
            if not clicked:
                html = await page.content()
                return False, f"Кнопка «Получить код» не найдена.\nHTML: <code>{html[:300]}</code>"

            # Wait for code input to appear (SPA re-renders)
            await asyncio.sleep(2)
            try:
                await page.wait_for_selector(
                    'input[placeholder*="Код"], input[placeholder*="код"], '
                    'input[name="code"], input[name="otp"], input[inputmode="numeric"]',
                    timeout=10000,
                )
            except Exception:
                pass

            # Verify code input appeared
            for sel in _CODE_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    logger.info("Code input appeared after submitting email")
                    return True, ""

            # Check for already logged in
            if "/login" not in page.url:
                return True, "__already_logged_in__"

            # Show current page content as diagnostic
            html = await page.content()
            return False, f"Поле для кода не появилось.\nHTML: <code>{html[:400]}</code>"
        except Exception as e:
            logger.error("submit_email error: %s", e)
            return False, str(e)

    async def submit_code(self, page, context, code: str) -> tuple[bool, str]:
        """
        Enter code from email and click "Подтвердить".
        Returns (True, cookie_string) on success, (False, error) otherwise.
        """
        try:
            filled = await _fill_first(page, _CODE_SELECTORS, code)
            if not filled:
                html = await page.content()
                return False, f"Поле для кода не найдено.\nHTML: <code>{html[:300]}</code>"

            await _click_first(page, _CONFIRM_SELECTORS)

            # Wait for SPA to react: either navigate away from /login or show an error.
            # Don't use wait_for_load_state — SPA doesn't reload, it redirects via JS router.
            try:
                await page.wait_for_function(
                    "() => !window.location.pathname.includes('/login')",
                    timeout=12000,
                )
            except Exception:
                # Might still be on login page — check for error message
                pass

            await asyncio.sleep(1)
            cur_url = page.url

            if "/login" not in cur_url and "/auth" not in cur_url and "/signin" not in cur_url:
                # Successfully left login page — extract cookies
                cookies = await context.cookies()
                cookie_string = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies
                    if c.get("domain", "").endswith("yoomarket.net")
                )
                if not cookie_string:
                    cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                logger.info("Login successful, %d cookies, url=%s", len(cookies), cur_url)
                return True, cookie_string

            # Still on login page — check for error text
            err_el = await page.query_selector(
                '.error, .alert-danger, [class*="error"], [class*="Error"], '
                '.v-alert, [role="alert"]'
            )
            if err_el:
                err_text = (await err_el.inner_text()).strip()[:200]
                return False, f"Ошибка: {err_text}"

            html = await page.content()
            return False, f"Код не принят, осталось на /login (html={len(html)}б)"

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
