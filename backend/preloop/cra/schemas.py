"""Versioned CRA result.json schema identifiers and shared field contracts.

These ids are the production contract for presets 004–007. Nested stub and
evidence-pack schemas are not execution ``result.json`` documents.
"""

from __future__ import annotations

import re
from datetime import timedelta
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

# Incompletion envelope. Every schema below requires a full audit body, so
# a run that stopped early (a human decision that never arrived, an input
# that was never delivered) had no valid way to say so and its report was
# discarded as a contract violation. The minimal envelope carries identity,
# the schema's completion signal set to "error", and a stated reason. It is
# a first-class result that always fails the execution and never releases.
INCOMPLETE_FIELD = "incomplete"
INCOMPLETE_REQUIRED: tuple[str, ...] = (
    "schema",
    "flow",
    "run_at",
    "regime_profile",
    INCOMPLETE_FIELD,
    "disclaimer",
)
# Optional context an interrupted run may still know. Audit body sections
# are deliberately absent: a document that carries findings, a gate, or a
# decision is claiming work, and claimed work is validated in full.
#
# "drift" is the one exception, and only for a release audit. Drift is a
# self-contained comparison against the previous run that finishes before the
# gate and the waiver question the run then dies on; round 2 wrote the whole
# analysis to evidence/drift-report.md and left the field null, so a consumer
# reading the envelope saw no drift at all. It is admitted on the same terms
# as any other claim: validated in full, and only when the report it
# summarizes is named under artifacts.
INCOMPLETE_OPTIONAL: tuple[str, ...] = (
    "git",
    "tool_versions",
    "inputs_declared",
    "runner",
    "checks",
    "assessments",
    "artifacts",
    "status",
    "verdict",
    "drift",
)
# Schemas whose incompletion envelope may carry the drift block.
INCOMPLETE_DRIFT_SCHEMAS: FrozenSet[str] = frozenset({SCHEMA_RELEASEAUDIT_V1})
INCOMPLETE_ALLOWED: FrozenSet[str] = frozenset(
    INCOMPLETE_REQUIRED + INCOMPLETE_OPTIONAL
)
# The completion signal each schema must set to the incompletion value.
INCOMPLETE_SIGNALS: Mapping[str, tuple[str, ...]] = {
    SCHEMA_SBOMAUDIT_V1: ("verdict",),
    SCHEMA_RELEASEAUDIT_V1: ("verdict",),
    SCHEMA_VULNSCAN_V1: ("status",),
    SCHEMA_DUEDILIGENCE_V1: ("status", "verdict"),
}
DUEDILIGENCE_STATUSES: FrozenSet[str] = frozenset({"success", "error"})
DUEDILIGENCE_VERDICTS: FrozenSet[str] = frozenset({"recorded", "error"})
DUEDILIGENCE_OUTCOMES: FrozenSet[str] = frozenset({"accepted", "rejected", "pending"})

SBOM_FORMATS: FrozenSet[str] = frozenset({"spdx", "cyclonedx"})
LICENSE_FLAGS: FrozenSet[str] = frozenset({"deny", "flag", "missing"})
FINDING_SEVERITIES: FrozenSet[str] = frozenset(
    {"critical", "high", "medium", "low", "unknown"}
)
MATCH_KINDS: FrozenSet[str] = frozenset({"database", "heuristic"})

# VEX statuses that can take a finding out of the severity gate population,
# and only with a machine-readable justification next to them. "affected"
# and "under_investigation" are statements that the finding stands, so they
# never suppress; a bare "not_affected" with no justification is an
# assertion rather than evidence and does not suppress either. Applied
# BEFORE the gate so a supplier's own VEX cannot be the thing that raises a
# danger approval (round 2 CRA rerun, P5).
VEX_SUPPRESSING_STATUSES: FrozenSet[str] = frozenset(
    {"not_affected", "fixed", "false_positive"}
)
VEX_NON_SUPPRESSING_STATUSES: FrozenSet[str] = frozenset(
    {"affected", "under_investigation"}
)
# Why the gate would have failed on a suppressed finding. Recorded so the
# suppression can be audited against the policy it displaced.
VEX_SUPPRESSION_REASONS: FrozenSet[str] = frozenset({"kev", "cvss", "unscored"})
# --- CRA Article 14 reporting -------------------------------------------
# The obligation (report an actively exploited vulnerability: early warning
# in 24 h, notification in 72 h, final report in 14 d) applies from
# 11 September 2026. The result contract carries the judgement and the
# clock; it never files anything and it is not a legal determination.
ART14_EARLY_WARNING = timedelta(hours=24)
ART14_NOTIFICATION = timedelta(hours=72)
ART14_FINAL_REPORT = timedelta(days=14)
ART14_DEADLINE_KEYS: tuple[str, ...] = (
    "early_warning_24h",
    "notification_72h",
    "final_report_14d",
)
# "undetermined" is not a synonym for "nothing to report". It is the
# required answer whenever the KEV fetch failed or the scan did not
# complete, so silence cannot read as safety.
ART14_ASSESSMENTS: FrozenSet[str] = frozenset(
    {"no_reportable_vulnerability", "reportable_candidate", "undetermined"}
)
ART14_UNDETERMINED = "undetermined"
ART14_REPORTABLE = "reportable_candidate"
ART14_NONE = "no_reportable_vulnerability"
# What the run actually read to call a vulnerability exploited. "none"
# means no exploitation evidence was found, not that none exists.
ART14_EXPLOITED_EVIDENCE: FrozenSet[str] = frozenset({"kev", "vendor_advisory", "none"})
# Where the affected/not-affected call came from. Unknown keeps the
# candidate undetermined rather than quietly clearing it.
ART14_AFFECTED_SOURCES: FrozenSet[str] = frozenset(
    {"vex", "reachability", "manual", "unknown"}
)
ART14_AFFECTED_VALUES: FrozenSet[str] = frozenset({"undetermined"})
# Where the manufacturer is in the filing workflow. Preloop tracks the
# state; it does not submit to the ENISA single reporting platform.
ART14_STATUSES: FrozenSet[str] = frozenset(
    {"none", "drafted", "submitted", "out_of_scope"}
)
ART14_STATUS_NEEDS_REASON = "out_of_scope"
ART14_REPORTING_FIELD = "reporting"
ART14_BASIS_004 = (
    "SBOM verification does not screen for vulnerabilities; run preset 005 or 006"
)

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
GATE_CVSS_MIN = 0.0
GATE_CVSS_MAX = 10.0

# Preset 006 collects interactive waivers via ask_user. A request_approval
# operation named waive_finding is not a waiver authority path.
WAIVE_FINDING_OPERATION = "waive_finding"
WAIVE_FINDING_DECISIONS = frozenset({"waive", "accept"})

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
