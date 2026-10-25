"""One attribution rule for an agent's spend on every console surface (B-02).

The Overview Inventory maps each gateway session slice to an agent by the
agent bound to the session, the session id, the session source id or the
runtime principal, per-run variants (``<base>:<run>``, ``<base>-<run>``)
included. The Agents list and Agent detail summed only rows whose usage-row
principal was the agent's base id, so rows written under a per-run principal
(or none) counted on the Overview and not on the agent pages, and a longer
window could show less spend than a shorter one.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage, crud_managed_agent
from preloop.services.model_gateway_usage import ModelGatewayUsageService

NOW = datetime.now(UTC).replace(tzinfo=None)


def _session(
    db: Session,
    user: Any,
    *,
    source_type: str,
    source_id: str,
    principal_id: Optional[str] = None,
    days_ago: int = 1,
) -> models.RuntimeSession:
    row = models.RuntimeSession(
        account_id=user.account_id,
        session_source_type=source_type,
        session_source_id=source_id,
        runtime_principal_type=source_type if principal_id else None,
        runtime_principal_id=principal_id,
        started_at=NOW - timedelta(days=days_ago),
        last_activity_at=NOW - timedelta(days=days_ago),
    )
    db.add(row)
    db.flush()
    return row


def _agent(
    db: Session, user: Any, *, base: str, session: models.RuntimeSession, name: str
) -> models.ManagedAgent:
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=user.account_id,
        runtime_session_id=session.id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=base,
        display_name=name,
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=NOW,
        last_seen_at=NOW,
    )
    db.add(agent)
    db.flush()
    return agent


def _usage(
    db: Session,
    user: Any,
    *,
    session: Optional[models.RuntimeSession],
    principal_id: Optional[str],
    cost: float,
    days_ago: float,
    provider: str = "provider-a",
    tokens: int = 100,
) -> None:
    row = crud_api_usage.log_gateway_request(
        db,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(user.account_id),
        runtime_session_id=str(session.id) if session else None,
        model_alias=f"{provider}-model",
        provider_name=provider,
        prompt_tokens=tokens,
        completion_tokens=tokens // 10,
        total_tokens=tokens + tokens // 10,
        estimated_cost=cost,
        runtime_principal_type="claude_code" if principal_id else None,
        runtime_principal_id=principal_id,
    )
    row.timestamp = NOW - timedelta(days=days_ago)
    db.flush()


@pytest.fixture
def fleet(db_session: Session, test_user: Any) -> dict[str, Any]:
    """Two agents whose spend lands under every principal shape seen on prod."""
    db = db_session
    base_a = f"claude-code-{uuid4().hex[:12]}"
    base_b = f"claude-code-{uuid4().hex[:12]}"
    bound_a = _session(
        db, test_user, source_type="claude_code", source_id=f"{base_a}:bound"
    )
    bound_b = _session(
        db, test_user, source_type="claude_code", source_id=base_b, principal_id=base_b
    )
    agent_a = _agent(db, test_user, base=base_a, session=bound_a, name="Agent A")
    agent_b = _agent(db, test_user, base=base_b, session=bound_b, name="Agent B")

    run_colon = _session(
        db,
        test_user,
        source_type="claude_code",
        source_id=f"{base_a}:run-1",
        principal_id=f"{base_a}:run-1",
        days_ago=3,
    )
    run_dash = _session(
        db,
        test_user,
        source_type="claude_code",
        source_id=f"{base_a}-20261020T101010",
        principal_id=f"{base_a}-20261020T101010",
        days_ago=20,
    )
    old_run = _session(
        db,
        test_user,
        source_type="claude_code",
        source_id=f"{base_a}:run-old",
        principal_id=f"{base_a}:run-old",
        days_ago=60,
    )

    # Subscription traffic under the base principal: priced at zero.
    for _ in range(3):
        _usage(
            db, test_user, session=bound_a, principal_id=base_a, cost=0.0, days_ago=1
        )
    # Priced side requests under the per-run principal, as on prod.
    for _ in range(2):
        _usage(
            db,
            test_user,
            session=run_colon,
            principal_id=f"{base_a}:run-1",
            cost=0.05,
            days_ago=2,
            provider="provider-b",
        )
    # A row in the agent's session that carries no principal at all.
    _usage(db, test_user, session=run_colon, principal_id=None, cost=0.02, days_ago=2)
    _usage(
        db,
        test_user,
        session=run_dash,
        principal_id=f"{base_a}-20261020T101010",
        cost=0.04,
        days_ago=20,
    )
    _usage(
        db,
        test_user,
        session=old_run,
        principal_id=f"{base_a}:run-old",
        cost=0.30,
        days_ago=60,
    )
    _usage(db, test_user, session=bound_b, principal_id=base_b, cost=0.07, days_ago=1)
    return {
        "a": agent_a,
        "b": agent_b,
        "base_b": base_b,
        "run_colon": run_colon,
        "account_id": str(test_user.account_id),
    }


def _overview_totals(
    db: Session, account_id: str, start: datetime, agents: list[models.ManagedAgent]
) -> dict[str, dict[str, float]]:
    """Mirror the Overview Inventory: session slices mapped to an agent."""
    slices = crud_api_usage.get_gateway_usage_by_session(
        db,
        account_id=account_id,
        start_date=start,
        end_date=NOW + timedelta(minutes=1),
        limit=1000,
    )

    def by_source(value: str) -> Optional[models.ManagedAgent]:
        for agent in agents:
            if value in (str(agent.id), agent.session_source_id):
                return agent
            if str(agent.runtime_session_id) == value:
                return agent
        matches = [
            agent
            for agent in agents
            if value.startswith(f"{agent.session_source_id}:")
            or value.startswith(f"{agent.session_source_id}-")
        ]
        matches.sort(key=lambda agent: -len(agent.session_source_id))
        return matches[0] if matches else None

    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "requests": 0, "tokens": 0}
    )
    for row in slices:
        for key in (
            "agent_id",
            "runtime_session_id",
            "session_source_id",
            "runtime_principal_id",
        ):
            agent = by_source(row[key]) if row.get(key) else None
            if agent is not None:
                break
        else:
            continue
        bucket = totals[str(agent.id)]
        bucket["cost"] += row["estimated_cost"]
        bucket["requests"] += row["request_count"]
        bucket["tokens"] += row["total_tokens"]
    return totals


def _crud_totals(aggregate: dict[str, Any]) -> dict[str, float]:
    return {
        "cost": aggregate["estimated_cost"],
        "requests": aggregate["total_requests"],
        "tokens": aggregate["total_tokens"],
    }


def _approx(left: dict[str, float], right: dict[str, float]) -> None:
    assert left["requests"] == right["requests"]
    assert left["tokens"] == right["tokens"]
    assert left["cost"] == pytest.approx(right["cost"])


@pytest.mark.parametrize("days", [7, 30, 365])
def test_agent_aggregate_matches_the_overview_slices(
    db_session: Session, fleet: dict[str, Any], days: int
) -> None:
    start = NOW - timedelta(days=days)
    agents = [fleet["a"], fleet["b"]]
    overview = _overview_totals(db_session, fleet["account_id"], start, agents)
    for agent in agents:
        aggregate = crud_managed_agent.get_usage_aggregate_for_account(
            db_session,
            account_id=fleet["account_id"],
            agent_id=str(agent.id),
            start_date=start,
        )
        _approx(_crud_totals(aggregate), overview[str(agent.id)])


def test_longer_window_never_has_less_spend(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    account_id = fleet["account_id"]
    agent_id = str(fleet["a"].id)
    listed = {
        item["id"]: item
        for item in crud_managed_agent.list_for_account(
            db_session, account_id=account_id, status="all", limit=100
        )["items"]
    }
    all_time = listed[agent_id]["estimated_cost"]
    detail_all_time = crud_managed_agent.get_usage_aggregate_for_account(
        db_session, account_id=account_id, agent_id=agent_id
    )["estimated_cost"]
    thirty = crud_managed_agent.get_usage_aggregate_for_account(
        db_session,
        account_id=account_id,
        agent_id=agent_id,
        start_date=NOW - timedelta(days=30),
    )["estimated_cost"]
    week = crud_managed_agent.get_usage_aggregate_for_account(
        db_session,
        account_id=account_id,
        agent_id=agent_id,
        start_date=NOW - timedelta(days=7),
    )["estimated_cost"]
    assert all_time == pytest.approx(detail_all_time)
    assert all_time == pytest.approx(0.0 + 0.10 + 0.02 + 0.04 + 0.30)
    assert thirty == pytest.approx(0.16)
    assert week == pytest.approx(0.12)
    assert listed[str(fleet["b"].id)]["estimated_cost"] == pytest.approx(0.07)
    assert listed[agent_id]["total_requests"] == 8
    # The bound session and two per-run sessions; one run is 60 days old.
    windowed = crud_managed_agent.get_usage_aggregate_for_account(
        db_session,
        account_id=account_id,
        agent_id=agent_id,
        start_date=NOW - timedelta(days=30),
    )
    assert windowed["session_count"] == 3


def test_a_row_under_another_agents_principal_counts_once(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """The usage-row principal wins when it names an agent's base id."""
    _usage(
        db_session,
        test_user,
        session=fleet["run_colon"],
        principal_id=fleet["base_b"],
        cost=0.01,
        days_ago=2,
    )
    account_id = fleet["account_id"]
    totals = {
        agent: crud_managed_agent.get_usage_aggregate_for_account(
            db_session, account_id=account_id, agent_id=str(fleet[agent].id)
        )["estimated_cost"]
        for agent in ("a", "b")
    }
    assert totals["b"] == pytest.approx(0.08)
    assert totals["a"] == pytest.approx(0.46)


