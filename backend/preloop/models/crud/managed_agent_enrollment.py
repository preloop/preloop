"""CRUD operations for durable managed-agent enrollment state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.redaction import redact_agent_config

from .base import CRUDBase

ManagedAgentEnrollment = models.ManagedAgentEnrollment

# Statuses of an enrollment that once reached a validated state. A restored
# enrollment was validated before the operator rolled the config back.
ONBOARDED_ENROLLMENT_STATUSES = ("validated", "restored")

# Manual mode: the CLI could not write the local agent config, printed the
# change for the user to apply by hand and is waiting for ``--resume``. Such a
# row is not onboarded. It ends ``validated`` (resumed), ``cancelled`` (the
# user gave up) or ``expired`` (never resumed within the TTL).
ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG = "pending_local_config"
ENROLLMENT_STATUS_CANCELLED = "cancelled"
ENROLLMENT_STATUS_EXPIRED = "expired"
# Statuses a resume may move a pending enrollment to.
PENDING_RESUME_STATUSES = ("validated", "applied")
CLOSED_PENDING_ENROLLMENT_STATUSES = (
    ENROLLMENT_STATUS_CANCELLED,
    ENROLLMENT_STATUS_EXPIRED,
)


def pending_enrollment_ttl() -> timedelta:
    """Return how long an enrollment may wait in ``pending_local_config``."""
    from preloop.config import settings

    return timedelta(hours=max(1, int(settings.preloop_pending_enrollment_ttl_hours)))


def pending_enrollment_expires_at(
    status: Optional[str], created_at: Optional[datetime]
) -> Optional[datetime]:
    """Return when a pending enrollment expires.

    Args:
        status: Enrollment status.
        created_at: When the enrollment was recorded.

    Returns:
        ``created_at`` plus the TTL for a ``pending_local_config`` enrollment,
        otherwise ``None``.
    """
    if status != ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG or created_at is None:
        return None
    return created_at + pending_enrollment_ttl()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CRUDManagedAgentEnrollment(CRUDBase[ManagedAgentEnrollment]):
    """CRUD helpers for managed-agent enrollment records."""

    def gateway_verified_enrollments(
        self, db: Session, *, account_id: str, enrollment_ids: list[str]
    ) -> set[str]:
        """Find enrollments with successful gateway traffic since application.

        Only server-recorded model requests authenticated by an agent-bound key
        count. Client telemetry, validation claims, sibling agents and traffic
        predating the current configuration cannot establish this evidence.

        Args:
            db: Active database session.
            account_id: Owning account.
            enrollment_ids: Current enrollments on the requested console page.

        Returns:
            Enrollment ids whose route has carried a successful request.
        """
        if not enrollment_ids:
            return set()
        usage = models.ApiUsage
        key = models.ApiKey
        success = (
            db.query(usage.id)
            .join(key, key.id == usage.api_key_id)
            .filter(
                usage.account_id == account_id,
                key.account_id == account_id,
                key.context_data.op("->>")("managed_agent_id")
                == cast(self.model.managed_agent_id, String),
                usage.action_type == "model_gateway",
                usage.status_code >= 200,
                usage.status_code < 300,
                usage.timestamp
                >= func.coalesce(self.model.last_applied_at, self.model.created_at),
            )
            .correlate(self.model)
            .exists()
        )
        rows = (
            db.query(self.model.id)
            .filter(
                self.model.account_id == account_id,
                self.model.id.in_(enrollment_ids),
                self.model.last_restored_at.is_(None),
                self.model.status != "restored",
                success,
            )
            .all()
        )
        return {str(row[0]) for row in rows}

    def list_latest_for_fleet_map(
        self,
        db: Session,
        *,
        account_id: str,
        agent_ids: list[str],
        limit: int,
    ) -> list[ManagedAgentEnrollment]:
        """Read latest enrollment per agent/type with a bounded result window.

        Args:
            db: Active database session.
            account_id: Owning tenant.
            agent_ids: Visible legacy agents without binding rows.
            limit: Source window including a truncation sentinel.

        Returns:
            Latest records, without loading historical enrollment rows.
        """
        if not agent_ids:
            return []
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id.in_(agent_ids),
            )
            .distinct(self.model.managed_agent_id, self.model.enrollment_type)
            .order_by(
                self.model.managed_agent_id,
                self.model.enrollment_type,
                self.model.created_at.desc(),
                self.model.id.desc(),
            )
            .limit(limit)
            .all()
        )

    def get_latest_for_agent(
        self, db: Session, *, account_id: str, agent_id: str
    ) -> Optional[ManagedAgentEnrollment]:
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
            )
            .order_by(self.model.created_at.desc())
            .first()
        )

    def list_latest_by_agents(
        self,
        db: Session,
        *,
        account_id: str,
        agent_ids: list[str],
    ) -> dict[str, dict[str, Optional[ManagedAgentEnrollment]]]:
        """Return latest enrollment picks for many agents in one query.

        Each agent id maps to a dict with:
        - ``cli_managed_config``: latest CLI managed-config enrollment
        - ``runtime_plugin_control``: latest runtime plugin control enrollment
        - ``any``: latest enrollment of any type

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            agent_ids: Managed agent ids to load enrollments for.

        Returns:
            Mapping from agent id string to the enrollment picks above.
        """
        normalized_ids = [str(agent_id) for agent_id in agent_ids]
        if not normalized_ids:
            return {}

        rows = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id.in_(normalized_ids),
            )
            .order_by(self.model.created_at.desc())
            .all()
        )

        result: dict[str, dict[str, Optional[ManagedAgentEnrollment]]] = {
            agent_id: {
                "cli_managed_config": None,
                "runtime_plugin_control": None,
                "any": None,
            }
            for agent_id in normalized_ids
        }
        for row in rows:
            bucket = result.get(str(row.managed_agent_id))
            if bucket is None:
                continue
            if bucket["any"] is None:
                bucket["any"] = row
            if (
                row.enrollment_type == "cli_managed_config"
                and bucket["cli_managed_config"] is None
            ):
                bucket["cli_managed_config"] = row
            if (
                row.enrollment_type == "runtime_plugin_control"
                and bucket["runtime_plugin_control"] is None
            ):
                bucket["runtime_plugin_control"] = row
        return result

    def list_for_agent(
        self, db: Session, *, account_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
            )
            .order_by(self.model.created_at.desc())
            .all()
        )
        return [self._to_summary(row) for row in rows]

    def create_for_agent(
        self,
        db: Session,
        *,
        account_id: Any,
        agent_id: Any,
        created_by_user_id: Any,
        enrollment_type: str,
        adapter_key: Optional[str] = None,
        status: str = "active",
        target_config_path: Optional[str] = None,
        discovered_config: Optional[dict[str, Any]] = None,
        managed_config: Optional[dict[str, Any]] = None,
        backup_metadata: Optional[dict[str, Any]] = None,
        validation_result: Optional[dict[str, Any]] = None,
        restore_available: bool = False,
        last_applied_at: Optional[datetime] = None,
        last_validated_at: Optional[datetime] = None,
        last_restored_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> ManagedAgentEnrollment:
        db_obj = ManagedAgentEnrollment(
            account_id=account_id,
            managed_agent_id=agent_id,
            created_by_user_id=created_by_user_id,
            enrollment_type=enrollment_type,
            adapter_key=adapter_key,
            status=status,
            target_config_path=target_config_path,
            # Client JSON: redacted on the way in, whatever the CLI sanitised.
            discovered_config=redact_agent_config(discovered_config or {}),
            managed_config=redact_agent_config(managed_config or {}),
            backup_metadata=redact_agent_config(backup_metadata or {}),
            validation_result=redact_agent_config(validation_result or {}),
            restore_available=restore_available,
            last_applied_at=last_applied_at,
            last_validated_at=last_validated_at,
            last_restored_at=last_restored_at,
        )
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def get_latest_for_agent_by_type(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        enrollment_type: str,
    ) -> Optional[ManagedAgentEnrollment]:
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
                self.model.enrollment_type == enrollment_type,
            )
            .order_by(self.model.created_at.desc())
            .first()
        )

    def get_for_agent(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        enrollment_id: str,
        for_update: bool = False,
    ) -> Optional[ManagedAgentEnrollment]:
        """Return one enrollment scoped to its account and agent.

        Args:
            db: Database session.
            account_id: Account the enrollment must belong to.
            agent_id: Managed agent the enrollment must belong to.
            enrollment_id: Enrollment id.
            for_update: Take a row lock so a status decision (resume, cancel,
                expire) cannot interleave with a concurrent one.

        Returns:
            The enrollment, or ``None``.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.managed_agent_id == agent_id,
            self.model.id == enrollment_id,
        )
        if for_update:
            query = query.with_for_update().populate_existing()
        return query.first()

    def has_onboarded_enrollment(
        self, db: Session, *, account_id: str, agent_id: str
    ) -> bool:
        """Tell whether the agent ever completed an onboarding.

        Args:
            db: Database session.
            account_id: Account the agent belongs to.
            agent_id: Managed agent id.

        Returns:
            True when an enrollment of the agent is validated or restored.
        """
        return (
            db.query(self.model.id)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
                self.model.status.in_(ONBOARDED_ENROLLMENT_STATUSES),
            )
            .first()
            is not None
        )

    def get_pending_for_agent(
        self, db: Session, *, account_id: str, agent_id: str
    ) -> Optional[ManagedAgentEnrollment]:
        """Return the agent's latest enrollment waiting for local config.

        Args:
            db: Database session.
            account_id: Account the agent belongs to.
            agent_id: Managed agent id.

        Returns:
            The newest ``pending_local_config`` enrollment, or ``None``.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
                self.model.status == ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG,
            )
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .first()
        )

    def list_stale_pending(
        self, db: Session, *, created_before: datetime, limit: int = 200
    ) -> list[ManagedAgentEnrollment]:
        """List pending enrollments older than the TTL, across accounts.

        Args:
            db: Database session.
            created_before: Cutoff as a naive UTC timestamp, like
                ``created_at`` is stored.
            limit: Upper bound for one sweep.

        Returns:
            Stale ``pending_local_config`` enrollments, oldest first.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.status == ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG,
                self.model.created_at < created_before,
            )
            .order_by(self.model.created_at.asc(), self.model.id.asc())
            .limit(limit)
            .all()
        )

    def close_pending(
        self,
        db: Session,
        *,
        enrollment: ManagedAgentEnrollment,
        status: str,
    ) -> ManagedAgentEnrollment:
        """Move a pending enrollment to ``cancelled`` or ``expired``.

        Flushes without committing: the caller cleans up in the same
        transaction.

        Args:
            db: Database session.
            enrollment: A ``pending_local_config`` enrollment.
            status: ``cancelled`` or ``expired``.

        Returns:
            The updated enrollment.

        Raises:
            ValueError: If ``status`` is not a closed pending status or the
                enrollment is not pending.
        """
        if status not in CLOSED_PENDING_ENROLLMENT_STATUSES:
            raise ValueError(f"Invalid closed enrollment status {status!r}")
        if enrollment.status != ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG:
            raise ValueError("Only a pending enrollment can be closed")
        enrollment.status = status
        enrollment.restore_available = False
        db.add(enrollment)
        db.flush()
        return enrollment

    def latest_prior_onboarding_at(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        exclude_enrollment_id: str,
    ) -> Optional[datetime]:
        """Return when another enrollment of this agent was last onboarded.

        An enrollment counts once it was validated, including one that was
        later restored (the operator un-onboarded it). Failed or pending
        attempts do not count.

        Args:
            db: Database session.
            account_id: Account the agent belongs to.
            agent_id: Managed agent id.
            exclude_enrollment_id: The enrollment being validated now.

        Returns:
            The latest validation time, or ``None`` if there is none.
        """
        return (
            db.query(func.max(self.model.last_validated_at))
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
                self.model.id != exclude_enrollment_id,
                self.model.status.in_(ONBOARDED_ENROLLMENT_STATUSES),
                self.model.last_validated_at.isnot(None),
            )
            .scalar()
        )

    def upsert_runtime_bootstrap(
        self,
        db: Session,
        *,
        account_id: Any,
        agent_id: Any,
        created_by_user_id: Any,
        session_source_type: str,
        session_source_id: str,
        session_reference: Optional[str],
        display_name: str,
        managed_mcp_servers: list[str],
        runtime_session_id: Any,
        commit: bool = True,
    ) -> ManagedAgentEnrollment:
        db_obj = self.get_latest_for_agent_by_type(
            db,
            account_id=str(account_id),
            agent_id=str(agent_id),
            enrollment_type="runtime_session_bootstrap",
        )
        now = _utc_now()
        discovered_config = {
            "session_source_type": session_source_type,
            "session_source_id": session_source_id,
            "session_reference": session_reference,
            "display_name": display_name,
        }
        managed_config = {
            "managed_mcp_servers": managed_mcp_servers,
            "runtime_session_id": str(runtime_session_id),
        }
        if db_obj is None:
            return self.create_for_agent(
                db,
                account_id=account_id,
                agent_id=agent_id,
                created_by_user_id=created_by_user_id,
                enrollment_type="runtime_session_bootstrap",
                adapter_key=session_source_type,
                status="active",
                target_config_path=session_reference,
                discovered_config=discovered_config,
                managed_config=managed_config,
                validation_result={"bootstrap": True},
                restore_available=False,
                last_applied_at=now,
                last_validated_at=now,
                commit=commit,
            )

        db_obj.created_by_user_id = created_by_user_id
        db_obj.adapter_key = session_source_type
        db_obj.status = "active"
        db_obj.target_config_path = session_reference
        db_obj.discovered_config = discovered_config
        db_obj.managed_config = managed_config
        db_obj.validation_result = {"bootstrap": True}
        db_obj.last_applied_at = now
        db_obj.last_validated_at = now
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def mark_validated(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        enrollment_id: str,
        validation_result: Optional[dict[str, Any]] = None,
        status: str = "validated",
        commit: bool = True,
    ) -> Optional[ManagedAgentEnrollment]:
        db_obj = self.get_for_agent(
            db,
            account_id=account_id,
            agent_id=agent_id,
            enrollment_id=enrollment_id,
        )
        if db_obj is None:
            return None
        if db_obj.status == ENROLLMENT_STATUS_PENDING_LOCAL_CONFIG:
            # ``validated`` and ``applied`` (the user confirmed the change and
            # no model probe applies) both end the wait. Any other status is
            # a resume that failed: the enrollment stays pending, so the user
            # can retry and the expiry sweep still cleans it up.
            if status in PENDING_RESUME_STATUSES:
                db_obj.status = status
                if db_obj.last_applied_at is None:
                    # The user applied the config by hand before resuming.
                    db_obj.last_applied_at = _utc_now()
        else:
            db_obj.status = status
        incoming = redact_agent_config(validation_result or {})
        if any(key.startswith("control_") for key in incoming):
            # A full static validation reports the control keys itself and
            # must be able to turn them off.
            db_obj.validation_result = incoming
        else:
            # A live-only update (deferred live validation) carries no control
            # keys. Keep what onboarding recorded and overlay the new keys.
            db_obj.validation_result = redact_agent_config(
                {
                    **(db_obj.validation_result or {}),
                    **incoming,
                }
            )
        db_obj.last_validated_at = _utc_now()
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def mark_restored(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        enrollment_id: str,
        backup_metadata: Optional[dict[str, Any]] = None,
        validation_result: Optional[dict[str, Any]] = None,
        status: str = "restored",
        commit: bool = True,
    ) -> Optional[ManagedAgentEnrollment]:
        db_obj = self.get_for_agent(
            db,
            account_id=account_id,
            agent_id=agent_id,
            enrollment_id=enrollment_id,
        )
        if db_obj is None:
            return None
        if backup_metadata:
            db_obj.backup_metadata = redact_agent_config(
                {
                    **(db_obj.backup_metadata or {}),
                    **backup_metadata,
                }
            )
        if validation_result:
            db_obj.validation_result = redact_agent_config(
                {
                    **(db_obj.validation_result or {}),
                    **validation_result,
                }
            )
        db_obj.status = status
        db_obj.restore_available = False
        db_obj.last_restored_at = _utc_now()
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    @staticmethod
    def _to_summary(enrollment: ManagedAgentEnrollment) -> dict[str, Any]:
        return {
            "id": str(enrollment.id),
            "created_by_user_id": (
                str(enrollment.created_by_user_id)
                if enrollment.created_by_user_id
                else None
            ),
            "enrollment_type": enrollment.enrollment_type,
            "adapter_key": enrollment.adapter_key,
            "status": enrollment.status,
            "target_config_path": enrollment.target_config_path,
            # Redacted again on read: rows stored before the write-side
            # redaction existed may still hold secrets.
            "discovered_config": redact_agent_config(
                enrollment.discovered_config or {}
            ),
            "managed_config": redact_agent_config(enrollment.managed_config or {}),
            "backup_metadata": redact_agent_config(enrollment.backup_metadata or {}),
            "validation_result": redact_agent_config(
                enrollment.validation_result or {}
            ),
            "restore_available": enrollment.restore_available,
            "last_applied_at": enrollment.last_applied_at,
            "last_validated_at": enrollment.last_validated_at,
            "last_restored_at": enrollment.last_restored_at,
            "created_at": enrollment.created_at,
            "updated_at": enrollment.updated_at,
            "expires_at": pending_enrollment_expires_at(
                enrollment.status, enrollment.created_at
            ),
        }


crud_managed_agent_enrollment = CRUDManagedAgentEnrollment(ManagedAgentEnrollment)
