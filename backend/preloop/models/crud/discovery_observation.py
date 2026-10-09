"""Tenant-scoped immutable observation storage and 90-day retention."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from preloop.models import models
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .discovered_agent_candidate import CANDIDATE_RETENTION_DAYS


class ObservationConflictError(ValueError):
    """An observation identifier was reused with different content."""


class CRUDDiscoveryObservation:
    """Persist source evidence without mutating candidate rows or emitting events."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        workstation_fingerprint: str,
        source_ref: UUID,
        evidence: dict[str, Any],
        now: datetime | None = None,
    ) -> models.DiscoveryObservation:
        """Insert once, accepting exact replay and rejecting changed replay."""
        received = now or datetime.now(UTC)
        observation_id = UUID(evidence["observation_id"])
        digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        model = models.DiscoveryObservation
        key = (
            model.account_id == account_id,
            model.workstation_fingerprint == workstation_fingerprint,
            model.source_ref == source_ref,
            model.observation_id == observation_id,
        )
        db.execute(
            insert(model)
            .values(
                id=uuid.uuid4(),
                account_id=account_id,
                workstation_fingerprint=workstation_fingerprint,
                source_ref=source_ref,
                observation_id=observation_id,
                ended_at=datetime.fromisoformat(
                    evidence["ended_at"].replace("Z", "+00:00")
                ),
                received_at=received,
                content_hash=digest,
                evidence=evidence,
            )
            .on_conflict_do_nothing(constraint="uq_discovery_observation_source")
        )
        row = db.scalar(select(model).where(*key))
        if row is None:
            raise RuntimeError("Observation was not persisted")
        if row.content_hash != digest:
            raise ObservationConflictError(
                "Observation identifier already contains different evidence"
            )
        return row

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        workstation_fingerprint: str | None = None,
        source_ref: UUID | None = None,
        agent_kind: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> tuple[list[models.DiscoveryObservation], int]:
        """Read retained observations with filters before applying a visible cap."""
        model = models.DiscoveryObservation
        cutoff = (now or datetime.now(UTC)) - timedelta(days=CANDIDATE_RETENTION_DAYS)
        filters = [model.account_id == account_id, model.received_at >= cutoff]
        if workstation_fingerprint:
            filters.append(model.workstation_fingerprint == workstation_fingerprint)
        if source_ref:
            filters.append(model.source_ref == source_ref)
        if agent_kind:
            filters.append(
                model.evidence["applications"].contains([{"agent_kind": agent_kind}])
            )
        total = db.scalar(select(func.count()).select_from(model).where(*filters)) or 0
        rows = list(
            db.scalars(
                select(model)
                .where(*filters)
                .order_by(model.ended_at.desc(), model.id)
                .limit(limit)
            )
        )
        return rows, total

    def purge_expired(
        self, db: Session, *, account_id: UUID, now: datetime | None = None
    ) -> int:
        """Delete only the caller's retained evidence; caller owns transaction."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=CANDIDATE_RETENTION_DAYS)
        result = db.execute(
            delete(models.DiscoveryObservation).where(
                models.DiscoveryObservation.account_id == account_id,
                models.DiscoveryObservation.received_at < cutoff,
            )
        )
        return int(result.rowcount)


crud_discovery_observation = CRUDDiscoveryObservation()
