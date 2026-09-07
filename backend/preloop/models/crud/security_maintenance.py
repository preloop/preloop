"""Transactional security-maintenance persistence and identity serialization."""

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models import models


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "STOPPED",
        "TIMED_OUT",
        "ABORTED",
    }
)
DEFAULT_DISPATCH_CLAIM_STALE_SECONDS = 120


def _naive_utc(value: datetime) -> datetime:
    """Normalize timestamps stored on dispatch claims to naive UTC."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def parse_dispatch_claimed_at(value: object) -> datetime | None:
    """Parse a stored claim timestamp. Unknown values are treated as missing."""
    if isinstance(value, datetime):
        return _naive_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _naive_utc(parsed)


def format_dispatch_claimed_at(now: datetime) -> str:
    """Serialize a claim timestamp for JSON storage."""
    return _naive_utc(now).isoformat()


def dispatch_job_is_claimable(
    record: Mapping[str, Any] | None,
    *,
    now: datetime,
    stale_after_seconds: int = DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
) -> bool:
    """Return True when a pending or abandoned dispatching record may be claimed.

    ``dispatching`` without ``dispatch_claimed_at`` is a legacy abandoned claim
    and is eligible immediately. A fresh timestamped claim is not.
    """
    data = dict(record or {})
    state = data.get("dispatch_state")
    if state in {None, "pending"}:
        return True
    if state != "dispatching":
        return False
    claimed_at = parse_dispatch_claimed_at(data.get("dispatch_claimed_at"))
    if claimed_at is None:
        return True
    timeout = max(1, int(stale_after_seconds))
    return claimed_at < _naive_utc(now) - timedelta(seconds=timeout)


def _write_dispatch_claim(
    data: dict[str, Any], *, now: datetime, claim_id: UUID
) -> dict[str, Any]:
    """Stamp CAS ownership onto a dispatch record."""
    updated = dict(data)
    updated["dispatch_state"] = "dispatching"
    updated["dispatch_claimed_at"] = format_dispatch_claimed_at(now)
    updated["dispatch_claim_id"] = str(claim_id)
    return updated


def _clear_dispatch_claim(
    data: dict[str, Any], *, dispatch_state: str
) -> dict[str, Any]:
    """Drop claim fields after a CAS finish or release."""
    updated = dict(data)
    updated["dispatch_state"] = dispatch_state
    updated.pop("dispatch_claimed_at", None)
    updated.pop("dispatch_claim_id", None)
    return updated


def _owns_dispatch_claim(data: Mapping[str, Any], claim_id: UUID) -> bool:
    """Return True when this claimant still owns the in-flight lease."""
    return data.get("dispatch_state") == "dispatching" and str(
        data.get("dispatch_claim_id") or ""
    ) == str(claim_id)


def item_identity_key(
    account_id: UUID,
    product_key: str,
    release_key: str,
    advisory_id: str,
    component_id: str,
) -> str:
    """Stable per-tenant identity for one advisory/component on a release."""
    material = (
        f"{account_id}:{product_key.strip().lower()}:"
        f"{release_key.strip().lower()}:"
        f"{advisory_id.strip().lower()}:{component_id.strip().lower()}"
    )
    return sha256(material.encode()).hexdigest()


class CRUDSecurityMaintenance:
    """Account-scoped inventory, items, append-only decisions, and baselines."""

    def get_project(
        self, db: Session, *, account_id: UUID, project_id: UUID
    ) -> models.Project | None:
        """Resolve project ownership through organization and tracker."""
        return db.scalar(
            select(models.Project)
            .join(
                models.Organization,
                models.Project.organization_id == models.Organization.id,
            )
            .join(models.Tracker, models.Organization.tracker_id == models.Tracker.id)
            .where(
                models.Project.id == project_id, models.Tracker.account_id == account_id
            )
        )

    def get_user(
        self, db: Session, *, account_id: UUID, user_id: UUID
    ) -> models.User | None:
        """Resolve a user that belongs to this tenant."""
        return db.scalar(
            select(models.User).where(
                models.User.id == user_id, models.User.account_id == account_id
            )
        )

    def get_flow(
        self, db: Session, *, account_id: UUID, flow_id: UUID
    ) -> models.Flow | None:
        """Load an account-owned flow."""
        return db.scalar(
            select(models.Flow).where(
                models.Flow.id == flow_id, models.Flow.account_id == account_id
            )
        )

    def get_approval_workflow(
        self, db: Session, *, account_id: UUID, workflow_id: UUID
    ) -> models.ApprovalWorkflow | None:
        """Load an account-owned approval workflow."""
        return db.scalar(
            select(models.ApprovalWorkflow).where(
                models.ApprovalWorkflow.id == workflow_id,
                models.ApprovalWorkflow.account_id == account_id,
            )
        )

    def get_approval_request(
        self, db: Session, *, account_id: UUID, request_id: UUID
    ) -> models.ApprovalRequest | None:
        """Load a tenant-scoped platform approval request."""
        return db.scalar(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.id == request_id,
                models.ApprovalRequest.account_id == account_id,
            )
        )

    def get_execution(
        self, db: Session, *, account_id: UUID, execution_id: UUID
    ) -> models.FlowExecution | None:
        """Load an execution only when its flow belongs to the tenant."""
        return db.scalar(
            select(models.FlowExecution)
            .join(models.Flow, models.FlowExecution.flow_id == models.Flow.id)
            .where(
                models.FlowExecution.id == execution_id,
                models.Flow.account_id == account_id,
            )
        )

    def get_issue(
        self, db: Session, *, account_id: UUID, issue_id: UUID
    ) -> models.Issue | None:
        """Resolve issue ownership through the tracker."""
        return db.scalar(
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(models.Issue.id == issue_id, models.Tracker.account_id == account_id)
        )

    def get_issue_for_project(
        self,
        db: Session,
        *,
        account_id: UUID,
        project_id: UUID,
        issue_id: UUID,
    ) -> models.Issue | None:
        """Resolve a tracker issue that belongs to this tenant project."""
        return db.scalar(
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(
                models.Issue.id == issue_id,
                models.Issue.project_id == project_id,
                models.Tracker.account_id == account_id,
            )
        )

    def get_organization(
        self, db: Session, *, account_id: UUID, organization_id: UUID
    ) -> models.Organization | None:
        """Resolve an organization through the tenant tracker."""
        return db.scalar(
            select(models.Organization)
            .join(models.Tracker, models.Organization.tracker_id == models.Tracker.id)
            .where(
                models.Organization.id == organization_id,
                models.Tracker.account_id == account_id,
            )
        )

    def get_or_create_tool_configuration(
        self, db: Session, *, account_id: UUID, tool_name: str
    ) -> models.ToolConfiguration:
        """Return the account tool row, creating it with a flush-only write."""
        row = db.scalar(
            select(models.ToolConfiguration).where(
                models.ToolConfiguration.account_id == account_id,
                models.ToolConfiguration.tool_name == tool_name,
                models.ToolConfiguration.managed_agent_id.is_(None),
            )
        )
        if row is not None:
            return row
        row = models.ToolConfiguration(
            account_id=account_id,
            tool_name=tool_name,
            tool_source="builtin",
        )
        db.add(row)
        db.flush()
        return row

    def validate_release_workflow(
        self,
        db: Session,
        *,
        account_id: UUID,
        workflow_id: UUID,
        owner_user_id: UUID | None,
        escalation_user_ids: list[UUID],
        timeout_seconds: int,
    ) -> models.ApprovalWorkflow:
        """Require a configured workflow. Never mutate shared workflow rows."""
        workflow = self.get_approval_workflow(
            db, account_id=account_id, workflow_id=workflow_id
        )
        if workflow is None:
            raise ValueError("approval_workflow_unavailable")
        approvers = list(workflow.approver_user_ids or [])
        if owner_user_id is not None and owner_user_id not in approvers:
            raise ValueError("approval_owner_not_in_workflow")
        _ = (escalation_user_ids, timeout_seconds)
        return workflow

    def apply_workflow_policy(
        self,
        db: Session,
        *,
        account_id: UUID,
        workflow_id: UUID,
        owner_user_id: UUID | None,
        escalation_user_ids: list[UUID],
        timeout_seconds: int,
    ) -> models.ApprovalWorkflow:
        """Back-compat alias. Shared workflow rows are not written."""
        return self.validate_release_workflow(
            db,
            account_id=account_id,
            workflow_id=workflow_id,
            owner_user_id=owner_user_id,
            escalation_user_ids=escalation_user_ids,
            timeout_seconds=timeout_seconds,
        )

    def update_release(
        self,
        db: Session,
        *,
        account_id: UUID,
        release_id: UUID,
        fields: dict[str, Any],
    ) -> models.SecurityMaintenanceRelease | None:
        """Update mutable inventory fields. Caller holds the release lock."""
        row = self.get_release(db, account_id=account_id, release_id=release_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row

    def update_item(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: UUID,
        fields: dict[str, Any],
    ) -> models.SecurityMaintenanceItem | None:
        """Update work-item fields. Caller holds the identity or item lock."""
        row = self.get_item(db, account_id=account_id, item_id=item_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row

    def get_item_by_approval_request(
        self, db: Session, *, account_id: UUID, request_id: UUID
    ) -> models.SecurityMaintenanceItem | None:
        """Find the work item bound to a platform approval request."""
        return db.scalar(
            select(models.SecurityMaintenanceItem).where(
                models.SecurityMaintenanceItem.account_id == account_id,
                models.SecurityMaintenanceItem.approval_request_id == request_id,
            )
        )

    def get_pending_maintenance_approval(
        self, db: Session, *, account_id: UUID, item_id: UUID
    ) -> models.ApprovalRequest | None:
        """Find an unbound pending platform request for this work item."""
        rows = db.scalars(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.account_id == account_id,
                models.ApprovalRequest.tool_name == "security_maintenance",
                models.ApprovalRequest.status == "pending",
            )
        )
        item_key = str(item_id)
        for row in rows:
            args = row.tool_args if isinstance(row.tool_args, dict) else {}
            if str(args.get("item_id") or "") == item_key:
                return row
        return None

    def list_reconcile_account_ids(self, db: Session) -> list[UUID]:
        """Accounts with items or baseline audits that may need dispatch retry."""
        account_ids = set(
            db.scalars(
                select(models.SecurityMaintenanceItem.account_id)
                .where(
                    models.SecurityMaintenanceItem.state.in_(
                        (
                            "tests_passed",
                            "approval_pending",
                            "remediation_pending",
                            "reaudit_pending",
                        )
                    )
                )
                .distinct()
            )
        )
        for row in db.scalars(select(models.SecurityMaintenanceRelease)):
            audit = (row.data or {}).get("baseline_audit")
            if not isinstance(audit, dict):
                continue
            if audit.get("dispatch_state") in {"pending", "dispatching"}:
                account_ids.add(row.account_id)
        return list(account_ids)

    def list_pending_baseline_releases(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: datetime | None = None,
        stale_after_seconds: int = DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
        limit: int = 50,
    ) -> list[models.SecurityMaintenanceRelease]:
        """Releases whose initial-baseline audit is committed but not dispatched.

        Includes abandoned ``dispatching`` claims (expired or legacy rows
        without a timestamp) whose bound execution is still ``PENDING``.
        """
        moment = now or datetime.now(timezone.utc).replace(tzinfo=None)
        pending: list[models.SecurityMaintenanceRelease] = []
        for row in self.list_releases(db, account_id=account_id):
            audit = (row.data or {}).get("baseline_audit")
            if not isinstance(audit, dict) or not audit.get("execution_id"):
                continue
            if not dispatch_job_is_claimable(
                audit, now=moment, stale_after_seconds=stale_after_seconds
            ):
                continue
            execution = self.get_execution(
                db,
                account_id=account_id,
                execution_id=UUID(str(audit["execution_id"])),
            )
            if execution is None or execution.status != "PENDING":
                continue
            pending.append(row)
            if len(pending) >= limit:
                break
        return pending

    def try_sweep_lock(self, db: Session, account_id: UUID) -> int | None:
        """Session-level sweep mutex. Survives commit; caller must unlock."""
        key = int.from_bytes(
            sha256(f"sm:sweep:{account_id}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        locked = db.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        return key if locked else None

    def release_sweep_lock(self, db: Session, key: int) -> None:
        """Release the session-level sweep mutex."""
        db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

    def list_reconcile_items(
        self, db: Session, *, account_id: UUID, limit: int = 50
    ) -> list[models.SecurityMaintenanceItem]:
        """Items that may need expiry, enqueue retry, or approval follow-up."""
        return list(
            db.scalars(
                select(models.SecurityMaintenanceItem)
                .where(
                    models.SecurityMaintenanceItem.account_id == account_id,
                    models.SecurityMaintenanceItem.state.in_(
                        (
                            "tests_passed",
                            "approval_pending",
                            "remediation_pending",
                            "reaudit_pending",
                        )
                    ),
                )
                .order_by(models.SecurityMaintenanceItem.created_at)
                .limit(limit)
            )
        )

    def set_accepted_baseline(
        self,
        db: Session,
        *,
        account_id: UUID,
        release_id: UUID,
        baseline_id: UUID,
        expected_current: UUID | None = None,
    ) -> models.SecurityMaintenanceRelease:
        """Write accepted_baseline_id at release scope. Caller holds the lock."""
        release = self.get_release(db, account_id=account_id, release_id=release_id)
        if release is None:
            raise ValueError("release_not_found")
        if (
            expected_current is not None
            and release.accepted_baseline_id != expected_current
        ):
            raise ValueError("accepted_baseline_conflict")
        release.accepted_baseline_id = baseline_id
        db.flush()
        return release

    @asynccontextmanager
    async def locked(
        self, db: Session, account_id: UUID, token: str
    ) -> AsyncIterator[None]:
        """Serialize one identity or release without blocking the event loop."""
        key = int.from_bytes(
            sha256(f"sm:{account_id}:{token}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        deadline = monotonic() + 10
        while not db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        ):
            if monotonic() >= deadline:
                db.rollback()
                raise ValueError("security_maintenance_operation_in_progress")
            await asyncio.sleep(0.05)
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def get_release(
        self, db: Session, *, account_id: UUID, release_id: UUID
    ) -> models.SecurityMaintenanceRelease | None:
        """Load one opted-in release for this tenant."""
        return db.scalar(
            select(models.SecurityMaintenanceRelease).where(
                models.SecurityMaintenanceRelease.id == release_id,
                models.SecurityMaintenanceRelease.account_id == account_id,
            )
        )

    def get_release_by_identity(
        self,
        db: Session,
        *,
        account_id: UUID,
        product_key: str,
        release_key: str,
    ) -> models.SecurityMaintenanceRelease | None:
        """Lookup the explicit inventory row. Absence means unsupported."""
        return db.scalar(
            select(models.SecurityMaintenanceRelease).where(
                models.SecurityMaintenanceRelease.account_id == account_id,
                models.SecurityMaintenanceRelease.product_key == product_key,
                models.SecurityMaintenanceRelease.release_key == release_key,
            )
        )

    def list_releases(
        self, db: Session, *, account_id: UUID
    ) -> list[models.SecurityMaintenanceRelease]:
        """List this tenant's opted-in releases."""
        return list(
            db.scalars(
                select(models.SecurityMaintenanceRelease)
                .where(models.SecurityMaintenanceRelease.account_id == account_id)
                .order_by(models.SecurityMaintenanceRelease.created_at)
            )
        )

    def create_release(
        self,
        db: Session,
        *,
        account_id: UUID,
        fields: dict[str, Any],
    ) -> models.SecurityMaintenanceRelease:
        """Insert an inventory row. Caller holds the identity lock."""
        row = models.SecurityMaintenanceRelease(account_id=account_id, **fields)
        db.add(row)
        db.flush()
        return row

    def get_item(
        self, db: Session, *, account_id: UUID, item_id: UUID
    ) -> models.SecurityMaintenanceItem | None:
        """Load one work item for this tenant."""
        return db.scalar(
            select(models.SecurityMaintenanceItem).where(
                models.SecurityMaintenanceItem.id == item_id,
                models.SecurityMaintenanceItem.account_id == account_id,
            )
        )

    def get_item_by_identity(
        self, db: Session, *, account_id: UUID, identity_key: str
    ) -> models.SecurityMaintenanceItem | None:
        """Dedup lookup used by concurrent scan ingest."""
        return db.scalar(
            select(models.SecurityMaintenanceItem).where(
                models.SecurityMaintenanceItem.account_id == account_id,
                models.SecurityMaintenanceItem.identity_key == identity_key,
            )
        )

    def list_items(
        self,
        db: Session,
        *,
        account_id: UUID,
        release_id: UUID | None = None,
        state: str | None = None,
    ) -> list[models.SecurityMaintenanceItem]:
        """List tenant items, optionally filtered."""
        stmt = select(models.SecurityMaintenanceItem).where(
            models.SecurityMaintenanceItem.account_id == account_id
        )
        if release_id is not None:
            stmt = stmt.where(models.SecurityMaintenanceItem.release_id == release_id)
        if state is not None:
            stmt = stmt.where(models.SecurityMaintenanceItem.state == state)
        return list(
            db.scalars(stmt.order_by(models.SecurityMaintenanceItem.created_at))
        )

    def create_item(
        self,
        db: Session,
        *,
        account_id: UUID,
        fields: dict[str, Any],
    ) -> models.SecurityMaintenanceItem:
        """Insert a work item. Caller holds the identity lock."""
        row = models.SecurityMaintenanceItem(account_id=account_id, **fields)
        db.add(row)
        db.flush()
        return row

    def list_decisions(
        self, db: Session, *, account_id: UUID, item_id: UUID
    ) -> list[models.SecurityMaintenanceDecision]:
        """Return append-only history for reconciliation."""
        return list(
            db.scalars(
                select(models.SecurityMaintenanceDecision)
                .where(
                    models.SecurityMaintenanceDecision.account_id == account_id,
                    models.SecurityMaintenanceDecision.item_id == item_id,
                )
                .order_by(models.SecurityMaintenanceDecision.created_at)
            )
        )

    def append_decision(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: UUID,
        kind: str,
        outcome: str,
        actor_user_id: UUID | None = None,
        execution_id: UUID | None = None,
        approval_request_id: UUID | None = None,
        evidence_ref: dict[str, Any] | None = None,
        publication_ref: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> models.SecurityMaintenanceDecision:
        """Insert a historical decision. Never update prior rows."""
        row = models.SecurityMaintenanceDecision(
            account_id=account_id,
            item_id=item_id,
            kind=kind,
            outcome=outcome,
            actor_user_id=actor_user_id,
            execution_id=execution_id,
            approval_request_id=approval_request_id,
            evidence_ref=evidence_ref or {},
            publication_ref=publication_ref or {},
            data=data or {},
        )
        db.add(row)
        db.flush()
        return row

    def get_baseline(
        self, db: Session, *, account_id: UUID, baseline_id: UUID
    ) -> models.SecurityMaintenanceBaseline | None:
        """Load one accepted baseline for this tenant."""
        return db.scalar(
            select(models.SecurityMaintenanceBaseline).where(
                models.SecurityMaintenanceBaseline.id == baseline_id,
                models.SecurityMaintenanceBaseline.account_id == account_id,
            )
        )

    def create_baseline(
        self,
        db: Session,
        *,
        account_id: UUID,
        fields: dict[str, Any],
    ) -> models.SecurityMaintenanceBaseline:
        """Insert an accepted baseline. Caller already validated the audit."""
        row = models.SecurityMaintenanceBaseline(account_id=account_id, **fields)
        db.add(row)
        db.flush()
        return row

    def create_execution(
        self,
        db: Session,
        *,
        flow_id: UUID,
        event: dict[str, Any],
    ) -> models.FlowExecution:
        """Create a PENDING execution the existing dispatcher can claim."""
        execution = models.FlowExecution(
            flow_id=flow_id, status="PENDING", trigger_event_details=event
        )
        db.add(execution)
        db.flush()
        return execution

    def create_issue(
        self,
        db: Session,
        *,
        fields: dict[str, Any],
    ) -> models.Issue:
        """Create the local remediation work item bound to the tenant tracker."""
        issue = models.Issue(**fields)
        db.add(issue)
        db.flush()
        return issue

    def _lock_item(
        self, db: Session, *, account_id: UUID, item_id: UUID
    ) -> models.SecurityMaintenanceItem | None:
        """Load one item with a row lock for CAS dispatch ownership."""
        return db.scalar(
            select(models.SecurityMaintenanceItem)
            .where(
                models.SecurityMaintenanceItem.id == item_id,
                models.SecurityMaintenanceItem.account_id == account_id,
            )
            .with_for_update()
        )

    def _lock_release(
        self, db: Session, *, account_id: UUID, release_id: UUID
    ) -> models.SecurityMaintenanceRelease | None:
        """Load one release with a row lock for CAS dispatch ownership."""
        return db.scalar(
            select(models.SecurityMaintenanceRelease)
            .where(
                models.SecurityMaintenanceRelease.id == release_id,
                models.SecurityMaintenanceRelease.account_id == account_id,
            )
            .with_for_update()
        )

    def _pending_bound_execution(
        self, db: Session, *, account_id: UUID, execution_id: UUID
    ) -> models.FlowExecution | None:
        """Return the tenant execution only while it is still PENDING."""
        execution = self.get_execution(
            db, account_id=account_id, execution_id=execution_id
        )
        if execution is None or execution.status != "PENDING":
            return None
        return execution

    def claim_item_dispatch(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: UUID,
        execution_id: UUID,
        kind: str,
        now: datetime,
        stale_after_seconds: int = DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
        claim_id: UUID | None = None,
    ) -> UUID | None:
        """Take an expiring dispatch claim for an item implementation or recheck.

        Caller holds the item identity lock. Only a still-PENDING bound
        execution with a pending or abandoned claim can be owned. Returns the
        claim id, or None when another claimant holds a fresh lease or the
        execution has already started or finished.
        """
        item = self._lock_item(db, account_id=account_id, item_id=item_id)
        if item is None:
            return None
        bound = (
            item.implementation_execution_id
            if kind == "implementation"
            else item.recheck_execution_id
        )
        if bound != execution_id:
            return None
        if (
            self._pending_bound_execution(
                db, account_id=account_id, execution_id=execution_id
            )
            is None
        ):
            return None
        data = dict(item.data or {})
        if not dispatch_job_is_claimable(
            data, now=now, stale_after_seconds=stale_after_seconds
        ):
            return None
        token = claim_id or uuid4()
        item.data = _write_dispatch_claim(data, now=now, claim_id=token)
        flag_modified(item, "data")
        db.flush()
        return token

    def finish_item_dispatch(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: UUID,
        execution_id: UUID,
        kind: str,
        claim_id: UUID,
        dispatched: bool,
    ) -> models.SecurityMaintenanceItem | None:
        """Complete or release an item dispatch claim. Stale claimants no-op.

        A successful finish may advance ``remediation_pending`` /
        ``reaudit_pending`` to the in-flight state. A failed finish returns
        the record to ``pending`` only while this claim id still owns it.
        """
        item = self._lock_item(db, account_id=account_id, item_id=item_id)
        if item is None:
            return None
        bound = (
            item.implementation_execution_id
            if kind == "implementation"
            else item.recheck_execution_id
        )
        if bound != execution_id:
            return None
        data = dict(item.data or {})
        if not _owns_dispatch_claim(data, claim_id):
            return None
        next_state = "dispatched" if dispatched else "pending"
        item.data = _clear_dispatch_claim(data, dispatch_state=next_state)
        flag_modified(item, "data")
        if dispatched:
            pending_state = (
                "remediation_pending" if kind == "implementation" else "reaudit_pending"
            )
            if item.state == pending_state:
                item.state = "remediating" if kind == "implementation" else "reauditing"
        db.flush()
        return item

    def claim_baseline_dispatch(
        self,
        db: Session,
        *,
        account_id: UUID,
        release_id: UUID,
        execution_id: UUID,
        now: datetime,
        stale_after_seconds: int = DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
        claim_id: UUID | None = None,
    ) -> UUID | None:
        """Take an expiring dispatch claim for an initial-baseline audit.

        Caller holds the release lock. Same PENDING and abandoned-claim rules
        as ``claim_item_dispatch``.
        """
        release = self._lock_release(db, account_id=account_id, release_id=release_id)
        if release is None:
            return None
        data = dict(release.data or {})
        audit = dict(data.get("baseline_audit") or {})
        if str(audit.get("execution_id") or "") != str(execution_id):
            return None
        if (
            self._pending_bound_execution(
                db, account_id=account_id, execution_id=execution_id
            )
            is None
        ):
            return None
        if not dispatch_job_is_claimable(
            audit, now=now, stale_after_seconds=stale_after_seconds
        ):
            return None
        token = claim_id or uuid4()
        data["baseline_audit"] = _write_dispatch_claim(audit, now=now, claim_id=token)
        release.data = data
        flag_modified(release, "data")
        db.flush()
        return token

    def finish_baseline_dispatch(
        self,
        db: Session,
        *,
        account_id: UUID,
        release_id: UUID,
        execution_id: UUID,
        claim_id: UUID,
        dispatched: bool,
    ) -> models.SecurityMaintenanceRelease | None:
        """Complete or release a baseline dispatch claim. Stale claimants no-op."""
        release = self._lock_release(db, account_id=account_id, release_id=release_id)
        if release is None:
            return None
        data = dict(release.data or {})
        audit = dict(data.get("baseline_audit") or {})
        if str(audit.get("execution_id") or "") != str(execution_id):
            return None
        if not _owns_dispatch_claim(audit, claim_id):
            return None
        next_state = "dispatched" if dispatched else "pending"
        data["baseline_audit"] = _clear_dispatch_claim(audit, dispatch_state=next_state)
        release.data = data
        flag_modified(release, "data")
        db.flush()
        return release


crud_security_maintenance = CRUDSecurityMaintenance()
