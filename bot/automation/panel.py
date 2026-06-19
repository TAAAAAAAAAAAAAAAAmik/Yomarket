"""YooMarket panel browser automation via Playwright (cookie-based auth)."""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

PANEL_URL = "https://panel.yoomarket.net"


def _parse_cookies(cookie_string: str) -> list[dict]:
    """Parse 'key=value; key2=value2' cookie string into Playwright cookie list."""
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


class YooMarketPanel:
    """Headless Chromium automation for the YooMarket seller panel."""

    def __init__(self, cookie_string: str) -> None:
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

    async def _new_page(self):
        """Create a new page with pre-loaded session cookies."""
        if not self._browser:
            await self.start()
        context = await self._browser.new_context()
        cookies = _parse_cookies(self.cookie_string)
        if cookies:
            await context.add_cookies(cookies)
        return await context.new_page(), context

    async def check_session(self) -> bool:
        """Returns True if the session cookies are still valid."""
        page, context = await self._new_page()
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

    async def bump_all_ads(self) -> tuple[int, str]:
        """Navigate to /goods and click all bump buttons."""
        page, context = await self._new_page()
        try:
            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            if "/login" in page.url or "/auth" in page.url:
                return 0, "❌ Сессия истекла — обнови куки в настройках"

            bump_buttons = []
            for selector in [
                'button:has-text("Поднять")',
                'a:has-text("Поднять")',
                '[data-action="bump"]',
                '.bump-btn',
            ]:
                try:
                    buttons = await page.query_selector_all(selector)
                    if buttons:
                        bump_buttons = buttons
                        break
                except Exception:
                    continue

            if not bump_buttons:
                return 0, "ℹ️ Кнопки поднятия не найдены (возможно, все уже подняты)"

            count = 0
            for btn in bump_buttons:
                try:
                    await btn.click()
                    count += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning("Bump click error: %s", e)

            return count, (f"✅ Поднято объявлений: {count}" if count else "⚠️ Не удалось поднять объявления")

        except Exception as e:
            logger.error("bump_all_ads error: %s", e)
            return 0, f"❌ Ошибка: {e}"
        finally:
            await page.close()
            await context.close()

    async def restore_sold_ads(self) -> tuple[int, str]:
        """Navigate to /goods and click all restore buttons."""
        page, context = await self._new_page()
        try:
            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            if "/login" in page.url or "/auth" in page.url:
                return 0, "❌ Сессия истекла — обнови куки в настройках"

            restore_buttons = []
            for selector in [
                'button:has-text("Восстановить")',
                'button:has-text("Активировать")',
                '[data-action="restore"]',
                'a:has-text("Восстановить")',
                'a:has-text("Активировать")',
            ]:
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
        """Withdraw balance if >= min_amount."""
        page, context = await self._new_page()
        try:
            current_balance = 0.0
            balance_found = False

            for path in ["/finance", "/wallet", "/balance"]:
                try:
                    await page.goto(PANEL_URL + path, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    if "/login" in page.url or "/auth" in page.url:
                        return False, "❌ Сессия истекла — обнови куки в настройках"
                    page_text = await page.inner_text("body")
                    for pattern in [
                        r"([\d\s]+[,.]?\d*)\s*₽",
                        r"([\d\s]+[,.]?\d*)\s*RUB",
                        r"Баланс[:\s]+([\d\s]+[,.]?\d*)",
                    ]:
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
                    logger.warning("Failed to load %s: %s", path, e)

            if not balance_found:
                return False, "❌ Не удалось найти баланс на странице"

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

            for selector in ['button[type="submit"]', 'button:has-text("Подтвердить")',
                             'button:has-text("OK")']:
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
