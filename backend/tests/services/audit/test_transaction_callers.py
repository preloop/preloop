"""Business callers must admit audit before their transaction commits."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestUpdate
from preloop.services import approval_service, policy_evaluator
from preloop.services.audit import service as audit


@pytest.mark.asyncio
async def test_approval_decision_enqueues_before_commit(monkeypatch: Any) -> None:
    monkeypatch.setenv("PRELOOP_AUDIT_OUTBOX_ENABLED", "true")
    sync_db = MagicMock()
    db = AsyncMock()
    db.run_sync.side_effect = lambda fn: fn(sync_db)
    request = models.ApprovalRequest(
        id=uuid4(), account_id=uuid4(), tool_name="deploy", status="pending"
    )
    service = approval_service.ApprovalService(db, "https://example.com")
    service.get_approval_request = AsyncMock(return_value=request)
    service._release_parked_executions = AsyncMock()
    seen = []
    audit_service = audit.AuditService()
    monkeypatch.setattr(approval_service, "_get_audit_service", lambda: audit_service)
    monkeypatch.setattr(
        audit_service,
        "enqueue_in_transaction",
        lambda session, op, **fields: seen.append((session, op, fields)),
    )

    async def commit() -> None:
        assert len(seen) == 1
        assert seen[0][0] is sync_db
        assert seen[0][1] == "log_approval_lifecycle"
        assert seen[0][2]["event"] == "denied"
        assert seen[0][2]["priority"] is True

    db.commit.side_effect = commit
    await service.update_approval_request(
        request.id, ApprovalRequestUpdate(status="declined", approver_comment="No")
    )
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_approval_enqueue_failure_prevents_commit(monkeypatch: Any) -> None:
    monkeypatch.setenv("PRELOOP_AUDIT_OUTBOX_ENABLED", "true")
    db = AsyncMock()
    db.run_sync.side_effect = lambda fn: fn(MagicMock())
    service = approval_service.ApprovalService(db, "https://example.com")
    request = models.ApprovalRequest(
        id=uuid4(), account_id=uuid4(), tool_name="deploy", status="pending"
    )
    service.get_approval_request = AsyncMock(return_value=request)
    service._release_parked_executions = AsyncMock()
    audit_service = audit.AuditService()
    monkeypatch.setattr(approval_service, "_get_audit_service", lambda: audit_service)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(audit_service, "enqueue_in_transaction", fail)
    with pytest.raises(RuntimeError, match="insert failed"):
        await service.update_approval_request(
            request.id, ApprovalRequestUpdate(status="approved")
        )
    db.commit.assert_not_awaited()
    service._release_parked_executions.assert_not_awaited()


def test_sync_policy_uses_caller_transaction(monkeypatch: Any, db_session: Any) -> None:
    from preloop.models.crud import crud_account

    monkeypatch.setenv("PRELOOP_AUDIT_OUTBOX_ENABLED", "true")
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Audit example", "is_active": True}
    )
    audit_service = audit.AuditService()
    monkeypatch.setattr(policy_evaluator, "_get_audit_service", lambda: audit_service)
    decision = policy_evaluator.evaluate_policy(
        db_session, "example_tool", {}, account.id
    )
    assert decision.action == "allow"
    rows = db_session.query(models.AuditOutbox).filter_by(account_id=account.id).all()
    assert len(rows) == 1
    assert rows[0].operation == "log_policy_decision"
    assert rows[0].priority is True


@pytest.fixture
def committed_data(db_engine: Any, monkeypatch: Any) -> Any:
    """Real commits, observed through independent connections (no savepoints)."""
    from types import SimpleNamespace
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setenv("PRELOOP_AUDIT_OUTBOX_ENABLED", "true")
    # Lifecycle tests elsewhere can disable the process singleton. These
    # transaction tests own an enabled service regardless of collection order.
    monkeypatch.setattr(audit, "_audit_service", audit.AuditService())
    monkeypatch.setattr(policy_evaluator, "submit_off_loop", lambda fn: None)
    with factory() as db:
        account = models.Account(organization_name="Outbox example", is_active=True)
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username=f"audit-{uuid4()}",
            email=f"audit-{uuid4()}@example.com",
            hashed_password="test-only",
        )
        workflow = models.ApprovalWorkflow(
            account_id=account.id,
            name="Audit example",
            approval_type="manual",
        )
        tool = models.ToolConfiguration(
            account_id=account.id,
            tool_name="example_tool",
            tool_source="builtin",
        )
        runtime = models.RuntimeSession(
            account_id=account.id,
            session_source_type="test",
            session_source_id=str(uuid4()),
            started_at=datetime.utcnow(),
        )
        db.add_all([user, workflow, tool, runtime])
        db.commit()
        data = SimpleNamespace(
            factory=factory,
            account_id=account.id,
            user_id=user.id,
            workflow_id=workflow.id,
            tool_id=tool.id,
            runtime_id=runtime.id,
        )
    try:
        yield data
    finally:
        with factory() as db:
            db.query(models.AuditLog).filter_by(account_id=data.account_id).delete()
            db.query(models.Account).filter_by(id=data.account_id).delete(
                synchronize_session=False
            )
            db.commit()


def approval_for(db: Any, monkeypatch: Any) -> Any:
    from preloop.models.db.session import SyncApprovalSession

    service = approval_service.ApprovalService(
        SyncApprovalSession(db), "https://example.com"
    )
    monkeypatch.setattr(service, "_broadcast_approval_update", AsyncMock())
    monkeypatch.setattr(service, "_enqueue_approval_webhook", AsyncMock())
    monkeypatch.setattr(service, "_release_parked_executions", AsyncMock())
    return service


async def create_request(service: Any, data: Any) -> Any:
    return await service.create_approval_request(
        account_id=str(data.account_id),
        tool_configuration_id=data.tool_id,
        approval_workflow_id=data.workflow_id,
        tool_name="example_tool",
        tool_args={},
    )


@pytest.mark.asyncio
async def test_creation_and_decision_commit_intents_together(
    committed_data: Any, monkeypatch: Any
) -> None:
    data = committed_data
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        await service.approve_request(
            request.id, user_id=data.user_id, comment="Approved"
        )
        request_id = request.id
    with data.factory() as observer:
        request = observer.get(models.ApprovalRequest, request_id)
        assert request.status == "approved"
        assert len(request.responses) == 1
        rows = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .order_by(models.AuditOutbox.seq)
            .all()
        )
        assert [r.payload["event"] for r in rows] == ["created", "approved"]
        assert all(r.priority for r in rows)
        from preloop.services.audit.outbox import decode_payload

        assert decode_payload(rows[1].payload)["approver_id"] == data.user_id
        assert rows[1].payload["extra_details"]["approval_count"] == 1
        assert (
            observer.query(models.AuditLog)
            .filter_by(account_id=data.account_id)
            .count()
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("on_creation", [True, False])
async def test_failed_commit_rolls_back_business_and_intent(
    committed_data: Any, monkeypatch: Any, on_creation: bool
) -> None:
    data = committed_data
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = None if on_creation else await create_request(service, data)
        request_id = request.id if request else None

        def fail_commit() -> None:
            # INSERT already happened in this connection, but is invisible outside.
            assert db.query(models.AuditOutbox).filter_by(
                account_id=data.account_id
            ).count() == (1 if on_creation else 2)
            with data.factory() as observer:
                assert observer.query(models.AuditOutbox).filter_by(
                    account_id=data.account_id
                ).count() == (0 if on_creation else 1)
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            if on_creation:
                await create_request(service, data)
            else:
                await service.update_approval_request(
                    request_id, ApprovalRequestUpdate(status="declined")
                )
        db.rollback()
    with data.factory() as observer:
        assert observer.query(models.AuditOutbox).filter_by(
            account_id=data.account_id
        ).count() == (0 if on_creation else 1)
        if on_creation:
            assert (
                observer.query(models.ApprovalRequest)
                .filter_by(account_id=data.account_id)
                .count()
                == 0
            )
        else:
            assert observer.get(models.ApprovalRequest, request_id).status == "pending"


@pytest.mark.asyncio
async def test_committed_caller_intents_replay_in_new_process(
    committed_data: Any, monkeypatch: Any
) -> None:
    import os
    import subprocess
    import sys

    data = committed_data
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        await service.decline_request(request.id, comment="Denied")
    # A separate interpreter has no in-memory writer state or borrowed Session.
    script = """
