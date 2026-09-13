"""Isolated registration and authority regressions for applied issue triage."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import mcp as mcp_router
from preloop.services.initialize_mcp import initialize_mcp_with_tools


TOOL_ARGUMENTS = {
    "get_issue_triage_context": {"issue": "example/project#17"},
    "apply_issue_triage": {
        "issue": "example/project#17",
        "expected_revision": "a" * 64,
        "complexity_label": "complexity:low",
        "assessment": "Remaining behavior: reject empty input. Acceptance: HTTP 400.",
        "title": "Reject empty input",
    },
}


@pytest.fixture
def mcp_server() -> Any:
    """Construct registrations without starting a server or provider connection."""
    return initialize_mcp_with_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_triage_catalog_matches_callable(mcp_server: Any, name: str) -> None:
    from preloop.api.endpoints.tools import BUILTIN_TOOLS

    entries = [entry for entry in BUILTIN_TOOLS if entry["name"] == name]
    assert len(entries) == 1
    entry = entries[0]
    tool = await mcp_server.get_tool(name)
    assert tool is not None
    signature = inspect.signature(tool.fn)
    parameters = {
        key: parameter
        for key, parameter in signature.parameters.items()
        if key != "ctx"
    }
    assert set(parameters) == set(entry["schema"]["properties"])
    required = {
        key
        for key, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert required == set(entry["schema"]["required"])
    assert entry["requires_tracker"] is True
    assert set(entry["required_tracker_types"]) == {"github", "gitlab"}
    assert tool.description == entry["description"]
    assert tool.parameters == entry["schema"]
    assert tool.parameters["additionalProperties"] is False
    assert not {
        "labels",
        "status",
        "assignee",
        "priority",
        "repository_url",
        "project_id",
        "tracker_id",
    }.intersection(parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_triage_without_context_never_calls_router(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    tool = await mcp_server.get_tool(name)
    assert tool is not None
    router = AsyncMock()
    approval = AsyncMock()
    monkeypatch.setattr(mcp_router, name, router)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: None,
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)

    result = await tool.fn(**TOOL_ARGUMENTS[name])

    assert "Error" in result
    approval.assert_not_awaited()
    router.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_triage_approval_denial_prevents_provider_path(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    tool = await mcp_server.get_tool(name)
    assert tool is not None
    router = AsyncMock()
    approval = AsyncMock(return_value=(False, "Denied by configured policy"))
    monkeypatch.setattr(mcp_router, name, router)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(account_id="authorized-account"),
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)

    result = await tool.fn(**TOOL_ARGUMENTS[name])

    assert result == "Denied by configured policy"
    router.assert_not_awaited()
    arguments = approval.await_args.kwargs
    assert arguments["tool_name"] == name
    assert arguments["tool_source"] == "builtin"
    assert arguments["account_id"] == "authorized-account"
    assert arguments["arguments"] == TOOL_ARGUMENTS[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_triage_approval_uses_same_arguments_as_execution(
    mcp_server: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    tool = await mcp_server.get_tool(name)
    assert tool is not None
    response = MagicMock()
    response.model_dump_json.return_value = '{"status":"needs_reconciliation"}'
    router = AsyncMock(return_value=response)
    approval = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(mcp_router, name, router)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(account_id="authorized-account"),
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)

    result = await tool.fn(**TOOL_ARGUMENTS[name])

    assert result == '{"status":"needs_reconciliation"}'
    assert approval.await_args.kwargs["arguments"] == TOOL_ARGUMENTS[name]
    router.assert_awaited_once()
    # Read-only endpoints may pass their issue positionally.
    executed = dict(router.await_args.kwargs)
    if router.await_args.args:
        assert router.await_args.args == (TOOL_ARGUMENTS[name]["issue"],)
        executed["issue"] = router.await_args.args[0]
    assert executed == TOOL_ARGUMENTS[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer invalid"}])
async def test_triage_endpoint_rejects_unauthenticated_call(
    monkeypatch: pytest.MonkeyPatch, name: str, headers: dict[str, str]
) -> None:
    monkeypatch.setattr(mcp_router, "_get_tool_db", lambda: MagicMock())
    monkeypatch.setattr(
        mcp_router, "get_http_request", lambda: SimpleNamespace(headers=headers)
    )
    monkeypatch.setattr(
        mcp_router, "get_user_from_token_if_valid", AsyncMock(return_value=None)
    )
    lookup = MagicMock()
    provider = AsyncMock()
    monkeypatch.setattr(mcp_router, "_find_issue_by_identifier", lookup)
    monkeypatch.setattr(mcp_router, "get_tracker_client", provider)

    with pytest.raises(HTTPException) as error:
        await getattr(mcp_router, name)(**TOOL_ARGUMENTS[name])

    assert error.value.status_code == 401
    lookup.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_write_propagates_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    user = SimpleNamespace(account_id="authorized-account")
    monkeypatch.setattr(mcp_router, "_get_tool_db", lambda: db)
    monkeypatch.setattr(
        mcp_router,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer valid"}),
    )
    monkeypatch.setattr(
        mcp_router, "get_user_from_token_if_valid", AsyncMock(return_value=user)
    )
    authorized = AsyncMock(
        side_effect=HTTPException(status_code=403, detail="edit_issues required")
    )
    monkeypatch.setattr(mcp_router, "_apply_authorized_issue_triage", authorized)

    with pytest.raises(HTTPException) as error:
        await mcp_router.apply_issue_triage(**TOOL_ARGUMENTS["apply_issue_triage"])

    assert error.value.status_code == 403
    assert authorized.await_args.kwargs == {
        "db": db,
        "current_user": user,
        **TOOL_ARGUMENTS["apply_issue_triage"],
    }


@pytest.mark.asyncio
async def test_triage_provider_uses_authorized_stored_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    user = SimpleNamespace(account_id="authorized-account")
    issue = SimpleNamespace(
        key="trusted/project#17",
        external_id="provider-id",
        project_id="stored-project",
        project=SimpleNamespace(organization_id="stored-org"),
    )
    lookup = MagicMock(return_value=issue)
    client = SimpleNamespace(
        tracker_type="github",
        connection_details={"owner": "trusted", "repo": "project"},
    )
    tracker = AsyncMock(return_value=client)
    monkeypatch.setattr(mcp_router, "_find_issue_by_identifier", lookup)
    monkeypatch.setattr(mcp_router, "get_tracker_client", tracker)

    stored, provider = await mcp_router._triage_provider(db, user, "caller-identifier")

    assert stored is issue
    lookup.assert_called_once_with(db, "caller-identifier", "authorized-account")
    tracker.assert_awaited_once_with("stored-org", "stored-project", db, user)
    assert provider.issue_path == "/repos/trusted/project/issues/17"


@pytest.mark.asyncio
async def test_triage_account_lookup_denial_never_builds_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_router,
        "_find_issue_by_identifier",
        MagicMock(side_effect=mcp_router.IssueNotFoundError("not found in account")),
    )
    tracker = AsyncMock()
    monkeypatch.setattr(mcp_router, "get_tracker_client", tracker)

    with pytest.raises(HTTPException) as error:
        await mcp_router._triage_provider(
            MagicMock(), SimpleNamespace(account_id="account"), "other-account-issue"
        )

    assert error.value.status_code == 404
    tracker.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_tracker_scope_denial_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = SimpleNamespace(
        project_id="excluded-project",
        project=SimpleNamespace(organization_id="org"),
    )
    monkeypatch.setattr(
        mcp_router, "_find_issue_by_identifier", MagicMock(return_value=issue)
    )
    monkeypatch.setattr(
        mcp_router,
        "get_tracker_client",
        AsyncMock(
            side_effect=HTTPException(status_code=403, detail="Project excluded")
        ),
    )

    with pytest.raises(HTTPException) as error:
        await mcp_router._triage_provider(
            MagicMock(), SimpleNamespace(account_id="account"), "excluded-issue"
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_failure", [False, True])
async def test_authorized_triage_preserves_provider_outcome_on_cache_failure(
    monkeypatch: pytest.MonkeyPatch, cache_failure: bool
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy.exc import SQLAlchemyError

    from preloop.schemas.issue_triage import IssueTriageResult, TriageIssue

    db = MagicMock()
    user = SimpleNamespace(account_id="authorized-account")
    stored = SimpleNamespace(meta_data={"other": "preserved"})
    provider = object()
    observed = TriageIssue(
        title="Provider title",
        body="Provider assessment",
        state="open",
        url="https://github.com/example/project/issues/17",
        labels=["P1", "complexity:low"],
        updated_at="2026-09-13T12:00:00Z",
    )
    operations = [{"operation": "update_content", "state": "confirmed"}]
    receipt = {"provider_revision": observed.revision}
    setter = MagicMock()
    cache = MagicMock(
        side_effect=SQLAlchemyError("Cache unavailable") if cache_failure else None
    )
    monkeypatch.setattr(
        mcp_router, "_triage_provider", AsyncMock(return_value=(stored, provider))
    )
    monkeypatch.setattr(mcp_router.crud_issue, "set_triage_receipt", setter)
    monkeypatch.setattr(mcp_router.crud_issue, "update", cache)

    async def apply(actual: Any, request: Any, record: Any) -> IssueTriageResult:
        assert actual is provider
        record(receipt)
        return IssueTriageResult(
            status="updated", issue=observed, operations=operations
        )

    monkeypatch.setattr("preloop.services.issue_triage.apply_triage", apply)

    result = await mcp_router._apply_authorized_issue_triage(
        db=db, current_user=user, **TOOL_ARGUMENTS["apply_issue_triage"]
    )

    setter.assert_called_once_with(db, db_obj=stored, receipt=receipt)
    values = cache.call_args.kwargs["obj_in"]
    assert values == {
        "title": observed.title,
        "description": observed.body,
        "status": observed.state,
        "meta_data": {"other": "preserved", "labels": observed.labels},
        "last_updated_external": datetime(2026, 9, 13, 12, tzinfo=timezone.utc),
    }
    assert result.issue == observed
    assert result.operations == operations
    assert result.cache_updated is not cache_failure
    assert result.status == ("partial" if cache_failure else "updated")
    if cache_failure:
        assert result.reason == "provider_result_cache_failed"
        assert "do not repeat completed writes" in result.next_action
