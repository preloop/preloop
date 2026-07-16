"""Module 07 — recorded first-user signup on the fresh instance.

Playwright chromium (headed by default), video ON. Registers the run's
generated user at /register, verifies the authed console loads, then saves
the browser storage state (session survives into modules 09/11) and mints an
API token for the CLI + API modules.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import browserlib  # noqa: E402
import riglib  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
STATE = riglib.run_dir() / "state" / "browser-state.json"


def register(page, creds) -> str:
    """Try to register; returns 'registered' or 'exists'."""
    page.goto(f"{URL}/register", wait_until="networkidle")
    browserlib.screenshot(page, "07-register-form")
    browserlib.fill_sl_input(page, "username", creds["username"])
    browserlib.fill_sl_input(page, "email", creds["email"])
    browserlib.fill_sl_input(page, "password", creds["password"])
    time.sleep(0.5)
    page.locator("sl-button[type=submit]").first.click()
    try:
        page.wait_for_url("**/console**", timeout=20000)
        return "registered"
    except Exception:
        pass
    try:
        page.wait_for_url("**/welcome**", timeout=5000)
        return "registered"
    except Exception:
        body = page.inner_text("body")
        if "already" in body.lower() or "exists" in body.lower():
            return "exists"
        browserlib.screenshot(page, "07-register-stuck")
        raise SystemExit(f"registration did not reach console/welcome: {page.url}")


def login(page, creds) -> None:
    page.goto(f"{URL}/login", wait_until="networkidle")
    browserlib.fill_sl_input(page, "username", creds["username"])
    browserlib.fill_sl_input(page, "password", creds["password"])
    page.locator("sl-button[type=submit]").first.click()
    page.wait_for_url("**/console**", timeout=20000)


def main() -> None:
    creds = riglib.load_creds()
    with sync_playwright() as pw:
        browser = browserlib.launch(pw)
        context = browserlib.new_context(browser)
        page = context.new_page()

        outcome = register(page, creds)
        if outcome == "exists":
            riglib.note("user already exists (resumed run) — logging in instead")
            login(page, creds)

        # Land on the console proper and let it render for the recording.
        if "/console" not in page.url:
            page.goto(f"{URL}/console", wait_until="networkidle")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        browserlib.screenshot(page, "07-console-after-signup")

        context.storage_state(path=str(STATE))
        video = page.video.path() if page.video else None
        context.close()
        browser.close()
        if video:
            riglib.log(f"browser video: {video}")

    # Mint an API token for the CLI/API modules and stash it with the creds.
    token = riglib.user_token(URL, creds)
    creds["api_token"] = token
    riglib.save_creds(creds)
    riglib.note(f"first user '{creds['username']}' signed up and API token minted")


if __name__ == "__main__":
    main()
