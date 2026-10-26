"""LinkedIn login + session persistence via Playwright.

Ported from PostPilot's utils/linkedin-login.js (Node/Playwright) to Python.
Session is a saved storage_state (cookies + localStorage) at
config.LINKEDIN_SESSION_PATH, reused across runs so login only happens once
until the session goes stale.
"""
import json
import os
import random
import time

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from contentpilot import config

LOGIN_URL = "https://www.linkedin.com/login"


def _random_delay(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _is_logged_in(page: Page) -> bool:
    # The "Start a post" composer only renders on the feed once authenticated.
    return page.locator('button:has-text("Start a post"), [aria-label="Start a post"]').count() > 0


def _handle_two_factor(page: Page) -> bool:
    pin_input = page.locator("#input__email_verification_pin, input[name='pin']")
    if pin_input.count() == 0:
        return False

    print("[linkedin_auth] 2FA/verification code requested.")
    code = input("Enter the verification code sent to you: ").strip()
    pin_input.first.fill(code)

    submit_button = page.locator("#email-pin-submit-button, button[type='submit']")
    submit_button.first.click()
    page.wait_for_load_state("domcontentloaded")
    time.sleep(2)
    return True


def _wait_for_manual_resolution(page: Page) -> None:
    print(
        "[linkedin_auth] Unrecognized checkpoint (captcha / device verification). "
        "Resolve it in the visible browser window, then come back here."
    )
    input("Press Enter once you're logged in and see your LinkedIn feed: ")


def login(playwright, headless: bool = False) -> Browser:
    """Runs the full login flow and persists session state. Returns the open Browser."""
    email, password = config.linkedin_credentials()

    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context()
    page = context.new_page()

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        username_field = page.locator(
            '#username, input[type="email"][autocomplete*="username"]'
        ).locator("visible=true").first
        password_field = page.locator(
            '#password, input[type="password"][autocomplete="current-password"]'
        ).locator("visible=true").first
        sign_in_button = page.locator('button[type="submit"]').or_(
            page.get_by_role("button", name="Sign in", exact=True)
        ).locator("visible=true").first

        username_field.wait_for(state="visible", timeout=45000)
        username_field.fill(email)
        _random_delay(0.2, 0.5)
        password_field.fill(password)
        _random_delay(0.2, 0.5)
        sign_in_button.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        attempts = 0
        while not _is_logged_in(page) and attempts < 5:
            attempts += 1
            if _handle_two_factor(page):
                continue
            if "/checkpoint/" in page.url:
                _wait_for_manual_resolution(page)
                continue
            time.sleep(2)

        page.goto(config.LINKEDIN_FEED_URL, wait_until="domcontentloaded")
        if not _is_logged_in(page):
            raise RuntimeError(
                "Login did not complete — feed composer button not found after checkpoint handling."
            )

        os.makedirs(os.path.dirname(config.LINKEDIN_SESSION_PATH), exist_ok=True)
        context.storage_state(path=config.LINKEDIN_SESSION_PATH)
        print(f"[linkedin_auth] Session saved to {config.LINKEDIN_SESSION_PATH}")

        context.close()
        return browser
    except Exception:
        context.close()
        browser.close()
        raise


def ensure_valid_session(playwright, headless: bool = False) -> tuple[Browser, BrowserContext, Page]:
    """Returns an authenticated (browser, context, page) tuple, logging in fresh if needed."""
    browser = playwright.chromium.launch(headless=headless)

    if os.path.exists(config.LINKEDIN_SESSION_PATH):
        context = browser.new_context(storage_state=config.LINKEDIN_SESSION_PATH)
        page = context.new_page()
        page.goto(config.LINKEDIN_FEED_URL, wait_until="domcontentloaded")
        if _is_logged_in(page):
            return browser, context, page
        context.close()

    browser.close()
    print("[linkedin_auth] No valid session — running login flow to refresh it.")
    browser = login(playwright, headless=headless)

    context = browser.new_context(storage_state=config.LINKEDIN_SESSION_PATH)
    page = context.new_page()
    page.goto(config.LINKEDIN_FEED_URL, wait_until="domcontentloaded")
    if not _is_logged_in(page):
        raise RuntimeError("Session still invalid after re-login — aborting.")
    return browser, context, page


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    with sync_playwright() as p:
        browser, context, page = ensure_valid_session(p)
        print("[test:linkedin_auth] logged in, on:", page.url)
        context.close()
        browser.close()
