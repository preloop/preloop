"""Repair contract failures the platform can derive on its own.

An audit whose checks all ran, whose numbers are reproducible and whose
evidence pack verifies used to be discarded because one enum was wrong: the
004 run in the CRA dogfood round 2 wrote ``pass_with_findings`` where its own
``minimum_elements.passed: false`` required ``fail``. The validator was right
and the outcome was still wrong, because the platform already knew the answer.

Two further defects are the same shape. ``minimum_elements`` used to be the
agent's claim about the SBOM, and a document author was enough for that claim
to read ``passed: true``. The platform now measures the delivered bytes
(:mod:`preloop.cra.sbom_measure`). Replacing the claim with that object is
not rewriting a measurement: the agent's field was a claim, and the
measurement of the bytes is the authority. ``counts_by_severity`` is
arithmetic over the findings the agent already submitted, so a mismatched
aggregate is rewritten from that list. Findings themselves are never edited.

Three rules keep this from laundering a release:

- a correction to a measurement may only make the result more severe. An
  agent who already failed minimum elements keeps that claim.
  ``counts_by_severity`` is always derived from the findings, in either
  direction, because the findings are never edited and the gate reads the
  findings, not the aggregate;
- the SBOM verdict label (004, and ``sbom_audit`` inside a release audit)
  still moves only toward a more severe label. The overall release-audit
  verdict is recomputed from the document's facts
  (:mod:`preloop.cra.verdict`) in both directions, but a downward
  correction moves one step at most, and a ``fail`` is never moved when
  the gate, SBOM validity or minimum elements did not pass. Coverage,
  license flags, the gate and the findings stay as submitted;
- every correction is recorded on the result under ``verdict_corrected``,
  with the submitted value and the reason. A minimum-elements correction
  keeps the agent's claim on that record.

``vuln_scan.closed_by_vex`` and ``limitations`` on a release audit are
platform facts derived from the findings and the checks. An agent-written
value that disagrees is replaced and recorded the same way.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from preloop.cra.sbom_measure import MEASURED_FIELD
from preloop.cra.schemas import (
    AUDIT_VERDICTS,
    CLOSED_BY_VEX_FIELD,
    LIMITATIONS_FIELD,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
)
from preloop.cra.verdict import (
    PASS_DEFINITION,
    limitation_names,
    release_verdict_basis,
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
    """One field the platform rewrote, with the value the agent submitted."""

    path: str
    submitted: str
    corrected: str
    reason: str
    agent_claim: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "path": self.path,
            "submitted": self.submitted,
            "corrected": self.corrected,
            "reason": self.reason,
            "corrected_by": CORRECTED_BY,
        }
        if self.agent_claim is not None:
            record["agent_claim"] = self.agent_claim
        return record


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

    A thin view over :func:`preloop.cra.verdict.release_verdict_basis`:
    ``(None, "")`` means the facts support ``pass``.
    """
    if not isinstance(obj, Mapping):
        return None, ""
    basis = release_verdict_basis(obj)
    if basis.verdict == "pass":
        return None, ""
    return basis.verdict, basis.reasons[0]


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


def _release_recompute(obj: Mapping[str, Any]) -> Optional[VerdictCorrection]:
    """Recompute the overall release verdict from the document's facts.

    Both directions are corrections, within two bounds: a ``fail`` is never
    moved when the gate, SBOM validity or minimum elements did not pass
    (:func:`preloop.cra.verdict.fail_locked`), and a downward correction
    moves one step (``fail`` to ``pass_with_findings``, or
    ``pass_with_findings`` to ``pass``). An upward correction goes straight
    to what the facts require, as it always has.
    """
    submitted = obj.get("verdict")
    if not isinstance(submitted, str) or submitted not in AUDIT_VERDICTS:
        # "error" and unknown labels are not repairable.
        return None
    basis = release_verdict_basis(obj)
    derived = basis.verdict
    if derived == submitted:
        return None
    if VERDICT_SEVERITY[derived] > VERDICT_SEVERITY[submitted]:
        return VerdictCorrection(
            path="result.verdict",
            submitted=submitted,
            corrected=derived,
            reason="; ".join(basis.reasons),
        )
    if submitted == "fail" and basis.fail_locked:
        return None
    step_down = {"fail": "pass_with_findings", "pass_with_findings": "pass"}
    # One step down; ``derived`` is below ``submitted``, so never past it.
    target = step_down[submitted]
    if target == "pass":
        reason = f"{PASS_DEFINITION} Recomputed: {basis.summary()}"
    else:
        reason = (
            "the gate, SBOM validity and minimum elements passed; "
            f"recomputed: {basis.summary()}"
        )
    return VerdictCorrection(
        path="result.verdict",
        submitted=submitted,
        corrected=target,
        reason=reason,
    )


