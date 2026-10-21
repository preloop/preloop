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
    by_model = crud_managed_agent.get_usage_by_model_for_account(
        db_session, account_id=account_id, agent_id=agent_id, start_date=start
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
