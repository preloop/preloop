"""Isolated security-maintenance authority tests against real CRUD records."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import secrets
import tarfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_security_maintenance, flow_artifact
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.security_maintenance import (
    DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
    dispatch_job_is_claimable,
    format_dispatch_claimed_at,
    item_identity_key,
)
from preloop.schemas.security_maintenance import (
    ApprovalDecisionRequest,
    BaselineAcceptRequest,
    RebuiltInputsRequest,
    ResumeRequest,
    ScanFinding,
    ScanIngestRequest,
    SupportedReleaseCreate,
)
from preloop.services.approval_service import ApprovalService
from preloop.services.flow_artifacts import manifest_digest, validate_archive
from preloop.services.flow_trigger_service import FlowDispatchError, FlowTriggerService
from preloop.services.security_maintenance import (
    SecurityMaintenanceService,
    _input_digest,
    _sbom_bytes_digest,
)
from preloop.services.security_maintenance_refs import (
    InvalidTransitionError,
    UnsupportedReleaseError,
    audit_acceptance,
    evidence_ref_from_execution,
    publication_ref_from_execution,
    tests_passed,
)
from preloop.utils.encryption import _get_fernet
from preloop.utils.verification_selection import (
    VERIFICATION_PRODUCER,
    VERIFIER_VERSION,
)

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

SHA = "a" * 40
TREE = "b" * 40
REPO_URL = "https://github.com/example/project.git"
PR_URL = "https://github.com/example/project/pull/7"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cra"


def _sbom_b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


SBOM_B64 = _sbom_b64({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []})
FRESH_SBOM_B64 = base64.b64encode(
    b'{"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [{"name": "libexample"}]}'
).decode()
REMOVED_SBOM_B64 = base64.b64encode(
    json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {
                    "name": "other-lib",
                    "version": "1.0.0",
                    "purl": "pkg:generic/other-lib@1.0.0",
                }
            ],
        }
    ).encode()
).decode()
VERSIONED_SBOM_B64 = base64.b64encode(
    json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {
                    "name": "libexample",
                    "version": "2.0.0",
                    "purl": "pkg:generic/libexample@2.0.0",
                }
            ],
        }
    ).encode()
).decode()
SPDX_REMOVED_B64 = base64.b64encode(
    json.dumps(
        {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "example-image",
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-other",
                    "name": "other-lib",
                    "versionInfo": "1.0.0",
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceType": "purl",
                            "referenceLocator": "pkg:generic/other-lib@1.0.0",
                        }
                    ],
                }
            ],
        }
    ).encode()
).decode()
MALFORMED_SBOM_B64 = base64.b64encode(b"not-json").decode()
AMBIGUOUS_SBOM_B64 = base64.b64encode(
    b'{"bomFormat": "CycloneDX", "spdxVersion": "SPDX-2.3"}'
).decode()
INCOMPLETE_SBOM_B64 = base64.b64encode(
    b'{"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [{}]}'
).decode()
UNSUPPORTED_SBOM_B64 = base64.b64encode(b'{"hello": 1}').decode()
OMITTED_INVENTORY_SBOM_B64 = _sbom_b64({"bomFormat": "CycloneDX", "specVersion": "1.5"})
NULL_INVENTORY_SBOM_B64 = _sbom_b64(
    {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": None}
)
UNSUPPORTED_VERSION_SBOM_B64 = _sbom_b64(
    {"bomFormat": "CycloneDX", "specVersion": "9.9", "components": []}
)
ARBITRARY_SPDX_VERSION_B64 = _sbom_b64({"spdxVersion": "not-a-spec", "packages": []})
MALFORMED_NESTING_SBOM_B64 = _sbom_b64(
    {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [{"name": "other-lib", "components": None}],
    }
)
EMPTY_CDX_B64 = _sbom_b64(
    {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000001",
        "components": [],
    }
)
EMPTY_SPDX_B64 = _sbom_b64(
    {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "example-image",
        "packages": [],
    }
)
OMITTED_SPDX_PACKAGES_B64 = _sbom_b64(
    {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "example-image",
    }
)
NULL_SPDX_PACKAGES_B64 = _sbom_b64(
    {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "example-image",
        "packages": None,
    }
)

PROFILE = {
    "profile_id": "maintenance-tests",
    "version": "v1",
    "always": [
        {
            "id": "unit",
            "command": "true",
            "reason": "unit checks",
            "scope": "unknown",
        }
    ],
    "rules": [],
}


def _verification(*, commit: str = SHA, observe_only: bool = False) -> dict:
    payload = {
        "producer": VERIFICATION_PRODUCER,
        "verifier_version": VERIFIER_VERSION,
        "profile_id": "maintenance-tests",
        "profile_version": "v1",
        "commit_sha": commit,
        "tree_hash": TREE,
        "clean_tree": True,
        "status": "passed",
        "checks": [{"id": "unit", "command": "true", "exit_code": 0}],
        "changed_files": [],
    }
    if observe_only:
        payload["source"] = "sandbox_log"
        payload["authenticated"] = False
    return payload


def _receipt(execution_id, *, sha: str = SHA) -> dict:
    return {
        "url": PR_URL,
        "number": 7,
        "branch": "preloop/cve-2024-0001",
        "provider": "github",
        "head_sha": sha,
        "repository_url": REPO_URL,
        "base": "main",
        "records": [{"execution_id": str(execution_id), "head_sha": sha}],
    }


def _isolated_git(project_id, tracker_id) -> dict:
    return {
        "enabled": True,
        "create_pull_request": True,
        "publication_mode": "isolated",
        "verification": {"mode": "gate", "profile": PROFILE},
        "repositories": [
            {"project_id": str(project_id), "tracker_id": str(tracker_id)}
        ],
    }


def _audit_git(project_id, tracker_id) -> dict:
    return {
        "enabled": True,
        "create_pull_request": False,
        "repositories": [
            {
                "project_id": str(project_id),
                "tracker_id": str(tracker_id),
                "repository_url": REPO_URL,
                "clone_path": "workspace-1",
            }
        ],
    }


def _world(db_session: Session, test_user: models.User):
    tracker = CRUDBase(models.Tracker).create(
        db_session,
        obj_in={
            "name": "Example tracker",
            "tracker_type": "github",
            "account_id": test_user.account_id,
            "api_key": "fake-local-only",
        },
    )
    org = CRUDBase(models.Organization).create(
        db_session,
        obj_in={
            "name": "example",
            "identifier": "example",
            "tracker_id": tracker.id,
        },
    )
    project = CRUDBase(models.Project).create(
        db_session,
        obj_in={
            "name": "project",
            "identifier": "example/project",
            "organization_id": org.id,
        },
    )
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="security-maintenance",
        approval_type="manual",
        approver_user_ids=[test_user.id],
        timeout_seconds=86400,
        approvals_required=1,
    )
    db_session.add(workflow)
    db_session.flush()
    issue = CRUDBase(models.Issue).create(
        db_session,
        obj_in={
            "title": "CVE-2024-0001 on example-widget 1.2",
            "description": "Remediate CVE-2024-0001 in libexample for release 1.2.",
            "status": "open",
            "issue_type": "vulnerability",
            "external_id": "42",
            "external_url": "https://github.com/example/project/issues/42",
            "project_id": project.id,
            "tracker_id": tracker.id,
            "key": "example#42",
        },
    )
    implementer = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": "Implement",
            "account_id": test_user.account_id,
            "agent_type": "codex",
            "agent_config": {},
            "prompt_template": "Implement the advisory. Do not push.",
            "is_enabled": True,
            "git_clone_config": _isolated_git(project.id, tracker.id),
        },
    )
    audit = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": "Audit",
            "account_id": test_user.account_id,
            "agent_type": "codex",
            "agent_config": {},
            "prompt_template": (
                "Required shape (preloop.cra.vulnscan/v1): "
                '{"schema": "preloop.cra.vulnscan/v1"}'
            ),
            "is_enabled": True,
            "git_clone_config": _audit_git(project.id, tracker.id),
        },
    )
    service = SecurityMaintenanceService(db_session, account_id=test_user.account_id)
    return service, project, workflow, implementer, audit, tracker, issue


def _issue(world) -> models.Issue:
    return world[6]


def _scan(**kwargs):
    findings = kwargs.pop(
        "findings",
        [ScanFinding(advisory_id="CVE-2024-0001", component_id="libexample")],
    )
    payload = {
        "product_key": "example-widget",
        "release_key": "1.2",
        "sbom_content_base64": SBOM_B64,
        "findings": findings,
    }
    payload.update(kwargs)
    return ScanIngestRequest(**payload)


async def _release(world, test_user):
    service, project, workflow, implementer, audit, _tracker, _issue = world
    return await service.create_release(
        SupportedReleaseCreate(
            product_key="example-widget",
            release_key="1.2",
            display_name="Example Widget 1.2",
            project_id=project.id,
            pinned_build_ref="v1.2.3",
            sbom_input_ref="sbom/image.spdx.json",
            audit_flow_id=audit.id,
            implementation_flow_id=implementer.id,
            recheck_flow_id=audit.id,
            approval_workflow_id=workflow.id,
            approval_owner_user_id=test_user.id,
        )
    )


def _archive_members(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _attach_verified_checkout(execution, sha: str) -> None:
    result = dict(execution.result or {})
    result["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
        "repositories": [
            {
                "remote": "https://github.com/example/firmware.git",
                "sha": sha,
                "clone_path": "firmware",
                "role": "code",
                "sha_status": "verified",
            }
        ],
    }
    execution.result = result


def _store_evidence(
    db_session,
    execution,
    *,
    expires=None,
    digest=None,
    sha: str = SHA,
    extra=None,
    include_head: bool = True,
    attach_checkout: bool | None = None,
) -> bytes:
    members = {"result.json": b'{"ok": true}'}
    if include_head:
        members["HEAD.txt"] = f"{sha}\n".encode()
    if extra:
        members.update(extra)
    archive = _archive_members(members)
    now = datetime.now(UTC)
    expires_at = expires or (now + timedelta(hours=1))
    sha256 = digest or hashlib.sha256(archive).hexdigest()
    expanded = validate_archive(
        archive, max_bytes=1_000_000, max_expanded_bytes=2_000_000
    )
    thread_id = str(execution.id)
    execution.trigger_event_details = {
        **(execution.trigger_event_details or {}),
        "_session_thread_id": thread_id,
    }
    previous_status = execution.status
    if previous_status not in {"PENDING", "INITIALIZING", "RUNNING"}:
        # Direct upload happens while the execution is still open.
        execution.status = "RUNNING"
        db_session.flush()
    manifest = {
        "version": 1,
        "kind": "evidence",
        "execution_id": str(execution.id),
        "thread_id": thread_id,
        "sha256": sha256,
        "size_bytes": len(archive),
        "expanded_bytes": expanded,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "metadata": {},
    }
    flow_artifact.store(
        db_session,
        values={
            "account_id": execution.flow.account_id,
            "flow_id": execution.flow_id,
            "thread_id": thread_id,
            "execution_id": execution.id,
            "kind": "evidence",
            "manifest": manifest,
            "manifest_sha256": manifest_digest(manifest),
            "ciphertext": _get_fernet().encrypt(archive),
            "availability": "available",
            "expires_at": expires_at,
        },
        quota_bytes=50_000_000,
    )
    if previous_status not in {"PENDING", "INITIALIZING", "RUNNING"}:
        execution.status = previous_status
        db_session.flush()
    if attach_checkout is None:
        attach_checkout = include_head
    if attach_checkout:
        _attach_verified_checkout(execution, sha)
    return archive


def _execution(db_session, flow, *, status="SUCCEEDED", result=None, details=None):
    execution = models.FlowExecution(
        flow_id=flow.id,
        status=status,
        result=result or {},
        trigger_event_details=details or {"_session_thread_id": "thread-1"},
    )
    db_session.add(execution)
    db_session.flush()
    execution.flow = flow
    return execution


def _screened_vulnscan(*, advisory="CVE-2024-0001", present=False):
    payload = copy.deepcopy(json.loads((FIXTURES / "result-vulnscan.json").read_text()))
    payload["inventory"]["components"] = 2
    payload["inventory"]["matchable"] = 2
    payload["inventory"]["unmatchable"] = 0
    payload["inventory"]["source_matrix"]["osv_purl"]["screenable"] = 2
    payload["inventory"]["source_matrix"]["osv_purl"]["blind"] = 0
    payload["inventory"]["source_matrix"]["screened_by_no_source"] = 0
    if present:
        payload["findings"][0]["id"] = advisory
        payload["counts_by_severity"]["high"] = 1
    else:
        payload["findings"] = []
        payload["counts_by_severity"]["high"] = 0
    payload["inventory"]["components_list"] = [
        {
            "id": "libexample",
            "purl": "pkg:generic/libexample@1.4.2",
            "name": "libexample",
            "sources": {"osv_purl": {"kind": "database", "screenable": 1, "blind": 0}},
        },
        {
            "id": "pkg:generic/other@1",
            "name": "other",
            "sources": {"osv_purl": {"kind": "database", "screenable": 1, "blind": 0}},
        },
    ]
    return payload


@pytest.fixture
def world(db_session, test_user):
    return _world(db_session, test_user)


@pytest.fixture(autouse=True)
def _quiet_approval_side_effects():
    with (
        patch.object(ApprovalService, "send_notifications", new_callable=AsyncMock),
        patch.object(
            ApprovalService, "_broadcast_approval_update", new_callable=AsyncMock
        ),
    ):
        yield


class TestEvidenceAuthority:
    def test_caller_available_and_digest_do_not_grant(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer)
        claimed = evidence_ref_from_execution(
            execution,
            {"available": True, "digest": "a" * 64, "artifact_id": str(uuid4())},
            db=db_session,
            account_id=implementer.account_id,
        )
        assert claimed.available is False
        assert claimed.reason != "explicit_receipt"
        assert claimed.reason != "caller_declared_available"

    def test_forged_digest_and_wrong_account_fail(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer)
        _store_evidence(db_session, execution)
        forged = evidence_ref_from_execution(
            execution,
            {"digest": "0" * 64},
            db=db_session,
            account_id=test_user.account_id,
        )
        assert forged.available is False
        assert forged.reason == "evidence_digest_mismatch"
        foreign = evidence_ref_from_execution(
            execution, db=db_session, account_id=uuid4()
        )
        assert foreign.available is False

    def test_wrong_execution_and_expired_artifact_fail(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        owned = _execution(db_session, implementer)
        other = _execution(db_session, implementer, details={"_session_thread_id": "x"})
        _store_evidence(db_session, owned)
        missing = evidence_ref_from_execution(
            other, db=db_session, account_id=implementer.account_id
        )
        assert missing.available is False
        expired_exec = _execution(
            db_session, implementer, details={"_session_thread_id": "expired"}
        )
        _store_evidence(
            db_session,
            expired_exec,
            expires=datetime.now(UTC) - timedelta(hours=1),
        )
        expired = evidence_ref_from_execution(
            expired_exec, db=db_session, account_id=implementer.account_id
        )
        assert expired.available is False
        assert "expired" in expired.reason

    def test_claimed_missing_denies_and_valid_archive_grants(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer)
        denied = evidence_ref_from_execution(
            execution,
            {"available": False, "reason": "operator_declared_missing"},
            db=db_session,
            account_id=test_user.account_id,
        )
        assert denied.available is False
        _store_evidence(db_session, execution)
        ok = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        assert ok.available is True
        assert ok.digest


class TestTestsAndPublication:
    def test_succeeded_without_verification_fails_closed(
        self, db_session, world
    ) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer, result={"status": "success"})
        publication = publication_ref_from_execution(execution, flow=implementer)
        passed, reason = tests_passed(
            execution, flow=implementer, publication=publication
        )
        assert passed is False
        assert reason == "trusted_verification_missing"

    def test_observe_only_and_agent_publication_fail(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(
            db_session,
            implementer,
            result={
                "verification": _verification(observe_only=True),
                "trusted_publication": {
                    "sha": SHA,
                    "url": "https://evil.example/pr",
                },
            },
        )
        publication = publication_ref_from_execution(execution, flow=implementer)
        assert publication.available is False
        passed, reason = tests_passed(
            execution, flow=implementer, publication=publication
        )
        assert passed is False
        assert reason in {
            "observe_only_verification",
            "trusted_verification_missing",
            "publication_head_sha_missing",
        }

    def test_controller_receipt_and_matching_verification_pass(
        self, db_session, world
    ) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer)
        receipt = _receipt(execution.id)
        execution.result = {
            "_private_publication": {"phase": "complete", "receipt": receipt},
            "trusted_publication": receipt,
            "verification": _verification(),
        }
        db_session.flush()
        publication = publication_ref_from_execution(execution, flow=implementer)
        assert publication.available is True
        assert publication.sha == SHA
        passed, reason = tests_passed(
            execution, flow=implementer, publication=publication
        )
        assert passed is True
        assert reason == "tests_passed"

    def test_stale_verification_commit_is_blocked(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker, *_ = world
        execution = _execution(db_session, implementer)
        receipt = _receipt(execution.id)
        execution.result = {
            "_private_publication": {"phase": "complete", "receipt": receipt},
            "trusted_publication": receipt,
            "verification": _verification(commit="c" * 40),
        }
        db_session.flush()
        publication = publication_ref_from_execution(execution, flow=implementer)
        passed, reason = tests_passed(
            execution, flow=implementer, publication=publication
        )
        assert passed is False
        assert "another commit" in reason or reason != "tests_passed"


class TestAuditAcceptance:
    def test_missing_checkout_and_agent_fields_cannot_prove_repair(
        self, db_session, world, test_user
    ) -> None:
        service, _project, _workflow, _implementer, audit, _tracker, *_ = world
        execution = _execution(
            db_session,
            audit,
            result={
                "schema": "preloop.cra.vulnscan/v1",
                "finding_absent": True,
                "checked_out_sha": SHA,
                "revision": SHA,
            },
        )
        evidence = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        # Evidence is missing here on purpose: absence cannot grant.
        denied = audit_acceptance(
            execution.result,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
        )
        assert denied.accepted is False
        _store_evidence(db_session, execution, include_head=False)
        evidence = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        missing_sha = audit_acceptance(
            execution.result,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert missing_sha.accepted is False
        assert missing_sha.reason == "missing_checked_out_sha"

    def test_unknown_cra_schema_and_blind_scan_are_rejected(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, _implementer, audit, _tracker, *_ = world
        execution = _execution(
            db_session,
            audit,
            details={"payload": {"sha": SHA}, "_session_thread_id": "thread-1"},
            result={"schema": "preloop.cra.unknown/v1", "verdict": "pass"},
        )
        _store_evidence(db_session, execution)
        evidence = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        unknown = audit_acceptance(
            execution.result,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert unknown.accepted is False
        assert unknown.reason == "unsupported_cra_schema"
        payload = _screened_vulnscan(present=False)
        payload["inventory"]["unmatchable"] = 2
        payload["inventory"]["matchable"] = 0
        payload["inventory"]["source_matrix"]["osv_purl"]["screenable"] = 0
        payload["inventory"]["source_matrix"]["osv_purl"]["blind"] = 2
        execution.result = payload
        db_session.flush()
        blind = audit_acceptance(
            payload,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert blind.accepted is False

    def test_screened_absence_accepts_when_validator_allows(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, _implementer, audit, _tracker, *_ = world
        payload = _screened_vulnscan(present=False)
        execution = _execution(
            db_session,
            audit,
            details={"payload": {"sha": SHA}, "_session_thread_id": "thread-1"},
            result=payload,
        )
        _store_evidence(db_session, execution)
        evidence = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        accepted = audit_acceptance(
            payload,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert accepted.accepted is True
        assert accepted.finding_verified_absent is True


class TestDurableLifecycle:
    @pytest.mark.asyncio
    async def test_unsupported_release_and_finding_absent_do_not_resolve(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        with pytest.raises(UnsupportedReleaseError):
            await service.ingest_scan(
                _scan(
                    product_key="missing",
                    release_key="0",
                    issue_id=_issue(world).id,
                )
            )
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            first = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            second = await service.ingest_scan(
                _scan(
                    issue_id=_issue(world).id,
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001",
                            component_id="libexample",
                            present=False,
                        )
                    ],
                )
            )
        assert len(first["items"]) == 1
        assert first["items"][0]["id"] == second["items"][0]["id"]
        assert second["items"][0]["state"] != "resolved"
        identity = item_identity_key(
            test_user.account_id,
            "example-widget",
            "1.2",
            "CVE-2024-0001",
            "libexample",
        )
        items = crud_security_maintenance.list_items(
            db_session, account_id=test_user.account_id
        )
        assert len(items) == 1
        assert items[0].identity_key == identity
        history = service.list_decisions(items[0].id)
        assert any(row["outcome"] == "finding_absent_unverified" for row in history)

    @pytest.mark.asyncio
    async def test_failed_tests_missing_evidence_stale_and_denied_approval(
        self, db_session, world, test_user
    ) -> None:
        service, _project, _workflow, implementer, audit, _tracker, *_ = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        item_id = ingested["items"][0]["id"]
        item = crud_security_maintenance.get_item(
            db_session, account_id=test_user.account_id, item_id=item_id
        )
        impl = crud_security_maintenance.get_execution(
            db_session,
            account_id=test_user.account_id,
            execution_id=item.implementation_execution_id,
        )
        impl.status = "SUCCEEDED"
        impl.result = {"status": "success"}
        db_session.flush()
        await service.finish_execution(impl)
        db_session.refresh(item)
        assert item.state == "tests_failed"
        impl.result = {
            "_private_publication": {
                "phase": "complete",
                "receipt": _receipt(impl.id),
            },
            "trusted_publication": _receipt(impl.id),
            "verification": _verification(),
        }
        db_session.flush()
        await service.finish_execution(impl)
        db_session.refresh(item)
        assert item.state == "approval_pending"
        stale = _execution(
            db_session,
            implementer,
            details=impl.trigger_event_details,
        )
        await service.finish_execution(stale)
        db_session.refresh(item)
        assert item.state == "approval_pending"
        assert any(
            row["outcome"] == "stale_completion"
            for row in service.list_decisions(item.id)
        )
        denied = await service.decide_approval(
            item.id,
            ApprovalDecisionRequest(reason="Hold for human review"),
            actor_user_id=test_user.id,
            approved=False,
        )
        assert denied["state"] == "held"
        request = crud_security_maintenance.get_approval_request(
            db_session,
            account_id=test_user.account_id,
            request_id=item.approval_request_id,
        )
        assert request.status == "declined"
        history = service.list_decisions(item.id)
        assert [row["outcome"] for row in history if row["kind"] == "approval"]

    @pytest.mark.asyncio
    async def test_happy_path_to_new_baseline(
        self, db_session, world, test_user
    ) -> None:
        service, _project, _workflow, implementer, audit, _tracker, *_ = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            item = crud_security_maintenance.get_item(
                db_session,
                account_id=test_user.account_id,
                item_id=ingested["items"][0]["id"],
            )
            impl = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.implementation_execution_id,
            )
            impl.status = "SUCCEEDED"
            impl.result = {
                "_private_publication": {
                    "phase": "complete",
                    "receipt": _receipt(impl.id),
                },
                "trusted_publication": _receipt(impl.id),
                "verification": _verification(),
            }
            db_session.flush()
            await service.finish_execution(impl)
            approved = await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="Ship the supported-release patch"),
                actor_user_id=test_user.id,
                approved=True,
            )
            assert approved["state"] == "awaiting_build"
            with pytest.raises(InvalidTransitionError, match="stale_sbom_reused"):
                await _submit_rebuild(service, item, test_user, sbom=SBOM_B64)
            rebuilt = await _submit_rebuild(service, item, test_user)
            assert rebuilt["state"] in {"reaudit_pending", "reauditing"}
            db_session.refresh(item)
            recheck = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.recheck_execution_id,
            )
            recheck.status = "SUCCEEDED"
            details = dict(recheck.trigger_event_details or {})
            details["_session_thread_id"] = str(recheck.id)
            payload = dict(details.get("payload") or {})
            assert payload.get("sha") == SHA
            files = payload.get("workspace_files") or []
            assert any(entry.get("content_base64") == FRESH_SBOM_B64 for entry in files)
            assert all(entry.get("content_base64") != SBOM_B64 for entry in files)
            envelope = payload.get("security_maintenance") or {}
            assert envelope.get("sbom_input_ref") == "sbom/image.spdx.json"
            assert envelope.get("pinned_build_ref") == SHA
            details["payload"] = payload
            recheck.trigger_event_details = details
            recheck.result = _screened_vulnscan(present=False)
            _store_evidence(db_session, recheck)
            await service.finish_execution(recheck)
        db_session.refresh(item)
        assert item.state == "resolved"
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=item.release_id
        )
        assert release.accepted_baseline_id is not None

    @pytest.mark.asyncio
    async def test_expired_approval_escalates_and_resume_preserves_history(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            item = crud_security_maintenance.get_item(
                db_session,
                account_id=test_user.account_id,
                item_id=ingested["items"][0]["id"],
            )
            impl = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.implementation_execution_id,
            )
            impl.status = "SUCCEEDED"
            impl.result = {
                "_private_publication": {
                    "phase": "complete",
                    "receipt": _receipt(impl.id),
                },
                "trusted_publication": _receipt(impl.id),
                "verification": _verification(),
            }
            db_session.flush()
            await service.finish_execution(impl)
            request = crud_security_maintenance.get_approval_request(
                db_session,
                account_id=test_user.account_id,
                request_id=item.approval_request_id,
            )
            request.expires_at = datetime.utcnow() - timedelta(hours=1)
            db_session.flush()
            expired = await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="deadline passed"),
                actor_user_id=test_user.id,
                approved=True,
            )
            assert expired["state"] == "escalated"
            before = service.list_decisions(item.id)
            resumed = await service.resume(
                item.id,
                ResumeRequest(reason="Retry after human review"),
                actor_user_id=test_user.id,
            )
            assert resumed["state"] in {
                "remediation_pending",
                "remediating",
                "reauditing",
            }
            after = service.list_decisions(item.id)
            assert len(after) > len(before)


async def _impl_to_approval(service, world, db_session, test_user):
    ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
    item = crud_security_maintenance.get_item(
        db_session, account_id=test_user.account_id, item_id=ingested["items"][0]["id"]
    )
    impl = crud_security_maintenance.get_execution(
        db_session,
        account_id=test_user.account_id,
        execution_id=item.implementation_execution_id,
    )
    impl.status = "SUCCEEDED"
    impl.result = {
        "_private_publication": {"phase": "complete", "receipt": _receipt(impl.id)},
        "trusted_publication": _receipt(impl.id),
        "verification": _verification(),
    }
    db_session.flush()
    await service.finish_execution(impl)
    db_session.refresh(item)
    return item, impl


async def _submit_rebuild(service, item, test_user, *, sbom: str | None = None):
    return await service.submit_rebuilt_inputs(
        item.id,
        RebuiltInputsRequest(
            published_sha=SHA, sbom_content_base64=sbom or FRESH_SBOM_B64
        ),
        actor_user_id=test_user.id,
    )


def _omitted_target_vulnscan() -> dict:
    payload = _screened_vulnscan(present=False)
    payload["inventory"]["components_list"] = [
        {
            "id": "other-product",
            "name": "other-product",
            "sources": {"osv_purl": {"kind": "database", "screenable": 1, "blind": 0}},
        }
    ]
    return payload


async def _to_awaiting_build(service, world, db_session, test_user):
    item, _impl = await _impl_to_approval(service, world, db_session, test_user)
    approved = await service.decide_approval(
        item.id,
        ApprovalDecisionRequest(reason="Ship the supported-release patch"),
        actor_user_id=test_user.id,
        approved=True,
    )
    assert approved["state"] == "awaiting_build"
    db_session.refresh(item)
    return item


async def _complete_recheck(service, db_session, test_user, item, result):
    db_session.refresh(item)
    recheck = crud_security_maintenance.get_execution(
        db_session,
        account_id=test_user.account_id,
        execution_id=item.recheck_execution_id,
    )
    recheck.status = "SUCCEEDED"
    details = dict(recheck.trigger_event_details or {})
    details["_session_thread_id"] = str(recheck.id)
    recheck.trigger_event_details = details
    recheck.result = result
    _store_evidence(db_session, recheck)
    await service.finish_execution(recheck)
    db_session.refresh(item)
    return item


class TestDispatchAndApprovals:
    @pytest.mark.asyncio
    async def test_dispatched_payload_uses_runner_entry(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        start = AsyncMock()

        async def passthrough(_execution_id, local):
            await local()

        with (
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                side_effect=passthrough,
            ),
            patch.object(FlowTriggerService, "_start_flow_execution", start),
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        start.assert_awaited()
        _flow, event, _nats = start.call_args.args
        payload = event["payload"]
        assert payload["object_attributes"]["number"] == 42
        assert payload["repository"]["full_name"] == "example/project"
        assert payload["workspace_files"]
        assert payload["workspace_files"][0]["path"] == "sbom/image.spdx.json"
        envelope = payload["security_maintenance"]
        assert envelope["sbom_input_ref"] == "sbom/image.spdx.json"
        assert envelope["pinned_build_ref"] == "v1.2.3"
        assert envelope["input_digest"]
        item = crud_security_maintenance.get_item(
            db_session,
            account_id=test_user.account_id,
            item_id=ingested["items"][0]["id"],
        )
        precreated = start.call_args.kwargs["precreated_execution"]
        assert precreated.id == item.implementation_execution_id

    @pytest.mark.asyncio
    async def test_second_session_sees_reserved_execution(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        execution_id = ingested["items"][0]["implementation_execution_id"]
        other = Session(bind=db_session.bind)
        try:
            found = crud_security_maintenance.get_execution(
                other, account_id=test_user.account_id, execution_id=execution_id
            )
            assert found is not None
            assert found.status == "PENDING"
        finally:
            other.close()

    @pytest.mark.asyncio
    async def test_duplicate_scan_keeps_one_execution(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            first = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            second = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        assert (
            first["items"][0]["implementation_execution_id"]
            == second["items"][0]["implementation_execution_id"]
        )
        executions = [
            row
            for row in db_session.query(models.FlowExecution).all()
            if row.trigger_event_details
            and (row.trigger_event_details.get("payload") or {}).get(
                "security_maintenance"
            )
        ]
        assert len(executions) == 1

    @pytest.mark.asyncio
    async def test_enqueue_failure_retries_without_second_execution(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=FlowDispatchError(
                "00000000-0000-0000-0000-000000000001",
                "PENDING",
                RuntimeError("broker_unavailable"),
            ),
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        item = crud_security_maintenance.get_item(
            db_session,
            account_id=test_user.account_id,
            item_id=ingested["items"][0]["id"],
        )
        assert item.state == "remediation_pending"
        first_id = item.implementation_execution_id
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as retry:
            await service.reconcile_item(item.id)
        retry.assert_awaited()
        db_session.refresh(item)
        assert item.implementation_execution_id == first_id

    @pytest.mark.asyncio
    async def test_console_approval_advances_without_bespoke_call(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            row = crud_security_maintenance.get_approval_request(
                db_session,
                account_id=test_user.account_id,
                request_id=item.approval_request_id,
            )
            updated = await service._approval_service().approve_request(
                row.id,
                "console ship",
                user_id=test_user.id,
                channel="console",
            )
            advanced = await service.reconcile_platform_approval(updated.id)
        assert advanced is not None
        assert advanced["state"] == "awaiting_build"

    @pytest.mark.asyncio
    async def test_quorum_and_non_approver(self, db_session, world, test_user) -> None:
        service, _project, workflow, *_rest = world
        workflow.approvals_required = 2
        other = models.User(
            account_id=test_user.account_id,
            email="approver2@example.com",
            username="approver2",
            full_name="Approver Two",
            is_active=True,
            hashed_password="x",
            user_source="local",
        )
        db_session.add(other)
        db_session.flush()
        workflow.approver_user_ids = [test_user.id, other.id]
        db_session.flush()
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            stranger = models.User(
                account_id=test_user.account_id,
                email="stranger@example.com",
                username="stranger",
                full_name="Stranger",
                is_active=True,
                hashed_password="x",
                user_source="local",
            )
            db_session.add(stranger)
            db_session.flush()
            with pytest.raises(InvalidTransitionError):
                await service.decide_approval(
                    item.id,
                    ApprovalDecisionRequest(reason="not eligible"),
                    actor_user_id=stranger.id,
                    approved=True,
                )
            pending = await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="first vote"),
                actor_user_id=test_user.id,
                approved=True,
            )
            assert pending["state"] == "approval_pending"
            done = await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="second vote"),
                actor_user_id=other.id,
                approved=True,
            )
            assert done["state"] == "awaiting_build"

    @pytest.mark.asyncio
    async def test_stale_recheck_cannot_regress_newer_baseline(
        self, db_session, world, test_user
    ) -> None:
        service, _project, _workflow, _implementer, audit, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="ship"),
                actor_user_id=test_user.id,
                approved=True,
            )
            db_session.refresh(item)
            await _submit_rebuild(service, item, test_user)
            db_session.refresh(item)
            recheck = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.recheck_execution_id,
            )
            newer = _execution(
                db_session,
                audit,
                details={"payload": {"sha": SHA}, "_session_thread_id": "newer"},
                result=_screened_vulnscan(present=False),
            )
            _store_evidence(db_session, newer)
            newer.created_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                hours=1
            )
            db_session.flush()
            release = crud_security_maintenance.get_release(
                db_session, account_id=test_user.account_id, release_id=item.release_id
            )
            baseline = crud_security_maintenance.create_baseline(
                db_session,
                account_id=test_user.account_id,
                fields={
                    "release_id": release.id,
                    "audit_execution_id": newer.id,
                    "result_digest": "d" * 64,
                    "verdict": "pass",
                    "evidence_ref": {},
                    "data": {},
                },
            )
            crud_security_maintenance.set_accepted_baseline(
                db_session,
                account_id=test_user.account_id,
                release_id=release.id,
                baseline_id=baseline.id,
            )
            recheck.status = "SUCCEEDED"
            details = dict(recheck.trigger_event_details or {})
            details["_session_thread_id"] = str(recheck.id)
            recheck.trigger_event_details = details
            recheck.result = _screened_vulnscan(present=False)
            _store_evidence(db_session, recheck)
            recheck.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                hours=1
            )
            db_session.flush()
            await service.finish_execution(recheck)
        db_session.refresh(item)
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=item.release_id
        )
        assert release.accepted_baseline_id == baseline.id
        assert item.state != "resolved"
        assert any(
            row["outcome"] == "stale_completion"
            for row in service.list_decisions(item.id)
        )


class TestHttpRbac:
    def test_managed_and_cross_item_credentials_are_denied(
        self, db_session, test_user, world
    ) -> None:
        from fastapi import HTTPException

        from preloop.api.endpoints.security_maintenance import (
            _reject_managed_credentials,
        )

        _service, _project, _workflow, implementer, *_rest = world
        owned = _execution(db_session, implementer)
        other = _execution(db_session, implementer)
        _key, _token = crud_api_key.create_runtime_key(
            db_session,
            name="execution-key",
            account_id=test_user.account_id,
            user_id=test_user.id,
            context_data={"flow_execution_id": str(other.id)},
            commit=False,
        )
        test_user._auth_api_key = _key
        with pytest.raises(HTTPException) as exc:
            _reject_managed_credentials(test_user)
        assert exc.value.status_code == 403
        agent_key, _agent_token = crud_api_key.create_runtime_key(
            db_session,
            name="agent-key",
            account_id=test_user.account_id,
            user_id=test_user.id,
            context_data={"managed_agent_id": str(uuid4())},
            commit=False,
        )
        test_user._auth_api_key = agent_key
        with pytest.raises(HTTPException):
            _reject_managed_credentials(test_user)
        delattr(test_user, "_auth_api_key")
        _reject_managed_credentials(test_user)
        assert owned.id != other.id

    def test_http_routes_with_rbac_and_real_credentials(
        self, db_session, test_user, world, monkeypatch
    ) -> None:
        from preloop.api.app import create_app
        from preloop.api.auth import get_current_active_user
        from preloop.api.auth.jwt import create_access_token
        from preloop.config import settings
        from preloop.models.db.session import get_db_session as get_db

        monkeypatch.setenv("DISABLE_RBAC", "false")
        settings.disable_rbac = False
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        with TestClient(app) as client:
            token = create_access_token({"sub": str(test_user.id)})
            denied = client.get(
                "/api/v1/security-maintenance/releases",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert denied.status_code in {200, 403, 401}
            app.dependency_overrides[get_current_active_user] = lambda: test_user
            listed = client.get("/api/v1/security-maintenance/releases")
            assert listed.status_code == 200
            _service, _project, _workflow, implementer, *_rest = world
            execution = _execution(db_session, implementer)
            key, secret = crud_api_key.create_runtime_key(
                db_session,
                name="http-execution",
                account_id=test_user.account_id,
                user_id=test_user.id,
                context_data={"flow_execution_id": str(execution.id)},
                commit=False,
            )
            test_user._auth_api_key = key
            blocked = client.post(
                f"/api/v1/security-maintenance/items/{uuid4()}/approve",
                json={"reason": "agent cannot approve"},
            )
            assert blocked.status_code == 403
        settings.disable_rbac = True
        monkeypatch.setenv("DISABLE_RBAC", "true")


class TestRebuildCheckoutAndSweep:
    @pytest.mark.asyncio
    async def test_create_release_does_not_mutate_shared_workflow(
        self, db_session, world, test_user
    ) -> None:
        service, _project, workflow, *_rest = world
        workflow.timeout_seconds = 1234
        db_session.flush()
        await _release(world, test_user)
        db_session.refresh(workflow)
        assert workflow.timeout_seconds == 1234
        assert workflow.approver_user_ids == [test_user.id]

    @pytest.mark.asyncio
    async def test_failed_audit_execution_is_not_accepted(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        created = await _release(world, test_user)
        audit = world[4]
        execution = _execution(
            db_session,
            audit,
            status="FAILED",
            result=_screened_vulnscan(present=False),
            details={
                "_session_thread_id": "failed-audit",
                "payload": {
                    "pinned_build_ref": "v1.2.3",
                    "sbom_input_ref": "sbom/image.spdx.json",
                    "security_maintenance": {
                        "release_id": created["id"],
                        "pinned_build_ref": "v1.2.3",
                        "sbom_input_ref": "sbom/image.spdx.json",
                    },
                },
            },
        )
        _store_evidence(db_session, execution)
        with pytest.raises(
            InvalidTransitionError, match="audit_execution_not_completed"
        ):
            await service.accept_baseline(
                created["id"],
                BaselineAcceptRequest(audit_execution_id=execution.id),
            )

    @pytest.mark.asyncio
    async def test_get_item_does_not_enqueue(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=FlowDispatchError(
                "00000000-0000-0000-0000-000000000001",
                "PENDING",
                RuntimeError("broker_unavailable"),
            ),
        ) as dispatched:
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            dispatched.reset_mock()
            listed = service.get_item(ingested["items"][0]["id"])
            assert listed["state"] == "remediation_pending"
            dispatched.assert_not_awaited()

    def test_concurrent_sweeps_do_not_share_lock(
        self, db_engine, db_session, test_user
    ) -> None:
        from sqlalchemy.orm import Session

        held = crud_security_maintenance.try_sweep_lock(
            db_session, test_user.account_id
        )
        assert held is not None
        connection = db_engine.connect()
        other = Session(bind=connection)
        try:
            skipped = crud_security_maintenance.try_sweep_lock(
                other, test_user.account_id
            )
            assert skipped is None
        finally:
            crud_security_maintenance.release_sweep_lock(db_session, held)
            other.close()
            connection.close()

    def test_checkout_ignores_payload_sha_and_head_txt(
        self, db_session, world, test_user
    ) -> None:
        from preloop.services.security_maintenance_refs import controller_checkout_sha

        _service, _project, _workflow, _implementer, audit, *_ = world
        missing = _execution(
            db_session,
            audit,
            details={"payload": {"sha": SHA}, "_session_thread_id": "missing-head"},
        )
        _store_evidence(
            db_session,
            missing,
            extra={"result.json": b'{"ok": true}'},
            include_head=True,
            attach_checkout=False,
        )
        assert (
            controller_checkout_sha(
                missing, db=db_session, account_id=test_user.account_id, flow=audit
            )
            is None
        )
        unverified = _execution(db_session, audit)
        _store_evidence(
            db_session, unverified, include_head=True, attach_checkout=False
        )
        result = dict(unverified.result or {})
        result["product_provenance"] = {
            "schema": "preloop.cra.product_provenance/v1",
            "mapping_status": "declared_unverified",
            "repositories": [
                {
                    "remote": "https://github.com/example/firmware.git",
                    "sha": SHA,
                    "clone_path": "firmware",
                    "role": "code",
                    "sha_status": "declared_unverified",
                }
            ],
        }
        unverified.result = result
        assert (
            controller_checkout_sha(
                unverified, db=db_session, account_id=test_user.account_id, flow=audit
            )
            is None
        )
        present = _execution(db_session, audit)
        _store_evidence(db_session, present, sha=SHA)
        assert (
            controller_checkout_sha(
                present, db=db_session, account_id=test_user.account_id, flow=audit
            )
            == SHA
        )

    def test_component_identity_and_partial_coverage(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, _implementer, audit, *_ = world
        execution = _execution(
            db_session, audit, result=_screened_vulnscan(present=False)
        )
        _store_evidence(db_session, execution)
        evidence = evidence_ref_from_execution(
            execution, db=db_session, account_id=test_user.account_id
        )
        other = _screened_vulnscan(present=False)
        other["inventory"]["components_list"] = [
            {
                "id": "other-product",
                "name": "other-product",
                "sources": {
                    "osv_purl": {"kind": "database", "screenable": 1, "blind": 0}
                },
            }
        ]
        rejected = audit_acceptance(
            other,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert rejected.accepted is False
        assert rejected.reason == "component_not_in_inventory"
        missing_list = _screened_vulnscan(present=False)
        missing_list["inventory"].pop("components_list")
        rejected_list = audit_acceptance(
            missing_list,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert rejected_list.reason == "component_identity_missing"
        partial = _screened_vulnscan(present=False)
        partial["inventory"]["components_list"][0]["sources"] = {
            "osv_purl": {"kind": "database", "screenable": 0, "blind": 1}
        }
        rejected_partial = audit_acceptance(
            partial,
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert rejected_partial.reason == "component_source_coverage_incomplete"
        accepted = audit_acceptance(
            _screened_vulnscan(present=False),
            evidence=evidence,
            execution=execution,
            flow=audit,
            candidate_revision=SHA,
            component_id="libexample",
            advisory_id="CVE-2024-0001",
            require_finding_absent=True,
            db=db_session,
            account_id=test_user.account_id,
        )
        assert accepted.accepted is True
        assert accepted.checked_out_sha == SHA

    @pytest.mark.asyncio
    async def test_managed_api_key_cannot_approve_via_console_route(
        self, db_session, world, test_user
    ) -> None:
        from preloop.api.app import create_app
        from preloop.models.db.session import SyncApprovalSession
        from preloop.models.db.session import get_db_session as get_db

        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
        _key, secret = crud_api_key.create_runtime_key(
            db_session,
            name="managed-console",
            account_id=test_user.account_id,
            user_id=test_user.id,
            context_data={"flow_execution_id": str(item.implementation_execution_id)},
            commit=False,
        )

        @asynccontextmanager
        async def _same_session():
            yield SyncApprovalSession(db_session)

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session",
            _same_session,
        ):
            with TestClient(app) as client:
                blocked = client.post(
                    f"/api/v1/approval-requests/{item.approval_request_id}/approve",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={"approved": True, "comment": "managed key must not ship"},
                )
        assert blocked.status_code == 403
        db_session.refresh(item)
        assert item.state in {"tests_passed", "approval_pending"}


class TestInputIntegrity:
    @pytest.mark.asyncio
    async def test_result_omission_does_not_prove_removal(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            await _submit_rebuild(service, item, test_user, sbom=FRESH_SBOM_B64)
            item = await _complete_recheck(
                service, db_session, test_user, item, _omitted_target_vulnscan()
            )
        assert item.state == "reaudit_incomplete"
        reasons = [
            row["data"].get("reason")
            for row in service.list_decisions(item.id)
            if row["kind"] == "recheck" and isinstance(row.get("data"), dict)
        ]
        assert "component_not_in_inventory" in reasons
        assert "component_removed_from_rebuild" not in reasons

    @pytest.mark.asyncio
    async def test_malformed_rebuilt_sbom_is_rejected(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            with pytest.raises(InvalidTransitionError, match="sbom_malformed"):
                await _submit_rebuild(service, item, test_user, sbom=MALFORMED_SBOM_B64)
            with pytest.raises(InvalidTransitionError, match="sbom_ambiguous"):
                await _submit_rebuild(service, item, test_user, sbom=AMBIGUOUS_SBOM_B64)
            with pytest.raises(InvalidTransitionError, match="sbom_incomplete"):
                await _submit_rebuild(
                    service, item, test_user, sbom=INCOMPLETE_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_unsupported"):
                await _submit_rebuild(
                    service, item, test_user, sbom=UNSUPPORTED_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_incomplete"):
                await _submit_rebuild(
                    service, item, test_user, sbom=OMITTED_INVENTORY_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_incomplete"):
                await _submit_rebuild(
                    service, item, test_user, sbom=NULL_INVENTORY_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_unsupported"):
                await _submit_rebuild(
                    service, item, test_user, sbom=UNSUPPORTED_VERSION_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_unsupported"):
                await _submit_rebuild(
                    service, item, test_user, sbom=ARBITRARY_SPDX_VERSION_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_malformed"):
                await _submit_rebuild(
                    service, item, test_user, sbom=MALFORMED_NESTING_SBOM_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_incomplete"):
                await _submit_rebuild(
                    service, item, test_user, sbom=OMITTED_SPDX_PACKAGES_B64
                )
            with pytest.raises(InvalidTransitionError, match="sbom_incomplete"):
                await _submit_rebuild(
                    service, item, test_user, sbom=NULL_SPDX_PACKAGES_B64
                )
            db_session.refresh(item)
            assert item.state == "awaiting_build"
            assert item.recheck_execution_id is None

    @pytest.mark.asyncio
    async def test_legitimate_removal_from_submitted_bytes(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            await _submit_rebuild(service, item, test_user, sbom=REMOVED_SBOM_B64)
            item = await _complete_recheck(
                service, db_session, test_user, item, _omitted_target_vulnscan()
            )
        assert item.state == "resolved"
        resolved = [
            row
            for row in service.list_decisions(item.id)
            if row["kind"] == "recheck" and row["outcome"] == "resolved"
        ]
        assert resolved
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=item.release_id
        )
        assert release.accepted_baseline_id is not None

    @pytest.mark.asyncio
    async def test_spdx_removal_from_submitted_bytes(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            await _submit_rebuild(service, item, test_user, sbom=SPDX_REMOVED_B64)
            item = await _complete_recheck(
                service, db_session, test_user, item, _omitted_target_vulnscan()
            )
        assert item.state == "resolved"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sbom", [EMPTY_CDX_B64, EMPTY_SPDX_B64])
    async def test_explicit_empty_inventory_removes_previously_present_target(
        self, db_session, world, test_user, sbom
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            await _submit_rebuild(service, item, test_user, sbom=sbom)
            item = await _complete_recheck(
                service, db_session, test_user, item, _omitted_target_vulnscan()
            )
        assert item.state == "resolved"

    @pytest.mark.asyncio
    async def test_version_update_is_not_removal(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item = await _to_awaiting_build(service, world, db_session, test_user)
            await _submit_rebuild(service, item, test_user, sbom=VERSIONED_SBOM_B64)
            omitted = await _complete_recheck(
                service, db_session, test_user, item, _omitted_target_vulnscan()
            )
            assert omitted.state == "reaudit_incomplete"
            resumed = await service.resume(
                item.id,
                ResumeRequest(reason="Retry after screening the new version"),
                actor_user_id=test_user.id,
            )
            assert resumed["state"] in {"reaudit_pending", "reauditing"}
            screened = _screened_vulnscan(present=False)
            screened["inventory"]["components_list"][0]["purl"] = (
                "pkg:generic/libexample@2.0.0"
            )
            item = await _complete_recheck(
                service, db_session, test_user, item, screened
            )
        assert item.state == "resolved"

    @pytest.mark.asyncio
    async def test_baseline_requires_exact_release_and_sbom_binding(
        self, db_session, world, test_user
    ) -> None:
        service, project, workflow, implementer, audit, *_ = world
        created = await _release(world, test_user)
        other = await service.create_release(
            SupportedReleaseCreate(
                product_key="other-widget",
                release_key="9.9",
                display_name="Other Widget 9.9",
                project_id=project.id,
                pinned_build_ref="v1.2.3",
                sbom_input_ref="sbom/image.spdx.json",
                audit_flow_id=audit.id,
                implementation_flow_id=implementer.id,
                recheck_flow_id=audit.id,
                approval_workflow_id=workflow.id,
                approval_owner_user_id=test_user.id,
            )
        )
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            scheduled = await service.schedule_baseline_audit(created["id"], SBOM_B64)
        bound = crud_security_maintenance.get_execution(
            db_session,
            account_id=test_user.account_id,
            execution_id=UUID(scheduled["execution_id"]),
        )
        bound.status = "SUCCEEDED"
        bound.result = _screened_vulnscan(present=False)
        _store_evidence(db_session, bound)
        accepted = await service.accept_baseline(
            created["id"],
            BaselineAcceptRequest(audit_execution_id=bound.id),
        )
        assert accepted["id"]

        missing_identity = _execution(
            db_session,
            audit,
            status="SUCCEEDED",
            result=_screened_vulnscan(present=False),
            details={
                "_session_thread_id": "no-release-id",
                "payload": {
                    "pinned_build_ref": "v1.2.3",
                    "sbom_input_ref": "sbom/image.spdx.json",
                    "workspace_files": [
                        {
                            "path": "sbom/image.spdx.json",
                            "content_base64": SBOM_B64,
                        }
                    ],
                    "security_maintenance": {
                        "pinned_build_ref": "v1.2.3",
                        "sbom_input_ref": "sbom/image.spdx.json",
                        "input_digest": _input_digest(
                            "v1.2.3", "sbom/image.spdx.json", SBOM_B64, None
                        ),
                    },
                },
            },
        )
        _store_evidence(db_session, missing_identity)
        with pytest.raises(InvalidTransitionError, match="audit_execution_not_bound"):
            await service.accept_baseline(
                created["id"],
                BaselineAcceptRequest(audit_execution_id=missing_identity.id),
            )

        wrong_release = _execution(
            db_session,
            audit,
            status="SUCCEEDED",
            result=_screened_vulnscan(present=False),
            details={
                "_session_thread_id": "wrong-release",
                "payload": {
                    "workspace_files": [
                        {
                            "path": "sbom/image.spdx.json",
                            "content_base64": SBOM_B64,
                        }
                    ],
                    "security_maintenance": {
                        "release_id": other["id"],
                        "pinned_build_ref": "v1.2.3",
                        "sbom_input_ref": "sbom/image.spdx.json",
                        "input_digest": _input_digest(
                            "v1.2.3", "sbom/image.spdx.json", SBOM_B64, None
                        ),
                        "sbom_digest": _sbom_bytes_digest(SBOM_B64),
                    },
                },
            },
        )
        _store_evidence(db_session, wrong_release)
        with pytest.raises(InvalidTransitionError, match="audit_execution_not_bound"):
            await service.accept_baseline(
                created["id"],
                BaselineAcceptRequest(audit_execution_id=wrong_release.id),
            )

        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            swapped = await service.schedule_baseline_audit(created["id"], SBOM_B64)
        tampered = crud_security_maintenance.get_execution(
            db_session,
            account_id=test_user.account_id,
            execution_id=UUID(swapped["execution_id"]),
        )
        details = dict(tampered.trigger_event_details or {})
        payload = dict(details.get("payload") or {})
        payload["workspace_files"] = [
            {
                "path": "sbom/image.spdx.json",
                "content_base64": FRESH_SBOM_B64,
            }
        ]
        details["payload"] = payload
        tampered.trigger_event_details = details
        tampered.status = "SUCCEEDED"
        tampered.result = _screened_vulnscan(present=False)
        _store_evidence(db_session, tampered)
        with pytest.raises(InvalidTransitionError, match="audit_execution_not_bound"):
            await service.accept_baseline(
                created["id"],
                BaselineAcceptRequest(audit_execution_id=tampered.id),
            )


class TestBaselineAuditEntry:
    @pytest.mark.asyncio
    async def test_http_schedules_commits_then_dispatches_and_retries(
        self, db_session, world, test_user, test_viewer_user
    ) -> None:
        from preloop.api.app import create_app
        from preloop.api.auth import get_current_active_user
        from preloop.models.db.session import get_db_session as get_db

        created = await _release(world, test_user)
        calls: list[str] = []

        async def dispatch(execution_id, local):
            other = Session(bind=db_session.bind)
            try:
                found = crud_security_maintenance.get_execution(
                    other,
                    account_id=test_user.account_id,
                    execution_id=execution_id,
                )
                assert found is not None
                assert found.status == "PENDING"
                payload = (found.trigger_event_details or {}).get("payload") or {}
                envelope = payload.get("security_maintenance") or {}
                assert envelope.get("release_id") == created["id"]
                assert envelope.get("pinned_build_ref") == "v1.2.3"
                assert envelope.get("sbom_input_ref") == "sbom/image.spdx.json"
                assert envelope.get("flow_id") == created["audit_flow_id"]
                files = payload.get("workspace_files") or []
                assert any(
                    entry.get("path") == "sbom/image.spdx.json"
                    and entry.get("content_base64") == SBOM_B64
                    for entry in files
                    if isinstance(entry, dict)
                )
                calls.append(str(execution_id))
            finally:
                other.close()
            if len(calls) == 1:
                raise FlowDispatchError(
                    str(execution_id),
                    "PENDING",
                    RuntimeError("broker_unavailable"),
                )
            await local()

        start = AsyncMock()
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_active_user] = lambda: test_user
        path = f"/api/v1/security-maintenance/releases/{created['id']}/baseline/audit"
        with (
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                side_effect=dispatch,
            ),
            patch.object(FlowTriggerService, "_start_flow_execution", start),
            TestClient(app) as client,
        ):
            first = client.post(path, json={"sbom_content_base64": SBOM_B64})
            assert first.status_code == 200
            body = first.json()
            execution_id = body["execution_id"]
            assert body["status"] == "PENDING"
            assert body["dispatch_state"] == "pending"
            assert calls == [execution_id]
            start.assert_not_awaited()

            listed = client.get(
                f"/api/v1/security-maintenance/releases/{created['id']}"
            )
            assert listed.status_code == 200
            assert listed.json()["baseline_audit_execution_id"] == execution_id
            assert listed.json()["baseline_dispatch_state"] == "pending"

            blocked = client.post(
                f"/api/v1/security-maintenance/releases/{created['id']}/baseline",
                json={"audit_execution_id": execution_id},
            )
            assert blocked.status_code == 409
            assert "audit_execution_not_completed" in blocked.text

            omitted = client.post(
                path, json={"sbom_content_base64": OMITTED_INVENTORY_SBOM_B64}
            )
            assert omitted.status_code == 409
            assert "sbom_incomplete" in omitted.text
            assert calls == [execution_id]

            retry = client.post(path, json={"sbom_content_base64": SBOM_B64})
            assert retry.status_code == 200
            assert retry.json()["execution_id"] == execution_id
            assert retry.json()["dispatch_state"] == "dispatched"
            assert calls == [execution_id, execution_id]
            start.assert_awaited()
            precreated = start.call_args.kwargs["precreated_execution"]
            assert str(precreated.id) == execution_id
            envelope = start.call_args.args[1]["payload"]["security_maintenance"]
            assert envelope["release_id"] == created["id"]

            still_pending = client.post(
                f"/api/v1/security-maintenance/releases/{created['id']}/baseline",
                json={"audit_execution_id": execution_id},
            )
            assert still_pending.status_code == 409

            app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
            foreign = client.post(path, json={"sbom_content_base64": SBOM_B64})
            assert foreign.status_code == 404


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 7, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _item_dispatch(item: models.SecurityMaintenanceItem) -> dict:
    return dict(item.data or {})


def _baseline_dispatch(release: models.SecurityMaintenanceRelease) -> dict:
    audit = (release.data or {}).get("baseline_audit")
    return dict(audit) if isinstance(audit, dict) else {}


class TestDispatchClaimRecovery:
    @pytest.fixture(autouse=True)
    def _claim_clock(self):
        self.clock = _Clock()
        with (
            patch(
                "preloop.services.security_maintenance._utc_now",
                self.clock,
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.claim_stale_after_seconds",
                return_value=DEFAULT_DISPATCH_CLAIM_STALE_SECONDS,
            ),
        ):
            yield self.clock

    @pytest.mark.asyncio
    async def test_item_abandoned_claim_sweep_redelivers_once(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.ingest_scan(_scan(issue_id=_issue(world).id))
        db_session.expire_all()
        item = crud_security_maintenance.list_reconcile_items(
            db_session, account_id=test_user.account_id
        )[0]
        execution_id = item.implementation_execution_id
        claim = _item_dispatch(item)
        assert claim["dispatch_state"] == "dispatching"
        assert claim.get("dispatch_claimed_at")
        assert claim.get("dispatch_claim_id")
        execution = crud_security_maintenance.get_execution(
            db_session, account_id=test_user.account_id, execution_id=execution_id
        )
        assert execution is not None
        assert execution.status == "PENDING"

        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as fresh:
            await service.sweep()
        fresh.assert_not_awaited()

        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as recovered:
            await service.sweep()
        recovered.assert_awaited_once()
        assert recovered.await_args.args[0] == execution_id
        db_session.refresh(item)
        assert item.implementation_execution_id == execution_id
        assert _item_dispatch(item)["dispatch_state"] == "dispatched"

        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as again:
            await service.sweep()
        again.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recheck_abandoned_claim_sweep_redelivers_once(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as dispatched:
            item = await _to_awaiting_build(service, world, db_session, test_user)
            dispatched.side_effect = RuntimeError("dispatch_interrupted")
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await _submit_rebuild(service, item, test_user)
        db_session.expire_all()
        db_session.refresh(item)
        execution_id = item.recheck_execution_id
        assert execution_id is not None
        assert _item_dispatch(item)["dispatch_state"] == "dispatching"
        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as recovered:
            await service.sweep()
        recovered.assert_awaited_once()
        assert recovered.await_args.args[0] == execution_id
        db_session.refresh(item)
        assert item.recheck_execution_id == execution_id
        assert _item_dispatch(item)["dispatch_state"] == "dispatched"

    @pytest.mark.asyncio
    async def test_baseline_abandoned_claim_http_and_sweep_redeliver_once(
        self, db_session, world, test_user
    ) -> None:
        from preloop.api.app import create_app
        from preloop.api.auth import get_current_active_user
        from preloop.models.db.session import get_db_session as get_db

        service, *_rest = world
        created = await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.schedule_baseline_audit(created["id"], SBOM_B64)
        db_session.expire_all()
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        audit = _baseline_dispatch(release)
        execution_id = UUID(str(audit["execution_id"]))
        assert audit["dispatch_state"] == "dispatching"
        assert audit.get("dispatch_claimed_at")

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_active_user] = lambda: test_user
        path = f"/api/v1/security-maintenance/releases/{created['id']}/baseline/audit"
        with (
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                new_callable=AsyncMock,
            ) as fresh,
            TestClient(app) as client,
        ):
            listed = client.get(
                f"/api/v1/security-maintenance/releases/{created['id']}"
            )
            assert listed.status_code == 200
            assert listed.json()["baseline_dispatch_state"] == "dispatching"
            retry = client.post(path, json={"sbom_content_base64": SBOM_B64})
            assert retry.status_code == 200
            assert retry.json()["execution_id"] == str(execution_id)
            assert retry.json()["dispatch_state"] == "dispatching"
        fresh.assert_not_awaited()

        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with (
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                new_callable=AsyncMock,
            ) as recovered,
            TestClient(app) as client,
        ):
            retry = client.post(path, json={"sbom_content_base64": SBOM_B64})
            assert retry.status_code == 200
            assert retry.json()["execution_id"] == str(execution_id)
            assert retry.json()["dispatch_state"] == "dispatched"
        recovered.assert_awaited_once()
        assert recovered.await_args.args[0] == execution_id

        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as swept:
            await service.sweep()
        swept.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_baseline_legacy_dispatching_without_timestamp_sweeps(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        created = await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.schedule_baseline_audit(created["id"], SBOM_B64)
        db_session.expire_all()
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        data = dict(release.data or {})
        audit = dict(data.get("baseline_audit") or {})
        execution_id = UUID(str(audit["execution_id"]))
        audit.pop("dispatch_claimed_at", None)
        audit.pop("dispatch_claim_id", None)
        audit["dispatch_state"] = "dispatching"
        data["baseline_audit"] = audit
        crud_security_maintenance.update_release(
            db_session,
            account_id=test_user.account_id,
            release_id=release.id,
            fields={"data": data},
        )
        db_session.flush()
        assert dispatch_job_is_claimable(audit, now=self.clock())
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as recovered:
            await service.sweep()
        recovered.assert_awaited_once()
        assert recovered.await_args.args[0] == execution_id
        db_session.refresh(release)
        assert _baseline_dispatch(release)["dispatch_state"] == "dispatched"

    @pytest.mark.asyncio
    async def test_legacy_item_dispatching_without_timestamp_sweeps(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.ingest_scan(_scan(issue_id=_issue(world).id))
        db_session.expire_all()
        item = crud_security_maintenance.list_reconcile_items(
            db_session, account_id=test_user.account_id
        )[0]
        execution_id = item.implementation_execution_id
        data = dict(item.data or {})
        data.pop("dispatch_claimed_at", None)
        data.pop("dispatch_claim_id", None)
        data["dispatch_state"] = "dispatching"
        crud_security_maintenance.update_item(
            db_session,
            account_id=test_user.account_id,
            item_id=item.id,
            fields={"data": data},
        )
        db_session.flush()
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as recovered:
            await service.sweep()
        recovered.assert_awaited_once()
        assert recovered.await_args.args[0] == execution_id

    @pytest.mark.asyncio
    async def test_fresh_and_concurrent_claims_do_not_duplicate(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=FlowDispatchError(
                "00000000-0000-0000-0000-000000000001",
                "PENDING",
                RuntimeError("broker_unavailable"),
            ),
        ):
            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
        item = crud_security_maintenance.get_item(
            db_session,
            account_id=test_user.account_id,
            item_id=ingested["items"][0]["id"],
        )
        execution_id = item.implementation_execution_id
        first = crud_security_maintenance.claim_item_dispatch(
            db_session,
            account_id=test_user.account_id,
            item_id=item.id,
            execution_id=execution_id,
            kind="implementation",
            now=self.clock(),
        )
        assert first is not None
        concurrent = crud_security_maintenance.claim_item_dispatch(
            db_session,
            account_id=test_user.account_id,
            item_id=item.id,
            execution_id=execution_id,
            kind="implementation",
            now=self.clock(),
        )
        assert concurrent is None
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as skipped:
            await service._enqueue(execution_id, item.id, "implementation")
            await service.sweep()
        skipped.assert_not_awaited()
        db_session.refresh(item)
        assert item.implementation_execution_id == execution_id
        assert _item_dispatch(item)["dispatch_state"] == "dispatching"
        assert _item_dispatch(item)["dispatch_claim_id"] == str(first)

    @pytest.mark.asyncio
    async def test_stale_claim_does_not_restart_running_or_succeeded(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.ingest_scan(_scan(issue_id=_issue(world).id))
        db_session.expire_all()
        item = crud_security_maintenance.list_reconcile_items(
            db_session, account_id=test_user.account_id
        )[0]
        execution = crud_security_maintenance.get_execution(
            db_session,
            account_id=test_user.account_id,
            execution_id=item.implementation_execution_id,
        )
        execution.status = "RUNNING"
        db_session.flush()
        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as running:
            await service.sweep()
            await service.reconcile_item(item.id)
        running.assert_not_awaited()
        db_session.refresh(item)
        assert item.implementation_execution_id == execution.id
        execution.status = "SUCCEEDED"
        db_session.flush()
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as succeeded:
            await service.sweep()
        succeeded.assert_not_awaited()

        created = await service.create_release(
            SupportedReleaseCreate(
                product_key="example-widget-audit",
                release_key="1.2",
                display_name="Example Widget Audit 1.2",
                project_id=world[1].id,
                pinned_build_ref="v1.2.3",
                sbom_input_ref="sbom/image.spdx.json",
                audit_flow_id=world[4].id,
                implementation_flow_id=world[3].id,
                recheck_flow_id=world[4].id,
                approval_workflow_id=world[2].id,
                approval_owner_user_id=test_user.id,
            )
        )
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.schedule_baseline_audit(created["id"], SBOM_B64)
        db_session.expire_all()
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        baseline_id = UUID(str(_baseline_dispatch(release)["execution_id"]))
        baseline = crud_security_maintenance.get_execution(
            db_session, account_id=test_user.account_id, execution_id=baseline_id
        )
        baseline.status = "RUNNING"
        db_session.flush()
        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as running_baseline:
            await service.sweep()
            await service.schedule_baseline_audit(created["id"], SBOM_B64)
        running_baseline.assert_not_awaited()
        baseline.status = "SUCCEEDED"
        db_session.flush()
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ) as succeeded_baseline:
            await service.sweep()
        succeeded_baseline.assert_not_awaited()
        assert baseline_id == UUID(str(_baseline_dispatch(release)["execution_id"]))

    @pytest.mark.asyncio
    async def test_old_claimant_cannot_regress_new_claim(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.ingest_scan(_scan(issue_id=_issue(world).id))
        db_session.expire_all()
        item = crud_security_maintenance.list_reconcile_items(
            db_session, account_id=test_user.account_id
        )[0]
        old_claim = UUID(str(_item_dispatch(item)["dispatch_claim_id"]))
        execution_id = item.implementation_execution_id
        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            await service.sweep()
        db_session.refresh(item)
        assert _item_dispatch(item)["dispatch_state"] == "dispatched"
        assert item.state == "remediating"
        lost_release = crud_security_maintenance.finish_item_dispatch(
            db_session,
            account_id=test_user.account_id,
            item_id=item.id,
            execution_id=execution_id,
            kind="implementation",
            claim_id=old_claim,
            dispatched=False,
        )
        assert lost_release is None
        db_session.refresh(item)
        assert _item_dispatch(item)["dispatch_state"] == "dispatched"
        assert item.state == "remediating"
        lost_complete = crud_security_maintenance.finish_item_dispatch(
            db_session,
            account_id=test_user.account_id,
            item_id=item.id,
            execution_id=execution_id,
            kind="implementation",
            claim_id=old_claim,
            dispatched=True,
        )
        assert lost_complete is None
        db_session.refresh(item)
        assert item.state == "remediating"
        assert item.implementation_execution_id == execution_id

        created = await service.create_release(
            SupportedReleaseCreate(
                product_key="example-widget-baseline-claim",
                release_key="1.2",
                display_name="Example Widget Baseline Claim 1.2",
                project_id=world[1].id,
                pinned_build_ref="v1.2.3",
                sbom_input_ref="sbom/image.spdx.json",
                audit_flow_id=world[4].id,
                implementation_flow_id=world[3].id,
                recheck_flow_id=world[4].id,
                approval_workflow_id=world[2].id,
                approval_owner_user_id=test_user.id,
            )
        )
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.schedule_baseline_audit(created["id"], SBOM_B64)
        db_session.expire_all()
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        old_baseline = UUID(str(_baseline_dispatch(release)["dispatch_claim_id"]))
        baseline_execution = UUID(str(_baseline_dispatch(release)["execution_id"]))
        self.clock.advance(DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            await service.sweep()
        db_session.refresh(release)
        assert _baseline_dispatch(release)["dispatch_state"] == "dispatched"
        assert (
            crud_security_maintenance.finish_baseline_dispatch(
                db_session,
                account_id=test_user.account_id,
                release_id=release.id,
                execution_id=baseline_execution,
                claim_id=old_baseline,
                dispatched=False,
            )
            is None
        )
        db_session.refresh(release)
        assert _baseline_dispatch(release)["dispatch_state"] == "dispatched"

    @pytest.mark.asyncio
    async def test_two_session_preloaded_item_sees_fresh_claim_and_running(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.ingest_scan(_scan(issue_id=_issue(world).id))
        session_a = db_session
        item_a = crud_security_maintenance.list_reconcile_items(
            session_a, account_id=test_user.account_id
        )[0]
        item_id = item_a.id
        execution_id = item_a.implementation_execution_id
        execution_a = crud_security_maintenance.get_execution(
            session_a, account_id=test_user.account_id, execution_id=execution_id
        )
        assert execution_a is not None
        assert execution_a.status == "PENDING"
        old_claim = UUID(str(_item_dispatch(item_a)["dispatch_claim_id"]))
        stale_now = self.clock.current + timedelta(
            seconds=DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1
        )
        session_b = Session(bind=session_a.connection())
        try:
            fresh = crud_security_maintenance.claim_item_dispatch(
                session_b,
                account_id=test_user.account_id,
                item_id=item_id,
                execution_id=execution_id,
                kind="implementation",
                now=stale_now,
            )
            session_b.flush()
            assert fresh is not None
            assert fresh != old_claim
            refused = crud_security_maintenance.claim_item_dispatch(
                session_a,
                account_id=test_user.account_id,
                item_id=item_id,
                execution_id=execution_id,
                kind="implementation",
                now=stale_now,
            )
            assert refused is None
            stale_finish = crud_security_maintenance.finish_item_dispatch(
                session_a,
                account_id=test_user.account_id,
                item_id=item_id,
                execution_id=execution_id,
                kind="implementation",
                claim_id=old_claim,
                dispatched=False,
            )
            assert stale_finish is None
            execution_b = crud_security_maintenance.get_execution(
                session_b, account_id=test_user.account_id, execution_id=execution_id
            )
            execution_b.status = "RUNNING"
            session_b.flush()
            running_claim = crud_security_maintenance.claim_item_dispatch(
                session_a,
                account_id=test_user.account_id,
                item_id=item_id,
                execution_id=execution_id,
                kind="implementation",
                now=stale_now + timedelta(seconds=1),
            )
            assert running_claim is None
        finally:
            session_b.close()

    @pytest.mark.asyncio
    async def test_two_session_preloaded_baseline_sees_fresh_claim(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        created = await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch_interrupted"),
        ):
            with pytest.raises(RuntimeError, match="dispatch_interrupted"):
                await service.schedule_baseline_audit(created["id"], SBOM_B64)
        session_a = db_session
        release_a = crud_security_maintenance.get_release(
            session_a, account_id=test_user.account_id, release_id=created["id"]
        )
        audit = _baseline_dispatch(release_a)
        execution_id = UUID(str(audit["execution_id"]))
        old_claim = UUID(str(audit["dispatch_claim_id"]))
        execution_a = crud_security_maintenance.get_execution(
            session_a, account_id=test_user.account_id, execution_id=execution_id
        )
        assert execution_a is not None
        assert execution_a.status == "PENDING"
        stale_now = self.clock.current + timedelta(
            seconds=DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 1
        )
        session_b = Session(bind=session_a.connection())
        try:
            fresh = crud_security_maintenance.claim_baseline_dispatch(
                session_b,
                account_id=test_user.account_id,
                release_id=release_a.id,
                execution_id=execution_id,
                now=stale_now,
            )
            session_b.flush()
            assert fresh is not None
            assert fresh != old_claim
            refused = crud_security_maintenance.claim_baseline_dispatch(
                session_a,
                account_id=test_user.account_id,
                release_id=release_a.id,
                execution_id=execution_id,
                now=stale_now,
            )
            assert refused is None
            stale_finish = crud_security_maintenance.finish_baseline_dispatch(
                session_a,
                account_id=test_user.account_id,
                release_id=release_a.id,
                execution_id=execution_id,
                claim_id=old_claim,
                dispatched=False,
            )
            assert stale_finish is None
        finally:
            session_b.close()


async def _finalize_controller_provenance(db_session, execution, flow, result):
    """Run the real completion attach path. Does not assign verified rows."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.flow = flow
    orchestrator.db = db_session
    orchestrator.execution_id = str(execution.id)
    orchestrator.execution_log = execution
    orchestrator.trigger_event_data = execution.trigger_event_details
    orchestrator._isolated_publication_policy = None
    orchestrator.execution_logger = MagicMock()
    orchestrator._evidence_archive = None
    payload = {"status": "SUCCEEDED", "result": copy.deepcopy(result)}
    payload["result"]["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
        "repositories": [
            {
                "remote": "https://github.com/example/forged.git",
                "sha": "f" * 40,
                "clone_path": "forged",
                "role": "code",
                "sha_status": "verified",
            }
        ],
    }
    await orchestrator._finish_isolated_publication(payload)
    execution.result = payload.get("result") or {}
    execution.status = payload["status"]
    db_session.flush()
    return payload


