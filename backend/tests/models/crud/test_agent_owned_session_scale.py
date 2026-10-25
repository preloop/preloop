"""Agent usage stays one bounded read however many sessions an agent owns.

The Agents list and Agent detail add the gateway rows of every session an
agent owns (B-02). Those sessions were sent to the database as a VALUES list
with three bind parameters per session, in three statements. A long-lived
agent owns thousands of per-run sessions, so each statement bound tens of
thousands of parameters, planned a hash join that read the whole
``api_usage`` table, and past 21,845 sessions failed outright (the protocol
allows 65,535 parameters). The session list is now one array parameter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_managed_agent

NOW = datetime.now(UTC).replace(tzinfo=None)
#: Largest parameter count any agent usage statement may bind.
MAX_PARAMS = 200
#: Run sessions with gateway rows (the newest ones); see ``_agent_with_runs``.
USED = 100
#: Rows the agent is credited with: two per used run session.
CREDITED = 2 * USED


def _agent_with_runs(
    db: Session, user: Any, runs: int, used: int = USED
) -> models.ManagedAgent:
    """An agent bound to one session plus ``runs`` per-run sessions.

    The newest ``used`` run sessions carry one row under the base principal
    (counted once, through the principal) and, a second later, one
    subscription row under their per-run principal (counted through the
    session, and the agent's newest request). The rest are empty, as most
    finished runs of a long-lived agent are from the usage side; they still
    belong to the agent, which is what used to size the statements. Few rows
    also keep the table small for the plan tests that run after this file.
    """
    base = f"claude-code-{uuid4().hex[:12]}"
    account_id = str(user.account_id)
    db.execute(
        text(
            """
            INSERT INTO runtime_session (
                id, account_id, session_source_type, session_source_id,
                runtime_principal_type, runtime_principal_id, started_at,
                last_activity_at
            )
            SELECT gen_random_uuid(), :account, 'claude_code',
                   :base || :sep || n, 'claude_code', :base || :sep || n,
                   :now - n * interval '1 minute', :now - n * interval '1 minute'
            FROM generate_series(0, :runs) AS n
            """
        ),
        {"account": account_id, "base": base, "sep": ":run-", "runs": runs, "now": NOW},
    )
    db.execute(
        text(
            """
            INSERT INTO api_usage (
                id, account_id, endpoint, method, status_code, duration,
                action_type, timestamp, runtime_principal_type,
                runtime_principal_id, runtime_session_id, prompt_tokens,
                completion_tokens, total_tokens, estimated_cost, model_alias,
                provider_name, cost_source, meta_data
            )
            SELECT gen_random_uuid(), :account, '/v1/messages', 'POST', 200, 0.1,
                   'model_gateway', s.started_at + k * interval '1 second',
                   'claude_code',
                   CASE WHEN k = 2 THEN s.session_source_id ELSE :base END,
                   s.id, 10, 1, 11, 0.01, 'model-' || k, 'provider-' || k,
                   CASE WHEN k = 2 THEN 'subscription' ELSE 'provider' END,
                   CASE WHEN k = 2
                        THEN '{"api_equivalent_cost": 0.02}'::jsonb END
            FROM runtime_session s, generate_series(1, 2) AS k
            WHERE s.account_id = :account
              AND s.session_source_id LIKE :base || :pattern
              AND s.started_at > :now - :used * interval '1 minute'
            """
        ),
        {
            "account": account_id,
            "base": base,
            "pattern": ":run-%",
            "now": NOW,
            "used": used,
        },
    )
    bound = (
        db.query(models.RuntimeSession)
        .filter(
            models.RuntimeSession.account_id == user.account_id,
            models.RuntimeSession.session_source_id == f"{base}:run-0",
        )
        .one()
    )
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=user.account_id,
        runtime_session_id=bound.id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=base,
        display_name="Long-lived runner",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=NOW,
        last_seen_at=NOW,
    )
    db.add(agent)
    db.flush()
    return agent


def _bound_parameters(db: Session) -> Iterator[list[tuple[str, int]]]:
    recorded: list[tuple[str, int]] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "api_usage" in statement:
            recorded.append((statement, len(parameters or {})))

    engine = db.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield recorded
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def bound_parameters(db_session: Session) -> Iterator[list[tuple[str, int]]]:
    yield from _bound_parameters(db_session)


def test_list_binds_few_parameters_for_an_agent_with_many_sessions(
    db_session: Session, test_user: Any, bound_parameters: list[tuple[str, int]]
) -> None:
    """Parameter count does not grow with the agent's session count."""
    _agent_with_runs(db_session, test_user, runs=1500)
    listed = crud_managed_agent.list_for_account(
        db_session, account_id=str(test_user.account_id), limit=20
    )["items"]

    assert listed[0]["total_requests"] == CREDITED
    assert listed[0]["latest_provider_name"] == "provider-2"
    assert listed[0]["subscription_usage"]["request_count"] == USED
    assert listed[0]["subscription_usage"]["api_equivalent_cost"] == pytest.approx(
        0.02 * USED
    )
    widest = max(bound_parameters, key=lambda item: item[1])
    assert widest[1] <= MAX_PARAMS, widest[0][:200]


def test_detail_binds_few_parameters_for_an_agent_with_many_sessions(
    db_session: Session, test_user: Any, bound_parameters: list[tuple[str, int]]
) -> None:
    """Summary, window aggregate and per-model rows stay bounded too."""
    agent = _agent_with_runs(db_session, test_user, runs=1500)
    account_id = str(test_user.account_id)
    ownership = crud_managed_agent.session_ownership(
        db_session, account_id=account_id, agent=agent
    )
    summary = crud_managed_agent.get_summary_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id), ownership=ownership
    )
    windowed = crud_managed_agent.get_usage_aggregate_for_account(
        db_session,
        account_id=account_id,
        agent_id=str(agent.id),
        start_date=NOW - timedelta(days=30),
        ownership=ownership,
    )
    models_rows = crud_managed_agent.get_usage_by_model_for_account(
        db_session,
        account_id=account_id,
        agent_id=str(agent.id),
        limit=10,
        ownership=ownership,
    )

    assert summary is not None and summary["total_requests"] == CREDITED
    assert windowed is not None and windowed["total_requests"] == CREDITED
    assert sum(row["request_count"] for row in models_rows) == CREDITED
    widest = max(bound_parameters, key=lambda item: item[1])
    assert widest[1] <= MAX_PARAMS, widest[0][:200]


def test_list_works_past_the_protocol_parameter_limit(
    db_session: Session, test_user: Any
) -> None:
    """22,000 owned sessions used to need 66,000 parameters and failed."""
    _agent_with_runs(db_session, test_user, runs=22_000)
    listed = crud_managed_agent.list_for_account(
        db_session, account_id=str(test_user.account_id), limit=20
    )["items"]
    assert listed[0]["total_requests"] == CREDITED


def test_detail_and_breakdown_work_past_the_protocol_parameter_limit(
    db_session: Session, test_user: Any
) -> None:
    """Agent detail reads and the per-agent breakdown survive 22,000 sessions."""
    agent = _agent_with_runs(db_session, test_user, runs=22_000)
    account_id = str(test_user.account_id)
    summary = crud_managed_agent.get_summary_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id)
    )
    models_rows = crud_managed_agent.get_usage_by_model_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id), limit=10
    )
    breakdown = crud_managed_agent.get_usage_by_agent_for_account(
        db_session, account_id=account_id
    )
    assert summary is not None and summary["total_requests"] == CREDITED
    assert sum(row["request_count"] for row in models_rows) == CREDITED
    assert [row["total_requests"] for row in breakdown] == [CREDITED]