from preloop.models.db.session import get_session_factory
from preloop.services.audit.service import AuditService
from preloop.services.audit.outbox import AuditOutboxRelay
relay = AuditOutboxRelay(session_factory=get_session_factory(), service_getter=AuditService)
print(relay.drain())
print(relay.drain())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-2:] == ["2", "0"]
    with data.factory() as observer:
        assert (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .count()
            == 0
        )
        assert (
            observer.query(models.AuditLog)
            .filter_by(account_id=data.account_id)
            .count()
            == 2
        )


@pytest.mark.asyncio
async def test_full_bounded_writer_does_not_drop_transactional_approval(
    committed_data: Any, monkeypatch: Any
) -> None:
    from threading import Event
    from preloop.services.audit import async_writer

    data = committed_data
    release, started = Event(), Event()
    writer = async_writer.BoundedAuditWriter(
        max_workers=1, max_pending=2, reserved_priority=1
    )
    monkeypatch.setattr(async_writer, "writer", writer)

    def blocked(db: Any) -> None:
        started.set()
        assert release.wait(15)

    try:
        writer.submit(None, MagicMock, blocked, {}, priority=False)
        assert started.wait(2)
        writer.submit(None, MagicMock, blocked, {}, priority=True)
        writer.submit(None, MagicMock, blocked, {}, priority=True)
        assert writer.pending_count == 2
        assert writer.dropped_count == 1
        with data.factory() as db:
            service = approval_for(db, monkeypatch)
            request = await create_request(service, data)
            await service.update_approval_request(
                request.id, ApprovalRequestUpdate(status="approved")
            )
        with data.factory() as observer:
            assert (
                observer.query(models.AuditOutbox)
                .filter_by(account_id=data.account_id)
                .count()
                == 2
            )
        assert writer.dropped_count == 1
    finally:
        release.set()
        writer._executor.shutdown(wait=True)


