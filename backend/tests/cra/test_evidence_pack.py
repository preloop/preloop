"""Evidence pack result binding compares complete sanitized agent JSON."""

from __future__ import annotations

from typing import Any

import pytest

from preloop.cra.evidence_pack import (
    EvidencePackError,
    accept_evidence_archive,
    archive_sha256,
    results_bind_content,
)

from .conftest import clone, make_evidence_archive


def _accept(archive: bytes, api_result: dict[str, Any], execution_id: str = "exec-1"):
    return accept_evidence_archive(
        archive,
        headers={"content-type": "application/gzip"},
        execution_id=execution_id,
        api_result=api_result,
    )


def test_identical_results_bind(sbomaudit_result: dict[str, Any]) -> None:
    results_bind_content(sbomaudit_result, clone(sbomaudit_result))


def test_controller_annotations_do_not_break_bind(
    sbomaudit_result: dict[str, Any],
) -> None:
    api = clone(sbomaudit_result)
    api["trusted_publication"] = {"url": "https://example.com/pr/1"}
    api["verification"] = {"status": "passed", "source": "sandbox_log"}
    api["provenance"] = {"controller": True}
    api["dossier"] = {"id": "dossier-1"}
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"schema": "preloop.cra.dossier_manifest/v1"}
    api["container_termination"] = {"exit_code": 0}
    results_bind_content(api, clone(sbomaudit_result))
    archive = make_evidence_archive(sbomaudit_result)
    packed = _accept(archive, api)
    assert packed is not None
    assert packed["schema"] == sbomaudit_result["schema"]


def test_actual_controller_annotations_do_not_hide_decision_change(
    duediligence_result: dict[str, Any],
) -> None:
    api = clone(duediligence_result)
    api["product_provenance"] = {
        "schema": "preloop.cra.product_provenance/v1",
        "mapping_status": "verified",
    }
    api["dossier_manifest"] = {"digest": "abc"}
    results_bind_content(api, clone(duediligence_result))
    packed = clone(duediligence_result)
    packed["decision"] = dict(packed["decision"])
    packed["decision"]["outcome"] = "rejected"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(api, packed)
    archive = make_evidence_archive(packed)
    with pytest.raises(EvidencePackError, match="content does not match"):
        _accept(archive, api)


def test_unknown_agent_field_is_bound(sbomaudit_result: dict[str, Any]) -> None:
    packed = clone(sbomaudit_result)
    packed["agent_extra"] = "invented"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(sbomaudit_result, packed)


def test_rejected_decision_does_not_bind_to_accepted(
    duediligence_result: dict[str, Any],
) -> None:
    packed = clone(duediligence_result)
    packed["decision"] = dict(packed["decision"])
    packed["decision"]["outcome"] = "rejected"
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(duediligence_result, packed)
    archive = make_evidence_archive(packed)
    with pytest.raises(EvidencePackError, match="content does not match"):
        _accept(archive, duediligence_result)


def test_gate_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["vuln_scan"] = dict(packed["vuln_scan"])
    packed["vuln_scan"]["gate"] = dict(packed["vuln_scan"]["gate"])
    packed["vuln_scan"]["gate"]["passed"] = False
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_waiver_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["vuln_scan"] = dict(packed["vuln_scan"])
    packed["vuln_scan"]["gate"] = dict(packed["vuln_scan"]["gate"])
    packed["vuln_scan"]["gate"]["waivers_applied"] = [
        {
            "id": "CVE-2024-0001",
            "reason": "invented",
            "author": "agent@example.com",
            "date": "2026-08-20",
        }
    ]
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_gap_register_mutation_is_bound(releaseaudit_result: dict[str, Any]) -> None:
    packed = clone(releaseaudit_result)
    packed["gap_register"] = {"items": [], "history_rows": []}
    with pytest.raises(EvidencePackError, match="content does not match"):
        results_bind_content(releaseaudit_result, packed)


def test_legacy_pack_without_result_json_uses_digest(
    sbomaudit_result: dict[str, Any],
) -> None:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        note = b"# evidence\n"
        info = tarfile.TarInfo("evidence/sbom-verify.md")
        info.size = len(note)
        tar.addfile(info, io.BytesIO(note))
    archive = buf.getvalue()
    digest = archive_sha256(archive)
    packed = accept_evidence_archive(
        archive,
        headers={
            "content-type": "application/gzip",
            "x-preloop-evidence-sha256": digest,
        },
        execution_id="exec-1",
        api_result=sbomaudit_result,
    )
    assert packed is None
