"""Full-population digest rankings, attribution and constant query counts."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage

START = datetime(2026, 8, 1, 9, 0, 0, 123456, UTC)
END = START + timedelta(days=7)


def usage(db: Session, account_id: object, **overrides: object) -> None:
    values = dict(
        account_id=account_id,
        timestamp=START,
        action_type="model_gateway",
        endpoint="/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        prompt_tokens=10,
        completion_tokens=1,
        total_tokens=11,
        estimated_cost=0,
        model_alias="Example model",
        provider_name="example",
    )
    values.update(overrides)
    db.add(models.ApiUsage(**values))


def managed_agent(
    db: Session, account_id: object, source: str, **extra: object
) -> models.ManagedAgent:
    agent = models.ManagedAgent(
        account_id=account_id,
        session_source_type="example",
        session_source_id=source,
        agent_kind="example",
        display_name="Example agent",
        lifecycle_state="active",
        lifecycle_updated_at=START,
        last_seen_at=START,
        **extra,
    )
    db.add(agent)
    db.flush()
    return agent


def ranking(db: Session, account_id: object) -> list[dict]:
    return crud_api_usage.get_gateway_usage_by_agent(
        db,
        account_id=str(account_id),
        start_date=START,
        end_date=END,
    )


def test_agents_cover_more_than_250_sessions_with_one_query(
    db_session: Session, test_user: models.User
) -> None:
    agent = managed_agent(db_session, test_user.account_id, "leading")
    for index in range(260):
        session = models.RuntimeSession(
            account_id=test_user.account_id,
            session_source_type="example",
            session_source_id=f"session-{index}",
            started_at=START,
        )
        db_session.add(session)
        db_session.flush()
        usage(
            db_session,
            test_user.account_id,
            runtime_session_id=session.id,
            runtime_principal_type="example",
            runtime_principal_id="leading",
        )
    # Direct usage, failure and subscription coverage belong to the population.
    usage(
        db_session,
        test_user.account_id,
        runtime_principal_type="example",
        runtime_principal_id="leading",
        status_code=500,
        cost_source="subscription",
    )
    for index in range(4):
        usage(
            db_session,
            test_user.account_id,
            runtime_principal_type="named",
            runtime_principal_id=str(index),
            runtime_principal_name="Same name",
        )
    usage(db_session, test_user.account_id)
    usage(db_session, test_user.account_id, timestamp=END)
    usage(db_session, test_user.account_id, timestamp=START - timedelta(microseconds=1))
    usage(db_session, test_user.account_id, action_type="imported")
    usage(db_session, test_user.account_id, meta_data={"purpose": "replay_validation"})
    db_session.flush()
    statements: list[str] = []

    def record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", record)
    try:
        rows = ranking(db_session, test_user.account_id)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", record)
    assert len(statements) == 1
    assert rows[0]["agent_id"] == str(agent.id)
    assert rows[0]["request_count"] == 261
    assert len(rows) == 4
    assert rows[-1]["total_count"] == 266
    assert rows[-1]["other_count"] == 2
    assert rows[-1]["request_count"] == 1
    assert sum(row["request_count"] for row in rows) + rows[-1]["other_count"] == 266
    assert rows[1]["identity"] < rows[2]["identity"]
    assert rows[1]["name"] != rows[2]["name"]


def test_attribution_precedence_account_isolation_and_no_fanout(
    db_session: Session, test_user: models.User, test_viewer_user: models.User
) -> None:
    account = test_user.account_id
    other = test_viewer_user.account_id
    session = models.RuntimeSession(
        account_id=account,
        session_source_type="example",
        session_source_id="session",
        started_at=START,
    )
    foreign_session = models.RuntimeSession(
        account_id=other,
        session_source_type="example",
        session_source_id="foreign",
        started_at=START,
        runtime_principal_name="Foreign name",
        runtime_principal_type="example",
        runtime_principal_id="foreign",
    )
    db_session.add_all([session, foreign_session])
    db_session.flush()
    direct = managed_agent(db_session, account, "direct")
    session_owner = managed_agent(
        db_session, account, "owner", runtime_session_id=session.id
    )
    # A legacy duplicate relationship cannot multiply one request.
    managed_agent(db_session, account, "duplicate", runtime_session_id=session.id)
    managed_agent(db_session, other, "foreign")
    usage(
        db_session,
        account,
        runtime_session_id=session.id,
        runtime_principal_type="example",
        runtime_principal_id="direct",
    )
    usage(db_session, account, runtime_session_id=session.id)
    usage(
        db_session,
        account,
        runtime_session_id=foreign_session.id,
        runtime_principal_type="example",
        runtime_principal_id="foreign",
    )
    usage(
        db_session,
        other,
        runtime_principal_type="example",
        runtime_principal_id="direct",
    )
    db_session.flush()
    rows = ranking(db_session, account)
    assert rows[-1]["total_count"] == 3
    assert rows[-1]["request_count"] == 1
    assert any(row["agent_id"] == str(direct.id) for row in rows)
    assert sum(row["request_count"] for row in rows) == 3
    assert all(row["name"] != "Foreign name" for row in rows)


def test_models_preserve_alias_identity_ties_unknown_and_remainders(
    db_session: Session, test_user: models.User, test_viewer_user: models.User
) -> None:
    account = test_user.account_id
    foreign = models.AIModel(
        account_id=test_viewer_user.account_id,
        name="Foreign model",
        provider_name="example",
        model_identifier="foreign",
    )
    db_session.add(foreign)
    db_session.flush()
    own = models.AIModel(
        account_id=account,
        name="Fallback model",
        provider_name="example",
        model_identifier="own",
    )
    db_session.add(own)
    db_session.flush()
    for alias in ["model-a", "model-b", "model-c", "model-d"]:
        usage(db_session, account, model_alias=alias, ai_model_id=own.id)
    usage(db_session, account, model_alias=None, ai_model_id=foreign.id)
    usage(db_session, account, model_alias=None)
    usage(
        db_session,
        account,
        model_alias="excluded",
        meta_data={"purpose": "replay_validation"},
    )
    usage(db_session, account, model_alias="excluded", timestamp=END)
    db_session.flush()
    rows = crud_api_usage.get_gateway_usage_by_model(
        db_session,
        account_id=str(account),
        start_date=START,
        end_date=END,
        digest_ranking=True,
    )
    assert [row["name"] for row in rows[:3]] == ["model-a", "model-b", "model-c"]
    assert rows[-1]["request_count"] == 2
    assert rows[-1]["other_count"] == 1
    assert rows[-1]["total_count"] == 6
