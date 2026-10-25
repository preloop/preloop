"""Pasted Jira tokens: cloud id persistence and manual webhook instructions.

These tests call the endpoint helpers and the unwrapped tracker handlers.
They do not need a migrated database, and they still cover the values the
tracker page renders.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.trackers import (
    _jira_cloud_id_from_client,
    _jira_manual_webhook,
    _merge_jira_token_details,
    get_tracker,
    register_tracker,
    update_tracker,
)
from preloop.schemas.tracker import TrackerResponse, TrackerUpdate
from preloop.sync.trackers.jira import pasted_gateway_site_detail

CLOUD_ID = "11223344-aaaa-bbbb-cccc-556677889900"
TOKEN = "scoped-token-value"


def test_cloud_id_from_client_accepts_only_a_string():
    assert _jira_cloud_id_from_client(SimpleNamespace(cloud_id=CLOUD_ID)) == CLOUD_ID
    assert _jira_cloud_id_from_client(SimpleNamespace(cloud_id="  ")) is None
    assert _jira_cloud_id_from_client(SimpleNamespace()) is None
    # An async test double grows attributes on access. Those must not be stored.
    assert _jira_cloud_id_from_client(AsyncMock()) is None
    assert _jira_cloud_id_from_client(MagicMock()) is None


def test_username_edit_keeps_scoped_binding_and_a_new_site_drops_it():
    previous = {
        "username": "ada@example.com",
        "cloud_id": CLOUD_ID,
        "webhook_status": "manual_required",
        "manual_webhook": {"organization_id": "org-1"},
        "api_key": TOKEN,
    }
    kept = _merge_jira_token_details(
        previous,
        {"username": "edited@example.com"},
        reset_binding=False,
    )
    assert kept["username"] == "edited@example.com"
    assert kept["cloud_id"] == CLOUD_ID
    assert kept["webhook_status"] == "manual_required"
    assert kept["manual_webhook"]["organization_id"] == "org-1"
    assert "api_key" not in kept
    assert TOKEN not in str(kept)

    reset = _merge_jira_token_details(
        previous,
        {"username": "edited@example.com"},
        reset_binding=True,
    )
    assert reset == {"username": "edited@example.com"}


def test_manual_webhook_uses_the_organization_id_and_secret(monkeypatch):
    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")
    org_id = uuid4()
    org = SimpleNamespace(id=org_id, webhook_secret="org-webhook-secret")
    tracker = SimpleNamespace(
        id=uuid4(),
        tracker_type="jira",
        connection_details={
            "username": "ada@example.com",
            "cloud_id": CLOUD_ID,
            "webhook_status": "manual_required",
            "manual_webhook": {
                "organization_id": str(org_id),
                "events": [
                    "jira:issue_created",
                    "jira:issue_updated",
                    "comment_created",
                    "comment_updated",
                ],
            },
        },
    )
    with patch("preloop.api.endpoints.trackers.crud_organization") as crud:
        crud.get_for_tracker.return_value = [org]
        manual = _jira_manual_webhook(MagicMock(), tracker, include_secret=True)

    assert manual is not None
    assert manual.organization_id == str(org_id)
    assert (
        manual.url
        == f"https://preloop.example.com/api/v1/private/webhooks/jira/{org_id}"
    )
    assert manual.secret == "org-webhook-secret"
    assert "comment_created" in manual.events
    assert "comment_updated" in manual.events
    assert TOKEN not in manual.url
    assert TOKEN not in manual.secret


def test_manual_webhook_is_absent_unless_registration_is_impossible():
    tracker = SimpleNamespace(
        id=uuid4(),
        tracker_type="jira",
        connection_details={"username": "ada@example.com", "cloud_id": CLOUD_ID},
    )
    assert _jira_manual_webhook(MagicMock(), tracker, include_secret=True) is None


def test_viewers_do_not_receive_the_organization_webhook_secret(monkeypatch):
    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")
    org_id = uuid4()
    org = SimpleNamespace(id=org_id, webhook_secret="org-webhook-secret")
    tracker = SimpleNamespace(
        id=uuid4(),
        tracker_type="jira",
        connection_details={
            "webhook_status": "manual_required",
            "manual_webhook": {"organization_id": str(org_id)},
        },
    )
    with patch("preloop.api.endpoints.trackers.crud_organization") as crud:
        crud.get_for_tracker.return_value = [org]
        manual = _jira_manual_webhook(MagicMock(), tracker, include_secret=False)

    assert manual is not None
    assert manual.secret is None
    assert manual.organization_id == str(org_id)
    assert str(org_id) in manual.url
    assert "org-webhook-secret" not in str(manual.model_dump())


def test_get_tracker_includes_the_secret_only_for_manage_trackers():
    account_id = uuid4()
    tracker_id = uuid4()
    account = SimpleNamespace(id=account_id)
    tracker = SimpleNamespace(id=tracker_id)
    user = SimpleNamespace(id=uuid4(), account_id=account_id, username="ada")
    payload = SimpleNamespace(jira_manual_webhook=None)

    def run(can_manage: bool) -> bool:
        with (
            patch("preloop.api.endpoints.trackers.crud_account") as accounts,
            patch("preloop.api.endpoints.trackers.crud_tracker") as trackers,
            patch("preloop.api.endpoints.trackers.has_permission") as allowed,
            patch("preloop.api.endpoints.trackers._jira_manual_webhook") as manual,
            patch.object(TrackerResponse, "model_validate", return_value=payload),
        ):
            accounts.get.return_value = account
            trackers.get_by_id_and_account.return_value = tracker
            allowed.return_value = can_manage
            manual.return_value = SimpleNamespace(secret="hidden")
            inspect.unwrap(get_tracker)(tracker_id, current_user=user, db=MagicMock())
            allowed.assert_called_once_with(
                user, "manage_trackers", allowed.call_args.args[2]
            )
            return bool(manual.call_args.kwargs["include_secret"])

    assert run(True) is True
    assert run(False) is False


@pytest.mark.asyncio
async def test_register_tracker_rejects_a_pasted_gateway_url():
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "name": "Jira",
            "type": "jira",
            "url": f"https://api.atlassian.com/ex/jira/{CLOUD_ID}",
            "api_key": TOKEN,
            "connection_details": {"username": "ada@example.com"},
        }
    )
    with pytest.raises(HTTPException) as caught:
        await inspect.unwrap(register_tracker)(
            request,
            MagicMock(),
            current_user=SimpleNamespace(account_id=uuid4(), is_superuser=True),
            db=MagicMock(),
        )
    assert caught.value.status_code == 400
    assert caught.value.detail == pasted_gateway_site_detail(
        f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"
    )


@pytest.mark.asyncio
async def test_update_tracker_rejects_a_pasted_gateway_url():
    account_id = uuid4()
    tracker_id = uuid4()
    tracker = SimpleNamespace(
        id=tracker_id,
        tracker_type="jira",
        auth_type="api_token",
        url="https://example-site.atlassian.net",
        connection_details={"username": "ada@example.com"},
        resolved_api_key=TOKEN,
    )
    user = SimpleNamespace(id=uuid4(), account_id=account_id, username="ada")
    with (
        patch("preloop.api.endpoints.trackers.crud_account") as accounts,
        patch("preloop.api.endpoints.trackers.crud_tracker") as trackers,
        patch("preloop.api.endpoints.trackers.is_managed_tracker", return_value=False),
    ):
        accounts.get.return_value = SimpleNamespace(id=account_id)
        trackers.get_by_id_and_account.return_value = tracker
        with pytest.raises(HTTPException) as caught:
            await inspect.unwrap(update_tracker)(
                tracker_id,
                TrackerUpdate(url=f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"),
                current_user=user,
                db=MagicMock(),
            )
    assert caught.value.status_code == 400
    trackers.update.assert_not_called()
