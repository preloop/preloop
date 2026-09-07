"""Runtime validator for the four CRA result.json schemas."""

from __future__ import annotations

import json
from typing import Any

from preloop.cra.schemas import (
    SCHEMA_DUEDILIGENCE_V1,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
    WAIVE_FINDING_OPERATION,
    expected_cra_schema_from_prompt,
)
from preloop.cra.validate import (
    AUTHORITY_REQUIRED,
    GatePolicy,
    PlatformApproval,
    parse_gate_policy,
    result_claims_authority,
    validate_cra_result,
)

from .conftest import clone


class TestExpectedSchemaFromPrompt:
    def test_required_shape_wins_over_nested_mentions(self) -> None:
        prompt = (
            "Previous result may be preloop.cra.vulnscan/v1.\n"
            "Required shape (preloop.cra.releaseaudit/v1):\n"
            '{ "schema": "preloop.cra.releaseaudit/v1" }'
        )
        assert expected_cra_schema_from_prompt(prompt) == SCHEMA_RELEASEAUDIT_V1

    def test_arbitrary_flow_is_not_expected_cra(self) -> None:
        assert expected_cra_schema_from_prompt("Fix the bug and open a PR.") is None

    def test_unsupported_version_in_required_shape_is_still_expected(self) -> None:
        prompt = 'Required shape (preloop.cra.sbomaudit/v2): { "schema": "..." }'
        assert expected_cra_schema_from_prompt(prompt) == "preloop.cra.sbomaudit/v2"


class TestValidFixtures:
    def test_sbomaudit(self, sbomaudit_result: dict[str, Any]) -> None:
        result = validate_cra_result(sbomaudit_result)
        assert result.ok
        assert result.schema_id == SCHEMA_SBOMAUDIT_V1
        assert result.execution_completed
        assert result.release_denied  # pass_with_findings

    def test_vulnscan(self, vulnscan_result: dict[str, Any]) -> None:
        result = validate_cra_result(vulnscan_result)
        assert result.ok
        assert result.schema_id == SCHEMA_VULNSCAN_V1
        assert result.execution_completed
        assert not result.release_denied  # gate.passed true

    def test_releaseaudit(self, releaseaudit_result: dict[str, Any]) -> None:
        result = validate_cra_result(releaseaudit_result)
        assert result.ok
        assert result.schema_id == SCHEMA_RELEASEAUDIT_V1
        assert result.execution_completed
        assert result.release_denied

    def test_duediligence(self, duediligence_result: dict[str, Any]) -> None:
        result = validate_cra_result(duediligence_result)
        assert result.ok
        assert result.schema_id == SCHEMA_DUEDILIGENCE_V1
        assert result.execution_completed
        assert not result.release_denied


class TestNonCraCompatibility:
    def test_eval_result_is_unchanged(self) -> None:
        payload = {
            "schema": "preloop.eval.result/v1",
            "status": "pass",
            "summary": "ok",
        }
        result = validate_cra_result(payload)
        assert result.skipped
        assert result.ok

    def test_plain_status_object_is_unchanged(self) -> None:
        result = validate_cra_result({"status": "success"})
        assert result.skipped

    def test_none_without_expected_schema_is_unchanged(self) -> None:
        result = validate_cra_result(None)
        assert result.skipped


