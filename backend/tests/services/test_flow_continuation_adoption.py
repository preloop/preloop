"""Selected-publication adoption guards and bounded read-only preflight."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.schemas.flow_continuation import (
    ContinuationAdoptRequest,
    ContinuationPreview,
)
from preloop.services import flow_continuation_adoption as service
from preloop.services.flow_feedback_provider import FeedbackState


def preview(**changes: object) -> ContinuationPreview:
    return ContinuationPreview(
        **{
            "execution_id": uuid4(),
            "flow_id": uuid4(),
            "pr_url": "https://github.com/a/b/pull/474",
            "branch": "fix/474",
            "head_sha": "a" * 40,
            "feedback_enabled": True,
            "artifact_upload_enabled": True,
            "feedback_readable": True,
            "native_resume_available": False,
            "allowed_recovery_modes": ["published_branch_handoff"],
            "warnings": [],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes,body_changes,reason",
    [
        ({}, {"expected_head_sha": "b" * 40}, "head changed"),
        ({"feedback_enabled": False}, {}, "must be enabled"),
        ({"artifact_upload_enabled": False}, {}, "must be enabled"),
        ({"feedback_readable": False}, {}, "cannot read"),
        ({}, {"recovery_mode": "native_resume"}, "unavailable"),
        ({}, {"acknowledge_fresh_conversation": False}, "Acknowledge"),
    ],
)
def test_adoption_preconditions_do_not_open_write_session(
    changes: dict, body_changes: dict, reason: str
) -> None:
    readiness = preview(**changes)
    body = ContinuationAdoptRequest(
        **{
            "expected_head_sha": "a" * 40,
            "recovery_mode": "published_branch_handoff",
            "acknowledge_fresh_conversation": True,
            **body_changes,
        }
    )
    with (
        patch.object(service, "preview_continuation", return_value=readiness),
        patch.object(service, "get_session_factory") as factory,
    ):
        with pytest.raises(service.ContinuationAdoptionError, match=reason) as error:
            service.adopt_continuation(uuid4(), readiness.execution_id, body)
        assert error.value.status_code == 409
        factory.assert_not_called()


def test_cross_account_source_is_not_found() -> None:
    account, execution = uuid4(), uuid4()
    factory = MagicMock()
    with (
        patch.object(service, "get_session_factory", return_value=factory),
        patch.object(service.crud_flow_execution, "get", return_value=None) as get,
    ):
        with pytest.raises(service.ContinuationAdoptionError) as error:
            service._load_source(account, execution)
        assert error.value.status_code == 404
        assert get.call_args.kwargs == {"id": execution, "account_id": account}


@pytest.mark.parametrize("status", ["FAILED", "RUNNING", "PENDING", "TIMED_OUT"])
def test_only_successful_publisher_can_be_selected(status: str) -> None:
    account, execution = uuid4(), uuid4()
    flow = SimpleNamespace(id=uuid4(), account_id=account)
    row = SimpleNamespace(flow_id=flow.id, status=status)
    with (
        patch.object(service, "get_session_factory", return_value=MagicMock()),
        patch.object(service.crud_flow_execution, "get", return_value=row),
        patch.object(service.crud_flow, "get", return_value=flow),
    ):
        with pytest.raises(service.ContinuationAdoptionError, match="successful"):
            service._load_source(account, execution)


@pytest.mark.asyncio
async def test_preflight_detects_closed_pr_and_permission_failure() -> None:
    source = {"provider": "github", "repository_id": "1", "number": "474", "policy": {}}
    client = service._BoundedReadClient(MagicMock())
    with patch.object(
        service.FeedbackProvider,
        "read",
        AsyncMock(return_value=FeedbackState("head", closed=True)),
    ):
        result = await service._feedback_preflight(
            client, source, {"open": True, "head_sha": "head"}
        )
        assert result["open"] is False
    with patch.object(
        service.FeedbackProvider, "read", AsyncMock(side_effect=ValueError("secret"))
    ):
        result = await service._feedback_preflight(
            client, source, {"open": True, "head_sha": "head"}
        )
        assert result["feedback_readable"] is False
        assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_preflight_caps_requests_and_disallows_provider_writes() -> None:
    raw = SimpleNamespace(_request=AsyncMock(return_value={}))
    client = service._BoundedReadClient(raw)
    for _ in range(12):
        await client._request("GET", "/pulls/474")
    with pytest.raises(service.ContinuationAdoptionError, match="limit"):
        await client._request("GET", "/pulls/474")
    assert raw._request.await_count == 12
    with pytest.raises(service.ContinuationAdoptionError, match="reads only"):
        await service._BoundedReadClient(raw)._request("POST", "/issues/474/comments")


@pytest.mark.parametrize(
    "route", ["preview_execution_continuation", "adopt_execution_continuation"]
)
def test_endpoints_release_auth_transaction_before_provider_and_sanitize_error(
    route: str,
) -> None:
    from preloop.api.endpoints import flows

    db, account, execution = MagicMock(), uuid4(), uuid4()
    user = SimpleNamespace(account_id=account)
    function = (
        flows.preview_continuation
        if route.startswith("preview")
        else flows.adopt_continuation
    )
    del function
    target = (
        "preview_continuation" if route.startswith("preview") else "adopt_continuation"
    )

    def call(*args: object) -> None:
        db.commit.assert_called_once()
        assert args[:2] == (account, execution)
        raise service.ContinuationAdoptionError("Selected publication changed")

    args = {"db": db, "current_user": user, "execution_id": execution}
    if route.startswith("adopt"):
        args["request"] = ContinuationAdoptRequest(
            recovery_mode="published_branch_handoff", expected_head_sha="a" * 40
        )
    with patch.object(flows, target, side_effect=call):
        with pytest.raises(HTTPException) as error:
            getattr(flows, route)(**args)
        assert error.value.status_code == 409
        assert error.value.detail == "Selected publication changed"


@pytest.mark.parametrize("auth_type", ["github_app", "oauth_app"])
def test_app_tracker_configuration_uses_scoped_external_installation(
    auth_type: str,
) -> None:
    from preloop.services import flow_feedback_provider as provider

    installation_id, account_id = uuid4(), uuid4()
    tracker = SimpleNamespace(
        tracker_type="github",
        auth_type=auth_type,
        oauth_installation_id=installation_id,
        account_id=account_id,
        url="https://api.github.com",
        connection_details={"auth_type": "api_token", "github_installation_id": 999},
    )
    db = MagicMock()
    with patch.object(
        provider.crud_oauth_app_installation,
        "get_by_id_provider_and_account",
        return_value=SimpleNamespace(external_id=123),
    ) as get:
        options = provider.feedback_tracker_options(db, tracker)
    assert options["auth_type"] == auth_type
    assert options["github_installation_id"] == 123
    get.assert_called_once_with(
        db, id=installation_id, provider="github", account_id=account_id
    )
    with patch.object(
        provider.crud_oauth_app_installation,
        "get_by_id_provider_and_account",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="installation unavailable"):
            provider.feedback_tracker_options(db, tracker)


def test_pat_tracker_cannot_inherit_another_installation_from_json() -> None:
    from preloop.services.flow_feedback_provider import feedback_tracker_options

    tracker = SimpleNamespace(
        tracker_type="github",
        auth_type="api_token",
        url="https://api.github.com",
        connection_details={"auth_type": "github_app", "github_installation_id": 999},
    )
    assert feedback_tracker_options(MagicMock(), tracker) == {
        "auth_type": "api_token",
        "url": "https://api.github.com",
    }


def test_missing_app_configuration_is_a_sanitized_precondition() -> None:
    with patch.object(
        service, "_load_source", side_effect=ValueError("private auth details")
    ):
        with pytest.raises(service.ContinuationAdoptionError) as error:
            service.preview_continuation(uuid4(), uuid4())
    assert error.value.status_code == 409
    assert str(error.value) == "Execution tracker configuration is unavailable"
