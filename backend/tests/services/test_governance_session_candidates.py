"""Stage A candidate pages skip covered history and preserve pending arrivals."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_api_usage,
    crud_governance_session_digest,
    crud_runtime_session,
)
from preloop.models.crud.governance_session_candidates import (
    list_session_digest_candidates,
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
    from preloop.models.gateway_usage import AGENT_GATEWAY_PREDICATE

    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261017_governance_candidate_idx.py"
    )
    spec = importlib.util.spec_from_file_location("candidate_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PREDICATE == AGENT_GATEWAY_PREDICATE
    index = next(i for i in models.ApiUsage.__table__.indexes if i.name == module.INDEX)
    assert [column.name for column in index.columns] == [
        "account_id",
        "runtime_session_id",
        "timestamp",
    ]
    assert str(index.dialect_options["postgresql"]["where"]) == module.PREDICATE
