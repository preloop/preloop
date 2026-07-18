"""Module 13 — recorded, unhurried console tour for the showcase video.

Walks the populated console in one continuous recorded browser session:
Overview -> Agents (canvas + cards) -> an agent detail -> Sessions with the
wasteful session's transcript (paused on, never scrolled — video production
rule) -> its Optimize results -> Cost -> Approvals -> Models.

Each stop lingers a few seconds and takes a still; deliberate small scrolls
only on list-style pages. Stop metadata (title, path, timestamp, screenshot)
is saved to ``state/tour-metadata.json`` for the compositor's title cards.
"""

from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import browserlib  # noqa: E402
import riglib  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
STATE = riglib.run_dir() / "state" / "browser-state.json"

LINGER_S = 3.0  # per-stop pause (2-4s per the shot plan)


def pick_agent_id(token: str) -> str | None:
    """A recognizable onboarded agent for the detail stop (Claude Code
    preferred), falling back to any managed agent."""
    agents = riglib.list_agents(URL, token)
    for preferred in ("Claude Code", "OpenCode", "Codex CLI"):
        for agent in agents:
            if agent.get("display_name") == preferred:
                return str(agent["id"])
    return str(agents[0]["id"]) if agents else None


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)
    agent_id = pick_agent_id(token)
    session_state = riglib.load_state("wasteful-session.json") or {}
    session_id = session_state.get("runtime_session_id")

    stops: list[dict] = []

    def record_stop(name: str, title: str, page) -> None:
        shot = f"13-tour-{len(stops) + 1:02d}-{name}"
        browserlib.screenshot(page, shot)
        stops.append(
            {
                "name": name,
                "title": title,
                "path": page.url.replace(URL, ""),
                "screenshot": f"{shot}.png",
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )

    def gentle_scroll(page, steps: int = 3, dy: int = 350) -> None:
        for _ in range(steps):
            page.mouse.wheel(0, dy)
            time.sleep(1.2)

    with sync_playwright() as pw:
        browser = browserlib.launch(pw)
        context = browserlib.new_context(browser, storage_state=str(STATE))
        page = context.new_page()

        # 1. Overview.
        page.goto(f"{URL}/console", wait_until="networkidle")
        time.sleep(LINGER_S)
        record_stop("overview", "Overview", page)

        # 2. Agents (populated fleet; canvas first, then the card list).
        page.goto(f"{URL}/console/agents", wait_until="networkidle")
        time.sleep(LINGER_S)
        record_stop("agents-canvas", "Agents — fleet canvas", page)
        cards_toggle = page.locator('sl-radio-button[value="cards"]').first
        if cards_toggle.count() > 0:
            cards_toggle.click()
            time.sleep(2)
            gentle_scroll(page, steps=2)
            record_stop("agents-cards", "Agents — card list", page)

        # 3. One agent in detail.
        if agent_id:
            page.goto(f"{URL}/console/agents/{agent_id}", wait_until="networkidle")
            time.sleep(LINGER_S)
            record_stop("agent-detail", "Agent detail", page)
            gentle_scroll(page, steps=2)

        # 4. Sessions — the wasteful session's transcript. PAUSE on the
        # transcript; never scroll it on camera (video production rule).
        sessions_url = f"{URL}/console/runtime-sessions"
        if session_id:
            sessions_url += f"?sessionId={session_id}"
        page.goto(sessions_url, wait_until="networkidle")
        time.sleep(LINGER_S + 1.5)
        record_stop(
            "session-transcript", "Sessions — wasteful session transcript", page
        )

        # 5. Optimize results for that session (served from the module-12
        # cache; best-effort — the tour must not fail on a cache miss).
        try:
            optimize_btn = page.locator(
                "preloop-session-observer sl-button", has_text="Optimize"
            ).first
            optimize_btn.wait_for(state="visible", timeout=15000)
            optimize_btn.click()
            time.sleep(2)
            if page.locator("session-optimization-panel div.suggestion").count() == 0:
                generate = page.locator(
                    "sl-button",
                    has_text="suggestions",
                )
                if generate.count() > 0:
                    generate.first.click()
                    page.wait_for_selector(
                        "session-optimization-panel div.suggestion", timeout=90000
                    )
            time.sleep(LINGER_S)
            record_stop("optimize-results", "Optimize — suggestions & savings", page)
        except Exception as exc:  # noqa: BLE001 — tour stops are best-effort
            riglib.note(f"tour: optimize results stop skipped ({exc})")

        # 6. Cost.
        page.goto(f"{URL}/console/cost", wait_until="networkidle")
        time.sleep(LINGER_S)
        record_stop("cost", "Cost", page)
        gentle_scroll(page, steps=2)

        # 7. Approvals.
        page.goto(f"{URL}/console/approvals", wait_until="networkidle")
        time.sleep(LINGER_S)
        record_stop("approvals", "Approvals", page)

        # 8. Models.
        page.goto(f"{URL}/console/ai-models", wait_until="networkidle")
        time.sleep(LINGER_S)
        record_stop("models", "Models", page)

        time.sleep(2)
        video = page.video.path() if page.video else None
        context.close()
        browser.close()
        if video:
            riglib.save_state("video-13.json", {"path": str(video)})

    riglib.save_state(
        "tour-metadata.json",
        {
            "run_id": riglib.run_dir().name,
            "session_id": session_id,
            "agent_id": agent_id,
            "stops": stops,
        },
    )
    if len(stops) < 6:
        raise SystemExit(f"console tour incomplete: only {len(stops)} stops recorded")
    riglib.note(
        f"console tour recorded {len(stops)} stops: "
        + ", ".join(s["name"] for s in stops)
    )


if __name__ == "__main__":
    main()
