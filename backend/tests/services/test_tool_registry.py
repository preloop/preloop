"""Approve-once registry persistence and lifecycle regression tests."""

import re
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any, AsyncIterator
from sqlalchemy import Engine

import pytest
from sqlalchemy.orm import Session

from preloop.models import models


def test_registry_idempotency_and_account_isolation(
    db_session: Session, test_user: models.User
) -> None:
    """protects=NULL-route uniqueness and counts; fails_when=duplicate grants;
    no_claim=sequential PostgreSQL calls, concurrency covered separately.
    """
    from preloop.models.crud import tool_registry

    identity = dict(
        account_id=test_user.account_id,
        source="mcp",
        server_id=None,
        server_label="docs",
        tool_name="fetch",
        agent_id=None,
    )
    entry, created = tool_registry.get_or_create_seen(db_session, **identity)
    again, created_again = tool_registry.get_or_create_seen(db_session, **identity)
    assert created and not created_again and again.id == entry.id
    assert again.seen_count == 2
    assert entry.state == "pending"
    other, _ = tool_registry.get_or_create_seen(
        db_session, **{**identity, "server_label": "other"}
    )
    assert other.id != entry.id
    approved = tool_registry.decide(
        db_session,
        entry_id=entry.id,
        account_id=test_user.account_id,
        state="approved",
        user_id=test_user.id,
    )
    assert approved.state == "approved" and approved.decided_by_user_id == test_user.id
    assert (
        tool_registry.decide(
            db_session,
            entry_id=uuid4(),
            account_id=test_user.account_id,
            state="denied",
            user_id=test_user.id,
        )
        is None
    )


@pytest.fixture
def registry_environment(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> models.ApprovalWorkflow:
    """Use real persistence and mock only notification/model delivery."""
    from unittest.mock import AsyncMock, MagicMock
    from preloop.models.crud.policy_defaults import replace
    from preloop.services.approval_service import ApprovalService

    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Review",
        approval_type="manual",
        timeout_seconds=300,
    )
    db_session.add(workflow)
    db_session.flush()
    replace(
        db_session,
        test_user.account_id,
        {
            "unknown_tools": "deny",
            "require_approval_for_new_tools": True,
            "default_approval_workflow": "Review",
        },
    )
    for method in (
        "send_notifications",
        "_broadcast_pending",
        "_broadcast_approval_update",
        "_enqueue_approval_webhook",
        "_enqueue_lifecycle",
        "_release_parked_executions",
    ):
        monkeypatch.setattr(ApprovalService, method, AsyncMock())
    monkeypatch.setattr(
        "preloop.services.approval_summary.generate_approval_summary",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator._log_policy_decision_async", MagicMock()
    )
    return workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("state,action", [("approved", "allow"), ("declined", "deny")])
async def test_pending_request_is_shared_and_resolution_changes_registry(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
    state: str,
    action: str,
) -> None:
    """protects=one request then durable decision; fails_when=repeat prompts or stale allow;
    no_claim=real evaluator/approval CRUD, notifications and summaries mocked.
    """
    from preloop.models.crud import tool_registry
    from preloop.models.db.session import SyncApprovalSession
    from preloop.models.schemas.approval_request import ApprovalRequestUpdate
    from preloop.services.approval_service import ApprovalService
    from preloop.services.policy_evaluator import evaluate_policy_async

    adapter = SyncApprovalSession(db_session)
    kwargs = dict(
        account_id=test_user.account_id,
        tool_name="mcp__docs__fetch",
        tool_args={},
        tool_source="mcp",
        server_label="docs",
        user_id=test_user.id,
    )
    first = await evaluate_policy_async(adapter, **kwargs)
    second = await evaluate_policy_async(adapter, **kwargs)
    assert first.action == second.action == "require_approval"
    first_id = first.rule_context["registry_approval_request_id"]
    assert first_id == second.rule_context["registry_approval_request_id"]
    [entry] = tool_registry.list_entries(db_session, account_id=test_user.account_id)
    assert entry.seen_count == 2 and str(entry.approval_request_id) == first_id
    await ApprovalService(adapter, "https://example.com").update_approval_request(
        entry.approval_request_id,
        ApprovalRequestUpdate(status=state),
        audit_approver_id=test_user.id,
    )
    after = await evaluate_policy_async(adapter, **kwargs)
    assert after.action == action and after.source == "registry"
    assert (
        tool_registry.get(
            db_session, account_id=test_user.account_id, entry_id=entry.id
        ).decided_by_user_id
        == test_user.id
    )
    tool_registry.decide(
        db_session,
        account_id=test_user.account_id,
        entry_id=entry.id,
        state="denied",
        user_id=test_user.id,
    )
    assert (await evaluate_policy_async(adapter, **kwargs)).action == "deny"


