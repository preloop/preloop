"""Offline fixture conformance and explicit import of operator pilot evidence.

No client, network, credential lookup or paid inference is performed. Synthetic
fixtures validate verdict/evidence semantics only, never application enforcement.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

CASE_IDS = (
    "allowed_prompt",
    "denied_prompt",
    "denied_tool_batch",
    "unknown_identity",
    "unknown_source",
    "unknown_event",
    "endpoint_outage",
    "policy_change",
    "revoked_credential",
    "alternate_covered_client",
    "direct_route_attempt",
    "rollback",
)
SCHEMA_PATH = (
    Path(__file__).parent.parent / "assets/schemas/anthropic-evidence.schema.json"
)
FIXTURE_PATH = Path(__file__).with_name("anthropic-protocol-fixtures.json")


def validate_evidence(evidence: dict[str, Any]) -> None:
    """Reject incomplete, mixed, unsafe or unsupported evidence claims.

    Args:
        evidence: Content-free protocol or application observation document.

    Raises:
        ValueError: The document cannot support its stated evidence kind.
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = list(Draft202012Validator(schema).iter_errors(evidence))
    if errors:
        raise ValueError("evidence_schema_invalid")
    cases = evidence["cases"]
    if len(cases) != len(CASE_IDS) or {c["id"] for c in cases} != set(CASE_IDS):
        raise ValueError("evidence_case_set_invalid")
    kind = evidence["kind"]
    if kind == "synthetic_protocol":
        if evidence["enforcement_state"] != "unverified":
            raise ValueError("synthetic_cannot_verify_enforcement")
        if evidence["test_tenant_ref"] is not None:
            raise ValueError("synthetic_cannot_claim_tenant")
    elif not evidence["test_tenant_ref"]:
        raise ValueError("live_test_tenant_required")
    for case in cases:
        if case["evidence_kind"] != kind:
            raise ValueError("mixed_evidence_kind")
        if case["status"] == "pass" and not case["observations"]:
            raise ValueError("passing_case_requires_observation")
        if case["id"] == "denied_tool_batch" and case["status"] == "pass":
            if case["side_effect_count"] != 0:
                raise ValueError("denied_batch_requires_zero_side_effects")
        if kind == "live_application" and case["status"] == "pass":
            if not case["artifact_refs"]:
                raise ValueError("live_pass_requires_artifact")
    if evidence["enforcement_state"] == "verified_for_recorded_surface":
        if any(case["status"] != "pass" for case in cases):
            raise ValueError("verified_requires_complete_pilot")
        if any(value == "unknown" for value in evidence["versions"].values()):
            raise ValueError("verified_requires_versions")
        if not evidence["surface_ref"] or not evidence["settings_artifact_ref"]:
            raise ValueError("verified_requires_effective_settings")


def build_template(kind: str, test_tenant_ref: str | None = None) -> dict[str, Any]:
    """Create a pending record with no inferred coverage or enforcement."""
    return {
        "schema_version": 1,
        "kind": kind,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "enforcement_state": "unverified",
        "test_tenant_ref": test_tenant_ref,
        "surface_ref": None,
        "settings_artifact_ref": None,
        "versions": dict.fromkeys(
            ("core", "backend", "cli", "client", "os"), "unknown"
        ),
        "cases": [
            {
                "id": case_id,
                "status": "pending",
                "evidence_kind": kind,
                "observations": [],
                "artifact_refs": [],
                "side_effect_count": None,
            }
            for case_id in CASE_IDS
        ],
    }


def run_fixtures(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Check supplied simulated outcomes, without executing an application.

    These outcomes are explicit local fixtures, not responses from a commercial
    adapter. Failure catches invalid wire/evidence assumptions, not tenant drift.
    """
    if len(fixtures) != len(CASE_IDS) or {f["id"] for f in fixtures} != set(CASE_IDS):
        raise ValueError("fixture_case_set_invalid")
    evidence = build_template("synthetic_protocol")
    by_id = {f["id"]: f for f in fixtures}
    for case in evidence["cases"]:
        fixture = by_id[case["id"]]
        outcome = fixture["outcome"]
        expected = fixture["expected"]
        valid = outcome == expected
        if "verdict" in outcome:
            verdict = outcome["verdict"]
            valid = valid and verdict.get("action") in {"allow", "deny"}
            valid = valid and set(verdict) <= {"action", "deny_reason", "reference_id"}
            valid = valid and len(verdict.get("reference_id", "")) <= 50
        if case["id"] == "denied_tool_batch":
            valid = valid and outcome.get("side_effect_count") == 0
        case.update(
            status="pass" if valid else "fail",
            observations=["fixture_matches" if valid else "fixture_mismatch"],
            side_effect_count=outcome.get("side_effect_count"),
        )
    validate_evidence(evidence)
    return evidence


def main() -> int:
    """Write a fixture report or validate explicitly authorized live evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--test-tenant-ref")
    parser.add_argument("--harmless-fixtures", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.live:
        if (
            not args.harmless_fixtures
            or not args.test_tenant_ref
            or not re.fullmatch(r"test-tenant-[0-9]{3}", args.test_tenant_ref)
        ):
            parser.error(
                "live mode requires harmless fixtures and a synthetic test-tenant reference"
            )
        if args.template:
            if args.evidence:
                parser.error("template mode does not import evidence")
            evidence = build_template("live_application", args.test_tenant_ref)
        else:
            if not args.evidence:
                parser.error("live mode requires an operator-supplied evidence file")
            try:
                evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                parser.error(f"could not read evidence file ({type(exc).__name__})")
            if not isinstance(evidence, dict):
                parser.error("evidence file must contain a JSON object")
            if evidence.get("kind") != "live_application":
                parser.error("live mode cannot import a synthetic result")
            if evidence.get("test_tenant_ref") != args.test_tenant_ref:
                parser.error("test tenant reference mismatch")
    else:
        if args.evidence or args.test_tenant_ref or args.harmless_fixtures:
            parser.error("live evidence flags require explicit --live")
        evidence = (
            build_template("synthetic_protocol")
            if args.template
            else run_fixtures(json.loads(FIXTURE_PATH.read_text()))
        )
    try:
        validate_evidence(evidence)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    return 1 if any(case["status"] == "fail" for case in evidence["cases"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
