"""Numeric strings on finding scores are coerced, not discarded.

A release audit can finish its checks, pass its severity gate and still
be stored as a failed execution when ``epss`` was written as a JSON
string. The platform coerces a finite in-range string to a number,
records the reported string, and re-validates. A string that does not
parse stays a contract failure.
"""

from __future__ import annotations

from typing import Any

from preloop.cra.evidence_pack import (
    accept_evidence_archive,
    archive_sha256,
    ensure_pack_manifest,
)
from preloop.cra.persist import apply_cra_persist_boundary
from preloop.cra.repair import (
    VERDICT_CORRECTED_FIELD,
    apply_coerced_finding_scores,
)
from preloop.cra.schemas import SCHEMA_VULNSCAN_V1
from preloop.services.flow_artifacts import evidence_receipt

from .conftest import clone, make_evidence_archive

REPORTED_EPSS = "0.023840000"
STORED_EPSS = float(REPORTED_EPSS)


def _finding(
    finding_id: str,
    *,
    epss: Any = None,
    cvss: Any = 4.0,
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "pkg": "libexample",
        "version": "1.0.0",
        "severity": "medium",
        "cvss": cvss,
        "epss": epss,
        "kev": False,
        "fix_version": None,
        "vex_status": None,
        "sources": ["osv_purl"],
        "match_kind": "database",
        "aliases": None,
    }