@pytest.mark.asyncio
async def test_expiry_replaces_pending_approval(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
) -> None:
    """protects=expired request can be renewed; fails_when=expiry grants access;
    no_claim=real approval lifecycle, delivery mocked.
    """
    from preloop.models.db.session import SyncApprovalSession
    from preloop.models.schemas.approval_request import ApprovalRequestUpdate
    from preloop.services.approval_service import ApprovalService
    from preloop.services.policy_evaluator import evaluate_policy_async

    adapter = SyncApprovalSession(db_session)
    kwargs = dict(
        account_id=test_user.account_id,
        tool_name="mcp__docs__fetch",
        tool_args={},
        tool_source="mcp",
        server_label="docs",
    )
    first = await evaluate_policy_async(adapter, **kwargs)
    first_id = first.rule_context["registry_approval_request_id"]
    await ApprovalService(adapter, "https://example.com").update_approval_request(
        first_id, ApprovalRequestUpdate(status="expired")
    )
    again = await evaluate_policy_async(adapter, **kwargs)
    assert again.action == "require_approval"
    assert again.rule_context["registry_approval_request_id"] != first_id


@pytest.mark.asyncio
async def test_manual_decision_resolves_open_request(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
) -> None:
    """protects=console and approval agree; fails_when=manual denial leaves open grant;
    no_claim=real endpoint/service/DB, notification delivery mocked.
    """
    from preloop.api.endpoints.tool_registry import RegistryDecision, decide_registry
    from preloop.models.crud import tool_registry
    from preloop.models.crud.approval_request import get_approval_request_async
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.policy_evaluator import evaluate_policy_async

    adapter = SyncApprovalSession(db_session)
    await evaluate_policy_async(
        adapter,
        account_id=test_user.account_id,
        tool_name="mcp__docs__fetch",
        tool_args={},
        tool_source="mcp",
        server_label="docs",
    )
    [entry] = tool_registry.list_entries(db_session, account_id=test_user.account_id)
    result = await asyncio.to_thread(
        decide_registry,
        entry.id,
        RegistryDecision(state="denied"),
        account=test_user.account,
        current_user=test_user,
        db=db_session,
    )
    assert result.state == "denied"
    request = await get_approval_request_async(
        adapter, request_id=entry.approval_request_id
    )
    assert request.status == "declined"


