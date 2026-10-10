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


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "STARTING"])
def test_only_finished_execution_can_be_selected(status: str) -> None:
    account, execution = uuid4(), uuid4()
    flow = SimpleNamespace(id=uuid4(), account_id=account)
    row = SimpleNamespace(flow_id=flow.id, status=status)
    with (
        patch.object(service, "get_session_factory", return_value=MagicMock()),
        patch.object(service.crud_flow_execution, "get", return_value=row),
        patch.object(service.crud_flow, "get", return_value=flow),
    ):
        with pytest.raises(service.ContinuationAdoptionError, match="finished"):
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

    def call(*args: object, **kwargs: object) -> None:
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


@pytest.mark.asyncio
async def test_gitlab_preflight_rejects_non_get_methods() -> None:
    gl = SimpleNamespace(http_get=object(), http_post=object())
    raw = SimpleNamespace(gl=gl, _make_request=AsyncMock(return_value={"id": 474}))
    client = service._BoundedReadClient(raw)
    assert await client._make_request(
        gl.http_get, "/projects/1/merge_requests/474"
    ) == {"id": 474}
    with pytest.raises(service.ContinuationAdoptionError, match="reads only"):
        await client._make_request(gl.http_post, "/projects/1/merge_requests/474/notes")
    raw._make_request.assert_awaited_once_with(
        gl.http_get, "/projects/1/merge_requests/474"
    )


