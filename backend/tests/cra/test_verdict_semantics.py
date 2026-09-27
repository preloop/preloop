"""Release verdict semantics: VEX-closed findings, limitations, gap items.

``pass`` means: minimum elements passed, gate passed, no open
(non-VEX-closed) findings, no failed cross-checks, no gap/partial register
items. The platform derives ``closed_by_vex`` and ``limitations`` at persist
and recomputes the overall label from those facts, within bounds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from preloop.cra.persist import apply_cra_persist_boundary
from preloop.cra.repair import (
    VERDICT_CORRECTED_FIELD,
    apply_derived_verdict_facts,
    failures_are_only_counts,
    verdict_corrections,
)
from preloop.cra.validate import validate_cra_result
from preloop.cra.verdict import (
    PASS_DEFINITION,
    classify_checks,
    count_closed_by_vex,
    input_declared_delivered,
    release_verdict_basis,
    vex_closure,
)

from .conftest import clone
from .verdict_fixtures import attested_release_audit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _persist(payload: dict[str, Any]) -> dict[str, Any]:
    decision = apply_cra_persist_boundary(payload)
    assert not decision.invalid, decision.validation.failures
    assert decision.artifact is not None
    return decision.artifact


def _verdict_records(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in artifact.get(VERDICT_CORRECTED_FIELD, [])
        if item["path"] == "result.verdict"
    ]


def _finding(doc: dict[str, Any], finding_id: str) -> dict[str, Any]:
    return next(
        item for item in doc["vuln_scan"]["findings"] if item["id"] == finding_id
    )


class TestAttestedShape:
    """The 2026-09-27 shape: 26 VEX-closed findings, 3 input-absent skips."""

    def test_gap_items_hold_it_at_pass_with_findings(self) -> None:
        artifact = _persist(attested_release_audit())

        assert artifact["verdict"] == "pass_with_findings"
        assert artifact["vuln_scan"]["closed_by_vex"] == 26
        assert len(artifact["limitations"]) == 3
        assert _verdict_records(artifact) == []

    def test_what_holds_it_is_the_gap_register_not_the_findings(self) -> None:
        basis = release_verdict_basis(attested_release_audit())

        joined = "; ".join(basis.reasons)
        assert "gap or partial items" in joined
        assert "zizmor_workflow_audit" in joined
        assert "open findings" not in joined
        assert "skipped" not in joined
        assert "support_window" not in joined

    def test_findings_stay_in_the_ledger(self) -> None:
        payload = attested_release_audit()
        artifact = _persist(payload)
        assert artifact["vuln_scan"]["findings"] == payload["vuln_scan"]["findings"]
        assert artifact["vuln_scan"]["gate"] == payload["vuln_scan"]["gate"]

    def test_limitations_name_the_missing_input(self) -> None:
        artifact = _persist(attested_release_audit())
        assert artifact["limitations"] == [
            {"check": "build_manifest_cross_check", "missing_input": "build_manifest"},
            {
                "check": "nvd_cpe_heuristic",
                "missing_input": "ecosystem-less component subset",
            },
            {
                "check": "osv_distro_heuristic",
                "missing_input": "distro-package component subset",
            },
        ]

    def test_an_agent_pass_is_escalated(self) -> None:
        payload = attested_release_audit()
        payload["verdict"] = "pass"
        assert not validate_cra_result(payload).ok

        artifact = _persist(payload)

        assert artifact["verdict"] == "pass_with_findings"
        [record] = _verdict_records(artifact)
        assert record["submitted"] == "pass"
        assert "gap or partial items" in record["reason"]


class TestEmptyGapRegister:
    def test_the_same_document_is_a_pass(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        assert payload["verdict"] == "pass_with_findings"

        artifact = _persist(payload)

        assert artifact["verdict"] == "pass"
        assert artifact["vuln_scan"]["closed_by_vex"] == 26
        assert len(artifact["limitations"]) == 3
        [record] = _verdict_records(artifact)
        assert record["submitted"] == "pass_with_findings"
        assert record["corrected"] == "pass"
        assert PASS_DEFINITION in record["reason"]
        assert "closed_by_vex=26, limitations=3" in record["reason"]

    def test_an_agent_pass_is_accepted_as_is(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["verdict"] = "pass"
        assert validate_cra_result(payload).ok

        artifact = _persist(payload)

        assert artifact["verdict"] == "pass"
        assert VERDICT_CORRECTED_FIELD not in artifact

    def test_a_declared_item_does_not_hold(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        statuses = {item["status"] for item in payload["gap_register"]["items"]}
        assert statuses == {"met", "declared"}
        mirror = next(
            item
            for item in payload["checks"]
            if item["name"] == "gap_register_support_window"
        )
        assert mirror["passed"] is False
        assert release_verdict_basis(payload).verdict == "pass"

    @pytest.mark.parametrize("status", ["gap", "partial"])
    def test_one_gap_or_partial_item_holds(self, status: str) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["gap_register"]["items"][0]["status"] = status
        basis = release_verdict_basis(payload)
        assert basis.verdict == "pass_with_findings"
        assert "cvd_policy" in basis.reasons[0]

    def test_a_secrets_finding_holds(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["gap_register"]["secrets_findings"] = [
            {
                "sha": "b" * 40,
                "path": "config/example.env",
                "subject": "example",
                "term": "token",
                "kind": "history",
                "status": "finding",
            }
        ]
        payload["gap_register"]["secrets_findings_count"] = 1
        basis = release_verdict_basis(payload)
        assert basis.reasons == ["gap register has 1 secrets findings"]

    def test_a_cross_check_that_ran_and_failed_holds(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["checks"][-1]["passed"] = False
        basis = release_verdict_basis(payload)
        assert basis.reasons == ["checks ran and failed: zizmor_workflow_audit"]

    @pytest.mark.parametrize("name", ["cvd_policy", "gap_register_cvd_policy"])
    def test_a_met_item_does_not_excuse_its_failed_check(self, name: str) -> None:
        payload = attested_release_audit(with_gaps=False)
        item = next(
            entry
            for entry in payload["gap_register"]["items"]
            if entry["id"] == "cvd_policy"
        )
        assert item["status"] == "met"
        payload["checks"].append(
            {"name": name, "passed": False, "skipped": False, "details": "absent"}
        )

        basis = release_verdict_basis(payload)

        assert basis.reasons == [f"checks ran and failed: {name}"]

    @pytest.mark.parametrize("name", ["support_window", "gap_register_support_window"])
    def test_a_declared_item_excuses_its_mirror_check(self, name: str) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["checks"].append(
            {"name": name, "passed": False, "skipped": False, "details": "declared"}
        )
        assert release_verdict_basis(payload).verdict == "pass"

    def test_an_id_listed_as_met_and_declared_does_not_excuse(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["gap_register"]["items"].append(
            {
                "id": "support_window",
                "title": "Support window",
                "status": "met",
                "evidence": "SECURITY.md:1",
            }
        )
        basis = release_verdict_basis(payload)
        assert basis.reasons == ["checks ran and failed: gap_register_support_window"]

    def test_the_sbom_license_findings_stay_on_the_nested_verdict(self) -> None:
        artifact = _persist(attested_release_audit(with_gaps=False))
        assert artifact["verdict"] == "pass"
        assert artifact["sbom_audit"]["verdict"] == "pass_with_findings"


class TestOpenFindings:
    @pytest.mark.parametrize("status", ["affected", "under_investigation"])
    def test_a_non_closing_vex_status_holds(self, status: str) -> None:
        payload = attested_release_audit(with_gaps=False)
        _finding(payload, "CVE-2026-10001")["vex_status"] = status

        artifact = _persist(payload)

        assert artifact["verdict"] == "pass_with_findings"
        assert artifact["vuln_scan"]["closed_by_vex"] == 25
        assert _verdict_records(artifact) == []
        assert "1 open findings" in release_verdict_basis(artifact).reasons[0]

    def test_an_unrecognised_justification_does_not_close(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        _finding(payload, "CVE-2026-10001")["vex_justification"] = "we think so"
        artifact = _persist(payload)
        assert artifact["vuln_scan"]["closed_by_vex"] == 25
        assert artifact["verdict"] == "pass_with_findings"

    def test_a_missing_statement_id_does_not_close(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        _finding(payload, "CVE-2026-10001")["vex_statement_id"] = None
        assert count_closed_by_vex(payload["vuln_scan"]["findings"]) == 25

    def test_false_positive_does_not_close(self) -> None:
        finding = {
            "vex_status": "false_positive",
            "vex_statement_id": "x#1",
            "vex_justification": "code_not_present",
        }
        assert vex_closure(finding) is None

    def test_fixed_with_a_recorded_statement_closes(self) -> None:
        finding = {
            "vex_status": "fixed",
            "vex_statement_id": "https://vex.example.com/doc#3",
            "vex_justification": "patched in the vendored copy",
        }
        closure = vex_closure(finding)
        assert closure is not None
        assert closure["vex_status"] == "fixed"

    def test_a_finding_without_vex_holds(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        finding = _finding(payload, "CVE-2026-10001")
        for key in ("vex_status", "vex_statement_id", "vex_justification"):
            finding[key] = None
        assert release_verdict_basis(payload).verdict == "pass_with_findings"

    def test_an_unscored_finding_without_vex_still_fails_the_gate(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        finding = _finding(payload, "GO-2026-0001")
        for key in ("vex_status", "vex_statement_id", "vex_justification"):
            finding[key] = None
        gate = payload["vuln_scan"]["gate"]

        # Claiming the gate passed is rejected by the deterministic gate.
        assert not validate_cra_result(payload).ok

        gate["vex_suppressed"] = []
        gate["passed"] = False
        gate["passed_before_waivers"] = False
        gate["unwaived_failures"] = ["GO-2026-0001"]
        payload["verdict"] = "fail"
        artifact = _persist(payload)

        assert artifact["verdict"] == "fail"
        assert artifact["vuln_scan"]["closed_by_vex"] == 25
        assert _verdict_records(artifact) == []

    def test_a_reported_gate_failure_labelled_findings_is_escalated(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        finding = _finding(payload, "GO-2026-0001")
        for key in ("vex_status", "vex_statement_id", "vex_justification"):
            finding[key] = None
        gate = payload["vuln_scan"]["gate"]
        gate["vex_suppressed"] = []
        gate["passed"] = False
        gate["passed_before_waivers"] = False
        gate["unwaived_failures"] = ["GO-2026-0001"]

        artifact = _persist(payload)

        assert artifact["verdict"] == "fail"
        [record] = _verdict_records(artifact)
        assert record["corrected"] == "fail"


class TestSkips:
    def test_a_skip_without_a_named_input_holds(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        del payload["checks"][5]["missing_input"]
        basis = release_verdict_basis(payload)
        assert basis.reasons == ["a check was skipped"]
        assert len(basis.limitations) == 2

    def test_a_skip_whose_input_was_delivered_holds(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["inputs_declared"]["build_manifest"] = "manifests/build.json"
        classified = classify_checks(payload)
        assert classified.unexplained_skips == ["build_manifest_cross_check"]
        assert release_verdict_basis(payload).verdict == "pass_with_findings"

    @pytest.mark.parametrize(
        ("value", "delivered"),
        [
            (None, False),
            ("", False),
            ("not delivered", False),
            ("none", False),
            ("No build manifest was delivered", False),
            ("manifests/build.json", True),
            (["a.json"], True),
            ([], False),
        ],
    )
    def test_declared_input_reading(self, value: Any, delivered: bool) -> None:
        inputs = {"build_manifest": value}
        assert input_declared_delivered(inputs, "build_manifest") is delivered

    def test_an_undeclared_input_name_is_not_a_contradiction(self) -> None:
        assert input_declared_delivered({"sbom": "x"}, "component subset") is False

    def test_missing_input_must_be_a_string(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["checks"][5]["missing_input"] = 3
        result = validate_cra_result(payload)
        assert not result.ok
        assert any("missing_input" in item for item in result.failures)


class TestDerivedTallies:
    def test_an_agent_tally_is_never_trusted(self) -> None:
        payload = attested_release_audit()
        payload["vuln_scan"]["closed_by_vex"] = 0
        payload["limitations"] = []
        result = validate_cra_result(payload)
        assert not result.ok

        artifact = _persist(payload)

        assert artifact["vuln_scan"]["closed_by_vex"] == 26
        assert len(artifact["limitations"]) == 3
        paths = {item["path"] for item in artifact[VERDICT_CORRECTED_FIELD]}
        assert paths == {"result.vuln_scan.closed_by_vex", "result.limitations"}

    def test_a_matching_tally_is_not_a_correction(self) -> None:
        payload = attested_release_audit()
        payload["vuln_scan"]["closed_by_vex"] = 26
        _, corrections, _ = apply_derived_verdict_facts(payload)
        assert [item.path for item in corrections] == []

    def test_a_bool_tally_is_rejected(self) -> None:
        payload = attested_release_audit()
        payload["vuln_scan"]["closed_by_vex"] = True
        assert not validate_cra_result(payload).ok

    def test_derived_fact_failures_do_not_block_count_repair(self) -> None:
        assert failures_are_only_counts(
            [
                "result.vuln_scan.counts_by_severity.medium is 1",
                "result.vuln_scan.closed_by_vex is 0 but 26 findings are closed",
            ]
        )
        assert not failures_are_only_counts(
            ["result.vuln_scan.closed_by_vex is 0 but 26 findings are closed"]
        )

    def test_the_submitted_payload_is_not_mutated(self) -> None:
        payload = attested_release_audit()
        before = clone(payload)
        apply_derived_verdict_facts(payload)
        verdict_corrections(payload)
        assert payload == before


class TestDrift:
    def test_drift_carries_both_tallies(self) -> None:
        payload = attested_release_audit()
        payload["drift"]["closed_by_vex"] = {"previous": 25, "current": 0}
        payload["drift"]["limitations"] = {
            "previous": ["build_manifest_cross_check"],
            "current": [],
        }

        artifact = _persist(payload)

        assert artifact["drift"]["closed_by_vex"] == {"previous": 25, "current": 26}
        assert artifact["drift"]["limitations"] == {
            "previous": ["build_manifest_cross_check"],
            "current": [
                "build_manifest_cross_check",
                "nvd_cpe_heuristic",
                "osv_distro_heuristic",
            ],
        }

    def test_an_older_baseline_has_no_previous(self) -> None:
        artifact = _persist(attested_release_audit())
        assert artifact["drift"]["closed_by_vex"] == {"previous": None, "current": 26}
        assert artifact["drift"]["limitations"]["previous"] is None

    def test_no_drift_block_is_invented(self) -> None:
        payload = attested_release_audit()
        payload["drift"] = None
        payload["artifacts"]["drift_report"] = None
        artifact = _persist(payload)
        assert artifact["drift"] is None

    def test_a_malformed_previous_is_rejected(self) -> None:
        payload = attested_release_audit()
        payload["vuln_scan"]["closed_by_vex"] = 26
        payload["limitations"] = release_verdict_basis(payload).limitations
        payload["drift"]["closed_by_vex"] = {"previous": "many", "current": 26}
        result = validate_cra_result(payload)
        assert any("drift.closed_by_vex.previous" in f for f in result.failures)


class TestCorrectionBounds:
    def test_a_fabricated_fail_moves_one_step(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["verdict"] = "fail"

        artifact = _persist(payload)

        assert artifact["verdict"] == "pass_with_findings"
        [record] = _verdict_records(artifact)
        assert (record["submitted"], record["corrected"]) == (
            "fail",
            "pass_with_findings",
        )

    def test_a_fail_is_kept_when_the_gate_failed(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["verdict"] = "fail"
        payload["vuln_scan"]["gate"]["passed"] = "yes"
        _, corrections = verdict_corrections(payload)
        assert corrections == []

    def test_a_fail_is_kept_when_minimum_elements_failed(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["verdict"] = "fail"
        payload["sbom_audit"]["minimum_elements"] = {
            "passed": False,
            "missing": ["supplier: 1"],
        }
        artifact = _persist(payload)
        assert artifact["verdict"] == "fail"
        assert artifact["sbom_audit"]["verdict"] == "fail"

    def test_components_no_source_screened_hold(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["vuln_scan"]["inventory"]["source_matrix"]["screened_by_no_source"] = 4
        basis = release_verdict_basis(payload)
        assert basis.reasons == ["4 components were screened by no source"]

    def test_an_error_verdict_is_not_touched(self) -> None:
        payload = attested_release_audit(with_gaps=False)
        payload["verdict"] = "error"
        _, corrections = verdict_corrections(payload)
        assert corrections == []


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize(
    "relative",
    [
        "backend/presets/006-release-security-audit.yaml",
        "docs/guide/flows/security-audit-presets.md",
    ],
)
def test_the_pass_sentence_is_written_where_agents_and_readers_see_it(
    relative: str,
) -> None:
    text = _normalized((REPO_ROOT / relative).read_text(encoding="utf-8"))
    assert _normalized(PASS_DEFINITION) in text