@pytest.mark.asyncio
async def test_registry_skip_simulation_and_explicit_rule_precedence(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
) -> None:
    """protects=rules override registry and settings preserve entries;
    fails_when=simulation writes or disable deletes; no_claim=no transport.
    """
    from preloop.models.crud import tool_registry
    from preloop.models.crud.policy_defaults import replace
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.policy_evaluator import evaluate_policy, evaluate_policy_async

    args = dict(
        account_id=test_user.account_id,
        tool_name="mcp__docs__fetch",
        tool_args={},
        tool_source="mcp",
        server_label="docs",
    )
    # Sync simulation is also used by async API handlers.
    assert (
        evaluate_policy(db_session, **args, record=False).action == "require_approval"
    )
    assert tool_registry.list_entries(db_session, account_id=test_user.account_id) == []
    evaluate_policy(db_session, **args)
    [entry] = tool_registry.list_entries(db_session, account_id=test_user.account_id)
    assert entry.tool_name == "fetch" and entry.server_label == "docs"
    tool_registry.decide(
        db_session,
        account_id=test_user.account_id,
        entry_id=entry.id,
        state="approved",
        user_id=test_user.id,
    )
    replace(db_session, test_user.account_id, {"unknown_tools": "deny"})
    assert (
        await evaluate_policy_async(SyncApprovalSession(db_session), **args)
    ).action == "deny"
    assert (
        tool_registry.get(
            db_session, account_id=test_user.account_id, entry_id=entry.id
        ).state
        == "approved"
    )
    replace(
        db_session,
        test_user.account_id,
        {
            "unknown_tools": "deny",
            "require_approval_for_new_tools": True,
            "default_approval_workflow": "Review",
        },
    )
    assert (
        await evaluate_policy_async(SyncApprovalSession(db_session), **args)
    ).action == "allow"
    [config] = (
        db_session.query(models.ToolConfiguration)
        .filter_by(account_id=test_user.account_id, tool_name=args["tool_name"])
        .all()
    )
    db_session.add(
        models.ToolAccessRule(
            tool_configuration_id=config.id,
            account_id=test_user.account_id,
            action="deny",
            condition_type="simple",
            condition_expression="true",
            priority=0,
            is_enabled=True,
        )
    )
    db_session.flush()
    decision = await evaluate_policy_async(SyncApprovalSession(db_session), **args)
    assert decision.action == "deny" and decision.matched


