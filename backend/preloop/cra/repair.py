"""Repair the one contract failure the platform can derive on its own.

An audit whose checks all ran, whose numbers are reproducible and whose
evidence pack verifies used to be discarded because one enum was wrong: the
004 run in the CRA dogfood round 2 wrote ``pass_with_findings`` where its own
``minimum_elements.passed: false`` required ``fail``. The validator was right
and the outcome was still wrong, because the platform already knew the answer.

So the platform writes it down instead of throwing the audit away. Two rules
keep that from becoming a way to launder a release:

- only the verdict label is ever rewritten, never a measurement. The fields
  the verdict is derived from (``valid``, ``minimum_elements``, ``coverage``,
  ``license_flags``, the gate) are exactly as the agent submitted them;
- a correction may only make the verdict more severe. Rewriting ``fail`` into
  ``pass`` would be the platform clearing a release it was asked to deny, so
  that direction stays a hard failure.

Every correction is recorded on the result under ``verdict_corrected`` with
both values and the reason, so a reader sees what the run said and what the
contract required.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from preloop.cra.schemas import (
    AUDIT_VERDICTS,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
)

#: Least severe first. A correction may only move to the right.
VERDICT_SEVERITY: Mapping[str, int] = {
    "pass": 0,
    "pass_with_findings": 1,
    "fail": 2,
}

#: Key the corrections are recorded under on the persisted result.
VERDICT_CORRECTED_FIELD = "verdict_corrected"

#: Who rewrote the label. Never the agent.
CORRECTED_BY = "platform_contract_validator"


@dataclass(frozen=True)
class VerdictCorrection:
    """One label the platform rewrote, with the value the agent submitted."""

    path: str
    submitted: str
    corrected: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "submitted": self.submitted,
            "corrected": self.corrected,
            "reason": self.reason,
            "corrected_by": CORRECTED_BY,
        }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def sbom_verdict_floor(body: Mapping[str, Any]) -> tuple[Optional[str], str]:
    """Least severe verdict the SBOM body itself supports, and why.

    Mirrors ``_reconcile_sbom_verdict``. ``(None, "")`` means the body forces
    nothing and any verdict the agent chose is its own call.
    """
    if not isinstance(body, Mapping):
        return None, ""
    valid = body.get("valid")
    minimum = body.get("minimum_elements")
    min_passed = minimum.get("passed") if isinstance(minimum, Mapping) else None
    if valid is False or min_passed is False:
        return (
            "fail",
            f"valid={valid!r}, minimum_elements.passed={min_passed!r}",
        )

    flags = body.get("license_flags")
    coverage = body.get("coverage") if isinstance(body.get("coverage"), Mapping) else {}
    unmatched = coverage.get("unmatched_vs_build")
    if isinstance(flags, list) and flags:
        return "pass_with_findings", "license flags are present"
    if isinstance(unmatched, list) and unmatched:
        return "pass_with_findings", "components in the build are absent from the SBOM"
    for pct_key in ("pct_with_version", "pct_with_license", "pct_with_identifier"):
        pct = coverage.get(pct_key)
        if _is_number(pct) and pct < 100:
            return "pass_with_findings", f"coverage.{pct_key} is {pct}"
    return None, ""


def release_verdict_floor(obj: Mapping[str, Any]) -> tuple[Optional[str], str]:
    """Least severe overall verdict a release audit body supports, and why.

    Mirrors ``_reconcile_release_verdict``.
    """
    if not isinstance(obj, Mapping):
        return None, ""
    sbom = obj.get("sbom_audit") if isinstance(obj.get("sbom_audit"), Mapping) else {}
    vuln = obj.get("vuln_scan") if isinstance(obj.get("vuln_scan"), Mapping) else {}
    gate = vuln.get("gate") if isinstance(vuln.get("gate"), Mapping) else {}
    if sbom.get("verdict") == "fail":
        return "fail", "sbom_audit.verdict is fail"
    if gate.get("passed") is False:
        return "fail", "vuln_scan.gate.passed is false"

    applied = gate.get("waivers_applied")
    if isinstance(applied, list) and applied:
        return "pass_with_findings", "waivers were applied"
    findings = vuln.get("findings")
    if isinstance(findings, list) and findings:
        return "pass_with_findings", "vuln_scan.findings is not empty"
    checks = obj.get("checks")
    if isinstance(checks, list) and any(
        isinstance(item, Mapping) and item.get("skipped") is True for item in checks
    ):
        return "pass_with_findings", "a check was skipped"
    return None, ""


def _escalation(
    body: Mapping[str, Any],
    floor: Optional[str],
    reason: str,
    *,
    path: str,
) -> Optional[VerdictCorrection]:
    """A correction only when the body demands a strictly more severe label."""
    submitted = body.get("verdict")
    if floor is None or submitted == floor:
        return None
    if submitted not in AUDIT_VERDICTS or floor not in VERDICT_SEVERITY:
        # "error" and unknown labels are not repairable: an incomplete run is
        # not a completed one with the wrong word on it.
        return None
    if VERDICT_SEVERITY[floor] <= VERDICT_SEVERITY[submitted]:
        # The body supports a less severe verdict than the agent chose. The
        # platform does not soften a verdict it was handed.
        return None
    return VerdictCorrection(
        path=f"{path}.verdict",
        submitted=submitted,
        corrected=floor,
        reason=reason,
    )


def verdict_corrections(payload: Any) -> tuple[Any, list[VerdictCorrection]]:
    """Return a copy with derivable verdict labels corrected, and the record.

    The payload is returned unchanged (and the list empty) when nothing is
    derivable, when the result is not a schema with a derivable verdict, or
    when the only disagreement would soften the verdict.
    """
    if not isinstance(payload, Mapping):
        return payload, []
    schema = payload.get("schema")
    if schema not in (SCHEMA_SBOMAUDIT_V1, SCHEMA_RELEASEAUDIT_V1):
        return payload, []

    corrected = copy.deepcopy(dict(payload))
    corrections: list[VerdictCorrection] = []

    if schema == SCHEMA_SBOMAUDIT_V1:
        floor, reason = sbom_verdict_floor(corrected)
        found = _escalation(corrected, floor, reason, path="result")
        if found:
            corrected["verdict"] = found.corrected
            corrections.append(found)
    else:
        sbom = corrected.get("sbom_audit")
        if isinstance(sbom, dict):
            floor, reason = sbom_verdict_floor(sbom)
            found = _escalation(sbom, floor, reason, path="result.sbom_audit")
            if found:
                sbom["verdict"] = found.corrected
                corrections.append(found)
        # After the nested correction, because a corrected sbom_audit fail
        # raises the floor for the overall verdict too.
        floor, reason = release_verdict_floor(corrected)
        found = _escalation(corrected, floor, reason, path="result")
        if found:
            corrected["verdict"] = found.corrected
            corrections.append(found)

    if not corrections:
        return payload, []

    existing = corrected.get(VERDICT_CORRECTED_FIELD)
    record = list(existing) if isinstance(existing, list) else []
    record.extend(item.as_dict() for item in corrections)
    corrected[VERDICT_CORRECTED_FIELD] = record
    return corrected, corrections


def corrections_summary(corrections: list[VerdictCorrection]) -> str:
    """One line for the execution log, naming both values."""
    return "; ".join(
        f"{item.path}: {item.submitted} -> {item.corrected} ({item.reason})"
        for item in corrections
    )
