"""Regression coverage for account-level unlisted MCP tool defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from typing import Any, AsyncIterator
from sqlalchemy.orm import Session
from preloop.models import models

import pytest

from preloop.services import policy_evaluator as evaluator


@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("site", ["no_configuration", "no_rules", "no_match", "scoped"])
@pytest.mark.parametrize("source", ["mcp", "agent", "builtin"])
@pytest.mark.parametrize("action", ["allow", "deny", "require_approval"])
@pytest.mark.asyncio
async def test_unmatched_defaults(
    monkeypatch: pytest.MonkeyPatch,
    async_path: bool,
    site: str,
    source: str,
    action: str,
) -> None:
    """protects=MCP defaults at every allow site; fails_when=unlisted MCP runs;
    no_claim=CRUD and audit are mocked, no transport or approval delivery.
    """
    workflow_id = uuid4()
    config = (
        None
        if site == "no_configuration"
        else SimpleNamespace(
            id=uuid4(),
            approval_workflow_id=None,
            tool_source=source,
        )
    )
    meta = {
        "policy_defaults": {
            "unknown_tools": action,
            "default_approval_workflow": "Review",
        }
    }
    rule = SimpleNamespace(
        id=uuid4(),
        condition_expression="false",
        condition_type="simple",
        action="allow",
        approval_workflow_id=None,
        description="never",
        priority=0,
        is_enabled=True,
    )
    rules = [rule] if site == "no_match" else []
    scoped = (
        [{"condition_expression": "false", "action": "allow"}]
        if site == "scoped"
        else []
    )
    monkeypatch.setattr(evaluator, "get_scoped_tool_rules", lambda *a, **kw: scoped)
    monkeypatch.setattr(evaluator, "_log_policy_decision_async", MagicMock())
    monkeypatch.setattr(
        evaluator.crud_account, "get", lambda *a, **kw: SimpleNamespace(meta_data=meta)
    )
    monkeypatch.setattr(evaluator, "get_meta_data_async", AsyncMock(return_value=meta))
    monkeypatch.setattr(
        evaluator.crud_tool_configuration, "get", lambda *a, **kw: config
    )
    monkeypatch.setattr(
        evaluator, "get_tool_config_by_id_async", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        evaluator.crud_tool_access_rule, "get_multi_by_config", lambda *a, **kw: rules
    )
    monkeypatch.setattr(
        evaluator, "get_multi_by_config_async", AsyncMock(return_value=rules)
    )
    monkeypatch.setattr(
        evaluator.crud_approval_workflow, "get_default", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        evaluator, "get_default_approval_workflow_async", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        evaluator.crud_approval_workflow,
        "get_by_name",
        lambda *a, **kw: SimpleNamespace(id=workflow_id),
    )
    db = MagicMock()
    db.run_sync = AsyncMock(side_effect=lambda fn, *args: fn(db, *args))
    kwargs = dict(
        db=db,
        account_id=uuid4(),
        tool_name="fetch",
        tool_args={},
        tool_configuration_id=uuid4(),
        subject_context={"tool_source": source},
    )
    decision = (
        await evaluator.evaluate_policy_async(**kwargs)
        if async_path
        else evaluator.evaluate_policy(**kwargs)
    )
    assert decision.action == (action if source == "mcp" else "allow")
    assert decision.matched is False
    assert decision.source == (
        "unknown_tools_default" if source == "mcp" else "no_configuration"
    )
    if action == "require_approval" and source == "mcp":
        assert decision.approval_workflow_id == workflow_id


@pytest.mark.parametrize("raw", [None, {}, {"inherit_from_parent": False}])
def test_omitted_defaults_preserve_allow(raw: dict | None) -> None:
    """protects=legacy accounts allow; fails_when=opt-in becomes mandatory;
    no_claim=pure parser, no database.
    """
    from preloop.services.policy.defaults import parse_defaults

    assert parse_defaults(raw).unknown_tools == "allow"


@pytest.mark.parametrize("action", ["allow", "deny", "require_approval"])
def test_validate_defaults(action: str) -> None:
    """protects=restrictive defaults load; fails_when=valid opt-in rejected;
    no_claim=reference names stand in for stored workflows.
    """
    from preloop.services.policy.loader import invalid_default_settings
    from preloop.services.policy.schema import DefaultsDefinition

    defaults = DefaultsDefinition(
        unknown_tools=action, default_approval_workflow="Review"
    )
    assert invalid_default_settings(defaults, {"Review"}) == []
    if action == "require_approval":
        errors = invalid_default_settings(
            DefaultsDefinition(unknown_tools=action), set()
        )
        assert errors and "default_approval_workflow" in errors[0]
    assert "default_approval_workflow" in invalid_default_settings(defaults, set())[0]


@pytest.mark.parametrize("raw", ["broken", {"unknown_tools": "invalid"}])
def test_malformed_defaults_do_not_become_allow(raw: object) -> None:
    """protects=fail closed for malformed stored state; fails_when=invalid allows;
    no_claim=validation only, transport callers handle the error.
    """
    from pydantic import ValidationError
    from preloop.services.policy.defaults import parse_defaults

    with pytest.raises(ValidationError):
        parse_defaults(raw)


@pytest.mark.asyncio
async def test_hook_mcp_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=direct MCP hook is gated; fails_when=no config skips evaluation;
    no_claim=evaluator mocked, no notification or CLI process.
    """
    from preloop.services.agent_permission_service import apply_native_access_rules

    evaluate = AsyncMock(
        return_value=evaluator.PolicyDecision(
            "deny",
            None,
            "Unlisted",
            matched=False,
            source="unknown_tools_default",
        )
    )
    monkeypatch.setattr(evaluator, "evaluate_policy_async", evaluate)
    result = await apply_native_access_rules(
        AsyncMock(),
        config=None,
        account_id=str(uuid4()),
        user_id=None,
        tool_name="mcp__plugin-x__fetch",
        tool_input={},
        managed_agent_id=None,
        runtime_session_id=None,
    )
    assert result is not None and result[0] == "deny"
    assert evaluate.call_args.kwargs["tool_source"] == "mcp"
    assert evaluate.call_args.kwargs["server_label"] == "plugin-x"


