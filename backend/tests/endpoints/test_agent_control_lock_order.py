"""Presence updates and operator lifecycle changes use the same row-lock order."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import RuntimeBearerAuthContext
from preloop.api.endpoints.agent_control import _touch_presence
from preloop.models import models
from preloop.models.crud import crud_managed_agent, crud_runtime_session


@pytest.fixture
def presence_scope(db_engine: Engine) -> Iterator[tuple[UUID, UUID, UUID]]:
    """Commit one synthetic active agent and its runtime session."""
    old = datetime.now(UTC) - timedelta(minutes=5)
    with Session(db_engine) as db:
        account = models.Account(organization_name="Presence lock test")
        db.add(account)
        db.flush()
        runtime = models.RuntimeSession(
            account_id=account.id,
            session_source_type="test",
            session_source_id=str(uuid4()),
            started_at=old,
            last_activity_at=old,
        )
        db.add(runtime)
        db.flush()
        agent = models.ManagedAgent(
            account_id=account.id,
            runtime_session_id=runtime.id,
            agent_kind="test",
            session_source_type=runtime.session_source_type,
            session_source_id=runtime.session_source_id,
            display_name="Synthetic agent",
            lifecycle_state="active",
            lifecycle_updated_at=old,
            last_seen_at=old,
        )
        db.add(agent)
        db.flush()
        scope = (account.id, runtime.id, agent.id)
        db.commit()
    try:
        yield scope
    finally:
        with Session(db_engine) as db:
            db.query(models.ManagedAgent).filter_by(id=scope[2]).delete()
            db.query(models.RuntimeSession).filter_by(id=scope[1]).delete()
            db.query(models.Account).filter_by(id=scope[0]).delete()
            db.commit()


def test_heartbeat_does_not_hold_runtime_while_waiting_for_operator_agent_lock(
    db_engine: Engine, presence_scope: tuple[UUID, UUID, UUID]
) -> None:
    """Run actual heartbeat CRUD against the operator's agent-then-runtime path."""
    account_id, runtime_id, agent_id = presence_scope
    heartbeat_requests_agent = Event()
    observed_at = datetime.now(UTC)

    def heartbeat() -> None:
        with Session(db_engine) as db:
            db.execute(text("SET LOCAL lock_timeout = '3s'"))
            runtime = db.get(models.RuntimeSession, runtime_id)
            agent = db.get(models.ManagedAgent, agent_id)
            assert runtime is not None and agent is not None
            context = RuntimeBearerAuthContext(
                user=models.User(),
                api_key=None,
                runtime_session=runtime,
                managed_agent=agent,
            )

            def before_update(
                connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if statement.lower().startswith("update managed_agent "):
                    heartbeat_requests_agent.set()

            event.listen(db.connection(), "before_cursor_execute", before_update)
            _touch_presence(db, context, observed_at=observed_at, session_mode="remote")

    conflict = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(db_engine) as operator:
            # Same order as update_account_managed_agent: agent first, then
            # the bound runtime when decommissioning. Roll back the probe so
            # heartbeat assertions remain independent of lifecycle transitions.
            crud_managed_agent.get_for_account(
                operator,
                account_id=str(account_id),
                agent_id=str(agent_id),
                for_update=True,
            )
            pending = executor.submit(heartbeat)
            try:
                assert heartbeat_requests_agent.wait(timeout=2), (
                    "Heartbeat never requested the agent lock"
                )
                operator.execute(text("SET LOCAL lock_timeout = '250ms'"))
                try:
                    crud_runtime_session.update_operator_state(
                        operator,
                        account_id=str(account_id),
                        runtime_session_id=str(runtime_id),
                        ended_at=observed_at,
                        commit=False,
                    )
                except OperationalError as exc:
                    conflict = exc
            finally:
                operator.rollback()
            pending.result(timeout=4)
    assert conflict is None, (
        "Heartbeat held the runtime lock while waiting for the operator's agent lock"
    )
    with Session(db_engine) as db:
        runtime = db.get(models.RuntimeSession, runtime_id)
        agent = db.get(models.ManagedAgent, agent_id)
        assert runtime is not None and agent is not None
        assert runtime.last_activity_at.replace(tzinfo=UTC) == observed_at
        assert agent.control_last_heartbeat_at.replace(tzinfo=UTC) == observed_at
        assert agent.control_session_mode == "remote"
        assert agent.lifecycle_state == "active"
