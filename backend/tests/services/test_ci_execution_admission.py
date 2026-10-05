"""Trusted provider admission and immutable CI dispatch correlation."""

from copy import deepcopy
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import gitlab
import httpx
import pytest
from sqlalchemy.orm import Session

from preloop.models import crud
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.schemas.ci_execution import CiReviewRequest, ci_review_event
from preloop.schemas.ci_principal import CiAction
from preloop.services import ci_execution as service
from preloop.sync.exceptions import TrackerError, TrackerResponseError
from tests.api.test_ci_execution import ci_resources as create_ci_resources
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    """Reuse the synthetic account/project/flow binding fixture."""
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def context() -> CiAuthorizationContext:
    """Build synthetic authority without credentials or external resources."""
    return CiAuthorizationContext(
        account_id=uuid4(),
        principal_id=uuid4(),
        key_id=uuid4(),
        project_id=uuid4(),
        flow_id=uuid4(),
        tracker_id=uuid4(),
        repository_identifier="12345",
        repository_slug="example/repository",
        tracker_type="github",
        tracker_url=None,
        actions=frozenset(CiAction),
    )


def source_for(context: CiAuthorizationContext) -> service._ProviderRead:
    """A provider read contains only accepted input and trusted integration data."""
    return service._ProviderRead(
        context=context,
        request=CiReviewRequest(pr_number=7, head_sha="a" * 40),
        tracker_key="synthetic-provider-secret",
        tracker_options={"synthetic": True},
    )


def provider_payload(
    context: CiAuthorizationContext, *, branch: str = "develop"
) -> dict[str, Any]:
    """Use immutable provider repository ids rather than slug-only admission."""
    if context.tracker_type == "github":
        repository = {
            "id": context.repository_identifier,
            "full_name": context.repository_slug,
        }
        return {
            "id": 7007,
            "number": 7,
            "state": "open",
            "head": {"repo": deepcopy(repository), "sha": "a" * 40},
            "base": {"repo": deepcopy(repository), "ref": branch},
        }
    return {
        "id": 7007,
        "iid": 7,
        "sha": "a" * 40,
        "state": "opened",
        "source_project_id": context.repository_identifier,
        "target_project_id": context.repository_identifier,
        "target_branch": branch,
    }


def mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    *,
    payload: Any = None,
    error: Exception | None = None,
) -> tuple[AsyncMock, Any]:
    """Mock the provider transport, leaving actual admission logic intact."""
    if context.tracker_type == "github":
        client = Mock()
        client._request = AsyncMock(return_value=payload, side_effect=error)
    else:
        client = Mock()
        client.gl.http_get = Mock(return_value=payload, side_effect=error)
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(service, "_create_review_client", factory)
    return factory, client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("branch", ["develop", "master"])
