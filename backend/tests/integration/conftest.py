"""
Pytest configuration and fixtures for integration tests.
"""

import os
from pathlib import Path

import pytest

from tests.integration.http_client import (
    create_integration_sync_client,
    wait_for_preloop_ready,
)

# Directory for screenshots on failure (for UI tests)
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test result on the item for fixtures to access."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# Test configuration
PRELOOP_URL = os.getenv("PRELOOP_TEST_URL", "").rstrip("/")
PRELOOP_API_KEY = os.getenv("PRELOOP_TEST_API_KEY", "")


@pytest.fixture(scope="session", autouse=True)
def wait_for_deployed_preloop() -> None:
    """Wait for DB-backed health before any integration test module runs."""
    if not PRELOOP_URL:
        return
    wait_for_preloop_ready(PRELOOP_URL)


@pytest.fixture(scope="module")
def preloop_client():
    """Create Preloop HTTP client with authentication."""
    if not PRELOOP_URL or not PRELOOP_API_KEY:
        pytest.skip("PRELOOP_TEST_URL and PRELOOP_TEST_API_KEY required")

    headers = {
        "Authorization": f"Bearer {PRELOOP_API_KEY}",
        "Content-Type": "application/json",
    }
    with create_integration_sync_client(
        base_url=PRELOOP_URL,
        headers=headers,
    ) as client:
        yield client


@pytest.mark.integration
def test_preloop_health(preloop_client):
    """
    Health Check: Verify Preloop instance is running and accessible.
    """
    print("\n" + "=" * 80)
    print("STEP 1: Health Check")
    print("=" * 80)

    response = preloop_client.get("/api/v1/health")
    assert response.status_code == 200, f"Health check failed: {response.text}"
    body = response.json()
    assert body.get("database") == "connected", body
    assert body.get("mcp_server") == "available", body
    print("✓ Preloop instance is healthy")
