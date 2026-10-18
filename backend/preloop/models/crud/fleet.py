"""Tenant-scoped fleet persistence. Callers own commit and notification delivery."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, func, or_, select, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


class CRUDFleetFinding:
    """Serialize lifecycle transitions so identical evaluations never notify twice."""

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        state: str | None = None,
        limit: int = 5000,
    ) -> list[models.FleetFinding]:
        """Read only the selected membership's findings."""
        query = select(models.FleetFinding).where(
            models.FleetFinding.account_id == account_id
        )
        if state is not None:
            query = query.where(models.FleetFinding.state == state)
        return list(
            db.scalars(
                query.order_by(
                    models.FleetFinding.opened_at.desc(), models.FleetFinding.id
                ).limit(max(0, min(limit, 5001)))
            )
        )

    def upsert(
        self,
        db: Session,
        *,
        account_id: UUID,
        rule: str,
        subject_key: str,
        state: str,
        now: datetime,
        evidence: dict[str, Any],
        limitation: str,
        device_key: str | None = None,
        user_id: UUID | None = None,
    ) -> tuple[models.FleetFinding, bool]:
        """Persist an evaluation and return whether its lifecycle changed.

        The boolean indicates a new open or state change, not a notification
        policy. The service decides whether a transition warrants delivery and
        records its outbox in the same transaction.
        """
        if state not in {"open", "resolved", "unknown", "suppressed"}:
            raise ValueError("Invalid finding state")
        model = models.FleetFinding
        values = dict(
            id=uuid4(),
            account_id=account_id,
            rule=rule,
            subject_key=subject_key,
            state=state,
            opened_at=now,
            last_evaluated_at=now,
            evidence=evidence,
            limitation=limitation,
            device_key=device_key,
            user_id=user_id,
            resolved_at=now if state == "resolved" else None,
        )
        inserted = db.scalar(
            insert(model)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_fleet_finding_subject")
            .returning(model.id)
        )
        row = db.scalar(
            select(model)
            .where(
                model.account_id == account_id,
                model.rule == rule,
                model.subject_key == subject_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        # A delayed evaluation must not overwrite fresher evidence.
        if now < row.last_evaluated_at:
            return row, False
        changed = inserted is not None or row.state != state
        previous_state = None if inserted is not None else row.state
        evidence_changed = inserted is not None or row.evidence != evidence
        if row.state == "resolved" and state == "open":
            row.opened_at = now
        row.state = state
        row.resolved_at = (row.resolved_at or now) if state == "resolved" else None
        row.last_evaluated_at = now
        row.evidence, row.limitation = evidence, limitation
        row.device_key, row.user_id = device_key, user_id
        if changed or evidence_changed:
            from preloop.models.crud import crud_audit_log

            crud_audit_log.log_action(
                db,
                account_id=account_id,
                action="configuration_change",
                resource_type="fleet_finding",
                resource_id=str(row.id),
                status="success",
                details={
                    "rule": rule,
                    "subject_key": subject_key,
                    "previous_state": previous_state,
                    "state": state,
                    "evidence": evidence,
                    "limitation": limitation,
                    "evaluated_at": now.isoformat(),
                },
                commit=False,
            )
        db.flush()
        return row, changed

    def list_account_ids(self, db: Session) -> list[UUID]:
        """List tenants with settings or durable findings for scheduled evaluation."""
        return list(
            db.scalars(
                union(
                    select(models.FleetAlertSettings.account_id),
                    select(models.FleetFinding.account_id),
                )
            )
        )

    def recent_agent_traffic(
        self,
        db: Session,
        *,
        account_id: UUID,
        since: datetime,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return governed gateway/MCP usage, excluding imports and replay."""
        from .api_usage import exclude_replay_usage_condition

        # Core usage and audit timestamps are stored as naive UTC.
        since = since.astimezone(UTC).replace(tzinfo=None) if since.tzinfo else since
        until = until or datetime.now(UTC)
        until = until.astimezone(UTC).replace(tzinfo=None) if until.tzinfo else until
        usage, agent = models.ApiUsage, models.ManagedAgent
        identity = or_(
            and_(
                usage.runtime_principal_type == "managed_agent",
                usage.runtime_principal_id == cast(agent.id, String),
            ),
            and_(
                usage.runtime_principal_type == agent.session_source_type,
                usage.runtime_principal_id == agent.session_source_id,
            ),
        )
        query = (
            select(agent.id, func.max(usage.timestamp).label("last_traffic_at"))
            .join(usage, and_(usage.account_id == agent.account_id, identity))
            .where(
                agent.account_id == account_id,
                agent.lifecycle_state != "decommissioned",
                usage.timestamp >= since,
                usage.timestamp <= until,
                usage.action_type == "model_gateway",
                or_(usage.usage_source.is_(None), usage.usage_source != "imported"),
                exclude_replay_usage_condition(),
            )
            .group_by(agent.id)
        )
        latest = {str(row.id): row.last_traffic_at for row in db.execute(query)}
        audit = models.AuditLog
        principal_type = audit.details["runtime_principal_type"].astext
        principal_id = audit.details["runtime_principal_id"].astext
        mcp_identity = or_(
            and_(
                principal_type == "managed_agent",
                principal_id == cast(agent.id, String),
            ),
            and_(
                principal_type == agent.session_source_type,
                principal_id == agent.session_source_id,
            ),
        )
        mcp = (
            select(agent.id, func.max(audit.timestamp).label("last_traffic_at"))
            .join(audit, and_(audit.account_id == agent.account_id, mcp_identity))
            .where(
                agent.account_id == account_id,
                agent.lifecycle_state != "decommissioned",
                audit.action == "tool_call",
                audit.timestamp >= since,
                audit.timestamp <= until,
                or_(
                    audit.details["purpose"].astext.is_(None),
                    audit.details["purpose"].astext != "replay_validation",
                ),
            )
            .group_by(agent.id)
        )
        for row in db.execute(mcp):
            key = str(row.id)
            latest[key] = max(latest.get(key, row.last_traffic_at), row.last_traffic_at)
        return [
            {"managed_agent_id": key, "last_traffic_at": value.replace(tzinfo=UTC)}
            for key, value in latest.items()
        ]


class CRUDFleetAlertSettings:
    """Account settings and their audit entry share the caller's transaction."""

    def get_for_account(
        self, db: Session, *, account_id: UUID
    ) -> models.FleetAlertSettings | None:
        """Read settings only within the selected account."""
        return db.scalar(
            select(models.FleetAlertSettings)
            .where(models.FleetAlertSettings.account_id == account_id)
            .execution_options(populate_existing=True)
        )

    def replace(
        self, db: Session, *, account_id: UUID, settings: dict[str, Any], user_id: UUID
    ) -> models.FleetAlertSettings:
        """Replace validated settings atomically with a configuration audit event."""
        from preloop.models.crud import crud_audit_log

        model = models.FleetAlertSettings
        db.execute(
            insert(model)
            .values(id=uuid4(), account_id=account_id, settings=settings)
            .on_conflict_do_update(
                index_elements=[model.account_id],
                set_={"settings": settings, "updated_at": func.now()},
            )
        )
        row = self.get_for_account(db, account_id=account_id)
        assert row is not None
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user_id,
            action="configuration_change",
            resource_type="fleet_alert_settings",
            resource_id=str(row.id),
            status="success",
            details={"settings": settings},
            commit=False,
        )
        return row


crud_fleet_finding = CRUDFleetFinding()
crud_fleet_alert_settings = CRUDFleetAlertSettings()
