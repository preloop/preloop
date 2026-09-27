"""Synthetic release audits in the shape of a CI-attested, VEX-covered run.

The shape: every finding carries a supplier VEX ``not_affected`` statement
with a machine-readable justification, three cross-checks were skipped
because their input was not delivered, and the gap register still has gap
and partial items. Names and ids are generic.
"""

from __future__ import annotations

from typing import Any

from .conftest import clone, load_json

VEX_DOC = "https://vex.example.com/example-frontend-1"
GO_VEX_DOC = "https://vex.example.com/example-cli-1"

LIMITATION_SKIPS: tuple[tuple[str, str], ...] = (
    ("build_manifest_cross_check", "build_manifest"),
    ("nvd_cpe_heuristic", "ecosystem-less component subset"),
    ("osv_distro_heuristic", "distro-package component subset"),
)

HOLDING_GAP_ITEMS: dict[str, str] = {
    "article14_runbooks": "gap",
    "update_and_signed_ota": "partial",
    "secrets_hygiene": "gap",
    "default_credentials_provisioning": "gap",
    "repo_hygiene": "gap",
}
MET_GAP_ITEMS: tuple[str, ...] = (
    "cvd_policy",
    "security_contact",
    "key_management",
    "ci_secret_scanning",
    "ci_sbom_job",
    "debug_leakage",
)


def _finding(
    finding_id: str,
    pkg: str,
    *,
    severity: str,
    cvss: float | None,
    statement: str,
    justification: str,
    sources: list[str],
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "pkg": pkg,
        "version": pkg.rsplit("@", 1)[-1],
        "severity": severity,
        "cvss": cvss,
        "epss": None,
        "kev": False,
        "fix_version": None,
        "vex_status": "not_affected",
        "vex_statement_id": statement,
        "vex_justification": justification,
        "sources": sources,
        "match_kind": "database",
        "waived": False,
        "aliases": None,
    }


def _findings() -> list[dict[str, Any]]:
    findings = [
        _finding(
            f"CVE-2026-{10001 + idx}",
            "example-types@1.0.0",
            severity="medium",
            cvss=5.3,
            statement=f"{VEX_DOC}#{idx}",
            justification="vulnerable_code_not_present",
            sources=["osv_git"],
        )
        for idx in range(24)
    ]
    findings.append(
        _finding(
            "CVE-2026-20001",
            "example-camelcase@4.3.0",
            severity="medium",
            cvss=6.5,
            statement=f"{VEX_DOC}#24",
            justification="vulnerable_code_not_in_execute_path",
            sources=["osv_git"],
        )
    )
    findings.append(
        _finding(
            "GO-2026-0001",
            "example.com/x/crypto@v0.57.0",
            severity="unknown",
            cvss=None,
            statement=f"{GO_VEX_DOC}#0",
            justification="vulnerable_code_not_present",
            sources=["osv_purl"],
        )
    )
    return findings


def _check(name: str, passed: bool, details: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "skipped": False, "details": details}


def _checks(gap_items: list[dict[str, Any]], zizmor_passed: bool) -> list[Any]:
    checks: list[dict[str, Any]] = [
        _check("sbom_format_validity", True, "CycloneDX 1.6, schema-valid"),
        _check("sbom_minimum_elements", True, "measured: passed"),
        _check("attestation_verification", True, "attestation verified"),
        _check("osv_purl_screen", True, "screened by purl; control passed"),
        _check("vex_applied_before_gate", True, "VEX applied before the gate"),
    ]
    for name, missing in LIMITATION_SKIPS:
        checks.append(
            {
                "name": name,
                "passed": True,
                "skipped": True,
                "missing_input": missing,
                "details": f"skipped: {missing} not delivered",
            }
        )
    for item in gap_items:
        checks.append(
            _check(
                f"gap_register_{item['id']}",
                item["status"] == "met",
                f"{item['status']} - {item['evidence']}",
            )
        )
    checks.append(
        _check(
            "zizmor_workflow_audit",
            zizmor_passed,
            "workflow findings" if not zizmor_passed else "no findings",
        )
    )
    return checks


