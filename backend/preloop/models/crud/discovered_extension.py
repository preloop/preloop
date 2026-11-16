"""Extension presence reconciliation; incomplete scans never prove removal."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.discovery_evidence import DiscoveryEvidence

from .expected_device import crud_expected_device
from .extension_approval import approval_join, decode_cursor, encode_cursor


class CRUDDiscoveredExtension:
    """Persist bounded reports and expose joined inventory for console and plugins."""

    def upsert_from_report(
        self,
        db: Session,
        *,
        account_id: UUID,
        workstation_fingerprint: str,
        evidence: DiscoveryEvidence,
        observation_id: UUID,
        now: datetime | None = None,
    ) -> None:
        """Union app contexts before reconciliation, serialized per account.

        A delayed or replayed observation cannot overwrite a newer context. Every
        context must be complete to remove an absent row from that profile/app.
        """
        crud_expected_device.lock_account(db, account_id=account_id)
        received = now or datetime.now(UTC)
        model = models.DiscoveredExtension
        contexts: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for app in evidence.applications:
            if app.extensions is not None:
                contexts[(app.profile_ref or "current", app.agent_kind)].append(app)
        for (profile_ref, agent_kind), apps in contexts.items():
            rows = list(
                db.scalars(
                    select(model).where(
                        model.account_id == account_id,
                        model.workstation_fingerprint == workstation_fingerprint,
                        model.profile_ref == profile_ref,
                        model.agent_kind == agent_kind,
                    )
                )
            )
            # Stored source windows, not client-controlled receipt times, determine
            # whether this report is newer than the current context snapshot.
            observation = models.DiscoveryObservation
            latest = db.scalar(
                select(func.max(observation.ended_at)).where(
                    observation.account_id == account_id,
                    observation.workstation_fingerprint == workstation_fingerprint,
                    observation.evidence["applications"].contains(
                        [
                            {
                                "agent_kind": agent_kind,
                                "profile_ref": None
                                if profile_ref == "current"
                                else profile_ref,
                            }
                        ]
                    ),
                    observation.evidence["schema_version"].as_integer() == 3,
                )
            )
            if (
                latest is not None
                and (
                    latest.replace(tzinfo=UTC)
                    if latest.tzinfo is None
                    else latest.astimezone(UTC)
                )
                > evidence.ended_at
            ):
                continue
            existing = {
                (
                    row.extension_kind,
                    row.extension_id,
                    row.scope,
                    row.project_ref_hash,
                ): row
                for row in rows
            }
            seen = set()
            for app in apps:
                for extension in app.extensions:
                    key = (
                        extension.kind,
                        extension.id,
                        extension.scope,
                        extension.project_ref_hash,
                    )
                    seen.add(key)
                    row = existing.get(key)
                    if row is None:
                        row = model(
                            account_id=account_id,
                            workstation_fingerprint=workstation_fingerprint,
                            profile_ref=profile_ref,
                            agent_kind=agent_kind,
                            extension_kind=extension.kind,
                            extension_id=extension.id,
                            scope=extension.scope,
                            project_ref_hash=extension.project_ref_hash,
                            first_seen_at=received,
                        )
                        db.add(row)
                        existing[key] = row
                    for field in (
                        "version",
                        "source_kind",
                        "source_ref_hash",
                        "enabled",
                        "content_hash",
                        "event",
                        "id_hashed",
                    ):
                        setattr(row, field, getattr(extension, field))
                    row.hook_count = extension.count
                    row.present = True
                    row.last_seen_at = received
                    row.last_observation_id = observation_id
            complete = all(app.completeness == "complete" for app in apps)
            # Failed profiles make their app claims insufficient for absence.
            complete = complete and all(
                profile.completeness == "complete"
                for profile in evidence.profiles
                if profile.profile_ref == profile_ref
            )
            if complete:
                for key, row in existing.items():
                    if key not in seen:
                        row.present = False
                        row.last_observation_id = observation_id
            db.flush()

    def completeness(
        self, db: Session, *, account_id: UUID, fingerprint: str | None = None
    ) -> str:
        """Describe the latest report per device without claiming unseen scope."""
        model = models.DiscoveryObservation
        query = select(model).where(model.account_id == account_id)
        if fingerprint:
            query = query.where(model.workstation_fingerprint == fingerprint)
        rows = list(
            db.scalars(
                query.distinct(model.workstation_fingerprint).order_by(
                    model.workstation_fingerprint,
                    model.ended_at.desc(),
                    model.received_at.desc(),
                )
            )
        )
        if not rows or any(row.evidence.get("schema_version", 1) < 3 for row in rows):
            return "unknown"
        if any(
            row.evidence.get("completeness") != "complete"
            or any(
                app.get("completeness") != "complete"
                for app in row.evidence.get("applications", [])
            )
            for row in rows
        ):
            return "partial"
        return "complete"

    def list_for_device(
        self, db: Session, *, account_id: UUID, fingerprint: str, **filters: Any
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Use the same tenant-safe join and pagination for a single device."""
        return self.list_for_account(
            db, account_id=account_id, fingerprint=fingerprint, **filters
        )

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        fingerprint: str | None = None,
        agent_kind: str | None = None,
        state: str | None = None,
        source_kind: str | None = None,
        present: bool = True,
        limit: int = 50,
        cursor: str | None = None,
        group: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return presence and raw name approvals, including fields future rules need.

        Name approval does not imply version compliance; callers evaluating a
        constraint can read version_constraint alongside observed version.
        """
        model, approval = models.DiscoveredExtension, models.ExtensionApproval
        approval_state = func.coalesce(approval.approval_state, "unknown")
        filters = [model.account_id == account_id, model.present == present]
        for column, value in (
            (model.workstation_fingerprint, fingerprint),
            (model.agent_kind, agent_kind),
            (approval_state, state),
            (model.source_kind, source_kind),
        ):
            if value is not None:
                filters.append(column == value)
        key = decode_cursor(cursor, 3 if group else 1)
        if group:
            columns = (model.agent_kind, model.extension_kind, model.extension_id)
            query = (
                select(
                    *columns,
                    func.count(func.distinct(model.workstation_fingerprint)),
                    func.array_agg(func.distinct(model.version)),
                    func.array_agg(func.distinct(model.source_kind)),
                    approval_state,
                )
                .outerjoin(approval, approval_join())
                .where(*filters)
                .group_by(*columns, approval_state)
                .order_by(*columns)
            )
            if key:
                query = query.where(tuple_(*columns) > tuple(key))
            rows = list(db.execute(query.limit(limit + 1)))
            items = [
                dict(
                    agent_kind=r[0],
                    extension_kind=r[1],
                    extension_id=r[2],
                    device_count=r[3],
                    versions=sorted(v for v in r[4] if v is not None),
                    source_kinds=sorted(r[5]),
                    approval_state=r[6],
                )
                for r in rows[:limit]
            ]
            next_cursor = (
                encode_cursor(list(rows[limit - 1][:3])) if len(rows) > limit else None
            )
        else:
            query = (
                select(model, approval)
                .outerjoin(approval, approval_join())
                .where(*filters)
                .order_by(model.id)
            )
            if key:
                query = query.where(model.id > UUID(key[0]))
            rows = list(db.execute(query.limit(limit + 1)))
            items = []
            for row, decision in rows[:limit]:
                item = row.to_dict()
                item.update(
                    fingerprint=row.workstation_fingerprint,
                    approval_state=decision.approval_state if decision else "unknown",
                    version_constraint=decision.version_constraint
                    if decision
                    else None,
                )
                items.append(item)
            next_cursor = (
                encode_cursor([str(rows[limit - 1][0].id)])
                if len(rows) > limit
                else None
            )
        return items, next_cursor


crud_discovered_extension = CRUDDiscoveredExtension()