def test_snapshot_and_rollback_preserve_defaults(
    db_session: Session, test_user: models.User
) -> None:
    """protects=rollback restores defaults and behavior; fails_when=deny is lost;
    no_claim=real PostgreSQL policy paths, no upstream MCP transport.
    """
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.services.policy_version_service import PolicyVersionService

    account_id = test_user.account_id

    def apply(action: str) -> None:
        """Apply the selected default through the normal loader."""
        result = PolicyApplier(db_session, account_id).apply(
            PolicyDocument.model_validate(
                {
                    "metadata": {"name": "Defaults regression"},
                    "defaults": {"unknown_tools": action},
                }
            )
        )
        assert result.success, result.errors

    apply("allow")
    service = PolicyVersionService(db_session, str(account_id))
    snapshot = service.create_snapshot()
    assert snapshot.snapshot_data["defaults"]["unknown_tools"] == "allow"
    apply("deny")
    assert (
        evaluator.evaluate_policy(
            db_session, "fetch", {}, account_id, subject_context={"tool_source": "mcp"}
        ).action
        == "deny"
    )
    _, success, error = service.rollback_to_snapshot(snapshot.id)
    assert success, error
    assert (
        evaluator.evaluate_policy(
            db_session, "fetch", {}, account_id, subject_context={"tool_source": "mcp"}
        ).action
        == "allow"
    )


