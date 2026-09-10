"""Drift that was measured is drift that is readable by machine.

Round 2's release audit wrote a complete evidence/drift-report.md, named it
under artifacts, and left result.drift null. The pack said the component count
fell from 277 to 261; the envelope said nothing at all. These tests hold the
two together, in a finished audit and in the incompletion envelope of a run
that died waiting for a waiver decision.
"""

from __future__ import annotations

from typing import Any

from preloop.cra.schemas import SCHEMA_RELEASEAUDIT_V1, SCHEMA_SBOMAUDIT_V1
from preloop.cra.validate import validate_cra_result

from .conftest import clone

DRIFT = {
    "baseline": {
        "schema": "preloop.cra.sbomaudit/v1",
        "run_at": "2026-09-08T00:30:29Z",
        "build_ref": "287f3410867199a0fe789145eb34d5028b69a133",
    },
    "sbom_changes": {
        "added": [],
        "removed": ["passlib", "pip", "setuptools"],
        "upgraded": [],
        "license_changes": [],
    },
    "new_vulns": ["GO-2026-5932"],
    "resolved_vulns": [],
    "new_kev": [],
    "gate_transitions": [],
    "alert": False,
}


def _envelope(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA_RELEASEAUDIT_V1,
        "flow": "release-security-audit",
        "run_at": "2026-09-09T12:00:00Z",
        "regime_profile": "cra",
        "verdict": "error",
        "incomplete": {
            "reason": (
                "A required human decision on the waiver question did not "
                "arrive before the execution limit."
            ),
            "stage": "vuln_scan",
        },
        "disclaimer": (
            "Machine-generated evidence for conformity assessment support. "
            "Not a conformity assessment, certification, or legal advice."
        ),
    }
    payload.update(extra)
    return payload


class TestFinishedAudit:
    def test_the_fixture_carries_its_drift(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        assert validate_cra_result(clone(releaseaudit_result)).ok

    def test_a_written_report_with_a_null_field_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        """The exact shape round 2 produced."""
        payload = clone(releaseaudit_result)
        payload["drift"] = None

        result = validate_cra_result(payload)

        assert not result.ok
        assert any(
            "drift-report.md was written and the machine-readable field is null" in item
            for item in result.failures
        )

    def test_no_report_and_no_drift_is_fine(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["drift"] = None
        payload["artifacts"]["drift_report"] = None

        assert validate_cra_result(payload).ok

    def test_drift_without_its_report_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["artifacts"]["drift_report"] = None

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("drift_report must name the report" in i for i in result.failures)

    def test_an_unidentified_baseline_is_not_drift(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["drift"]["baseline"] = {
            "schema": None,
            "run_at": None,
            "build_ref": None,
        }

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("baseline.schema must name" in item for item in result.failures)

    def test_alert_must_be_stated(self, releaseaudit_result: dict[str, Any]) -> None:
        payload = clone(releaseaudit_result)
        del payload["drift"]["alert"]

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("drift.alert must be a boolean" in item for item in result.failures)

    def test_vulnerability_lists_hold_ids(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["drift"]["new_kev"] = [{"id": "CVE-2021-44228"}]

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("drift.new_kev[0] must be" in item for item in result.failures)


class TestIncompletionEnvelope:
    def test_a_bare_envelope_still_validates(self) -> None:
        result = validate_cra_result(_envelope())
        assert result.ok
        assert result.incomplete

    def test_the_envelope_may_carry_measured_drift(self) -> None:
        payload = _envelope(
            drift=clone(DRIFT),
            artifacts={"drift_report": "evidence/drift-report.md"},
        )

        result = validate_cra_result(payload)

        assert result.ok, result.failures
        assert result.incomplete
        # Incomplete is incomplete: carrying drift never releases anything.
        assert result.release_denied

    def test_the_envelope_must_carry_the_drift_it_wrote(self) -> None:
        payload = _envelope(artifacts={"drift_report": "evidence/drift-report.md"})

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("machine-readable field is null" in i for i in result.failures)

    def test_drift_in_the_envelope_is_validated_in_full(self) -> None:
        payload = _envelope(
            drift={"baseline": {}, "alert": "yes"},
            artifacts={"drift_report": "evidence/drift-report.md"},
        )

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("baseline.schema must name" in item for item in result.failures)
        assert any("alert must be a boolean" in item for item in result.failures)

    def test_the_envelope_still_refuses_the_audit_body(self) -> None:
        """Drift is the exception, not the opening of a door."""
        payload = _envelope(
            drift=clone(DRIFT),
            artifacts={"drift_report": "evidence/drift-report.md"},
            vuln_scan={"findings": []},
        )

        result = validate_cra_result(payload)

        assert not result.ok

    def test_an_sbom_audit_envelope_has_no_drift(self) -> None:
        payload = _envelope(
            schema=SCHEMA_SBOMAUDIT_V1,
            flow="sbom-verify",
            drift=clone(DRIFT),
            artifacts={"drift_report": "evidence/drift-report.md"},
        )

        result = validate_cra_result(payload)

        assert not result.ok
        assert any("drift is not part of" in item for item in result.failures)