def test_per_model_rows_add_up_to_the_aggregate(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    account_id = fleet["account_id"]
    agent_id = str(fleet["a"].id)
    start = NOW - timedelta(days=30)
    # A limit above the fixture's model count: the default top 10 would
    # drop the remainder of an agent with more models.
    by_model = crud_managed_agent.get_usage_by_model_for_account(
        db_session,
        account_id=account_id,
        agent_id=agent_id,
        start_date=start,
        limit=100,
    )
    aggregate = crud_managed_agent.get_usage_aggregate_for_account(
        db_session, account_id=account_id, agent_id=agent_id, start_date=start
    )
    assert sum(row["estimated_cost"] for row in by_model) == pytest.approx(
        aggregate["estimated_cost"]
    )
    assert sum(row["request_count"] for row in by_model) == aggregate["total_requests"]


def test_summary_usage_by_agent_reads_the_same_rule(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    start = NOW - timedelta(days=30)
    summary = ModelGatewayUsageService(db_session).get_account_summary(
        account=account,
        start_date=start,
        end_date=NOW + timedelta(minutes=1),
        breakdowns={"agents"},
    )
    by_agent = {row.agent_id: row for row in summary.usage_by_agent}
    for agent in (fleet["a"], fleet["b"]):
        aggregate = crud_managed_agent.get_usage_aggregate_for_account(
            db_session,
            account_id=fleet["account_id"],
            agent_id=str(agent.id),
            start_date=start,
            end_date=NOW + timedelta(minutes=1),
        )
        row = by_agent[str(agent.id)]
        assert row.estimated_cost == pytest.approx(aggregate["estimated_cost"])
        assert row.request_count == aggregate["total_requests"]
        assert row.token_usage.total_tokens == aggregate["total_tokens"]


# D-05: subscription-routed usage counts in an agent's figures, but not as
# spend. Spend stays the metered amount; the API-equivalent cost of the
# subscription rows is an estimate reported next to it, with its coverage.


def _subscription_usage(
    db: Session,
    user: Any,
    *,
    session: models.RuntimeSession,
    principal_id: Optional[str],
    api_equivalent: Optional[float],
    days_ago: float = 1,
) -> None:
    row = crud_api_usage.log_gateway_request(
        db,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(user.account_id),
        runtime_session_id=str(session.id),
        model_alias="anthropic/sub-model",
        provider_name="anthropic",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=0.0,
        cost_source="subscription",
        runtime_principal_type="claude_code" if principal_id else None,
        runtime_principal_id=principal_id,
        meta_data=(
            {"api_equivalent_cost": api_equivalent}
            if api_equivalent is not None
            else {}
        ),
    )
    row.timestamp = NOW - timedelta(days=days_ago)
    db.flush()


def _surfaces(
    db: Session, account: models.Account, agent: models.ManagedAgent, start: datetime
) -> list[tuple[float, Any]]:
    """Spend and the subscription block as the list, detail and Overview read them."""
    account_id = str(account.id)
    end = NOW + timedelta(minutes=1)
    detail = crud_managed_agent.get_usage_aggregate_for_account(
        db, account_id=account_id, agent_id=str(agent.id), start_date=start
    )
    summary = ModelGatewayUsageService(db).get_account_summary(
        account=account, start_date=start, end_date=end, breakdowns={"agents"}
    )
    overview = next(
        row for row in summary.usage_by_agent if row.agent_id == str(agent.id)
    )
    return [
        (detail["estimated_cost"], detail["subscription_usage"]),
        (overview.estimated_cost, overview.subscription_usage),
    ]


def test_spend_is_metered_and_the_estimate_adds_the_api_equivalent(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    agent = fleet["a"]
    base = agent.session_source_id
    bound = db_session.get(models.RuntimeSession, agent.runtime_session_id)
    # Two subscription rows under the base principal, one in a per-run
    # session under the per-run principal: all three are the agent's.
    _subscription_usage(
        db_session, test_user, session=bound, principal_id=base, api_equivalent=0.5
    )
    _subscription_usage(
        db_session, test_user, session=bound, principal_id=base, api_equivalent=0.25
    )
    _subscription_usage(
        db_session,
        test_user,
        session=fleet["run_colon"],
        principal_id=f"{base}:run-1",
        api_equivalent=1.0,
    )
    account = db_session.get(models.Account, test_user.account_id)
    start = NOW - timedelta(days=30)
    for spend, block in _surfaces(db_session, account, agent, start):
        # Metered only: the fixture's priced rows in 30 days, no subscription.
        assert spend == pytest.approx(0.16)
        assert block is not None
        block = block if isinstance(block, dict) else block.model_dump()
        assert block["request_count"] == 3
        assert block["covered_requests"] == 3
        assert block["coverage"] == pytest.approx(1.0)
        assert block["api_equivalent_cost"] == pytest.approx(1.75)
        assert block["api_equivalent_cost_is_estimate"] is True
        assert block["total_tokens"] == 3300
    listed = next(
        item
        for item in crud_managed_agent.list_for_account(
            db_session, account_id=str(account.id), status="all", limit=100
        )["items"]
        if item["id"] == str(agent.id)
    )
    # All time: the 60-day-old priced run too; tokens include subscription use.
    assert listed["estimated_cost"] == pytest.approx(0.46)
    assert listed["subscription_usage"]["api_equivalent_cost"] == pytest.approx(1.75)
    assert listed["subscription_usage"]["request_count"] == 3
    assert listed["subscription_usage_computed"] is True
    assert listed["token_usage"]["total_tokens"] >= 3300


def test_a_subscription_row_without_an_estimate_is_reported_not_zeroed(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    agent = fleet["a"]
    bound = db_session.get(models.RuntimeSession, agent.runtime_session_id)
    _subscription_usage(
        db_session,
        test_user,
        session=bound,
        principal_id=agent.session_source_id,
        api_equivalent=0.5,
    )
    _subscription_usage(
        db_session,
        test_user,
        session=bound,
        principal_id=agent.session_source_id,
        api_equivalent=None,
    )
    account = db_session.get(models.Account, test_user.account_id)
    for _, block in _surfaces(db_session, account, agent, NOW - timedelta(days=30)):
        block = block if isinstance(block, dict) else block.model_dump()
        # The estimate covers one of two requests: coverage says so, and the
        # sum is over covered rows only, as on the Cost page.
        assert block["request_count"] == 2
        assert block["covered_requests"] == 1
        assert block["coverage"] == pytest.approx(0.5)
        assert block["api_equivalent_cost"] == pytest.approx(0.5)


def test_an_agent_without_subscription_use_has_no_subscription_block(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    account = db_session.get(models.Account, test_user.account_id)
    for _, block in _surfaces(
        db_session, account, fleet["b"], NOW - timedelta(days=30)
    ):
        assert block is None


def test_the_agent_detail_response_carries_the_subscription_block(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    from preloop.api.endpoints.account import _build_managed_agent_detail_response

    agent = fleet["a"]
    bound = db_session.get(models.RuntimeSession, agent.runtime_session_id)
    _subscription_usage(
        db_session,
        test_user,
        session=bound,
        principal_id=agent.session_source_id,
        api_equivalent=0.5,
    )
    response = _build_managed_agent_detail_response(
        db_session,
        account_id=fleet["account_id"],
        agent_id=str(agent.id),
        start_date=NOW - timedelta(days=30),
        sections=frozenset(),
    )
    block = response.aggregate.subscription_usage
    assert block is not None
    assert block.request_count == 1
    assert block.api_equivalent_cost == pytest.approx(0.5)
    assert response.aggregate.estimated_cost == pytest.approx(0.16)
    assert response.agent.subscription_usage is not None
    assert response.agent.subscription_usage_computed is True


def test_activity_status_follows_traffic_in_per_run_sessions(
    db_session: Session, test_user: Any
) -> None:
    """An agent whose bound session is quiet but whose run is busy is active."""
    base = f"claude-code-{uuid4().hex[:12]}"
    bound = _session(db_session, test_user, source_type="claude_code", source_id=base)
    agent = _agent(db_session, test_user, base=base, session=bound, name="Runner")
    run = _session(
        db_session,
        test_user,
        source_type="claude_code",
        source_id=f"{base}:run-now",
        principal_id=f"{base}:run-now",
        days_ago=0,
    )
    _usage(
        db_session,
        test_user,
        session=run,
        principal_id=f"{base}:run-now",
        cost=0.02,
        days_ago=2 / (24 * 60),
    )
    account_id = str(test_user.account_id)
    listed = next(
        item
        for item in crud_managed_agent.list_for_account(
            db_session, account_id=account_id, status="all", limit=100
        )["items"]
        if item["id"] == str(agent.id)
    )
    detail = crud_managed_agent.get_summary_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id)
    )
    for summary in (listed, detail):
        assert summary["total_requests"] == 1
        assert summary["activity_status"] == "active_now"
        assert summary["is_active_now"] is True


def _statements(db: Session):
    """Record the SQL a block runs, as a list filled in place."""
    from contextlib import contextmanager

    from sqlalchemy import event

    @contextmanager
    def recording():
        statements: list[str] = []

        def record(conn, cursor, statement, *args):
            statements.append(" ".join(statement.split()))

        engine = db.get_bind().engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            event.remove(engine, "before_cursor_execute", record)

    return recording()


def _busy_runner(db: Session, user: Any, name: str) -> models.ManagedAgent:
    base = f"claude-code-{uuid4().hex[:12]}"
    bound = _session(db, user, source_type="claude_code", source_id=base)
    agent = _agent(db, user, base=base, session=bound, name=name)
    _usage(db, user, session=bound, principal_id=base, cost=0.0, days_ago=3)
    run = _session(
        db,
        user,
        source_type="claude_code",
        source_id=f"{base}:run",
        principal_id=f"{base}:run",
    )
    _usage(db, user, session=run, principal_id=f"{base}:run", cost=0.01, days_ago=0.01)
    return agent


def test_the_list_reads_latest_models_in_one_statement(
    db_session: Session, test_user: Any
) -> None:
    """No query per agent for the latest model of its per-run sessions."""
    account_id = str(test_user.account_id)
    _busy_runner(db_session, test_user, "Runner 1")
    with _statements(db_session) as one:
        crud_managed_agent.list_for_account(
            db_session, account_id=account_id, status="all", limit=100
        )
    for index in range(2, 5):
        _busy_runner(db_session, test_user, f"Runner {index}")
    with _statements(db_session) as four:
        listed = crud_managed_agent.list_for_account(
            db_session, account_id=account_id, status="all", limit=100
        )["items"]
    assert len(listed) == 4
    assert all(item["latest_provider_name"] == "provider-a" for item in listed)

    def usage_reads(statements: list[str]) -> list[str]:
        return [s for s in statements if "api_usage" in s]

    assert len(usage_reads(four)) == len(usage_reads(one))


def test_the_agent_detail_resolves_its_sessions_once(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.api.endpoints.account import (
        MANAGED_AGENT_DETAIL_SECTIONS,
        _build_managed_agent_detail_response,
    )

    with _statements(db_session) as statements:
        _build_managed_agent_detail_response(
            db_session,
            account_id=fleet["account_id"],
            agent_id=str(fleet["a"].id),
            start_date=NOW - timedelta(days=30),
            sections=frozenset({"usage_by_model"}) & MANAGED_AGENT_DETAIL_SECTIONS,
        )
    session_matches = [
        s for s in statements if "FROM runtime_session" in s and "LIKE" in s
    ]
    assert len(session_matches) == 1


def test_list_skips_subscription_metadata_reads(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """The list never detoasts historical JSON for the optional price estimate."""
    with _statements(db_session) as statements:
        result = crud_managed_agent.list_for_account(
            db_session, account_id=str(test_user.account_id), limit=100
        )
    assert result["items"]
    assert all(item["subscription_usage_computed"] for item in result["items"])
    usage_reads = [statement for statement in statements if "api_usage" in statement]
    assert not usage_reads
    assert any("agent_usage_total" in statement for statement in statements)


def _assert_stored_matches_source(db: Session, account_id: str) -> None:
    """Compare every stored list figure with a fresh source aggregate."""
    from preloop.models.crud.managed_agent import _usage_aggregates_for_principals

    agents = db.query(models.ManagedAgent).filter_by(account_id=account_id).all()
    principals = [(a.session_source_type, a.session_source_id) for a in agents]
    source = _usage_aggregates_for_principals(
        db, account_id=account_id, principals=principals
    )
    stored = _usage_aggregates_for_principals(
        db, account_id=account_id, principals=principals, use_stored_totals=True
    )
    for principal in principals:
        actual = dict(stored[principal])
        assert actual.pop("_stored_totals") is True
        expected = source[principal]
        assert actual.pop("estimated_cost") == pytest.approx(expected["estimated_cost"])
        expected = {k: v for k, v in expected.items() if k != "estimated_cost"}
        assert actual == expected


def test_stored_totals_follow_corrections_moves_deletes_and_rollback(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Source updates replace prior contributions, including metadata and owner."""
    account_id = str(test_user.account_id)
    _subscription_usage(
        db_session,
        test_user,
        session=fleet["run_colon"],
        principal_id=None,
        api_equivalent=1.0,
        days_ago=0,
    )
    _assert_stored_matches_source(db_session, account_id)
    usage = (
        db_session.query(models.ApiUsage)
        .filter_by(account_id=account_id, cost_source="subscription")
        .one()
    )
    usage.meta_data = {"api_equivalent_cost": 2.5}
    usage.prompt_tokens = 2500
    usage.estimated_cost = 0.1
    db_session.flush()
    _assert_stored_matches_source(db_session, account_id)
    usage.runtime_principal_type = fleet["b"].session_source_type
    usage.runtime_principal_id = fleet["b"].session_source_id
    usage.runtime_session_id = fleet["b"].runtime_session_id
    db_session.flush()
    _assert_stored_matches_source(db_session, account_id)
    with db_session.begin_nested() as savepoint:
        db_session.delete(usage)
        db_session.flush()
        _assert_stored_matches_source(db_session, account_id)
        savepoint.rollback()
    _assert_stored_matches_source(db_session, account_id)
    db_session.delete(usage)
    db_session.flush()
    _assert_stored_matches_source(db_session, account_id)


def test_partial_backfill_does_not_publish_partial_totals(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Until backfill completes, metered figures use the hotfix fallback."""
    state = db_session.get(models.AgentUsageBackfill, "agents")
    assert state is not None
    state.ready = False
    db_session.flush()
    listed = crud_managed_agent.list_for_account(
        db_session, account_id=str(test_user.account_id), limit=100
    )["items"]
    assert listed
    assert all(not item["subscription_usage_computed"] for item in listed)
    assert all(item["subscription_usage"] is None for item in listed)
    assert next(item for item in listed if item["id"] == str(fleet["a"].id))[
        "estimated_cost"
    ] == pytest.approx(0.46)


def test_repeated_ready_list_reads_distinguish_no_subscription_from_unavailable(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Ready reads stay computed, including agents with no subscription traffic."""
    state = db_session.get(models.AgentUsageBackfill, "agents")
    assert state is not None and state.ready is True
    agent = fleet["a"]
    _subscription_usage(
        db_session,
        test_user,
        session=db_session.get(models.RuntimeSession, agent.runtime_session_id),
        principal_id=agent.session_source_id,
        api_equivalent=0.5,
    )
    for _ in range(3):
        listed = crud_managed_agent.list_for_account(
            db_session, account_id=str(test_user.account_id), limit=100
        )["items"]
        assert all(item["subscription_usage_computed"] is True for item in listed)
        by_id = {item["id"]: item for item in listed}
        assert by_id[str(agent.id)]["subscription_usage"]["api_equivalent_cost"] == 0.5
        assert by_id[str(fleet["b"].id)]["subscription_usage"] is None
        db_session.expire_all()
        assert db_session.get(models.AgentUsageBackfill, "agents").ready is True


def test_totals_follow_session_ownership_changes_without_recounting(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Counters keep source identity; changing a binding changes its reader."""
    fleet["b"].runtime_session_id = fleet["run_colon"].id
    db_session.flush()
    _assert_stored_matches_source(db_session, str(test_user.account_id))


def test_account_deletion_does_not_recreate_counters_during_fk_cascades(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Session/account SET NULL updates must not resurrect a deleted owner."""
    account_id = test_user.account_id
    account = db_session.get(models.Account, account_id)
    assert account is not None
    db_session.delete(account)
    db_session.flush()
    assert (
        db_session.query(models.AgentUsageTotal)
        .filter_by(account_id=account_id)
        .count()
        == 0
    )


def _mixed_usage(db: Session, user: Any, fleet: dict[str, Any]) -> None:
    """Errors, cache tokens and subscription rows on top of the fleet's spend."""
    for status_code, cache_read, cache_write, principal_id, session in (
        (500, None, None, fleet["a"].session_source_id, fleet["run_colon"]),
        (200, 40, 7, f"{fleet['a'].session_source_id}:run-1", fleet["run_colon"]),
        (429, 0, None, fleet["b"].session_source_id, None),
    ):
        row = crud_api_usage.log_gateway_request(
            db,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=status_code,
            duration=0.1,
            account_id=str(user.account_id),
            runtime_session_id=str(session.id) if session else None,
            model_alias="provider-z-latest",
            provider_name="provider-z",
            prompt_tokens=300,
            completion_tokens=30,
            total_tokens=330,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
            estimated_cost=0.07,
            runtime_principal_type="claude_code",
            runtime_principal_id=principal_id,
        )
        row.timestamp = NOW - timedelta(minutes=1)
    _subscription_usage(
        db, user, session=fleet["run_colon"], principal_id=None, api_equivalent=1.25
    )
    _subscription_usage(
        db,
        user,
        session=db.get(models.RuntimeSession, fleet["b"].runtime_session_id),
        principal_id=fleet["b"].session_source_id,
        api_equivalent=None,
    )
    db.flush()


def _set_backfill_ready(db: Session, ready: bool) -> None:
    state = db.get(models.AgentUsageBackfill, "agents")
    assert state is not None
    state.ready = ready
    db.flush()


def _without_cost(value: dict[str, Any]) -> tuple[dict[str, Any], float]:
    value = dict(value)
    return value, float(value.pop("estimated_cost") or 0.0)


def test_all_time_detail_reads_stored_totals_with_identical_figures(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Agent detail all-time summary and aggregate switch to agent_usage_total."""
    account_id = str(test_user.account_id)
    _mixed_usage(db_session, test_user, fleet)

    def read(agent: models.ManagedAgent) -> tuple[Any, Any, list[str]]:
        with _statements(db_session) as statements:
            summary = crud_managed_agent.get_summary_for_account(
                db_session, account_id=account_id, agent_id=str(agent.id)
            )
            aggregate = crud_managed_agent.get_usage_aggregate_for_account(
                db_session, account_id=account_id, agent_id=str(agent.id)
            )
        return summary, aggregate, statements

    for agent in (fleet["a"], fleet["b"]):
        _set_backfill_ready(db_session, False)
        live_summary, live_aggregate, live_sql = read(agent)
        _set_backfill_ready(db_session, True)
        stored_summary, stored_aggregate, stored_sql = read(agent)

        assert any("listed_principals" in s for s in live_sql)
        assert not any("listed_principals" in s for s in stored_sql)
        assert any("agent_usage_total" in s for s in stored_sql)
        assert "_stored_totals" not in stored_aggregate

        expected, expected_cost = _without_cost(live_aggregate)
        actual, actual_cost = _without_cost(stored_aggregate)
        assert actual == expected
        assert actual_cost == pytest.approx(expected_cost)
        expected, expected_cost = _without_cost(live_summary)
        actual, actual_cost = _without_cost(stored_summary)
        assert actual == expected
        assert actual_cost == pytest.approx(expected_cost)

    a_aggregate = crud_managed_agent.get_usage_aggregate_for_account(
        db_session, account_id=account_id, agent_id=str(fleet["a"].id)
    )
    assert a_aggregate["failed_requests"] >= 1
    assert a_aggregate["cache_read_tokens"] >= 40
    assert a_aggregate["latest_model_alias"] == "provider-z-latest"
    assert a_aggregate["subscription_usage"]["api_equivalent_cost"] == 1.25


def test_windowed_reads_keep_aggregating_api_usage(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """Stored counters have no time axis, so a window always reads api_usage."""
    account_id = str(test_user.account_id)
    _set_backfill_ready(db_session, True)
    with _statements(db_session) as statements:
        crud_managed_agent.get_usage_aggregate_for_account(
            db_session,
            account_id=account_id,
            agent_id=str(fleet["a"].id),
            start_date=NOW - timedelta(days=7),
        )
        crud_managed_agent.get_usage_by_agent_for_account(
            db_session, account_id=account_id, start_date=NOW - timedelta(days=7)
        )
    assert sum("listed_principals" in s for s in statements) == 2
    assert not any("agent_usage_total" in s for s in statements)


def test_all_time_usage_by_agent_reads_stored_totals_with_identical_figures(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """The Overview agent breakdown without a window uses the stored counters."""
    account_id = str(test_user.account_id)
    _mixed_usage(db_session, test_user, fleet)
    _set_backfill_ready(db_session, False)
    live = crud_managed_agent.get_usage_by_agent_for_account(
        db_session, account_id=account_id
    )
    _set_backfill_ready(db_session, True)
    with _statements(db_session) as statements:
        stored = crud_managed_agent.get_usage_by_agent_for_account(
            db_session, account_id=account_id
        )
    assert not any("listed_principals" in s for s in statements)
    assert len(stored) == len(live) == 2
    by_agent = {row["agent_id"]: row for row in live}
    for row in stored:
        assert "_stored_totals" not in row
        actual, actual_cost = _without_cost(row)
        expected, expected_cost = _without_cost(by_agent[row["agent_id"]])
        assert actual == expected
        assert actual_cost == pytest.approx(expected_cost)


def test_stored_latest_model_keeps_the_live_tie_break(
    db_session: Session, fleet: dict[str, Any], test_user: Any
) -> None:
    """On equal timestamps the agent's own principal wins, as on the live path."""
    account_id = str(test_user.account_id)
    agent = fleet["a"]
    tie = NOW + timedelta(minutes=5)
    for principal_id, alias, provider in (
        (agent.session_source_id, "direct-model", "direct-provider"),
        (f"{agent.session_source_id}:run-1", "session-model", "session-provider"),
        (f"{agent.session_source_id}:run-1", "session-model-2", "session-provider"),
    ):
        row = crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            account_id=account_id,
            runtime_session_id=str(fleet["run_colon"].id),
            model_alias=alias,
            provider_name=provider,
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            estimated_cost=0.01,
            runtime_principal_type="claude_code",
            runtime_principal_id=principal_id,
        )
        row.timestamp = tie
    db_session.flush()
    _set_backfill_ready(db_session, False)
    live = crud_managed_agent.get_usage_aggregate_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id)
    )
    _set_backfill_ready(db_session, True)
    stored = crud_managed_agent.get_usage_aggregate_for_account(
        db_session, account_id=account_id, agent_id=str(agent.id)
    )
    assert live["latest_model_alias"] == "direct-model"
    assert stored["latest_model_alias"] == live["latest_model_alias"]
    assert stored["latest_provider_name"] == live["latest_provider_name"]
    assert stored["last_request_at"] == live["last_request_at"]
