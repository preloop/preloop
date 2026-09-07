"""Durable supported-release vulnerability maintenance on existing flows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_security_maintenance
from preloop.models.crud.security_maintenance import item_identity_key
from preloop.schemas.security_maintenance import (
    ApprovalDecisionRequest,
    BaselineAcceptRequest,
    ResumeRequest,
    ScanFinding,
    ScanIngestRequest,
    SupportedReleaseCreate,
    SupportedReleaseUpdate,
)
from preloop.services.flow_trigger_service import FlowDispatchError
from preloop.services.security_maintenance_refs import (
    CrossAccountError,
    InvalidTransitionError,
    MissingEvidenceError,
    SourceOutageError,
    UnsupportedReleaseError,
    audit_acceptance,
    evidence_ref_from_execution,
    human_platform_approval,
    publication_ref_from_execution,
    tests_passed,
)

AUTO_DISPATCH_STATES = frozenset({"open"})
IN_FLIGHT_STATES = frozenset(
    {
        "remediation_pending",
        "remediating",
        "tests_passed",
        "approval_pending",
        "reaudit_pending",
        "reauditing",
    }
)
RESUME_STATES = frozenset(
    {
        "open",
        "remediation_pending",
        "tests_failed",
        "held",
        "escalated",
        "reaudit_incomplete",
    }
)
ENVELOPE_KEY = "security_maintenance"
DEFAULT_APPROVAL_TIMEOUT = 86400
TERMINAL = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED", "TIMED_OUT", "ABORTED"}
)


class SecurityMaintenanceService:
    """Tenant controller for inventory, scan ingest, dispatch, and baselines."""

    def __init__(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.account_id = account_id
        self._now = now or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def _serialize_release(
        self, row: models.SecurityMaintenanceRelease
    ) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "product_key": row.product_key,
            "release_key": row.release_key,
            "display_name": row.display_name,
            "project_id": str(row.project_id),
            "pinned_build_ref": row.pinned_build_ref,
            "sbom_input_ref": row.sbom_input_ref,
            "audit_flow_id": str(row.audit_flow_id),
            "implementation_flow_id": str(row.implementation_flow_id),
            "recheck_flow_id": str(row.recheck_flow_id)
            if row.recheck_flow_id
            else None,
            "accepted_baseline_id": (
                str(row.accepted_baseline_id) if row.accepted_baseline_id else None
            ),
            "approval_workflow_id": str(row.approval_workflow_id),
            "approval_owner_user_id": (
                str(row.approval_owner_user_id) if row.approval_owner_user_id else None
            ),
            "escalation_user_ids": [
                str(item) for item in (row.escalation_user_ids or [])
            ],
            "escalation_after_seconds": row.escalation_after_seconds,
            "max_retries": row.max_retries,
            "allowed_model_ids": (
                [str(item) for item in (row.allowed_model_ids or [])]
                if row.allowed_model_ids is not None
                else None
            ),
            "allowed_input_kinds": (
                list(row.allowed_input_kinds or [])
                if row.allowed_input_kinds is not None
                else None
            ),
            "enabled": row.enabled,
        }

    def _serialize_item(self, row: models.SecurityMaintenanceItem) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "release_id": str(row.release_id),
            "identity_key": row.identity_key,
            "product_key": row.product_key,
            "release_key": row.release_key,
            "advisory_id": row.advisory_id,
            "component_id": row.component_id,
            "state": row.state,
            "issue_id": str(row.issue_id) if row.issue_id else None,
            "implementation_execution_id": (
                str(row.implementation_execution_id)
                if row.implementation_execution_id
                else None
            ),
            "recheck_execution_id": (
                str(row.recheck_execution_id) if row.recheck_execution_id else None
            ),
            "approval_request_id": (
                str(row.approval_request_id) if row.approval_request_id else None
            ),
            "retry_count": row.retry_count,
            "scan_fingerprint": row.scan_fingerprint,
            "data": dict(row.data or {}),
        }

    def _serialize_decision(
        self, row: models.SecurityMaintenanceDecision
    ) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "item_id": str(row.item_id),
            "kind": row.kind,
            "outcome": row.outcome,
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "execution_id": str(row.execution_id) if row.execution_id else None,
            "approval_request_id": (
                str(row.approval_request_id) if row.approval_request_id else None
            ),
            "evidence_ref": dict(row.evidence_ref or {}),
            "publication_ref": dict(row.publication_ref or {}),
            "data": dict(row.data or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _serialize_baseline(
        self, row: models.SecurityMaintenanceBaseline
    ) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "release_id": str(row.release_id),
            "item_id": str(row.item_id) if row.item_id else None,
            "audit_execution_id": str(row.audit_execution_id),
            "result_digest": row.result_digest,
            "verdict": row.verdict,
            "evidence_ref": dict(row.evidence_ref or {}),
            "data": dict(row.data or {}),
        }

    def _require_project(self, project_id: UUID) -> models.Project:
        project = crud_security_maintenance.get_project(
            self.db, account_id=self.account_id, project_id=project_id
        )
        if project is None:
            raise CrossAccountError("project_not_in_account")
        return project

    def _require_flow(self, flow_id: UUID, *, kind: str) -> models.Flow:
        flow = crud_security_maintenance.get_flow(
            self.db, account_id=self.account_id, flow_id=flow_id
        )
        if flow is None or not flow.is_enabled:
            raise InvalidTransitionError(f"{kind}_flow_unavailable")
        return flow

    def _repo_ids(self, flow: models.Flow) -> set[str]:
        repos = (flow.git_clone_config or {}).get("repositories") or []
        return {str(repo.get("project_id")) for repo in repos if repo.get("project_id")}

    def _assert_model_allowed(
        self, flow: models.Flow, allowed: list[UUID] | None, label: str
    ) -> None:
        if not allowed:
            return
        if flow.ai_model_id is None or flow.ai_model_id not in allowed:
            raise InvalidTransitionError(f"{label}_model_not_allowed")

    def _assert_audit_flow(self, flow: models.Flow, project: models.Project) -> None:
        git = flow.git_clone_config or {}
        if git.get("create_pull_request"):
            raise InvalidTransitionError("audit_flow_cannot_publish")
        ids = self._repo_ids(flow)
        if ids and str(project.id) not in ids:
            raise InvalidTransitionError("audit_flow_project_mismatch")

    def _assert_implementation_flow(
        self, flow: models.Flow, project: models.Project
    ) -> None:
        ids = self._repo_ids(flow)
        if ids and str(project.id) not in ids:
            raise InvalidTransitionError("implementation_flow_project_mismatch")

    def _validate_flows(
        self,
        *,
        project: models.Project,
        audit_flow_id: UUID,
        implementation_flow_id: UUID,
        recheck_flow_id: UUID | None,
        allowed_model_ids: list[UUID] | None,
    ) -> None:
        audit = self._require_flow(audit_flow_id, kind="audit")
        implementer = self._require_flow(implementation_flow_id, kind="implementation")
        recheck = (
            self._require_flow(recheck_flow_id, kind="recheck")
            if recheck_flow_id
            else audit
        )
        self._assert_audit_flow(audit, project)
        self._assert_audit_flow(recheck, project)
        self._assert_implementation_flow(implementer, project)
        for flow, label in (
            (audit, "audit"),
            (implementer, "implementation"),
            (recheck, "recheck"),
        ):
            self._assert_model_allowed(flow, allowed_model_ids, label)

    def _require_workflow(self, workflow_id: UUID) -> models.ApprovalWorkflow:
        workflow = crud_security_maintenance.get_approval_workflow(
            self.db, account_id=self.account_id, workflow_id=workflow_id
        )
        if workflow is None:
            raise InvalidTransitionError("approval_workflow_unavailable")
        return workflow

    def _require_user(self, user_id: UUID, error: str) -> models.User:
        user = crud_security_maintenance.get_user(
            self.db, account_id=self.account_id, user_id=user_id
        )
        if user is None:
            raise CrossAccountError(error)
        return user

    async def create_release(self, request: SupportedReleaseCreate) -> dict[str, Any]:
        """Persist an explicit opt-in inventory row."""
        token = f"release:{request.product_key}:{request.release_key}"
        async with crud_security_maintenance.locked(self.db, self.account_id, token):
            existing = crud_security_maintenance.get_release_by_identity(
                self.db,
                account_id=self.account_id,
                product_key=request.product_key,
                release_key=request.release_key,
            )
            if existing:
                raise InvalidTransitionError("release_already_configured")
            project = self._require_project(request.project_id)
            self._require_workflow(request.approval_workflow_id)
            if request.approval_owner_user_id:
                self._require_user(
                    request.approval_owner_user_id, "approval_owner_not_in_account"
                )
            for user_id in request.escalation_user_ids:
                self._require_user(user_id, "escalation_user_not_in_account")
            self._validate_flows(
                project=project,
                audit_flow_id=request.audit_flow_id,
                implementation_flow_id=request.implementation_flow_id,
                recheck_flow_id=request.recheck_flow_id,
                allowed_model_ids=request.allowed_model_ids,
            )
            row = crud_security_maintenance.create_release(
                self.db,
                account_id=self.account_id,
                fields={
                    "product_key": request.product_key,
                    "release_key": request.release_key,
                    "display_name": request.display_name,
                    "project_id": request.project_id,
                    "pinned_build_ref": request.pinned_build_ref,
                    "sbom_input_ref": request.sbom_input_ref,
                    "audit_flow_id": request.audit_flow_id,
                    "implementation_flow_id": request.implementation_flow_id,
                    "recheck_flow_id": request.recheck_flow_id,
                    "approval_workflow_id": request.approval_workflow_id,
                    "approval_owner_user_id": request.approval_owner_user_id,
                    "escalation_user_ids": request.escalation_user_ids or None,
                    "escalation_after_seconds": request.escalation_after_seconds,
                    "max_retries": request.max_retries,
                    "allowed_model_ids": request.allowed_model_ids,
                    "allowed_input_kinds": request.allowed_input_kinds,
                    "enabled": request.enabled,
                    "data": {},
                },
            )
            return self._serialize_release(row)

    def list_releases(self) -> list[dict[str, Any]]:
        """Return this tenant's opted-in releases."""
        return [
            self._serialize_release(row)
            for row in crud_security_maintenance.list_releases(
                self.db, account_id=self.account_id
            )
        ]

    def get_release(self, release_id: UUID) -> dict[str, Any]:
        """Return one inventory row or raise."""
        row = crud_security_maintenance.get_release(
            self.db, account_id=self.account_id, release_id=release_id
        )
        if row is None:
            raise CrossAccountError("release_not_found")
        return self._serialize_release(row)

    def _require_release(self, release_id: UUID) -> models.SecurityMaintenanceRelease:
        row = crud_security_maintenance.get_release(
            self.db, account_id=self.account_id, release_id=release_id
        )
        if row is None:
            raise CrossAccountError("release_not_found")
        return row

    def _require_item(self, item_id: UUID) -> models.SecurityMaintenanceItem:
        item = crud_security_maintenance.get_item(
            self.db, account_id=self.account_id, item_id=item_id
        )
        if item is None:
            raise CrossAccountError("item_not_found")
        return item

    async def update_release(
        self, release_id: UUID, request: SupportedReleaseUpdate
    ) -> dict[str, Any]:
        """Update mutable inventory fields. Identity stays fixed."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"release-id:{release_id}"
        ):
            row = self._require_release(release_id)
            updates = request.model_dump(exclude_unset=True)
            if "approval_workflow_id" in updates:
                self._require_workflow(updates["approval_workflow_id"])
            if updates.get("approval_owner_user_id"):
                self._require_user(
                    updates["approval_owner_user_id"], "approval_owner_not_in_account"
                )
            for user_id in updates.get("escalation_user_ids") or []:
                self._require_user(user_id, "escalation_user_not_in_account")
            for key, value in updates.items():
                setattr(row, key, value)
            project = self._require_project(row.project_id)
            self._validate_flows(
                project=project,
                audit_flow_id=row.audit_flow_id,
                implementation_flow_id=row.implementation_flow_id,
                recheck_flow_id=row.recheck_flow_id,
                allowed_model_ids=row.allowed_model_ids,
            )
            self.db.flush()
            return self._serialize_release(row)

    def list_items(
        self,
        *,
        release_id: UUID | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tenant work items."""
        return [
            self._serialize_item(row)
            for row in crud_security_maintenance.list_items(
                self.db,
                account_id=self.account_id,
                release_id=release_id,
                state=state,
            )
        ]

    def get_item(self, item_id: UUID) -> dict[str, Any]:
        """Return one work item or raise."""
        return self._serialize_item(self._require_item(item_id))

    def list_decisions(self, item_id: UUID) -> list[dict[str, Any]]:
        """Return append-only history. Prior rows are never rewritten."""
        self._require_item(item_id)
        return [
            self._serialize_decision(row)
            for row in crud_security_maintenance.list_decisions(
                self.db, account_id=self.account_id, item_id=item_id
            )
        ]

    async def accept_baseline(
        self, release_id: UUID, request: BaselineAcceptRequest
    ) -> dict[str, Any]:
        """Accept an initial baseline from a bound audit execution."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"release-id:{release_id}"
        ):
            release = self._require_release(release_id)
            execution = self._bound_execution(request.audit_execution_id)
            if execution.flow_id not in {
                release.audit_flow_id,
                release.recheck_flow_id,
            }:
                raise InvalidTransitionError("audit_execution_not_bound")
            flow = self._require_flow(execution.flow_id, kind="audit")
            evidence = evidence_ref_from_execution(
                execution,
                request.evidence_ref,
                db=self.db,
                account_id=self.account_id,
            )
            if not evidence.available:
                raise MissingEvidenceError(evidence.reason)
            result = execution.result if isinstance(execution.result, dict) else {}
            audit = audit_acceptance(
                result,
                evidence=evidence,
                execution=execution,
                flow=flow,
            )
            if not audit.accepted:
                raise InvalidTransitionError(audit.reason)
            baseline = crud_security_maintenance.create_baseline(
                self.db,
                account_id=self.account_id,
                fields={
                    "release_id": release.id,
                    "audit_execution_id": execution.id,
                    "result_digest": audit.digest,
                    "verdict": audit.verdict,
                    "evidence_ref": evidence.as_dict(),
                    "data": {"reason": audit.reason, "schema_id": audit.schema_id},
                },
            )
            release.accepted_baseline_id = baseline.id
            self.db.flush()
            return self._serialize_baseline(baseline)

    async def ingest_scan(self, request: ScanIngestRequest) -> dict[str, Any]:
        """Upsert findings onto one opted-in release. Unsupported names fail."""
        token = f"scan:{request.product_key}:{request.release_key}"
        async with crud_security_maintenance.locked(self.db, self.account_id, token):
            release = crud_security_maintenance.get_release_by_identity(
                self.db,
                account_id=self.account_id,
                product_key=request.product_key,
                release_key=request.release_key,
            )
            if release is None or not release.enabled:
                raise UnsupportedReleaseError("unsupported_release")
            if release.allowed_input_kinds is not None:
                if request.input_kind not in release.allowed_input_kinds:
                    raise InvalidTransitionError("input_kind_not_allowed")
            if request.execution_id:
                self._bound_execution(request.execution_id)
            if not request.available:
                raise SourceOutageError("scan_source_unavailable")
            items: list[dict[str, Any]] = []
            for finding in request.findings:
                items.append(
                    await self._upsert_finding(
                        release,
                        finding,
                        source=request.source,
                        execution_id=request.execution_id,
                    )
                )
            return {"release": self._serialize_release(release), "items": items}

    async def _upsert_finding(
        self,
        release: models.SecurityMaintenanceRelease,
        finding: ScanFinding,
        *,
        source: str,
        execution_id: UUID | None,
    ) -> dict[str, Any]:
        identity = item_identity_key(
            self.account_id,
            release.product_key,
            release.release_key,
            finding.advisory_id,
            finding.component_id,
        )
        fingerprint = sha256(
            dumps(
                {
                    "advisory_id": finding.advisory_id,
                    "component_id": finding.component_id,
                    "aliases": finding.aliases,
                    "severity": finding.severity,
                    "present": finding.present,
                    "source": source,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        item = crud_security_maintenance.get_item_by_identity(
            self.db, account_id=self.account_id, identity_key=identity
        )
        if item is None:
            if not finding.present:
                raise InvalidTransitionError("finding_absent_without_item")
            item = crud_security_maintenance.create_item(
                self.db,
                account_id=self.account_id,
                fields={
                    "release_id": release.id,
                    "identity_key": identity,
                    "product_key": release.product_key,
                    "release_key": release.release_key,
                    "advisory_id": finding.advisory_id,
                    "component_id": finding.component_id,
                    "state": "open",
                    "scan_fingerprint": fingerprint,
                    "data": {
                        "severity": finding.severity,
                        "aliases": finding.aliases,
                        "source": source,
                    },
                },
            )
            self._append(
                item,
                kind="scan",
                outcome="opened",
                execution_id=execution_id,
                data={"fingerprint": fingerprint, "present": True},
            )
            self._ensure_issue(item, release)
            if item.state in AUTO_DISPATCH_STATES:
                await self._dispatch_remediation(item, release)
            return self._serialize_item(item)
        item.scan_fingerprint = fingerprint
        data = dict(item.data or {})
        data.update(
            {
                "severity": finding.severity,
                "aliases": finding.aliases,
                "source": source,
                "last_present": finding.present,
            }
        )
        item.data = data
        if not finding.present:
            self._append(
                item,
                kind="scan",
                outcome="finding_absent_unverified",
                execution_id=execution_id,
                data={"fingerprint": fingerprint},
            )
            self.db.flush()
            return self._serialize_item(item)
        self._append(
            item,
            kind="scan",
            outcome="updated",
            execution_id=execution_id,
            data={"fingerprint": fingerprint, "state": item.state},
        )
        if (
            item.state in AUTO_DISPATCH_STATES
            and item.implementation_execution_id is None
        ):
            await self._dispatch_remediation(item, release)
        self.db.flush()
        return self._serialize_item(item)

    async def resume(
        self, item_id: UUID, request: ResumeRequest, *, actor_user_id: UUID
    ) -> dict[str, Any]:
        """Human retry. History stays append-only."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state not in RESUME_STATES:
                raise InvalidTransitionError("item_not_resumable")
            release = self._require_release(item.release_id)
            if item.retry_count >= release.max_retries:
                raise InvalidTransitionError("retry_budget_exhausted")
            item.retry_count += 1
            self._append(
                item,
                kind="resume",
                outcome="accepted",
                actor_user_id=actor_user_id,
                data={"reason": request.reason, "from_state": item.state},
            )
            if item.state in {"reaudit_incomplete", "tests_passed", "approval_pending"}:
                await self._dispatch_recheck(item, release)
            else:
                item.implementation_execution_id = None
                await self._dispatch_remediation(item, release)
            return self._serialize_item(item)

    async def decide_approval(
        self,
        item_id: UUID,
        request: ApprovalDecisionRequest,
        *,
        actor_user_id: UUID,
        approved: bool,
    ) -> dict[str, Any]:
        """Apply a human decision already stored, or record one on the platform row."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state not in {"tests_passed", "approval_pending"}:
                raise InvalidTransitionError("item_not_awaiting_approval")
            row = self._require_platform_approval(item)
            outcome = human_platform_approval(row)
            if outcome == "approval_expired" or self._approval_expired(row):
                row.status = "expired"
                item.state = "escalated"
                self._append(
                    item,
                    kind="approval",
                    outcome="expired",
                    actor_user_id=actor_user_id,
                    approval_request_id=row.id,
                )
                return self._serialize_item(item)
            if not approved:
                row.status = "declined"
                row.resolved_at = self._now()
                row.approver_comment = request.reason
                row.decided_by_ai = False
                row.auto_approved_reason = None
                item.state = "held"
                self._append(
                    item,
                    kind="approval",
                    outcome="denied",
                    actor_user_id=actor_user_id,
                    approval_request_id=row.id,
                    data={"reason": request.reason},
                )
                return self._serialize_item(item)
            if outcome != "approved":
                row.status = "approved"
                row.resolved_at = self._now()
                row.approver_comment = request.reason
                row.decided_by_ai = False
                row.auto_approved_reason = None
            outcome = human_platform_approval(row)
            if outcome != "approved":
                raise InvalidTransitionError(outcome)
            item.state = "reaudit_pending"
            self._append(
                item,
                kind="approval",
                outcome="approved",
                actor_user_id=actor_user_id,
                approval_request_id=row.id,
                data={"reason": request.reason},
            )
            release = self._require_release(item.release_id)
            await self._dispatch_recheck(item, release)
            return self._serialize_item(item)

    async def escalate_item(
        self, item_id: UUID, request: ApprovalDecisionRequest, *, actor_user_id: UUID
    ) -> dict[str, Any]:
        """Hold for named escalation owners. Never auto-releases."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            item.state = "escalated"
            self._append(
                item,
                kind="approval",
                outcome="escalated",
                actor_user_id=actor_user_id,
                approval_request_id=item.approval_request_id,
                data={"reason": request.reason},
            )
            return self._serialize_item(item)

    async def finish_execution(self, execution: models.FlowExecution) -> None:
        """Trusted completion hook. Stale or failed runs never advance a baseline."""
        envelope = _envelope(execution)
        if not envelope:
            return
        item_id = UUID(str(envelope["item_id"]))
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            kind = str(envelope.get("kind") or "")
            if kind == "implementation":
                await self._finish_implementation(item, execution)
            elif kind == "recheck":
                await self._finish_recheck(item, execution)

    async def _finish_implementation(
        self,
        item: models.SecurityMaintenanceItem,
        execution: models.FlowExecution,
    ) -> None:
        if item.implementation_execution_id != execution.id:
            self._append(
                item,
                kind="implementation",
                outcome="stale_completion",
                execution_id=execution.id,
            )
            return
        flow = self._require_flow(execution.flow_id, kind="implementation")
        publication = publication_ref_from_execution(execution, flow=flow)
        passed, reason = tests_passed(execution, flow=flow, publication=publication)
        evidence = evidence_ref_from_execution(
            execution, db=self.db, account_id=self.account_id
        )
        if execution.status not in TERMINAL:
            return
        if not passed:
            item.state = "tests_failed"
            self._append(
                item,
                kind="implementation",
                outcome="tests_failed",
                execution_id=execution.id,
                evidence_ref=evidence.as_dict(),
                publication_ref=publication.as_dict(),
                data={"reason": reason},
            )
            return
        item.state = "tests_passed"
        data = dict(item.data or {})
        data["published_sha"] = publication.sha
        item.data = data
        self._append(
            item,
            kind="implementation",
            outcome="tests_passed",
            execution_id=execution.id,
            evidence_ref=evidence.as_dict(),
            publication_ref=publication.as_dict(),
            data={"reason": reason},
        )
        self._open_approval(item)

    async def _finish_recheck(
        self,
        item: models.SecurityMaintenanceItem,
        execution: models.FlowExecution,
    ) -> None:
        if item.recheck_execution_id != execution.id:
            self._append(
                item,
                kind="recheck",
                outcome="stale_completion",
                execution_id=execution.id,
            )
            return
        release = self._require_release(item.release_id)
        flow = self._require_flow(execution.flow_id, kind="recheck")
        evidence = evidence_ref_from_execution(
            execution, db=self.db, account_id=self.account_id
        )
        publication = publication_ref_from_execution(
            self._bound_execution(item.implementation_execution_id)
            if item.implementation_execution_id
            else execution,
            flow=self._require_flow(
                release.implementation_flow_id, kind="implementation"
            ),
        )
        candidate = _optional_published_sha(item, publication)
        result = execution.result if isinstance(execution.result, dict) else {}
        audit = audit_acceptance(
            result,
            evidence=evidence,
            execution=execution,
            flow=flow,
            candidate_revision=candidate,
            component_id=item.component_id,
            advisory_id=item.advisory_id,
            require_finding_absent=True,
        )
        if not evidence.available or not audit.accepted:
            item.state = "reaudit_incomplete"
            self._append(
                item,
                kind="recheck",
                outcome="incomplete",
                execution_id=execution.id,
                evidence_ref=evidence.as_dict(),
                publication_ref=publication.as_dict(),
                data={"reason": audit.reason, "verdict": audit.verdict},
            )
            return
        baseline = crud_security_maintenance.create_baseline(
            self.db,
            account_id=self.account_id,
            fields={
                "release_id": release.id,
                "item_id": item.id,
                "audit_execution_id": execution.id,
                "result_digest": audit.digest,
                "verdict": audit.verdict,
                "evidence_ref": evidence.as_dict(),
                "data": {
                    "advisory_id": item.advisory_id,
                    "component_id": item.component_id,
                    "checked_out_sha": audit.checked_out_sha,
                },
            },
        )
        release.accepted_baseline_id = baseline.id
        item.state = "resolved"
        self._append(
            item,
            kind="recheck",
            outcome="resolved",
            execution_id=execution.id,
            evidence_ref=evidence.as_dict(),
            publication_ref=publication.as_dict(),
            data={"baseline_id": str(baseline.id), "reason": audit.reason},
        )

    async def _dispatch_remediation(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
    ) -> None:
        if item.state in IN_FLIGHT_STATES and item.implementation_execution_id:
            return
        flow = self._require_flow(release.implementation_flow_id, kind="implementation")
        self._assert_implementation_flow(
            flow, self._require_project(release.project_id)
        )
        event = self._trigger_event(
            release,
            item,
            kind="implementation",
            sha=None,
            flow=flow,
        )
        execution = crud_security_maintenance.create_execution(
            self.db, flow_id=flow.id, event=event
        )
        item.implementation_execution_id = execution.id
        item.state = "remediation_pending"
        self._append(
            item,
            kind="implementation",
            outcome="dispatched",
            execution_id=execution.id,
        )
        await self._dispatch(execution)

    async def _dispatch_recheck(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
    ) -> None:
        flow_id = release.recheck_flow_id or release.audit_flow_id
        flow = self._require_flow(flow_id, kind="recheck")
        self._assert_audit_flow(flow, self._require_project(release.project_id))
        published = _optional_published_sha(item, None)
        if not published and item.implementation_execution_id:
            implementer = self._require_flow(
                release.implementation_flow_id, kind="implementation"
            )
            publication = publication_ref_from_execution(
                self._bound_execution(item.implementation_execution_id),
                flow=implementer,
            )
            published = publication.sha
        if not published:
            raise InvalidTransitionError("published_sha_missing")
        event = self._trigger_event(
            release, item, kind="recheck", sha=published, flow=flow
        )
        execution = crud_security_maintenance.create_execution(
            self.db, flow_id=flow.id, event=event
        )
        item.recheck_execution_id = execution.id
        item.state = "reauditing"
        self._append(
            item, kind="recheck", outcome="dispatched", execution_id=execution.id
        )
        await self._dispatch(execution)

    def _trigger_event(
        self,
        release: models.SecurityMaintenanceRelease,
        item: models.SecurityMaintenanceItem,
        *,
        kind: str,
        sha: str | None,
        flow: models.Flow,
    ) -> dict[str, Any]:
        project = self._require_project(release.project_id)
        return {
            "source": str(project.organization_id),
            "type": "security_maintenance",
            "project_id": str(project.id),
            "payload": {
                "sha": sha,
                ENVELOPE_KEY: {
                    "item_id": str(item.id),
                    "release_id": str(release.id),
                    "kind": kind,
                    "advisory_id": item.advisory_id,
                    "component_id": item.component_id,
                    "product_key": release.product_key,
                    "release_key": release.release_key,
                    "flow_id": str(flow.id),
                },
            },
        }

    async def _dispatch(self, execution: models.FlowExecution) -> None:
        from preloop.services.issue_lifecycle_worker import dispatch_lifecycle_execution
        from preloop.services.flow_trigger_service import FlowTriggerService

        flow = self._require_flow(execution.flow_id, kind="dispatch")

        async def local() -> None:
            service = FlowTriggerService(self.db)
            await service._start_flow_execution(
                flow,
                execution.trigger_event_details or {},
                None,
                precreated_execution=execution,
            )

        try:
            await dispatch_lifecycle_execution(execution.id, local)
        except FlowDispatchError as exc:
            raise SourceOutageError("dispatch_unavailable") from exc

    def _open_approval(self, item: models.SecurityMaintenanceItem) -> None:
        release = self._require_release(item.release_id)
        workflow = self._require_workflow(release.approval_workflow_id)
        tool = (
            self.db.query(models.ToolConfiguration)
            .filter(
                models.ToolConfiguration.account_id == self.account_id,
                models.ToolConfiguration.tool_name == "security_maintenance",
            )
            .first()
        )
        if tool is None:
            tool = models.ToolConfiguration(
                account_id=self.account_id,
                tool_name="security_maintenance",
                tool_source="builtin",
            )
            self.db.add(tool)
            self.db.flush()
        expires = self._now() + timedelta(seconds=DEFAULT_APPROVAL_TIMEOUT)
        row = models.ApprovalRequest(
            account_id=self.account_id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            tool_name="security_maintenance",
            tool_args={
                "item_id": str(item.id),
                "advisory_id": item.advisory_id,
                "component_id": item.component_id,
            },
            status="pending",
            expires_at=expires,
            decided_by_ai=False,
        )
        self.db.add(row)
        self.db.flush()
        item.approval_request_id = row.id
        item.state = "approval_pending"
        self._append(
            item,
            kind="approval",
            outcome="requested",
            approval_request_id=row.id,
        )

    def _ensure_issue(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
    ) -> None:
        if item.issue_id:
            return
        project = self._require_project(release.project_id)
        organization = self.db.get(models.Organization, project.organization_id)
        if organization is None:
            raise CrossAccountError("project_not_in_account")
        issue = crud_security_maintenance.create_issue(
            self.db,
            fields={
                "title": (
                    f"{item.advisory_id} on {release.product_key} {release.release_key}"
                ),
                "description": (
                    f"Remediate {item.advisory_id} in {item.component_id} "
                    f"for supported release {release.release_key}."
                ),
                "status": "open",
                "issue_type": "vulnerability",
                "external_id": f"sm-{item.identity_key[:16]}",
                "project_id": project.id,
                "tracker_id": organization.tracker_id,
                "key": f"{release.product_key}#{item.advisory_id}",
            },
        )
        item.issue_id = issue.id

    def _bound_execution(self, execution_id: UUID) -> models.FlowExecution:
        execution = crud_security_maintenance.get_execution(
            self.db, account_id=self.account_id, execution_id=execution_id
        )
        if execution is None:
            raise CrossAccountError("execution_not_in_account")
        return execution

    def _require_platform_approval(
        self, item: models.SecurityMaintenanceItem
    ) -> models.ApprovalRequest:
        if item.approval_request_id is None:
            raise InvalidTransitionError("approval_missing")
        row = crud_security_maintenance.get_approval_request(
            self.db, account_id=self.account_id, request_id=item.approval_request_id
        )
        if row is None:
            raise InvalidTransitionError("approval_missing")
        return row

    def _approval_expired(self, row: models.ApprovalRequest) -> bool:
        if row.status == "expired":
            return True
        if row.status != "pending" or row.expires_at is None:
            return False
        expires = row.expires_at
        now = self._now()
        if expires.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=expires.tzinfo)
        return expires <= now

    def _append(
        self,
        item: models.SecurityMaintenanceItem,
        *,
        kind: str,
        outcome: str,
        actor_user_id: UUID | None = None,
        execution_id: UUID | None = None,
        approval_request_id: UUID | None = None,
        evidence_ref: dict[str, Any] | None = None,
        publication_ref: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        crud_security_maintenance.append_decision(
            self.db,
            account_id=self.account_id,
            item_id=item.id,
            kind=kind,
            outcome=outcome,
            actor_user_id=actor_user_id,
            execution_id=execution_id,
            approval_request_id=approval_request_id,
            evidence_ref=evidence_ref,
            publication_ref=publication_ref,
            data=data,
        )


def _envelope(execution: models.FlowExecution) -> dict[str, Any] | None:
    details = execution.trigger_event_details or {}
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    envelope = payload.get(ENVELOPE_KEY) or details.get(ENVELOPE_KEY)
    if isinstance(envelope, dict) and envelope.get("item_id"):
        return envelope
    return None


def _optional_published_sha(
    item: models.SecurityMaintenanceItem, publication: Any
) -> str | None:
    data = item.data or {}
    sha = data.get("published_sha")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    if publication is not None:
        return getattr(publication, "sha", None)
    return None
