"""Deterministic dossier manifests. No invented human approvals."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.services.product_dossier import (
    DOSSIER_MANIFEST_SCHEMA,
    build_dossier_manifest,
    content_digest,
    platform_approvals_from_records,
)
from preloop.services.product_provenance import (
    PRODUCT_PROVENANCE_SCHEMA,
    RuntimeProvenanceFacts,
    validate_product_provenance,
)

EXECUTION = "11111111-1111-4111-8111-111111111111"
FIRMWARE = "https://github.com/example/firmware.git"
SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_manifest_is_deterministic_and_redacts_secrets() -> None:
    first = build_dossier_manifest(
        execution_id=EXECUTION,
        result={
            "schema": "preloop.cra.releaseaudit/v1",
            "verdict": "pass",
            "token": "ghp_thiswouldbeasecretvalue1234567890",
        },
        provenance=None,
        artifact_refs={"audit_report": "evidence/audit-report.md"},
        generated_at=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )
    second = build_dossier_manifest(
        execution_id=EXECUTION,
        result={
            "schema": "preloop.cra.releaseaudit/v1",
            "verdict": "pass",
            "token": "ghp_thiswouldbeasecretvalue1234567890",
        },
        provenance=None,
        artifact_refs={"audit_report": "evidence/audit-report.md"},
        generated_at=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )
    assert first == second
    assert first["schema"] == DOSSIER_MANIFEST_SCHEMA
    assert first["result"]["token"] == "[REDACTED]"
    assert first["source_inputs"]["status"] == "legacy_unmapped"
    assert first["evidence"]["kind"] == "evidence"
    assert first["evidence"]["retained"] is False
    assert first["evidence"]["integrity_verified"] is False
    assert "blob_storage" not in first
    assert "evidence_integration" not in first
    assert first["digests"]["raw_result_digest"] == content_digest(
        {
            "schema": "preloop.cra.releaseaudit/v1",
            "verdict": "pass",
            "token": "[REDACTED]",
        }
    )
    assert (
        first["digests"]["annotated_result_digest"]
        == first["digests"]["raw_result_digest"]
    )
    assert "dossier_manifest" not in first["result"]


def test_platform_approvals_ignore_agent_reviewer_and_keep_ids() -> None:
    record = SimpleNamespace(
        id=UUID(EXECUTION),
        status="approved",
        tool_name="request_approval",
        decided_by_ai=False,
        auto_approved_reason=None,
        responses=[
            {
                "user_id": "22222222-2222-4222-8222-222222222222",
                "decision": "approved",
            }
        ],
        resolved_at=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )
    copied = platform_approvals_from_records([record])
    assert copied[0]["id"] == EXECUTION
    assert copied[0]["reviewer_user_id"] == "22222222-2222-4222-8222-222222222222"
    assert copied[0]["source"] == "platform_approval_request"
    assert copied[0]["decided_by_human"] is True
    agent_only = SimpleNamespace(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        status="approved",
        tool_name="request_approval",
        decided_by_ai=True,
        auto_approved_reason=None,
        responses=[],
        resolved_at=None,
    )
    ai = platform_approvals_from_records([agent_only])
    assert ai[0]["decided_by_human"] is False
    assert ai[0]["reviewer_user_id"] is None


def test_manifest_includes_verified_mapping_not_agent_sha_attestation() -> None:
    from preloop.services.product_provenance import sha256_digest

    artifact = b'{"spdxVersion":"SPDX-2.3","name":"example-product"}'
    provenance = validate_product_provenance(
        {
            "schema": PRODUCT_PROVENANCE_SCHEMA,
            "product": "example-product",
            "release": "1.4.2",
            "sbom": {
                "digest": sha256_digest(artifact),
                "path": "sbom/image.spdx.json",
            },
            "repositories": [
                {
                    "remote": FIRMWARE,
                    "sha": SHA,
                    "clone_path": "firmware",
                    "role": "code",
                }
            ],
        },
        RuntimeProvenanceFacts(
            authorized_remotes=(FIRMWARE,),
            clone_paths=("firmware",),
            clone_shas={FIRMWARE: SHA},
            sbom_bytes=artifact,
            sbom_path="sbom/image.spdx.json",
        ),
    )
    manifest = build_dossier_manifest(
        execution_id=EXECUTION,
        result={"verdict": "pass", "git": {"commit": SHA}},
        provenance=provenance,
        artifact_refs={"result": "result.json"},
        generated_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    assert provenance is not None
    assert manifest["source_inputs"]["mapping_status"] == "verified"
    assert "not a cryptographic" in manifest["source_inputs"]["attestation"].lower()
    assert manifest["source_inputs"]["repositories"][0]["sha_status"] == "verified"
    assert (
        manifest["digests"]["raw_result_digest"]
        != manifest["digests"]["annotated_result_digest"]
    )
    assert "product_provenance" in manifest["result"]
    assert "dossier_manifest" not in manifest["result"]


def test_manifest_copies_verified_evidence_receipt_not_placeholders() -> None:
    manifest = build_dossier_manifest(
        execution_id=EXECUTION,
        result={"verdict": "pass"},
        provenance=None,
        artifact_refs={},
        generated_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        evidence_receipt={
            "kind": "evidence",
            "status": "available",
            "sha256": "d" * 64,
            "artifact_id": "44444444-4444-4444-8444-444444444444",
            "execution_id": EXECUTION,
            "integrity_verified": True,
            "retention_hours": 720,
        },
    )
    assert manifest["evidence"]["kind"] == "evidence"
    assert manifest["evidence"]["sha256"] == "d" * 64
    assert manifest["evidence"]["retained"] is True
    assert manifest["evidence"]["integrity_verified"] is True
    assert "evidence_workstream" not in str(manifest)
    assert manifest["evidence"]["object_lock"] is False
