"""A derivable verdict label is corrected and recorded, never thrown away.

Round 2 of the CRA dogfood lost a complete 004 audit because the model wrote
``pass_with_findings`` next to its own ``minimum_elements.passed: false``.
Every check had run, the evidence pack verified, and the platform already knew
the right label. These tests pin the repair and, more importantly, its limits:
the platform escalates a verdict, it never softens one, and it never touches a
measurement.
"""

from __future__ import annotations

from typing import Any

from preloop.cra.persist import apply_cra_persist_boundary
from preloop.cra.repair import (
    CORRECTED_BY,
    VERDICT_CORRECTED_FIELD,
    release_verdict_floor,
    sbom_verdict_floor,
    verdict_corrections,
)
from preloop.cra.schemas import INVALID_ERROR

from .conftest import clone


class TestSbomVerdictFloor:
    """What the SBOM body itself supports."""

    def test_failed_minimum_elements_forces_fail(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 2"]}
        floor, reason = sbom_verdict_floor(payload)
        assert floor == "fail"
        assert "minimum_elements.passed=False" in reason

    def test_invalid_sbom_forces_fail(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["valid"] = False
        floor, _ = sbom_verdict_floor(payload)
        assert floor == "fail"

    def test_license_flags_force_findings(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        floor, reason = sbom_verdict_floor(payload)
        assert floor == "pass_with_findings"
        assert "license flags" in reason

    def test_coverage_gap_forces_findings(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["license_flags"] = []
        floor, reason = sbom_verdict_floor(payload)
        assert floor == "pass_with_findings"
        assert "pct_with_license" in reason

    def test_clean_body_forces_nothing(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["license_flags"] = []
        payload["coverage"] = {
            "components": 3,
            "pct_with_version": 100.0,
            "pct_with_license": 100.0,
            "pct_with_identifier": 100.0,
            "unmatched_vs_build": None,
        }
        assert sbom_verdict_floor(payload) == (None, "")


class TestReleaseVerdictFloor:
    def test_failed_sbom_forces_fail(self, releaseaudit_result: dict[str, Any]) -> None:
        payload = clone(releaseaudit_result)
        payload["sbom_audit"]["verdict"] = "fail"
        floor, reason = release_verdict_floor(payload)
        assert floor == "fail"
        assert reason == "sbom_audit.verdict is fail"

    def test_failed_gate_forces_fail(self, releaseaudit_result: dict[str, Any]) -> None:
        payload = clone(releaseaudit_result)
        payload["vuln_scan"]["gate"]["passed"] = False
        floor, reason = release_verdict_floor(payload)
        assert floor == "fail"
        assert reason == "vuln_scan.gate.passed is false"

    def test_skipped_check_forces_findings(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        floor, reason = release_verdict_floor(clone(releaseaudit_result))
        assert floor == "pass_with_findings"
        assert reason == "a check was skipped"


class TestCorrectionIsAnEscalationOnly:
    def test_the_round_two_case_is_corrected(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 2"]}
        corrected, corrections = verdict_corrections(payload)

        assert [item.corrected for item in corrections] == ["fail"]
        assert corrected["verdict"] == "fail"
        assert corrected[VERDICT_CORRECTED_FIELD] == [
            {
                "path": "result.verdict",
                "submitted": "pass_with_findings",
                "corrected": "fail",
                "reason": "valid=True, minimum_elements.passed=False",
                "corrected_by": CORRECTED_BY,
            }
        ]

    def test_a_fail_is_never_softened(self, sbomaudit_result: dict[str, Any]) -> None:
        """The platform does not clear a release it was handed as failing."""
        payload = clone(sbomaudit_result)
        payload["verdict"] = "fail"
        corrected, corrections = verdict_corrections(payload)
        assert corrections == []
        assert corrected is payload

    def test_an_incomplete_run_is_not_repaired(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["verdict"] = "error"
        payload["valid"] = False
        _, corrections = verdict_corrections(payload)
        assert corrections == []

    def test_measurements_are_left_alone(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 2"]}
        corrected, _ = verdict_corrections(payload)
        for key in ("valid", "minimum_elements", "coverage", "license_flags", "checks"):
            assert corrected[key] == payload[key]

    def test_the_submitted_payload_is_not_mutated(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["valid"] = False
        verdict_corrections(payload)
        assert payload["verdict"] == "pass_with_findings"
        assert VERDICT_CORRECTED_FIELD not in payload

    def test_a_nested_sbom_fail_raises_the_overall_verdict(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["sbom_audit"]["minimum_elements"] = {
            "passed": False,
            "missing": ["supplier: 1"],
        }
        corrected, corrections = verdict_corrections(payload)

        assert [item.path for item in corrections] == [
            "result.sbom_audit.verdict",
            "result.verdict",
        ]
        assert corrected["sbom_audit"]["verdict"] == "fail"
        assert corrected["verdict"] == "fail"

    def test_non_cra_payloads_are_untouched(self) -> None:
        payload = {"status": "success", "verdict": "pass"}
        corrected, corrections = verdict_corrections(payload)
        assert corrections == []
        assert corrected is payload


class TestPersistBoundary:
    def test_a_complete_audit_survives_a_wrong_label(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 2"]}

        decision = apply_cra_persist_boundary(payload)

        assert not decision.invalid
        assert decision.fail_closed_status is None
        assert decision.artifact is not None
        assert decision.artifact["verdict"] == "fail"
        assert decision.artifact[VERDICT_CORRECTED_FIELD][0]["submitted"] == (
            "pass_with_findings"
        )
        assert decision.artifact["coverage"] == payload["coverage"]
        assert any(
            "verdict corrected" in item for item in decision.validation.advisories
        )

    def test_a_second_defect_still_fails_closed(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        """The label is repairable; the rest of the contract is not."""
        payload = clone(sbomaudit_result)
        payload["minimum_elements"] = {"passed": False, "missing": ["supplier: 2"]}
        payload["coverage"]["components"] = "three"

        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        assert decision.artifact is not None
        assert decision.artifact["error"] == INVALID_ERROR
        assert decision.artifact["raw"]["verdict"] == "pass_with_findings"
        joined = "; ".join(decision.validation.failures)
        assert "verdict must be fail" not in joined
        assert any("components" in item for item in decision.validation.failures)
        assert all(
            "verdict must be fail" not in item for item in decision.artifact["failures"]
        )

    def test_a_fabricated_fail_still_fails_closed(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["verdict"] = "fail"

        decision = apply_cra_persist_boundary(payload)

        assert decision.invalid
        assert "do not fabricate a fail" in "; ".join(decision.validation.failures)
