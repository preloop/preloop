"""Isolated security-maintenance authority tests against real CRUD records."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import tarfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_security_maintenance, flow_artifact
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.security_maintenance import item_identity_key
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
SBOM_B64 = base64.b64encode(
    b'{"bomFormat": "CycloneDX", "specVersion": "1.5"}'
).decode()
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
            {"project_id": str(project_id), "tracker_id": str(tracker_id)}
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


def _store_evidence(
    db_session,
    execution,
    *,
    expires=None,
    digest=None,
    sha: str = SHA,
    extra=None,
    include_head: bool = True,
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

    def test_checkout_ignores_payload_sha_and_requires_head_txt(
        self, db_session, world, test_user
    ) -> None:
        from preloop.services.security_maintenance_refs import controller_checkout_sha

        _service, _project, _workflow, _implementer, audit, *_ = world
        execution = _execution(
            db_session,
            audit,
            details={"payload": {"sha": SHA}, "_session_thread_id": "no-head"},
        )
        _store_evidence(db_session, execution, extra={"result.json": b"{}"})
        # Default store writes HEAD.txt; overwrite with archive that has none.
        execution.trigger_event_details = {
            **(execution.trigger_event_details or {}),
            "_session_thread_id": str(execution.id),
        }
        missing = _execution(
            db_session,
            audit,
            details={"payload": {"sha": SHA}, "_session_thread_id": "missing-head"},
        )
        archive = _archive_members({"result.json": b'{"ok": true}'})
        now = datetime.now(UTC)
        sha256 = hashlib.sha256(archive).hexdigest()
        expanded = validate_archive(
            archive, max_bytes=1_000_000, max_expanded_bytes=2_000_000
        )
        thread_id = str(missing.id)
        missing.trigger_event_details = {
            **(missing.trigger_event_details or {}),
            "_session_thread_id": thread_id,
        }
        manifest = {
            "version": 1,
            "kind": "evidence",
            "execution_id": str(missing.id),
            "thread_id": thread_id,
            "sha256": sha256,
            "size_bytes": len(archive),
            "expanded_bytes": expanded,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "metadata": {},
        }
        flow_artifact.store(
            db_session,
            values={
                "account_id": missing.flow.account_id,
                "flow_id": missing.flow_id,
                "thread_id": thread_id,
                "execution_id": missing.id,
                "kind": "evidence",
                "manifest": manifest,
                "manifest_sha256": manifest_digest(manifest),
                "ciphertext": _get_fernet().encrypt(archive),
                "availability": "available",
                "expires_at": now + timedelta(hours=1),
            },
            quota_bytes=50_000_000,
        )
        assert (
            controller_checkout_sha(
                missing, db=db_session, account_id=test_user.account_id
            )
            is None
        )
        present = _execution(db_session, audit)
        _store_evidence(db_session, present, sha=SHA)
        assert (
            controller_checkout_sha(
                present, db=db_session, account_id=test_user.account_id
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
