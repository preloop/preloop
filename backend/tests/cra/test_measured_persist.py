"""Persist uses the platform SBOM measurement and derived severity counts."""

from __future__ import annotations

import base64
import json
from typing import Any

from preloop.cra.persist import apply_cra_persist_boundary
from preloop.cra.repair import VERDICT_CORRECTED_FIELD
from preloop.cra.sbom_measure import MEASURED_FIELD

from .conftest import clone


def _seed(raw: bytes, path: str = "sbom/example.cdx.json") -> dict[str, Any]:
    return {
        "workspace_files": [
            {"path": path, "content_base64": base64.b64encode(raw).decode()}
        ]
    }


def _cdx_missing_supplier() -> bytes:
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "timestamp": "2026-01-01T00:00:00Z",
            "authors": [{"name": "Example Author"}],
            "component": {
                "bom-ref": "app",
                "name": "example-app",
                "supplier": {"name": "Root Supplier"},
            },
        },
        "components": [
            {
                "bom-ref": "lib-a",
                "name": "lib-a",
                "version": "1.0.0",
                "purl": "pkg:generic/lib-a@1.0.0",
            }
        ],
        "dependencies": [{"ref": "lib-a", "dependsOn": []}],
    }
    return json.dumps(document).encode()


def _cdx_complete() -> bytes:
    document = json.loads(_cdx_missing_supplier())
    document["components"][0]["supplier"] = {"name": "Example Supplier"}
    return json.dumps(document).encode()


def _finding(finding_id: str, severity: str) -> dict[str, Any]:
    return {
        "id": finding_id,
        "pkg": "libexample",
        "version": "1.0.0",
        "severity": severity,
        "cvss": 5.0,
        "epss": None,
        "kev": False,
        "fix_version": None,
        "vex_status": None,
        "sources": ["osv_purl"],
        "match_kind": "database",
        "aliases": None,
    }


class TestMeasuredMinimumElements:
    def test_lenient_claim_is_replaced_and_verdict_fails(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        decision = apply_cra_persist_boundary(
            payload, trigger_payload=_seed(_cdx_missing_supplier())
        )

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        assert artifact["verdict"] == "fail"
        assert artifact["minimum_elements"]["passed"] is False
        assert "supplier" in artifact["minimum_elements"]["missing"]
        assert artifact[MEASURED_FIELD]["passed"] is False
        claim = artifact[VERDICT_CORRECTED_FIELD][0]["agent_claim"]
        assert claim["passed"] is True
        assert payload["minimum_elements"]["passed"] is True
        assert payload["verdict"] == "pass_with_findings"

    def test_stricter_agent_claim_is_kept(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 1"]}
        payload["verdict"] = "fail"
        decision = apply_cra_persist_boundary(
            payload, trigger_payload=_seed(_cdx_complete())
        )

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        assert artifact["minimum_elements"] == {
            "passed": False,
            "missing": ["supplier: 1"],
        }
        assert artifact[MEASURED_FIELD]["passed"] is True
        assert artifact["verdict"] == "fail"

    def test_missing_seeds_skip_measurement(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        assert artifact["verdict"] == "pass_with_findings"
        assert artifact["minimum_elements"]["passed"] is True
        assert artifact[MEASURED_FIELD]["status"] == "skipped"
        assert "no SBOM seeds" in artifact[MEASURED_FIELD]["reason"]


class TestDerivedSeverityCounts:
    def test_fabricated_medium_count_is_derived(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        severities = [
            "high",
            "medium",
            "medium",
            "medium",
            "medium",
            "medium",
            "medium",
            "low",
            "unknown",
        ]
        payload["vuln_scan"]["findings"] = [
            _finding(f"CVE-2024-000{index}", severity)
            for index, severity in enumerate(severities)
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 0,
            "high": 1,
            "medium": 7,
            "low": 1,
            "unknown": 1,
        }
        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        counts = artifact["vuln_scan"]["counts_by_severity"]
        assert counts == {
            "critical": 0,
            "high": 1,
            "medium": 6,
            "low": 1,
            "unknown": 1,
        }
        recorded = [
            item
            for item in artifact[VERDICT_CORRECTED_FIELD]
            if item["path"].endswith("counts_by_severity.medium")
        ]
        assert recorded[0]["submitted"] == "7"
        assert recorded[0]["corrected"] == "6"
        assert len(artifact["vuln_scan"]["findings"]) == 9

    def test_another_failure_still_fails_closed(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["vuln_scan"]["findings"] = [_finding("CVE-2024-0001", "medium")]
        payload["vuln_scan"]["counts_by_severity"]["medium"] = 7
        payload["sbom_audit"]["coverage"]["components"] = "three"
        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        joined = "; ".join(decision.validation.failures)
        assert "components" in joined
        assert decision.artifact is not None
        assert (
            decision.artifact["raw"]["vuln_scan"]["counts_by_severity"]["medium"] == 7
        )


class TestNestedSbomFloor:
    def test_failed_minimum_elements_fail_a_passing_gate(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["sbom_audit"]["minimum_elements"] = {
            "passed": False,
            "missing": ["supplier"],
        }
        payload["sbom_audit"]["license_flags"] = []
        payload["sbom_audit"]["coverage"]["pct_with_version"] = 100
        payload["sbom_audit"]["coverage"]["pct_with_license"] = 100
        payload["sbom_audit"]["coverage"]["pct_with_identifier"] = 100
        payload["sbom_audit"]["verdict"] = "pass"
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["findings"] = []
        payload["checks"] = [
            {
                "name": "build_cross_check",
                "passed": True,
                "skipped": False,
                "details": "matched",
            }
        ]
        payload["verdict"] = "pass"
        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        artifact = decision.artifact
        assert artifact is not None
        assert artifact["sbom_audit"]["verdict"] == "fail"
        assert artifact["verdict"] == "fail"
        assert artifact["vuln_scan"]["gate"]["passed"] is True
