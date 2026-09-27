"""Release-audit verdict semantics, derived from the document's own facts.

``pass`` means: minimum elements passed, the severity gate passed, there are
no open (non-VEX-closed) findings, no failed cross-checks, and no gap or
partial gap-register items.

The verdict used to be held at ``pass_with_findings`` by two things that are
not findings against the product. A finding the supplier had closed with a
justified VEX statement counted as "findings remain", and a cross-check that
never ran because its input was not delivered counted like a failure. The
rules below separate those from what still holds a release:

- a finding with ``vex_status`` ``not_affected`` (recognised justification)
  or ``fixed`` (justification recorded), and a statement id, is *closed*. It
  stays in the findings ledger and the evidence pack and is counted in
  ``vuln_scan.closed_by_vex``. ``affected``, ``under_investigation``, an
  unjustified or free-text ``not_affected``, and ``false_positive`` leave it
  open;
- a check skipped because an input was not delivered (it names that input
  in ``missing_input``, and ``inputs_declared`` does not contradict it) is a
  *limitation* recorded in ``limitations[]``. Any other skip, and any check
  that ran and failed, still holds the verdict;
- gap-register ``gap`` / ``partial`` items and secrets findings hold the
  verdict; ``met`` and ``declared`` items do not. A check that mirrors a
  register item (``gap_register_<item id>``) follows its item;
- waivers, a blind top-level negative control, and components no source
  could screen keep holding the verdict, as before.

Everything here is a pure function of the submitted document. The persist
boundary (:mod:`preloop.cra.repair`) and the validator
(:mod:`preloop.cra.validate`) both read it, so the label the platform
corrects to and the label the validator accepts cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from preloop.cra.schemas import (
    MISSING_INPUT_FIELD,
    VEX_CLOSING_STATUSES,
    VEX_RECOGNISED_JUSTIFICATIONS,
)
from preloop.security.gap_register import secrets_findings_count

#: The sentence the presets and the docs carry. Kept here so a test can
#: hold every copy of it to the implementation.
PASS_DEFINITION = (
    "pass means: minimum elements passed, gate passed, no open "
    "(non-VEX-closed) findings, no failed cross-checks, no gap/partial "
    "register items."
)

GAP_HOLDING_STATUSES = frozenset({"gap", "partial"})
GAP_CHECK_PREFIX = "gap_register_"

#: ``inputs_declared`` values that say an input was not delivered.
_ABSENT_TEXT = frozenset({"", "none", "null", "n/a", "na", "absent", "missing"})
_ABSENT_PREFIXES = ("not ", "no ", "none")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def vex_closure(finding: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """Return the VEX statement that closes this finding, or ``None``.

    A finding is closed when its status is ``not_affected`` with a
    justification from the OpenVEX / CSAF / CycloneDX vocabulary, or
    ``fixed`` with a recorded justification (OpenVEX defines no vocabulary
    for ``fixed``), and in both cases a statement id is recorded.

    Args:
        finding: One entry from ``vuln_scan.findings``.

    Returns:
        ``{"vex_status", "vex_statement_id", "vex_justification"}`` for a
        closed finding, otherwise ``None``.
    """
    if not isinstance(finding, Mapping):
        return None
    status = _text(finding.get("vex_status")).lower()
    if status not in VEX_CLOSING_STATUSES:
        return None
    statement_id = _text(finding.get("vex_statement_id"))
    justification = _text(finding.get("vex_justification"))
    if not statement_id or not justification:
        return None
    if (
        status == "not_affected"
        and justification.lower() not in VEX_RECOGNISED_JUSTIFICATIONS
    ):
        return None
    return {
        "vex_status": status,
        "vex_statement_id": statement_id,
        "vex_justification": justification,
    }


def count_closed_by_vex(findings: Any) -> int:
    """Number of findings a valid VEX statement closes."""
    if not isinstance(findings, list):
        return 0
    return sum(1 for item in findings if vex_closure(item) is not None)


def open_findings(findings: Any) -> list[Mapping[str, Any]]:
    """Findings that are not closed by VEX, in submitted order."""
    if not isinstance(findings, list):
        return []
    return [
        item
        for item in findings
        if isinstance(item, Mapping) and vex_closure(item) is None
    ]


def input_declared_delivered(inputs_declared: Any, name: str) -> bool:
    """True when ``inputs_declared`` says the named input *was* delivered.

    An unknown key is not a contradiction: some inputs (a component subset a
    heuristic screen targets, for example) are not declared inputs at all.
    """
    inputs = _mapping(inputs_declared)
    if name not in inputs:
        return False
    value = inputs[name]
    if value is None or value is False:
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        return not (text in _ABSENT_TEXT or text.startswith(_ABSENT_PREFIXES))
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


@dataclass(frozen=True)
class CheckClassification:
    """How each entry in ``checks[]`` bears on the verdict."""

    limitations: list[dict[str, str]] = field(default_factory=list)
    unexplained_skips: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _gap_item_ids(obj: Mapping[str, Any]) -> set[str]:
    register = _mapping(obj.get("gap_register"))
    items = register.get("items")
    if not isinstance(items, list):
        return set()
    return {
        _text(item.get("id"))
        for item in items
        if isinstance(item, Mapping) and _text(item.get("id"))
    }


def _governed_by_register(name: str, gap_ids: set[str]) -> bool:
    if name in gap_ids:
        return True
    return name.startswith(GAP_CHECK_PREFIX) and (
        name[len(GAP_CHECK_PREFIX) :] in gap_ids
    )


def classify_checks(obj: Mapping[str, Any]) -> CheckClassification:
    """Split ``checks[]`` into limitations, holding skips and failures.

    Args:
        obj: A ``preloop.cra.releaseaudit/v1`` document.

    Returns:
        The classification. Limitations are ``{"check", "missing_input"}``
        in submitted order.
    """
    result = CheckClassification()
    checks = obj.get("checks")
    if not isinstance(checks, list):
        return result
    inputs = obj.get("inputs_declared")
    gap_ids = _gap_item_ids(obj)
    for idx, item in enumerate(checks):
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name")) or f"checks[{idx}]"
        if item.get("skipped") is True:
            missing = _text(item.get(MISSING_INPUT_FIELD))
            if missing and not input_declared_delivered(inputs, missing):
                result.limitations.append({"check": name, "missing_input": missing})
            else:
                result.unexplained_skips.append(name)
            continue
        if item.get("passed") is False and not _governed_by_register(name, gap_ids):
            result.failed.append(name)
    return result


def derive_limitations(obj: Mapping[str, Any]) -> list[dict[str, str]]:
    """The ``limitations[]`` list the platform records on a release audit."""
    return classify_checks(obj).limitations


def fail_locked(obj: Mapping[str, Any]) -> bool:
    """True when a ``fail`` label may not be corrected to anything else.

    The gate did not pass, or the SBOM is not valid, or minimum elements did
    not pass, or the nested SBOM audit failed. Anything other than an
    explicit ``True`` locks: a malformed gate is not a passed gate.
    """
    sbom = _mapping(obj.get("sbom_audit"))
    gate = _mapping(_mapping(obj.get("vuln_scan")).get("gate"))
    minimum = _mapping(sbom.get("minimum_elements"))
    return (
        gate.get("passed") is not True
        or sbom.get("valid") is not True
        or minimum.get("passed") is not True
        or sbom.get("verdict") == "fail"
    )


@dataclass(frozen=True)
class ReleaseVerdictBasis:
    """The verdict a release audit's facts support, and why."""

    verdict: str
    reasons: list[str]
    closed_by_vex: int
    limitations: list[dict[str, str]]
    fail_locked: bool

    def summary(self) -> str:
        """One line naming the reasons and the two tallies."""
        tallies = (
            f"closed_by_vex={self.closed_by_vex}, limitations={len(self.limitations)}"
        )
        if not self.reasons:
            return f"no holding facts ({tallies})"
        return f"{'; '.join(self.reasons)} ({tallies})"