@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("tool_name", ["fetch", "docs_fetch"])
@pytest.mark.asyncio
async def test_rules_do_not_cross_mcp_servers(
    db_session: Session,
    test_user: models.User,
    async_path: bool,
    tool_name: str,
) -> None:
    """protects=server identity including prefixes; fails_when=other server allows;
    no_claim=real database/evaluator, no network MCP execution.
    """
    from preloop.models import models
    from preloop.models.crud.policy_defaults import replace
    from preloop.models.db.session import SyncApprovalSession

    servers = [
        models.MCPServer(
            account_id=test_user.account_id,
            name=name,
            url="https://example.com/mcp",
            transport="http-streaming",
            auth_type="none",
        )
        for name in ("docs-a", "docs-b")
    ]
    db_session.add_all(servers)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_source="mcp",
        mcp_server_id=servers[0].id,
        tool_name=tool_name,
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()
    db_session.add(
        models.ToolAccessRule(
            account_id=test_user.account_id,
            tool_configuration_id=config.id,
            action="allow",
            condition_expression="true",
        )
    )
    db_session.flush()
    replace(db_session, test_user.account_id, {"unknown_tools": "deny"})
    kwargs = dict(
        tool_name=tool_name,
        tool_args={},
        account_id=test_user.account_id,
        tool_source="mcp",
        record=False,
    )
    if async_path:
        decision = await evaluator.evaluate_policy_async(
            SyncApprovalSession(db_session), mcp_server_id=servers[1].id, **kwargs
        )
        allowed = await evaluator.evaluate_policy_async(
            SyncApprovalSession(db_session), mcp_server_id=servers[0].id, **kwargs
        )
    else:
        decision = evaluator.evaluate_policy(
            db_session, mcp_server_id=servers[1].id, **kwargs
        )
        allowed = evaluator.evaluate_policy(
            db_session, mcp_server_id=servers[0].id, **kwargs
        )
    assert decision.action == "deny" and decision.source == "unknown_tools_default"
    assert allowed.action == "allow" and allowed.matched is True
    assert allowed.source == "rule"


@pytest.mark.asyncio
async def test_replay_rechecks_default_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=approved replay sees new deny; fails_when=replay dispatch bypasses;
    no_claim=database and evaluator mocked; actual replay guard exercised.
    """
    from contextlib import asynccontextmanager
    from preloop.services.dynamic_fastmcp import DynamicFastMCP, _approved_id_var

    server = DynamicFastMCP("replay-test")
    account_id, server_id, config_id = uuid4(), uuid4(), uuid4()
    server._proxied_tool_servers["docs_fetch"] = str(server_id)
    request = SimpleNamespace(
        account_id=account_id,
        status="approved",
        tool_name="docs_fetch",
        tool_configuration_id=config_id,
        approval_workflow_id=uuid4(),
    )

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        """Supply an isolated mock transaction."""
        yield AsyncMock()

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(
        "preloop.models.crud.approval_request.get_approval_request_async",
        AsyncMock(return_value=request),
    )
    monkeypatch.setattr(
        "preloop.models.crud.tool_configuration.get_tool_config_by_id_async",
        AsyncMock(return_value=SimpleNamespace(mcp_server_id=server_id)),
    )
    monkeypatch.setattr(
        evaluator,
        "evaluate_policy_async",
        AsyncMock(
            return_value=evaluator.PolicyDecision(
                "deny", None, "Unlisted", matched=False, source="unknown_tools_default"
            )
        ),
    )
    token = _approved_id_var.set(str(uuid4()))
    try:
        assert "blocks approval replay" in await server._approval_replay_denial(
            str(account_id), "docs_fetch", {}
        )
        request.status = "declined"
        assert "grant is invalid" in await server._approval_replay_denial(
            str(account_id), "docs_fetch", {}
        )
    finally:
        _approved_id_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,workflow,valid",
    [
        ("allow", None, True),
        ("deny", None, True),
        ("require_approval", "Review", True),
        ("require_approval", None, False),
        ("require_approval", "missing", False),
    ],
)
async def test_validate_upload_parity(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    workflow: str | None,
    valid: bool,
) -> None:
    """protects=validate and upload agree; fails_when=restrictive intent is ignored;
    no_claim=real DB and endpoints, upload dry-run skips notification delivery.
    """
    import io
    import json
    from fastapi import HTTPException, UploadFile
    from preloop.api.endpoints import policies
    from preloop.models import models

    db_session.add(
        models.ApprovalWorkflow(account_id=test_user.account_id, name="Review")
    )
    db_session.flush()
    data = json.dumps(
        {
            "metadata": {"name": "Parity"},
            "defaults": {
                "unknown_tools": action,
                "default_approval_workflow": workflow,
            },
        }
    ).encode()
    result = await policies.validate_policy(
        file=UploadFile(io.BytesIO(data), filename="policy.json"),
        check_server_references=True,
        account=test_user.account,
        current_user=test_user,
        db=db_session,
    )
    assert result.is_valid is valid
    kwargs = dict(
        file=UploadFile(io.BytesIO(data), filename="policy.json"),
        dry_run=True,
        resolve_env=False,
        skip_missing_servers=False,
        account=test_user.account,
        current_user=test_user,
        db=db_session,
    )
    if valid:
        assert (await policies.upload_policy(**kwargs)).success
    else:
        with pytest.raises(HTTPException) as error:
            await policies.upload_policy(**kwargs)
        assert error.value.status_code == 400
        assert "default_approval_workflow" in str(error.value.detail)


@pytest.mark.asyncio
async def test_hook_does_not_adopt_proxy_configuration(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=hook/proxy route isolation; fails_when=proxy allow widens hook;
    no_claim=real permission/evaluator/DB, no live hook process or MCP network.
    """
    from contextlib import asynccontextmanager
    from preloop.models.crud.policy_defaults import replace
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services import agent_permission_service as service

    server = models.MCPServer(
        account_id=test_user.account_id,
        name="docs",
        url="https://example.com/mcp",
        transport="http-streaming",
        auth_type="none",
    )
    db_session.add(server)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_source="mcp",
        mcp_server_id=server.id,
        tool_name="mcp__docs__fetch",
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()
    db_session.add(
        models.ToolAccessRule(
            account_id=test_user.account_id,
            tool_configuration_id=config.id,
            action="allow",
            condition_expression="true",
        )
    )
    db_session.flush()
    replace(db_session, test_user.account_id, {"unknown_tools": "deny"})

    @asynccontextmanager
    async def session() -> AsyncIterator[SyncApprovalSession]:
        """Provide the real transaction through the production async adapter."""
        yield SyncApprovalSession(db_session)

    monkeypatch.setattr(service, "get_async_db_session", session)
    monkeypatch.setattr(evaluator, "_log_policy_decision_async", MagicMock())
    decision, reason, request_id, _ = await service.request_agent_permission(
        base_url="https://example.com",
        account_id=str(test_user.account_id),
        user_id=test_user.id,
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name=None,
        source="claude_code",
        tool_name="mcp__docs__fetch",
        tool_input={},
        agent_reasoning=None,
        client_decision="allow",
    )
    assert decision == "deny" and request_id is None
    assert "unknown_tools=deny" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["deny", "require_approval"])