def _with_findings(
    payload: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    document = clone(payload)
    document["vuln_scan"]["findings"] = findings
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for item in findings:
        severity = item["severity"]
        if severity in counts:
            counts[severity] += 1
    document["vuln_scan"]["counts_by_severity"] = counts
    return document


def _string_epss_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """25 identical numeric-string EPSS values, then one null."""
    findings = [
        _finding(f"CVE-2024-{1000 + index}", epss=REPORTED_EPSS) for index in range(25)
    ]
    findings.append(_finding("CVE-2024-1999", epss=None))
    return _with_findings(payload, findings)


def _score_records(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in artifact[VERDICT_CORRECTED_FIELD]
        if item["path"].endswith(".epss") or item["path"].endswith(".cvss")
    ]


class TestEpssNumericStrings:
    def test_twenty_five_strings_and_one_null_are_coerced(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _string_epss_batch(releaseaudit_result)
        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        findings = artifact["vuln_scan"]["findings"]
        assert len(findings) == 26
        for item in findings[:25]:
            assert item["epss"] == STORED_EPSS
            assert type(item["epss"]) is float
        assert findings[25]["epss"] is None
        assert payload["vuln_scan"]["findings"][0]["epss"] == REPORTED_EPSS

        recorded = _score_records(artifact)
        assert len(recorded) == 25
        assert recorded[0]["submitted"] == REPORTED_EPSS
        assert recorded[0]["corrected"] == str(STORED_EPSS)
        assert recorded[0]["path"] == "result.vuln_scan.findings[0].epss"
        assert recorded[24]["path"] == "result.vuln_scan.findings[24].epss"
        assert artifact["verdict"] == "pass_with_findings"
        assert artifact["vuln_scan"]["gate"]["passed"] is True

    def test_non_numeric_string_names_the_finding(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _with_findings(
            releaseaudit_result,
            [_finding("CVE-2024-1000", epss="not-a-score")],
        )
        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        joined = "; ".join(decision.validation.failures)
        assert "result.vuln_scan.findings[0].epss" in joined
        assert "not-a-score" in joined
        assert decision.artifact is not None
        assert decision.artifact["raw"]["vuln_scan"]["findings"][0]["epss"] == (
            "not-a-score"
        )

    def test_out_of_range_string_is_not_coerced(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _with_findings(
            releaseaudit_result,
            [_finding("CVE-2024-1000", epss="1.5")],
        )
        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        joined = "; ".join(decision.validation.failures)
        assert "findings[0].epss" in joined
        assert "'1.5'" in joined

    def test_coerced_document_round_trips_through_persist_and_receipts(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _string_epss_batch(releaseaudit_result)
        decision = apply_cra_persist_boundary(payload)
        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None

        archive = ensure_pack_manifest(make_evidence_archive(artifact))
        digest = archive_sha256(archive)
        receipt = evidence_receipt(
            status="available",
            execution_id="exec-score",
            transport="direct_upload",
            archive=archive,
            integrity_verified=True,
        )
        assert receipt["sha256"] == digest
        assert receipt["size_bytes"] == len(archive)

        packed = accept_evidence_archive(
            archive,
            headers={
                "content-type": "application/gzip",
                "x-preloop-evidence-sha256": digest,
                "x-preloop-evidence-status": "available",
            },
            execution_id="exec-score",
            api_result=artifact,
            receipt=receipt,
        )
        assert packed is not None
        assert packed["vuln_scan"]["findings"][0]["epss"] == STORED_EPSS
        assert packed["vuln_scan"]["findings"][25]["epss"] is None
        score_rows = [
            item
            for item in packed[VERDICT_CORRECTED_FIELD]
            if str(item.get("path", "")).endswith(".epss")
        ]
        assert len(score_rows) == 25
        assert score_rows[0]["submitted"] == REPORTED_EPSS


class TestCvssNumericStrings:
    def test_in_range_string_is_coerced(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _with_findings(
            releaseaudit_result,
            [_finding("CVE-2024-1000", cvss="7.5")],
        )
        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        assert artifact["vuln_scan"]["findings"][0]["cvss"] == 7.5
        recorded = _score_records(artifact)
        assert len(recorded) == 1
        assert recorded[0]["path"] == "result.vuln_scan.findings[0].cvss"
        assert recorded[0]["submitted"] == "7.5"
        assert recorded[0]["corrected"] == "7.5"
        assert artifact["vuln_scan"]["gate"]["passed"] is True

    def test_high_score_string_does_not_clear_the_gate(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = _with_findings(
            releaseaudit_result,
            [_finding("CVE-2024-1000", cvss="9.8")],
        )
        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        joined = "; ".join(decision.validation.failures).lower()
        assert "gate" in joined or "cvss" in joined
        assert decision.artifact is not None
        assert decision.artifact["raw"]["vuln_scan"]["findings"][0]["cvss"] == "9.8"


class TestScoreCoercionUnit:
    def test_vulnscan_path_and_boundaries(self) -> None:
        payload = {
            "schema": SCHEMA_VULNSCAN_V1,
            "findings": [
                {"epss": "0", "cvss": "10"},
                {"epss": "1", "cvss": "0.0"},
                {"epss": None, "cvss": 4},
            ],
        }
        corrected, corrections = apply_coerced_finding_scores(payload)

        assert corrected["findings"][0]["epss"] == 0
        assert corrected["findings"][0]["cvss"] == 10
        assert corrected["findings"][1]["epss"] == 1
        assert corrected["findings"][1]["cvss"] == 0
        assert corrected["findings"][2]["epss"] is None
        assert corrected["findings"][2]["cvss"] == 4
        paths = [item.path for item in corrections]
        assert paths == [
            "result.findings[0].cvss",
            "result.findings[0].epss",
            "result.findings[1].cvss",
            "result.findings[1].epss",
        ]
        assert payload["findings"][0]["epss"] == "0"

    def test_one_unparseable_string_blocks_every_coercion(self) -> None:
        payload = {
            "schema": SCHEMA_VULNSCAN_V1,
            "findings": [
                {"epss": REPORTED_EPSS, "cvss": 4.0},
                {"epss": "not-a-score", "cvss": 4.0},
            ],
        }
        corrected, corrections = apply_coerced_finding_scores(payload)

        assert corrections == []
        assert corrected is payload
        assert corrected["findings"][0]["epss"] == REPORTED_EPSS