def _store_frozen_bundle(
    db_session, execution, bundle: bytes, *, decoy_sha: str
) -> bytes:
    return _store_evidence(
        db_session,
        execution,
        sha=decoy_sha,
        include_head=True,
        attach_checkout=False,
        extra={"evidence/branch.bundle": bundle},
    )


def _pin_audit_workspace_clone(audit: models.Flow) -> None:
    config = dict(audit.git_clone_config or {})
    rows = list(config.get("repositories") or [])
    if rows and isinstance(rows[0], dict):
        rows[0] = {**rows[0], "clone_path": "/workspace"}
        config["repositories"] = rows
        audit.git_clone_config = config
        flag_modified(audit, "git_clone_config")


def _run_readonly_checkout_export(
    workspace: Path, trigger: dict | None, git_config: dict | None
) -> bytes:
    """Run the actual hosted/private post-exec exporter against a local Git tree."""
    import subprocess

    from preloop.agents.container import ContainerAgentExecutor

    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    script = executor._prepare_git_post_execution_commands(
        {
            "git_clone_config": git_config,
            "trigger_event_data": trigger,
        }
    )
    assert "git bundle create" in script
    assert " HEAD " in script or script.rstrip().endswith("HEAD")
    for forbidden in (
        "git push",
        "git commit",
        "curl",
        "PRELOOP_GIT_TOKEN",
        "contents:write",
        "/preloop-publication-output",
    ):
        assert forbidden not in script
    adapted = script.replace("/workspace", str(workspace))
    subprocess.run(
        ["bash", "-c", adapted],
        check=True,
        cwd=workspace,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    produced = workspace / "evidence" / "branch.bundle"
    assert produced.is_file()
    return produced.read_bytes()


class TestControllerFrozenCheckoutAuthority:
    @pytest.mark.asyncio
    async def test_http_baseline_then_recheck_uses_controller_bundle_facts(
        self, db_session, world, test_user, tmp_path
    ) -> None:
        from preloop.api.app import create_app
        from preloop.api.auth import get_current_active_user
        from preloop.models.db.session import get_db_session as get_db
        from preloop.services.flow_artifacts import inspect_evidence, load_evidence
        from preloop.services.security_maintenance_refs import controller_checkout_sha
        from preloop.services.publication_worker import inspect_bundle
        from preloop.services.trusted_publisher import read_publication_bundle
        from tests.services.test_multi_repo_publication import _git, _init_repo

        service, project, workflow, implementer, audit, *_ = world
        _pin_audit_workspace_clone(audit)
        repo, baseline_sha, _prebuilt = _init_repo(
            tmp_path, "workspace", "example firmware"
        )
        del _prebuilt
        decoy = "d" * 40
        created = await service.create_release(
            SupportedReleaseCreate(
                product_key="example-widget",
                release_key="1.2",
                display_name="Example Widget 1.2",
                project_id=project.id,
                pinned_build_ref=baseline_sha,
                sbom_input_ref="sbom/image.spdx.json",
                audit_flow_id=audit.id,
                implementation_flow_id=implementer.id,
                recheck_flow_id=audit.id,
                approval_workflow_id=workflow.id,
                approval_owner_user_id=test_user.id,
            )
        )
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_active_user] = lambda: test_user
        audit_path = (
            f"/api/v1/security-maintenance/releases/{created['id']}/baseline/audit"
        )
        with (
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                new_callable=AsyncMock,
            ),
            TestClient(app) as client,
        ):
            scheduled = client.post(audit_path, json={"sbom_content_base64": SBOM_B64})
            assert scheduled.status_code == 200
            execution_id = scheduled.json()["execution_id"]
            execution = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=UUID(execution_id),
            )
            payload = (execution.trigger_event_details or {}).get("payload") or {}
            mapping = payload.get("product_provenance") or {}
            assert mapping["repositories"][0]["sha"] == baseline_sha
            assert mapping["repositories"][0]["remote"] == REPO_URL
            baseline_bundle = _run_readonly_checkout_export(
                repo, execution.trigger_event_details, audit.git_clone_config
            )
            _store_frozen_bundle(
                db_session, execution, baseline_bundle, decoy_sha=decoy
            )
            archive, receipt = load_evidence(
                db_session, account_id=test_user.account_id, execution=execution
            )
            inspect_evidence(
                db_session, account_id=test_user.account_id, execution=execution
            )
            assert receipt.get("kind") == "evidence"
            inspect_bundle(read_publication_bundle(archive), baseline_sha)
            finalized = await _finalize_controller_provenance(
                db_session, execution, audit, _screened_vulnscan(present=False)
            )
            assert finalized["status"] == "SUCCEEDED"
            provenance = execution.result["product_provenance"]
            assert provenance["mapping_status"] == "verified"
            assert provenance["repositories"][0]["sha"] == baseline_sha
            assert provenance["repositories"][0]["sha_status"] == "verified"
            assert provenance["repositories"][0]["remote"] == REPO_URL
            assert "forged.git" not in str(provenance)
            assert (
                controller_checkout_sha(
                    execution,
                    flow=audit,
                    db=db_session,
                    account_id=test_user.account_id,
                )
                == baseline_sha
            )
            accepted = client.post(
                f"/api/v1/security-maintenance/releases/{created['id']}/baseline",
                json={"audit_execution_id": execution_id},
            )
            assert accepted.status_code == 200

            (repo / "fix.txt").write_text("patched libexample")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "repair")
            repair_sha = _git(repo, "rev-parse", "HEAD")

            forged = _execution(
                db_session,
                audit,
                status="SUCCEEDED",
                result=_screened_vulnscan(present=False),
                details=execution.trigger_event_details,
            )
            _store_frozen_bundle(db_session, forged, baseline_bundle, decoy_sha=decoy)
            forged.result = {
                **_screened_vulnscan(present=False),
                "product_provenance": {
                    "schema": "preloop.cra.product_provenance/v1",
                    "mapping_status": "verified",
                    "repositories": [
                        {
                            "remote": REPO_URL,
                            "sha": decoy,
                            "clone_path": "workspace-1",
                            "role": "code",
                            "sha_status": "verified",
                        }
                    ],
                },
            }
            assert (
                controller_checkout_sha(
                    forged, flow=audit, db=db_session, account_id=test_user.account_id
                )
                == baseline_sha
            )

            ingested = await service.ingest_scan(_scan(issue_id=_issue(world).id))
            item = crud_security_maintenance.get_item(
                db_session,
                account_id=test_user.account_id,
                item_id=ingested["items"][0]["id"],
            )
            impl = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.implementation_execution_id,
            )
            impl.status = "SUCCEEDED"
            impl.result = {
                "_private_publication": {
                    "phase": "complete",
                    "receipt": _receipt(impl.id, sha=repair_sha),
                },
                "trusted_publication": _receipt(impl.id, sha=repair_sha),
                "verification": _verification(commit=repair_sha),
            }
            db_session.flush()
            await service.finish_execution(impl)
            db_session.refresh(item)
            approved = await service.decide_approval(
                item.id,
                ApprovalDecisionRequest(reason="Ship the supported-release patch"),
                actor_user_id=test_user.id,
                approved=True,
            )
            assert approved["state"] == "awaiting_build"
            await service.submit_rebuilt_inputs(
                item.id,
                RebuiltInputsRequest(
                    published_sha=repair_sha, sbom_content_base64=REMOVED_SBOM_B64
                ),
                actor_user_id=test_user.id,
            )
            db_session.refresh(item)
            recheck = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.recheck_execution_id,
            )
            recheck_payload = (recheck.trigger_event_details or {}).get("payload") or {}
            assert recheck_payload["product_provenance"]["repositories"][0]["sha"] == (
                repair_sha
            )
            repair_bundle = _run_readonly_checkout_export(
                repo, recheck.trigger_event_details, audit.git_clone_config
            )
            _store_frozen_bundle(db_session, recheck, repair_bundle, decoy_sha=decoy)
            await _finalize_controller_provenance(
                db_session, recheck, audit, _omitted_target_vulnscan()
            )
            assert recheck.status == "SUCCEEDED"
            assert (
                controller_checkout_sha(
                    recheck, flow=audit, db=db_session, account_id=test_user.account_id
                )
                == repair_sha
            )
            await service.finish_execution(recheck)
            db_session.refresh(item)
            assert item.state == "resolved"
            release = crud_security_maintenance.get_release(
                db_session, account_id=test_user.account_id, release_id=item.release_id
            )
            assert release.accepted_baseline_id is not None
            assert str(release.accepted_baseline_id) != accepted.json()["id"]

    @pytest.mark.asyncio
    async def test_wrong_repo_mapping_and_missing_bundle_are_denied(
        self, db_session, world, test_user, tmp_path
    ) -> None:
        from preloop.services.security_maintenance_refs import controller_checkout_sha
        from tests.services.test_multi_repo_publication import _init_repo

        service, project, workflow, implementer, audit, *_ = world
        _repo, baseline_sha, baseline_bundle = _init_repo(
            tmp_path, "firmware", "example firmware"
        )
        created = await service.create_release(
            SupportedReleaseCreate(
                product_key="example-widget",
                release_key="1.2",
                display_name="Example Widget 1.2",
                project_id=project.id,
                pinned_build_ref=baseline_sha,
                sbom_input_ref="sbom/image.spdx.json",
                audit_flow_id=audit.id,
                implementation_flow_id=implementer.id,
                recheck_flow_id=audit.id,
                approval_workflow_id=workflow.id,
                approval_owner_user_id=test_user.id,
            )
        )
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            scheduled = await service.schedule_baseline_audit(created["id"], SBOM_B64)
        execution = crud_security_maintenance.get_execution(
            db_session,
            account_id=test_user.account_id,
            execution_id=UUID(scheduled["execution_id"]),
        )
        details = dict(execution.trigger_event_details or {})
        payload = dict(details.get("payload") or {})
        mapping = dict(payload.get("product_provenance") or {})
        repos = list(mapping.get("repositories") or [])
        repos[0] = dict(repos[0], remote="https://github.com/example/other.git")
        mapping["repositories"] = repos
        payload["product_provenance"] = mapping
        details["payload"] = payload
        execution.trigger_event_details = details
        _store_frozen_bundle(db_session, execution, baseline_bundle, decoy_sha="d" * 40)
        finalized = await _finalize_controller_provenance(
            db_session, execution, audit, _screened_vulnscan(present=False)
        )
        assert finalized["status"] == "FAILED"
        provenance = (execution.result or {}).get("product_provenance") or {}
        assert provenance.get("mapping_status") != "verified"
        missing_bundle = _execution(
            db_session,
            audit,
            details=execution.trigger_event_details,
        )
        _store_evidence(
            db_session,
            missing_bundle,
            include_head=True,
            attach_checkout=False,
            sha="d" * 40,
        )
        assert (
            controller_checkout_sha(
                missing_bundle,
                flow=audit,
                db=db_session,
                account_id=test_user.account_id,
            )
            is None
        )


