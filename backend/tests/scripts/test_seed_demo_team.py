"""The demo team seed is deterministic, idempotent and visible to the readers."""

from datetime import UTC, datetime, timedelta
from typing import Optional

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import budget as budget_endpoint
from preloop.api.endpoints.budget import _policies_to_response
from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_budget_policy,
    crud_issue_cost,
)
from preloop.models.crud.work_item_cost import execution_links, work_ref_calls
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from scripts.seed_demo_team import (
    ACCOUNT_ID,
    MEMBERS,
    Seeder,
    remove_demo_account,
    sid,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

#: Seed clocks across the calendar week, including both ends of a Monday:
#: the budget cards read the current week's bucket, which is nearly empty
#: early on a Monday unless the seed fills it.
SEED_CLOCKS = (
    datetime(2026, 11, 9, 0, 30, tzinfo=UTC),  # Monday, just after the week starts
    datetime(2026, 11, 9, 23, 30, tzinfo=UTC),  # Monday, late
    datetime(2026, 11, 11, 12, 0, tzinfo=UTC),  # Wednesday noon
    datetime(2026, 11, 15, 23, 30, tzinfo=UTC),  # Sunday, just before the week ends
    datetime(2026, 12, 1, 0, 30, tzinfo=UTC),  # first half hour of a month
)


def _freeze_budget_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Make the budget reader see ``now`` as the current instant."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Optional[object] = None) -> "_Frozen":  # type: ignore[override]
            return cls.fromtimestamp(now.timestamp(), tz=tz)  # type: ignore[arg-type]

    monkeypatch.setattr(budget_endpoint, "datetime", _Frozen)


def _seed(db: Session, now: datetime = NOW) -> dict[str, int]:
    remove_demo_account(db)
    seeder = Seeder(db, days=7, scale=1, seed=7, now=now)
    seeder.create_people("not-a-real-password")
    seeder.build()
    counts = seeder.write()
    db.flush()
    return counts


def test_build_is_deterministic_for_a_seed() -> None:
    def rows() -> dict[str, list[dict[str, object]]]:
        seeder = Seeder(None, days=7, scale=1, seed=7, now=NOW)  # type: ignore[arg-type]
        seeder.users = {m.key: sid("user", m.key) for m in MEMBERS}
        seeder.build()
        for runner in seeder.data.rows["runner"]:
            # A random secret by design: runner tokens must not be guessable.
            assert len(runner.pop("token_hash")) == 64
        return dict(seeder.data.rows)

    first, second = rows(), rows()
    assert first == second
    assert len(first["api_usage"]) > 500
    assert {r["model_alias"] for r in first["api_usage"]} >= {
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-5-codex",
        "openai/gpt-4.1",
        "google/gemini-2.5-pro",
    }


@pytest.mark.parametrize("now", SEED_CLOCKS, ids=lambda d: d.strftime("%a-%m%d-%H%M"))
def test_seed_is_idempotent_and_reaches_cost_readers(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    # The seed and the budget reader must agree on "now"; otherwise the test
    # depends on the wall clock (it read an empty week after the fixed clock's).
    _freeze_budget_clock(monkeypatch, now)
    first = _seed(db_session, now)
    second = _seed(db_session, now)
    assert first == second
    assert first["pull_request"] == 20 and first["issue"] == 25

    account = crud_account.get(db_session, ACCOUNT_ID)
    assert account is not None
    summary = ModelGatewayUsageService(db_session).get_account_summary(
        account=account,
        start_date=now - timedelta(days=8),
        end_date=now + timedelta(days=1),
        include_breakdown=False,
    )
    assert summary.total_requests == first["api_usage"]
    assert summary.estimated_cost > 0

    since, until = now - timedelta(days=8), now + timedelta(days=1)
    links = execution_links(
        db_session, account_id=ACCOUNT_ID, since=since, until=until, limit=5000
    )
    assert any(row.pr_key and row.issue_key for row in links)
    refs = [
        row.work_ref
        for row in work_ref_calls(
            db_session, account_id=ACCOUNT_ID, since=since, until=until, limit=5000
        )
    ]
    assert any(ref.get("action") == "opened" and ref.get("closes") for ref in refs)
    assert any(ref.get("kind") == "pull_request" and ref.get("ref") for ref in refs)

    rollups = [
        crud_issue_cost.get_rollup(
            db_session, account_id=ACCOUNT_ID, rollup_id=sid("rollup", n)
        )
        for n in (101, 102)
    ]
    assert all(r is not None and r.run_count > 0 for r in rollups)

    policies = crud_budget_policy.get_multi(
        db_session, account_id=str(ACCOUNT_ID), limit=10
    )
    states = sorted(
        (p.current_spend_usd or 0) / p.hard_limit_usd
        for p in _policies_to_response(db_session, policies)
    )
    assert states[0] < 0.75  # healthy
    assert 0.8 <= states[1] < 1  # near the limit
    assert states[2] >= 1  # over
    assert crud_api_usage.get(db_session, sid("usage", 0)) is not None

    # Seed v2: runners online with inventories, agents seen now, run logs,
    # agent sessions under their agent's source id (owner attribution).
    from preloop.models import models
    from preloop.services.runner_presence import derive_presence
    from scripts.seed_demo_team import AGENTS, RUNNER_IDS, keep_alive

    # The test seeds at a fixed clock; keep_alive brings heartbeats to now.
    assert keep_alive(db_session, interval=0, rounds=1) == 1
    for runner_id in RUNNER_IDS:
        runner = db_session.get(models.FlowRunner, runner_id)
        assert runner is not None
        db_session.refresh(runner)
        assert derive_presence(runner).state == "online"
        assert runner.harness_inventory["entries"]
    agents = {
        a.key: db_session.get(models.ManagedAgent, sid("agent", a.key)) for a in AGENTS
    }
    for agent in agents.values():
        assert agent is not None and agent.last_seen_at is not None
    logs = (
        db_session.query(models.FlowExecutionLog)
        .join(models.FlowExecution)
        .join(models.Flow)
        .filter(models.Flow.account_id == ACCOUNT_ID)
        .all()
    )
    assert logs and {"agent_log_line", "tool_call", "status_update"} <= {
        log.log_type for log in logs
    }
    sessions = (
        db_session.query(models.RuntimeSession)
        .filter(
            models.RuntimeSession.account_id == ACCOUNT_ID,
            models.RuntimeSession.runtime_principal_type == "managed_agent",
        )
        .all()
    )
    bases = {a.session_source_id for a in agents.values() if a is not None}
    assert sessions and all(
        s.session_source_id.split(":")[0] in bases for s in sessions
    )


def test_delete_unattached_keeps_people_with_a_membership(db_session: Session) -> None:
    from preloop.models.crud import crud_person

    _seed(db_session)
    assert crud_person.delete_unattached(db_session, emails=[MEMBERS[0].email]) == 0
    assert crud_person.list_by_email(db_session, email=MEMBERS[0].email)