def _fail_reasons(obj: Mapping[str, Any]) -> list[str]:
    sbom = _mapping(obj.get("sbom_audit"))
    gate = _mapping(_mapping(obj.get("vuln_scan")).get("gate"))
    minimum = _mapping(sbom.get("minimum_elements"))
    reasons: list[str] = []
    if sbom.get("verdict") == "fail":
        reasons.append("sbom_audit.verdict is fail")
    if gate.get("passed") is False:
        reasons.append("vuln_scan.gate.passed is false")
    if sbom.get("valid") is False:
        reasons.append("sbom_audit.valid is false")
    if minimum.get("passed") is False:
        reasons.append("sbom_audit.minimum_elements.passed is false")
    return reasons


def _unrecorded_facts(obj: Mapping[str, Any]) -> list[str]:
    """Pass needs positive facts; a missing one holds, it does not fail."""
    sbom = _mapping(obj.get("sbom_audit"))
    gate = _mapping(_mapping(obj.get("vuln_scan")).get("gate"))
    minimum = _mapping(sbom.get("minimum_elements"))
    reasons: list[str] = []
    if gate.get("passed") is not True and gate.get("passed") is not False:
        reasons.append("vuln_scan.gate.passed is not recorded")
    if sbom.get("valid") is not True and sbom.get("valid") is not False:
        reasons.append("sbom_audit.valid is not recorded")
    if minimum.get("passed") is not True and minimum.get("passed") is not False:
        reasons.append("sbom_audit.minimum_elements.passed is not recorded")
    return reasons