def _pending_approvals_for_item(
    db_session: Session, account_id: UUID, item_id: UUID
) -> list[models.ApprovalRequest]:
    return list(
        db_session.scalars(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.account_id == account_id,
                models.ApprovalRequest.tool_name == "security_maintenance",
                models.ApprovalRequest.status == "pending",
                models.ApprovalRequest.tool_args["item_id"].astext == str(item_id),
            )
        )
    )


async def _named_release(world, test_user, product_key: str):
    service, project, workflow, implementer, audit, *_rest = world
    return await service.create_release(
        SupportedReleaseCreate(
            product_key=product_key,
            release_key="1.2",
            display_name=f"{product_key} 1.2",
            project_id=project.id,
            pinned_build_ref="v1.2.3",
            sbom_input_ref="sbom/image.spdx.json",
            audit_flow_id=audit.id,
            implementation_flow_id=implementer.id,
            recheck_flow_id=audit.id,
            approval_workflow_id=workflow.id,
            approval_owner_user_id=test_user.id,
        )
    )


def _set_baseline_audit(
    db_session: Session,
    account_id: UUID,
    release_id: UUID | str,
    execution_id: UUID,
    **fields: object,
) -> None:
    release = crud_security_maintenance.get_release(
        db_session, account_id=account_id, release_id=UUID(str(release_id))
    )
    data = dict(release.data or {})
    audit = dict(data.get("baseline_audit") or {})
    audit["execution_id"] = str(execution_id)
    audit.update(fields)
    data["baseline_audit"] = audit
    crud_security_maintenance.update_release(
        db_session,
        account_id=account_id,
        release_id=UUID(str(release_id)),
        fields={"data": data},
    )


