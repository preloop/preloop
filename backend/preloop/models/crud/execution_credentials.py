"""Persist only execution credential fingerprints and apply them at boundaries."""

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.encryption import credential_fingerprint_key
from preloop.utils.execution_credentials import (
    CredentialMasker,
    credential_variants,
    matching_prefix,
)

FINGERPRINT_MODE = "exec_fingerprint_protection"
FINGERPRINT_BACKFILL = "cred_execution_fingerprints"


def protection_enabled(db: Session) -> bool:
    """Hold the rollout mode stable until this transaction completes."""
    return bool(
        db.scalar(
            select(models.AgentUsageBackfill.ready)
            .where(models.AgentUsageBackfill.id == FINGERPRINT_MODE)
            .with_for_update(read=True)
        )
    )


def set_protection(db: Session, enabled: bool) -> None:
    """Switch all writers together and resume or pause the historical backfill."""
    mode = db.execute(
        select(models.AgentUsageBackfill)
        .where(models.AgentUsageBackfill.id == FINGERPRINT_MODE)
        .with_for_update()
    ).scalar_one()
    if not enabled and db.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM agent_usage_backfill "
            "WHERE name = 'exec_hmac_writers') OR EXISTS (SELECT 1 FROM flow_execution "
            "WHERE credential_fingerprints @? "
            """'$[*] ? (exists(@.hmac_sha256) && !(exists(@.sha256)))'"""
            ")"
        )
    ):
        raise RuntimeError(
            "Keyed-only fingerprints require the dual-reader release or newer. "
            "Keep protection enabled when rolling back to that release."
        )
    if mode.ready == enabled:
        db.commit()
        return
    mode.ready = enabled
    db.execute(
        update(models.AgentUsageBackfill)
        .where(models.AgentUsageBackfill.id == FINGERPRINT_BACKFILL)
        .values(ready=not enabled, last_usage_id=None)
    )
    db.commit()


def stored_masker(fingerprints: Iterable[dict[str, Any]]) -> CredentialMasker:
    """Construct the server reader after ORM field decryption."""
    items = list(fingerprints)
    key = (
        credential_fingerprint_key() if any("hmac_sha256" in i for i in items) else b""
    )
    return CredentialMasker(items, key=key)


def issued_fingerprints(
    values: Iterable[str], *, protected: bool
) -> list[dict[str, Any]]:
    """Retain the previous reader's verifier for lossless downgrade.

    In protected mode the ORM encrypts the prefix and rollback digest at rest.
    The public matching tag is keyed with a separate deployment-derived key.
    """
    key = credential_fingerprint_key() if protected else b""
    result = {}
    for raw in credential_variants(values):
        legacy = hashlib.sha256(raw).hexdigest()
        item: dict[str, Any] = {
            "prefix": matching_prefix(raw),
            "length": len(raw),
            "sha256": legacy,
        }
        if protected:
            item["hmac_sha256"] = hmac.digest(key, raw, "sha256").hex()
        result[legacy] = item
    return list(result.values())


def save_inventory(
    db: Session, execution_id: UUID, items: list[dict[str, Any]], *, protected: bool
) -> None:
    """Write through the sealing type, or use previous-reader JSON during rollout."""
    db.execute(
        update(models.FlowExecution)
        .where(models.FlowExecution.id == execution_id)
        .values(
            credential_fingerprints=items if protected else type_coerce(items, JSONB)
        )
        .execution_options(synchronize_session=False)
    )


def register(db: Session, execution_id: Any, values: Iterable[str]) -> CredentialMasker:
    """Atomically extend an execution's masking inventory before delivery.

    Args:
        db: Controller-owned database session.
        execution_id: Execution receiving the credentials.
        values: Issued credentials, used only to compute fingerprints.

    Returns:
        Masker covering every credential issued to this execution.
    """
    protected = protection_enabled(db)
    added = issued_fingerprints(values, protected=protected)
    if not added:
        return for_execution(db, execution_id)
    execution_uuid = UUID(str(execution_id))
    previous: Any = db.execute(
        select(models.FlowExecution.credential_fingerprints)
        .where(models.FlowExecution.id == execution_uuid)
        .with_for_update()
    ).scalar_one()
    fingerprints = {
        (item.get("hmac_sha256") or item["sha256"]): item
        for item in [*(previous or []), *added]
    }
    items = list(fingerprints.values())
    save_inventory(db, execution_uuid, items, protected=protected)
    db.commit()
    return stored_masker(items)


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
    return stored_masker(value if isinstance(value, list) else ())


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