def test_concurrent_first_calls_share_request(
    db_engine: Engine,
    registry_environment: models.ApprovalWorkflow,
) -> None:
    """protects=one request for concurrent NULL-server routes;
    fails_when=two requests escape; no_claim=local PostgreSQL, delivery mocked.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import select, delete
    from preloop.models.crud import crud_account
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.policy_evaluator import evaluate_policy_async

    with Session(db_engine) as db:
        account = crud_account.create(db, obj_in={"organization_name": "Example"})
        account_id = account.id
        account.meta_data = {
            "policy_defaults": {
                "unknown_tools": "deny",
                "require_approval_for_new_tools": True,
                "default_approval_workflow": "Review",
            }
        }
        db.add(
            models.ApprovalWorkflow(
                account_id=account_id,
                name="Review",
                approval_type="manual",
                timeout_seconds=300,
            )
        )
        db.commit()
    barrier = Barrier(2)

    def call() -> str:
        """Race independent transactions at the evaluator boundary."""
        with Session(db_engine) as db:
            barrier.wait(timeout=10)
            result = asyncio.run(
                evaluate_policy_async(
                    SyncApprovalSession(db),
                    account_id=account_id,
                    tool_name="mcp__docs__fetch",
                    tool_args={},
                    tool_source="mcp",
                    server_label="docs",
                )
            )
            return result.rule_context["registry_approval_request_id"]

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda _: call(), range(2)))
        assert a == b
        with Session(db_engine) as db:
            entries = list(
                db.scalars(
                    select(models.ToolRegistryEntry).where(
                        models.ToolRegistryEntry.account_id == account_id
                    )
                )
            )
            requests = list(
                db.scalars(
                    select(models.ApprovalRequest).where(
                        models.ApprovalRequest.account_id == account_id
                    )
                )
            )
            assert len(entries) == len(requests) == 1
            assert entries[0].seen_count == 2
    finally:
        with Session(db_engine) as db:
            db.execute(delete(models.Account).where(models.Account.id == account_id))
            db.commit()


@pytest.mark.parametrize("status", ["declined", "cancelled", "expired"])
def test_shared_registry_park_refusal_is_execution_gate(status: str) -> None:
    """protects=shared registry refusals fail closed on native replay;
    fails_when=markerless shared requests resume; no_claim=no live orchestrator.
    """
    from types import SimpleNamespace
    from preloop.services.native_flow_park import is_execution_gate, refused_or_expired

    request = SimpleNamespace(
        tool_args={}, rule_context={"source": "registry"}, status=status
    )
    assert is_execution_gate(request) and refused_or_expired(request)


def test_registry_existing_route_performance(
    db_session: Session,
    test_user: models.User,
) -> None:
    """protects=10k-route lookup stays indexed and touches a bounded page count;
    fails_when=sequential scans or a lookup reads pages in proportion to the table;
    no_claim=wall-clock latency, which depends on runner load and is only printed.
    """
    from datetime import datetime, timezone
    from time import perf_counter
    from sqlalchemy import insert, text
    from preloop.models.crud import tool_registry

    now = datetime.now(timezone.utc)
    db_session.execute(
        insert(models.ToolRegistryEntry),
        [
            dict(
                id=uuid4(),
                account_id=test_user.account_id,
                tool_source="mcp",
                server_label="docs",
                tool_name=f"fetch_{i}",
                state="approved",
                first_seen_at=now,
                last_seen_at=now,
                seen_count=1,
            )
            for i in range(10_000)
        ],
    )
    db_session.execute(text("ANALYZE tool_registry_entry"))
    samples = []
    for i in range(1000):
        start = perf_counter()
        entry, created = tool_registry.get_or_create_seen(
            db_session,
            account_id=test_user.account_id,
            source="mcp",
            server_id=None,
            server_label="docs",
            tool_name=f"fetch_{i}",
            agent_id=None,
        )
        samples.append((perf_counter() - start) * 1000)
        assert not created and entry.state == "approved"
    p95 = sorted(samples)[949]
    plan = "\n".join(
        row[0]
        for row in db_session.execute(
            text(
                "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM tool_registry_entry "
                "WHERE account_id=:account AND tool_source='mcp' "
                "AND mcp_server_id IS NULL AND server_label='docs' AND tool_name='fetch_1'"
            ),
            {"account": test_user.account_id},
        )
    )
    print(f"registry existing-route p95={p95:.3f} ms\n{plan}")
    assert "Index Scan using uq_tool_registry_entry" in plan
    # A unique-index probe on 10k rows reads a few pages (btree root, leaf,
    # heap). A sequential scan reads the whole heap, well over 100 pages.
    # Page counts do not change with runner load the way p95 latency does.
    pages = sum(
        int(n)
        for n in re.findall(r"shared (?:hit|read)=(\d+)", plan.split("Planning")[0])
    )
    assert 0 < pages <= 16, plan


@pytest.mark.asyncio
async def test_registry_api_account_filter_and_missing_decision(
    db_session: Session,
    test_user: models.User,
) -> None:
    """protects=account isolation and list state pagination;
    fails_when=foreign id can be decided; no_claim=RBAC supplied by plugin, not mocked here.
    """
    from fastapi import HTTPException
    from preloop.api.endpoints.tool_registry import (
        list_registry,
        decide_registry,
        RegistryDecision,
    )
    from preloop.models.crud import tool_registry, crud_account

    identity = dict(source="mcp", server_id=None, server_label="docs", agent_id=None)
    for name in ["fetch", "search"]:
        tool_registry.get_or_create_seen(
            db_session, account_id=test_user.account_id, tool_name=name, **identity
        )
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other Example"}
    )
    entry, _ = tool_registry.get_or_create_seen(
        db_session, account_id=foreign.id, tool_name="fetch", **identity
    )
    own = list_registry(
        state="pending",
        page=0,
        page_size=1,
        account=test_user.account,
        current_user=test_user,
        db=db_session,
    )
    assert len(own) == 1 and own[0].id != entry.id
    next_page = list_registry(
        state="pending",
        page=1,
        page_size=1,
        account=test_user.account,
        current_user=test_user,
        db=db_session,
    )
    assert len(next_page) == 1 and own[0].id != next_page[0].id
    with pytest.raises(HTTPException) as error:
        await asyncio.to_thread(
            decide_registry,
            entry.id,
            RegistryDecision(state="approved"),
            account=test_user.account,
            current_user=test_user,
            db=db_session,
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_hook_reuses_registry_request_when_parking(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=hook parks on the shared request despite client allow;
    fails_when=duplicate request or client bypass; no_claim=park orchestration mocked.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from preloop.models.crud import tool_registry
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services import agent_permission_service as service, native_flow_park

    @asynccontextmanager
    async def session() -> AsyncIterator[SyncApprovalSession]:
        """Use the real database through the shared async service adapter."""
        yield SyncApprovalSession(db_session)

    monkeypatch.setattr(service, "get_async_db_session", session)
    monkeypatch.setattr(
        service, "resolve_workflow", AsyncMock(return_value=registry_environment)
    )
    monkeypatch.setattr(native_flow_park, "pending_park_reason", lambda _: None)
    monkeypatch.setattr(native_flow_park, "consume_grant", lambda **_: None)
    monkeypatch.setattr(
        native_flow_park, "resolve_window", lambda *_: SimpleNamespace(seconds=300)
    )
    parked_calls: list[dict[str, Any]] = []

    def park(**kwargs: Any) -> bool:
        """Capture the actual expiry passed to the parking service."""
        parked_calls.append(kwargs)
        return True

    monkeypatch.setattr(native_flow_park, "park", park)
    flow = models.Flow(
        account_id=test_user.account_id,
        created_by_user_id=test_user.id,
        name="Example",
        prompt_template="Review",
        agent_config={},
        approval_window_seconds=172800,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(execution)
    db_session.flush()
    key = models.ApiKey(
        account_id=test_user.account_id,
        user_id=test_user.id,
        name="Example credential",
        key="synthetic-registry-test-credential",
    )
    db_session.add(key)
    db_session.flush()
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await service.request_agent_permission(
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
        api_key_id=key.id,
        flow_execution_id=execution.id,
    )
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    [entry] = tool_registry.list_entries(db_session, account_id=test_user.account_id)
    assert result[0] == "park" and result[2] == str(entry.approval_request_id)
    assert (
        db_session.query(models.ApprovalRequest)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 1
    )

    request = db_session.get(models.ApprovalRequest, entry.approval_request_id)
    assert str(request.execution_id) == str(execution.id)
    assert request.api_key_id == key.id
    assert parked_calls[0]["expires_at"] == request.expires_at
    # created_at is PostgreSQL's transaction start, which can precede this
    # request's deadline calculation by arbitrary setup/summary work.
    assert before + timedelta(days=2) <= request.expires_at <= after + timedelta(days=2)


@pytest.mark.asyncio
async def test_registry_replay_rechecks_revocation_and_errors(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=approved async replay cannot bypass revocation or DB errors;
    fails_when=upstream dispatch becomes reachable; no_claim=no live MCP network.
    """
    from contextlib import asynccontextmanager
    from preloop.models.crud import tool_registry
    from preloop.models.db.session import SyncApprovalSession
    from preloop.models.schemas.approval_request import ApprovalRequestUpdate
    from preloop.services.approval_service import ApprovalService
    from preloop.services.dynamic_fastmcp import DynamicFastMCP, _approved_id_var
    from preloop.services.policy_evaluator import evaluate_policy_async

    server = models.MCPServer(
        account_id=test_user.account_id,
        name="docs",
        url="https://example.com/mcp",
        transport="http-streaming",
        auth_type="none",
    )
    db_session.add(server)
    db_session.flush()
    adapter = SyncApprovalSession(db_session)
    decision = await evaluate_policy_async(
        adapter,
        account_id=test_user.account_id,
        tool_name="docs_fetch",
        tool_args={},
        tool_source="mcp",
        mcp_server_id=server.id,
    )
    request_id = decision.rule_context["registry_approval_request_id"]
    await ApprovalService(adapter, "https://example.com").update_approval_request(
        request_id,
        ApprovalRequestUpdate(status="approved"),
        audit_approver_id=test_user.id,
    )

    @asynccontextmanager
    async def session() -> AsyncIterator[SyncApprovalSession]:
        """Keep the replay guard on the real account transaction."""
        yield adapter

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    mcp = DynamicFastMCP("registry-replay")
    mcp._proxied_tool_servers["docs_fetch"] = str(server.id)
    token = _approved_id_var.set(request_id)
    try:
        assert (
            await mcp._approval_replay_denial(
                str(test_user.account_id), "docs_fetch", {}
            )
            is None
        )
        [entry] = tool_registry.list_entries(
            db_session, account_id=test_user.account_id
        )
        tool_registry.decide(
            db_session,
            account_id=test_user.account_id,
            entry_id=entry.id,
            state="denied",
            user_id=test_user.id,
        )
        assert "blocks approval replay" in await mcp._approval_replay_denial(
            str(test_user.account_id), "docs_fetch", {}
        )

        def broken(*args: object, **kwargs: object) -> None:
            """Inject an unavailable registry, never a permissive fallback."""
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(tool_registry, "find", broken)
        assert "policy check failed" in await mcp._approval_replay_denial(
            str(test_user.account_id), "docs_fetch", {}
        )
    finally:
        _approved_id_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["allow", "deny", "require_approval", "registry"])