@pytest.mark.parametrize("commit", [True, False])
def test_policy_intent_follows_callers_business_write(
    committed_data: Any, commit: bool
) -> None:
    data = committed_data
    with data.factory() as db:
        account = db.get(models.Account, data.account_id)
        account.organization_name = "Updated atomically"
        policy_evaluator.evaluate_policy(db, "unknown_tool", {}, data.account_id)
        with data.factory() as observer:
            assert (
                observer.query(models.AuditOutbox)
                .filter_by(account_id=data.account_id)
                .count()
                == 0
            )
        db.commit() if commit else db.rollback()
    with data.factory() as observer:
        assert observer.query(models.AuditOutbox).filter_by(
            account_id=data.account_id
        ).count() == int(commit)
        assert (
            observer.get(models.Account, data.account_id).organization_name
            == "Updated atomically"
        ) is commit


@pytest.mark.asyncio
async def test_async_policy_flushes_using_owning_async_session(
    committed_data: Any,
) -> None:
    import os
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    data = committed_data
    engine = create_async_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
    )
    try:
        async with async_sessionmaker(engine)() as db:
            await policy_evaluator.evaluate_policy_async(
                db, "unknown_tool", {}, data.account_id
            )
            with data.factory() as observer:
                assert (
                    observer.query(models.AuditOutbox)
                    .filter_by(account_id=data.account_id)
                    .count()
                    == 0
                )
            await db.commit()
        with data.factory() as observer:
            assert (
                observer.query(models.AuditOutbox)
                .filter_by(account_id=data.account_id)
                .count()
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fail_enqueue", [False, True])
def test_mcp_activity_and_audit_share_commit(
    committed_data: Any, monkeypatch: Any, fail_enqueue: bool
) -> None:
    from preloop.services import dynamic_fastmcp
    from preloop.services.dynamic_mcp_server import UserContext
    from preloop.services.sensitive_data import storage
    from preloop.services.policy.schema import SensitiveDataConfig

    data = committed_data
    storage.prime_cache(data.account_id, SensitiveDataConfig())
    mcp = dynamic_fastmcp.DynamicFastMCP("audit-test")
    context = UserContext(
        user_id=str(data.user_id),
        account_id=str(data.account_id),
        username="example",
        has_tracker=False,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        runtime_session_id=str(data.runtime_id),
    )
    monkeypatch.setattr(dynamic_fastmcp, "get_db", lambda: iter([data.factory()]))
    monkeypatch.setattr(
        "preloop.services.account_realtime.emit_account_event", lambda event: None
    )
    service = audit.AuditService()
    monkeypatch.setattr("preloop.services.audit.get_audit_service", lambda: service)
    enqueue = service.enqueue_in_transaction

    def admit(db: Any, operation: Any, **fields: Any) -> None:
        enqueue(db, operation, **fields)
        if fail_enqueue:
            raise RuntimeError("admission failed")

    monkeypatch.setattr(service, "enqueue_in_transaction", admit)
    fields = dict(
        account_id=data.account_id,
        user_id=data.user_id,
        tool_name="example_tool",
        tool_args={},
        result="executed",
    )

    def persist() -> None:
        mcp._persist_tool_call_activity(
            context,
            tool_name="example_tool",
            client_tool_name="example_tool",
            status="succeeded",
            summary=None,
            arguments={},
            correlation_id="example-call",
            audit_fields=fields,
        )

    if fail_enqueue:
        with pytest.raises(RuntimeError, match="admission failed"):
            persist()
    else:
        persist()
    with data.factory() as observer:
        assert observer.query(models.RuntimeSessionActivity).filter_by(
            account_id=data.account_id
        ).count() == int(not fail_enqueue)
        assert observer.query(models.AuditOutbox).filter_by(
            account_id=data.account_id
        ).count() == int(not fail_enqueue)
    storage.invalidate_cache(data.account_id)


@pytest.mark.parametrize("action", ["deny", "require_approval", "notify"])
@pytest.mark.parametrize("fail_enqueue", [False, True])
def test_model_policy_hit_and_intent_are_atomic(
    committed_data: Any, monkeypatch: Any, action: Any, fail_enqueue: bool
) -> None:
    from preloop.services import model_content_policy, policy_notices
    from preloop.services.policy.schema import ModelIORule
    from preloop.services.audit.outbox import decode_payload

    data = committed_data
    notices = []
    monkeypatch.setattr(
        model_content_policy, "schedule_policy_match_hit", notices.append
    )
    monkeypatch.setattr(model_content_policy, "schedule_policy_notice", notices.append)
    rule = ModelIORule.model_validate(
        {
            "id": "example-rule",
            "target": "model.request",
            "approval_workflow": str(data.workflow_id)
            if action == "require_approval"
            else None,
            "conditions": [
                {"expression": "request.text.contains('example')", "action": action}
            ],
        }
    )
    decision = model_content_policy.evaluate_model_io(
        rules=[rule],
        target="model.request",
        text="example private content",
        account_id=data.account_id,
        user_id=data.user_id,
    )
    assert decision.action == action
    assert len(notices) == 1
    assert notices[0].audit_fields is not None
    assert "example private content" not in str(notices[0].audit_fields)
    service = audit.AuditService()
    monkeypatch.setattr(policy_evaluator, "_get_audit_service", lambda: service)
    enqueue = service.enqueue_in_transaction

    def admit(db: Any, operation: Any, **fields: Any) -> None:
        enqueue(db, operation, **fields)
        if fail_enqueue:
            raise RuntimeError("admission failed")

    monkeypatch.setattr(service, "enqueue_in_transaction", admit)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([data.factory()])
    )
    if action == "notify":
        with data.factory() as db:
            if fail_enqueue:
                with pytest.raises(RuntimeError, match="admission failed"):
                    policy_notices.record_policy_notice(
                        db, notices[0], deliver=lambda *args: {}
                    )
                db.rollback()
            else:
                policy_notices.record_policy_notice(
                    db, notices[0], deliver=lambda *args: {}
                )
    else:
        policy_notices._persist_policy_match_hit(notices[0])
    with data.factory() as observer:
        hits = (
            observer.query(models.PolicyNoticeHit)
            .filter_by(account_id=data.account_id)
            .all()
        )
        intents = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .all()
        )
        assert len(hits) == len(intents) == int(not fail_enqueue)
        if hits:
            fields = decode_payload(intents[0].payload)
            assert fields["extra_details"]["audit_event_id"] == str(
                hits[0].audit_event_id
            )
            assert fields["action"] == action


