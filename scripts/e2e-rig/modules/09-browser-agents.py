"""Module 09 — recorded browser session confirms every onboarded agent shows
up on the agents view with a healthy (active, non-suspended) status.

Assertion is two-layered: the API is the source of truth for lifecycle
state; the recorded page must additionally display each agent by name.
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


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)

    status, body = riglib.api_request(f"{URL}/api/v1/agents", token=token)
    if status != 200:
        raise SystemExit(f"GET /api/v1/agents failed ({status}): {body}")
    agents = body.get("agents", body) if isinstance(body, dict) else body
    if not agents:
        raise SystemExit("API reports zero managed agents after onboarding")

    unhealthy = [
        a["display_name"] for a in agents if a.get("lifecycle_state") not in ("active",)
    ]
    names = [a["display_name"] for a in agents]
    riglib.log(f"API managed agents: {names}")
    if unhealthy:
        raise SystemExit(f"agents not in active lifecycle: {unhealthy}")

    with sync_playwright() as pw:
        browser = browserlib.launch(pw)
        context = browserlib.new_context(browser, storage_state=str(STATE))
        page = context.new_page()
        page.goto(f"{URL}/console/agents", wait_until="networkidle")
        time.sleep(3)  # let websocket/badges settle for the recording

        page_text = page.inner_text("body")
        missing = [n for n in names if n not in page_text]
        browserlib.screenshot(page, "09-agents-view")

        # Scroll through the list so the recording shows every card.
        page.mouse.wheel(0, 600)
        time.sleep(1.5)
        context.storage_state(path=str(STATE))
        context.close()
        browser.close()

    if missing:
        raise SystemExit(f"agents missing from the agents view: {missing}")
    riglib.note(
        f"{len(names)} agents visible on /console/agents and active via API: "
        + ", ".join(names)
    )


if __name__ == "__main__":
    main()
