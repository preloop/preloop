"""Fail-closed authority for evidence, tests, publication, and CRA results.

Caller-supplied artifact ids, digests, availability flags, agent SHAs, and
agent ``trusted_publication`` dictionaries are never sufficient. This module
resolves already-persisted controller records through existing CRUD, artifact,
publication, and verification helpers. It does not vendor the CRA validator.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from json import dumps
from types import SimpleNamespace
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.cra.schemas import (
    DATABASE_SOURCES,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
    is_known_cra_result_schema,
)
from preloop.cra.validate import (
    AUTHORITY_REQUIRED,
    PlatformApproval,
    validate_cra_result,
)
from preloop.models import models
from preloop.models.crud import flow_artifact as artifact_crud
from preloop.services.flow_artifacts import (
    EvidenceUnavailableError,
    artifact_thread_id,
    inspect_evidence,
    load_evidence,
    validate_archive,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_VERIFICATION_BLOCKED,
    FAILURE_CATEGORY_VERIFICATION_FAILED,
)
from preloop.services.private_publication import trusted_private_receipt
from preloop.services.trusted_publisher import PublicationError
from preloop.utils.verification_selection import evaluate_from_raw


HEX40 = re.compile(r"[0-9a-f]{40}")
EVIDENCE_KIND = "evidence"
SCREENING_SCHEMAS = frozenset({SCHEMA_VULNSCAN_V1, SCHEMA_RELEASEAUDIT_V1})


class SecurityMaintenanceError(ValueError):
    """Base error for durable maintenance transitions."""


class UnsupportedReleaseError(SecurityMaintenanceError):
    """Scan or baseline named a product/release that is not opted in."""


class CrossAccountError(SecurityMaintenanceError):
    """Caller referenced an object owned by another tenant."""


class SourceOutageError(SecurityMaintenanceError):
    """Scan source or dispatch boundary was unavailable."""


class MissingEvidenceError(SecurityMaintenanceError):
    """Required evidence receipt is missing, expired, or failed."""


class StaleCompletionError(SecurityMaintenanceError):
    """Completion named an execution that is not bound to the item."""


class InvalidTransitionError(SecurityMaintenanceError):
    """Requested state change is not allowed from the current item state."""


@dataclass(frozen=True)
class EvidenceRef:
    """Pointer to evidence owned by the artifact/evidence workstream."""

    kind: str
    available: bool
    execution_id: UUID | None = None
    artifact_id: str | None = None
    digest: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-stable representation persisted on decisions."""
        return {
            "kind": self.kind,
            "available": self.available,
            "execution_id": str(self.execution_id) if self.execution_id else None,
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PublicationRef:
    """Remote publication receipt. Local commits alone are not success."""

    available: bool
    sha: str | None = None
    pull_request_url: str | None = None
    repository: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-stable representation persisted on decisions."""
        return {
            "available": self.available,
            "sha": self.sha,
            "pull_request_url": self.pull_request_url,
            "repository": self.repository,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AuditAcceptance:
    """Fail-closed judgment of an audit/recheck result for baseline use."""

    accepted: bool
    verdict: str
    reason: str
    schema_id: str | None = None
    finding_verified_absent: bool = False
    checked_out_sha: str | None = None
    digest: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def result_digest(result: dict[str, Any] | None) -> str:
    """Stable digest of a structured execution result."""
    payload = dumps(result or {}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def evidence_ref_from_execution(
    execution: models.FlowExecution,
    supplied: dict[str, Any] | None = None,
    *,
    db: Session,
    account_id: UUID,
) -> EvidenceRef:
    """Resolve evidence via account/execution-bound CRUD. Claims never grant.

    A caller ``available: false`` may deny. A caller artifact id, digest, or
    ``available: true`` cannot make missing, expired, foreign, or corrupt
    bytes look present.
    """
    claimed = supplied or {}
    if claimed.get("available") is False:
        return EvidenceRef(
            kind=str(claimed.get("kind") or "declared"),
            available=False,
            execution_id=execution.id,
            artifact_id=_optional_str(claimed.get("artifact_id")),
            digest=_optional_str(claimed.get("digest")),
            reason=str(claimed.get("reason") or "evidence_unavailable"),
        )
    claimed_id = _optional_str(claimed.get("artifact_id"))
    claimed_digest = _optional_str(claimed.get("digest"))
    try:
        archive, receipt = _load_bound_evidence(
            db,
            account_id=account_id,
            execution=execution,
            claimed_artifact_id=claimed_id,
        )
    except EvidenceUnavailableError as exc:
        return EvidenceRef(
            kind=str((exc.receipt or {}).get("transport") or "missing"),
            available=False,
            execution_id=execution.id,
            artifact_id=_optional_str((exc.receipt or {}).get("artifact_id"))
            or claimed_id,
            digest=_optional_str((exc.receipt or {}).get("sha256")) or claimed_digest,
            reason=f"evidence_{exc.code}",
        )
    if receipt.get("integrity_verified") is not True:
        return EvidenceRef(
            kind=str(receipt.get("kind") or EVIDENCE_KIND),
            available=False,
            execution_id=execution.id,
            artifact_id=_optional_str(receipt.get("artifact_id")) or claimed_id,
            digest=_optional_str(receipt.get("sha256")) or claimed_digest,
            reason="evidence_not_verified",
        )
    stored_digest = _optional_str(receipt.get("sha256"))
    stored_id = _optional_str(receipt.get("artifact_id"))
    if claimed_digest and claimed_digest != stored_digest:
        return EvidenceRef(
            kind=str(receipt.get("kind") or EVIDENCE_KIND),
            available=False,
            execution_id=execution.id,
            artifact_id=stored_id or claimed_id,
            digest=stored_digest,
            reason="evidence_digest_mismatch",
        )
    if claimed_id and stored_id and claimed_id != stored_id:
        return EvidenceRef(
            kind=str(receipt.get("kind") or EVIDENCE_KIND),
            available=False,
            execution_id=execution.id,
            artifact_id=stored_id,
            digest=stored_digest,
            reason="evidence_artifact_mismatch",
        )
    try:
        validate_archive(
            archive,
            max_bytes=_evidence_max_bytes(),
            max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
        )
    except ValueError as exc:
        return EvidenceRef(
            kind=str(receipt.get("kind") or EVIDENCE_KIND),
            available=False,
            execution_id=execution.id,
            artifact_id=stored_id,
            digest=stored_digest,
            reason=str(exc) or "artifact_corrupt",
        )
    if hashlib.sha256(archive).hexdigest() != stored_digest:
        return EvidenceRef(
            kind=str(receipt.get("kind") or EVIDENCE_KIND),
            available=False,
            execution_id=execution.id,
            artifact_id=stored_id,
            digest=stored_digest,
            reason="evidence_digest_mismatch",
        )
    return EvidenceRef(
        kind=str(receipt.get("kind") or receipt.get("transport") or EVIDENCE_KIND),
        available=True,
        execution_id=execution.id,
        artifact_id=stored_id,
        digest=stored_digest,
        reason=str(receipt.get("transport") or "evidence_available"),
    )


def publication_ref_from_execution(
    execution: models.FlowExecution,
    *,
    flow: models.Flow | None = None,
) -> PublicationRef:
    """Read a controller publication receipt. Agent URLs and ``sha`` keys fail."""
    git = (flow.git_clone_config if flow is not None else None) or {}
    try:
        receipt = trusted_private_receipt(execution)
    except PublicationError:
        receipt = None
    if isinstance(receipt, dict):
        return _publication_from_controller_receipt(
            receipt, execution_id=execution.id, reason="trusted_private_receipt"
        )
    if git.get("publication_mode") != "isolated":
        return PublicationRef(available=False, reason="publication_mode_not_isolated")
    if (git.get("verification") or {}).get("mode") != "gate":
        return PublicationRef(
            available=False, reason="verification_gate_not_configured"
        )
    result = execution.result if isinstance(execution.result, dict) else {}
    trusted = result.get("trusted_publication")
    if not isinstance(trusted, dict):
        return PublicationRef(available=False, reason="publication_receipt_missing")
    return _publication_from_controller_receipt(
        trusted, execution_id=execution.id, reason="isolated_controller_receipt"
    )


def tests_passed(
    execution: models.FlowExecution,
    *,
    flow: models.Flow,
    publication: PublicationRef,
) -> tuple[bool, str]:
    """Require controller verification of this execution's published commit.

    ``SUCCEEDED`` without a failure category is not a test receipt. Observe-only
    sandbox-log scrapes and agent ``verification_reported`` claims cannot pass.
    """
    if execution.status != "SUCCEEDED":
        return False, f"execution_{str(execution.status).lower()}"
    category = getattr(execution, "failure_category", None)
    if category == FAILURE_CATEGORY_VERIFICATION_FAILED:
        return False, "verification_failed"
    if category == FAILURE_CATEGORY_VERIFICATION_BLOCKED:
        return False, "verification_blocked"
    git = flow.git_clone_config or {}
    verification_cfg = git.get("verification") or {}
    profile = verification_cfg.get("profile")
    if verification_cfg.get("mode") != "gate" or not isinstance(profile, dict):
        return False, "verification_gate_not_configured"
    if not publication.available or not _hex40(publication.sha):
        return False, "trusted_verification_missing"
    result = execution.result if isinstance(execution.result, dict) else {}
    if result.get("verification_reported") is not None and _is_observe_only(
        result.get("verification")
    ):
        return False, "agent_authored_verification"
    evidence = result.get("verification")
    if not isinstance(evidence, dict):
        return False, "trusted_verification_missing"
    if _is_observe_only(evidence):
        return False, "observe_only_verification"
    tree_hash = _optional_str(evidence.get("tree_hash"))
    if not tree_hash:
        return False, "trusted_verification_missing"
    decision = evaluate_from_raw(
        evidence,
        profile=profile,
        commit_sha=publication.sha,
        tree_hash=tree_hash,
        clean_tree=True,
        changed_files=tuple(evidence.get("changed_files") or ()),
    )
    if not decision.get("allowed"):
        return False, str(decision.get("reason") or "verification_not_passed")
    return True, "tests_passed"


def audit_acceptance(
    result: dict[str, Any] | None,
    *,
    evidence: EvidenceRef,
    execution: models.FlowExecution,
    flow: models.Flow | None = None,
    candidate_revision: str | None = None,
    component_id: str | None = None,
    advisory_id: str | None = None,
    platform_approvals: Sequence[Any] | None = None,
    require_finding_absent: bool = False,
    db: Session | None = None,
    account_id: UUID | None = None,
    rebuilt_omits_target: bool = False,
) -> AuditAcceptance:
    """Fail closed unless the contracts validator and screening both succeed.

    Agent ``finding_absent``, ``checked_out_sha``, and ``revision`` fields cannot
    prove repair. Missing checkout SHA fails when a candidate revision is
    required. Unknown ``preloop.cra.*`` schemas are unsupported.
    """
    digest = result_digest(result)
    if str(getattr(execution, "status", "") or "") != "SUCCEEDED":
        return AuditAcceptance(
            accepted=False,
            verdict="incomplete",
            reason="audit_execution_not_completed",
            digest=digest,
        )
    if not evidence.available:
        return AuditAcceptance(
            accepted=False,
            verdict="incomplete",
            reason="missing_evidence",
            digest=digest,
        )
    checkout = controller_checkout_sha(
        execution, flow=flow, db=db, account_id=account_id
    )
    if candidate_revision:
        if not _hex40(checkout):
            return AuditAcceptance(
                accepted=False,
                verdict="incomplete",
                reason="missing_checked_out_sha",
                digest=digest,
            )
        if checkout != candidate_revision:
            return AuditAcceptance(
                accepted=False,
                verdict="incomplete",
                reason="audit_revision_mismatch",
                schema_id=_schema_id(result) if isinstance(result, dict) else None,
                checked_out_sha=checkout,
                digest=digest,
            )
    prompt = flow.prompt_template if flow is not None else None
    approvals = _platform_approvals(platform_approvals)
    cra = validate_cra_result(
        result,
        prompt=prompt,
        platform_approvals=approvals,
        require_coverage=require_finding_absent,
        authority=AUTHORITY_REQUIRED,
    )
    schema_id = cra.schema_id
    if cra.skipped or not schema_id or not is_known_cra_result_schema(schema_id):
        claimed = _schema_id(result) if isinstance(result, dict) else None
        reason = "unstructured_result_not_authoritative"
        if claimed and str(claimed).startswith("preloop.cra."):
            reason = "unsupported_cra_schema"
        return AuditAcceptance(
            accepted=False,
            verdict="unknown",
            reason=reason,
            schema_id=claimed or schema_id,
            checked_out_sha=checkout,
            digest=digest,
            details={"failures": list(cra.failures)},
        )
    if not cra.ok or cra.incomplete or not cra.execution_completed:
        return AuditAcceptance(
            accepted=False,
            verdict="incomplete" if cra.incomplete else "unknown",
            reason="audit_verdict_not_accepted",
            schema_id=schema_id,
            checked_out_sha=checkout,
            digest=digest,
            details={"failures": list(cra.failures), "incomplete": cra.incomplete},
        )
    payload = result if isinstance(result, dict) else {}
    screened, screen_reason = _target_component_screened(
        payload,
        schema_id=schema_id,
        component_id=component_id,
        rebuilt_omits_target=rebuilt_omits_target,
    )
    finding_absent = _advisory_absent_from_findings(payload, advisory_id)
    if require_finding_absent:
        if not screened:
            return AuditAcceptance(
                accepted=False,
                verdict=_verdict_from_cra(payload, schema_id),
                reason=screen_reason or "component_not_screened",
                schema_id=schema_id,
                finding_verified_absent=False,
                checked_out_sha=checkout,
                digest=digest,
            )
        if not finding_absent:
            return AuditAcceptance(
                accepted=False,
                verdict=_verdict_from_cra(payload, schema_id),
                reason="finding_not_verified_absent",
                schema_id=schema_id,
                finding_verified_absent=False,
                checked_out_sha=checkout,
                digest=digest,
            )
    if cra.release_denied and require_finding_absent:
        return AuditAcceptance(
            accepted=False,
            verdict=_verdict_from_cra(payload, schema_id),
            reason="audit_release_denied",
            schema_id=schema_id,
            finding_verified_absent=finding_absent,
            checked_out_sha=checkout,
            digest=digest,
        )
    return AuditAcceptance(
        accepted=True,
        verdict=_verdict_from_cra(payload, schema_id),
        reason="audit_accepted",
        schema_id=schema_id,
        finding_verified_absent=bool(require_finding_absent and finding_absent),
        checked_out_sha=checkout,
        digest=digest,
        details={"advisories": list(cra.advisories)},
    )


def controller_checkout_sha(
    execution: models.FlowExecution,
    *,
    flow: models.Flow | None = None,
    db: Session | None = None,
    account_id: UUID | None = None,
) -> str | None:
    """SHA from controller-verified frozen publication checkout records.

    Agent-writable ``HEAD.txt`` and trigger ``payload.sha`` are not authority.
    A forged evidence archive cannot establish release or build provenance.
    """
    shas = _frozen_checkout_shas(execution, flow=flow, db=db, account_id=account_id)
    if not shas:
        return None
    publication = publication_ref_from_execution(execution, flow=flow)
    if publication.available and _hex40(publication.sha):
        bound = str(publication.sha).lower()
        if bound in shas:
            return bound
    if len(shas) == 1:
        return next(iter(shas))
    return None


def _frozen_checkout_shas(
    execution: models.FlowExecution,
    *,
    flow: models.Flow | None,
    db: Session | None,
    account_id: UUID | None,
) -> set[str]:
    """Collect SHAs proven by frozen bundles or controller provenance records.

    Observed bundle SHAs are authoritative. Agent-written
    ``product_provenance`` rows cannot add or override them. ``HEAD.txt`` is
    never read. When no bundle is present, controller-attached verified
    records and isolated publication receipts remain available for helper
    coverage.
    """
    found: set[str] = set()
    publication = publication_ref_from_execution(execution, flow=flow)
    if publication.available and _hex40(publication.sha):
        found.add(str(publication.sha).lower())
    observed: set[str] = set()
    if db is not None and account_id is not None:
        try:
            archive, _receipt = load_evidence(
                db, account_id=account_id, execution=execution
            )
        except EvidenceUnavailableError:
            archive = None
        else:
            policy = checkout_observation_policy(flow, execution)
            if policy is not None:
                from preloop.services.multi_repo_publication import (
                    observed_checkout_shas,
                )
                from preloop.services.trusted_publisher import PublicationError

                try:
                    for sha in observed_checkout_shas(policy, archive).values():
                        if _hex40(sha):
                            observed.add(str(sha).lower())
                except PublicationError:
                    observed = set()
    if observed:
        return observed
    result = execution.result if isinstance(execution.result, dict) else {}
    provenance = result.get("product_provenance")
    if isinstance(provenance, dict):
        rows = provenance.get("repositories")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("sha_status") or "") != "verified":
                    continue
                sha = _optional_str(row.get("sha"))
                if _hex40(sha):
                    found.add(str(sha).lower())
    return found


def checkout_observation_policy(
    flow: models.Flow | None,
    execution: models.FlowExecution | None = None,
) -> Any | None:
    """Rebuild checkout targets so frozen evidence bundles can be inspected.

    Pins come from the trusted flow config or the controller envelope on the
    execution (published SHA / hex40 pinned build). Trigger ``payload.sha`` is
    not used. ``git_clone_config.repositories[].repository_url`` must be set;
    project/tracker ids alone cannot prove a remote.
    """
    git = (flow.git_clone_config if flow is not None else None) or {}
    if not isinstance(git, dict):
        return None
    envelope, mapping_pins = _execution_checkout_pins(execution)
    from preloop.services.multi_repo_publication import IsolatedPublicationTarget

    targets: list[Any] = []
    rows = git.get("repositories")
    if not isinstance(rows, list):
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        url = _optional_str(row.get("repository_url"))
        pin = _optional_str(row.get("commit") or row.get("pin_sha") or row.get("sha"))
        if not _hex40(pin):
            pin = mapping_pins.get(_normalize_remote(url) if url else "")
        if not _hex40(pin):
            pin = envelope
        if url is None or not _hex40(pin):
            continue
        path = str(row.get("clone_path") or f"workspace-{index + 1}")
        targets.append(
            IsolatedPublicationTarget(
                tracker_id=str(row.get("tracker_id") or ""),
                repository_url=url,
                clone_path=path,
                role=str(row.get("role") or "code"),
                branch=str(row.get("branch") or "main"),
                base=str(row.get("base") or "main"),
                expected_remote_sha=None,
                base_sha=str(pin).lower(),
            )
        )
    if not targets:
        return None
    return SimpleNamespace(targets=tuple(targets))


def _checkout_policy_from_flow(
    flow: models.Flow | None,
    execution: models.FlowExecution | None = None,
) -> Any | None:
    """Alias for :func:`checkout_observation_policy`."""
    return checkout_observation_policy(flow, execution)


def _execution_checkout_pins(
    execution: models.FlowExecution | None,
) -> tuple[str | None, dict[str, str]]:
    """Controller envelope SHA plus mapping pins keyed by normalized remote."""
    if execution is None:
        return None, {}
    details = getattr(execution, "trigger_event_details", None)
    if not isinstance(details, dict):
        return None, {}
    payload = details.get("payload")
    if not isinstance(payload, dict):
        payload = details
    envelope = payload.get("security_maintenance")
    envelope_sha = None
    if isinstance(envelope, dict):
        for key in ("published_sha", "pinned_build_ref"):
            value = _optional_str(envelope.get(key))
            if _hex40(value):
                envelope_sha = str(value).lower()
                break
    mapping = payload.get("product_provenance")
    pins: dict[str, str] = {}
    if isinstance(mapping, dict):
        rows = mapping.get("repositories")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                remote = _normalize_remote(
                    _optional_str(row.get("remote") or row.get("repository_url"))
                )
                sha = _optional_str(
                    row.get("sha") or row.get("commit") or row.get("head_sha")
                )
                if remote and _hex40(sha):
                    pins[remote] = str(sha).lower()
    return envelope_sha, pins


def _normalize_remote(url: str | None) -> str:
    if not url:
        return ""
    from preloop.services.product_provenance import normalize_repository_url

    try:
        return normalize_repository_url(url)
    except Exception:
        return url.strip().lower().rstrip("/")


def human_platform_approval(row: models.ApprovalRequest | None) -> str:
    """Classify a stored approval request. Agent result JSON is ignored."""
    if row is None:
        return "approval_missing"
    if row.status == "expired":
        return "approval_expired"
    if row.status in {"declined", "cancelled"}:
        return "approval_denied"
    if row.status != "approved":
        return "approval_pending"
    if getattr(row, "decided_by_ai", False):
        return "approval_not_human"
    if getattr(row, "auto_approved_reason", None):
        return "approval_not_human"
    return "approved"


def _load_bound_evidence(
    db: Session,
    *,
    account_id: UUID,
    execution: models.FlowExecution,
    claimed_artifact_id: str | None,
) -> tuple[bytes, dict[str, Any]]:
    if claimed_artifact_id:
        artifact = _bound_artifact(
            db,
            account_id=account_id,
            execution=execution,
            artifact_id=claimed_artifact_id,
        )
        if artifact is None:
            raise EvidenceUnavailableError(
                "missing",
                {"status": "missing", "execution_id": str(execution.id)},
            )
    archive, receipt = load_evidence(db, account_id=account_id, execution=execution)
    kind = str(receipt.get("kind") or EVIDENCE_KIND)
    if kind not in {EVIDENCE_KIND, "legacy"}:
        raise EvidenceUnavailableError(
            "failed",
            {**receipt, "error": "artifact_kind_mismatch"},
        )
    inspect_evidence(db, account_id=account_id, execution=execution)
    return archive, {**receipt, "kind": kind}


def _bound_artifact(
    db: Session,
    *,
    account_id: UUID,
    execution: models.FlowExecution,
    artifact_id: str,
) -> models.FlowArtifact | None:
    try:
        parsed = UUID(str(artifact_id))
    except (TypeError, ValueError):
        return None
    thread_id = artifact_thread_id(execution.trigger_event_details, execution.id)
    artifact = artifact_crud.get(
        db,
        artifact_id=parsed,
        account_id=account_id,
        flow_id=execution.flow_id,
        thread_id=thread_id,
    )
    if artifact is None or artifact.execution_id != execution.id:
        return None
    return artifact


def _publication_from_controller_receipt(
    receipt: dict[str, Any],
    *,
    execution_id: UUID,
    reason: str,
) -> PublicationRef:
    sha = _optional_str(receipt.get("head_sha"))
    if not _hex40(sha):
        return PublicationRef(available=False, reason="publication_head_sha_missing")
    records = receipt.get("records")
    if isinstance(records, list) and records:
        bound = False
        for record in records:
            if not isinstance(record, dict):
                continue
            if (
                str(record.get("execution_id")) == str(execution_id)
                and _optional_str(record.get("head_sha")) == sha
            ):
                bound = True
                break
        if not bound:
            return PublicationRef(
                available=False, reason="publication_execution_mismatch"
            )
    url = _optional_str(receipt.get("url") or receipt.get("pull_request_url"))
    repo = _optional_str(receipt.get("repository_url") or receipt.get("repository"))
    if not url:
        return PublicationRef(available=False, reason="publication_receipt_incomplete")
    return PublicationRef(
        available=True,
        sha=sha,
        pull_request_url=url,
        repository=repo,
        reason=reason,
    )


def _is_observe_only(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    if value.get("authenticated") is False:
        return True
    if value.get("source") == "sandbox_log":
        return True
    return False


def _target_component_screened(
    payload: dict[str, Any],
    *,
    schema_id: str,
    component_id: str | None,
    rebuilt_omits_target: bool = False,
) -> tuple[bool, str]:
    if schema_id not in SCREENING_SCHEMAS:
        return False, "component_not_screened"
    inventory = _inventory_from_payload(payload, schema_id)
    if not isinstance(inventory, dict):
        return False, "component_not_screened"
    listings = inventory.get("components_list")
    if not isinstance(listings, list) or not listings:
        return False, "component_identity_missing"
    names: set[str] = set()
    targets: list[dict[str, Any]] = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        identities = {
            str(item.get("id") or "").strip(),
            str(item.get("purl") or "").strip(),
            str(item.get("name") or "").strip(),
        }
        identities.discard("")
        names.update(identities)
        if component_id and component_id in identities:
            targets.append(item)
    if component_id and component_id not in names:
        if rebuilt_omits_target:
            return True, "component_removed_from_rebuild"
        return False, "component_not_in_inventory"
    unmatchable = inventory.get("unmatchable")
    if type(unmatchable) is int and unmatchable > 0:
        return False, "component_screening_incomplete"
    if component_id:
        if not targets:
            return False, "component_not_in_inventory"
        for target in targets:
            if _component_sources_screenable(target):
                return True, "component_screened"
        return False, "component_source_coverage_incomplete"
    matchable = inventory.get("matchable")
    if type(matchable) is not int or matchable <= 0:
        return False, "component_not_screened"
    if not any(
        _component_sources_screenable(item)
        for item in listings
        if isinstance(item, dict)
    ):
        return False, "component_source_coverage_incomplete"
    return True, "component_screened"


def _component_sources_screenable(component: dict[str, Any]) -> bool:
    sources = component.get("sources") or component.get("source_matrix")
    if not isinstance(sources, dict) or not sources:
        return False
    for key in DATABASE_SOURCES:
        entry = sources.get(key)
        if isinstance(entry, dict) and type(entry.get("screenable")) is int:
            if entry["screenable"] > 0:
                return True
    return False


def _inventory_from_payload(
    payload: dict[str, Any], schema_id: str
) -> dict[str, Any] | None:
    if schema_id == SCHEMA_VULNSCAN_V1:
        inventory = payload.get("inventory")
        return inventory if isinstance(inventory, dict) else None
    nested = payload.get("vuln_scan")
    if isinstance(nested, dict) and isinstance(nested.get("inventory"), dict):
        return nested["inventory"]
    inventory = payload.get("inventory")
    return inventory if isinstance(inventory, dict) else None


def _findings_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings = payload.get("findings")
    if isinstance(findings, list):
        return [item for item in findings if isinstance(item, dict)]
    nested = payload.get("vuln_scan")
    if isinstance(nested, dict) and isinstance(nested.get("findings"), list):
        return [item for item in nested["findings"] if isinstance(item, dict)]
    return []


def _advisory_absent_from_findings(
    payload: dict[str, Any], advisory_id: str | None
) -> bool:
    if not advisory_id:
        return False
    needle = advisory_id.strip().lower()
    for item in _findings_from_payload(payload):
        candidates = [item.get("id"), *(item.get("aliases") or [])]
        if needle in {str(value).strip().lower() for value in candidates if value}:
            return False
        pkg = str(item.get("pkg") or "").strip().lower()
        if pkg and pkg == needle:
            return False
    return True


def _verdict_from_cra(payload: dict[str, Any], schema_id: str) -> str:
    if schema_id in SCREENING_SCHEMAS:
        verdict = payload.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            return verdict.strip().lower()
        nested = payload.get("vuln_scan")
        if isinstance(nested, dict) and isinstance(nested.get("verdict"), str):
            return nested["verdict"].strip().lower()
        status = str(payload.get("status") or "").strip().lower()
        if status:
            return status
    verdict = payload.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        return verdict.strip().lower()
    return "unknown"


def _platform_approvals(rows: Sequence[Any] | None) -> list[Any] | None:
    if rows is None:
        return []
    out: list[Any] = []
    for row in rows:
        if isinstance(row, PlatformApproval):
            out.append(row)
            continue
        status = str(getattr(row, "status", "") or "")
        out.append(
            PlatformApproval(
                id=str(getattr(row, "id", "")),
                status=status,
                tool_name=str(getattr(row, "tool_name", "") or ""),
                operation=getattr(row, "operation", None),
            )
        )
    return out


def _schema_id(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("schema", "schema_id", "$schema"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _evidence_max_bytes() -> int:
    return int(
        getattr(settings, "flow_evidence_max_bytes", None)
        or settings.workspace_snapshot_max_bytes
    )


def _hex40(value: str | None) -> bool:
    return bool(value) and HEX40.fullmatch(value or "") is not None


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
