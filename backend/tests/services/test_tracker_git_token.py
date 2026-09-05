"""Tests for tracker git token resolution.

Regression cover for the post-execution push that ran with no credentials:
a GitHub App tracker stores no API key, so every caller that read
``Tracker.resolved_api_key`` directly resolved an empty token, the agent
container got no credential helper, and ``git push`` failed with "could not
read Username for 'https://github.com'".
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services.tracker_git_token import resolve_tracker_git_token

pytestmark = pytest.mark.asyncio

PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
APP_TOKEN = "ghs_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


def _tracker(*, api_key="", auth_type="api_token", installation_id=None):
    tracker = MagicMock()
    tracker.id = "tracker-1"
    tracker.resolved_api_key = api_key
    tracker.auth_type = auth_type
    if installation_id is None:
        tracker.oauth_installation = None
    else:
        tracker.oauth_installation = MagicMock(external_id=installation_id)
    return tracker


@pytest.fixture
def github_app_plugin(monkeypatch):
    """Install a stand-in for the proprietary GitHub App plugin."""

    service = MagicMock()
    service.get_installation_access_token = AsyncMock(return_value=APP_TOKEN)

    module = types.ModuleType("preloop.plugins.proprietary.github_app.service")
    module.get_github_app_service = lambda: service

    for name in (
        "preloop.plugins.proprietary",
        "preloop.plugins.proprietary.github_app",
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = []  # mark as a package
            monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(
        sys.modules, "preloop.plugins.proprietary.github_app.service", module
    )
    return service


class TestResolveTrackerGitToken:
    async def test_stored_key_is_returned_as_is(self):
        assert await resolve_tracker_git_token(_tracker(api_key=PAT)) == PAT

    async def test_app_tracker_mints_an_installation_token(self, github_app_plugin):
        tracker = _tracker(auth_type="github_app", installation_id=4242)

        assert await resolve_tracker_git_token(tracker) == APP_TOKEN
        github_app_plugin.get_installation_access_token.assert_awaited_once_with(4242)

    async def test_oauth_app_auth_type_is_also_supported(self, github_app_plugin):
        tracker = _tracker(auth_type="oauth_app", installation_id=7)
        assert await resolve_tracker_git_token(tracker) == APP_TOKEN

    async def test_stored_key_wins_without_calling_the_plugin(self, github_app_plugin):
        tracker = _tracker(api_key=PAT, auth_type="github_app", installation_id=4242)

        assert await resolve_tracker_git_token(tracker) == PAT
        github_app_plugin.get_installation_access_token.assert_not_awaited()

    async def test_app_tracker_without_installation_returns_none(self):
        tracker = _tracker(auth_type="github_app")
        assert await resolve_tracker_git_token(tracker) is None

    async def test_minting_failure_degrades_to_no_token(self, github_app_plugin):
        github_app_plugin.get_installation_access_token.side_effect = RuntimeError(
            "GitHub is down"
        )
        tracker = _tracker(auth_type="github_app", installation_id=4242)

        assert await resolve_tracker_git_token(tracker) is None

    async def test_api_token_tracker_without_key_returns_none(self):
        assert await resolve_tracker_git_token(_tracker()) is None

    async def test_missing_tracker_returns_none(self):
        assert await resolve_tracker_git_token(None) is None
