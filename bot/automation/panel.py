"""YooMarket panel browser automation via Playwright."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

PANEL_URL = "https://panel.yoomarket.net"


class YooMarketPanel:
    """Headless Chromium automation for the YooMarket seller panel."""

    def __init__(self, login: str, password: str) -> None:
        self.login = login
        self.password = password
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        """Launch headless Chromium browser."""
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("Playwright browser started")

    async def close(self) -> None:
        """Close browser and playwright."""
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
        except Exception as e:
            logger.warning("Error closing browser: %s", e)
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        except Exception as e:
            logger.warning("Error stopping playwright: %s", e)
        logger.info("Playwright browser closed")

    async def _new_page(self):
        """Create a new browser page."""
        if not self._browser:
            await self.start()
        return await self._browser.new_page()

    async def check_login(self, page) -> bool:
        """Check if the user is currently logged in."""
        try:
            await page.goto(PANEL_URL + "/", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            current_url = page.url
            # If redirected to login page, not logged in
            if "/login" in current_url or "/auth" in current_url or "/signin" in current_url:
                return False
            # Check for login form on current page
            login_input = await page.query_selector('input[type="email"], input[name="login"], input[name="email"]')
            if login_input:
                return False
            return True
        except Exception as e:
            logger.warning("check_login error: %s", e)
            return False

    async def do_login(self, page) -> bool:
        """Fill and submit the login form."""
        try:
            await page.goto(PANEL_URL + "/login", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Try various email selectors
            login_filled = False
            for selector in ['input[type="email"]', 'input[name="login"]', 'input[name="email"]']:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.fill(self.login)
                        login_filled = True
                        logger.debug("Filled login with selector: %s", selector)
                        break
                except Exception:
                    continue

            if not login_filled:
                logger.error("Could not find login input field")
                return False

            # Fill password
            pwd_el = await page.query_selector('input[type="password"]')
            if not pwd_el:
                logger.error("Could not find password input field")
                return False
            await pwd_el.fill(self.password)

            # Click submit
            submit_el = await page.query_selector('button[type="submit"]')
            if not submit_el:
                logger.error("Could not find submit button")
                return False
            await submit_el.click()

            # Wait for navigation after login
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            current_url = page.url
            if "/login" in current_url or "/auth" in current_url or "/signin" in current_url:
                logger.warning("Login failed — still on login page after submit")
                return False

            logger.info("Login successful")
            return True
        except Exception as e:
            logger.error("do_login error: %s", e)
            return False

    async def ensure_logged_in(self, page) -> bool:
        """Make sure we're logged in, logging in if necessary."""
        logged_in = await self.check_login(page)
        if logged_in:
            return True
        logger.info("Not logged in, attempting login...")
        return await self.do_login(page)

    async def bump_all_ads(self) -> tuple[int, str]:
        """Navigate to /goods and click all bump buttons."""
        page = None
        try:
            page = await self._new_page()
            ok = await self.ensure_logged_in(page)
            if not ok:
                return (0, "❌ Не удалось войти в панель")

            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Find bump buttons with various selectors
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
                        logger.debug("Found %d bump buttons with selector: %s", len(buttons), selector)
                        break
                except Exception:
                    continue

            if not bump_buttons:
                return (0, "ℹ️ Кнопки поднятия не найдены (возможно, все уже подняты)")

            count = 0
            for btn in bump_buttons:
                try:
                    await btn.click()
                    count += 1
                    logger.debug("Bumped ad #%d", count)
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning("Failed to click bump button #%d: %s", count + 1, e)

            msg = f"✅ Поднято объявлений: {count}" if count else "⚠️ Ни одно объявление не удалось поднять"
            return (count, msg)

        except Exception as e:
            logger.error("bump_all_ads error: %s", e)
            return (0, f"❌ Ошибка при поднятии: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def restore_sold_ads(self) -> tuple[int, str]:
        """Navigate to /goods and click all restore buttons."""
        page = None
        try:
            page = await self._new_page()
            ok = await self.ensure_logged_in(page)
            if not ok:
                return (0, "❌ Не удалось войти в панель")

            await page.goto(PANEL_URL + "/goods", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Find restore buttons with various selectors
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
                        logger.debug("Found %d restore buttons with selector: %s", len(buttons), selector)
                        break
                except Exception:
                    continue

            if not restore_buttons:
                return (0, "ℹ️ Кнопки восстановления не найдены (нет проданных/истёкших товаров)")

            count = 0
            for btn in restore_buttons:
                try:
                    await btn.click()
                    count += 1
                    logger.debug("Restored ad #%d", count)
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning("Failed to click restore button #%d: %s", count + 1, e)

            msg = f"✅ Восстановлено объявлений: {count}" if count else "⚠️ Ни одно объявление не удалось восстановить"
            return (count, msg)

        except Exception as e:
            logger.error("restore_sold_ads error: %s", e)
            return (0, f"❌ Ошибка при восстановлении: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def withdraw_balance(self, min_amount: int) -> tuple[bool, str]:
        """Withdraw balance if it is at or above min_amount."""
        page = None
        try:
            page = await self._new_page()
            ok = await self.ensure_logged_in(page)
            if not ok:
                return (False, "❌ Не удалось войти в панель")

            # Try /finance first, then /wallet
            balance_found = False
            current_balance = 0

            for path in ["/finance", "/wallet", "/balance"]:
                try:
                    await page.goto(PANEL_URL + path, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=15000)

                    # Try to read balance from page text
                    page_text = await page.inner_text("body")
                    # Look for patterns like "1 234 ₽", "1234.56 ₽", "1234 RUB" etc.
                    patterns = [
                        r"([\d\s]+[,.]?\d*)\s*₽",
                        r"([\d\s]+[,.]?\d*)\s*RUB",
                        r"Баланс[:\s]+([\d\s]+[,.]?\d*)",
                        r"Balance[:\s]+([\d\s]+[,.]?\d*)",
                    ]
                    for pattern in patterns:
                        matches = re.findall(pattern, page_text)
                        if matches:
                            raw = matches[0].replace(" ", "").replace(",", ".").strip()
                            try:
                                current_balance = float(raw)
                                balance_found = True
                                logger.info("Found balance: %.2f on path %s", current_balance, path)
                                break
                            except ValueError:
                                continue
                    if balance_found:
                        break
                except Exception as e:
                    logger.warning("Failed to load %s: %s", path, e)

            if not balance_found:
                return (False, "❌ Не удалось найти баланс на странице")

            if current_balance < min_amount:
                return (False, f"ℹ️ Баланс {current_balance:.0f} ₽ ниже порога {min_amount} ₽")

            # Find withdraw button
            withdraw_clicked = False
            for selector in [
                'button:has-text("Вывести")',
                'button:has-text("Вывод")',
                'a:has-text("Вывести")',
                'a:has-text("Вывод средств")',
                '[data-action="withdraw"]',
                '.withdraw-btn',
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.5)
                        withdraw_clicked = True
                        logger.debug("Clicked withdraw button with selector: %s", selector)
                        break
                except Exception:
                    continue

            if not withdraw_clicked:
                return (False, f"❌ Кнопка вывода не найдена (баланс: {current_balance:.0f} ₽)")

            # Try to fill amount input if it appeared
            try:
                amount_input = await page.query_selector('input[type="number"], input[name="amount"], input[placeholder*="сумм"]')
                if amount_input:
                    await amount_input.fill(str(int(current_balance)))
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug("No amount input or error filling it: %s", e)

            # Try to confirm / submit
            confirmed = False
            for selector in [
                'button[type="submit"]',
                'button:has-text("Подтвердить")',
                'button:has-text("Вывести")',
                'button:has-text("OK")',
            ]:
                try:
                    btn = await page.query_selector(selector)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1.5)
                        confirmed = True
                        logger.debug("Confirmed withdraw with selector: %s", selector)
                        break
                except Exception:
                    continue

            if confirmed:
                return (True, f"✅ Вывод {current_balance:.0f} ₽ выполнен")
            else:
                return (True, f"⚠️ Кнопка вывода нажата, но подтверждение не найдено (баланс: {current_balance:.0f} ₽)")

        except Exception as e:
            logger.error("withdraw_balance error: %s", e)
            return (False, f"❌ Ошибка при выводе: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
