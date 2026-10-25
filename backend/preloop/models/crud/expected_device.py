"""Atomic tenant-local roster replacement and bounded source evidence reads."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from .discovered_agent_candidate import CANDIDATE_RETENTION_DAYS

MAX_ROSTER_DEVICES = 5000


class CRUDExpectedDevice:
    """Shared persistence only; commercial import policy lives in a plugin."""

    def replace(
        self, db: Session, *, account_id: UUID, devices: list[dict[str, Any]]
    ) -> list[models.ExpectedDevice]:
        """Replace one roster atomically, preserving known report history.

        Serialize concurrent imports on the account row. The caller commits.
        Attributes must already be privacy-filtered by the calling service.
        """
        if len(devices) > MAX_ROSTER_DEVICES:
            raise ValueError("Roster limit exceeded")
        refs = [item["device_ref_hash"] for item in devices]
        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate device reference")
        self.lock_account(db, account_id=account_id)
        model = models.ExpectedDevice
        db.execute(
            delete(model).where(
                model.account_id == account_id, model.device_ref_hash.not_in(refs)
            )
        )
        if devices:
            stmt = insert(model).values(
                [
                    {**item, "id": uuid.uuid4(), "account_id": account_id}
                    for item in devices
                ]
            )
            db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_expected_device_ref",
                    set_={
                        "platform": stmt.excluded.platform,
                        "attributes": stmt.excluded.attributes,
                        "updated_at": func.now(),
                    },
                )
            )
        # An administrator may import a roster after reports already exist.
        # Seed known receipt history so later retention cannot turn those
        # devices into a false never-reported result.
        observation = models.DiscoveryObservation
        candidate = models.DiscoveredAgentCandidate
        receipt = (
            select(func.max(observation.received_at))
            .where(
                observation.account_id == account_id,
                observation.workstation_fingerprint == model.device_ref_hash,
            )
            .correlate(model)
            .scalar_subquery()
        )
        legacy_receipt = (
            select(func.timezone("UTC", func.max(candidate.last_seen_at)))
            .where(
                candidate.account_id == account_id,
                candidate.workstation_fingerprint == model.device_ref_hash,
            )
            .correlate(model)
            .scalar_subquery()
        )
        db.execute(
            update(model)
            .where(model.account_id == account_id)
            .values(
                last_reported_at=func.greatest(
                    model.last_reported_at, receipt, legacy_receipt
                )
            )
        )
        db.flush()
        return self.list_for_account(db, account_id=account_id)

    def lock_account(self, db: Session, *, account_id: UUID) -> None:
        """Serialize imports/reports before report inserts acquire FK locks."""
        if (
            db.scalar(
                select(models.Account.id)
                .where(models.Account.id == account_id)
                .with_for_update(key_share=True)
            )
            is None
        ):
            raise ValueError("Account not found")

    def touch_last_reported_at(
        self,
        db: Session,
        *,
        account_id: UUID,
        device_ref_hash: str,
        at: datetime,
    ) -> None:
        """Preserve receipts across concurrent import, including absent rows.

        Report paths acquire the account NO KEY UPDATE lock before inserts.
        The same lock serializes this receipt update against
        roster replacement. It is compatible with report inserts' foreign-key
        KEY SHARE locks, avoiding lock-upgrade deadlocks between reporters.
        If an import won first, this statement sees its committed roster row;
        if the report won first, the import later seeds its committed receipt.
        The caller owns commit, so report data and receipt remain atomic.
        """
        self.lock_account(db, account_id=account_id)
        normalized_at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
        db.execute(
            update(models.ExpectedDevice)
            .where(
                models.ExpectedDevice.account_id == account_id,
                models.ExpectedDevice.device_ref_hash == device_ref_hash,
            )
            .values(
                last_reported_at=func.greatest(
                    models.ExpectedDevice.last_reported_at, normalized_at
                )
            )
        )

    def list_for_account(
        self, db: Session, *, account_id: UUID
    ) -> list[models.ExpectedDevice]:
        """Read only the requested tenant, ordered by opaque reference."""
        return list(
            db.scalars(
                select(models.ExpectedDevice)
                .where(models.ExpectedDevice.account_id == account_id)
                .order_by(models.ExpectedDevice.device_ref_hash)
                .execution_options(populate_existing=True)
            )
        )

    def latest_observations(
        self,
        db: Session,
        *,
        account_id: UUID,
        limit: int = 10000,
        now: datetime | None = None,
    ) -> tuple[list[models.DiscoveryObservation], int]:
        """Newest retained window per fingerprint and source, not last receipt.

        A cap cannot hide older windows behind repeated reports from one source.
        Callers must expose truncation and treat absent matches as unknown then.
        """
        model = models.DiscoveryObservation
        cutoff = (now or datetime.now(UTC)) - timedelta(days=CANDIDATE_RETENTION_DAYS)
        latest = (
            select(model.id)
            .where(model.account_id == account_id, model.received_at >= cutoff)
            .distinct(model.workstation_fingerprint, model.source_ref)
            .order_by(
                model.workstation_fingerprint,
                model.source_ref,
                model.ended_at.desc(),
                model.received_at.desc(),
                model.id,
            )
            .subquery()
        )
        total = db.scalar(select(func.count()).select_from(latest)) or 0
        rows = list(
            db.scalars(
                select(model)
                .join(latest, model.id == latest.c.id)
                .order_by(model.workstation_fingerprint, model.source_ref)
                .limit(limit)
            )
        )
        return rows, total


crud_expected_device = CRUDExpectedDevice()