async def test_proxy_dispatch_obeys_defaults_and_registry(
    db_session: Session,
    test_user: models.User,
    registry_environment: models.ApprovalWorkflow,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """protects=real firewall dispatch and approval lifecycle enforce defaults;
    fails_when=unlisted/pending/revoked tool reaches upstream;
    no_claim=MCP network, notification/model delivery and independent session close mocked.
    """
    from contextlib import asynccontextmanager
    from preloop.models.crud import tool_registry
    from preloop.models.crud.policy_defaults import replace
    from preloop.models.db.session import SyncApprovalSession
    from preloop.models.schemas.approval_request import ApprovalRequestUpdate
    from preloop.services import approval_helper, policy_evaluator
    from preloop.services.approval_service import ApprovalService
    from preloop.services.dynamic_mcp_server import UserContext
    from tests.services.test_proxied_tool_error_audit import _setup

    real_evaluator = policy_evaluator.evaluate_policy_async
    real_require = approval_helper.require_approval
    user_context = UserContext(
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username="testuser",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )
    mcp, client, _, server = _setup(monkeypatch, user_context)
    flow = models.Flow(
        account_id=test_user.account_id,
        created_by_user_id=test_user.id,
        name="Example",
        prompt_template="Review",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(execution)
    db_session.flush()
    user_context.flow_id = str(flow.id)
    user_context.flow_execution_id = str(execution.id)
    row = models.MCPServer(
        account_id=test_user.account_id,
        name="docs",
        url="https://example.com/mcp",
        transport="http-streaming",
        auth_type="none",
    )
    db_session.add(row)
    db_session.flush()
    server.id = str(row.id)
    mcp._proxied_tool_servers["safe_tool"] = str(row.id)
    registry_environment.async_approval_enabled = True
    replace(
        db_session,
        test_user.account_id,
        {
            "unknown_tools": "deny" if mode == "registry" else mode,
            "require_approval_for_new_tools": mode == "registry",
            "default_approval_workflow": "Review",
        },
    )

    class Adapter(SyncApprovalSession):
        """The test owns the outer transaction, so close only releases its wrapper."""

        async def close(self) -> None:
            """Keep the transaction available for lifecycle assertions."""

    adapter = Adapter(db_session)

    @asynccontextmanager
    async def session() -> AsyncIterator[Adapter]:
        """Use real CRUD on one fixture transaction."""
        yield adapter

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(policy_evaluator, "evaluate_policy_async", real_evaluator)
    monkeypatch.setattr(approval_helper, "require_approval", real_require)
    storage_scopes: list[str | None] = []
    original_storage = ApprovalService._storage_redacted_tool_args

    async def store(self: ApprovalService, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Observe the real redaction scope without replacing redaction."""
        storage_scopes.append(kwargs.get("server_name"))
        return await original_storage(self, *args, **kwargs)

    monkeypatch.setattr(ApprovalService, "_storage_redacted_tool_args", store)
    result = await mcp.call_tool("safe_tool", {"ok": "yes"})
    if mode == "allow":
        client.call_tool.assert_awaited_once()
        return
    client.call_tool.assert_not_awaited()
    if mode == "deny":
        assert "unknown_tools=deny" in str(result)
        return
    requests = (
        db_session.query(models.ApprovalRequest)
        .filter_by(account_id=test_user.account_id)
        .all()
    )
    assert (
        len(requests) == 1
        and requests[0].approval_workflow_id == registry_environment.id
    )
    assert requests[0].status == "pending"
    if mode == "registry":
        assert str(requests[0].execution_id) == str(execution.id)
    assert "upstream" in storage_scopes
    if mode == "registry":
        await mcp.call_tool("safe_tool", {"ok": "yes"})
        assert (
            db_session.query(models.ApprovalRequest)
            .filter_by(account_id=test_user.account_id)
            .count()
            == 1
        )
        await ApprovalService(adapter, "https://example.com").update_approval_request(
            requests[0].id,
            ApprovalRequestUpdate(status="approved"),
            audit_approver_id=test_user.id,
        )
        await mcp.call_tool("safe_tool", {"ok": "yes"})
        client.call_tool.assert_awaited_once()
        [entry] = tool_registry.list_entries(
            db_session, account_id=test_user.account_id
        )
        tool_registry.decide(
            db_session,
            account_id=test_user.account_id,
            entry_id=entry.id,
            state="denied",
            user_id=test_user.id,
        )
        await mcp.call_tool("safe_tool", {"ok": "yes"})
        client.call_tool.assert_awaited_once()


@pytest.mark.parametrize("status", ["approved", "declined", "expired"])
def test_registry_resume_does_not_share_caller_arguments(status: str) -> None:
    """protects=each shared observer resumes its own call without argument leakage;
    fails_when=first caller command enters another run; no_claim=runtime resume mocked.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    from preloop.services.approval_park import answer_from_request, answers_prompt_block
    from preloop.services.native_flow_park import fail_closed

    request = SimpleNamespace(
        id="shared-request",
        status=status,
        account_id="account",
        tool_name="mcp__docs__fetch",
        tool_args={
            "command": "first-caller-private-argument",
            "question": "private-question",
        },
        rule_context={"source": "registry"},
    )
    for execution_id in ("first-run", "second-run"):
        answer = answer_from_request(request)
        prompt = answers_prompt_block(answer)
        assert "first-caller-private-argument" not in str(answer) + prompt
        assert "private-question" not in str(answer) + prompt
        assert "native_command" not in answer
        if status == "approved":
            assert "your own original tool call" in prompt
            assert "get_approval_status" not in prompt
        else:
            with patch(
                "preloop.models.crud.crud_flow_execution.fail_parked_for_request",
                return_value=False,
            ) as fail:
                fail_closed(None, SimpleNamespace(id=execution_id), request)
                assert fail.call_args.kwargs["execution_id"] == execution_id
                assert (
                    "first-caller-private-argument"
                    not in fail.call_args.kwargs["error_message"]
                )
