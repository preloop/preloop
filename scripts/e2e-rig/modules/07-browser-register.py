"""Module 07 — recorded first-user signup on the fresh instance.

Playwright chromium (headed by default), video ON. Registers the run's
generated user at /register, verifies the authed console loads, then saves
the browser storage state (session survives into modules 09/11) and mints an
API token for the CLI + API modules.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import browserlib  # noqa: E402
import riglib  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
STATE = riglib.run_dir() / "state" / "browser-state.json"


def bootstrap_register_url() -> str:
    """Return the register URL, tokenized when the installer printed one.

    Module 05 captures the installer's terminal output (including the
    "Next steps" footer with the setup link) in logs/05-install.txt. Newer
    releases print a ``/register#bootstrap=<token>`` link there; the first
    signup must go through it. Older releases have no token — fall back to
    the plain register URL. The token itself is never logged.
    """
    log_path = riglib.run_dir() / "logs" / "05-install.txt"
    try:
        installer_output = log_path.read_text()
    except OSError:
        return f"{URL}/register"
    match = re.search(r"/register#bootstrap=([0-9a-fA-F]+)", installer_output)
    if not match:
        return f"{URL}/register"
    riglib.note("using the tokenized bootstrap setup link from the installer output")
    return f"{URL}/register#bootstrap={match.group(1)}"


def register(page, creds) -> str:
    """Try to register; returns 'registered', 'needs-login' or 'exists'."""
    page.goto(bootstrap_register_url(), wait_until="networkidle")
    browserlib.screenshot(page, "07-register-form")
    browserlib.fill_sl_input(page, "username", creds["username"])
    browserlib.fill_sl_input(page, "email", creds["email"])
    browserlib.fill_sl_input(page, "password", creds["password"])
    time.sleep(0.5)
    page.locator("sl-button[type=submit]").first.click()

    # Success auto-logs-in and lands on /console (or a pending redirect);
    # if auto-login fails the app falls back to /login?registered=true.
    try:
        page.wait_for_url(re.compile(r"/(console|welcome|login)"), timeout=25000)
    except Exception:
        # Still on /register: look for the validation error (get_by_text
        # pierces Lit shadow DOM, inner_text('body') does not).
        if page.get_by_text("already registered").count() > 0:
            return "exists"
        browserlib.screenshot(page, "07-register-stuck")
        raise SystemExit(f"registration did not navigate: {page.url}")
    if "/login" in page.url:
        return "needs-login"
    return "registered"


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
        elif outcome == "needs-login":
            riglib.note("registered; auto-login fell back to the sign-in page")
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
            riglib.save_state("video-07.json", {"path": str(video)})

    # Mint an API token for the CLI/API modules and stash it with the creds.
    token = riglib.user_token(URL, creds)
    creds["api_token"] = token
    riglib.save_creds(creds)
    riglib.note(f"first user '{creds['username']}' signed up and API token minted")


if __name__ == "__main__":
    main()
