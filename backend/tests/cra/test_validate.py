"""Runtime validator for the four CRA result.json schemas."""

from __future__ import annotations

from typing import Any

from preloop.cra.schemas import (
    SCHEMA_DUEDILIGENCE_V1,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
    expected_cra_schema_from_prompt,
)
from preloop.cra.validate import (
    AUTHORITY_REQUIRED,
    PlatformApproval,
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
