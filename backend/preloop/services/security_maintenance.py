"""Durable supported-release vulnerability maintenance on existing flows."""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable, Iterable
from json import JSONDecodeError, dumps, loads
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.cra.schemas import SBOM_FORMATS
from preloop.models import models
from preloop.models.crud import crud_security_maintenance
from preloop.models.crud.security_maintenance import item_identity_key
from preloop.models.db.session import SyncApprovalSession
from preloop.schemas.security_maintenance import (
    ApprovalDecisionRequest,
    BaselineAcceptRequest,
    RebuiltInputsRequest,
    ResumeRequest,
    ScanFinding,
    ScanIngestRequest,
    SupportedReleaseCreate,
    SupportedReleaseUpdate,
)
from preloop.services.approval_service import ApprovalService
from preloop.services.flow_trigger_service import FlowDispatchError
from preloop.services.security_maintenance_refs import (
    CrossAccountError,
    InvalidTransitionError,
    MissingEvidenceError,
    SourceOutageError,
    StaleCompletionError,
    UnsupportedReleaseError,
    audit_acceptance,
    evidence_ref_from_execution,
    human_platform_approval,
    publication_ref_from_execution,
    tests_passed,
)
from preloop.utils.workspace_seed import parse_workspace_files

logger = logging.getLogger(__name__)

AUTO_DISPATCH_STATES = frozenset({"open"})
IN_FLIGHT_STATES = frozenset(
    {
        "remediation_pending",
        "remediating",
        "tests_passed",
        "approval_pending",
        "awaiting_build",
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
ADVERTISED_SBOM_KINDS = frozenset({"cyclonedx-json", "spdx-json"}) | set(SBOM_FORMATS)
_MAX_SBOM_NESTING = 32
DEFAULT_APPROVAL_TIMEOUT = 86400
TERMINAL = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "STOPPED", "TIMED_OUT", "ABORTED"}
)
TOOL_NAME = "security_maintenance"
AUTHENTICATED_DECISION_CHANNEL = "console"
SWEEP_LIMIT = 50


