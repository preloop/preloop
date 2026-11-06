"""The demo team seed is deterministic, idempotent and visible to the readers."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

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


def _seed(db: Session) -> dict[str, int]:
    remove_demo_account(db)
    seeder = Seeder(db, days=7, scale=1, seed=7, now=NOW)
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


def test_seed_is_idempotent_and_reaches_cost_readers(db_session: Session) -> None:
    first = _seed(db_session)
    second = _seed(db_session)
    assert first == second
    assert first["pull_request"] == 20 and first["issue"] == 25

    account = crud_account.get(db_session, ACCOUNT_ID)
    assert account is not None
    summary = ModelGatewayUsageService(db_session).get_account_summary(
        account=account,
        start_date=NOW - timedelta(days=8),
        end_date=NOW + timedelta(days=1),
        include_breakdown=False,
    )
    assert summary.total_requests == first["api_usage"]
    assert summary.estimated_cost > 0

    since, until = NOW - timedelta(days=8), NOW + timedelta(days=1)
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


def test_delete_unattached_keeps_people_with_a_membership(db_session: Session) -> None:
    from preloop.models.crud import crud_person

    _seed(db_session)
    assert crud_person.delete_unattached(db_session, emails=[MEMBERS[0].email]) == 0
    assert crud_person.list_by_email(db_session, email=MEMBERS[0].email)