@pytest.mark.asyncio
async def test_slow_relay_does_not_block_async_admission(
    committed_data: Any, monkeypatch: Any
) -> None:
    import asyncio
    import os
    import threading
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from preloop.services.audit import outbox

    data = committed_data
    started, release = threading.Event(), threading.Event()
    request_thread = threading.get_ident()
    relay_threads = []
    relay_service = audit.AuditService()
    original = relay_service.log_policy_decision

    def slow_delivery(**fields: Any) -> None:
        relay_threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        original(**fields)

    monkeypatch.setattr(relay_service, "log_policy_decision", slow_delivery)
    with data.factory() as db:
        policy_evaluator.evaluate_policy(db, "unknown_tool", {}, data.account_id)
        db.commit()
    relay = outbox.AuditOutboxRelay(
        session_factory=data.factory,
        service_getter=lambda: relay_service,
        poll_interval=0.01,
    )
    engine = create_async_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
    )
    relay.start()
    try:
        assert await asyncio.to_thread(started.wait, 2)
        ticks = []

        async def heartbeat() -> None:
            for _ in range(3):
                await asyncio.sleep(0)
                ticks.append(True)

        async def admit() -> None:
            async with async_sessionmaker(engine)() as db:
                await policy_evaluator.evaluate_policy_async(
                    db, "second_tool", {}, data.account_id
                )
                await db.commit()

        await asyncio.wait_for(asyncio.gather(admit(), heartbeat()), timeout=3)
        assert len(ticks) == 3
        assert relay_threads and request_thread not in relay_threads
        with data.factory() as observer:
            assert (
                observer.query(models.AuditOutbox)
                .filter_by(account_id=data.account_id)
                .count()
                == 2
            )
    finally:
        release.set()
        await asyncio.to_thread(relay.stop)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_enqueue", [False, True])
