"""Real PostgreSQL concurrency tests, run by ordinary backend CI.

Use only a synthetic DATABASE_URL. Each test creates/drops its own random
schema, with minimal tenant/secret fixtures; no provider traffic is made.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, event, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.callback_receipt import (
    CallbackBindingConflictError,
    CallbackReplayConflictError,
    crud_callback_key_binding,
    crud_callback_receipt,
)


@pytest.fixture
def synthetic_database():
    """Create an isolated schema in CI's explicitly configured test database."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL DATABASE_URL required")
    schema = "callback_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))

    @event.listens_for(engine, "connect")
    def select_schema(connection, record) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f"SET search_path TO {schema}")
        connection.commit()

    engine.dispose()
    metadata = MetaData()
    account = Table(
        "account", metadata, Column("id", PGUUID(as_uuid=True), primary_key=True)
    )
    models.SecretReference.__table__.to_metadata(metadata)
    models.CallbackReceipt.__table__.to_metadata(metadata)
    models.CallbackKeyBinding.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    accounts = [uuid4(), uuid4()]
    integrations = [uuid4(), uuid4()]
    with engine.begin() as conn:
        conn.execute(account.insert(), [{"id": key} for key in accounts])
        conn.execute(
            metadata.tables["secret_reference"].insert(),
            [
                {
                    "id": key,
                    "account_id": owner,
                    "name": "synthetic callback",
                    "backend_type": "local",
                    "secret_kind": "synthetic_callback",
                    "status": "active",
                }
                for key, owner in zip(integrations, accounts, strict=True)
            ],
        )
    yield engine, accounts, integrations
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    engine.dispose()


def test_workers_complete_exactly_one_verdict(synthetic_database) -> None:
    engine, accounts, integrations = synthetic_database
    start = threading.Barrier(2)
    evaluations = []

    def worker() -> dict:
        with Session(engine) as db:
            start.wait(timeout=5)

            def evaluate(reference) -> tuple[dict, dict]:
                evaluations.append(reference)
                time.sleep(0.05)
                return {"action": "allow", "reference_id": str(reference)}, {
                    "reason": "policy_allowed"
                }

            row = crud_callback_receipt.complete_once(
                db,
                account_id=accounts[0],
                integration_id=integrations[0],
                delivery_digest="a" * 64,
                body_digest="b" * 64,
                evaluate=evaluate,
            )
            return dict(row.verdict)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert results[0] == results[1]
    assert len(evaluations) == 1
    with Session(engine) as db:
        assert db.query(models.CallbackReceipt).count() == 1
        with pytest.raises(CallbackReplayConflictError):
            crud_callback_receipt.complete_once(
                db,
                account_id=accounts[0],
                integration_id=integrations[0],
                delivery_digest="a" * 64,
                body_digest="c" * 64,
                evaluate=lambda ref: pytest.fail("Replay evaluated"),
            )


def test_rollback_allows_crash_recovery(synthetic_database) -> None:
    engine, accounts, integrations = synthetic_database
    args = {
        "account_id": accounts[0],
        "integration_id": integrations[0],
        "delivery_digest": "a" * 64,
        "body_digest": "b" * 64,
    }

    def fail(reference):
        raise RuntimeError("synthetic crash")

    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            crud_callback_receipt.complete_once(db, evaluate=fail, **args)
    with Session(engine) as db:
        assert db.query(models.CallbackReceipt).count() == 0
        row = crud_callback_receipt.complete_once(
            db,
            evaluate=lambda ref: ({"action": "deny"}, {"reason": "policy_denied"}),
            **args,
        )
        assert row.verdict == {"action": "deny"}


def test_cross_tenant_same_key_registration_is_atomic(synthetic_database) -> None:
    engine, accounts, integrations = synthetic_database
    start = threading.Barrier(2)

    def worker(index: int) -> str:
        with Session(engine) as db:
            start.wait(timeout=5)
            try:
                crud_callback_key_binding.bind(
                    db,
                    account_id=accounts[index],
                    integration_id=integrations[index],
                    signing_key_digest="c" * 64,
                    digest_epoch="d" * 32,
                    now=datetime.now(timezone.utc),
                )
                db.commit()
                return "bound"
            except CallbackBindingConflictError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == ["bound", "rejected"]
    with Session(engine) as db:
        assert db.query(models.CallbackKeyBinding).count() == 1


def test_rotation_expiry_and_epoch_change_do_not_reassign(synthetic_database) -> None:
    engine, accounts, integrations = synthetic_database
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        crud_callback_key_binding.bind(
            db,
            account_id=accounts[0],
            integration_id=integrations[0],
            signing_key_digest="c" * 64,
            digest_epoch="d" * 32,
            now=now,
        )
        db.commit()
        crud_callback_key_binding.bind(
            db,
            account_id=accounts[0],
            integration_id=integrations[0],
            signing_key_digest="e" * 64,
            digest_epoch="d" * 32,
            now=now,
        )
        crud_callback_key_binding.retire(
            db,
            account_id=accounts[0],
            integration_id=integrations[0],
            signing_key_digest="c" * 64,
            expires_at=now + timedelta(seconds=120),
        )
        db.commit()
        args = {
            "account_id": accounts[0],
            "integration_id": integrations[0],
            "signing_key_digest": "c" * 64,
            "digest_epoch": "d" * 32,
        }
        assert crud_callback_key_binding.assert_usable(db, now=now, **args)
        assert not crud_callback_key_binding.assert_usable(
            db, now=now + timedelta(seconds=120), **args
        )
        with pytest.raises(CallbackBindingConflictError):
            crud_callback_key_binding.bind(
                db,
                account_id=accounts[1],
                integration_id=integrations[1],
                signing_key_digest="c" * 64,
                digest_epoch="d" * 32,
                now=now + timedelta(seconds=121),
            )
        with pytest.raises(CallbackBindingConflictError):
            crud_callback_key_binding.bind(
                db,
                account_id=accounts[1],
                integration_id=integrations[1],
                signing_key_digest="f" * 64,
                digest_epoch="0" * 32,
                now=now,
            )