def _coverage_holds(vuln: Mapping[str, Any]) -> list[str]:
    inventory = _mapping(vuln.get("inventory"))
    reasons: list[str] = []
    control = _mapping(inventory.get("negative_control"))
    if control.get("method_blind") is True:
        reasons.append("the inventory negative control is blind")
    matrix = _mapping(inventory.get("source_matrix"))
    unscreened = matrix.get("screened_by_no_source")
    if (
        isinstance(unscreened, int)
        and not isinstance(unscreened, bool)
        and unscreened > 0
    ):
        reasons.append(f"{unscreened} components were screened by no source")
    return reasons


def _gap_holds(obj: Mapping[str, Any]) -> list[str]:
    register = obj.get("gap_register")
    if not isinstance(register, Mapping):
        return []
    reasons: list[str] = []
    items = register.get("items")
    holding = [
        _text(item.get("id")) or f"items[{idx}]"
        for idx, item in enumerate(items if isinstance(items, list) else [])
        if isinstance(item, Mapping)
        and _text(item.get("status")).lower() in GAP_HOLDING_STATUSES
    ]
    if holding:
        reasons.append("gap register has gap or partial items: " + ", ".join(holding))
    rows = register.get("secrets_findings") or register.get("history_rows") or []
    secrets = secrets_findings_count(rows if isinstance(rows, list) else [])
    if secrets:
        reasons.append(f"gap register has {secrets} secrets findings")
    return reasons


def release_verdict_basis(obj: Mapping[str, Any]) -> ReleaseVerdictBasis:
    """Derive the verdict a release audit's own facts support.

    Args:
        obj: A ``preloop.cra.releaseaudit/v1`` document.

    Returns:
        The derived verdict with every reason that holds it, the
        ``closed_by_vex`` tally, the limitations, and whether a ``fail``
        label is locked.
    """
    vuln = _mapping(obj.get("vuln_scan"))
    gate = _mapping(vuln.get("gate"))
    findings = vuln.get("findings")
    checks = classify_checks(obj)
    closed = count_closed_by_vex(findings)
    locked = fail_locked(obj)

    fail_reasons = _fail_reasons(obj)
    if fail_reasons:
        return ReleaseVerdictBasis(
            verdict="fail",
            reasons=fail_reasons,
            closed_by_vex=closed,
            limitations=checks.limitations,
            fail_locked=locked,
        )

    reasons = _unrecorded_facts(obj)
    applied = gate.get("waivers_applied")
    if isinstance(applied, list) and applied:
        reasons.append("waivers were applied")
    still_open = open_findings(findings)
    if still_open:
        reasons.append(
            f"vuln_scan has {len(still_open)} open findings not closed by VEX"
        )
    if checks.unexplained_skips:
        reasons.append("a check was skipped")
    if checks.failed:
        reasons.append("checks ran and failed: " + ", ".join(checks.failed))
    reasons.extend(_gap_holds(obj))
    reasons.extend(_coverage_holds(vuln))
    return ReleaseVerdictBasis(
        verdict="pass_with_findings" if reasons else "pass",
        reasons=reasons,
        closed_by_vex=closed,
        limitations=checks.limitations,
        fail_locked=locked,
    )


def limitation_names(limitations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Check names of a limitations list, for drift."""
    return [
        _text(item.get("check"))
        for item in limitations
        if isinstance(item, Mapping) and _text(item.get("check"))
    ]
