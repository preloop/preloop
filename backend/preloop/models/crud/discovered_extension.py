"""Extension presence reconciliation; incomplete scans never prove removal."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import String, and_, func, or_, select, tuple_
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.discovery_evidence import DiscoveryEvidence

from .expected_device import crud_expected_device
from .extension_approval import (
    approval_join,
    crud_extension_approval,
    decode_cursor,
    effective_state,
    effective_state_sql,
    encode_cursor,
)

CONTENT_HASH_BUCKETS = 20


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
                    observation.evidence.contains(
                        {
                            "applications": [
                                {
                                    "agent_kind": agent_kind,
                                    "extensions": [],
                                    "profile_ref": None
                                    if profile_ref == "current"
                                    else profile_ref,
                                }
                            ],
                        }
                    ),
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
                        "content_hash_status",
                        "event",
                        "id_hashed",
                    ):
                        setattr(row, field, getattr(extension, field))
                    row.details = (
                        extension.details.model_dump(mode="json")
                        if extension.details is not None
                        else None
                    )
                    row.hook_count = extension.count
                    row.present = True
                    row.last_seen_at = received
                    row.last_observation_id = observation_id
            complete = evidence.completeness != "failed" and all(
                app.completeness == "complete" for app in apps
            )
            # Failed profiles make their app claims insufficient for absence.
            complete = complete and all(
                profile.completeness != "failed"
                for profile in evidence.profiles
                if profile.profile_ref == profile_ref
            )
            if complete:
                for key, row in existing.items():
                    if key not in seen:
                        row.present = False
                        row.last_observation_id = observation_id
            db.flush()

    def coverage(
        self, db: Session, *, account_id: UUID, fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        """Return coverage facts from each device's latest account-local report."""
        model = models.DiscoveryObservation
        query = select(model.id, model.workstation_fingerprint).where(
            model.account_id == account_id
        )
        if fingerprint:
            query = query.where(model.workstation_fingerprint == fingerprint)
        latest = (
            query.distinct(model.workstation_fingerprint)
            .order_by(
                model.workstation_fingerprint,
                model.ended_at.desc(),
                model.received_at.desc(),
            )
            .subquery()
        )
        # First select one id per device through the ordered index. Only then
        # extract bounded coverage metadata; never transfer extension payloads.
        rows = db.execute(
            select(
                model.workstation_fingerprint,
                model.evidence["collector_version"].as_string(),
                model.evidence["schema_version"].as_integer(),
                func.coalesce(
                    model.evidence["extension_completeness"].as_string(),
                    model.evidence["scope_completeness"]["user_config"].as_string(),
                    model.evidence["completeness"].as_string(),
                ),
                model.evidence["scan_scope"].contains(["user_config"]),
                model.evidence["extension_errors"],
                model.evidence["errors"],
                model.ended_at,
            ).join(latest, latest.c.id == model.id)
        )
        result = []
        for row in rows:
            device, collector, version, state, user_scope, errors, generic = row[:7]
            reported_at = row[7]
            completeness = (
                "unknown"
                if (version or 1) < 3 or not user_scope
                else "complete"
                if state == "complete"
                else "partial"
            )
            result.append(
                {
                    "fingerprint": device,
                    "collector_version": collector,
                    "completeness": completeness,
                    "reasons": errors or generic or [],
                    "reported_at": reported_at,
                }
            )
        return result

    def completeness(
        self, db: Session, *, account_id: UUID, fingerprint: str | None = None
    ) -> Literal["complete", "partial", "unknown"]:
        """Describe the latest report per device without claiming unseen scope."""
        rows = self.coverage(db, account_id=account_id, fingerprint=fingerprint)
        return self.coverage_completeness(rows)

    @staticmethod
    def coverage_completeness(
        rows: list[dict[str, Any]],
    ) -> Literal["complete", "partial", "unknown"]:
        """Combine coverage, preserving unknown scope across devices."""
        if not rows or any(row["completeness"] == "unknown" for row in rows):
            return "unknown"
        return (
            "complete"
            if all(row["completeness"] == "complete" for row in rows)
            else "partial"
        )

    def count_for_account(self, db: Session, *, account_id: UUID) -> int:
        """Count distinct present extension identities without loading inventory."""
        model = models.DiscoveredExtension
        identities = (
            select(model.agent_kind, model.extension_kind, model.extension_id)
            .where(model.account_id == account_id, model.present.is_(True))
            .distinct()
            .subquery()
        )
        return int(db.scalar(select(func.count()).select_from(identities)) or 0)

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
        q: str | None = None,
        extension_kind: str | None = None,
        extension_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return presence, raw approvals and content-pinned effective state.

        Name approval does not imply version compliance; callers evaluating a
        constraint can read version_constraint alongside observed version.
        ``state`` filters on the per-device effective state, so in group mode
        an aggregate counts only the devices in that state.
        """
        model, approval = models.DiscoveredExtension, models.ExtensionApproval
        approval_state = func.coalesce(approval.approval_state, "unknown")
        filters = [model.account_id == account_id, model.present == present]
        for column, value in (
            (model.workstation_fingerprint, fingerprint),
            (model.agent_kind, agent_kind),
            (effective_state_sql(), state),
            (model.source_kind, source_kind),
            (model.extension_kind, extension_kind),
            (model.extension_id, extension_id),
        ):
            if value is not None:
                filters.append(column == value)
        if q and q.strip():
            term = q.strip()
            matches = or_(
                model.extension_id.icontains(term, autoescape=True),
                model.details["name"].as_string().icontains(term, autoescape=True),
                model.details["description"]
                .as_string()
                .icontains(term, autoescape=True),
            )
            if group:
                # Search selects extension keys. Every observation for those
                # keys still contributes to counts and the worst effective state.
                columns = (model.agent_kind, model.extension_kind, model.extension_id)
                matching_keys = select(*columns).where(
                    model.account_id == account_id, model.present == present, matches
                )
                filters.append(tuple_(*columns).in_(matching_keys))
            else:
                filters.append(matches)
        display_name = func.coalesce(
            func.nullif(model.details["name"].as_string(), ""), model.extension_id
        )
        key = decode_cursor(cursor, 4 if group else 3, max_value_length=300)
        if group:
            columns = (model.agent_kind, model.extension_kind, model.extension_id)
            # Pick the same latest matching observation for both display and
            # sorting, before paginating the grouped inventory.
            latest_details = (
                select(*columns, model.details, display_name.label("display_name"))
                .outerjoin(approval, approval_join())
                .where(*filters)
                .distinct(*columns)
                .order_by(*columns, model.last_seen_at.desc(), model.id.desc())
                .subquery()
            )
            ordering = (
                model.extension_kind,
                latest_details.c.display_name,
                model.extension_id,
                model.agent_kind,
            )
            query = (
                select(
                    *columns,
                    func.count(func.distinct(model.workstation_fingerprint)),
                    func.array_agg(func.distinct(model.version)),
                    func.array_agg(func.distinct(model.source_kind)),
                    approval_state,
                    func.max(approval.id.cast(String)),
                    func.bool_or(effective_state_sql() == "content_changed"),
                    func.bool_or(effective_state_sql() == "content_unverified"),
                    latest_details.c.details,
                    latest_details.c.display_name,
                )
                .outerjoin(approval, approval_join())
                .where(*filters)
                .join(
                    latest_details,
                    and_(
                        model.agent_kind == latest_details.c.agent_kind,
                        model.extension_kind == latest_details.c.extension_kind,
                        model.extension_id == latest_details.c.extension_id,
                    ),
                )
                .group_by(
                    *columns,
                    approval_state,
                    latest_details.c.details,
                    latest_details.c.display_name,
                )
                .order_by(*ordering)
            )
            if key:
                query = query.where(tuple_(*ordering) > tuple(key))
            rows = list(db.execute(query.limit(limit + 1)))
            page = rows[:limit]
            pins = crud_extension_approval.pins_for(
                db, approval_ids=[UUID(r[7]) for r in page if r[7]]
            )
            buckets = self._content_hashes(
                db, account_id=account_id, keys=[tuple(r[:3]) for r in page]
            )
            items = []
            for r in page:
                pinned = (
                    {p["content_hash"] for p in pins.get(UUID(r[7]), [])}
                    if r[7]
                    else set()
                )
                items.append(
                    dict(
                        details=r[10],
                        agent_kind=r[0],
                        extension_kind=r[1],
                        extension_id=r[2],
                        device_count=r[3],
                        versions=sorted(v for v in r[4] if v is not None),
                        source_kinds=sorted(r[5]),
                        approval_state=r[6],
                        # Worst state over exactly the rows counted above, so
                        # filters and bucket truncation cannot change the badge.
                        effective_state=(
                            r[6]
                            if r[6] != "approved"
                            else "content_changed"
                            if r[8]
                            else "content_unverified"
                            if r[9]
                            else "approved"
                        ),
                        pin_count=len(pinned),
                        content_hashes=hash_buckets(
                            buckets.get(tuple(r[:3]), []), pinned
                        ),
                    )
                )
            next_cursor = (
                encode_cursor(
                    [
                        rows[limit - 1][1],
                        rows[limit - 1][11],
                        rows[limit - 1][2],
                        rows[limit - 1][0],
                    ]
                )
                if len(rows) > limit
                else None
            )
        else:
            device_ordering = (model.extension_kind, display_name, model.id)
            device_query = (
                select(model, approval)
                .outerjoin(approval, approval_join())
                .where(*filters)
                .order_by(*device_ordering)
            )
            if key:
                device_query = device_query.where(
                    tuple_(*device_ordering) > (key[0], key[1], UUID(key[2]))
                )
            rows = list(db.execute(device_query.limit(limit + 1)))
            pins = crud_extension_approval.pins_for(
                db, approval_ids={d.id for _, d in rows[:limit] if d is not None}
            )
            items = []
            for row, decision in rows[:limit]:
                item = row.to_dict()
                item.update(
                    fingerprint=row.workstation_fingerprint,
                    approval_state=decision.approval_state if decision else "unknown",
                    effective_state=effective_state(
                        row,
                        decision,
                        [p["content_hash"] for p in pins.get(decision.id, [])]
                        if decision
                        else [],
                    ),
                    version_constraint=decision.version_constraint
                    if decision
                    else None,
                )
                items.append(item)
            next_cursor = (
                encode_cursor(
                    [
                        rows[limit - 1][0].extension_kind,
                        (rows[limit - 1][0].details or {}).get("name")
                        or rows[limit - 1][0].extension_id,
                        str(rows[limit - 1][0].id),
                    ]
                )
                if len(rows) > limit
                else None
            )
        return items, next_cursor

    def _content_hashes(
        self,
        db: Session,
        *,
        account_id: UUID,
        keys: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], list[tuple[str | None, str, int]]]:
        """Count present (device, profile) pairs per content hash for each key.

        Null hashes form one bucket whose status is the greatest reported one
        (``unreadable`` > ``too_large`` > ``ok``).
        """
        if not keys:
            return {}
        model = models.DiscoveredExtension
        columns = (model.agent_kind, model.extension_kind, model.extension_id)
        rows = db.execute(
            select(
                *columns,
                model.content_hash,
                func.max(model.content_hash_status),
                func.count(
                    func.distinct(
                        tuple_(model.workstation_fingerprint, model.profile_ref)
                    )
                ),
            )
            .where(
                model.account_id == account_id,
                model.present.is_(True),
                tuple_(*columns).in_(keys),
            )
            .group_by(*columns, model.content_hash)
        )
        result: dict[tuple[str, str, str], list[tuple[str | None, str, int]]] = (
            defaultdict(list)
        )
        for kind, ext_kind, ext_id, content_hash, status, count in rows:
            result[(kind, ext_kind, ext_id)].append((content_hash, status, count))
        return result


def hash_buckets(
    observed: list[tuple[str | None, str, int]], pinned: set[str]
) -> list[dict[str, Any]]:
    """Most common hashes first (ties by hash, null last), top 20, plus all pins."""
    ordered = sorted(observed, key=lambda b: (-b[2], b[0] is None, b[0] or ""))
    top = ordered[:CONTENT_HASH_BUCKETS]
    shown = {b[0] for b in top}
    counts = {b[0]: b for b in ordered}
    for content_hash in sorted(pinned - shown):
        top.append(counts.get(content_hash, (content_hash, "ok", 0)))
    return [
        dict(
            content_hash=content_hash,
            status=status if content_hash is None else "ok",
            device_count=count,
            pinned=content_hash in pinned,
        )
        for content_hash, status, count in top
    ]


crud_discovered_extension = CRUDDiscoveredExtension()
