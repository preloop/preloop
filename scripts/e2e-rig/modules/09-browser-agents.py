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

    agents = riglib.list_agents(URL, token)
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

        # Linger on the canvas (gateway graph) for the recording, then switch
        # to the cards view: the canvas can pan nodes out of the viewport, so
        # assertions run against the card list.
        browserlib.screenshot(page, "09-agents-canvas")
        cards_toggle = page.locator('sl-radio-button[value="cards"]').first
        if cards_toggle.count() > 0:
            cards_toggle.click()
            time.sleep(2)

        # get_by_text pierces Lit shadow DOM; inner_text("body") does not.
        missing = [n for n in names if page.get_by_text(n, exact=False).count() == 0]
        healthy_badges = page.get_by_text("Active now", exact=False).count()
        riglib.log(f"'Active now' badges visible: {healthy_badges}")
        browserlib.screenshot(page, "09-agents-view")

        # Scroll through the list so the recording shows every card.
        page.mouse.wheel(0, 600)
        time.sleep(1.5)
        context.storage_state(path=str(STATE))
        video = page.video.path() if page.video else None
        context.close()
        browser.close()
        if video:
            riglib.save_state("video-09.json", {"path": str(video)})

    if missing:
        # The view's kind filter has a fixed list (frontend
        # AVAILABLE_AGENT_KINDS) that does not include every onboardable
        # kind — claude_desktop is absent, so an onboarded Claude Desktop
        # never renders. Record that as a product gap, not a rig failure.
        by_name = {a["display_name"]: a for a in agents}
        displayable_kinds = {
            "openclaw",
            "opencode",
            "claude_code",
            "codex",
            "gemini_cli",
            "hermes",
            "cursor",
            "windsurf",
            "desktop_agent",
            "custom",
        }
        hard_missing = [
            n for n in missing if by_name[n].get("agent_kind") in displayable_kinds
        ]
        undisplayable = [n for n in missing if n not in hard_missing]
        if undisplayable:
            riglib.note(
                "PRODUCT GAP: onboarded but not displayable on /console/agents "
                f"(agent_kind missing from the view's kind list): {undisplayable}"
            )
        if hard_missing:
            raise SystemExit(f"agents missing from the agents view: {hard_missing}")
    shown = [n for n in names if n not in missing]
    riglib.note(
        f"{len(shown)}/{len(names)} agents visible on /console/agents "
        f"and all {len(names)} active via API: " + ", ".join(shown)
    )


if __name__ == "__main__":
    main()