async def test_immutable_provider_routes_preserve_non_main_target(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    provider: str,
    branch: str,
) -> None:
    context = replace(context, tracker_type=provider)
    source = source_for(context)
    factory, client = mock_provider(
        monkeypatch, context, payload=provider_payload(context, branch=branch)
    )
    binding = await service._read_provider(source)
    factory.assert_awaited_once_with(source)
    if provider == "github":
        client._request.assert_awaited_once_with("GET", "/repositories/12345/pulls/7")
        assert (
            ci_review_event(binding)["payload"]["pull_request"]["base"]["ref"] == branch
        )
    else:
        client.gl.http_get.assert_called_once_with("/projects/12345/merge_requests/7")
        assert client.gl.timeout == 20
        assert (
            ci_review_event(binding)["payload"]["object_attributes"]["target_branch"]
            == branch
        )
    assert binding.head_sha == source.request.head_sha
    assert binding.base_branch == branch
    assert source.tracker_key not in repr(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_immutable_identifier_is_one_encoded_route_segment(
    monkeypatch: pytest.MonkeyPatch, context: CiAuthorizationContext, provider: str
) -> None:
    context = replace(
        context, tracker_type=provider, repository_identifier="123/45?other=repo"
    )
    _, client = mock_provider(monkeypatch, context, payload=provider_payload(context))
    await service._read_provider(source_for(context))
    if provider == "github":
        client._request.assert_awaited_once_with(
            "GET", "/repositories/123%2F45%3Fother%3Drepo/pulls/7"
        )
    else:
        client.gl.http_get.assert_called_once_with(
            "/projects/123%2F45%3Fother%3Drepo/merge_requests/7"
        )


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize(
    "mismatch",
    ["repository", "fork", "number", "head", "state", "provider_id", "branch"],
)
def test_provider_mismatch_denies(
    context: CiAuthorizationContext, provider: str, mismatch: str
) -> None:
    context = replace(context, tracker_type=provider)
    payload = provider_payload(context)
    if mismatch == "repository":
        if provider == "github":
            payload["base"]["repo"]["id"] = "different-repository"
        else:
            payload["target_project_id"] = "different-repository"
    elif mismatch == "fork":
        if provider == "github":
            payload["head"]["repo"]["id"] = "different-fork"
        else:
            payload["source_project_id"] = "different-fork"
    elif mismatch == "number":
        payload["number" if provider == "github" else "iid"] = 8
    elif mismatch == "head":
        if provider == "github":
            payload["head"]["sha"] = "b" * 40
        else:
            payload["sha"] = "b" * 40
    elif mismatch == "state":
        payload["state"] = "closed"
    elif mismatch == "provider_id":
        payload["id"] = None
    elif provider == "github":
        payload["base"]["ref"] = "main;synthetic-command"
    else:
        payload["target_branch"] = "main;synthetic-command"
    with pytest.raises(service.CiReviewDeniedError):
        service._accepted_binding(source_for(context), payload)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("provider_id", [True, "", None, {}, []])
def test_malformed_provider_identity_denies(
    context: CiAuthorizationContext, provider: str, provider_id: Any
) -> None:
    context = replace(context, tracker_type=provider)
    payload = provider_payload(context)
    payload["id"] = provider_id
    with pytest.raises(service.CiReviewDeniedError):
        service._accepted_binding(source_for(context), payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected",
    [
        (
            TrackerResponseError("synthetic-provider-secret body", status_code=403),
            service.CiReviewDeniedError,
        ),
        (
            TrackerResponseError("synthetic-provider-secret body", status_code=404),
            service.CiReviewDeniedError,
        ),
        (
            TrackerResponseError("synthetic-provider-secret body", status_code=500),
            service.CiReviewUnavailableError,
        ),
        (
            TrackerError("synthetic-provider-secret body"),
            service.CiReviewUnavailableError,
        ),
        (
            httpx.ConnectError("synthetic-provider-secret body"),
            service.CiReviewUnavailableError,
        ),
        (
            TimeoutError("synthetic-provider-secret body"),
            service.CiReviewUnavailableError,
        ),
    ],
)
async def test_provider_exceptions_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected: type[Exception],
) -> None:
    mock_provider(monkeypatch, context, error=error)
    with pytest.raises(expected) as caught:
        await service._read_provider(source_for(context))
    assert "synthetic-provider-secret" not in str(caught.value)
    assert "synthetic-provider-secret" not in caplog.text
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_gitlab_error_response_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = replace(context, tracker_type="gitlab")
    mock_provider(
        monkeypatch,
        context,
        error=gitlab.exceptions.GitlabGetError(
            "synthetic-provider-secret body", response_code=500
        ),
    )
    with pytest.raises(service.CiReviewUnavailableError) as caught:
        await service._read_provider(source_for(context))
    assert "synthetic-provider-secret" not in str(caught.value)
    assert "synthetic-provider-secret" not in caplog.text


