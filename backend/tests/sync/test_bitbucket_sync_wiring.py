"""Tests for Bitbucket Cloud in the scanner, host helpers and PR listing."""

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints.pull_requests import _tracker_kind
from preloop.models.models import Tracker
from preloop.sync.scanner.core import TrackerClient, _process_organization
from preloop.sync.trackers.bitbucket import BitbucketTracker
from preloop.utils.git_credentials import credential_username
from preloop.utils.repo_urls import tracker_host_kind


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bitbucket.org/ws/repo", "bitbucket"),
        ("https://api.bitbucket.org/2.0/repositories", "bitbucket"),
        ("https://bitbucket.org.example.com/ws/repo", None),
        ("https://notbitbucket.org/ws/repo", None),
    ],
)
def test_tracker_host_kind(url: str, expected: str | None) -> None:
    assert tracker_host_kind(url) == expected


def test_credential_username_default_for_bitbucket() -> None:
    assert credential_username("bitbucket", "bitbucket") == "x-bitbucket-api-token-auth"
    assert credential_username(None, "bitbucket") == "x-bitbucket-api-token-auth"


def test_pull_request_list_kind() -> None:
    assert _tracker_kind("bitbucket") == "bitbucket"


def _tracker(**details) -> MagicMock:
    tracker = MagicMock(spec=Tracker)
    tracker.id = "t-1"
    tracker.tracker_type = "bitbucket"
    tracker.resolved_api_key = "tok"
    tracker.auth_type = "oauth_token"
    tracker.connection_details = {"workspace": "ws", **details}
    tracker.url = "https://bitbucket.org"
    return tracker


def test_tracker_client_builds_bitbucket_client() -> None:
    client = TrackerClient(_tracker(repository="repo"))
    assert isinstance(client.client, BitbucketTracker)
    assert client.client.auth_type == "oauth_token"
    assert client.client.repo_full_name == "ws/repo"


def test_tracker_client_without_credentials() -> None:
    client = TrackerClient(_tracker(), initialize_client=False)
    assert isinstance(client.client, BitbucketTracker)
    assert client.client.api_key == ""


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
@patch("os.getenv")
async def test_scanner_registers_hooks_per_repository(
    mock_getenv, mock_crud_org
) -> None:
    mock_getenv.return_value = "https://preloop.test"
    first = SimpleNamespace(id="p-1", identifier="r-1", slug="ws/one")
    second = SimpleNamespace(id="p-2", identifier="r-2", slug="ws/two")
    org = MagicMock()
    org.id = "org-1"
    org.webhook_secret = "secret"
    org.last_webhook_update = None
    org.last_polled = None

    client = AsyncMock(spec=TrackerClient)
    client.tracker_type = "bitbucket"
    client.client = AsyncMock()
    client.scan_projects.return_value = [first, second]
    client.scan_issues.return_value = ([], 0)
    client.client.is_webhook_registered_for_project = AsyncMock(
        side_effect=[True, False]
    )
    client.client.register_webhook = AsyncMock(return_value=True)

    stats = await _process_organization(
        db=MagicMock(spec=Session),
        client=client,
        org=org,
        since=datetime.datetime.now(datetime.timezone.utc),
        force_update=False,
    )

    client.client.register_webhook.assert_awaited_once()
    kwargs = client.client.register_webhook.await_args.kwargs
    assert kwargs["project"] is second
    assert kwargs["secret"] == "secret"
    assert kwargs["webhook_url"] == (
        "https://preloop.test/api/v1/private/webhooks/bitbucket/org-1"
    )
    assert stats["projects"] == 2
