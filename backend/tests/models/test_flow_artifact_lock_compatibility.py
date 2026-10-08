"""Artifact quota locks must exclude writers without blocking audit foreign keys."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account_halt, crud_audit_log
from preloop.models.crud import flow_artifact


@pytest.fixture
def artifact_values(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Commit synthetic parents for independent transaction visibility."""
    with Session(db_engine) as db:
        account = models.Account(organization_name="Artifact lock test")
        db.add(account)
        db.flush()
        flow = models.Flow(
            account_id=account.id,
            name="Artifact lock test",
            prompt_template="test",
            agent_type="codex",
            agent_config={},
        )
        db.add(flow)
        db.flush()
        execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
        db.add(execution)
        db.flush()
        values = {
            "account_id": account.id,
            "flow_id": flow.id,
            "execution_id": execution.id,
            "thread_id": str(uuid4()),
            "kind": "workspace",
            "manifest": {},
            "manifest_sha256": "0" * 64,
            "ciphertext": b"synthetic",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        db.commit()
    try:
        yield values
    finally:
        with Session(db_engine) as db:
            db.query(models.FlowArtifact).filter_by(
                account_id=values["account_id"]
            ).delete()
            db.query(models.FlowExecution).filter_by(id=values["execution_id"]).delete()
            db.query(models.Flow).filter_by(id=values["flow_id"]).delete()
            db.query(models.Account).filter_by(id=values["account_id"]).delete()
            db.commit()


def test_artifact_store_allows_concurrent_audit_insert(
    db_engine: Engine, artifact_values: dict[str, Any]
) -> None:
    """Exercise actual store CRUD while a child insert uses another connection."""
    audit_ids = []
    with Session(db_engine) as storage, Session(db_engine) as audit:

        def insert_audit_after_lock(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            # The quota aggregate runs after store acquires the account lock.
            if "sum(octet_length(" not in statement.lower():
                return
            audit.execute(text("SET LOCAL lock_timeout = '250ms'"))
            entry = crud_audit_log.log_action(
                audit,
                account_id=artifact_values["account_id"],
                action="artifact_checkpoint",
                resource_type="flow_execution",
                status="success",
            )
            audit_ids.append(entry.id)

        event.listen(
            storage.connection(), "before_cursor_execute", insert_audit_after_lock
        )
        artifact = flow_artifact.store(storage, values=artifact_values, quota_bytes=100)
        assert artifact.id is not None
        assert len(audit_ids) == 1


def test_artifact_store_serializes_writers_and_rechecks_quota(
    db_engine: Engine, artifact_values: dict[str, Any]
) -> None:
    """A contender cannot bypass admission or reuse an out-of-date quota total."""
    with Session(db_engine) as owner, Session(db_engine) as contender:
        crud_account_halt.lock_account(owner, account_id=artifact_values["account_id"])
        contender.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            flow_artifact.store(contender, values=artifact_values, quota_bytes=10)
        contender.rollback()
        owner.rollback()
        flow_artifact.store(contender, values=artifact_values, quota_bytes=10)
        with pytest.raises(ValueError, match="artifact_quota_exceeded") as refused:
            flow_artifact.store(contender, values=artifact_values, quota_bytes=10)
        contender.rollback()
        # The refusal carries the numbers admission compared (#1339).
        assert isinstance(refused.value, flow_artifact.ArtifactQuotaExceeded)
        assert refused.value.numbers() == {
            "retained_bytes": len(b"synthetic"),
            "quota_bytes": 10,
            "incoming_bytes": len(b"synthetic"),
        }


def test_lock_refreshes_stale_running_identity_after_concurrent_close(
    db_engine: Engine, artifact_values: dict[str, Any]
) -> None:
    from preloop.models.crud import crud_flow_execution

    execution_id = artifact_values["execution_id"]
    with Session(db_engine) as loader, Session(db_engine) as closer:
        loaded = loader.get(models.FlowExecution, execution_id)
        assert loaded is not None and loaded.status == "RUNNING"
        closing = closer.get(models.FlowExecution, execution_id)
        assert closing is not None
        closing.status = "FAILED"
        closer.commit()
        with pytest.raises(ValueError, match="artifact_execution_closed"):
            crud_flow_execution.lock_for_artifact_put(loader, execution_id=execution_id)
        assert loaded.status == "FAILED"


def test_quota_refusal_reports_exact_retained_quota_and_incoming(
    db_engine: Engine, artifact_values: dict[str, Any]
) -> None:
    """A known retained total is reported exactly; the comparison is unchanged."""
    with Session(db_engine) as db:
        flow_artifact.store(
            db, values={**artifact_values, "ciphertext": b"a" * 40}, quota_bytes=100
        )
        flow_artifact.store(
            db,
            values={**artifact_values, "kind": "evidence", "ciphertext": b"b" * 25},
            quota_bytes=100,
        )
        # Exactly at the quota is still admitted: 65 + 35 == 100.
        flow_artifact.store(
            db, values={**artifact_values, "ciphertext": b"c" * 35}, quota_bytes=100
        )
        with pytest.raises(flow_artifact.ArtifactQuotaExceeded) as refused:
            flow_artifact.store(
                db, values={**artifact_values, "ciphertext": b"d"}, quota_bytes=100
            )
        db.rollback()
        assert str(refused.value) == "artifact_quota_exceeded"
        assert refused.value.numbers() == {
            "retained_bytes": 100,
            "quota_bytes": 100,
            "incoming_bytes": 1,
        }


def test_usage_matches_admission_aggregate(
    db_engine: Engine, artifact_values: dict[str, Any]
) -> None:
    """usage() reports bytes by kind, pending cleanup and the next expiry."""
    now = datetime.now(UTC)
    soon = now + timedelta(hours=2)
    with Session(db_engine) as db:
        assert flow_artifact.usage(db, account_id=artifact_values["account_id"]) == {
            "retained_bytes": 0,
            "by_kind": {},
            "expired_pending_cleanup": 0,
            "next_expiry_at": None,
        }
        flow_artifact.store(
            db,
            values={**artifact_values, "ciphertext": b"a" * 40, "expires_at": soon},
            quota_bytes=1000,
        )
        flow_artifact.store(
            db,
            values={
                **artifact_values,
                "ciphertext": b"b" * 30,
                "expires_at": now + timedelta(hours=5),
            },
            quota_bytes=1000,
        )
        flow_artifact.store(
            db,
            values={
                **artifact_values,
                "kind": "evidence",
                "ciphertext": b"e" * 7,
                "expires_at": now - timedelta(minutes=1),
            },
            quota_bytes=1000,
        )
        cleared = flow_artifact.store(
            db,
            values={
                **artifact_values,
                "ciphertext": b"x",
                "expires_at": now - timedelta(hours=1),
            },
            quota_bytes=1000,
        )
        cleared.ciphertext = None
        cleared.availability = "expired"
        db.commit()
        report = flow_artifact.usage(db, account_id=artifact_values["account_id"])
        assert report["retained_bytes"] == 77
        assert report["by_kind"] == {
            "workspace": {"bytes": 70, "count": 3},
            "evidence": {"bytes": 7, "count": 1},
        }
        # Past expires_at with ciphertext still present: the evidence row only.
        assert report["expired_pending_cleanup"] == 1
        assert report["next_expiry_at"] == now - timedelta(minutes=1)
        # The same total admission compares: 77 + 24 > 100 refuses.
        with pytest.raises(flow_artifact.ArtifactQuotaExceeded) as refused:
            flow_artifact.store(
                db, values={**artifact_values, "ciphertext": b"z" * 24}, quota_bytes=100
            )
        db.rollback()
        assert refused.value.retained_bytes == report["retained_bytes"]
        other = flow_artifact.usage(db, account_id=uuid4())
        assert other["retained_bytes"] == 0 and other["by_kind"] == {}