class TestMalformedNested:
    def test_nested_source_not_object(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["source"] = "spdx"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("source" in item for item in result.failures)

    def test_nested_gate_missing_on_vulnscan(
        self, vulnscan_result: dict[str, Any]
    ) -> None:
        payload = clone(vulnscan_result)
        del payload["gate"]
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("gate" in item for item in result.failures)

    def test_nested_inventory_not_object(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["vuln_scan"]["inventory"] = []
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("inventory" in item for item in result.failures)


class TestMissingAndUnknownSchema:
    def test_expected_cra_missing_result_fails(self) -> None:
        prompt = 'Required shape (preloop.cra.sbomaudit/v1): { "schema": "x" }'
        result = validate_cra_result(None, prompt=prompt)
        assert result.invalid
        assert any("no result.json" in item for item in result.failures)

    def test_expected_cra_missing_schema_field(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        del payload["schema"]
        prompt = "Required shape (preloop.cra.sbomaudit/v1): {}"
        result = validate_cra_result(payload, prompt=prompt)
        assert result.invalid
        assert any("no schema" in item for item in result.failures)

    def test_unsupported_version_fails(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["schema"] = "preloop.cra.sbomaudit/v2"
        result = validate_cra_result(payload)
        assert result.invalid
        assert any("unsupported" in item.lower() for item in result.failures)

    def test_expected_unsupported_version_cannot_evade(self) -> None:
        prompt = "Required shape (preloop.cra.sbomaudit/v2): {}"
        result = validate_cra_result({"status": "success"}, prompt=prompt)
        assert result.invalid


class TestBoolIntGotchas:
    def test_valid_int_one_is_rejected(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["valid"] = 1
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("boolean" in item for item in result.failures)

    def test_gate_passed_one_is_rejected(self, vulnscan_result: dict[str, Any]) -> None:
        payload = clone(vulnscan_result)
        payload["gate"]["passed"] = 1
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("gate.passed" in item for item in result.failures)

    def test_components_bool_is_rejected(self, vulnscan_result: dict[str, Any]) -> None:
        payload = clone(vulnscan_result)
        payload["inventory"]["components"] = True
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("integer" in item for item in result.failures)


class TestInconsistentVerdict:
    def test_sbom_fail_forces_overall_fail(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["sbom_audit"]["valid"] = False
        payload["sbom_audit"]["minimum_elements"]["passed"] = False
        payload["sbom_audit"]["verdict"] = "fail"
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("must be fail" in item for item in result.failures)

    def test_fabricated_overall_fail_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["verdict"] = "fail"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("do not fabricate" in item for item in result.failures)

    def test_completed_fail_is_execution_completed(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        payload["valid"] = False
        payload["minimum_elements"]["passed"] = False
        payload["verdict"] = "fail"
        result = validate_cra_result(payload)
        assert result.ok
        assert result.execution_completed
        assert result.release_denied


class TestPartialCoverage:
    def test_matchable_unmatchable_must_sum(
        self, vulnscan_result: dict[str, Any]
    ) -> None:
        payload = clone(vulnscan_result)
        payload["inventory"]["matchable"] = 0
        payload["inventory"]["unmatchable"] = 0
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("coverage contradiction" in item for item in result.failures)

    def test_missing_pct_is_advisory_by_default(
        self, sbomaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(sbomaudit_result)
        del payload["coverage"]["pct_with_identifier"]
        result = validate_cra_result(payload)
        assert result.ok
        assert result.advisories
        required = validate_cra_result(payload, require_coverage=True)
        assert not required.ok


class TestWaivedFindings:
    def test_agent_asserted_waiver_does_not_pass_gate(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["vuln_scan"]["findings"] = [
            {
                "id": "CVE-2024-0001",
                "pkg": "libexample",
                "version": "1.0",
                "severity": "critical",
                "cvss": 9.8,
                "epss": None,
                "kev": True,
                "fix_version": None,
                "vex_status": None,
                "sources": ["osv_purl"],
                "match_kind": "database",
                "waived": True,
                "aliases": None,
            }
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = False
        payload["vuln_scan"]["gate"]["waivers_applied"] = [
            {
                "id": "CVE-2024-0001",
                "reason": "agent invented this",
                "author": "agent",
                "date": "2026-08-20",
            }
        ]
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("authentic human approval" in item for item in result.failures)

    def test_delivered_waiver_may_pass_gate(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        waiver = {
            "id": "CVE-2024-0001",
            "reason": "Feature not compiled into the shipped image.",
            "author": "release-manager@example.com",
            "date": "2026-08-20",
        }
        payload["vuln_scan"]["findings"] = [
            {
                "id": "CVE-2024-0001",
                "pkg": "libexample",
                "version": "1.0",
                "severity": "critical",
                "cvss": 9.8,
                "epss": None,
                "kev": True,
                "fix_version": None,
                "vex_status": None,
                "sources": ["osv_purl"],
                "match_kind": "database",
                "waived": True,
                "aliases": None,
            }
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = False
        payload["vuln_scan"]["gate"]["waivers_applied"] = [waiver]
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(payload, delivered_waivers=[waiver])
        assert result.ok, result.failures

    def test_approval_id_must_match_platform_record(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        waiver = {
            "id": "CVE-2024-0001",
            "reason": "Feature not compiled into the shipped image.",
            "author": "release-manager@example.com",
            "date": "2026-08-20",
            "approval_id": "not-a-real-approval",
        }
        payload["vuln_scan"]["findings"] = [
            {
                "id": "CVE-2024-0001",
                "pkg": "libexample",
                "version": "1.0",
                "severity": "critical",
                "cvss": 9.8,
                "epss": None,
                "kev": True,
                "fix_version": None,
                "vex_status": None,
                "sources": ["osv_purl"],
                "match_kind": "database",
                "waived": True,
                "aliases": None,
            }
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = False
        payload["vuln_scan"]["gate"]["waivers_applied"] = [waiver]
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(payload, platform_approvals=[])
        assert not result.ok
        assert any("approval_id" in item for item in result.failures)

    def test_forged_approval_id_without_lookup_is_not_authentic(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        waiver = {
            "id": "CVE-2024-0001",
            "reason": "Feature not compiled into the shipped image.",
            "author": "release-manager@example.com",
            "date": "2026-08-20",
            "approval_id": "forged-approval-id",
        }
        payload["vuln_scan"]["findings"] = [
            {
                "id": "CVE-2024-0001",
                "pkg": "libexample",
                "version": "1.0",
                "severity": "critical",
                "cvss": 9.8,
                "epss": None,
                "kev": True,
                "fix_version": None,
                "vex_status": None,
                "sources": ["osv_purl"],
                "match_kind": "database",
                "waived": True,
                "aliases": None,
            }
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = False
        payload["vuln_scan"]["gate"]["waivers_applied"] = [waiver]
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("authentic human approval" in item for item in result.failures)

    def test_claimed_waivers_fail_closed_when_authority_unavailable(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        waiver = {
            "id": "CVE-2024-0001",
            "reason": "Feature not compiled into the shipped image.",
            "author": "release-manager@example.com",
            "date": "2026-08-20",
            "approval_id": "11111111-1111-1111-1111-111111111111",
        }
        payload["vuln_scan"]["findings"] = [
            {
                "id": "CVE-2024-0001",
                "pkg": "libexample",
                "version": "1.0",
                "severity": "critical",
                "cvss": 9.8,
                "epss": None,
                "kev": True,
                "fix_version": None,
                "vex_status": None,
                "sources": ["osv_purl"],
                "match_kind": "database",
                "waived": True,
                "aliases": None,
            }
        ]
        payload["vuln_scan"]["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        payload["vuln_scan"]["gate"]["passed"] = True
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = False
        payload["vuln_scan"]["gate"]["waivers_applied"] = [waiver]
        payload["verdict"] = "pass_with_findings"
        result = validate_cra_result(
            payload, platform_approvals=None, authority=AUTHORITY_REQUIRED
        )
        assert not result.ok
        assert any("unavailable" in item for item in result.failures)


class TestDueDiligenceDecision:
    def test_authenticity_verified_must_be_false(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        payload["evidence"]["ce_declaration"]["authenticity_verified"] = True
        result = validate_cra_result(payload)
        assert not result.ok

    def test_accepted_without_platform_approval_when_lookup_ran(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(payload, platform_approvals=[])
        assert not result.ok
        assert any(
            "request_approval" in item or "platform approval" in item
            for item in result.failures
        )

    def test_matching_platform_approval_records_decision(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="request_approval",
                    operation=payload["decision"]["approval_operation"],
                )
            ],
        )
        assert result.ok, result.failures

    def test_pending_cannot_be_recorded(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        payload["decision"]["outcome"] = "pending"
        payload["verdict"] = "recorded"
        result = validate_cra_result(payload)
        assert not result.ok

    def test_unavailable_authority_fails_closed(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        result = validate_cra_result(
            duediligence_result,
            platform_approvals=None,
            authority=AUTHORITY_REQUIRED,
        )
        assert not result.ok
        assert any("unavailable" in item for item in result.failures)

    def test_ask_user_answer_is_not_a_component_risk_approval(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="ask_user",
                    operation=payload["decision"]["approval_operation"],
                )
            ],
        )
        assert not result.ok
        assert any("request_approval" in item for item in result.failures)

    def test_unrelated_granted_approval_does_not_bind(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="request_approval",
                    operation="Publish pull request for issue 12",
                )
            ],
        )
        assert not result.ok

    def test_missing_operation_cannot_authorize(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="request_approval",
                    operation=None,
                )
            ],
        )
        assert not result.ok

    def test_rejected_approval_cannot_record_accepted(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="declined",
                    tool_name="request_approval",
                    operation=payload["decision"]["approval_operation"],
                )
            ],
        )
        assert not result.ok

    def test_ai_approved_request_cannot_record_accepted(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="request_approval",
                    operation=payload["decision"]["approval_operation"],
                    decided_by_ai=True,
                )
            ],
        )
        assert not result.ok

    def test_auto_approved_request_cannot_record_accepted(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="approved",
                    tool_name="request_approval",
                    operation=payload["decision"]["approval_operation"],
                    auto_approved_reason="bypass",
                )
            ],
        )
        assert not result.ok

    def test_expired_approval_cannot_record_accepted(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload,
            authority=AUTHORITY_REQUIRED,
            platform_approvals=[
                PlatformApproval(
                    id="11111111-1111-1111-1111-111111111111",
                    status="expired",
                    tool_name="request_approval",
                    operation=payload["decision"]["approval_operation"],
                )
            ],
        )
        assert not result.ok

    def test_forged_decision_without_matching_row(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        payload = clone(duediligence_result)
        result = validate_cra_result(
            payload, authority=AUTHORITY_REQUIRED, platform_approvals=[]
        )
        assert not result.ok


class TestSchemaDifferences:
    def test_sbomaudit_rejects_status(self, sbomaudit_result: dict[str, Any]) -> None:
        payload = clone(sbomaudit_result)
        payload["status"] = "success"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("status is not part of" in item for item in result.failures)

    def test_vulnscan_rejects_verdict(self, vulnscan_result: dict[str, Any]) -> None:
        payload = clone(vulnscan_result)
        payload["verdict"] = "pass"
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("verdict is not part of" in item for item in result.failures)


def _kev_finding(
    finding_id: str = "CVE-2024-0001",
    *,
    aliases: list[str] | None = None,
    severity: str = "critical",
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "pkg": "libexample",
        "version": "1.0",
        "severity": severity,
        "cvss": 9.8,
        "epss": None,
        "kev": True,
        "fix_version": None,
        "vex_status": None,
        "sources": ["osv_purl"],
        "match_kind": "database",
        "waived": True,
        "aliases": aliases,
    }


def _waiver(
    finding_id: str = "CVE-2024-0001",
    *,
    reason: str = "Feature not compiled into the shipped image.",
    author: str = "release-manager@example.com",
    date: str = "2026-08-20",
    approval_id: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": finding_id,
        "reason": reason,
        "author": author,
        "date": date,
    }
    if approval_id is not None:
        entry["approval_id"] = approval_id
    return entry


def _release_with_kevs(
    payload: dict[str, Any],
    findings: list[dict[str, Any]],
    waivers: list[Any],
    *,
    passed: bool = True,
    unwaived: list[Any] | None = None,
) -> dict[str, Any]:
    out = clone(payload)
    out["vuln_scan"]["findings"] = findings
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for item in findings:
        severity = item.get("severity")
        if severity in counts:
            counts[str(severity)] += 1
    out["vuln_scan"]["counts_by_severity"] = counts
    out["vuln_scan"]["gate"]["passed"] = passed
    out["vuln_scan"]["gate"]["passed_before_waivers"] = False
    out["vuln_scan"]["gate"]["waivers_applied"] = waivers
    if unwaived is not None:
        out["vuln_scan"]["gate"]["unwaived_failures"] = unwaived
    out["verdict"] = "pass_with_findings"
    return out


class TestUnhashableJsonMembership:
    def test_non_cra_error_list_is_unchanged(self) -> None:
        payload: dict[str, Any] = {"error": []}
        result = validate_cra_result(payload)
        assert result.skipped
        assert result.ok

    def test_non_cra_error_object_is_unchanged(self) -> None:
        payload: dict[str, Any] = {"error": {}}
        result = validate_cra_result(payload)
        assert result.skipped

    def test_other_schema_error_object_is_unchanged(self) -> None:
        payload = {"schema": "other/v1", "error": {"message": "example"}}
        result = validate_cra_result(payload)
        assert result.skipped

    def test_nested_primitives_lists_objects_never_crash(
        self, sbomaudit_result: dict[str, Any], vulnscan_result: dict[str, Any]
    ) -> None:
        mutations: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (sbomaudit_result, {"error": []}),
            (sbomaudit_result, {"error": {}}),
            (sbomaudit_result, {"error": {"message": "example"}}),
            (sbomaudit_result, {"schema": ["preloop.cra.sbomaudit/v1"]}),
            (sbomaudit_result, {"schema": {"id": "preloop.cra.sbomaudit/v1"}}),
            (sbomaudit_result, {"verdict": ["pass"]}),
            (sbomaudit_result, {"verdict": {"value": "pass"}}),
            (sbomaudit_result, {"runner": {"kind": ["hosted"]}}),
            (sbomaudit_result, {"runner": {"kind": {"name": "hosted"}}}),
            (vulnscan_result, {"status": ["success"]}),
            (vulnscan_result, {"status": {"ok": True}}),
            (sbomaudit_result, {"license_flags": [["copyleft"]]}),
        ]
        for base, extra in mutations:
            payload = clone(base)
            payload.update(extra)
            result = validate_cra_result(payload)
            assert result is not None

        expected = "preloop.cra.sbomaudit/v1"
        malformed = validate_cra_result(
            {"schema": ["preloop.cra.sbomaudit/v1"], "error": []},
            expected_schema=expected,
        )
        assert malformed.invalid

        nested = clone(vulnscan_result)
        nested["findings"] = [
            {
                **_kev_finding(),
                "severity": ["critical"],
                "match_kind": {"kind": "database"},
                "sources": [["osv_purl"]],
            }
        ]
        nested["counts_by_severity"] = {
            "critical": 1,
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }
        result = validate_cra_result(nested)
        assert result.invalid

        cra_error = clone(sbomaudit_result)
        cra_error["error"] = []
        result = validate_cra_result(cra_error)
        assert not result.skipped


class TestResultClaimsAuthorityContext:
    def test_non_cra_decision_does_not_claim_authority(self) -> None:
        assert not result_claims_authority(
            {"decision": {"outcome": "accepted"}, "status": "success"}
        )

    def test_non_cra_error_list_does_not_claim_authority(self) -> None:
        assert not result_claims_authority(
            {"error": [], "decision": {"outcome": "accepted"}}
        )

    def test_expected_cra_context_may_claim(
        self, duediligence_result: dict[str, Any]
    ) -> None:
        prompt = (
            "Required shape (preloop.cra.duediligence/v1): "
            '{ "schema": "preloop.cra.duediligence/v1" }'
        )
        assert result_claims_authority(duediligence_result, prompt=prompt)


class TestGateWaiverRecompute:
    def test_one_waiver_does_not_cover_second_kev(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver("CVE-2024-0001")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("CVE-2024-0001"), _kev_finding("CVE-2024-0002")],
            [waiver],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(payload, delivered_waivers=[waiver])
        assert not result.ok
        assert any("unwaived" in item for item in result.failures)

    def test_non_object_applied_entry_is_reported(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver()
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            ["not-an-object", waiver],
            passed=True,
        )
        result = validate_cra_result(payload, delivered_waivers=[waiver])
        assert not result.ok
        assert any("not an object" in item for item in result.failures)

    def test_ghsa_alias_matches_cve_waiver(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver("CVE-2024-0001")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("GHSA-xxxx-yyyy-zzzz", aliases=["CVE-2024-0001"])],
            [waiver],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(payload, delivered_waivers=[waiver])
        assert result.ok, result.failures

    def test_changed_delivered_waiver_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        delivered = _waiver(reason="Original approved reason.")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [_waiver(reason="Agent altered the reason.")],
            passed=True,
        )
        result = validate_cra_result(payload, delivered_waivers=[delivered])
        assert not result.ok
        assert any("delivered waiver contents" in item for item in result.failures)

    def test_unrelated_delete_file_approval_cannot_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-delete")
        payload = _release_with_kevs(
            releaseaudit_result, [_kev_finding()], [waiver], passed=True
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-delete",
                    status="approved",
                    tool_name="delete_file",
                    operation="delete /workspace/secret.txt",
                    tool_args={"path": "/workspace/secret.txt"},
                )
            ],
        )
        assert not result.ok
        assert any("ask_user" in item for item in result.failures)

    def test_request_approval_for_different_finding_cannot_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result, [_kev_finding()], [waiver], passed=True
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation="Waive CVE-2024-9999 in the shipped image",
                    tool_args={
                        "operation": "Waive CVE-2024-9999 in the shipped image",
                        "context": "Unrelated advisory CVE-2024-9999",
                        "reasoning": "Different finding",
                    },
                )
            ],
        )
        assert not result.ok

    def test_prose_request_approval_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [waiver],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation="Waive CVE-2024-0001 in the shipped image",
                    tool_args={
                        "operation": "Waive CVE-2024-0001 in the shipped image",
                        "context": "Release gate for CVE-2024-0001",
                        "reasoning": "Not reachable in the compiled image",
                    },
                )
            ],
        )
        assert not result.ok

    def test_mitigation_request_approval_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver("CVE-2026-0001", approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("CVE-2026-0001")],
            [waiver],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation="Install security update",
                    tool_args={
                        "operation": "Install security update",
                        "context": "Fix CVE-2026-0001. Do not waive it.",
                        "reasoning": "Patch the image",
                    },
                )
            ],
        )
        assert not result.ok

    def test_negated_waiver_text_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver("CVE-2026-0001", approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("CVE-2026-0001")],
            [waiver],
            passed=True,
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation="Do not waive CVE-2026-0001",
                    tool_args={
                        "operation": "Do not waive CVE-2026-0001",
                        "context": "Fix CVE-2026-0001. Do not waive it.",
                    },
                )
            ],
        )
        assert not result.ok

    def test_cve_prefix_overlap_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver("CVE-2026-0001", approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("CVE-2026-0001")],
            [waiver],
            passed=True,
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation=WAIVE_FINDING_OPERATION,
                    tool_args={
                        "operation": WAIVE_FINDING_OPERATION,
                        "finding_ids": ["CVE-2026-00010"],
                        "decision": "waive",
                    },
                )
            ],
        )
        assert not result.ok

    def test_canonical_waive_finding_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [waiver],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="request_approval",
                    operation=WAIVE_FINDING_OPERATION,
                    tool_args={
                        "operation": WAIVE_FINDING_OPERATION,
                        "finding_ids": ["CVE-2024-0001"],
                        "decision": "waive",
                        "reason": waiver["reason"],
                        "author": waiver["author"],
                        "date": waiver["date"],
                    },
                )
            ],
        )
        assert not result.ok

    def test_ai_approved_ask_user_cannot_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [waiver],
            passed=True,
            unwaived=[],
        )
        answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="ask_user",
                    tool_args={
                        "question": "Which residual risks do you accept?",
                        "options": [waiver["id"]],
                    },
                    tool_result={
                        "answer": answer,
                        "answered_by": waiver["author"],
                        "answered_at": f"{waiver['date']}T12:00:00Z",
                    },
                    responses=[
                        {
                            "user_id": waiver["author"],
                            "decision": "approved",
                            "comment": answer,
                            "timestamp": f"{waiver['date']}T12:00:00Z",
                        }
                    ],
                    approver_comment=answer,
                    resolved_at=f"{waiver['date']}T12:00:00Z",
                    decided_by_ai=True,
                )
            ],
        )
        assert not result.ok

    def test_auto_approved_ask_user_cannot_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [waiver],
            passed=True,
            unwaived=[],
        )
        answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="ask_user",
                    tool_args={
                        "question": "Which residual risks do you accept?",
                        "options": [waiver["id"]],
                    },
                    tool_result={
                        "answer": answer,
                        "answered_by": waiver["author"],
                        "answered_at": f"{waiver['date']}T12:00:00Z",
                    },
                    responses=[
                        {
                            "user_id": waiver["author"],
                            "decision": "approved",
                            "comment": answer,
                            "timestamp": f"{waiver['date']}T12:00:00Z",
                        }
                    ],
                    approver_comment=answer,
                    resolved_at=f"{waiver['date']}T12:00:00Z",
                    auto_approved_reason="bypass",
                )
            ],
        )
        assert not result.ok

    def test_ask_user_exact_tool_result_may_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [waiver],
            passed=True,
            unwaived=[],
        )
        answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="ask_user",
                    tool_args={
                        "question": "Which residual risks do you accept?",
                        "options": [waiver["id"]],
                        "context": "Fix CVE-2024-0001. Do not waive it.",
                    },
                    tool_result={
                        "answer": answer,
                        "answered_by": waiver["author"],
                        "answered_at": f"{waiver['date']}T12:00:00Z",
                    },
                    responses=[
                        {
                            "user_id": waiver["author"],
                            "decision": "approved",
                            "comment": answer,
                            "timestamp": f"{waiver['date']}T12:00:00Z",
                        }
                    ],
                    approver_comment=answer,
                    resolved_at=f"{waiver['date']}T12:00:00Z",
                )
            ],
        )
        assert result.ok, result.failures

    def test_ask_user_approved_without_matching_answer_does_not_waive(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        waiver = _waiver(approval_id="appr-1")
        payload = _release_with_kevs(
            releaseaudit_result, [_kev_finding()], [waiver], passed=True
        )
        result = validate_cra_result(
            payload,
            platform_approvals=[
                PlatformApproval(
                    id="appr-1",
                    status="approved",
                    tool_name="ask_user",
                    tool_args={
                        "question": "Fix CVE-2024-0001. Do not waive it.",
                        "options": ["yes", "no"],
                    },
                    tool_result={
                        "answer": "yes",
                        "answered_by": waiver["author"],
                        "answered_at": f"{waiver['date']}T12:00:00Z",
                    },
                    approver_comment="yes",
                    resolved_at=f"{waiver['date']}T12:00:00Z",
                )
            ],
        )
        assert not result.ok

    def test_delivered_waiver_scope_must_match(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        delivered = _waiver()
        applied = dict(delivered)
        applied["scope"] = "all-products"
        payload = _release_with_kevs(
            releaseaudit_result, [_kev_finding()], [applied], passed=True
        )
        result = validate_cra_result(payload, delivered_waivers=[delivered])
        assert not result.ok
        assert any("delivered waiver contents" in item for item in result.failures)

    def test_delivered_waiver_matching_scope_may_pass(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        delivered = dict(_waiver())
        delivered["scope"] = "shipped-image"
        delivered["package"] = "libexample"
        delivered["version"] = "1.0"
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding()],
            [delivered],
            passed=True,
            unwaived=[],
        )
        result = validate_cra_result(payload, delivered_waivers=[delivered])
        assert result.ok, result.failures

    def test_partial_coverage_is_rejected(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        first = _waiver("CVE-2024-0001")
        payload = _release_with_kevs(
            releaseaudit_result,
            [_kev_finding("CVE-2024-0001"), _kev_finding("CVE-2024-0002")],
            [first],
            passed=True,
            unwaived=["CVE-2024-0002"],
        )
        result = validate_cra_result(payload, delivered_waivers=[first])
        assert not result.ok
        joined = " ".join(result.failures)
        assert "CVE-2024-0002" in joined or "unwaived" in joined


def _cvss_only_finding(
    finding_id: str = "CVE-2026-0001", *, cvss: float = 9.8
) -> dict[str, Any]:
    finding = _kev_finding(finding_id)
    finding["kev"] = False
    finding["cvss"] = cvss
    finding["waived"] = False
    return finding


class TestAuthoritativeGatePolicy:
    def test_model_chosen_cvss_99_does_not_lower_bar(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        finding = _cvss_only_finding()
        payload = _release_with_kevs(
            releaseaudit_result, [finding], [], passed=True, unwaived=[]
        )
        payload["vuln_scan"]["gate"]["policy"] = "fail on CVSS >= 99"
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = True
        result = validate_cra_result(payload)
        assert not result.ok

    def test_nan_and_infinity_thresholds_keep_default(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        finding = _cvss_only_finding()
        payload = _release_with_kevs(
            releaseaudit_result, [finding], [], passed=True, unwaived=[]
        )
        payload["vuln_scan"]["gate"]["policy"] = "fail on CVSS >= Infinity"
        for raw in (float("nan"), float("inf"), float("-inf"), 99, "NaN"):
            policy = parse_gate_policy({"fail_on_cvss_gte": raw})
            assert policy.fail_on_cvss_gte == 9.0
            result = validate_cra_result(payload, gate_policy=policy)
            assert not result.ok

    def test_operator_cvss_7_override_is_stricter(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        finding = _cvss_only_finding(cvss=8.0)
        payload = _release_with_kevs(
            releaseaudit_result, [finding], [], passed=True, unwaived=[]
        )
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = True
        default_result = validate_cra_result(payload)
        assert default_result.ok, default_result.failures
        override = parse_gate_policy({"fail_on_cvss_gte": 7.0, "fail_on_kev": True})
        assert override.fail_on_cvss_gte == 7.0
        result = validate_cra_result(payload, gate_policy=override)
        assert not result.ok

    def test_fail_on_kev_false_is_honored(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        finding = _kev_finding()
        finding["cvss"] = 5.0
        finding["waived"] = False
        payload = _release_with_kevs(
            releaseaudit_result, [finding], [], passed=True, unwaived=[]
        )
        payload["vuln_scan"]["gate"]["passed_before_waivers"] = True
        kev_on = validate_cra_result(payload)
        assert not kev_on.ok
        kev_off = validate_cra_result(
            payload, gate_policy=GatePolicy(fail_on_kev=False, fail_on_cvss_gte=9.0)
        )
        assert kev_off.ok, kev_off.failures


class TestNestedJsonAndErrorEnvelopes:
    def test_gap_register_malformed_nested_is_invalid(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = clone(releaseaudit_result)
        payload["gap_register"] = {
            "items": [{"status": {}}],
            "history_rows": [42],
        }
        result = validate_cra_result(payload)
        assert result.invalid
        joined = " ".join(result.failures)
        assert "status" in joined or "not an object" in joined

    def test_error_envelope_failures_object_does_not_crash(
        self, releaseaudit_result: dict[str, Any]
    ) -> None:
        payload = {
            "error": "cra_result_invalid",
            "failures": [{}],
            "raw": releaseaudit_result,
        }
        result = validate_cra_result(payload)
        assert result.invalid
        assert result.failures
        assert all(isinstance(item, str) for item in result.failures)
