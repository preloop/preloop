"""Persist only execution credential fingerprints and apply them at boundaries."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.execution_credentials import CredentialMasker


def register(db: Session, execution_id: Any, values: Iterable[str]) -> CredentialMasker:
    """Atomically extend an execution's masking inventory before delivery.

    Args:
        db: Controller-owned database session.
        execution_id: Execution receiving the credentials.
        values: Issued credentials, used only to compute fingerprints.

    Returns:
        Masker covering every credential issued to this execution.
    """
    added = CredentialMasker.from_values(values).fingerprints
    if not added:
        return for_execution(db, execution_id)
    execution_uuid = UUID(str(execution_id))
    previous: Any = db.execute(
        select(models.FlowExecution.credential_fingerprints)
        .where(models.FlowExecution.id == execution_uuid)
        .with_for_update()
    ).scalar_one()
    fingerprints = {
        (item.get("hmac_sha256") or item.get("sha256")): item
        for item in [*(previous or []), *added]
    }
    db.execute(
        update(models.FlowExecution)
        .where(models.FlowExecution.id == execution_uuid)
        .values(credential_fingerprints=list(fingerprints.values()))
    )
    db.commit()
    return CredentialMasker(fingerprints.values())


def for_execution(db: Session, execution_id: Any) -> CredentialMasker:
    """Read current fingerprints without an ORM identity-map or process cache."""
    if execution_id is None:
        return CredentialMasker()
    try:
        execution_uuid = UUID(str(execution_id))
    except ValueError:
        return CredentialMasker()
    value = db.execute(
        select(models.FlowExecution.credential_fingerprints).where(
            models.FlowExecution.id == execution_uuid
        )
    ).scalar_one_or_none()
    return CredentialMasker(value if isinstance(value, list) else ())


def for_session(db: Session, runtime_session_id: Any) -> CredentialMasker:
    """Resolve the server-owned execution link for a session's output."""
    if runtime_session_id is None:
        return CredentialMasker()
    try:
        session_uuid = UUID(str(runtime_session_id))
    except ValueError:
        return CredentialMasker()
    row = db.execute(
        select(
            models.RuntimeSession.session_source_type,
            models.RuntimeSession.session_source_id,
            models.RuntimeSession.runtime_principal_type,
            models.RuntimeSession.runtime_principal_id,
        ).where(models.RuntimeSession.id == session_uuid)
    ).first()
    if row is None:
        return CredentialMasker()
    if row[0] == "flow_execution":
        return for_execution(db, row[1])
    if row[2] == "flow_execution":
        return for_execution(db, row[3])
    return CredentialMasker()