def _gap_register(*, with_gaps: bool) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {"id": item_id, "title": item_id, "status": "met", "evidence": "README.md:1"}
        for item_id in MET_GAP_ITEMS
    ]
    items.append(
        {
            "id": "support_window",
            "title": "Declared support window",
            "status": "declared",
            "evidence": "SECURITY.md:16 (proposed wording)",
        }
    )
    rows: list[dict[str, Any]] = []
    if with_gaps:
        items.extend(
            {"id": item_id, "title": item_id, "status": status, "evidence": "x:1"}
            for item_id, status in HOLDING_GAP_ITEMS.items()
        )
        rows = [
            {
                "sha": f"{idx:040x}",
                "path": f"tests/fixtures/example-{idx}.env",
                "subject": "test fixture",
                "term": "password",
                "kind": "history",
                "status": "finding",
            }
            for idx in range(1, 3)
        ]
    else:
        items.extend(
            {"id": item_id, "title": item_id, "status": "met", "evidence": "x:1"}
            for item_id in HOLDING_GAP_ITEMS
        )
    return {
        "ran": True,
        "repo": {
            "remote": "https://example.com/example/product.git",
            "commit": "a" * 40,
            "branch": "main",
        },
        "items": items,
        "secrets_findings": rows,
        "secrets_findings_count": len(rows),
        "not_checkable": ["whether any historical credential is still live"],
        "resolved": [],
        "ready": False,
    }


def attested_release_audit(*, with_gaps: bool = True) -> dict[str, Any]:
    """A release audit in the shape of a CI-attested, VEX-covered run.

    Args:
        with_gaps: When false, the gap register has no gap or partial item
            and no secrets finding (one ``declared`` item stays), and the
            workflow audit passes.

    Returns:
        A ``preloop.cra.releaseaudit/v1`` document the validator accepts.
    """
    doc = clone(load_json("cra", "result-releaseaudit.json"))
    doc["inputs_declared"] = {
        "sbom": "sbom/example.cdx.json (workspace seed)",
        "vex": "vex/example.openvex.json",
        "previous_result": "previous/result.json",
        "build_manifest": None,
        "license_policy_file": None,
    }
    sbom = doc["sbom_audit"]
    sbom["source"]["format"] = "cyclonedx"
    sbom["source"]["spec_version"] = "1.6"
    sbom["coverage"] = {
        "components": 30,
        "pct_with_version": 100.0,
        "pct_with_license": 90.0,
        "pct_with_license_concluded": 0.0,
        "pct_with_license_declared": 90.0,
        "pct_with_identifier": 100.0,
        "db_resolvable": 30,
        "not_db_resolvable": 0,
        "unmatched_vs_build": None,
    }
    sbom["license_flags"] = [
        {"component": "example-lib@1.0.0", "license": None, "flag": "missing"}
    ]
    sbom["verdict"] = "pass_with_findings"

    vuln = doc["vuln_scan"]
    inventory = vuln["inventory"]
    inventory.update(
        {
            "components": 30,
            "matchable": 30,
            "unmatchable": 0,
            "db_resolvable": 30,
            "not_db_resolvable": 0,
            "by_ecosystem": {"npm": 25, "golang": 5},
        }
    )
    matrix = inventory["source_matrix"]
    matrix["osv_purl"].update({"screenable": 30, "blind": 0})
    matrix["osv_git"].update({"screenable": 20, "blind": 10})
    for key in ("nvd_cpe", "osv_distro"):
        matrix[key].update({"screenable": 0, "blind": 30})
        matrix[key]["negative_control"]["method_blind"] = False
        matrix[key]["negative_control"]["result"] = "control passed"
    matrix["screened_by_no_source"] = 0

    findings = _findings()
    vuln["findings"] = findings
    vuln["counts_by_severity"] = {
        "critical": 0,
        "high": 0,
        "medium": 25,
        "low": 0,
        "unknown": 1,
    }
    vuln["gate"]["policy"] = "default: KEV, CVSS >= 9.0, unscored database findings"
    vuln["gate"]["vex_suppressed"] = [
        {
            "id": "GO-2026-0001",
            "vex_status": "not_affected",
            "vex_statement_id": f"{GO_VEX_DOC}#0",
            "vex_justification": "vulnerable_code_not_present",
            "would_have_failed": "unscored",
        }
    ]

    register = _gap_register(with_gaps=with_gaps)
    doc["gap_register"] = register
    doc["artifacts"]["gap_register"] = "evidence/gap-register.md"
    doc["checks"] = _checks(register["items"], zizmor_passed=not with_gaps)
    # What an honest agent writes. The tallies are written out rather than
    # derived here so the tests do not grade the code with itself.
    doc["limitations"] = [
        {"check": check["name"], "missing_input": check["missing_input"]}
        for check in doc["checks"]
        if check.get("missing_input")
    ]
    doc["verdict"] = "pass_with_findings"
    return doc