async def test_notification_timeline_and_intent_commit_together(
    committed_data: Any, monkeypatch: Any, fail_enqueue: bool
) -> None:
    data = committed_data
    service_audit = audit.AuditService()
    monkeypatch.setattr(approval_service, "_get_audit_service", lambda: service_audit)
    enqueue = service_audit.enqueue_in_transaction

    def admit(db: Any, operation: Any, **fields: Any) -> None:
        enqueue(db, operation, **fields)
        if operation == "log_approval_notification" and fail_enqueue:
            raise RuntimeError("notification admission failed")

    monkeypatch.setattr(service_audit, "enqueue_in_transaction", admit)
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        if fail_enqueue:
            with pytest.raises(RuntimeError, match="notification admission failed"):
                await service._audit_notification_result(
                    request,
                    "email",
                    {"sent": 1, "success": True},
                    [data.user_id],
                    "notification-example",
                )
        else:
            await service._audit_notification_result(
                request,
                "email",
                {"sent": 1, "success": True},
                [data.user_id],
                "notification-example",
            )
    with data.factory() as observer:
        assert observer.query(models.ApprovalEvent).filter_by(
            account_id=data.account_id, event_type="notification_sent"
        ).count() == int(not fail_enqueue)
        assert observer.query(models.AuditOutbox).filter_by(
            account_id=data.account_id, operation="log_approval_notification"
        ).count() == int(not fail_enqueue)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["ai_allow", "ai_deny", "bypass"])
