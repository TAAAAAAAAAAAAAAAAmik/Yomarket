"""YooMarket panel automation: HTTP-based SMS login + Playwright fallback."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

PANEL_URL = "https://panel.yoomarket.net"

# Phone/email input selectors to try in order
_PHONE_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="login"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[placeholder*="телефон"]',
    'input[placeholder*="phone"]',
    'input[placeholder*="mail"]',
]

# SMS code input selectors
_CODE_SELECTORS = [
    'input[name="code"]',
    'input[name="sms"]',
    'input[name="otp"]',
    'input[type="number"][maxlength]',
    'input[placeholder*="код"]',
    'input[placeholder*="code"]',
    'input[inputmode="numeric"]',
]

# Submit button selectors
_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Войти")',
    'button:has-text("Получить")',
    'button:has-text("Далее")',
    'button:has-text("Отправить")',
    'button:has-text("Подтвердить")',
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
    SMS login via plain HTTP — no browser, works on mobile.

    Flow:
      1. send_sms(phone)  → bot receives SMS code
      2. verify_code(code) → returns (True, cookie_string) or (False, error)
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._csrf: str = ""
        self._phone: str = ""

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
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
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

    async def send_sms(self, phone_or_email: str) -> tuple[bool, str]:
        """POST phone/email to trigger SMS. Returns (True, '') or (False, error)."""
        if not self._session:
            return False, "Сессия не запущена"

        self._phone = phone_or_email

        # Step 1: Load login page → grab CSRF token + detect site type
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with self._session.get(PANEL_URL + "/login", timeout=timeout) as resp:
                html = await resp.text()
                for pattern in [
                    r'"csrfToken"\s*:\s*"([^"]+)"',
                    r'name=["\']_token["\']\s+(?:content|value)=["\']([^"\']+)["\']',
                    r'content=["\']([^"\']+)["\']\s+name=["\']csrf-token["\']',
                    r'"_token"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, html)
                    if m:
                        self._csrf = m.group(1)
                        break
        except Exception as e:
            return False, f"Не удалось загрузить страницу входа: {e}"

        # Step 2: Try multiple endpoint patterns
        attempts: list[tuple[str, str, dict]] = []

        # JSON REST API (common for Vue/React SPAs)
        for path in ("/api/auth/phone", "/api/auth/send-code", "/api/login",
                     "/api/auth/login", "/api/v1/auth/phone", "/api/user/login"):
            attempts.append((PANEL_URL + path, "json", {
                "phone": phone_or_email,
                "email": phone_or_email,
                "login": phone_or_email,
            }))

        # Form POST (Laravel/PHP style)
        if self._csrf:
            attempts.append((PANEL_URL + "/login", "form", {
                "_token": self._csrf,
                "login": phone_or_email,
                "phone": phone_or_email,
                "email": phone_or_email,
            }))

        timeout = aiohttp.ClientTimeout(total=10)
        for url, kind, payload in attempts:
            try:
                if kind == "json":
                    ctx = self._session.post(url, json=payload, timeout=timeout, allow_redirects=False)
                else:
                    ctx = self._session.post(url, data=payload, timeout=timeout, allow_redirects=False)

                async with ctx as resp:
                    text = await resp.text()
                    status = resp.status

                    if status in (200, 201):
                        # Look for success signals in response body
                        ok_patterns = [
                            r'"success"\s*:\s*true',
                            r'"status"\s*:\s*"ok"',
                            r'"sent"\s*:\s*true',
                            r'"code"', r'"sms"', r'otp',
                            r'код', r'отправ', r'смс',
                        ]
                        if any(re.search(p, text, re.I) for p in ok_patterns):
                            logger.info("SMS sent via %s", url)
                            return True, ""

                    elif status == 302:
                        loc = resp.headers.get("Location", "")
                        if any(kw in loc for kw in ("code", "sms", "verify", "otp")):
                            logger.info("SMS sent (redirect) via %s → %s", url, loc)
                            return True, ""

            except Exception as e:
                logger.debug("Attempt %s failed: %s", url, e)
                continue

        return False, (
            "❌ Не удалось автоматически запросить SMS.\n\n"
            "Попробуй войти через метод <b>«Вставить cookies»</b>:\n"
            "1. С компьютера зайди на <b>panel.yoomarket.net</b>\n"
            "2. Войди через SMS вручную\n"
            "3. F12 → Console → введи <code>document.cookie</code>\n"
            "4. Скопируй и отправь боту"
        )

    async def verify_code(self, code: str) -> tuple[bool, str]:
        """Submit SMS code. Returns (True, cookie_string) or (False, error)."""
        if not self._session:
            return False, "Сессия не запущена"

        attempts: list[tuple[str, str, dict]] = []

        # JSON REST API
        for path in ("/api/auth/verify", "/api/auth/confirm", "/api/login/verify",
                     "/api/v1/auth/verify", "/api/auth/check-code"):
            attempts.append((PANEL_URL + path, "json", {
                "phone": self._phone,
                "email": self._phone,
                "code": code,
                "sms_code": code,
                "otp": code,
            }))

        # Form POST
        if self._csrf:
            attempts.append((PANEL_URL + "/login", "form", {
                "_token": self._csrf,
                "code": code,
                "sms_code": code,
                "phone": self._phone,
            }))

        timeout = aiohttp.ClientTimeout(total=12)
        for url, kind, payload in attempts:
            try:
                if kind == "json":
                    ctx = self._session.post(url, json=payload, timeout=timeout, allow_redirects=True)
                else:
                    ctx = self._session.post(url, data=payload, timeout=timeout, allow_redirects=True)

                async with ctx as resp:
                    final_url = str(resp.url)
                    text = await resp.text()

                    # Success: we left the login/auth page
                    not_on_login = all(
                        kw not in final_url
                        for kw in ("/login", "/auth", "/signin", "/sign-in")
                    )
                    if resp.status in (200, 201) and not_on_login:
                        cookie_string = _extract_cookies(self._session, PANEL_URL)
                        if cookie_string:
                            logger.info("Login verified via %s", url)
                            return True, cookie_string

                    # Check JSON for token
                    if resp.status in (200, 201) and "{" in text:
                        try:
                            data = _json.loads(text)
                            for key in ("token", "access_token", "api_token", "session"):
                                if data.get(key):
                                    cookie_string = _extract_cookies(self._session, PANEL_URL)
                                    return True, cookie_string or f"{key}={data[key]}"
                        except Exception:
                            pass

            except Exception as e:
                logger.debug("Verify attempt %s failed: %s", url, e)
                continue

        return False, "Неверный код или код истёк. Попробуй запросить SMS снова."


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

        timeout = aiohttp.ClientTimeout(total=15)

        # Try common internal API endpoints
        endpoints = [
            "/api/goods",
            "/api/goods/create",
            "/api/products",
            "/api/products/create",
            "/api/v1/goods",
            "/api/v1/products",
            "/api/listings",
            "/api/ads",
        ]

        for path in endpoints:
            url = PANEL_URL + path
            try:
                async with self._session.post(url, json=payload, timeout=timeout) as resp:
                    text = await resp.text()
                    if resp.status in (200, 201):
                        try:
                            data = _json.loads(text)
                        except Exception:
                            data = {}
                        # Extract created product ID
                        pid = (
                            data.get("id")
                            or (data.get("data") or {}).get("id")
                            or (data.get("product") or {}).get("id")
                            or (data.get("good") or {}).get("id")
                            or ""
                        )
                        logger.info("Product created via %s, id=%s", path, pid)
                        return True, str(pid) if pid else "создан"
                    elif resp.status == 401:
                        return False, "❌ Сессия панели истекла — обнови cookies"
                    elif resp.status == 422:
                        # Validation error means endpoint exists but form is wrong
                        try:
                            data = _json.loads(text)
                            errs = data.get("errors") or data.get("message") or text[:200]
                            return False, f"❌ Ошибка валидации: {errs}"
                        except Exception:
                            return False, f"❌ Ошибка валидации (422): {text[:200]}"
            except aiohttp.ClientError as e:
                logger.debug("Panel endpoint %s error: %s", path, e)
                continue

        return False, (
            "❌ Не удалось найти эндпоинт создания товара в панели.\n\n"
            "Попробуй создать товар вручную на <b>panel.yoomarket.net</b>"
        )

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
    # SMS login flow (interactive — called step by step from handlers)
    # ------------------------------------------------------------------

    async def open_login_page(self) -> tuple[object, object]:
        """Open a fresh browser context at the login page. Returns (page, context)."""
        if not self._browser:
            await self.start()
        context = await self._browser.new_context()
        page = await context.new_page()
        await page.goto(PANEL_URL + "/login", timeout=20000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        return page, context

    async def submit_phone(self, page, phone_or_email: str) -> tuple[bool, str]:
        """
        Fill the phone/email field and click submit.
        Returns (True, '') if SMS code field appeared, (False, error) otherwise.
        """
        try:
            filled = await _fill_first(page, _PHONE_SELECTORS, phone_or_email)
            if not filled:
                return False, "Поле ввода телефона/email не найдено на странице входа"

            await _click_first(page, _SUBMIT_SELECTORS)
            # Wait for either a code input or navigation
            await asyncio.sleep(3)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Check if SMS code input appeared
            for sel in _CODE_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    return True, ""

            # Maybe already logged in (no 2FA)
            if "/login" not in page.url and "/auth" not in page.url:
                return True, "__already_logged_in__"

            return False, "Поле для кода не появилось — возможно, неверный номер или страница изменилась"
        except Exception as e:
            logger.error("submit_phone error: %s", e)
            return False, str(e)

    async def submit_code(self, page, context, code: str) -> tuple[bool, str]:
        """
        Enter SMS code and submit. If login succeeds, return (True, cookie_string).
        Otherwise (False, error_message).
        """
        try:
            filled = await _fill_first(page, _CODE_SELECTORS, code)
            if not filled:
                return False, "Поле для кода не найдено"

            await _click_first(page, _SUBMIT_SELECTORS)
            await asyncio.sleep(3)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Check if login succeeded
            if "/login" in page.url or "/auth" in page.url or "/signin" in page.url:
                # Check for error message on page
                error_el = await page.query_selector('.error, .alert-danger, [class*="error"]')
                if error_el:
                    error_text = await error_el.inner_text()
                    return False, f"Ошибка: {error_text.strip()[:100]}"
                return False, "Неверный код или истёк срок действия"

            # Extract cookies
            cookies = await context.cookies()
            cookie_string = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies
                if c.get("domain", "").endswith("yoomarket.net")
            )
            if not cookie_string:
                # Fallback: get all cookies
                cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            logger.info("Login successful, extracted %d cookies", len(cookies))
            return True, cookie_string

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

