"""Real PostgreSQL account isolation, publication cache upserts and weekly bounds."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_api_usage, crud_runtime_session
from preloop.models.crud import weekly_value as crud

END = datetime(2026, 10, 17, tzinfo=UTC)
START = END - timedelta(days=7)
PR = "https://github.com/acme/repo/pull/12"


def test_cache_is_account_scoped_and_preserves_existing_attribution(
    db_session, test_user
) -> None:
    account = test_user.account_id
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other weekly value"}
    )
    original = models.IssueCostPullRequest(
        account_id=account, pr_key=PR, ambiguous=True
    )
    db_session.add(original)
    db_session.flush()
    for aid, title in ((account, "mine"), (other.id, "theirs")):
        crud.store(
            db_session, account_id=aid, pr_key=PR, evidence={"title": title}, now=END
        )
    db_session.expire_all()
    mine = crud.cached(db_session, account_id=account, pr_keys=[PR])[PR]
    assert mine.value_evidence == {"title": "mine"}
    assert mine.ambiguous is True
    crud.store(
        db_session,
        account_id=account,
        pr_key=PR,
        evidence={"title": "updated"},
        now=END,
    )
    assert (
        len(
            db_session.scalars(
                select(models.IssueCostPullRequest).where(
                    models.IssueCostPullRequest.account_id == account
                )
            ).all()
        )
        == 1
    )
    db_session.expire_all()
    assert crud.cached(db_session, account_id=other.id, pr_keys=[PR])[
        PR
    ].value_evidence == {"title": "theirs"}


def test_candidates_half_open_window_and_unknown_cost(
    db_session: Session, test_user: models.User
) -> None:
    account = test_user.account_id
    flow = models.Flow(
        name="Outcome flow", account_id=account, prompt_template="work", agent_config={}
    )
    db_session.add(flow)
    db_session.flush()
    for stamp in (START, END - timedelta(seconds=1), END, START - timedelta(seconds=1)):
        db_session.add(
            models.FlowExecution(
                flow_id=flow.id,
                status="COMPLETED",
                start_time=(stamp - timedelta(hours=1)).replace(tzinfo=None),
                end_time=stamp.replace(tzinfo=None),
                result={"pr_url": PR},
            )
        )
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account,
        session_source_type="codex",
        session_source_id=str(uuid4()),
        started_at=START,
        last_activity_at=START,
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=1,
        account_id=str(account),
        runtime_session_id=str(session.id),
        estimated_cost=1,
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=1,
        account_id=str(account),
        runtime_session_id=str(session.id),
        estimated_cost=None,
    )
    db_session.flush()
    candidates = crud.candidates(db_session, account_id=account, start=START, end=END)
    assert len([r for r in candidates if r["kind"] == "flow"]) == 2
    assert all(r["cost"] is None for r in candidates if r["kind"] == "flow")
    assert next(r for r in candidates if r["id"] == str(session.id))["cost"] is None
    assert crud.candidates(db_session, account_id=uuid4(), start=START, end=END) == []