async def test_automated_decision_keeps_attribution_in_transaction(
    committed_data: Any, monkeypatch: Any, decision: str
) -> None:
    data = committed_data
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        if decision == "bypass":
            await service._auto_approve_without_review(
                request,
                reason_code="native_tool_approvals_off",
                reason="Configured bypass",
            )
        elif decision == "ai_allow":
            await service._auto_approve_request(
                request.id, reason="Allowed by reviewer"
            )
        else:
            await service._auto_deny_request(request.id, reason="Denied by reviewer")
    with data.factory() as observer:
        rows = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .order_by(models.AuditOutbox.seq)
            .all()
        )
        assert len(rows) == 2
        fields = rows[1].payload
        assert fields["event"] == ("denied" if decision == "ai_deny" else "approved")
        assert fields["approver_id"] is None
        assert fields["reason"].startswith(
            "[BYPASS]" if decision == "bypass" else "[AI]"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
async def test_policy_redacts_with_uncommitted_config_in_owning_session(
    committed_data: Any, monkeypatch: Any, use_async: bool
) -> None:
    """Flush retains the server scope and never opens a second policy session."""
    import os
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from preloop.models.crud import crud_account
    from preloop.services.sensitive_data import storage
    from preloop.services.sensitive_data.redact import redaction_token

    data = committed_data
    metadata = {
        "sensitive_data": {
            "rules": [
                {
                    "id": "scoped-redaction",
                    "on": ["tool.args"],
                    "scope": {"tools": ["unknown_tool"], "servers": ["example-server"]},
                    "types": ["email"],
                    "action": "redact",
                }
            ]
        }
    }
    storage.invalidate_cache(data.account_id)
    monkeypatch.setattr(
        storage,
        "_load_config",
        MagicMock(side_effect=AssertionError("separate policy session")),
    )
    args = {"note": "Contact jane@example.com", "api_key": "example-test-secret"}

    def change_config(db: Any) -> None:
        account = crud_account.get(db, id=data.account_id)
        account.meta_data = metadata
        db.flush()

    if use_async:
        engine = create_async_engine(
            os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
        )
        try:
            async with async_sessionmaker(engine)() as db:
                await db.run_sync(change_config)
                await policy_evaluator.evaluate_policy_async(
                    db,
                    "unknown_tool",
                    args,
                    data.account_id,
                    server_name="example-server",
                )
                await db.commit()
        finally:
            await engine.dispose()
    else:
        with data.factory() as db:
            change_config(db)
            policy_evaluator.evaluate_policy(
                db, "unknown_tool", args, data.account_id, server_name="example-server"
            )
            db.commit()
    with data.factory() as observer:
        row = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .one()
        )
        stored = row.payload["tool_args"]
        assert stored["note"] == f"Contact {redaction_token('email')}"
        assert "jane@example.com" not in str(row.payload)
        assert "example-test-secret" not in str(row.payload)
    assert args["note"] == "Contact jane@example.com"
    storage._load_config.assert_not_called()