def test_retry_enabled_ci_flow_is_refused(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    """A no-progress retry would launch a second run outside CI ownership."""
    _, _, flow, _ = ci_resources
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    flow.agent_config = {"retry_on_no_progress": {"enabled": True}}
    db_session.commit()
    with pytest.raises(
        service.CiReviewDeniedError,
        match="Restricted CI requires a single review execution",
    ):
        service._review_flow(db_session, context)
    assert crud.crud_ci_execution.count(db_session, context=context) == 0


@pytest.mark.asyncio
async def test_denied_provider_admission_has_no_execution_or_dispatch_side_effect(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context)
    payload["head"]["sha"] = "b" * 40
    mock_provider(monkeypatch, context, payload=payload)
    dispatch = AsyncMock()
    monkeypatch.setattr("preloop.sync.services.event_bus.get_nats_client", dispatch)
    before = crud.crud_ci_execution.count(db_session, context=context)
    with pytest.raises(service.CiReviewDeniedError):
        await service.trigger_ci_review(
            db_session, context=context, request=source_for(context).request
        )
    assert crud.crud_ci_execution.count(db_session, context=context) == before
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["head", "provider_id", "target_branch", "closed"])
async def test_dispatch_rereads_accepted_review_and_denies_changes(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context)
    binding = service._accepted_binding(source_for(context), payload)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=binding, event={}
    )
    if change == "head":
        payload["head"]["sha"] = "b" * 40
    elif change == "provider_id":
        payload["id"] = 8008
    elif change == "target_branch":
        payload["base"]["ref"] = "master"
    else:
        payload["state"] = "closed"
    mock_provider(monkeypatch, context, payload=payload)
    with pytest.raises(service.CiReviewDeniedError):
        await service.ensure_ci_dispatch_admission(db_session, execution=execution)


@pytest.mark.asyncio
async def test_binding_move_during_provider_read_denies_dispatch(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context)
    binding = service._accepted_binding(source_for(context), payload)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=binding, event={}
    )

    async def moved_repository(*args: Any, **kwargs: Any) -> dict[str, Any]:
        crud.crud_project.update(
            db_session, db_obj=ci_resources[1], obj_in={"identifier": "moved"}
        )
        return payload

    _, client = mock_provider(monkeypatch, context, payload=payload)
    client._request.side_effect = moved_repository
    with pytest.raises(PermissionError):
        await service.ensure_ci_dispatch_admission(db_session, execution=execution)


@pytest.mark.asyncio
async def test_revoked_initiating_key_preserves_accepted_dispatch_not_api_use(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context)
    binding = service._accepted_binding(source_for(context), payload)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=binding, event={}
    )
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    mock_provider(monkeypatch, context, payload=payload)
    await service.ensure_ci_dispatch_admission(db_session, execution=execution)
    with pytest.raises(PermissionError):
        service._load_request_source(db_session, context, source_for(context).request)


@pytest.mark.asyncio
async def test_removed_trigger_grant_denies_before_provider_read(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    grant = ci_resources[3].model_copy(update={"actions": (CiAction.READ_EXECUTION,)})
    crud.crud_ci_principal.change(
        db_session, actor=ci_resources[0], principal_id=principal.id, grant=grant
    )
    factory, _ = mock_provider(monkeypatch, context, payload=provider_payload(context))
    with pytest.raises(PermissionError):
        await service.ensure_ci_dispatch_admission(db_session, execution=execution)
    factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["develop", "master"])
