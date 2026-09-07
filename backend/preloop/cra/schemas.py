"""Versioned CRA result.json schema identifiers and shared field contracts.

These ids are the production contract for presets 004–007. Nested stub and
evidence-pack schemas are not execution ``result.json`` documents.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Mapping, Optional

SCHEMA_SBOMAUDIT_V1 = "preloop.cra.sbomaudit/v1"
SCHEMA_VULNSCAN_V1 = "preloop.cra.vulnscan/v1"
SCHEMA_RELEASEAUDIT_V1 = "preloop.cra.releaseaudit/v1"
SCHEMA_DUEDILIGENCE_V1 = "preloop.cra.duediligence/v1"

# Result.json contracts only. Stub/pack schemas live in evidence files.
CRA_RESULT_SCHEMAS: FrozenSet[str] = frozenset(
    {
        SCHEMA_SBOMAUDIT_V1,
        SCHEMA_VULNSCAN_V1,
        SCHEMA_RELEASEAUDIT_V1,
        SCHEMA_DUEDILIGENCE_V1,
    }
)

CRA_SCHEMA_PREFIX = "preloop.cra."

FLOW_BY_SCHEMA: Mapping[str, str] = {
    SCHEMA_SBOMAUDIT_V1: "sbom-verify",
    SCHEMA_VULNSCAN_V1: "sbom-exploit-check",
    SCHEMA_RELEASEAUDIT_V1: "release-security-audit",
    SCHEMA_DUEDILIGENCE_V1: "component-due-diligence",
}

REGIME_PROFILE = "cra"

DISCLAIMER = (
    "Machine-generated evidence for conformity assessment support. "
    "Not a conformity assessment, certification, or legal advice."
)

AUDIT_VERDICTS: FrozenSet[str] = frozenset({"pass", "pass_with_findings", "fail"})
# Orchestrator incompletion signal: the audit could not complete. Not a
# successful release and not a completed-fail audit.
AUDIT_INCOMPLETE_VERDICT = "error"

VULNSCAN_STATUSES: FrozenSet[str] = frozenset({"success", "error"})
DUEDILIGENCE_STATUSES: FrozenSet[str] = frozenset({"success", "error"})
DUEDILIGENCE_VERDICTS: FrozenSet[str] = frozenset({"recorded", "error"})
DUEDILIGENCE_OUTCOMES: FrozenSet[str] = frozenset({"accepted", "rejected", "pending"})

SBOM_FORMATS: FrozenSet[str] = frozenset({"spdx", "cyclonedx"})
LICENSE_FLAGS: FrozenSet[str] = frozenset({"deny", "flag", "missing"})
FINDING_SEVERITIES: FrozenSet[str] = frozenset(
    {"critical", "high", "medium", "low", "unknown"}
)
MATCH_KINDS: FrozenSet[str] = frozenset({"database", "heuristic"})
SOURCE_KINDS: FrozenSet[str] = frozenset({"database", "heuristic"})
SOURCE_MATRIX_KEYS: tuple[str, ...] = (
    "osv_purl",
    "osv_git",
    "nvd_cpe",
    "osv_distro",
)
DATABASE_SOURCES: FrozenSet[str] = frozenset({"osv_purl", "osv_git"})
HEURISTIC_SOURCES: FrozenSet[str] = frozenset({"nvd_cpe", "osv_distro"})

RUNNER_KINDS: FrozenSet[str] = frozenset({"hosted", "self_hosted"})

ENVELOPE_REQUIRED: tuple[str, ...] = (
    "schema",
    "flow",
    "run_at",
    "git",
    "tool_versions",
    "inputs_declared",
    "runner",
    "regime_profile",
    "checks",
    "assessments",
    "artifacts",
    "disclaimer",
)

SBOMAUDIT_REQUIRED: tuple[str, ...] = ENVELOPE_REQUIRED + (
    "source",
    "valid",
    "minimum_elements",
    "coverage",
    "license_flags",
    "delta",
    "verdict",
)

VULNSCAN_REQUIRED: tuple[str, ...] = ENVELOPE_REQUIRED + (
    "status",
    "source_sbom",
    "db_versions",
    "inventory",
    "findings",
    "counts_by_severity",
    "art14_candidates",
    "gate",
    "new_since_last_run",
)

RELEASEAUDIT_REQUIRED: tuple[str, ...] = ENVELOPE_REQUIRED + (
    "sbom_audit",
    "vuln_scan",
    "drift",
    "verdict",
    "gap_register",
    "evidence_storage",
)

DUEDILIGENCE_REQUIRED: tuple[str, ...] = ENVELOPE_REQUIRED + (
    "component",
    "product",
    "usage_context",
    "evidence",
    "decision",
    "record",
    "status",
    "verdict",
)

# 004/006 have no top-level status. 005 has status+gate and no verdict.
SCHEMAS_WITHOUT_STATUS: FrozenSet[str] = frozenset(
    {SCHEMA_SBOMAUDIT_V1, SCHEMA_RELEASEAUDIT_V1}
)
SCHEMAS_WITH_STATUS: FrozenSet[str] = frozenset(
    {SCHEMA_VULNSCAN_V1, SCHEMA_DUEDILIGENCE_V1}
)

DEFAULT_GATE_CVSS = 9.0

_REQUIRED_SHAPE = re.compile(r"Required shape \((preloop\.cra\.[^)\s]+)\)")

CAPTURE_ERROR_CODES: FrozenSet[str] = frozenset(
    {
        "result_artifact_fetch_failed",
        "result_artifact_too_large",
        "result_artifact_invalid_json",
        "result_artifact_not_object",
        "cra_result_invalid",
        "cra_result_missing",
        "cra_schema_unsupported",
    }
)

INVALID_ERROR = "cra_result_invalid"
MISSING_ERROR = "cra_result_missing"
UNSUPPORTED_ERROR = "cra_schema_unsupported"


def is_cra_schema_id(value: object) -> bool:
    """Return True when ``value`` looks like a CRA schema id string."""
    return isinstance(value, str) and value.startswith(CRA_SCHEMA_PREFIX)


def is_known_cra_result_schema(value: object) -> bool:
    """Return True when ``value`` is a supported CRA result.json schema id."""
    return isinstance(value, str) and value in CRA_RESULT_SCHEMAS


def expected_cra_schema_from_prompt(prompt: Optional[str]) -> Optional[str]:
    """Return the result.json schema this flow is contracted to write.

    Uses the YAML ``Required shape (schema-id):`` marker so nested mentions
    of stub/pack schemas or sibling audit ids do not misclassify the flow.
    Unknown ``preloop.cra.*`` ids in that marker still count as expected CRA
    (unsupported version) so a missing schema cannot evade validation.
    """
    if not prompt:
        return None
    matches = _REQUIRED_SHAPE.findall(prompt)
    for schema_id in matches:
        if schema_id in CRA_RESULT_SCHEMAS or (
            schema_id.startswith(CRA_SCHEMA_PREFIX)
            and "/v" in schema_id
            and "repostub" not in schema_id
            and "evidencepack" not in schema_id
        ):
            return schema_id
    return None
