"""Isolated security-maintenance authority tests against real CRUD records."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_security_maintenance, flow_artifact
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.security_maintenance import item_identity_key
from preloop.schemas.security_maintenance import (
    ApprovalDecisionRequest,
    ResumeRequest,
    ScanFinding,
    ScanIngestRequest,
    SupportedReleaseCreate,
)
from preloop.services.flow_artifacts import manifest_digest, validate_archive
from preloop.services.security_maintenance import SecurityMaintenanceService
from preloop.services.security_maintenance_refs import (
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
from backend.tests.services.test_flow_artifacts import archive_with

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

SHA = "a" * 40
TREE = "b" * 40
REPO_URL = "https://github.com/example/project.git"
PR_URL = "https://github.com/example/project/pull/7"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cra"

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
        account_id=test_user.account_id, name="security-maintenance"
    )
    db_session.add(workflow)
    db_session.flush()
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
    return service, project, workflow, implementer, audit, tracker


async def _release(world, test_user):
    service, project, workflow, implementer, audit, _tracker = world
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


def _store_evidence(db_session, execution, *, expires=None, digest=None) -> bytes:
    archive = archive_with("result.json", b'{"ok": true}')
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
    return payload


@pytest.fixture
def world(db_session, test_user):
    return _world(db_session, test_user)


class TestEvidenceAuthority:
    def test_caller_available_and_digest_do_not_grant(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
        execution = _execution(db_session, implementer, result={"status": "success"})
        publication = publication_ref_from_execution(execution, flow=implementer)
        passed, reason = tests_passed(
            execution, flow=implementer, publication=publication
        )
        assert passed is False
        assert reason == "trusted_verification_missing"

    def test_observe_only_and_agent_publication_fail(self, db_session, world) -> None:
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        _service, _project, _workflow, implementer, _audit, _tracker = world
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
        service, _project, _workflow, _implementer, audit, _tracker = world
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
        _store_evidence(db_session, execution)
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
        )
        assert missing_sha.accepted is False
        assert missing_sha.reason == "missing_checked_out_sha"

    def test_unknown_cra_schema_and_blind_scan_are_rejected(
        self, db_session, world, test_user
    ) -> None:
        _service, _project, _workflow, _implementer, audit, _tracker = world
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
        )
        assert blind.accepted is False

    def test_screened_absence_accepts_when_validator_allows(
        self, db_session, world, test_user
    ) -> None:
        pytest.importorskip("preloop.cra.validate")
        _service, _project, _workflow, _implementer, audit, _tracker = world
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
                ScanIngestRequest(
                    product_key="missing",
                    release_key="0",
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001", component_id="libexample"
                        )
                    ],
                )
            )
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            first = await service.ingest_scan(
                ScanIngestRequest(
                    product_key="example-widget",
                    release_key="1.2",
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001", component_id="libexample"
                        )
                    ],
                )
            )
            second = await service.ingest_scan(
                ScanIngestRequest(
                    product_key="example-widget",
                    release_key="1.2",
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
        service, _project, _workflow, implementer, audit, _tracker = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(
                ScanIngestRequest(
                    product_key="example-widget",
                    release_key="1.2",
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001", component_id="libexample"
                        )
                    ],
                )
            )
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
        pytest.importorskip("preloop.cra.validate")
        service, _project, _workflow, implementer, audit, _tracker = world
        await _release(world, test_user)
        with patch(
            "preloop.services.issue_lifecycle_worker.dispatch_lifecycle_execution",
            new_callable=AsyncMock,
        ):
            ingested = await service.ingest_scan(
                ScanIngestRequest(
                    product_key="example-widget",
                    release_key="1.2",
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001", component_id="libexample"
                        )
                    ],
                )
            )
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
            assert approved["state"] in {"reaudit_pending", "reauditing"}
            db_session.refresh(item)
            recheck = crud_security_maintenance.get_execution(
                db_session,
                account_id=test_user.account_id,
                execution_id=item.recheck_execution_id,
            )
            recheck.status = "SUCCEEDED"
            recheck.trigger_event_details = {
                **(recheck.trigger_event_details or {}),
                "_session_thread_id": str(recheck.id),
                "payload": {
                    "sha": SHA,
                    "security_maintenance": (recheck.trigger_event_details or {})
                    .get("payload", {})
                    .get("security_maintenance"),
                },
            }
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
            ingested = await service.ingest_scan(
                ScanIngestRequest(
                    product_key="example-widget",
                    release_key="1.2",
                    findings=[
                        ScanFinding(
                            advisory_id="CVE-2024-0001", component_id="libexample"
                        )
                    ],
                )
            )
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


def test_execution_api_key_is_rejected(db_session, test_user, world) -> None:
    from fastapi import HTTPException

    from preloop.api.endpoints.security_maintenance import _reject_execution_api_key

    _service, _project, _workflow, implementer, _audit, _tracker = world
    execution = _execution(db_session, implementer)
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="execution-key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_execution_id": str(execution.id)},
        commit=False,
    )

    class _Request:
        headers = {"authorization": f"Bearer {token}"}

    item = {
        "implementation_execution_id": str(execution.id),
        "recheck_execution_id": None,
    }
    with pytest.raises(HTTPException) as exc:
        _reject_execution_api_key(_Request(), db_session, test_user, item)
    assert exc.value.status_code == 403
    assert "execution_api_key_cannot_approve" in str(exc.value.detail)
