"""Stage A candidate pages skip covered history and preserve pending arrivals."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_api_usage,
    crud_governance_session_digest,
    crud_runtime_session,
)
from preloop.models.crud.governance_session_candidates import (
    list_session_digest_candidates,
    list_session_digest_retries,
)


def test_candidates_are_bounded_account_scoped_and_keyset_stable(
    db_session: Session,
    test_user: models.User,
) -> None:
    owner = test_user.account_id
    now = datetime.now(timezone.utc)
    identifiers = []
    for index in range(3):
        runtime = crud_runtime_session.upsert_by_source(
            db_session,
            account_id=owner,
            session_source_type="test",
            session_source_id=f"candidate-{index}",
            started_at=now - timedelta(hours=2),
        )
        identifiers.append(runtime.id)
        crud_api_usage.create(
            db_session,
            obj_in={
                "endpoint": "test",
                "method": "POST",
                "status_code": 200,
                "duration": 0,
                "account_id": owner,
                "runtime_session_id": runtime.id,
                "action_type": "model_gateway",
                "timestamp": now,
                "meta_data": {"purpose": "customer.task"},
            },
        )
    first = list_session_digest_candidates(db_session, account_id=owner, limit=1)
    assert first == sorted(identifiers)[:1]
    crud_governance_session_digest.upsert(
        db_session,
        account_id=owner,
        runtime_session_id=first[0],
        window_start=now - timedelta(hours=2),
        window_end=now,
        request_watermark=1,
    )
    assert (
        list_session_digest_candidates(
            db_session,
            account_id=owner,
            after_id=first[0],
            limit=2,
        )
        == sorted(identifiers)[1:]
    )
    for purpose in ("governance_session_digest", "governance_review", "session_title"):
        crud_api_usage.create(
            db_session,
            obj_in={
                "endpoint": "test",
                "method": "POST",
                "status_code": 200,
                "duration": 0,
                "account_id": owner,
                "runtime_session_id": first[0],
                "action_type": "model_gateway",
                "timestamp": now + timedelta(seconds=1),
                "meta_data": {"purpose": purpose},
            },
        )
    assert first[0] not in list_session_digest_candidates(db_session, account_id=owner)
    assert list_session_digest_candidates(db_session, account_id=uuid4()) == []
    # A new request at precisely the covered timestamp must still be found.
    crud_api_usage.create(
        db_session,
        obj_in={
            "endpoint": "test",
            "method": "POST",
            "status_code": 200,
            "duration": 0,
            "account_id": owner,
            "runtime_session_id": first[0],
            "action_type": "model_gateway",
            "timestamp": now,
        },
    )
    assert first[0] in list_session_digest_candidates(db_session, account_id=owner)
    # Newer retained traffic is found even after a cumulative count shrinks.
    crud_governance_session_digest.upsert(
        db_session,
        account_id=owner,
        runtime_session_id=first[0],
        window_start=now - timedelta(hours=2),
        window_end=now - timedelta(seconds=1),
        request_watermark=100,
    )
    assert first[0] in list_session_digest_candidates(db_session, account_id=owner)


def test_candidate_index_matches_frozen_migration_predicate() -> None:
    import importlib.util
    from pathlib import Path
    from preloop.models.gateway_usage import (
        AGENT_GATEWAY_INDEX_PREDICATE,
        AGENT_GATEWAY_PREDICATE,
    )

    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261017_governance_candidate_idx.py"
    )
    spec = importlib.util.spec_from_file_location("candidate_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PREDICATE == AGENT_GATEWAY_INDEX_PREDICATE
    assert AGENT_GATEWAY_PREDICATE == module.PREDICATE.replace(
        "NOT IN (", "NOT IN ('governance_exception_synthesis', "
    )
    index = next(i for i in models.ApiUsage.__table__.indexes if i.name == module.INDEX)
    assert [column.name for column in index.columns] == [
        "account_id",
        "runtime_session_id",
        "timestamp",
    ]
    assert str(index.dialect_options["postgresql"]["where"]) == module.PREDICATE


def test_session_traffic_uses_the_same_internal_purpose_vocabulary() -> None:
    from preloop.models.gateway_usage import GATEWAY_USAGE_PURPOSES
    from preloop.models.crud.runtime_session import INTERNAL_USAGE_PURPOSES

    assert set(INTERNAL_USAGE_PURPOSES) == GATEWAY_USAGE_PURPOSES


def test_retry_reader_limits_orders_and_excludes_completed_or_live_claims(
    db_session: Session,
    test_user: models.User,
) -> None:
    owner = test_user.account_id
    now = datetime.now(timezone.utc)
    runtime = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=owner,
        session_source_type="test",
        session_source_id="retry-candidates",
        started_at=now - timedelta(days=2),
    )
    sid = runtime.id
    expected = []
    for index, status in enumerate(
        [
            "complete",
            "budget_limited",
            "provider_failed",
            "summarizing",
            "summarizing",
            "retry_exhausted",
            "parse_failed",
            "capture_incomplete",
            "capture_incomplete",
        ]
    ):
        row = crud_governance_session_digest.upsert(
            db_session,
            account_id=owner,
            runtime_session_id=sid,
            segment_index=index,
            window_start=now - timedelta(days=2),
            window_end=now - timedelta(hours=index),
            digest={"summary_status": status},
        )
        if index in (4, 7):
            crud_governance_session_digest.update(
                db_session, db_obj=row, obj_in={"updated_at": now - timedelta(hours=1)}
            )
        if index in (1, 2, 4, 6, 7):
            expected.insert(0, row.id)
    assert (
        list_session_digest_retries(
            db_session, account_id=owner, stale_before=now - timedelta(minutes=10)
        )
        == expected
    )
    assert (
        list_session_digest_retries(
            db_session,
            account_id=owner,
            stale_before=now - timedelta(minutes=10),
            limit=2,
        )
        == expected[:2]
    )
    assert (
        list_session_digest_retries(
            db_session,
            account_id=owner,
            stale_before=now - timedelta(minutes=10),
            runtime_session_id=uuid4(),
        )
        == []
    )
    assert (
        list_session_digest_retries(db_session, account_id=uuid4(), stale_before=now)
        == []
    )


def test_retry_query_uses_partial_queue_index(db_session: Session) -> None:
    from unittest.mock import MagicMock
    from sqlalchemy import text
    from preloop.models.models import governance_review as gov

    fake = MagicMock()
    fake.scalars.return_value.all.return_value = []
    list_session_digest_retries(
        fake, account_id=uuid4(), stale_before=datetime.now(timezone.utc)
    )
    statement = fake.scalars.call_args.args[0]
    sql = str(
        statement.compile(
            dialect=db_session.get_bind().dialect,
            compile_kwargs={"literal_binds": True},
        )
    )
    assert gov.SESSION_DIGEST_RETRY_PREDICATE in sql
    assert gov.SESSION_DIGEST_CAPTURE_RETRY_PREDICATE in sql
    # Prove predicate/index eligibility independently of tiny-fixture cost estimates.
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(db_session.execute(text("EXPLAIN (COSTS OFF) " + sql)).scalars())
    assert "ix_governance_session_digest_retry_queue" in plan
    # Check each UNION branch separately: one indexed branch must not hide a
    # scan of all the account's completed segments in the other branch.
    branches = statement.get_final_froms()[0].element.selects
    assert len(branches) == 3
    for branch, index_name in zip(
        branches,
        (
            "ix_governance_session_digest_retry_queue",
            "ix_governance_session_digest_parse_retry",
            "ix_governance_session_digest_capture_retry",
        ),
        strict=True,
    ):
        branch_sql = str(
            branch.compile(
                dialect=db_session.get_bind().dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        branch_plan = "\n".join(
            db_session.execute(text("EXPLAIN (COSTS OFF) " + branch_sql)).scalars()
        )
        assert index_name in branch_plan
        assert "Index Cond:" in branch_plan and "account_id" in branch_plan


@pytest.mark.parametrize(
    "migration_name,predicate_name",
    [
        ("20261023_governance_retry_idx.py", "SESSION_DIGEST_RETRY_PREDICATE"),
        ("20261103_governance_parse_retry.py", "SESSION_DIGEST_PARSE_RETRY_PREDICATE"),
    ],
)
def test_retry_index_matches_frozen_migration(
    migration_name: str, predicate_name: str
) -> None:
    import importlib.util
    from pathlib import Path
    from preloop.models.models import governance_review as gov

    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions"
        / migration_name
    )
    spec = importlib.util.spec_from_file_location("retry_index_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PREDICATE == getattr(gov, predicate_name)
    index = next(
        i
        for i in models.GovernanceSessionDigest.__table__.indexes
        if i.name == module.INDEX
    )
    assert str(index.dialect_options["postgresql"]["where"]) == module.PREDICATE
    assert [column.name for column in index.columns] == [
        "account_id",
        "window_end",
        "runtime_session_id",
        "segment_index",
        "id",
    ]
