"""Tracker endpoint validation and service hook setup for Azure DevOps."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import azure_devops_webhooks as hooks
from preloop.api.endpoints import trackers
from preloop.models.schemas.tracker import TrackerTypeSchema
from preloop.schemas.tracker import TrackerTestRequest


def request(**details: object) -> TrackerTestRequest:
    return TrackerTestRequest(
        tracker_type="azure_devops",
        api_key="pat",
        url="https://dev.azure.com",
        connection_details={"organization": "fabrikam", **details},
    )


def test_test_request_is_validated_and_pinned() -> None:
    data = request(project="Fabrikam")
    auth = trackers._bitbucket_auth_details(
        trackers.TrackerType.AZURE_DEVOPS, data, None
    )
    assert auth == {"auth_type": "api_token"}
    assert data.connection_details == {
        "organization": "fabrikam",
        "project": "Fabrikam",
    }
    assert TrackerTypeSchema("azure_devops") is TrackerTypeSchema.AZURE_DEVOPS


@pytest.mark.parametrize(
    "url", ["https://fabrikam.visualstudio.com", "https://tfs.example.com"]
)
def test_unsupported_hosts_are_refused(url: str) -> None:
    data = request()
    data.url = url
    with pytest.raises(HTTPException) as exc:
        trackers._bitbucket_auth_details(trackers.TrackerType.AZURE_DEVOPS, data, None)
    assert exc.value.status_code == 400


def _tracker(secret: str | None = "s") -> SimpleNamespace:
    return SimpleNamespace(
        id="t1",
        tracker_type="azure_devops",
        resolved_api_key="pat",
        resolved_webhook_secret=secret,
        webhook_secret_id="ref" if secret else None,
        connection_details={"organization": "fabrikam", "project": "Fabrikam"},
    )


def test_status_reports_callback_and_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")
    status = hooks._status(_tracker())
    assert status["callback_url"] == (
        "https://preloop.example.com/api/v1/private/webhooks/azure_devops/t1"
    )
    assert status["header_name"] == "X-Preloop-Webhook-Secret"
    assert status["signature"] == "configured"
    assert hooks._status(_tracker(None))["signature"] == "missing_secret"


def test_register_generates_missing_secret_and_maps_permission_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.sync.exceptions import TrackerPermissionError

    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")
    tracker = _tracker(None)
    fn = hooks.register_azure_devops_webhook.__wrapped__
    with (
        patch.object(hooks, "_tracker", return_value=tracker),
        patch.object(hooks, "ensure_permission_in_oss"),
        patch.object(hooks, "crud_tracker") as crud,
        patch.object(
            hooks.AzureDevOpsTracker,
            "ensure_service_hooks",
            new_callable=AsyncMock,
            return_value={"ids": ["a", "b"], "created": 2},
        ) as ensure,
    ):
        result = fn("t1", None, current_user=MagicMock(), db=MagicMock())
        assert result["registration"]["status"] == "registered"
        secret = crud.update.call_args.kwargs["obj_in"]["jira_webhook_secret"]
        assert ensure.await_args.args[1] == secret
        ensure.side_effect = TrackerPermissionError("no", status_code=403)
        with pytest.raises(HTTPException) as exc:
            fn("t1", None, current_user=MagicMock(), db=MagicMock())
        assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "full_name,allowed",
    [
        ("fabrikam/Fabrikam/web", True),
        ("FABRIKAM/fabrikam/WEB", True),
        ("fabrikam/Other/web", False),
        ("fabrikam/Fabrikam/api", False),
        ("contoso/Fabrikam/web", False),
        ("fabrikam/web", False),
    ],
)
def test_tracker_client_enforces_the_binding(full_name: str, allowed: bool) -> None:
    from preloop.api.common import azure_devops_repo_full_name

    config = {"organization": "fabrikam", "project": "Fabrikam", "repository": "web"}
    project = SimpleNamespace(meta_data={"full_name": full_name}, slug=None)
    if allowed:
        assert azure_devops_repo_full_name(config, project) == full_name
    else:
        with pytest.raises(HTTPException) as exc:
            azure_devops_repo_full_name(config, project)
        assert exc.value.status_code == 403
