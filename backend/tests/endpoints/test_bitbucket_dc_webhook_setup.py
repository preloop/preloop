"""Bitbucket Data Center webhook setup endpoints (inspection, rotation, registration)."""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import bitbucket_dc_webhooks as setup
from preloop.sync.exceptions import TrackerPermissionError
from preloop.utils import bitbucket_dc as dc
from preloop.utils.bitbucket_dc_webhooks import BITBUCKET_DC_WEBHOOK_EVENTS

pytestmark = pytest.mark.asyncio

INSTANCE = "https://bitbucket.example.com/bitbucket"
TRACKER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
USER = SimpleNamespace(account_id=uuid.uuid4(), username="tester")

status_endpoint = inspect.unwrap(setup.get_bitbucket_dc_webhook_status)
rotate_endpoint = inspect.unwrap(setup.rotate_bitbucket_dc_webhook_secret)
register_endpoint = inspect.unwrap(setup.register_bitbucket_dc_webhook)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv(dc.ENV_ENABLED, "true")
    monkeypatch.setenv(dc.ENV_INSTANCES, json.dumps([INSTANCE]))
    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")


def tracker(**overrides) -> MagicMock:
    row = MagicMock()
    row.id = TRACKER_ID
    row.tracker_type = "bitbucket_dc"
    row.auth_type = "api_token"
    row.url = INSTANCE
    row.webhook_secret_id = uuid.uuid4()
    row.resolved_webhook_secret = "current-secret"
    row.resolved_api_key = "pat"
    row.connection_details = {
        "instance_url": INSTANCE,
        "project_key": "PRJ",
        "repository_slug": "my-repo",
        "repository_id": 42,
    }
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


CALLBACK = (
    f"https://preloop.example.com/api/v1/private/webhooks/bitbucket_dc/{TRACKER_ID}"
)


async def test_status_reports_signature_and_callback_separately() -> None:
    with patch.object(setup, "crud_tracker") as crud:
        crud.get_by_id_and_account.return_value = tracker(webhook_secret_id=None)
        result = await status_endpoint(
            tracker_id=TRACKER_ID, check=False, current_user=USER, db=MagicMock()
        )
    assert result["callback_url"] == CALLBACK
    assert result["signature"] == "missing_secret"
    assert result["required_events"] == list(BITBUCKET_DC_WEBHOOK_EVENTS)
    assert result["registration"] is None


async def test_status_check_surfaces_permission_denied() -> None:
    client = MagicMock(repo_full_name="PRJ/my-repo")
    client.inspect_repository_webhook = AsyncMock(
        return_value={"status": "permission_denied", "missing_events": []}
    )
    with (
        patch.object(setup, "crud_tracker") as crud,
        patch.object(setup, "create_tracker_client", AsyncMock(return_value=client)),
    ):
        crud.get_by_id_and_account.return_value = tracker()
        result = await status_endpoint(
            tracker_id=TRACKER_ID, check=True, current_user=USER, db=MagicMock()
        )
    assert result["signature"] == "configured"
    assert result["registration"]["status"] == "permission_denied"


async def test_other_tracker_types_and_disabled_flag_are_not_found(monkeypatch) -> None:
    with patch.object(setup, "crud_tracker") as crud:
        crud.get_by_id_and_account.return_value = tracker(tracker_type="bitbucket")
        with pytest.raises(HTTPException) as exc:
            await status_endpoint(
                tracker_id=TRACKER_ID, check=False, current_user=USER, db=MagicMock()
            )
        assert exc.value.status_code == 404
        monkeypatch.setenv(dc.ENV_ENABLED, "false")
        crud.get_by_id_and_account.return_value = tracker()
        with pytest.raises(HTTPException) as exc:
            await status_endpoint(
                tracker_id=TRACKER_ID, check=False, current_user=USER, db=MagicMock()
            )
        assert exc.value.status_code == 404


async def test_rotation_stores_a_new_encrypted_secret_and_returns_it_once() -> None:
    row = tracker()
    with patch.object(setup, "crud_tracker") as crud:
        crud.get_by_id_and_account.return_value = row
        first = await rotate_endpoint(
            tracker_id=TRACKER_ID, current_user=USER, db=MagicMock()
        )
        second = await rotate_endpoint(
            tracker_id=TRACKER_ID, current_user=USER, db=MagicMock()
        )
    assert first["secret"] != second["secret"]
    stored = [
        c.kwargs["obj_in"]["jira_webhook_secret"] for c in crud.update.call_args_list
    ]
    assert stored == [first["secret"], second["secret"]]


async def test_registration_permission_error_gives_admin_instructions() -> None:
    client = MagicMock()
    client.ensure_repository_webhook = AsyncMock(
        side_effect=TrackerPermissionError("denied", status_code=403)
    )
    with (
        patch.object(setup, "crud_tracker") as crud,
        patch.object(setup, "create_tracker_client", AsyncMock(return_value=client)),
    ):
        crud.get_by_id_and_account.return_value = tracker()
        with pytest.raises(HTTPException) as exc:
            await register_endpoint(
                tracker_id=TRACKER_ID, body=None, current_user=USER, db=MagicMock()
            )
    assert exc.value.status_code == 403
    assert "repository administrator" in exc.value.detail
    assert "current-secret" not in exc.value.detail


async def test_registration_is_repeatable() -> None:
    client = MagicMock()
    client.ensure_repository_webhook = AsyncMock(
        side_effect=[
            {"id": 100, "created": True, "updated": 0},
            {"id": 100, "created": False, "updated": 1},
        ]
    )
    with (
        patch.object(setup, "crud_tracker") as crud,
        patch.object(setup, "create_tracker_client", AsyncMock(return_value=client)),
    ):
        crud.get_by_id_and_account.return_value = tracker()
        first = await register_endpoint(
            tracker_id=TRACKER_ID, body=None, current_user=USER, db=MagicMock()
        )
        second = await register_endpoint(
            tracker_id=TRACKER_ID, body=None, current_user=USER, db=MagicMock()
        )
    assert first["registration"]["id"] == second["registration"]["id"] == 100
    assert client.ensure_repository_webhook.await_args.args == (
        CALLBACK,
        "current-secret",
    )


@pytest.mark.parametrize(
    ("overrides", "env_unset"),
    [
        ({"auth_type": "managed_oauth"}, False),
        ({"resolved_webhook_secret": None}, False),
        ({}, True),
    ],
)
async def test_registration_preconditions(monkeypatch, overrides, env_unset) -> None:
    if env_unset:
        monkeypatch.delenv("PRELOOP_URL")
    factory = AsyncMock()
    with (
        patch.object(setup, "crud_tracker") as crud,
        patch.object(setup, "create_tracker_client", factory),
    ):
        crud.get_by_id_and_account.return_value = tracker(**overrides)
        with pytest.raises(HTTPException) as exc:
            await register_endpoint(
                tracker_id=TRACKER_ID, body=None, current_user=USER, db=MagicMock()
            )
    assert exc.value.status_code == 409
    factory.assert_not_awaited()