async def test_admitted_review_creates_one_owned_execution_and_ignores_remote_controls(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    from preloop.services.flow_trigger_service import FlowTriggerService

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context, branch=branch)
    payload.update(
        {"_feedback_prompt": "synthetic-provider-secret", "runner_pool": "private"}
    )
    payload["head"]["repo"]["clone_url"] = "https://invalid.example/other-repository"
    mock_provider(monkeypatch, context, payload=payload)
    nats = Mock()
    nats_loader = AsyncMock(return_value=nats)
    dispatch = AsyncMock()
    monkeypatch.setattr("preloop.sync.services.event_bus.get_nats_client", nats_loader)
    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", dispatch)
    projection = await service.trigger_ci_review(
        db_session, context=context, request=source_for(context).request
    )
    rows = crud.crud_ci_execution.list(db_session, context=context)
    assert len(rows) == 1
    assert rows[0].ci_principal_id == context.principal_id
    assert rows[0].initiating_ci_key_id == context.key_id
    assert rows[0].ci_review_binding["base_branch"] == branch
    assert projection["id"] == str(rows[0].id)
    assert projection["head_sha"] == "a" * 40
    assert "synthetic-provider-secret" not in repr(projection)
    assert "synthetic-provider-secret" not in repr(rows[0].trigger_event_details)
    assert (
        "clone_url"
        not in rows[0].trigger_event_details["payload"]["pull_request"]["head"]["repo"]
    )
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["precreated_execution"].id == rows[0].id
    assert (
        dispatch.await_args.kwargs["event_data"]["payload"]["pull_request"]["base"][
            "ref"
        ]
        == branch
    )
    nats_loader.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_transport_failure_records_safe_terminal_failure(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    mock_provider(monkeypatch, context, payload=provider_payload(context))
    monkeypatch.setattr(
        "preloop.sync.services.event_bus.get_nats_client",
        AsyncMock(side_effect=RuntimeError("synthetic-provider-secret transport")),
    )
    with pytest.raises(service.CiReviewUnavailableError) as caught:
        await service.trigger_ci_review(
            db_session, context=context, request=source_for(context).request
        )
    assert caught.value.execution_id is not None
    execution = crud.crud_ci_execution.get(
        db_session,
        context=context,
        execution_id=caught.value.execution_id,
        action=CiAction.READ_EXECUTION,
    )
    assert execution is not None
    assert execution.status == "FAILED"
    assert execution.failure_category == "verification_blocked"
    assert execution.end_time is not None
    assert "synthetic-provider-secret" not in execution.error_message
    assert "synthetic-provider-secret" not in str(caught.value)
    assert "synthetic-provider-secret" not in caplog.text


@pytest.mark.asyncio
async def test_binding_move_during_initial_provider_read_denies_before_insert(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    payload = provider_payload(context)

    async def moved_repository(*args: Any, **kwargs: Any) -> dict[str, Any]:
        crud.crud_project.update(
            db_session, db_obj=ci_resources[1], obj_in={"identifier": "moved"}
        )
        return payload

    _, client = mock_provider(monkeypatch, context, payload=payload)
    client._request.side_effect = moved_repository
    create = Mock(wraps=crud.crud_ci_execution.create)
    monkeypatch.setattr(crud.crud_ci_execution, "create", create)
    nats = AsyncMock()
    monkeypatch.setattr("preloop.sync.services.event_bus.get_nats_client", nats)
    with pytest.raises(PermissionError):
        await service.trigger_ci_review(
            db_session, context=context, request=source_for(context).request
        )
    create.assert_not_called()
    nats.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_gitlab_factory_failure_does_not_log_provider_body(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exercise the real client-construction boundary, not just transport errors."""
    context = replace(context, tracker_type="gitlab")
    client = Mock()
    client.auth.side_effect = gitlab.exceptions.GitlabHttpError(
        "synthetic-provider-secret body", response_code=500
    )
    client.http_get.side_effect = gitlab.exceptions.GitlabHttpError(
        "synthetic-provider-secret body", response_code=500
    )
    monkeypatch.setattr(gitlab, "Gitlab", Mock(return_value=client))
    with pytest.raises(service.CiReviewUnavailableError) as caught:
        await service._read_provider(source_for(context))
    assert "synthetic-provider-secret" not in str(caught.value)
    assert "synthetic-provider-secret" not in caplog.text


@pytest.mark.asyncio
async def test_gitlab_all_network_calls_have_timeout_and_run_off_loop(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
) -> None:
    """Client construction may authenticate; transport safety starts there."""
    import threading

    context = replace(context, tracker_type="gitlab")
    loop_thread = threading.get_ident()
    calls: list[tuple[str, int, Any]] = []

    class FakeGitlab:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.timeout = kwargs.get("timeout")

        def auth(self) -> None:
            calls.append(("auth", threading.get_ident(), self.timeout))

        def http_get(self, path: str) -> dict[str, Any]:
            calls.append((path, threading.get_ident(), self.timeout))
            return provider_payload(context)

    monkeypatch.setattr(gitlab, "Gitlab", FakeGitlab)
    await service._read_provider(source_for(context))
    assert calls
    assert all(thread != loop_thread for _, thread, _ in calls)
    assert all(
        isinstance(timeout, (int, float)) and 0 < timeout <= 25
        for _, _, timeout in calls
    )


@pytest.mark.asyncio
async def test_github_app_token_failure_does_not_log_provider_body(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Trusted App auth must not leak its token-acquisition failure upstream."""
    import sys
    from types import ModuleType

    app = Mock()
    app.get_installation_access_token = AsyncMock(
        side_effect=RuntimeError("synthetic-provider-secret token response")
    )
    module = ModuleType("preloop.plugins.proprietary.github_app.service")
    module.get_github_app_service = lambda: app
    monkeypatch.setitem(sys.modules, module.__name__, module)
    source = service._ProviderRead(
        context=context,
        request=CiReviewRequest(pr_number=7, head_sha="a" * 40),
        tracker_key="",
        tracker_options={"auth_type": "github_app", "github_installation_id": 42},
    )
    with pytest.raises(service.CiReviewUnavailableError) as caught:
        await service._read_provider(source)
    app.get_installation_access_token.assert_awaited_once_with(42)
    assert "synthetic-provider-secret" not in str(caught.value)
    assert "synthetic-provider-secret" not in caplog.text


@pytest.mark.asyncio
async def test_gitlab_provider_host_cannot_be_repointed_by_tracker_options(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
) -> None:
    """Numeric repository ids are scoped to the immutable accepted host."""
    context = replace(
        context, tracker_type="gitlab", tracker_url="https://approved.example"
    )
    client = Mock()
    client.http_get.return_value = provider_payload(context)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(gitlab, "Gitlab", constructor)
    source = service._ProviderRead(
        context=context,
        request=source_for(context).request,
        tracker_key="synthetic-provider-secret",
        tracker_options={"url": "https://different.example"},
    )
    try:
        await service._read_provider(source)
    except (service.CiReviewDeniedError, service.CiReviewUnavailableError):
        return
    constructor.assert_called_once()
    assert constructor.call_args.args[0] == "https://approved.example"


@pytest.mark.asyncio
async def test_github_custom_host_never_sends_bound_key_to_public_provider(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
) -> None:
    """Unsupported private hosts must fail closed before a public API request."""
    context = replace(context, tracker_url="https://approved.example")
    source = service._ProviderRead(
        context=context,
        request=source_for(context).request,
        tracker_key="synthetic-provider-secret",
        tracker_options={"url": context.tracker_url},
    )
    requested_hosts: list[str] = []

    async def fake_request(client: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        requested_hosts.append(client.API_BASE_URL)
        return provider_payload(context)

    monkeypatch.setattr(service._ReviewGitHubTracker, "_request", fake_request)
    try:
        await service._read_provider(source)
    except (service.CiReviewDeniedError, service.CiReviewUnavailableError):
        assert not requested_hosts
        return
    assert requested_hosts
    assert all(host.startswith("https://approved.example/") for host in requested_hosts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound, effective, expected",
    [
        (None, None, "https://gitlab.com"),
        (
            "https://approved.example/api/v4",
            "https://approved.example/",
            "https://approved.example",
        ),
        (
            "https://approved.example/",
            "https://approved.example/api/v4/",
            "https://approved.example",
        ),
    ],
)
async def test_gitlab_equivalent_bound_provider_urls_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    context: CiAuthorizationContext,
    bound: str | None,
    effective: str | None,
    expected: str,
) -> None:
    context = replace(context, tracker_type="gitlab", tracker_url=bound)
    client = Mock()
    client.http_get.return_value = provider_payload(context)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(gitlab, "Gitlab", constructor)
    source = service._ProviderRead(
        context=context,
        request=source_for(context).request,
        tracker_key="synthetic-provider-secret",
        tracker_options={"url": effective},
    )
    assert (await service._read_provider(source)).head_sha == "a" * 40
    assert constructor.call_args.args[0] == expected


def test_persisted_result_projection_omits_runtime_prompt_and_mcp_column_names() -> (
    None
):
    """Controller column names remain private even when nested in an artifact."""
    result = {
        "review": "synthetic persisted review",
        "nested": [
            {
                "resolved_prompt": "synthetic-private-prompt",
                "resolved_input_prompt": "synthetic-private-input-prompt",
                "mcp_usage_logs": [{"authorization": "synthetic-provider-secret"}],
                "score": 1,
            }
        ],
    }
    projected = service.public_ci_result(result)
    assert projected == {
        "review": "synthetic persisted review",
        "nested": [{"score": 1}],
    }


@pytest.mark.asyncio
async def test_worker_admission_denial_does_not_reload_expired_fields_on_loop(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker final-status reads after rejection must not issue blocking SQL."""
    import threading
    from types import SimpleNamespace

    from sqlalchemy import event

    from preloop.services import flow_execution_runner

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    orchestrator = SimpleNamespace(
        db=db_session, execution_log=execution, run=AsyncMock()
    )
    monkeypatch.setattr(
        service,
        "ensure_ci_dispatch_admission",
        AsyncMock(side_effect=PermissionError("denied")),
    )
    loop_thread = threading.get_ident()
    query_threads: list[int] = []
    connection = db_session.get_bind()

    def record_query(*args: Any, **kwargs: Any) -> None:
        query_threads.append(threading.get_ident())

    event.listen(connection, "before_cursor_execute", record_query)
    try:
        await flow_execution_runner.run_existing_execution(orchestrator)
        # These are the caller's exact post-run primitive accesses.
        assert orchestrator.execution_log.agent_session_reference is None
        assert orchestrator.execution_log.status == "FAILED"
    finally:
        event.remove(connection, "before_cursor_execute", record_query)
    orchestrator.run.assert_not_awaited()
    assert query_threads
    assert all(thread != loop_thread for thread in query_threads)


@pytest.mark.parametrize(
    "field",
    [
        "apiKey",
        "accessToken",
        "clientSecret",
        "resolvedInputPrompt",
        "mcpUsageLogs",
        "API-KEY",
        "access.token",
        "client secret",
        "resolved-input-prompt",
        "MCP.Usage.Logs",
        "providerApiKey",
        "provider.accessToken",
        "provider-clientSecret",
    ],
)
def test_result_projection_denies_alternate_runtime_field_spellings(field: str) -> None:
    value = {
        "review": "persisted",
        "nested": [{field: "synthetic-private-value", "score": 1}],
    }
    assert service.public_ci_result(value) == {
        "review": "persisted",
        "nested": [{"score": 1}],
    }


@pytest.mark.parametrize(
    "field",
    [
        "providerToken",
        "apiToken",
        "authToken",
        "providerCredential",
        "providerAuthorization",
        "requestHeaders",
        "providerSecrets",
        "provider.token",
        "provider-credential",
        "request headers",
    ],
)
def test_result_projection_denies_prefixed_credential_class_fields(field: str) -> None:
    value = {"ReviewScore": 1, "nested": [{field: "synthetic-private-value"}]}
    assert service.public_ci_result(value) == {"ReviewScore": 1, "nested": [{}]}