def test_metadata_only_workspace_still_allows_native_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean checkpoint has no ciphertext and can still be resumed."""
    from datetime import UTC, datetime, timedelta

    account, execution_id, tracker_id, flow_id = uuid4(), uuid4(), uuid4(), uuid4()
    native_id = uuid4()
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    manifest_sha = "c" * 64
    flow = SimpleNamespace(
        id=flow_id,
        account_id=account,
        is_enabled=True,
        agent_config={"feedback": {"enabled": True}},
        trigger_event_source=str(tracker_id),
    )
    row = SimpleNamespace(
        id=execution_id,
        flow_id=flow_id,
        status="SUCCEEDED",
        result={
            "pr_url": "https://github.com/example/repo/pull/42",
            "pr_source_branch": "fix/41",
        },
        cli_session={
            "agent_type": "opencode",
            "session_id": "ses_ab12cd34",
            "artifact_reference": {
                "artifact_id": str(native_id),
                "execution_id": str(execution_id),
                "manifest_sha256": manifest_sha,
            },
        },
        trigger_event_details={
            "source": "github",
            "tracker_id": str(tracker_id),
            "payload": {"repository": {"id": 123}, "issue": {"number": 41}},
        },
    )
    tracker = SimpleNamespace(
        account_id=account, tracker_type="github", resolved_api_key="synthetic-key"
    )
    workspace = SimpleNamespace(
        id=uuid4(),
        execution_id=execution_id,
        kind="workspace",
        ciphertext=None,
        availability="available",
        expires_at=expires_at,
        manifest={"metadata": {"metadata_only": True}},
    )
    native = SimpleNamespace(
        id=native_id,
        execution_id=execution_id,
        kind="native_session",
        ciphertext=b"session-bytes",
        availability="available",
        expires_at=expires_at,
        manifest_sha256=manifest_sha,
        manifest={},
    )
    monkeypatch.setattr(service, "get_session_factory", lambda: MagicMock())
    monkeypatch.setattr(service.crud_flow_execution, "get", lambda *a, **k: row)
    monkeypatch.setattr(service.crud_flow, "get", lambda *a, **k: flow)
    monkeypatch.setattr(service.crud_tracker, "get", lambda *a, **k: tracker)
    monkeypatch.setattr(service, "feedback_tracker_options", lambda *a: {})
    monkeypatch.setattr(service.crud_flow_feedback, "find", lambda *a, **k: [])
    monkeypatch.setattr(service.flow_artifact, "latest", lambda *a, **k: workspace)
    monkeypatch.setattr(service.flow_artifact, "get", lambda *a, **k: native)

    source = service._load_source(account, execution_id)
    assert source["native_resume_available"] is True
    assert source["native_resume_expires_at"] == expires_at

    workspace.manifest = {"metadata": {}}
    refused = service._load_source(account, execution_id)
    assert refused["native_resume_available"] is False


def test_preview_surfaces_native_checkpoint_expiry() -> None:
    """Preview advertises the real checkpoint window, not a policy lifetime."""
    from datetime import UTC, datetime

    expiry = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    source = {
        "execution_id": uuid4(),
        "flow_id": uuid4(),
        "pr_url": "https://github.com/example/repo/pull/474",
        "branch": "fix/474",
        "provider": "github",
        "repository_id": "123",
        "number": "474",
        "feedback_enabled": True,
        "policy": {"max_age_hours": 168},
        "native_resume_available": True,
        "native_resume_expires_at": expiry,
        "existing_thread_id": None,
        "existing_thread_state": None,
        "tracker_id": uuid4(),
        "tracker_key": "synthetic-key",
        "tracker_options": {},
    }
    publication = {
        "open": True,
        "same_repository": True,
        "branch": "fix/474",
        "head_sha": "a" * 40,
        "pr_url": "https://github.com/example/repo/pull/474",
        "feedback_readable": True,
    }
    with (
        patch.object(service, "_load_source", return_value=source),
        patch.object(
            service, "_bounded_publication", AsyncMock(return_value=publication)
        ),
    ):
        result = service.preview_continuation(uuid4(), uuid4())
    assert result.native_resume_available is True
    assert result.native_resume_expires_at == expiry
    assert result.allowed_recovery_modes == ["native_resume"]
    # An unavailable native checkpoint advertises neither a mode nor a window.
    with (
        patch.object(
            service,
            "_load_source",
            return_value={
                **source,
                "native_resume_available": False,
                "native_resume_expires_at": None,
            },
        ),
        patch.object(
            service, "_bounded_publication", AsyncMock(return_value=publication)
        ),
    ):
        result = service.preview_continuation(uuid4(), uuid4())
    assert result.native_resume_available is False
    assert result.native_resume_expires_at is None
    assert result.allowed_recovery_modes == ["published_branch_handoff"]


@pytest.fixture
def lost_publication(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """An owned failed execution whose local runner never uploaded its result."""
    account, execution_id, tracker_id, flow_id = uuid4(), uuid4(), uuid4(), uuid4()
    flow = SimpleNamespace(
        id=flow_id,
        account_id=account,
        is_enabled=True,
        agent_config={"feedback": {"enabled": True, "trusted_reviewer_ids": [42]}},
        trigger_event_source=str(tracker_id),
    )
    row = SimpleNamespace(
        id=execution_id,
        flow_id=flow_id,
        status="FAILED",
        result=None,
        cli_session=None,
        trigger_event_details={
            "source": "github",
            "tracker_id": str(tracker_id),
            "payload": {"repository": {"id": 123}, "issue": {"number": 41}},
        },
    )
    tracker = SimpleNamespace(
        account_id=account, tracker_type="github", resolved_api_key="synthetic-key"
    )
    factory = MagicMock()
    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service.crud_flow_execution, "get", lambda *args, **kwargs: row)
    monkeypatch.setattr(
        service.crud_flow_execution,
        "lock_for_runner_completion",
        lambda *args, **kwargs: row,
    )
    monkeypatch.setattr(service.crud_flow, "get", lambda *args, **kwargs: flow)
    monkeypatch.setattr(service.crud_tracker, "get", lambda *args, **kwargs: tracker)
    monkeypatch.setattr(service, "feedback_tracker_options", lambda *args: {})
    monkeypatch.setattr(service.flow_artifact, "latest", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.crud_flow_feedback, "find", lambda *args, **kwargs: [])
    monkeypatch.setattr(service.settings, "flow_artifact_direct_upload", True)
    publication = {
        "open": True,
        "same_repository": True,
        "branch": "fix/41",
        "head_sha": "a" * 40,
        "pr_url": "https://github.com/example/repo/pull/42",
        "feedback_readable": True,
    }
    monkeypatch.setattr(
        service, "_bounded_publication", AsyncMock(return_value=publication)
    )
    return account, row, flow, publication, factory


@pytest.mark.parametrize(
    "status", ["SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED", "CANCELLED", "ABORTED"]
)
def test_missing_publication_requires_selected_pr_and_honest_fresh_recovery(
    lost_publication: tuple, status: str
) -> None:
    account, row, _, publication, factory = lost_publication
    row.status = status
    with pytest.raises(
        service.ContinuationAdoptionError, match="Provide its published PR"
    ):
        service.preview_continuation(account, row.id)
    readiness = service.preview_continuation(
        account, row.id, pr_url=publication["pr_url"], branch="fix/41"
    )
    assert readiness.allowed_recovery_modes == ["published_branch_handoff"]
    assert not readiness.native_resume_available
    assert row.result is None  # Preview never writes a binding or starts a thread.
    factory().commit.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", False),
        ("same_repository", False),
        ("branch", "other"),
        ("pr_url", "https://github.com/other/repo/pull/42"),
    ],
)
def test_selected_pr_provider_binding_must_match(
    lost_publication: tuple, field: str, value: object
) -> None:
    account, row, _, publication, _ = lost_publication
    selected = publication["pr_url"]
    publication[field] = value
    with pytest.raises(service.ContinuationAdoptionError, match="binding changed"):
        service.preview_continuation(account, row.id, pr_url=selected, branch="fix/41")


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr_url", "https://github.com/example/repo/pull/99"),
        ("pr_source_branch", "other"),
    ],
)
def test_selected_pr_cannot_override_recorded_binding(
    lost_publication: tuple, field: str, value: str
) -> None:
    account, row, _, publication, _ = lost_publication
    row.result = {field: value}
    with pytest.raises(service.ContinuationAdoptionError, match="conflicts"):
        service.preview_continuation(
            account, row.id, pr_url=publication["pr_url"], branch="fix/41"
        )
    service._bounded_publication.assert_not_called()


def test_old_flow_upgrade_is_explicit_and_does_not_create_backlog_thread(
    lost_publication: tuple,
) -> None:
    account, row, flow, publication, _ = lost_publication
    flow.agent_config = {"execution_path": "ephemeral"}
    readiness = service.preview_continuation(
        account, row.id, pr_url=publication["pr_url"], branch="fix/41"
    )
    assert not readiness.feedback_enabled
    assert row.result is None
    with pytest.raises(service.ContinuationAdoptionError, match="must be enabled"):
        service.adopt_continuation(
            account,
            row.id,
            ContinuationAdoptRequest(
                pr_url=publication["pr_url"],
                branch="fix/41",
                expected_head_sha="a" * 40,
                recovery_mode="published_branch_handoff",
                acknowledge_fresh_conversation=True,
            ),
        )


def test_selected_pr_adoption_rechecks_head_and_requires_fresh_ack(
    lost_publication: tuple,
) -> None:
    account, row, _, publication, _ = lost_publication
    base = dict(
        pr_url=publication["pr_url"],
        branch="fix/41",
        expected_head_sha="a" * 40,
        recovery_mode="published_branch_handoff",
    )
    with pytest.raises(service.ContinuationAdoptionError, match="Acknowledge"):
        service.adopt_continuation(account, row.id, ContinuationAdoptRequest(**base))
    with pytest.raises(service.ContinuationAdoptionError, match="head changed"):
        service.adopt_continuation(
            account,
            row.id,
            ContinuationAdoptRequest(
                **{**base, "expected_head_sha": "b" * 40},
                acknowledge_fresh_conversation=True,
            ),
        )


def test_selected_pr_adoption_atomically_binds_without_changing_failed_status(
    lost_publication: tuple,
) -> None:
    account, row, _, publication, factory = lost_publication
    request = ContinuationAdoptRequest(
        pr_url=publication["pr_url"],
        branch="fix/41",
        expected_head_sha="a" * 40,
        recovery_mode="published_branch_handoff",
        acknowledge_fresh_conversation=True,
    )
    thread = SimpleNamespace(
        id=uuid4(),
        state="waiting",
        pr_url=publication["pr_url"],
        context={
            "adoption": {
                "source_execution_id": str(row.id),
                "recovery_mode": "published_branch_handoff",
            }
        },
    )
    with (
        patch.object(
            service.crud_flow_execution, "bind_publication", return_value=row
        ) as bind,
        patch.object(service, "register_thread", return_value=thread) as register,
    ):
        response = service.adopt_continuation(account, row.id, request)
    assert response.thread_id == thread.id
    assert bind.call_args.kwargs["commit"] is False
    assert register.call_args.kwargs["commit"] is False
    factory().__enter__().commit.assert_called_once()
    assert row.status == "FAILED" and row.cli_session is None


def test_adoption_conflicting_existing_thread_does_not_commit(
    lost_publication: tuple,
) -> None:
    account, row, _, publication, factory = lost_publication
    request = ContinuationAdoptRequest(
        pr_url=publication["pr_url"],
        branch="fix/41",
        expected_head_sha="a" * 40,
        recovery_mode="published_branch_handoff",
        acknowledge_fresh_conversation=True,
    )
    thread = SimpleNamespace(
        context={
            "adoption": {
                "source_execution_id": str(uuid4()),
                "recovery_mode": "published_branch_handoff",
            }
        }
    )
    with (
        patch.object(service.crud_flow_execution, "bind_publication", return_value=row),
        patch.object(service, "register_thread", return_value=thread),
    ):
        with pytest.raises(service.ContinuationAdoptionError, match="already has"):
            service.adopt_continuation(account, row.id, request)
    factory().__enter__().commit.assert_not_called()


@pytest.mark.parametrize("suffix", ["pull/42/", "issues/42/"])
def test_selected_pr_url_is_canonical_before_preview_and_adoption(
    lost_publication: tuple, suffix: str
) -> None:
    account, row, _, publication, factory = lost_publication
    selected = "https://github.com/example/repo/" + suffix
    readiness = service.preview_continuation(
        account, row.id, pr_url=selected, branch="fix/41"
    )
    assert readiness.pr_url == publication["pr_url"]
    request = ContinuationAdoptRequest(
        pr_url=selected,
        branch="fix/41",
        expected_head_sha="a" * 40,
        recovery_mode="published_branch_handoff",
        acknowledge_fresh_conversation=True,
    )
    thread = SimpleNamespace(
        id=uuid4(),
        state="waiting",
        pr_url=publication["pr_url"],
        context={
            "adoption": {
                "source_execution_id": str(row.id),
                "recovery_mode": "published_branch_handoff",
            }
        },
    )
    with (
        patch.object(
            service.crud_flow_execution, "bind_publication", return_value=row
        ) as bind,
        patch.object(service, "register_thread", return_value=thread),
    ):
        service.adopt_continuation(account, row.id, request)
    assert bind.call_args.kwargs["pr_url"] == publication["pr_url"]


@pytest.mark.asyncio
async def test_bitbucket_read_publication_matches_repository_uuid() -> None:
    """The Bitbucket read validates both sides against the bound repo UUID."""
    repo_uuid = "22222222-2222-2222-2222-222222222222"
    pr = {
        "state": "OPEN",
        "source": {
            "branch": {"name": "feat/x"},
            "commit": {"hash": "head"},
            "repository": {"uuid": "{" + repo_uuid + "}", "full_name": "ws/repo"},
        },
        "destination": {
            "branch": {"name": "main"},
            "repository": {"uuid": "{" + repo_uuid + "}", "full_name": "ws/repo"},
        },
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
    }
    raw = SimpleNamespace(
        _request=AsyncMock(return_value=SimpleNamespace(json=lambda: pr))
    )
    source = {
        "provider": "bitbucket",
        "repository_id": f"ws/{repo_uuid}",
        "number": "7",
        "tracker_id": uuid4(),
        "tracker_key": "k",
        "tracker_options": {},
        "policy": {},
    }
    with (
        patch.object(service, "create_tracker_client", AsyncMock(return_value=raw)),
        patch.object(
            service.FeedbackProvider,
            "read",
            AsyncMock(return_value=FeedbackState("head")),
        ),
    ):
        result = await service._read_publication(source)
    assert result["open"] is True
    assert result["same_repository"] is True
    assert result["branch"] == "feat/x"
    assert result["pr_url"] == "https://bitbucket.org/ws/repo/pull-requests/7"
    path = raw._request.await_args_list[0].args[1]
    assert path == "repositories/ws/%7B" + repo_uuid + "%7D/pullrequests/7"


@pytest.mark.asyncio
async def test_bitbucket_read_publication_rejects_fork_source() -> None:
    """A PR whose source lives in another repository is not the publication."""
    repo_uuid = "22222222-2222-2222-2222-222222222222"
    pr = {
        "state": "OPEN",
        "source": {
            "branch": {"name": "feat/x"},
            "commit": {"hash": "head"},
            "repository": {"uuid": "{99999999-9999-9999-9999-999999999999}"},
        },
        "destination": {
            "branch": {"name": "main"},
            "repository": {"uuid": "{" + repo_uuid + "}"},
        },
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
    }
    raw = SimpleNamespace(
        _request=AsyncMock(return_value=SimpleNamespace(json=lambda: pr))
    )
    source = {
        "provider": "bitbucket",
        "repository_id": f"ws/{repo_uuid}",
        "number": "7",
        "tracker_id": uuid4(),
        "tracker_key": "k",
        "tracker_options": {},
        "policy": {},
    }
    with (
        patch.object(service, "create_tracker_client", AsyncMock(return_value=raw)),
        patch.object(
            service.FeedbackProvider,
            "read",
            AsyncMock(return_value=FeedbackState("head")),
        ),
    ):
        result = await service._read_publication(source)
    assert result["same_repository"] is False


def test_jira_triggered_source_uses_the_bound_repository() -> None:
    """Review on #1434: a Jira-triggered execution published to its bound
    Bitbucket repository; adoption must key on that binding instead of
    refusing with "no valid provider PR binding"."""
    account, execution = uuid4(), uuid4()
    bitbucket_tracker = uuid4()
    flow = SimpleNamespace(
        id=uuid4(), account_id=account, trigger_event_source=str(uuid4())
    )
    row = SimpleNamespace(
        flow_id=flow.id,
        status="SUCCEEDED",
        trigger_event_details={
            "source": "jira",
            "tracker_id": str(uuid4()),
            "payload": {"issue": {"key": "JMR-4"}},
        },
        result={
            "pr_url": "https://bitbucket.org/ws/repo/pull-requests/5",
            "pr_source_branch": "preloop/issue-JMR-4-11386163",
        },
    )
    bound = (
        "bitbucket",
        str(bitbucket_tracker),
        {"full_name": "ws/repo", "uuid": "22222222-2222-2222-2222-222222222222"},
    )
    with (
        patch.object(service, "get_session_factory", return_value=MagicMock()),
        patch.object(service.crud_flow_execution, "get", return_value=row),
        patch.object(service.crud_flow, "get", return_value=flow),
        patch.object(service, "bound_repository", return_value=bound) as resolver,
        patch.object(service.crud_tracker, "get", return_value=None) as tracker_get,
    ):
        with pytest.raises(service.ContinuationAdoptionError) as error:
            service._load_source(account, execution)
    # Past the provider check: the bound Bitbucket tracker was looked up.
    assert error.value.status_code == 404
    assert resolver.called
    assert tracker_get.call_args.kwargs["id"] == bitbucket_tracker