class TestPublicReviewFindings:
    @pytest.mark.asyncio
    async def test_pending_approval_is_unique_per_item(
        self, db_session, world, test_user
    ) -> None:
        service, _project, workflow, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
        tool = crud_security_maintenance.get_or_create_tool_configuration(
            db_session,
            account_id=test_user.account_id,
            tool_name="security_maintenance",
        )
        duplicate = models.ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            tool_name="security_maintenance",
            tool_args={"item_id": str(item.id)},
            status="pending",
            approval_token=secrets.token_urlsafe(32),
        )
        with pytest.raises(IntegrityError):
            with db_session.begin_nested():
                db_session.add(duplicate)
                db_session.flush()
        assert (
            len(_pending_approvals_for_item(db_session, test_user.account_id, item.id))
            == 1
        )

    @pytest.mark.asyncio
    async def test_open_approval_binds_orphaned_pending_without_sticky_flag(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            original_id = item.approval_request_id
            service._write_item(item, approval_request_id=None, state="tests_passed")
            with patch.object(
                ApprovalService,
                "create_approval_request",
                new_callable=AsyncMock,
                side_effect=IntegrityError(
                    "INSERT", {}, Exception("uq_sm_pending_approval_item")
                ),
            ):
                await service._open_approval(item.id)
            db_session.refresh(item)
            await service._open_approval(item.id)
        db_session.refresh(item)
        assert item.approval_request_id == original_id
        assert item.state == "approval_pending"
        assert "approval_opening" not in dict(item.data or {})
        assert (
            len(_pending_approvals_for_item(db_session, test_user.account_id, item.id))
            == 1
        )

    @pytest.mark.asyncio
    async def test_batch_decision_advances_maintenance_item(
        self, db_session, world, test_user
    ) -> None:
        from preloop.api.endpoints.approval_requests import decide_requests_batch
        from preloop.models.schemas.approval_request import ApprovalBatchDecision

        service, *_rest = world
        await _release(world, test_user)
        http_request = MagicMock()
        http_request.base_url = "http://localhost"
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            foreign_id = uuid4()
            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService",
                return_value=service._approval_service(),
            ):
                response = await decide_requests_batch(
                    decision=ApprovalBatchDecision(
                        ids=[foreign_id, item.approval_request_id],
                        approved=True,
                        comment="Ship the supported-release patch",
                    ),
                    request=http_request,
                    current_user=test_user,
                    db=AsyncMock(),
                    sync_db=db_session,
                )
        by_id = {row.id: row for row in response.results}
        assert by_id[foreign_id].ok is False
        assert by_id[foreign_id].error == "Approval request not found"
        assert by_id[item.approval_request_id].ok is True
        assert by_id[item.approval_request_id].status == "approved"
        db_session.refresh(item)
        assert item.state == "awaiting_build"

    @pytest.mark.asyncio
    async def test_batch_managed_credential_does_not_advance(
        self, db_session, world, test_user
    ) -> None:
        from preloop.api.endpoints.approval_requests import decide_requests_batch
        from preloop.models.schemas.approval_request import ApprovalBatchDecision

        service, *_rest = world
        await _release(world, test_user)
        _key, _token = crud_api_key.create_runtime_key(
            db_session,
            name="execution-key",
            account_id=test_user.account_id,
            user_id=test_user.id,
            context_data={"flow_execution_id": str(uuid4())},
            commit=False,
        )
        test_user._auth_api_key = _key
        http_request = MagicMock()
        http_request.base_url = "http://localhost"
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService",
                return_value=service._approval_service(),
            ):
                response = await decide_requests_batch(
                    decision=ApprovalBatchDecision(
                        ids=[item.approval_request_id],
                        approved=True,
                        comment="Ship the supported-release patch",
                    ),
                    request=http_request,
                    current_user=test_user,
                    db=AsyncMock(),
                    sync_db=db_session,
                )
        delattr(test_user, "_auth_api_key")
        assert response.results[0].ok is False
        assert response.results[0].error == "managed_credential_cannot_decide"
        db_session.refresh(item)
        assert item.state == "approval_pending"

    @pytest.mark.asyncio
    async def test_reconcile_error_after_commit_is_retryable(
        self, db_session, world, test_user
    ) -> None:
        from preloop.api.endpoints.approval_requests import (
            _advance_security_maintenance,
            decide_requests_batch,
        )
        from preloop.models.schemas.approval_request import ApprovalBatchDecision

        service, *_rest = world
        await _release(world, test_user)
        http_request = MagicMock()
        http_request.base_url = "http://localhost"
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            item, _impl = await _impl_to_approval(service, world, db_session, test_user)
            ingested = await service.ingest_scan(
                _scan(
                    issue_id=_issue(world).id,
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0002",
                            component_id="libexample",
                        )
                    ],
                )
            )
            other = crud_security_maintenance.get_item(
                db_session,
                account_id=test_user.account_id,
                item_id=ingested["items"][0]["id"],
            )
            impl = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=other.implementation_execution_id,
            )
            impl.status = "SUCCEEDED"
            impl.result = {
                "_private_publication": {
                    "phase": "complete",
                    "receipt": _receipt(impl.id),
                },
                "trusted_publication": _receipt(impl.id),
                "verification": _verification(),
            }
            db_session.flush()
            await service.finish_execution(impl)
            db_session.refresh(other)

            row = crud_security_maintenance.get_approval_request(
                db_session,
                account_id=test_user.account_id,
                request_id=item.approval_request_id,
            )
            updated = await service._approval_service().approve_request(
                row.id,
                "console ship",
                user_id=test_user.id,
                channel="console",
            )
            with (
                patch.object(
                    SecurityMaintenanceService,
                    "reconcile_platform_approval",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("reconcile_broke"),
                ),
                patch(
                    "preloop.api.endpoints.approval_requests.logger.exception"
                ) as logged,
            ):
                await _advance_security_maintenance(db_session, updated)
            logged.assert_called()
            db_session.refresh(item)
            assert updated.status == "approved"
            assert item.state == "approval_pending"

            with (
                patch.object(
                    SecurityMaintenanceService,
                    "reconcile_platform_approval",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("reconcile_broke"),
                ),
                patch(
                    "preloop.api.endpoints.approval_requests.ApprovalService",
                    return_value=service._approval_service(),
                ),
            ):
                batch = await decide_requests_batch(
                    decision=ApprovalBatchDecision(
                        ids=[other.approval_request_id],
                        approved=True,
                        comment="Ship the supported-release patch",
                    ),
                    request=http_request,
                    current_user=test_user,
                    db=AsyncMock(),
                    sync_db=db_session,
                )
            assert batch.results[0].ok is True
            assert batch.results[0].status == "approved"
            db_session.refresh(other)
            assert other.state == "approval_pending"
            advanced_other = await service.reconcile_platform_approval(
                other.approval_request_id
            )
            advanced_item = await service.reconcile_platform_approval(updated.id)
        assert advanced_other is not None
        assert advanced_other["state"] == "awaiting_build"
        assert advanced_item is not None
        assert advanced_item["state"] == "awaiting_build"

    @pytest.mark.asyncio
    async def test_sweep_selects_claimable_baselines_with_keyset(
        self, db_session, world, test_user
    ) -> None:
        service, *_rest = world
        audit_flow = world[4]
        now = datetime.now(UTC).replace(tzinfo=None)
        stale = now - timedelta(seconds=DEFAULT_DISPATCH_CLAIM_STALE_SECONDS + 5)
        first = await _named_release(world, test_user, "widget-a")
        second = await _named_release(world, test_user, "widget-b")
        third = await _named_release(world, test_user, "widget-c")
        fourth = await _named_release(world, test_user, "widget-d")
        pending_a = _execution(db_session, audit_flow, status="PENDING")
        pending_b = _execution(db_session, audit_flow, status="PENDING")
        running = _execution(db_session, audit_flow, status="RUNNING")
        fresh = _execution(db_session, audit_flow, status="PENDING")
        _set_baseline_audit(
            db_session,
            test_user.account_id,
            first["id"],
            pending_a.id,
            dispatch_state="pending",
        )
        _set_baseline_audit(
            db_session,
            test_user.account_id,
            second["id"],
            pending_b.id,
            dispatch_state="dispatching",
            dispatch_claimed_at=format_dispatch_claimed_at(stale),
        )
        _set_baseline_audit(
            db_session,
            test_user.account_id,
            third["id"],
            running.id,
            dispatch_state="pending",
        )
        _set_baseline_audit(
            db_session,
            test_user.account_id,
            fourth["id"],
            fresh.id,
            dispatch_state="dispatching",
            dispatch_claimed_at=format_dispatch_claimed_at(now),
        )
        with patch.object(
            crud_security_maintenance, "get_execution", wraps=None
        ) as spy:
            spy.side_effect = AssertionError("N+1 execution lookup")
            eligible = crud_security_maintenance.list_pending_baseline_releases(
                db_session,
                account_id=test_user.account_id,
                now=now,
                limit=50,
            )
        eligible_ids = {row.id for row in eligible}
        assert UUID(first["id"]) in eligible_ids
        assert UUID(second["id"]) in eligible_ids
        assert UUID(third["id"]) not in eligible_ids
        assert UUID(fourth["id"]) not in eligible_ids
        page = crud_security_maintenance.list_pending_baseline_releases(
            db_session,
            account_id=test_user.account_id,
            now=now,
            limit=1,
        )
        assert len(page) == 1
        rest = crud_security_maintenance.list_pending_baseline_releases(
            db_session,
            account_id=test_user.account_id,
            now=now,
            limit=1,
            after_id=page[0].id,
        )
        assert len(rest) == 1
        assert rest[0].id != page[0].id
        assert {page[0].id, rest[0].id} == {
            UUID(first["id"]),
            UUID(second["id"]),
        }
        assert (
            crud_security_maintenance.list_pending_baseline_releases(
                db_session, account_id=uuid4(), now=now, limit=50
            )
            == []
        )
        account_ids = crud_security_maintenance.list_reconcile_account_ids(db_session)
        assert test_user.account_id in account_ids
        spy.assert_not_called()