async def test_hook_honours_mcp_scoped_source(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """protects=MCP-scoped hook rules; fails_when=client fallback discards them;
    no_claim=evaluator mocked to isolate the hook's decision-source filter.
    """
    from preloop.services.agent_permission_service import apply_native_access_rules

    decision = evaluator.PolicyDecision(
        action, uuid4(), "Scoped MCP rule", source="scoped_rule", stored_source="mcp"
    )
    monkeypatch.setattr(
        evaluator, "evaluate_policy_async", AsyncMock(return_value=decision)
    )
    result = await apply_native_access_rules(
        AsyncMock(),
        config=None,
        tool_name="mcp__docs__fetch",
        tool_input={},
        account_id=str(uuid4()),
        user_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
    )
    assert result is not None and result[0] == action


@pytest.mark.asyncio
async def test_replay_includes_original_subject_scopes(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=subject denies added after approval block replay;
    fails_when=missing subject lets a scoped deny pass; no_claim=request lookup mocked, real evaluator/DB.
    """
    from contextlib import asynccontextmanager
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.dynamic_fastmcp import DynamicFastMCP, _approved_id_var
    from preloop.services.subject_governance import set_subject_governance

    account_id, agent_id, server_id = test_user.account_id, uuid4(), uuid4()
    request = SimpleNamespace(
        account_id=account_id,
        status="approved",
        tool_name="fetch",
        tool_configuration_id=uuid4(),
        approval_workflow_id=uuid4(),
        managed_agent_id=agent_id,
        api_key_id=None,
        runtime_session_id=None,
        execution_id=None,
    )
    test_user.account.meta_data = set_subject_governance(
        {},
        subject_type="managed_agents",
        subject_id=str(agent_id),
        config={
            "tool_rules": {
                "fetch": [
                    {"source": "mcp", "action": "deny", "condition_expression": "true"}
                ]
            }
        },
    )
    db_session.flush()

    @asynccontextmanager
    async def session() -> AsyncIterator[SyncApprovalSession]:
        """Use real account rules for the replay evaluation."""
        yield SyncApprovalSession(db_session)

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(
        "preloop.models.crud.approval_request.get_approval_request_async",
        AsyncMock(return_value=request),
    )
    monkeypatch.setattr(
        "preloop.models.crud.tool_configuration.get_tool_config_by_id_async",
        AsyncMock(return_value=SimpleNamespace(mcp_server_id=server_id)),
    )
    monkeypatch.setattr(evaluator, "_log_policy_decision_async", MagicMock())
    mcp = DynamicFastMCP("subject-replay")
    mcp._proxied_tool_servers["fetch"] = str(server_id)
    token = _approved_id_var.set(str(uuid4()))
    try:
        assert "blocks approval replay" in await mcp._approval_replay_denial(
            str(account_id), "fetch", {}
        )
    finally:
        _approved_id_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,collision", [(None, False), ("docs", False), (None, True)]
)
async def test_legacy_replay_resolves_only_an_unambiguous_route(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str | None,
    collision: bool,
) -> None:
    """protects=in-flight legacy approvals survive with one provable route;
    fails_when=NULL config grants a colliding server; no_claim=request lookup mocked, inventory/CRUD real.
    """
    from contextlib import asynccontextmanager
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.dynamic_fastmcp import DynamicFastMCP, _approved_id_var

    servers = [
        models.MCPServer(
            account_id=test_user.account_id,
            name=f"docs-{i}",
            url="https://example.com/mcp",
            transport="http-streaming",
            auth_type="none",
            status="active",
            tool_prefix=prefix,
        )
        for i in range(2 if collision else 1)
    ]
    db_session.add_all(servers)
    db_session.flush()
    for server in servers:
        db_session.add(
            models.MCPTool(
                mcp_server_id=server.id,
                name="fetch",
                input_schema={},
                discovered_at="2026-11-01T10:00:00Z",
            )
        )
    tool_name = f"{prefix}_fetch" if prefix else "fetch"
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name=tool_name,
        tool_source="mcp",
        mcp_server_id=None,
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()
    request = SimpleNamespace(
        account_id=test_user.account_id,
        status="approved",
        tool_name=tool_name,
        tool_configuration_id=config.id,
        approval_workflow_id=uuid4(),
        execution_id=None,
        api_key_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
    )

    @asynccontextmanager
    async def session() -> AsyncIterator[SyncApprovalSession]:
        """Share the real inventory transaction with the replay guard."""
        yield SyncApprovalSession(db_session)

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(
        "preloop.models.crud.approval_request.get_approval_request_async",
        AsyncMock(return_value=request),
    )
    monkeypatch.setattr(evaluator, "_log_policy_decision_async", MagicMock())
    mcp = DynamicFastMCP("legacy-replay")
    mcp._proxied_tool_servers[tool_name] = str(servers[0].id)
    token = _approved_id_var.set(str(uuid4()))
    try:
        denial = await mcp._approval_replay_denial(
            str(test_user.account_id), tool_name, {}
        )
        if collision:
            assert "route changed" in denial
        else:
            assert denial is None
    finally:
        _approved_id_var.reset(token)


def test_default_approval_context_has_a_visible_reason() -> None:
    """protects=approver sees why an unlisted call is held;
    fails_when=missing label hides the block; no_claim=no browser or notification.
    """
    decision = evaluator._unmatched_policy_decision(
        account_meta={"policy_defaults": {"unknown_tools": "require_approval"}},
        tool_source="mcp",
        tool_config=None,
        decision=evaluator.PolicyDecision("allow", None, "Unlisted", matched=False),
        workflow=SimpleNamespace(id=uuid4()),
    )
    assert decision.rule_context["source"] == "unknown_tools_default"
    assert decision.rule_context["rule_name"] == "Unlisted MCP tool"
    assert "No access rule matched" in decision.rule_context["explanation"]
