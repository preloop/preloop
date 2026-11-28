"""MCP tool spans from an onboarded agent keep their session (#2593).

A durable onboarded credential carries no ``runtime_session_id`` in its
context data, so the dispatcher used to skip the ``execute_tool`` span. It
now falls back to the agent's current open session, as the permission hook
does for the same call.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import models
from preloop.services import otel_export
from preloop.services.dynamic_fastmcp import otlp_tool_session_id


@pytest.fixture
def shared_factory(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Route the session lookup into the test's rollback transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(
        "preloop.services.agent_current_session.get_session_factory",
        lambda: factory,
    )
    return factory


@pytest.fixture
def otlp_on() -> Any:
    exporter = InMemorySpanExporter()
    otel_export.configure_for_tests(exporter)
    yield exporter
    otel_export.shutdown_otel()


def _agent(db: Session, account_id: Any, *, ended: bool = False) -> Any:
    now = datetime.now(timezone.utc)
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"src-{uuid4()}",
        started_at=now,
        last_activity_at=now,
        ended_at=now if ended else None,
    )
    db.add(session)
    db.flush()
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=account_id,
        runtime_session_id=session.id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=session.session_source_id,
        display_name="Onboarded agent",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    return agent


def _context(account_id: Any, **kwargs: Any) -> SimpleNamespace:
    base = {
        "account_id": str(account_id),
        "runtime_session_id": None,
        "managed_agent_id": None,
        "runtime_principal_type": None,
        "runtime_principal_id": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_explicit_session_wins(otlp_on, test_user) -> None:
    explicit = str(uuid4())
    ctx = _context(test_user.account_id, runtime_session_id=explicit)
    assert asyncio.run(otlp_tool_session_id(ctx)) == explicit


def test_onboarded_agent_uses_current_session(
    otlp_on, shared_factory, db_session, test_user
) -> None:
    agent = _agent(db_session, test_user.account_id)
    ctx = _context(test_user.account_id, managed_agent_id=str(agent.id))
    assert asyncio.run(otlp_tool_session_id(ctx)) == str(agent.runtime_session_id)


def test_managed_agent_principal_uses_current_session(
    otlp_on, shared_factory, db_session, test_user
) -> None:
    agent = _agent(db_session, test_user.account_id)
    ctx = _context(
        test_user.account_id,
        runtime_principal_type="managed_agent",
        runtime_principal_id=str(agent.id),
    )
    assert asyncio.run(otlp_tool_session_id(ctx)) == str(agent.runtime_session_id)


def test_ended_session_is_not_used(
    otlp_on, shared_factory, db_session, test_user
) -> None:
    agent = _agent(db_session, test_user.account_id, ended=True)
    ctx = _context(test_user.account_id, managed_agent_id=str(agent.id))
    assert asyncio.run(otlp_tool_session_id(ctx)) is None


def test_other_account_agent_is_not_used(
    otlp_on, shared_factory, db_session, test_user
) -> None:
    agent = _agent(db_session, test_user.account_id)
    ctx = _context(uuid4(), managed_agent_id=str(agent.id))
    assert asyncio.run(otlp_tool_session_id(ctx)) is None


def test_no_lookup_when_otlp_disabled(monkeypatch, test_user) -> None:
    otel_export.shutdown_otel()
    monkeypatch.setattr(otel_export, "is_enabled", lambda: False)

    def _fail(*_a: Any, **_k: Any) -> None:
        raise AssertionError("session lookup must not run with OTLP off")

    monkeypatch.setattr(
        "preloop.services.agent_current_session.current_agent_session_id", _fail
    )
    ctx = _context(test_user.account_id, managed_agent_id=str(uuid4()))
    assert asyncio.run(otlp_tool_session_id(ctx)) is None


def test_onboarded_tool_span_shares_chat_conversation_id(
    otlp_on, shared_factory, db_session, test_user
) -> None:
    """End to end: the resolved id lands on the exported execute_tool span."""
    agent = _agent(db_session, test_user.account_id)
    ctx = _context(test_user.account_id, managed_agent_id=str(agent.id))
    otel_export.emit_tool_call(
        tool_name="get_approval_status",
        runtime_session_id=asyncio.run(otlp_tool_session_id(ctx)),
        account_id=ctx.account_id,
    )
    spans = [
        s
        for s in otlp_on.get_finished_spans()
        if s.attributes.get("gen_ai.operation.name") == "execute_tool"
    ]
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.conversation.id"] == str(
        agent.runtime_session_id
    )