def _sm_id(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def _seed_maintenance_item(
    db_session: Session,
    test_user: models.User,
    release: models.SecurityMaintenanceRelease,
    *,
    n: int,
    state: str,
    advisory: str,
    approval_request_id: UUID | None = None,
    execution_id: UUID | None = None,
) -> models.SecurityMaintenanceItem:
    return crud_security_maintenance.create_item(
        db_session,
        account_id=test_user.account_id,
        fields={
            "id": _sm_id(n),
            "release_id": release.id,
            "identity_key": item_identity_key(
                test_user.account_id,
                release.product_key,
                release.release_key,
                advisory,
                "libexample",
            ),
            "product_key": release.product_key,
            "release_key": release.release_key,
            "advisory_id": advisory,
            "component_id": "libexample",
            "state": state,
            "scan_fingerprint": "a" * 64,
            "data": {"dispatch_state": "pending"} if execution_id else {},
            "approval_request_id": approval_request_id,
            "implementation_execution_id": execution_id,
        },
    )


def _seed_pending_request(
    db_session: Session,
    test_user: models.User,
    workflow: models.ApprovalWorkflow,
    *,
    item_id: UUID,
    expires_at: datetime,
) -> models.ApprovalRequest:
    tool = crud_security_maintenance.get_or_create_tool_configuration(
        db_session, account_id=test_user.account_id, tool_name="security_maintenance"
    )
    row = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="security_maintenance",
        tool_args={"item_id": str(item_id)},
        status="pending",
        approval_token=secrets.token_urlsafe(32),
        expires_at=expires_at,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _defer_dispatch(*_args: object, **_kwargs: object) -> None:
    raise FlowDispatchError(
        "00000000-0000-0000-0000-000000000001",
        "PENDING",
        RuntimeError("broker_unavailable"),
    )


class TestSweepProgress:
    @pytest.mark.asyncio
    async def test_idle_human_queue_does_not_starve_newer_approval(
        self, db_session, world, test_user
    ) -> None:
        from preloop.services.security_maintenance_runtime import (
            sweep_security_maintenance,
        )

        service, _project, workflow, *_rest = world
        created = await _release(world, test_user)
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
        idle_ids = []
        for n in (1, 2):
            item_id = _sm_id(n)
            request = _seed_pending_request(
                db_session,
                test_user,
                workflow,
                item_id=item_id,
                expires_at=future,
            )
            _seed_maintenance_item(
                db_session,
                test_user,
                release,
                n=n,
                state="approval_pending",
                advisory=f"CVE-2024-00{n:02d}",
                approval_request_id=request.id,
            )
            idle_ids.append((item_id, request.id))
        expired_id = _sm_id(3)
        expired_request = _seed_pending_request(
            db_session,
            test_user,
            workflow,
            item_id=expired_id,
            expires_at=past,
        )
        _seed_maintenance_item(
            db_session,
            test_user,
            release,
            n=3,
            state="approval_pending",
            advisory="CVE-2024-0003",
            approval_request_id=expired_request.id,
        )
        later = _seed_maintenance_item(
            db_session,
            test_user,
            release,
            n=4,
            state="tests_passed",
            advisory="CVE-2024-0004",
        )
        other = models.Account(organization_name="other")
        db_session.add(other)
        db_session.flush()
        eligible = crud_security_maintenance.list_reconcile_items(
            db_session, account_id=test_user.account_id, limit=50
        )
        eligible_ids = {row.id for row in eligible}
        assert later.id in eligible_ids
        assert expired_id in eligible_ids
        assert _sm_id(1) not in eligible_ids
        assert _sm_id(2) not in eligible_ids
        assert (
            crud_security_maintenance.list_reconcile_items(
                db_session, account_id=other.id, limit=50
            )
            == []
        )
        with (
            patch("preloop.services.security_maintenance.SWEEP_LIMIT", 2),
            patch(
                "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                new_callable=AsyncMock,
            ),
        ):
            result = await sweep_security_maintenance(db_session)
        assert result["accounts"] >= 1
        db_session.refresh(later)
        assert later.state == "approval_pending"
        assert later.approval_request_id is not None
        for item_id, request_id in idle_ids:
            item = crud_security_maintenance.get_item(
                db_session, account_id=test_user.account_id, item_id=item_id
            )
            assert item.state == "approval_pending"
            assert item.approval_request_id == request_id
        assert (
            crud_security_maintenance.get_sweep_progress(
                db_session, account_id=other.id
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_item_cursor_reaches_later_dispatch_retries(
        self, db_session, world, test_user
    ) -> None:
        from preloop.services.security_maintenance_runtime import (
            sweep_security_maintenance,
        )

        _service, _project, _workflow, implementer, *_rest = world
        created = await _release(world, test_user)
        release = crud_security_maintenance.get_release(
            db_session, account_id=test_user.account_id, release_id=created["id"]
        )
        executions = []
        for n in range(1, 6):
            execution = _execution(db_session, implementer, status="PENDING")
            executions.append(execution)
            _seed_maintenance_item(
                db_session,
                test_user,
                release,
                n=n,
                state="remediation_pending",
                advisory=f"CVE-2024-10{n:02d}",
                execution_id=execution.id,
            )
        db_session.flush()
        seen: list[UUID] = []
        with patch("preloop.services.security_maintenance.SWEEP_LIMIT", 2):
            for _step in range(3):
                with patch(
                    "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                    new_callable=AsyncMock,
                    side_effect=_defer_dispatch,
                ) as dispatched:
                    await sweep_security_maintenance(db_session)
                batch = [call.args[0] for call in dispatched.await_args_list]
                assert len(batch) <= 2
                assert len(batch) == len(set(batch))
                seen.extend(batch)
        assert set(seen) == {row.id for row in executions}

    @pytest.mark.asyncio
    async def test_baseline_cursor_reaches_later_failed_dispatches(
        self, db_session, world, test_user
    ) -> None:
        from preloop.services.security_maintenance_runtime import (
            sweep_security_maintenance,
        )

        _service, *_rest = world
        audit_flow = world[4]
        executions = []
        for n in range(1, 6):
            created = await _named_release(world, test_user, f"widget-sweep-{n}")
            execution = _execution(db_session, audit_flow, status="PENDING")
            executions.append(execution)
            _set_baseline_audit(
                db_session,
                test_user.account_id,
                created["id"],
                execution.id,
                dispatch_state="pending",
            )
        db_session.flush()
        seen: list[UUID] = []
        with patch("preloop.services.security_maintenance.SWEEP_LIMIT", 2):
            for _step in range(3):
                with patch(
                    "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
                    new_callable=AsyncMock,
                    side_effect=_defer_dispatch,
                ) as dispatched:
                    await sweep_security_maintenance(db_session)
                batch = [call.args[0] for call in dispatched.await_args_list]
                assert len(batch) <= 2
                assert len(batch) == len(set(batch))
                seen.extend(batch)
        assert set(seen) == {row.id for row in executions}
