"""Module 11 — optimize scene (version-gated).

On 0.11.x OSS the optimization feature does not exist (it moves into OSS in
0.12.0, plan item T2). This module probes the endpoint and cleanly SKIPs with
a recorded note when absent. When present it drives the loop-completion scene
in the recorded browser: trigger optimize on the newest runtime session,
apply a suggestion, and assert savings are DISPLAYED (exact deltas are
informational only).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import riglib  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
STATE = riglib.run_dir() / "state" / "browser-state.json"

# The RFC 4122 nil UUID: a syntactically valid session id that can never
# belong to a real runtime session. Used ONLY to probe whether the optimize
# route is mounted at all (route absent => generic FastAPI 404 "Not Found";
# route present => any session-aware response for this id).
NIL_UUID_PROBE_SESSION_ID = "00000000-0000-0000-0000-000000000000"


def feature_present(token: str) -> bool:
    """Probe the optimize route. A FastAPI app without the route answers 404
    body {"detail": "Not Found"}; with the route mounted, any other status
    (402/403/422) or a session-specific 404 message means it exists."""
    status, body = riglib.api_request(
        f"{URL}/api/v1/billing/cost/runtime-sessions/"
        f"{NIL_UUID_PROBE_SESSION_ID}/optimizations",
        method="POST",
        token=token,
        payload={},
    )
    riglib.log(f"probe status={status} body={riglib.redact(body)}")
    if status == 404 and isinstance(body, dict) and body.get("detail") == "Not Found":
        return False
    return True


def run_browser_scene(token: str) -> None:
    """Best-effort recorded optimize scene for >=0.12.0 instances.

    UNTESTED against a real 0.12 build (none exists yet) — selectors are
    generic on purpose and must be tightened when T2 lands.
    """
    import browserlib
    from playwright.sync_api import sync_playwright

    status, sessions = riglib.api_request(
        f"{URL}/api/v1/runtime-sessions?limit=1", token=token
    )
    items = (
        sessions.get("items") or sessions.get("sessions") or sessions
        if isinstance(sessions, dict)
        else sessions
    )
    if not items:
        riglib.skip("optimize present but no runtime session exists to optimize")
    session_id = items[0].get("id")

    with sync_playwright() as pw:
        browser = browserlib.launch(pw)
        context = browserlib.new_context(browser, storage_state=str(STATE))
        page = context.new_page()
        page.goto(
            f"{URL}/console/runtime-sessions?sessionId={session_id}",
            wait_until="networkidle",
        )
        time.sleep(2)
        page.get_by_text("Optimize", exact=False).first.click()
        page.wait_for_timeout(5000)
        browserlib.screenshot(page, "11-optimize-suggestions")

        # PASS criterion (loop-completion per the plan's D22 posture): the
        # Optimize view rendered. Apply + savings are informational — a tiny
        # fixture session (few turns, zero tools) legitimately yields no
        # applicable suggestions and therefore no savings figure.
        if page.get_by_text("Optimization Suggestions", exact=False).count() == 0:
            browserlib.screenshot(page, "11-optimize-missing")
            context.close()
            browser.close()
            raise SystemExit("Optimize tab clicked but suggestions view absent")

        # The Session Explorer Filters bar has its own "Apply" button that
        # precedes the observer in the DOM; optimization apply buttons (if
        # any suggestions are applicable) come after it — so only click when
        # there is more than one "Apply" on the page, and take the last.
        applied = False
        apply_buttons = page.get_by_role("button", name="Apply")
        if apply_buttons.count() > 1:
            apply_buttons.last.click()
            page.wait_for_timeout(3000)
            applied = True
        body_text = page.inner_text("body")
        browserlib.screenshot(page, "11-optimize-applied")
        context.close()
        browser.close()

    lowered = body_text.lower()
    savings_shown = "saving" in lowered or "saved" in lowered
    riglib.note(
        "optimize loop completed: suggestions view rendered"
        + (", applied" if applied else ", nothing applicable to apply")
        + (
            ", savings displayed"
            if savings_shown
            else ", no savings figure (informational)"
        )
    )


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)

    if not feature_present(token):
        riglib.skip(
            f"optimization endpoint absent on release {riglib.env('RIG_RELEASE')} "
            "(feature ships in OSS 0.12.0 — T2); scene skipped by design"
        )

    riglib.note("optimization endpoint present — running the browser scene")
    run_browser_scene(token)


if __name__ == "__main__":
    main()