def verdict_corrections(payload: Any) -> tuple[Any, list[VerdictCorrection]]:
    """Return a copy with derivable verdict labels corrected, and the record.

    The payload is returned unchanged (and the list empty) when nothing is
    derivable or the result is not a schema with a derivable verdict. SBOM
    verdicts only escalate; the overall release verdict is recomputed in
    both directions within the bounds of :func:`_release_recompute`.
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
        found = _release_recompute(corrected)
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


SEVERITY_COUNT_KEYS: tuple[str, ...] = (
    "critical",
    "high",
    "medium",
    "low",
    "unknown",
)


#: Failure prefixes for platform-derived facts. The persist boundary
#: re-derives these, so they do not count as an unrelated contract failure.
DERIVED_FACT_FAILURE_PREFIXES: tuple[str, ...] = (
    f"result.vuln_scan.{CLOSED_BY_VEX_FIELD}",
    f"result.{LIMITATIONS_FIELD}",
    f"result.drift.{CLOSED_BY_VEX_FIELD}",
    f"result.drift.{LIMITATIONS_FIELD}",
)


def failures_are_only_counts(failures: Sequence[str]) -> bool:
    """True when every failure is a ``counts_by_severity`` disagreement.

    A second, unrelated failure means the run still fails closed. Counts are
    repaired only when they are the whole of the contract failure. A
    disagreement on a platform-derived fact (``closed_by_vex``,
    ``limitations``) is not unrelated: persist re-derives it anyway.
    """
    if not failures:
        return False
    if not any("counts_by_severity" in item for item in failures):
        return False
    return all(
        "counts_by_severity" in item or item.startswith(DERIVED_FACT_FAILURE_PREFIXES)
        for item in failures
    )


def _record(body: dict[str, Any], corrections: list[VerdictCorrection]) -> None:
    if not corrections:
        return
    existing = body.get(VERDICT_CORRECTED_FIELD)
    record = list(existing) if isinstance(existing, list) else []
    record.extend(item.as_dict() for item in corrections)
    body[VERDICT_CORRECTED_FIELD] = record


def _sbom_body(
    payload: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], str]:
    """Return ``(copy, sbom body, minimum_elements path)`` for a CRA audit."""
    schema = payload.get("schema")
    if schema == SCHEMA_SBOMAUDIT_V1:
        corrected = copy.deepcopy(dict(payload))
        return corrected, corrected, "result.minimum_elements"
    if schema == SCHEMA_RELEASEAUDIT_V1:
        corrected = copy.deepcopy(dict(payload))
        sbom = corrected.get("sbom_audit")
        if not isinstance(sbom, dict):
            return None, None, ""
        return corrected, sbom, "result.sbom_audit.minimum_elements"
    return None, None, ""


def apply_measured_minimum_elements(
    payload: Any,
) -> tuple[Any, list[VerdictCorrection]]:
    """Replace a too-lenient minimum-elements claim with the measurement.

    The measurement must already be attached under
    :data:`~preloop.cra.sbom_measure.MEASURED_FIELD`. When the agent said
    ``passed: true`` and the measurement found missing elements, the claim
    is replaced by the measured object and kept on the correction record.
    An agent who is already stricter than the measurement is left alone.
    A skipped measurement changes nothing.

    Returns:
        The payload (a copy only when a correction was applied) and the
        corrections, which may be empty.
    """
    if not isinstance(payload, Mapping):
        return payload, []
    measured_holder: Any = payload
    if payload.get("schema") == SCHEMA_RELEASEAUDIT_V1:
        measured_holder = payload.get("sbom_audit")
    if not isinstance(measured_holder, Mapping):
        return payload, []
    measured = measured_holder.get(MEASURED_FIELD)
    if not isinstance(measured, Mapping) or measured.get("status") == "skipped":
        return payload, []
    if measured.get("passed") is not False:
        return payload, []
    claim = measured_holder.get("minimum_elements")
    claim_passed = claim.get("passed") if isinstance(claim, Mapping) else None
    if claim_passed is not True:
        return payload, []

    corrected, body, path = _sbom_body(payload)
    if corrected is None or body is None:
        return payload, []
    agent_claim = copy.deepcopy(dict(claim)) if isinstance(claim, Mapping) else {}
    body["minimum_elements"] = copy.deepcopy(dict(measured))
    correction = VerdictCorrection(
        path=path,
        submitted="true",
        corrected="false",
        reason=(
            "agent claimed minimum_elements.passed true; platform measurement "
            "of the delivered SBOM bytes found missing elements"
        ),
        agent_claim=agent_claim,
    )
    _record(corrected, [correction])
    return corrected, [correction]


def _derive_counts(findings: Any) -> dict[str, int]:
    counts = {key: 0 for key in SEVERITY_COUNT_KEYS}
    if not isinstance(findings, list):
        return counts
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        severity = item.get("severity")
        if isinstance(severity, str) and severity in counts:
            counts[severity] += 1
    return counts


def apply_derived_severity_counts(
    payload: Any,
) -> tuple[Any, list[VerdictCorrection]]:
    """Recompute ``counts_by_severity`` from the submitted findings.

    Findings are not edited. Each key that disagrees is recorded with the
    reported value and the derived value. When the submitted counts already
    match, the payload is returned unchanged.
    """
    if not isinstance(payload, Mapping):
        return payload, []
    schema = payload.get("schema")
    if schema not in (SCHEMA_VULNSCAN_V1, SCHEMA_RELEASEAUDIT_V1):
        return payload, []
    corrected = copy.deepcopy(dict(payload))
    if schema == SCHEMA_VULNSCAN_V1:
        parents = [("result", corrected)]
    else:
        vuln = corrected.get("vuln_scan")
        if not isinstance(vuln, dict):
            return payload, []
        parents = [("result.vuln_scan", vuln)]

    corrections: list[VerdictCorrection] = []
    for path, parent in parents:
        derived = _derive_counts(parent.get("findings"))
        reported = parent.get("counts_by_severity")
        reported_map = reported if isinstance(reported, Mapping) else {}
        for key in SEVERITY_COUNT_KEYS:
            current = reported_map.get(key)
            if current == derived[key]:
                continue
            submitted = "absent" if key not in reported_map else str(current)
            corrections.append(
                VerdictCorrection(
                    path=f"{path}.counts_by_severity.{key}",
                    submitted=submitted,
                    corrected=str(derived[key]),
                    reason="derived from the findings list",
                )
            )
        if any(
            item.path.startswith(f"{path}.counts_by_severity") for item in corrections
        ):
            parent["counts_by_severity"] = derived
    if not corrections:
        return payload, []
    _record(corrected, corrections)
    return corrected, corrections


def _tally_correction(
    path: str, submitted: Any, derived: Any, reason: str
) -> VerdictCorrection:
    return VerdictCorrection(
        path=path,
        submitted=repr(submitted),
        corrected=repr(derived),
        reason=reason,
    )


def _stamp_drift(
    drift: dict[str, Any], closed: int, names: list[str]
) -> list[VerdictCorrection]:
    """Write the current tallies into ``drift``, keeping the agent's baseline.

    ``previous`` is what the baseline result recorded (``None`` when it
    predates these fields); only the agent can read the baseline, so only
    ``current`` is stamped. A disagreeing ``current`` is recorded.
    """
    corrections: list[VerdictCorrection] = []
    for key, current in ((CLOSED_BY_VEX_FIELD, closed), (LIMITATIONS_FIELD, names)):
        block = drift.get(key)
        submitted = block.get("current") if isinstance(block, dict) else None
        if isinstance(block, dict) and "current" in block and submitted != current:
            corrections.append(
                _tally_correction(
                    f"result.drift.{key}.current",
                    submitted,
                    current,
                    "derived from this run's findings and checks",
                )
            )
        previous = block.get("previous") if isinstance(block, dict) else None
        drift[key] = {"previous": previous, "current": current}
    return corrections


def apply_derived_verdict_facts(
    payload: Any,
) -> tuple[Any, list[VerdictCorrection], bool]:
    """Stamp ``closed_by_vex``, ``limitations`` and their drift on a release audit.

    Both are derived from the document (the findings and their VEX status,
    and the checks) and never taken from the agent. An agent value that
    disagrees is replaced and recorded as a correction; an absent one is
    simply stamped.

    Returns:
        The payload (a copy only when something was stamped), the
        corrections, and whether anything was stamped.
    """
    if not isinstance(payload, Mapping):
        return payload, [], False
    if payload.get("schema") != SCHEMA_RELEASEAUDIT_V1:
        return payload, [], False
    if not isinstance(payload.get("vuln_scan"), Mapping):
        return payload, [], False
    corrected = copy.deepcopy(dict(payload))
    vuln = corrected["vuln_scan"]
    basis = release_verdict_basis(corrected)
    corrections: list[VerdictCorrection] = []

    if CLOSED_BY_VEX_FIELD in vuln and vuln[CLOSED_BY_VEX_FIELD] != basis.closed_by_vex:
        corrections.append(
            _tally_correction(
                f"result.vuln_scan.{CLOSED_BY_VEX_FIELD}",
                vuln[CLOSED_BY_VEX_FIELD],
                basis.closed_by_vex,
                "derived from the findings list and each finding's VEX status",
            )
        )
    vuln[CLOSED_BY_VEX_FIELD] = basis.closed_by_vex

    if (
        LIMITATIONS_FIELD in corrected
        and corrected[LIMITATIONS_FIELD] != basis.limitations
    ):
        corrections.append(
            _tally_correction(
                f"result.{LIMITATIONS_FIELD}",
                corrected[LIMITATIONS_FIELD],
                basis.limitations,
                "derived from the skipped checks that name a missing input",
            )
        )
    corrected[LIMITATIONS_FIELD] = copy.deepcopy(basis.limitations)

    drift = corrected.get("drift")
    if isinstance(drift, dict):
        corrections.extend(
            _stamp_drift(
                drift, basis.closed_by_vex, limitation_names(basis.limitations)
            )
        )

    _record(corrected, corrections)
    return corrected, corrections, corrected != dict(payload)
