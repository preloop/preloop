"""Registration and authority regressions for triage on the standard tools."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import mcp as mcp_router
from preloop.services.initialize_mcp import initialize_mcp_with_tools


REMOVED_TOOLS = ("get_issue_triage_context", "apply_issue_triage")

TRIAGE_WRITE = {
    "issue": "example/project#17",
    "expected_revision": "a" * 64,
    "complexity_label": "complexity:low",
    "assessment": "Remaining behavior: reject empty input. Acceptance: HTTP 400.",
    "title": "Reject empty input",
}

TOOL_ARGUMENTS = {
    "get_issue": {
        "issue": "example/project#17",
        "include": ["label_catalog", "revision"],
    },
    "update_issue": {
        "issue": TRIAGE_WRITE["issue"],
        "title": TRIAGE_WRITE["title"],
        "description": None,
        "status": None,
        "priority": None,
        "assignee": None,
        "labels": None,
        "add_reaction": None,
        "remove_reaction": None,
        "expected_revision": TRIAGE_WRITE["expected_revision"],
        "assessment": TRIAGE_WRITE["assessment"],
        "complexity_label": TRIAGE_WRITE["complexity_label"],
    },
}


@pytest.fixture
def mcp_server() -> Any:
    """Construct registrations without starting a server or provider connection."""
    return initialize_mcp_with_tools()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", REMOVED_TOOLS)
async def test_removed_triage_tools_are_gone(mcp_server: Any, name: str) -> None:
    """No agent can receive a schema for a tool that no longer exists (#661)."""
    from preloop.api.endpoints.tools import BUILTIN_TOOLS
    from preloop.tools import builtin_defs

    assert await mcp_server.get_tool(name) is None
    assert name not in {entry["name"] for entry in BUILTIN_TOOLS}
    assert not hasattr(mcp_router, name)
    assert not any(
        isinstance(value, dict) and value.get("name") == name
        for value in vars(builtin_defs).values()
    )


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
    assert required == set(entry["schema"]["required"]) == {"issue"}
    assert entry["requires_tracker"] is True
    assert tool.description == entry["description"]
    assert tool.parameters == entry["schema"]
    # The router accepts exactly what the advertised schema offers.
    router = inspect.signature(getattr(mcp_router, name).__wrapped__)
    assert set(router.parameters) == set(entry["schema"]["properties"])


def test_catalog_documents_the_triage_parameters() -> None:
    from preloop.api.endpoints.tools import BUILTIN_TOOLS

    catalog = {entry["name"]: entry for entry in BUILTIN_TOOLS}
    get_issue = catalog["get_issue"]["schema"]["properties"]["include"]
    assert get_issue["items"]["enum"] == ["label_catalog", "revision"]
    update_issue = catalog["update_issue"]["schema"]["properties"]
    assert update_issue["expected_revision"]["pattern"] == "^[0-9a-f]{64}$"
    assert update_issue["assessment"]["maxLength"] == 16000
    assert update_issue["complexity_label"]["default"] is None
    assert "expected_revision" not in catalog["update_issue"]["schema"]["required"]


def test_include_vocabulary_matches_advertised_schema() -> None:
    """Runtime validation and GetIssueRequest follow GET_ISSUE_SCHEMA."""
    from typing import get_args

    from preloop.api.endpoints.tools import BUILTIN_TOOLS
    from preloop.schemas.mcp import GetIssueRequest
    from preloop.tools.builtin_defs import GET_ISSUE_SCHEMA

    advertised = tuple(GET_ISSUE_SCHEMA["properties"]["include"]["items"]["enum"])
    catalog = {entry["name"]: entry for entry in BUILTIN_TOOLS}
    assert advertised == tuple(
        catalog["get_issue"]["schema"]["properties"]["include"]["items"]["enum"]
    )
    assert mcp_router.TRIAGE_INCLUDES == advertised

    list_type = get_args(GetIssueRequest.model_fields["include"].annotation)[0]
    literal = get_args(list_type)[0]
    assert get_args(literal) == advertised


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
    monkeypatch.setattr(mcp_router, "_get_tool_db", MagicMock)
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


@pytest.fixture
def authenticated(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Authenticate one tool call without touching a database or provider."""
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
    return SimpleNamespace(db=db, user=user)


@pytest.mark.asyncio
async def test_get_issue_include_returns_the_triage_context(
    authenticated: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include returns exactly what the removed context tool returned."""
    from preloop.schemas.issue_triage import (
        ComplexityScheme,
        IssueTriageContext,
        TriageIssue,
    )

    observed = TriageIssue(
        title="Issue",
        body="Human scope.",
        state="open",
        url="https://github.com/example/project/issues/17",
        labels=["P1"],
        updated_at="2026-09-13T12:00:00Z",
    )
    context = IssueTriageContext(
        issue=observed,
        expected_revision="b" * 64,
        catalogue=[{"name": "P1", "description": "Priority"}],
        complexity_scheme=ComplexityScheme(name="standard", labels=["complexity:low"]),
        limitations=["ambiguous_complexity_vocabulary"],
    )
    read = AsyncMock(return_value=context)
    monkeypatch.setattr(mcp_router, "_read_triage_context", read)
    monkeypatch.setattr(
        mcp_router, "_find_issue_by_identifier", MagicMock(return_value=_stored())
    )
    monkeypatch.setattr(
        mcp_router.crud_issue_compliance_result,
        "get_for_issue",
        MagicMock(return_value=[]),
    )

    result = await mcp_router.get_issue(
        "example/project#17", include=["label_catalog", "revision"]
    )

    read.assert_awaited_once_with(
        authenticated.db, authenticated.user, "example/project#17"
    )
    assert result.label_catalog == context.catalogue
    assert result.complexity_scheme == context.complexity_scheme
    assert result.expected_revision == context.expected_revision
    assert result.provider_issue == observed
    assert result.triage_limitations == context.limitations
    assert result.concurrency == context.concurrency
    # The synchronized snapshot is still the body of the response.
    assert result.title == "Stored title"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "include,expected",
    [
        (None, set()),
        ([], set()),
        (["revision"], {"expected_revision", "provider_issue"}),
        (["label_catalog"], {"label_catalog", "complexity_scheme"}),
    ],
)
async def test_get_issue_reads_the_provider_only_when_asked(
    authenticated: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    include: list[str] | None,
    expected: set[str],
) -> None:
    from preloop.schemas.issue_triage import IssueTriageContext, TriageIssue

    context = IssueTriageContext(
        issue=TriageIssue(
            title="Issue",
            body="Body",
            state="open",
            url="https://github.com/example/project/issues/17",
            labels=[],
        ),
        expected_revision="c" * 64,
        catalogue=[],
        complexity_scheme=None,
    )
    read = AsyncMock(return_value=context)
    monkeypatch.setattr(mcp_router, "_read_triage_context", read)
    monkeypatch.setattr(
        mcp_router, "_find_issue_by_identifier", MagicMock(return_value=_stored())
    )
    monkeypatch.setattr(
        mcp_router.crud_issue_compliance_result,
        "get_for_issue",
        MagicMock(return_value=[]),
    )

    result = await mcp_router.get_issue("example/project#17", include=include)

    assert read.await_count == (1 if include else 0)
    populated = {
        field
        for field in ("expected_revision", "provider_issue", "label_catalog")
        if getattr(result, field) is not None
    }
    assert populated == (expected - {"complexity_scheme"})
    if not include:
        assert result.triage_limitations is None
        assert result.concurrency is None


@pytest.mark.asyncio
async def test_get_issue_rejects_an_unknown_include(
    authenticated: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = AsyncMock()
    lookup = MagicMock()
    monkeypatch.setattr(mcp_router, "_read_triage_context", read)
    monkeypatch.setattr(mcp_router, "_find_issue_by_identifier", lookup)

    with pytest.raises(HTTPException) as error:
        await mcp_router.get_issue("example/project#17", include=["comments"])

    assert error.value.status_code == 422
    assert "comments" in error.value.detail
    read.assert_not_awaited()
    lookup.assert_not_called()


def _stored() -> SimpleNamespace:
    """One synchronized issue record with the attributes get_issue reads."""
    return SimpleNamespace(
        id=uuid4(),
        external_id="17",
        key="example/project#17",
        title="Stored title",
        description="Stored description",
        status="open",
        priority=None,
        project_id=uuid4(),
        external_url="https://github.com/example/project/issues/17",
        created_at="2026-09-13T11:00:00Z",
        updated_at="2026-09-13T12:00:00Z",
        meta_data={"labels": ["P1"]},
        project=SimpleNamespace(
            name="project",
            identifier="project",
            slug="project",
            organization_id="org",
            organization=SimpleNamespace(name="example"),
        ),
    )


@pytest.mark.asyncio
async def test_update_issue_triage_arguments_reach_the_authorized_writer(
    authenticated: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.schemas.issue_triage import IssueTriageResult

    receipt = IssueTriageResult(status="updated")
    authorized = AsyncMock(return_value=receipt)
    tracker = AsyncMock()
    monkeypatch.setattr(mcp_router, "_apply_authorized_issue_triage", authorized)
    monkeypatch.setattr(mcp_router, "get_tracker_client", tracker)

    result = await mcp_router.update_issue(**TRIAGE_WRITE)

    assert result is receipt
    tracker.assert_not_awaited()
    assert authorized.await_args.kwargs == {
        "db": authenticated.db,
        "current_user": authenticated.user,
        **TRIAGE_WRITE,
    }


@pytest.mark.asyncio
async def test_update_issue_triage_propagates_permission_denial(
    authenticated: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorized = AsyncMock(
        side_effect=HTTPException(status_code=403, detail="edit_issues required")
    )
    monkeypatch.setattr(mcp_router, "_apply_authorized_issue_triage", authorized)

    with pytest.raises(HTTPException) as error:
        await mcp_router.update_issue(**TRIAGE_WRITE)

    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,missing",
    [
        ({"assessment": TRIAGE_WRITE["assessment"]}, "expected_revision"),
        ({"expected_revision": TRIAGE_WRITE["expected_revision"]}, "assessment"),
        ({"complexity_label": "complexity:low"}, "expected_revision"),
    ],
)
async def test_update_issue_refuses_an_incomplete_triage_write(
    authenticated: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    missing: str,
) -> None:
    authorized = AsyncMock()
    lookup = MagicMock()
    monkeypatch.setattr(mcp_router, "_apply_authorized_issue_triage", authorized)
    monkeypatch.setattr(mcp_router, "_find_issue_by_identifier", lookup)

    with pytest.raises(HTTPException) as error:
        await mcp_router.update_issue("example/project#17", **arguments)

    assert error.value.status_code == 422
    assert missing in error.value.detail
    authorized.assert_not_awaited()
    lookup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("labels", ["P1"]),
        ("status", "closed"),
        ("assignee", "someone"),
        ("add_reaction", "eyes"),
        ("description", "Replace the whole body"),
    ],
)
async def test_update_issue_refuses_to_mix_triage_with_metadata(
    authenticated: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    """A triage write manages content and complexity labels only."""
    authorized = AsyncMock()
    lookup = MagicMock()
    monkeypatch.setattr(mcp_router, "_apply_authorized_issue_triage", authorized)
    monkeypatch.setattr(mcp_router, "_find_issue_by_identifier", lookup)

    with pytest.raises(HTTPException) as error:
        await mcp_router.update_issue(**{**TRIAGE_WRITE, field: value})

    assert error.value.status_code == 422
    assert field in error.value.detail
    authorized.assert_not_awaited()
    lookup.assert_not_called()


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
    expected = IssueTriageResult(
        status="partial" if cache_failure else "updated",
        issue=observed,
        operations=operations,
        cache_updated=not cache_failure,
        reason="provider_result_cache_failed" if cache_failure else None,
    )
    monkeypatch.setattr(
        mcp_router, "_triage_provider", AsyncMock(return_value=(stored, provider))
    )
    controlled = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "preloop.services.issue_triage_controller.apply_controlled_triage", controlled
    )
    result = await mcp_router._apply_authorized_issue_triage(
        db=db, current_user=user, **TRIAGE_WRITE
    )
    assert result is expected
    arguments = controlled.call_args.kwargs
    assert arguments["issue"] is stored
    assert arguments["provider"] is provider
    assert arguments["account_id"] == user.account_id
    assert arguments["current_user"] is user
    assert arguments["execution_id"] is None
    assert arguments["request"].expected_revision == TRIAGE_WRITE["expected_revision"]
    # Actual durable receipt/cache failure and no-packet recovery are exercised
    # with PostgreSQL in test_issue_triage_controller.py.
