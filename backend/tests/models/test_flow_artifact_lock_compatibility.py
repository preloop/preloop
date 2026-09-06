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
        with pytest.raises(ValueError, match="artifact_quota_exceeded"):
            flow_artifact.store(contender, values=artifact_values, quota_bytes=10)
        contender.rollback()
