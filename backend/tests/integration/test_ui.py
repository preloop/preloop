import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.environ.get("PRELOOP_TEST_URL")
USERNAME = os.environ.get("PRELOOP_TEST_USERNAME")
PASSWORD = os.environ.get("PRELOOP_TEST_PASSWORD")

# Directory for screenshots on failure
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


def _artifact_safe_name(value: str) -> str:
    """Normalize test IDs so screenshot artifacts have portable filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "ui-test"


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def capture_screenshot_on_failure(request, page: Page):
    """Capture a screenshot if the test fails."""
    yield
    # After test execution, check if it failed
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        screenshot_name = _artifact_safe_name(request.node.nodeid)
        screenshot_path = SCREENSHOTS_DIR / f"{screenshot_name}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved to: {screenshot_path}")


@pytest.mark.skipif(
    not all([BASE_URL, USERNAME, PASSWORD]), reason="Missing test credentials"
)
def test_lit_app_successful_login(page: Page):
    """
    This test verifies the login functionality of the Lit web application.
    It navigates to the site, clicks the sign-in button, fills out the
    login form within the web components, and asserts that the dashboard
    is visible after a successful login.
    """
    # Pre-set localStorage to dismiss the welcome card onboarding flow,
    # ensuring the main dashboard view is rendered immediately.
    page.add_init_script(
        "window.localStorage.setItem('dashboard_welcome_dismissed', 'true');"
        "window.localStorage.removeItem('accessToken');"
        "window.localStorage.removeItem('refreshToken');"
    )

    # 1. Navigate directly to the login page to avoid any landing page routing issues
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")

    # 2. Wait for the page to load and locate the main app shell
    lit_app_shell = page.locator("lit-app")
    expect(lit_app_shell).to_be_visible(timeout=10000)

    # 3. Wait for the routed login form to hydrate inside the Lit app.
    # Custom element hosts can report as hidden even when their shadow DOM
    # content is interactive, so assert against visible form content instead.
    login_view = lit_app_shell.locator("login-view")
    login_heading = login_view.get_by_role("heading", name=re.compile(r"Sign in to"))
    login_form = login_view.locator("form")
    expect(login_heading).to_be_visible(timeout=15000)
    expect(login_form).to_be_visible(timeout=15000)

    # 4. Fill in the login credentials and submit the form.
    # We locate the Shoelace (sl-*) input components by their name attribute
    username_input = login_view.locator('sl-input[name="username"]').locator("input")
    password_input = login_view.locator('sl-input[name="password"]').locator("input")
    submit_button = login_view.locator('sl-button[type="submit"]')

    # Wait for inputs to be ready
    expect(username_input).to_be_visible(timeout=5000)
    expect(password_input).to_be_visible(timeout=5000)

    username_input.fill(USERNAME)
    password_input.fill(PASSWORD)
    submit_button.click()

    # 5. Wait for successful login and check that the dashboard route has loaded.
    expect(page).to_have_url(re.compile(r"/console(?:$|[/?#])"), timeout=15000)
    dashboard_heading = lit_app_shell.locator("dashboard-view h1")
    expect(dashboard_heading).to_be_visible(timeout=15000)
    expect(dashboard_heading).to_contain_text("Overview")