@pytest.mark.asyncio
async def test_note_prepare_intent_survives_declined_approval(
    committed_data: Any, monkeypatch: Any
) -> None:
    from preloop.services import agent_send_note, initialize_mcp
    from preloop.services.dynamic_mcp_server import UserContext
    from preloop.services import dynamic_fastmcp_http
    from preloop.models.db import session

    data = committed_data
    caller = UserContext(
        user_id=str(data.user_id), account_id=str(data.account_id), username="example"
    )
    monkeypatch.setattr(
        dynamic_fastmcp_http, "get_current_user_context", lambda: caller
    )
    monkeypatch.setattr(initialize_mcp, "_mcp_caller_session_ids", lambda *a: [])
    monkeypatch.setattr(session, "get_db_session", lambda: iter([data.factory()]))
    monkeypatch.setattr(
        initialize_mcp, "require_approval", AsyncMock(return_value=(False, "declined"))
    )

    def prepare(db: Any, **fields: Any) -> dict:
        assert fields["prepare_only"] is True
        policy_evaluator._log_policy_decision_async(
            db=db,
            account_id=data.account_id,
            tool_name="send_note",
            action="require_approval",
            rule_description="Scope approval required",
        )
        return {
            "status": "requires_approval",
            "approval": {
                "workflow_id": str(data.workflow_id),
                "rule_context": {},
                "facts": {},
                "arguments": {"text": "Example note"},
            },
        }

    monkeypatch.setattr(agent_send_note, "send_note_from_agent", prepare)
    server = initialize_mcp.initialize_mcp_with_tools()
    tool = await server.get_tool("send_note")
    assert await tool.fn(text="Example note") == "declined"
    with data.factory() as observer:
        row = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .one()
        )
        assert row.payload["action"] == "require_approval"
        assert (
            observer.query(models.AgentControlCommand)
            .filter_by(account_id=data.account_id)
            .count()
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_fails", [False, True])
async def test_legacy_mcp_audit_failure_preserves_tool_outcome(
    monkeypatch: Any, handler_fails: bool
) -> None:
    from preloop.services import dynamic_mcp_server as module
    from preloop.services.audit import outbox
    from preloop.services import db_executor

    monkeypatch.setenv("PRELOOP_AUDIT_OUTBOX_ENABLED", "true")
    fake_server = MagicMock()
    handlers = []
    fake_server.call_tool.return_value = lambda fn: handlers.append(fn) or fn
    monkeypatch.setattr(module, "Server", lambda *a, **kw: fake_server)
    server = module.DynamicMCPServer()
    caller = module.UserContext(
        user_id=str(uuid4()), account_id=str(uuid4()), username="example"
    )
    monkeypatch.setattr(server, "_extract_user_context", lambda ctx: caller)
    tool = MagicMock()
    tool.name = "example_tool"
    monkeypatch.setattr(server, "_get_tools_for_user", lambda ctx: [tool])
    monkeypatch.setattr(
        server, "_evaluate_policy", AsyncMock(return_value=("allow", None, "allowed"))
    )
    handler = AsyncMock(return_value="completed")
    if handler_fails:
        handler.side_effect = ValueError("original tool failure")
    server._tool_handlers["example_tool"] = handler
    admissions = []

    async def fail_admission(fn: Any) -> None:
        db = MagicMock()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                module,
                "_log_tool_execution_async",
                lambda **fields: admissions.append(fields),
            )
            fn(db)
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(db_executor, "run_db_async", fail_admission)
    metric = MagicMock()
    monkeypatch.setattr(outbox, "record_admission_failure", metric, raising=False)
    result = await handlers[0]("example_tool", {})
    assert result[0].text == (
        "Error executing tool: original tool failure" if handler_fails else "completed"
    )
    handler.assert_awaited_once()
    assert [fields["status"] for fields in admissions] == [
        "failed" if handler_fails else "executed"
    ]
    metric.assert_called_once_with("tool_outcome_admission")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_channel", ["mobile_push", "slack"])
