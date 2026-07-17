"""Playwright helpers shared by the browser modules.

Headed by default (recording doubles as launch footage); falls back to
headless automatically when no display is available. Every context records
video into RIG_RUN_DIR/browser-videos/.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import riglib  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}


def want_headless() -> bool:
    if os.environ.get("RIG_HEADLESS", "0") == "1":
        return True
    # macOS always has a window server for a logged-in user; on Linux require
    # a display.
    if sys.platform == "darwin":
        return False
    return not os.environ.get("DISPLAY")


def launch(pw, slowmo: int = 150):
    """Launch chromium headed if possible; fall back to headless."""
    headless = want_headless()
    try:
        browser = pw.chromium.launch(headless=headless, slow_mo=slowmo)
    except Exception as exc:  # e.g. missing display server
        if headless:
            raise
        riglib.note(f"headed launch failed ({exc}); falling back to headless")
        headless = True
        browser = pw.chromium.launch(headless=True, slow_mo=slowmo)
    riglib.log(f"chromium launched (headless={headless})")
    return browser


def new_context(browser, storage_state: str | None = None):
    video_dir = riglib.run_dir() / "browser-videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "viewport": VIEWPORT,
        "record_video_dir": str(video_dir),
        "record_video_size": VIEWPORT,
    }
    if storage_state and Path(storage_state).exists():
        kwargs["storage_state"] = storage_state
    return browser.new_context(**kwargs)


def fill_sl_input(page, input_id: str, value: str) -> None:
    """Fill a Shoelace <sl-input id=...> (Playwright pierces shadow DOM)."""
    locator = page.locator(f"sl-input#{input_id} input").first
    locator.wait_for(state="visible", timeout=15000)
    locator.click()
    locator.fill(value)


def screenshot(page, name: str) -> None:
    path = riglib.run_dir() / "screenshots" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=False)
    riglib.log(f"screenshot: {path}")