class SecurityMaintenanceService:
    """Tenant controller for inventory, scan ingest, dispatch, and baselines."""

    def __init__(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: Callable[[], Any] | None = None,
    ) -> None:
        self.db = db
        self.account_id = account_id
        from datetime import datetime, timezone

        self._now = now or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def _approval_service(self) -> ApprovalService:
        base_url = os.getenv("PRELOOP_URL", "http://localhost")
        return ApprovalService(SyncApprovalSession(self.db), base_url)

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

    def _write_item(
        self, item: models.SecurityMaintenanceItem, **fields: Any
    ) -> models.SecurityMaintenanceItem:
        updated = crud_security_maintenance.update_item(
            self.db, account_id=self.account_id, item_id=item.id, fields=fields
        )
        if updated is None:
            raise CrossAccountError("item_not_found")
        return updated

    def _write_release(
        self, release: models.SecurityMaintenanceRelease, **fields: Any
    ) -> models.SecurityMaintenanceRelease:
        updated = crud_security_maintenance.update_release(
            self.db, account_id=self.account_id, release_id=release.id, fields=fields
        )
        if updated is None:
            raise CrossAccountError("release_not_found")
        return updated

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

    def _validate_release_workflow(
        self,
        workflow_id: UUID,
        *,
        owner_user_id: UUID | None,
        escalation_user_ids: list[UUID],
        timeout_seconds: int,
    ) -> models.ApprovalWorkflow:
        try:
            return crud_security_maintenance.validate_release_workflow(
                self.db,
                account_id=self.account_id,
                workflow_id=workflow_id,
                owner_user_id=owner_user_id,
                escalation_user_ids=escalation_user_ids,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            raise InvalidTransitionError(str(exc)) from exc

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
            self._validate_release_workflow(
                request.approval_workflow_id,
                owner_user_id=request.approval_owner_user_id,
                escalation_user_ids=request.escalation_user_ids,
                timeout_seconds=request.escalation_after_seconds,
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
            row = self._write_release(row, **updates)
            project = self._require_project(row.project_id)
            self._validate_flows(
                project=project,
                audit_flow_id=row.audit_flow_id,
                implementation_flow_id=row.implementation_flow_id,
                recheck_flow_id=row.recheck_flow_id,
                allowed_model_ids=row.allowed_model_ids,
            )
            self._validate_release_workflow(
                row.approval_workflow_id,
                owner_user_id=row.approval_owner_user_id,
                escalation_user_ids=list(row.escalation_user_ids or []),
                timeout_seconds=row.escalation_after_seconds,
            )
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
            if execution.status != "SUCCEEDED":
                raise InvalidTransitionError("audit_execution_not_completed")
            if execution.flow_id not in {
                release.audit_flow_id,
                release.recheck_flow_id,
            }:
                raise InvalidTransitionError("audit_execution_not_bound")
            if not _execution_bound_to_release(execution, release):
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
                platform_approvals=[],
                db=self.db,
                account_id=self.account_id,
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
                    "data": {
                        "reason": audit.reason,
                        "schema_id": audit.schema_id,
                        "result": result,
                    },
                },
            )
            crud_security_maintenance.set_accepted_baseline(
                self.db,
                account_id=self.account_id,
                release_id=release.id,
                baseline_id=baseline.id,
            )
            return self._serialize_baseline(baseline)

    async def schedule_baseline_audit(
        self, release_id: UUID, sbom_content_base64: str
    ) -> dict[str, Any]:
        """Create a bound initial-baseline audit with controller envelope records."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"release-id:{release_id}"
        ):
            release = self._require_release(release_id)
            files = [
                {
                    "path": release.sbom_input_ref,
                    "content_base64": sbom_content_base64,
                }
            ]
            parse_workspace_files({"workspace_files": files})
            _parse_supplied_sbom(
                sbom_content_base64, allowed_kinds=release.allowed_input_kinds
            )
            digest = _input_digest(
                release.pinned_build_ref,
                release.sbom_input_ref,
                sbom_content_base64,
                release.accepted_baseline_id,
            )
            flow = self._require_flow(release.audit_flow_id, kind="audit")
            self._assert_audit_flow(flow, self._require_project(release.project_id))
            event = {
                "source": "security_maintenance",
                "type": "security_maintenance",
                "payload": {
                    "workspace_files": files,
                    ENVELOPE_KEY: {
                        "release_id": str(release.id),
                        "kind": "baseline",
                        "product_key": release.product_key,
                        "release_key": release.release_key,
                        "flow_id": str(flow.id),
                        "pinned_build_ref": release.pinned_build_ref,
                        "sbom_input_ref": release.sbom_input_ref,
                        "accepted_baseline_id": (
                            str(release.accepted_baseline_id)
                            if release.accepted_baseline_id
                            else None
                        ),
                        "input_digest": digest,
                        "sbom_digest": _sbom_bytes_digest(sbom_content_base64),
                    },
                },
            }
            execution = crud_security_maintenance.create_execution(
                self.db, flow_id=flow.id, event=event
            )
            return {
                "execution_id": str(execution.id),
                "input_digest": digest,
                "sbom_digest": _sbom_bytes_digest(sbom_content_base64),
            }

    async def ingest_scan(self, request: ScanIngestRequest) -> dict[str, Any]:
        """Upsert findings onto one opted-in release. Unsupported names fail."""
        token = f"scan:{request.product_key}:{request.release_key}"
        queued: list[tuple[UUID, UUID, str]] = []
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
                serialized, job = self._upsert_finding(
                    release,
                    finding,
                    request=request,
                )
                items.append(serialized)
                if job is not None:
                    queued.append(job)
        for execution_id, item_id, kind in queued:
            await self._enqueue(execution_id, item_id, kind)
        release = crud_security_maintenance.get_release_by_identity(
            self.db,
            account_id=self.account_id,
            product_key=request.product_key,
            release_key=request.release_key,
        )
        assert release is not None
        return {"release": self._serialize_release(release), "items": items}

    def _upsert_finding(
        self,
        release: models.SecurityMaintenanceRelease,
        finding: ScanFinding,
        *,
        request: ScanIngestRequest,
    ) -> tuple[dict[str, Any], tuple[UUID, UUID, str] | None]:
        identity = item_identity_key(
            self.account_id,
            release.product_key,
            release.release_key,
            finding.advisory_id,
            finding.component_id,
        )
        fingerprint = _sha256_json(
            {
                "advisory_id": finding.advisory_id,
                "component_id": finding.component_id,
                "aliases": finding.aliases,
                "severity": finding.severity,
                "present": finding.present,
                "source": request.source,
            }
        )
        item = crud_security_maintenance.get_item_by_identity(
            self.db, account_id=self.account_id, identity_key=identity
        )
        job: tuple[UUID, UUID, str] | None = None
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
                        "source": request.source,
                    },
                },
            )
            self._append(
                item,
                kind="scan",
                outcome="opened",
                execution_id=request.execution_id,
                data={"fingerprint": fingerprint, "present": True},
            )
            self._bind_issue(item, release, request.issue_id)
            self._snapshot_inputs(item, release, request)
            if item.state in AUTO_DISPATCH_STATES:
                job = self._reserve_remediation(item, release)
            return self._serialize_item(item), job
        data = dict(item.data or {})
        data.update(
            {
                "severity": finding.severity,
                "aliases": finding.aliases,
                "source": request.source,
                "last_present": finding.present,
            }
        )
        item = self._write_item(item, scan_fingerprint=fingerprint, data=data)
        if request.issue_id and item.issue_id is None:
            self._bind_issue(item, release, request.issue_id)
        self._snapshot_inputs(item, release, request)
        if not finding.present:
            self._append(
                item,
                kind="scan",
                outcome="finding_absent_unverified",
                execution_id=request.execution_id,
                data={"fingerprint": fingerprint},
            )
            return self._serialize_item(item), None
        self._append(
            item,
            kind="scan",
            outcome="updated",
            execution_id=request.execution_id,
            data={"fingerprint": fingerprint, "state": item.state},
        )
        if (
            item.state in AUTO_DISPATCH_STATES
            and item.implementation_execution_id is None
        ):
            job = self._reserve_remediation(item, release)
        return self._serialize_item(item), job

    async def resume(
        self, item_id: UUID, request: ResumeRequest, *, actor_user_id: UUID
    ) -> dict[str, Any]:
        """Human retry. History stays append-only."""
        queued: tuple[UUID, UUID, str] | None = None
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state not in RESUME_STATES:
                raise InvalidTransitionError("item_not_resumable")
            release = self._require_release(item.release_id)
            if item.retry_count >= release.max_retries:
                raise InvalidTransitionError("retry_budget_exhausted")
            item = self._write_item(item, retry_count=item.retry_count + 1)
            self._append(
                item,
                kind="resume",
                outcome="accepted",
                actor_user_id=actor_user_id,
                data={"reason": request.reason, "from_state": item.state},
            )
            if item.state in {"reaudit_incomplete"}:
                queued = self._reserve_recheck(item, release)
            else:
                item = self._write_item(item, implementation_execution_id=None)
                queued = self._reserve_remediation(item, release)
            serialized = self._serialize_item(item)
        if queued is not None:
            await self._enqueue(*queued)
            serialized = self._serialize_item(self._require_item(item_id))
        return serialized

    async def decide_approval(
        self,
        item_id: UUID,
        request: ApprovalDecisionRequest,
        *,
        actor_user_id: UUID,
        approved: bool,
    ) -> dict[str, Any]:
        """Record a vote through ApprovalService, then advance the item."""
        request_id: UUID | None = None
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state in {
                "held",
                "escalated",
                "reauditing",
                "reaudit_pending",
                "awaiting_build",
            }:
                return self._serialize_item(item)
            if item.state not in {"tests_passed", "approval_pending"}:
                raise InvalidTransitionError("item_not_awaiting_approval")
            release = self._require_release(item.release_id)
            workflow = self._require_workflow(release.approval_workflow_id)
            self._assert_actor_can_decide(workflow, release, actor_user_id)
            row = self._require_platform_approval(item)
            request_id = row.id
        service = self._approval_service()
        if approved:
            updated = await service.approve_request(
                request_id,
                request.reason,
                user_id=actor_user_id,
                channel=AUTHENTICATED_DECISION_CHANNEL,
            )
        else:
            updated = await service.decline_request(
                request_id,
                request.reason,
                user_id=actor_user_id,
                channel=AUTHENTICATED_DECISION_CHANNEL,
            )
        if updated is None:
            raise InvalidTransitionError("approval_missing")
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            self._apply_platform_approval(item, updated, actor_user_id=actor_user_id)
            return self._serialize_item(self._require_item(item_id))

    async def submit_rebuilt_inputs(
        self,
        item_id: UUID,
        request: RebuiltInputsRequest,
        *,
        actor_user_id: UUID,
    ) -> dict[str, Any]:
        """Ingest a rebuilt SBOM bound to the published SHA, then reserve recheck."""
        queued: tuple[UUID, UUID, str] | None = None
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state != "awaiting_build":
                raise InvalidTransitionError("item_not_awaiting_build")
            snapshot = dict(item.data or {})
            published = str(snapshot.get("published_sha") or "").strip().lower()
            if request.published_sha != published:
                raise InvalidTransitionError("published_sha_mismatch")
            original = snapshot.get("original_sbom_digest")
            new_digest = _sbom_bytes_digest(request.sbom_content_base64)
            if original and new_digest == original:
                raise InvalidTransitionError("stale_sbom_reused")
            release = self._require_release(item.release_id)
            pin = request.pinned_build_ref or request.published_sha
            files: list[dict[str, Any]] = [
                {
                    "path": release.sbom_input_ref,
                    "content_base64": request.sbom_content_base64,
                }
            ]
            if release.accepted_baseline_id:
                baseline = crud_security_maintenance.get_baseline(
                    self.db,
                    account_id=self.account_id,
                    baseline_id=release.accepted_baseline_id,
                )
                previous = (baseline.data or {}).get("result") if baseline else None
                if isinstance(previous, dict):
                    files.append(
                        {
                            "path": "previous-result.json",
                            "content_base64": base64.b64encode(
                                dumps(previous, sort_keys=True).encode()
                            ).decode("ascii"),
                        }
                    )
            parse_workspace_files({"workspace_files": files})
            _parse_supplied_sbom(
                request.sbom_content_base64,
                allowed_kinds=release.allowed_input_kinds,
            )
            rebuilt_digest = _input_digest(
                pin,
                release.sbom_input_ref,
                request.sbom_content_base64,
                release.accepted_baseline_id,
            )
            snapshot.update(
                {
                    "rebuilt_pinned_build_ref": pin,
                    "rebuilt_published_sha": request.published_sha,
                    "rebuilt_input_digest": rebuilt_digest,
                    "rebuilt_sbom_digest": new_digest,
                    "workspace_files": files,
                }
            )
            item = self._write_item(item, data=snapshot)
            self._append(
                item,
                kind="build",
                outcome="accepted",
                actor_user_id=actor_user_id,
                data={
                    "published_sha": request.published_sha,
                    "sbom_digest": new_digest,
                },
            )
            queued = self._reserve_recheck(item, release)
            serialized = self._serialize_item(item)
        if queued is not None:
            await self._enqueue(*queued)
            serialized = self._serialize_item(self._require_item(item_id))
        return serialized

    async def escalate_item(
        self, item_id: UUID, request: ApprovalDecisionRequest, *, actor_user_id: UUID
    ) -> dict[str, Any]:
        """Hold for named escalation owners. Never auto-releases."""
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            item = self._write_item(item, state="escalated")
            self._append(
                item,
                kind="approval",
                outcome="escalated",
                actor_user_id=actor_user_id,
                approval_request_id=item.approval_request_id,
                data={"reason": request.reason},
            )
            return self._serialize_item(item)

    async def reconcile_platform_approval(
        self, request_id: UUID
    ) -> dict[str, Any] | None:
        """Advance the bound item after a console or token ApprovalService decision."""
        item = crud_security_maintenance.get_item_by_approval_request(
            self.db, account_id=self.account_id, request_id=request_id
        )
        if item is None:
            return None
        return await self.reconcile_item(item.id)

    async def reconcile_item(self, item_id: UUID) -> dict[str, Any]:
        """Idempotent expiry, enqueue retry, and platform-approval follow-up."""
        queued: list[tuple[UUID, UUID, str]] = []
        open_approval = False
        approval_row_id: UUID | None = None
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            job = self._pending_dispatch_job(item)
            if job is not None:
                queued.append(job)
            item = self._require_item(item_id)
            if item.state == "tests_passed" and item.approval_request_id is None:
                open_approval = True
            elif item.state == "approval_pending" and item.approval_request_id:
                approval_row_id = item.approval_request_id
        if open_approval:
            await self._open_approval(item_id)
        if approval_row_id is not None:
            service = self._approval_service()
            current = await service.get_approval_request(approval_row_id)
            if current is not None:
                guarded = await service._reject_if_not_actionable(current)
                current = guarded or current
                async with crud_security_maintenance.locked(
                    self.db, self.account_id, f"item:{item_id}"
                ):
                    item = self._require_item(item_id)
                    self._apply_platform_approval(item, current)
        for job in queued:
            await self._enqueue(*job)
        return self._serialize_item(self._require_item(item_id))

    async def sweep(self) -> dict[str, Any]:
        """Bounded background reconcile. Concurrent sweeps of one tenant skip."""
        lock = crud_security_maintenance.try_sweep_lock(self.db, self.account_id)
        if lock is None:
            return {"acquired": False, "reconciled": 0}
        try:
            items = crud_security_maintenance.list_reconcile_items(
                self.db, account_id=self.account_id, limit=SWEEP_LIMIT
            )
            for item in items:
                try:
                    await self.reconcile_item(item.id)
                except Exception:
                    logger.exception(
                        "Security-maintenance reconcile failed for item %s", item.id
                    )
            return {"acquired": True, "reconciled": len(items)}
        finally:
            crud_security_maintenance.release_sweep_lock(self.db, lock)

    async def finish_execution(self, execution: models.FlowExecution) -> None:
        """Trusted completion hook. Stale or failed runs never advance a baseline."""
        envelope = _envelope(execution)
        if not envelope:
            return
        item_id = UUID(str(envelope["item_id"]))
        queued: tuple[UUID, UUID, str] | None = None
        open_approval = False
        kind = str(envelope.get("kind") or "")
        if kind == "recheck":
            item = self._require_item(item_id)
            async with crud_security_maintenance.locked(
                self.db, self.account_id, f"release-id:{item.release_id}"
            ):
                item = self._require_item(item_id)
                await self._finish_recheck(item, execution)
            return
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if kind == "implementation":
                open_approval = await self._finish_implementation(item, execution)
            elif kind == "recheck":
                await self._finish_recheck(item, execution)
        if open_approval:
            await self._open_approval(item_id)
        if queued is not None:
            await self._enqueue(*queued)

    async def _finish_implementation(
        self,
        item: models.SecurityMaintenanceItem,
        execution: models.FlowExecution,
    ) -> bool:
        if item.implementation_execution_id != execution.id:
            self._append(
                item,
                kind="implementation",
                outcome="stale_completion",
                execution_id=execution.id,
            )
            return False
        flow = self._require_flow(execution.flow_id, kind="implementation")
        publication = publication_ref_from_execution(execution, flow=flow)
        passed, reason = tests_passed(execution, flow=flow, publication=publication)
        evidence = evidence_ref_from_execution(
            execution, db=self.db, account_id=self.account_id
        )
        if execution.status not in TERMINAL:
            return False
        if not passed:
            item = self._write_item(item, state="tests_failed")
            self._append(
                item,
                kind="implementation",
                outcome="tests_failed",
                execution_id=execution.id,
                evidence_ref=evidence.as_dict(),
                publication_ref=publication.as_dict(),
                data={"reason": reason},
            )
            return False
        data = dict(item.data or {})
        data["published_sha"] = publication.sha
        item = self._write_item(item, state="tests_passed", data=data)
        self._append(
            item,
            kind="implementation",
            outcome="tests_passed",
            execution_id=execution.id,
            evidence_ref=evidence.as_dict(),
            publication_ref=publication.as_dict(),
            data={"reason": reason},
        )
        return True

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
        if self._baseline_is_newer(release, execution):
            self._append(
                item,
                kind="recheck",
                outcome="stale_completion",
                execution_id=execution.id,
                data={"reason": "newer_baseline_present"},
            )
            return
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
        platform_row = (
            self._require_platform_approval(item) if item.approval_request_id else None
        )
        try:
            rebuilt_omits_target = _rebuilt_omits_target(item, release)
        except InvalidTransitionError as exc:
            item = self._write_item(item, state="reaudit_incomplete")
            self._append(
                item,
                kind="recheck",
                outcome="incomplete",
                execution_id=execution.id,
                evidence_ref=evidence.as_dict(),
                publication_ref=publication.as_dict(),
                data={"reason": str(exc)},
            )
            return
        audit = audit_acceptance(
            result,
            evidence=evidence,
            execution=execution,
            flow=flow,
            candidate_revision=candidate,
            component_id=item.component_id,
            advisory_id=item.advisory_id,
            platform_approvals=[platform_row] if platform_row is not None else [],
            require_finding_absent=True,
            db=self.db,
            account_id=self.account_id,
            rebuilt_omits_target=rebuilt_omits_target,
        )
        if not evidence.available or not audit.accepted:
            item = self._write_item(item, state="reaudit_incomplete")
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
                    "result": result,
                },
            },
        )
        try:
            crud_security_maintenance.set_accepted_baseline(
                self.db,
                account_id=self.account_id,
                release_id=release.id,
                baseline_id=baseline.id,
                expected_current=release.accepted_baseline_id,
            )
        except ValueError as exc:
            raise StaleCompletionError(str(exc)) from exc
        item = self._write_item(item, state="resolved")
        self._append(
            item,
            kind="recheck",
            outcome="resolved",
            execution_id=execution.id,
            evidence_ref=evidence.as_dict(),
            publication_ref=publication.as_dict(),
            data={"baseline_id": str(baseline.id), "reason": audit.reason},
        )

    def _baseline_is_newer(
        self,
        release: models.SecurityMaintenanceRelease,
        execution: models.FlowExecution,
    ) -> bool:
        if release.accepted_baseline_id is None:
            return False
        current = crud_security_maintenance.get_baseline(
            self.db,
            account_id=self.account_id,
            baseline_id=release.accepted_baseline_id,
        )
        if current is None or current.audit_execution_id == execution.id:
            return False
        current_exec = crud_security_maintenance.get_execution(
            self.db,
            account_id=self.account_id,
            execution_id=current.audit_execution_id,
        )
        if current_exec is None or current_exec.created_at is None:
            return False
        incoming = execution.created_at
        if incoming is None:
            return True
        return current_exec.created_at >= incoming

    def _reserve_remediation(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
    ) -> tuple[UUID, UUID, str] | None:
        if item.implementation_execution_id and item.state in IN_FLIGHT_STATES:
            return None
        flow = self._require_flow(release.implementation_flow_id, kind="implementation")
        self._assert_implementation_flow(
            flow, self._require_project(release.project_id)
        )
        event = self._trigger_event(
            release,
            item,
            kind="implementation",
            sha=_checkout_sha(item, release, kind="implementation"),
            flow=flow,
        )
        execution = crud_security_maintenance.create_execution(
            self.db, flow_id=flow.id, event=event
        )
        data = dict(item.data or {})
        data["dispatch_state"] = "pending"
        item = self._write_item(
            item,
            implementation_execution_id=execution.id,
            state="remediation_pending",
            data=data,
        )
        self._append(
            item,
            kind="implementation",
            outcome="reserved",
            execution_id=execution.id,
        )
        return execution.id, item.id, "implementation"

    def _reserve_recheck(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
    ) -> tuple[UUID, UUID, str] | None:
        if item.recheck_execution_id and item.state in {
            "reaudit_pending",
            "reauditing",
        }:
            execution = crud_security_maintenance.get_execution(
                self.db,
                account_id=self.account_id,
                execution_id=item.recheck_execution_id,
            )
            if execution is not None and execution.status == "PENDING":
                return execution.id, item.id, "recheck"
        flow_id = release.recheck_flow_id or release.audit_flow_id
        flow = self._require_flow(flow_id, kind="recheck")
        self._assert_audit_flow(flow, self._require_project(release.project_id))
        published = _checkout_sha(item, release, kind="recheck")
        if not published:
            raise InvalidTransitionError("published_sha_missing")
        event = self._trigger_event(
            release, item, kind="recheck", sha=published, flow=flow
        )
        execution = crud_security_maintenance.create_execution(
            self.db, flow_id=flow.id, event=event
        )
        data = dict(item.data or {})
        data["dispatch_state"] = "pending"
        item = self._write_item(
            item,
            recheck_execution_id=execution.id,
            state="reaudit_pending",
            data=data,
        )
        self._append(
            item, kind="recheck", outcome="reserved", execution_id=execution.id
        )
        return execution.id, item.id, "recheck"

    def _trigger_event(
        self,
        release: models.SecurityMaintenanceRelease,
        item: models.SecurityMaintenanceItem,
        *,
        kind: str,
        sha: str | None,
        flow: models.Flow,
    ) -> dict[str, Any]:
        self._assert_fresh_inputs(item, release, kind=kind)
        project = self._require_project(release.project_id)
        issue = None
        if item.issue_id:
            issue = crud_security_maintenance.get_issue_for_project(
                self.db,
                account_id=self.account_id,
                project_id=project.id,
                issue_id=item.issue_id,
            )
            if issue is None:
                raise InvalidTransitionError("issue_not_bound")
        else:
            raise InvalidTransitionError("issue_required")
        organization = crud_security_maintenance.get_organization(
            self.db,
            account_id=self.account_id,
            organization_id=project.organization_id,
        )
        if organization is None:
            raise CrossAccountError("project_not_in_account")
        snapshot = dict(item.data or {})
        pin = (
            snapshot.get("rebuilt_pinned_build_ref")
            if kind == "recheck"
            else snapshot.get("pinned_build_ref") or release.pinned_build_ref
        )
        sbom_input_ref = str(snapshot.get("sbom_input_ref") or release.sbom_input_ref)
        sbom_b64 = _sbom_b64_from_files(snapshot.get("workspace_files"), sbom_input_ref)
        payload: dict[str, Any] = {
            "sha": sha,
            "object_attributes": {
                "title": issue.title,
                "description": issue.description,
                "iid": issue.external_id,
                "number": _issue_number(issue),
                "url": issue.external_url,
            },
            "repository": {
                "name": project.name,
                "full_name": project.identifier,
            },
            "workspace_files": snapshot.get("workspace_files") or [],
            ENVELOPE_KEY: {
                "item_id": str(item.id),
                "release_id": str(release.id),
                "kind": kind,
                "advisory_id": item.advisory_id,
                "component_id": item.component_id,
                "product_key": release.product_key,
                "release_key": release.release_key,
                "flow_id": str(flow.id),
                "pinned_build_ref": pin,
                "sbom_input_ref": sbom_input_ref,
                "accepted_baseline_id": snapshot.get("accepted_baseline_id")
                or (
                    str(release.accepted_baseline_id)
                    if release.accepted_baseline_id
                    else None
                ),
                "input_digest": (
                    snapshot.get("rebuilt_input_digest")
                    if kind == "recheck"
                    else snapshot.get("input_digest")
                ),
                "sbom_digest": _sbom_bytes_digest(sbom_b64) if sbom_b64 else None,
                "published_sha": snapshot.get("published_sha"),
                "issue_id": str(issue.id),
                "issue_number": _issue_number(issue),
                "repository": project.identifier,
            },
        }
        parse_workspace_files(payload)
        return {
            "source": str(issue.tracker_id),
            "type": "issue_labeled"
            if kind == "implementation"
            else "security_maintenance",
            "project_id": str(project.id),
            "payload": payload,
        }

    async def _enqueue(self, execution_id: UUID, item_id: UUID, kind: str) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService
        from preloop.services.issue_lifecycle_worker import dispatch_lifecycle_execution

        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            bound = (
                item.implementation_execution_id
                if kind == "implementation"
                else item.recheck_execution_id
            )
            if bound != execution_id:
                return
            data = dict(item.data or {})
            if data.get("dispatch_state") != "pending":
                return
            data["dispatch_state"] = "dispatching"
            self._write_item(item, data=data)
        execution = self._bound_execution(execution_id)
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
        except FlowDispatchError:
            logger.warning(
                "Security-maintenance enqueue deferred for execution %s", execution.id
            )
            async with crud_security_maintenance.locked(
                self.db, self.account_id, f"item:{item_id}"
            ):
                item = self._require_item(item_id)
                data = dict(item.data or {})
                if data.get("dispatch_state") == "dispatching":
                    data["dispatch_state"] = "pending"
                    self._write_item(item, data=data)
            return
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            bound = (
                item.implementation_execution_id
                if kind == "implementation"
                else item.recheck_execution_id
            )
            if bound != execution_id:
                return
            data = dict(item.data or {})
            if data.get("dispatch_state") != "dispatching":
                return
            data["dispatch_state"] = "dispatched"
            pending_state = (
                "remediation_pending" if kind == "implementation" else "reaudit_pending"
            )
            fields: dict[str, Any] = {"data": data}
            if item.state == pending_state:
                fields["state"] = (
                    "remediating" if kind == "implementation" else "reauditing"
                )
            item = self._write_item(item, **fields)
            self._append(
                item,
                kind=kind,
                outcome="dispatched",
                execution_id=execution_id,
            )

    async def _open_approval(self, item_id: UUID) -> None:
        tool_id = None
        workflow = None
        timeout = DEFAULT_APPROVAL_TIMEOUT
        tool_args: dict[str, Any] = {}
        execution_id: str | None = None
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            if item.state != "tests_passed":
                return
            if item.approval_request_id is not None:
                self._write_item(item, state="approval_pending")
                return
            existing = crud_security_maintenance.get_pending_maintenance_approval(
                self.db, account_id=self.account_id, item_id=item.id
            )
            if existing is not None:
                self._write_item(
                    item, approval_request_id=existing.id, state="approval_pending"
                )
                return
            release = self._require_release(item.release_id)
            workflow = self._validate_release_workflow(
                release.approval_workflow_id,
                owner_user_id=release.approval_owner_user_id,
                escalation_user_ids=list(release.escalation_user_ids or []),
                timeout_seconds=release.escalation_after_seconds,
            )
            tool = crud_security_maintenance.get_or_create_tool_configuration(
                self.db, account_id=self.account_id, tool_name=TOOL_NAME
            )
            tool_id = tool.id
            timeout = release.escalation_after_seconds
            execution_id = (
                str(item.implementation_execution_id)
                if item.implementation_execution_id
                else None
            )
            tool_args = {
                "item_id": str(item.id),
                "advisory_id": item.advisory_id,
                "component_id": item.component_id,
                "release_id": str(release.id),
                "policy": {
                    "owner_user_id": str(release.approval_owner_user_id)
                    if release.approval_owner_user_id
                    else None,
                    "escalation_user_ids": [
                        str(user_id) for user_id in (release.escalation_user_ids or [])
                    ],
                    "timeout_seconds": release.escalation_after_seconds,
                },
            }
            data = dict(item.data or {})
            data["approval_opening"] = True
            self._write_item(item, data=data)
        if workflow is None or tool_id is None:
            return
        service = self._approval_service()
        created = await service.create_approval_request(
            account_id=str(self.account_id),
            tool_configuration_id=tool_id,
            approval_workflow_id=workflow.id,
            tool_name=TOOL_NAME,
            tool_args=tool_args,
            execution_id=execution_id,
            timeout_seconds=timeout,
        )
        try:
            await service.send_notifications(created, workflow)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Security-maintenance approval %s created without notify: %s",
                created.id,
                exc,
            )
        async with crud_security_maintenance.locked(
            self.db, self.account_id, f"item:{item_id}"
        ):
            item = self._require_item(item_id)
            data = dict(item.data or {})
            data.pop("approval_opening", None)
            if item.state == "tests_passed" and item.approval_request_id is None:
                item = self._write_item(
                    item,
                    approval_request_id=created.id,
                    state="approval_pending",
                    data=data,
                )
                self._append(
                    item,
                    kind="approval",
                    outcome="requested",
                    approval_request_id=created.id,
                )
            else:
                self._write_item(item, data=data)

    def _pending_dispatch_job(
        self, item: models.SecurityMaintenanceItem
    ) -> tuple[UUID, UUID, str] | None:
        data = dict(item.data or {})
        if data.get("dispatch_state") == "dispatching":
            return None
        if item.state == "remediation_pending" and item.implementation_execution_id:
            execution = self._bound_execution(item.implementation_execution_id)
            if execution.status == "PENDING":
                return execution.id, item.id, "implementation"
        if item.state == "reaudit_pending" and item.recheck_execution_id:
            execution = self._bound_execution(item.recheck_execution_id)
            if execution.status == "PENDING":
                return execution.id, item.id, "recheck"
        return None

    def _apply_platform_approval(
        self,
        item: models.SecurityMaintenanceItem,
        row: models.ApprovalRequest,
        *,
        actor_user_id: UUID | None = None,
    ) -> None:
        if item.state not in {"tests_passed", "approval_pending"}:
            return
        outcome = human_platform_approval(row)
        if outcome == "approval_expired":
            self._write_item(item, state="escalated")
            self._append(
                item,
                kind="approval",
                outcome="expired",
                actor_user_id=actor_user_id,
                approval_request_id=row.id,
            )
            return
        if outcome == "approval_denied":
            self._write_item(item, state="held")
            self._append(
                item,
                kind="approval",
                outcome="denied",
                actor_user_id=actor_user_id,
                approval_request_id=row.id,
            )
            return
        if outcome != "approved":
            return
        self._write_item(item, state="awaiting_build")
        self._append(
            item,
            kind="approval",
            outcome="approved",
            actor_user_id=actor_user_id,
            approval_request_id=row.id,
        )

    def _assert_actor_can_decide(
        self,
        workflow: models.ApprovalWorkflow,
        release: models.SecurityMaintenanceRelease,
        actor_user_id: UUID,
    ) -> None:
        allowed = set(workflow.approver_user_ids or [])
        if release.approval_owner_user_id:
            allowed.add(release.approval_owner_user_id)
        if actor_user_id not in allowed:
            raise InvalidTransitionError("not_an_approver")

    def _bind_issue(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
        issue_id: UUID | None,
    ) -> None:
        if item.issue_id:
            return
        if issue_id is None:
            raise InvalidTransitionError("issue_required")
        issue = crud_security_maintenance.get_issue_for_project(
            self.db,
            account_id=self.account_id,
            project_id=release.project_id,
            issue_id=issue_id,
        )
        if issue is None:
            raise CrossAccountError("issue_not_in_project")
        self._write_item(item, issue_id=issue.id)

    def _snapshot_inputs(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
        request: ScanIngestRequest,
    ) -> None:
        files = [entry.model_dump() for entry in request.workspace_files]
        if request.sbom_content_base64:
            files.append(
                {
                    "path": release.sbom_input_ref,
                    "content_base64": request.sbom_content_base64,
                }
            )
        previous = None
        if release.accepted_baseline_id:
            baseline = crud_security_maintenance.get_baseline(
                self.db,
                account_id=self.account_id,
                baseline_id=release.accepted_baseline_id,
            )
            if baseline is not None:
                previous = (baseline.data or {}).get("result")
                if isinstance(previous, dict):
                    files.append(
                        {
                            "path": "previous-result.json",
                            "content_base64": base64.b64encode(
                                dumps(previous, sort_keys=True).encode()
                            ).decode("ascii"),
                        }
                    )
        parse_workspace_files({"workspace_files": files} if files else {})
        digest = _input_digest(
            release.pinned_build_ref,
            release.sbom_input_ref,
            request.sbom_content_base64,
            release.accepted_baseline_id,
        )
        original_sbom = None
        if request.sbom_content_base64:
            original_sbom = _sbom_bytes_digest(request.sbom_content_base64)
        data = dict(item.data or {})
        data.update(
            {
                "pinned_build_ref": release.pinned_build_ref,
                "sbom_input_ref": release.sbom_input_ref,
                "accepted_baseline_id": str(release.accepted_baseline_id)
                if release.accepted_baseline_id
                else None,
                "input_digest": digest,
                "original_sbom_digest": original_sbom
                or data.get("original_sbom_digest"),
                "workspace_files": files,
            }
        )
        self._write_item(item, data=data)

    def _assert_fresh_inputs(
        self,
        item: models.SecurityMaintenanceItem,
        release: models.SecurityMaintenanceRelease,
        *,
        kind: str,
    ) -> None:
        snapshot = dict(item.data or {})
        if kind == "recheck":
            rebuilt = snapshot.get("rebuilt_input_digest")
            if not rebuilt:
                raise InvalidTransitionError("rebuilt_inputs_required")
            computed = _input_digest(
                str(snapshot.get("rebuilt_pinned_build_ref") or ""),
                str(snapshot.get("sbom_input_ref") or release.sbom_input_ref),
                _sbom_b64_from_files(
                    snapshot.get("workspace_files"),
                    str(snapshot.get("sbom_input_ref") or release.sbom_input_ref),
                ),
                snapshot.get("accepted_baseline_id"),
            )
            if computed != rebuilt:
                raise InvalidTransitionError("stale_rebuilt_inputs")
            if snapshot.get("rebuilt_published_sha") != snapshot.get("published_sha"):
                raise InvalidTransitionError("rebuilt_sha_mismatch")
            return
        expected = _input_digest(
            release.pinned_build_ref,
            release.sbom_input_ref,
            _sbom_b64_from_files(
                snapshot.get("workspace_files"), release.sbom_input_ref
            ),
            release.accepted_baseline_id,
        )
        stored = snapshot.get("input_digest")
        if stored and stored != expected:
            raise InvalidTransitionError("stale_input_refs")
        if (
            snapshot.get("pinned_build_ref")
            not in {
                None,
                release.pinned_build_ref,
            }
            and snapshot.get("pinned_build_ref") != release.pinned_build_ref
        ):
            raise InvalidTransitionError("stale_input_refs")

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


def _controller_release_envelope(
    execution: models.FlowExecution,
) -> dict[str, Any] | None:
    details = execution.trigger_event_details or {}
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    envelope = payload.get(ENVELOPE_KEY) or details.get(ENVELOPE_KEY)
    if isinstance(envelope, dict) and envelope.get("release_id"):
        return envelope
    return None


def _execution_bound_to_release(
    execution: models.FlowExecution, release: models.SecurityMaintenanceRelease
) -> bool:
    envelope = _controller_release_envelope(execution)
    if envelope is None:
        return False
    if str(envelope.get("release_id")) != str(release.id):
        return False
    if envelope.get("pinned_build_ref") != release.pinned_build_ref:
        return False
    if envelope.get("sbom_input_ref") != release.sbom_input_ref:
        return False
    expected_baseline = (
        str(release.accepted_baseline_id) if release.accepted_baseline_id else None
    )
    envelope_baseline = envelope.get("accepted_baseline_id")
    if expected_baseline is not None:
        if str(envelope_baseline or "") != expected_baseline:
            return False
    elif envelope_baseline not in {None, ""}:
        return False
    details = execution.trigger_event_details or {}
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    actual_b64 = _sbom_b64_from_files(
        payload.get("workspace_files"), release.sbom_input_ref
    )
    if not actual_b64:
        return False
    expected = _input_digest(
        release.pinned_build_ref,
        release.sbom_input_ref,
        actual_b64,
        release.accepted_baseline_id,
    )
    if envelope.get("input_digest") != expected:
        return False
    stored_digest = envelope.get("sbom_digest")
    if stored_digest and stored_digest != _sbom_bytes_digest(actual_b64):
        return False
    return True


def _sbom_bytes_digest(content_base64: str) -> str:
    from hashlib import sha256

    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception:
        raw = content_base64.encode()
    return sha256(raw).hexdigest()


def _rebuilt_omits_target(
    item: models.SecurityMaintenanceItem,
    release: models.SecurityMaintenanceRelease,
) -> bool:
    snapshot = dict(item.data or {})
    if not snapshot.get("rebuilt_input_digest"):
        return False
    path = str(snapshot.get("sbom_input_ref") or release.sbom_input_ref)
    sbom_b64 = _sbom_b64_from_files(snapshot.get("workspace_files"), path)
    if not sbom_b64:
        raise InvalidTransitionError("rebuilt_sbom_missing")
    computed = _input_digest(
        str(snapshot.get("rebuilt_pinned_build_ref") or ""),
        path,
        sbom_b64,
        snapshot.get("accepted_baseline_id"),
    )
    if computed != snapshot.get("rebuilt_input_digest"):
        raise InvalidTransitionError("stale_rebuilt_inputs")
    stored_digest = snapshot.get("rebuilt_sbom_digest")
    if stored_digest and stored_digest != _sbom_bytes_digest(sbom_b64):
        raise InvalidTransitionError("stale_rebuilt_inputs")
    identities = _parse_supplied_sbom(
        sbom_b64, allowed_kinds=release.allowed_input_kinds
    )
    return not _identity_matches(item.component_id, identities)


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


def _checkout_sha(
    item: models.SecurityMaintenanceItem,
    release: models.SecurityMaintenanceRelease,
    *,
    kind: str,
) -> str | None:
    if kind == "recheck":
        published = _optional_published_sha(item, None)
        return published
    pinned = str((item.data or {}).get("pinned_build_ref") or release.pinned_build_ref)
    if len(pinned) == 40 and all(ch in "0123456789abcdef" for ch in pinned.lower()):
        return pinned.lower()
    return None


def _issue_number(issue: models.Issue) -> str | int:
    raw = str(issue.external_id or "").strip()
    if raw.isdigit():
        return int(raw)
    return raw


def _sha256_json(payload: dict[str, Any]) -> str:
    from hashlib import sha256

    return sha256(dumps(payload, sort_keys=True).encode()).hexdigest()


def _input_digest(
    pinned_build_ref: str,
    sbom_input_ref: str,
    sbom_b64: str | None,
    baseline_id: UUID | str | None,
) -> str:
    from hashlib import sha256

    material = sha256()
    material.update(pinned_build_ref.encode())
    material.update(b"\0")
    material.update(sbom_input_ref.encode())
    material.update(b"\0")
    material.update((sbom_b64 or "").encode())
    material.update(b"\0")
    material.update(str(baseline_id or "").encode())
    return material.hexdigest()


def _sbom_b64_from_files(files: Any, sbom_input_ref: str) -> str | None:
    if not isinstance(files, list):
        return None
    for entry in files:
        if isinstance(entry, dict) and entry.get("path") == sbom_input_ref:
            value = entry.get("content_base64")
            return value if isinstance(value, str) else None
    return None


def _canonical_sbom_kind(kind: str) -> str:
    token = kind.strip().lower()
    if token in {"cyclonedx", "cyclonedx-json"}:
        return "cyclonedx-json"
    if token in {"spdx", "spdx-json"}:
        return "spdx-json"
    return token


def _allowed_sbom_kinds(allowed_kinds: Iterable[str] | None) -> set[str]:
    if not allowed_kinds:
        return {kind for kind in ADVERTISED_SBOM_KINDS if kind.endswith("-json")}
    normalized = {_canonical_sbom_kind(str(kind)) for kind in allowed_kinds}
    advertised = {_canonical_sbom_kind(kind) for kind in ADVERTISED_SBOM_KINDS}
    return {kind for kind in normalized if kind in advertised}


def _identity_tokens(value: str) -> set[str]:
    token = value.strip()
    if not token:
        return set()
    tokens = {token}
    if token.lower().startswith("pkg:"):
        base = token.split("#", 1)[0].split("?", 1)[0]
        tokens.add(base)
        if "@" in base:
            without_version = base.rsplit("@", 1)[0]
            tokens.add(without_version)
            name = without_version.rsplit("/", 1)[-1]
            if name:
                tokens.add(name)
    return {item for item in tokens if item}


def _identity_matches(component_id: str, identities: set[str]) -> bool:
    needles = {item.lower() for item in _identity_tokens(component_id)}
    haystack = {item.lower() for item in identities}
    return bool(needles & haystack)


def _component_identity_tokens(entry: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("id", "name", "purl", "bom-ref", "bom_ref", "SPDXID"):
        raw = entry.get(key)
        if isinstance(raw, str):
            tokens.update(_identity_tokens(raw))
    refs = entry.get("externalRefs")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if str(ref.get("referenceType") or "").strip().lower() != "purl":
                continue
            locator = ref.get("referenceLocator")
            if isinstance(locator, str):
                tokens.update(_identity_tokens(locator))
    return tokens


def _walk_cyclonedx_components(items: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > _MAX_SBOM_NESTING:
        raise InvalidTransitionError("sbom_incomplete")
    if items is None:
        return []
    if not isinstance(items, list):
        raise InvalidTransitionError("sbom_malformed")
    found: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidTransitionError("sbom_malformed")
        found.append(item)
        if "components" in item:
            found.extend(
                _walk_cyclonedx_components(item.get("components"), depth=depth + 1)
            )
    return found


def _cyclonedx_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    metadata = document.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise InvalidTransitionError("sbom_malformed")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    if root is not None and not isinstance(root, dict):
        raise InvalidTransitionError("sbom_malformed")
    if isinstance(root, dict):
        entries.append(root)
        if "components" in root:
            entries.extend(_walk_cyclonedx_components(root.get("components")))
    if "components" in document:
        entries.extend(_walk_cyclonedx_components(document.get("components")))
    return entries


def _spdx_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    packages = document.get("packages")
    if packages is None:
        return []
    if not isinstance(packages, list):
        raise InvalidTransitionError("sbom_malformed")
    entries: list[dict[str, Any]] = []
    for item in packages:
        if not isinstance(item, dict):
            raise InvalidTransitionError("sbom_malformed")
        if str(item.get("SPDXID") or "").strip() == "SPDXRef-DOCUMENT":
            continue
        entries.append(item)
    return entries


def _detect_sbom_kind(document: dict[str, Any]) -> str:
    has_cyclonedx = str(document.get("bomFormat") or "").strip().lower() == "cyclonedx"
    has_spdx = bool(str(document.get("spdxVersion") or "").strip())
    if has_cyclonedx and has_spdx:
        raise InvalidTransitionError("sbom_ambiguous")
    if has_cyclonedx:
        return "cyclonedx-json"
    if has_spdx:
        return "spdx-json"
    raise InvalidTransitionError("sbom_unsupported")


def _parse_supplied_sbom(
    content_base64: str, *, allowed_kinds: Iterable[str] | None
) -> set[str]:
    """Parse advertised JSON SBOM bytes and return component identity tokens."""
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise InvalidTransitionError("sbom_malformed") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidTransitionError("sbom_malformed") from exc
    try:
        document = loads(text)
    except JSONDecodeError as exc:
        raise InvalidTransitionError("sbom_malformed") from exc
    if not isinstance(document, dict):
        raise InvalidTransitionError("sbom_malformed")
    kind = _detect_sbom_kind(document)
    allowed = _allowed_sbom_kinds(allowed_kinds)
    if kind not in allowed:
        raise InvalidTransitionError("sbom_unsupported")
    entries = (
        _cyclonedx_entries(document)
        if kind == "cyclonedx-json"
        else _spdx_entries(document)
    )
    identities: set[str] = set()
    for entry in entries:
        tokens = _component_identity_tokens(entry)
        if not tokens:
            raise InvalidTransitionError("sbom_incomplete")
        identities.update(tokens)
    return identities