async def test_notification_admission_failure_does_not_abort_fanout(
    committed_data: Any, monkeypatch: Any, failed_channel: str
) -> None:
    from contextlib import asynccontextmanager
    from sqlalchemy import inspect
    from preloop.models.db import session
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.audit import outbox
    from preloop.services import chat_worker

    data = committed_data
    service_audit = audit.AuditService()
    monkeypatch.setattr(approval_service, "_get_audit_service", lambda: service_audit)
    enqueue = service_audit.enqueue_in_transaction
    admissions = []

    def admit(db: Any, operation: str, **fields: Any) -> None:
        if operation == "log_approval_notification":
            admissions.append(fields["channel"])
            if fields["channel"] == failed_channel:
                raise RuntimeError("outbox unavailable")
        enqueue(db, operation, **fields)

    monkeypatch.setattr(service_audit, "enqueue_in_transaction", admit)

    @asynccontextmanager
    async def audit_session() -> Any:
        with data.factory() as db:
            yield SyncApprovalSession(db)

    monkeypatch.setattr(session, "get_async_db_session", audit_session)
    monkeypatch.setattr(chat_worker, "enqueue_approval_notifications", lambda *a: 0)
    metric = MagicMock()
    monkeypatch.setattr(outbox, "record_admission_failure", metric, raising=False)
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        workflow = db.get(models.ApprovalWorkflow, data.workflow_id)
        workflow.approval_type = "slack"
        workflow.approval_config = {"webhook_url": "https://example.com/hook"}
        db.commit()
        service._resolve_bypass = AsyncMock(return_value=None)
        service._get_all_approver_user_ids = AsyncMock(return_value=[data.user_id])
        service._partition_approvers_for_stagger = AsyncMock(
            return_value={
                "push_capable": [data.user_id],
                "all_email": [data.user_id],
                "email_only": [data.user_id],
                "both_immediate": [],
                "both_stagger": [],
            }
        )
        service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 1}
        )
        service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1}
        )
        service.post_webhook_notification = AsyncMock(return_value=True)
        expired_before = set(inspect(request).expired_attributes)
        results = await service.send_notifications(request, workflow)
        service._send_email_notification.assert_awaited_once()
        service.post_webhook_notification.assert_awaited_once()
        assert results["audit_errors"] == [failed_channel]
        assert results["email"]["success"] is True
        assert admissions == ["mobile_push", "email", "slack"]
        assert inspect(request).expired_attributes == expired_before
    metric.assert_called_once_with("notification_admission")
    with data.factory() as observer:
        intents = (
            observer.query(models.AuditOutbox)
            .filter_by(
                account_id=data.account_id, operation="log_approval_notification"
            )
            .all()
        )
        assert sorted(row.payload["channel"] for row in intents) == sorted(
            {"email", "mobile_push", "slack"} - {failed_channel}
        )
        assert (
            observer.query(models.ApprovalEvent)
            .filter_by(account_id=data.account_id, event_type="notification_sent")
            .count()
            == 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_path", ["late_decision", "poll"])
async def test_expiry_paths_retain_timeout_reason(
    committed_data: Any, monkeypatch: Any, expiry_path: str
) -> None:
    from contextlib import asynccontextmanager
    from datetime import timedelta
    from preloop.models.db import session
    from preloop.models.db.session import SyncApprovalSession

    data = committed_data
    monkeypatch.setattr(
        approval_service.ApprovalService,
        "_approvals_frozen",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        approval_service.ApprovalService, "_broadcast_approval_update", AsyncMock()
    )
    monkeypatch.setattr(
        approval_service.ApprovalService, "_release_parked_executions", AsyncMock()
    )

    @asynccontextmanager
    async def poll_session() -> Any:
        with data.factory() as db:
            yield SyncApprovalSession(db)

    monkeypatch.setattr(session, "get_async_db_session", poll_session)
    with data.factory() as db:
        service = approval_for(db, monkeypatch)
        request = await create_request(service, data)
        request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        if expiry_path == "late_decision":
            result = await service._reject_if_not_actionable(request)
            assert result.status == "expired"
        else:
            with pytest.raises(TimeoutError, match="expired without response"):
                await service.wait_for_approval(request.id, poll_interval=0.01)
    with data.factory() as observer:
        rows = (
            observer.query(models.AuditOutbox)
            .filter_by(account_id=data.account_id)
            .order_by(models.AuditOutbox.seq)
            .all()
        )
        assert len(rows) == 2
        assert rows[-1].payload["event"] == "expired"
        assert rows[-1].payload["reason"] == "Request timed out without response"
