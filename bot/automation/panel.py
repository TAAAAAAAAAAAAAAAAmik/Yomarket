"""YooMarket panel browser automation via Playwright (cookie-based auth + SMS login)."""
from __future__ import annotations

import asyncio
import logging
import re

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

