"""Runtime validation and contradiction reconciliation for CRA result.json.

Malformed known schemas and unsupported CRA versions fail explicitly. Unknown
non-CRA JSON is left unchanged. Gaps (missing optional coverage) are advisory
unless a caller marks them as a required release policy. Agent-asserted
waivers never count as authentic human approval.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Set
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from preloop.cra.schemas import (
    AUDIT_INCOMPLETE_VERDICT,
    AUDIT_VERDICTS,
    DATABASE_SOURCES,
    DEFAULT_GATE_CVSS,
    DISCLAIMER,
    DUEDILIGENCE_OUTCOMES,
    DUEDILIGENCE_REQUIRED,
    DUEDILIGENCE_STATUSES,
    DUEDILIGENCE_VERDICTS,
    FINDING_SEVERITIES,
    FLOW_BY_SCHEMA,
    GATE_CVSS_MAX,
    GATE_CVSS_MIN,
    HEURISTIC_SOURCES,
    INCOMPLETE_ALLOWED,
    INCOMPLETE_FIELD,
    INCOMPLETE_REQUIRED,
    INCOMPLETE_SIGNALS,
    INVALID_ERROR,
    LICENSE_FLAGS,
    MATCH_KINDS,
    MISSING_ERROR,
    REGIME_PROFILE,
    RELEASEAUDIT_REQUIRED,
    RUNNER_KINDS,
    SBOM_FORMATS,
    SBOMAUDIT_REQUIRED,
    SCHEMA_DUEDILIGENCE_V1,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
    SCHEMAS_WITHOUT_STATUS,
    SOURCE_KINDS,
    SOURCE_MATRIX_KEYS,
    UNSUPPORTED_ERROR,
    VULNSCAN_REQUIRED,
    VULNSCAN_STATUSES,
    expected_cra_schema_from_prompt,
    is_cra_schema_id,
    is_known_cra_result_schema,
)
from preloop.security.gap_register import validate_gap_register
from preloop.security.waivers import (
    apply_waivers,
    normalize_waiver_id,
    validate_waiver_entries,
)

_SEVERITY_COUNT_KEYS = ("critical", "high", "medium", "low", "unknown")
_WRAP_ERROR_CODES = frozenset({INVALID_ERROR, MISSING_ERROR, UNSUPPORTED_ERROR})
_RECORDED_OUTCOMES = frozenset({"accepted", "rejected"})
_PASS_OR_FINDINGS = frozenset({"pass", "pass_with_findings"})
_GAP_STATUSES = frozenset({"gap", "partial"})
_WAIVER_CONTENT_FIELDS = ("id", "reason", "author", "date")
_WAIVER_METADATA_KEYS = frozenset({"approval_id"})
_FINDING_ACCEPT_LINE = re.compile(
    r"^\s*(?P<id>[A-Za-z0-9][A-Za-z0-9._+-]{2,})\s*:\s*(?P<reason>\S.*)$"
)

AuthorityMode = Literal["offline", "required"]
AUTHORITY_OFFLINE: AuthorityMode = "offline"
AUTHORITY_REQUIRED: AuthorityMode = "required"


def json_in(value: Any, options: Set[Any]) -> bool:
    """Return whether ``value`` is in ``options`` without raising on JSON.

    Arbitrary result.json may put lists or objects where a string enum is
    expected. ``value in frozenset`` raises ``TypeError`` for those.
    """
    if isinstance(value, (list, dict)):
        return False
    try:
        return value in options
    except TypeError:
        return False


def failure_strings(values: Any) -> list[str]:
    """Coerce a failures field into strings without joining unhashable JSON.

    Malformed envelopes may put objects in ``failures``. Those must become
    explicit invalid messages, never ``TypeError`` on ``str.join``.
    """
    if isinstance(values, str):
        return [values] if values.strip() else []
    if not isinstance(values, (list, tuple)):
        if values is None:
            return []
        return [f"non-string failure: {type(values).__name__}"]
    out: list[str] = []
    for item in values:
        if isinstance(item, str):
            if item.strip():
                out.append(item)
        elif item is not None:
            out.append(f"non-string failure: {type(item).__name__}")
    return out


class CraResultValidationError(ValueError):
    """Raised when a CRA result.json document fails contract validation."""

    def __init__(self, failures: Sequence[Any]) -> None:
        self.failures = failure_strings(failures)
        super().__init__("; ".join(self.failures) if self.failures else INVALID_ERROR)


@dataclass(frozen=True)
class PlatformApproval:
    """A platform approval record that can authenticate a human decision.

    Only fields the control plane actually stores. Reviewer identity is not
    copied onto result.json (the due-diligence contract keeps ``reviewer``
    null).
    """

    id: str
    status: str
    tool_name: str
    operation: Optional[str] = None
    tool_args: Optional[Mapping[str, Any]] = None
    tool_result: Optional[Any] = None
    responses: Optional[Sequence[Any]] = None
    approver_comment: Optional[str] = None
    resolved_at: Optional[str] = None
    decided_by_ai: bool = False
    auto_approved_reason: Optional[str] = None


@dataclass(frozen=True)
class GatePolicy:
    """Authoritative KEV/CVSS gate policy from trigger/flow/CI config.

    Never parsed from agent-authored ``gate.policy`` display text. Default is
    fail on KEV or CVSS >= 9.0. Operator override is the trigger/CI
    ``gate.fail_on_kev`` / ``gate.fail_on_cvss_gte`` fields only; there is no
    per-product policy table.
    """

    fail_on_kev: bool = True
    fail_on_cvss_gte: float = DEFAULT_GATE_CVSS


DEFAULT_GATE_POLICY = GatePolicy()


@dataclass
class CraValidationResult:
    """Outcome of validating one candidate result.json object."""

    ok: bool
    failures: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    schema_id: Optional[str] = None
    expected_schema: Optional[str] = None
    skipped: bool = False
    execution_completed: bool = False
    release_denied: bool = True
    incomplete: bool = False

    @property
    def invalid(self) -> bool:
        """True when persist must fail closed (no successful CRA release)."""
        return not self.ok and not self.skipped


def wrap_invalid_cra_result(
    raw: Any,
    failures: Sequence[str],
    *,
    error: str = INVALID_ERROR,
) -> dict[str, Any]:
    """Preserve the raw document beside an explicit validation error.

    Callers persist this object so diagnosis is possible without inventing a
    successful pack.
    """
    if isinstance(raw, Mapping) and json_in(raw.get("error"), _WRAP_ERROR_CODES):
        return dict(raw)
    safe = failure_strings(failures)
    payload: dict[str, Any] = {
        "error": error,
        "detail": "; ".join(safe) if safe else error,
        "failures": safe,
    }
    if raw is not None:
        payload["raw"] = raw
    return payload


def _finite_cvss(value: Any) -> Optional[float]:
    """Return a CVSS threshold in [0, 10], or None when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number < GATE_CVSS_MIN or number > GATE_CVSS_MAX:
        return None
    return number


def parse_gate_policy(configured: Any) -> GatePolicy:
    """Build gate policy from trustworthy operator config.

    Invalid, non-finite, or out-of-range CVSS values are ignored (default
    9.0 remains). ``fail_on_kev`` must be a JSON boolean. Model-authored
    ``gate.policy`` display strings are never consulted.
    """
    if not isinstance(configured, Mapping):
        return DEFAULT_GATE_POLICY
    fail_on_kev = DEFAULT_GATE_POLICY.fail_on_kev
    raw_kev = configured.get("fail_on_kev")
    if type(raw_kev) is bool:
        fail_on_kev = raw_kev
    cvss = DEFAULT_GATE_POLICY.fail_on_cvss_gte
    parsed = _finite_cvss(configured.get("fail_on_cvss_gte"))
    if parsed is not None:
        cvss = parsed
    return GatePolicy(fail_on_kev=fail_on_kev, fail_on_cvss_gte=cvss)


def gate_policy_from_trigger(payload: Any) -> GatePolicy:
    """Read operator gate fields from a trigger/CI payload, if present."""
    if not isinstance(payload, Mapping):
        return DEFAULT_GATE_POLICY
    gate = payload.get("gate")
    if isinstance(gate, Mapping):
        return parse_gate_policy(gate)
    nested = payload.get("payload")
    if isinstance(nested, Mapping) and isinstance(nested.get("gate"), Mapping):
        return parse_gate_policy(nested.get("gate"))
    return DEFAULT_GATE_POLICY


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool)


def _require_keys(
    obj: Mapping[str, Any], required: Sequence[str], *, path: str
) -> list[str]:
    return [f"{path}.{key} is required" for key in required if key not in obj]


def _check_disclaimer(obj: Mapping[str, Any], *, path: str) -> list[str]:
    value = obj.get("disclaimer")
    if value != DISCLAIMER:
        return [f"{path}.disclaimer must be the honesty sentence, got {value!r}"]
    return []


def _check_envelope(
    obj: Mapping[str, Any],
    *,
    schema_id: str,
    path: str = "result",
) -> list[str]:
    failures: list[str] = []
    expected_flow = FLOW_BY_SCHEMA.get(schema_id)
    if obj.get("schema") != schema_id:
        failures.append(
            f"{path}.schema must be {schema_id!r}, got {obj.get('schema')!r}"
        )
    if expected_flow is not None and obj.get("flow") != expected_flow:
        failures.append(
            f"{path}.flow must be {expected_flow!r}, got {obj.get('flow')!r}"
        )
    if obj.get("regime_profile") != REGIME_PROFILE:
        failures.append(
            f"{path}.regime_profile must be {REGIME_PROFILE!r}, "
            f"got {obj.get('regime_profile')!r}"
        )
    run_at = obj.get("run_at")
    if not isinstance(run_at, str) or not run_at.strip():
        failures.append(f"{path}.run_at must be a non-empty ISO-8601 string")
    git = obj.get("git")
    if git is not None and not isinstance(git, Mapping):
        failures.append(f"{path}.git must be an object or null")
    elif isinstance(git, Mapping):
        dirty = git.get("dirty")
        if "dirty" in git and not _is_bool(dirty):
            failures.append(
                f"{path}.git.dirty must be a boolean, not {type(dirty).__name__}"
            )
    tool_versions = obj.get("tool_versions")
    if not isinstance(tool_versions, Mapping):
        failures.append(f"{path}.tool_versions must be an object")
    inputs = obj.get("inputs_declared")
    if not isinstance(inputs, Mapping):
        failures.append(f"{path}.inputs_declared must be an object")
    runner = obj.get("runner")
    if not isinstance(runner, Mapping):
        failures.append(f"{path}.runner must be an object")
    else:
        kind = runner.get("kind")
        if kind is not None and not json_in(kind, RUNNER_KINDS):
            failures.append(
                f"{path}.runner.kind must be hosted|self_hosted|null, got {kind!r}"
            )
    if json_in(schema_id, SCHEMAS_WITHOUT_STATUS) and "status" in obj:
        failures.append(
            f"{path}.status is not part of {schema_id}; completion is the verdict"
        )
    checks = obj.get("checks")
    if not isinstance(checks, list):
        failures.append(f"{path}.checks must be a list")
    else:
        for idx, item in enumerate(checks):
            failures.extend(_check_check_item(item, path=f"{path}.checks[{idx}]"))
    assessments = obj.get("assessments")
    if not isinstance(assessments, list):
        failures.append(f"{path}.assessments must be a list")
    artifacts = obj.get("artifacts")
    if not isinstance(artifacts, Mapping):
        failures.append(f"{path}.artifacts must be an object")
    failures.extend(_check_disclaimer(obj, path=path))
    return failures


def is_incomplete_envelope(obj: Any) -> bool:
    """Return True when ``obj`` is the minimal incompletion envelope.

    The marker is an ``incomplete`` object and nothing outside the allowed
    key set. A document that also carries audit body sections (findings, a
    gate, a decision) is claiming completed work and is validated in full,
    even when it names a reason for stopping.
    """
    if not isinstance(obj, Mapping):
        return False
    if not isinstance(obj.get(INCOMPLETE_FIELD), Mapping):
        return False
    return set(obj) <= INCOMPLETE_ALLOWED


def _check_incomplete_optional(obj: Mapping[str, Any], *, path: str) -> list[str]:
    """Type-check the context fields an interrupted run may still carry."""
    failures: list[str] = []
    git = obj.get("git")
    if git is not None and not isinstance(git, Mapping):
        failures.append(f"{path}.git must be an object or null")
    for key in ("tool_versions", "inputs_declared", "runner", "artifacts"):
        if key in obj and not isinstance(obj.get(key), Mapping):
            failures.append(f"{path}.{key} must be an object")
    runner = obj.get("runner")
    if isinstance(runner, Mapping):
        kind = runner.get("kind")
        if kind is not None and not json_in(kind, RUNNER_KINDS):
            failures.append(
                f"{path}.runner.kind must be hosted|self_hosted|null, got {kind!r}"
            )
    if "assessments" in obj and not isinstance(obj.get("assessments"), list):
        failures.append(f"{path}.assessments must be a list")
    checks = obj.get("checks")
    if "checks" in obj and not isinstance(checks, list):
        failures.append(f"{path}.checks must be a list")
    elif isinstance(checks, list):
        for idx, item in enumerate(checks):
            failures.extend(_check_check_item(item, path=f"{path}.checks[{idx}]"))
    return failures


def _validate_incomplete_envelope(
    obj: Mapping[str, Any], *, schema_id: str, path: str = "result"
) -> list[str]:
    """Validate the minimal envelope of a run that could not complete.

    Identity and honesty only: the reason must be stated, the completion
    signal must say error, and no audit body may be smuggled in. Callers
    treat the outcome as incomplete, which fails the execution and denies
    the release regardless of what the reason says.
    """
    failures = _require_keys(obj, INCOMPLETE_REQUIRED, path=path)
    expected_flow = FLOW_BY_SCHEMA.get(schema_id)
    if expected_flow is not None and obj.get("flow") != expected_flow:
        failures.append(
            f"{path}.flow must be {expected_flow!r}, got {obj.get('flow')!r}"
        )
    if obj.get("regime_profile") != REGIME_PROFILE:
        failures.append(
            f"{path}.regime_profile must be {REGIME_PROFILE!r}, "
            f"got {obj.get('regime_profile')!r}"
        )
    run_at = obj.get("run_at")
    if not isinstance(run_at, str) or not run_at.strip():
        failures.append(f"{path}.run_at must be a non-empty ISO-8601 string")
    incomplete = obj.get(INCOMPLETE_FIELD)
    if isinstance(incomplete, Mapping):
        reason = incomplete.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            failures.append(
                f"{path}.{INCOMPLETE_FIELD}.reason must state, in prose, what "
                "stopped the run"
            )
        stage = incomplete.get("stage")
        if stage is not None and not isinstance(stage, str):
            failures.append(f"{path}.{INCOMPLETE_FIELD}.stage must be a string or null")
    signals = INCOMPLETE_SIGNALS.get(schema_id, ())
    for signal in signals:
        if obj.get(signal) != AUDIT_INCOMPLETE_VERDICT:
            failures.append(
                f"{path}.{signal} must be {AUDIT_INCOMPLETE_VERDICT!r} in an "
                f"incompletion envelope, got {obj.get(signal)!r}"
            )
    if json_in(schema_id, SCHEMAS_WITHOUT_STATUS) and "status" in obj:
        failures.append(
            f"{path}.status is not part of {schema_id}; completion is the verdict"
        )
    failures.extend(_check_disclaimer(obj, path=path))
    failures.extend(_check_incomplete_optional(obj, path=path))
    return failures


def _check_check_item(item: Any, *, path: str) -> list[str]:
    if not isinstance(item, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    if not isinstance(item.get("name"), str) or not item["name"]:
        failures.append(f"{path}.name must be a non-empty string")
    for flag in ("passed", "skipped"):
        if flag in item and not _is_bool(item.get(flag)):
            failures.append(
                f"{path}.{flag} must be a boolean, not {type(item.get(flag)).__name__}"
            )
    return failures


def _check_source(source: Any, *, path: str) -> list[str]:
    if not isinstance(source, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    fmt = source.get("format")
    if not json_in(fmt, SBOM_FORMATS):
        failures.append(f"{path}.format must be spdx|cyclonedx, got {fmt!r}")
    spec = source.get("spec_version")
    if not isinstance(spec, str) or not spec:
        failures.append(f"{path}.spec_version must be a non-empty string")
    return failures


def _check_minimum_elements(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    if not _is_bool(value.get("passed")):
        failures.append(
            f"{path}.passed must be a boolean, not {type(value.get('passed')).__name__}"
        )
    missing = value.get("missing")
    if not isinstance(missing, list):
        failures.append(f"{path}.missing must be a list")
    return failures


def _check_coverage(
    coverage: Any,
    *,
    path: str,
    extra_keys: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """Return (failures, advisories) for a coverage object."""
    if not isinstance(coverage, Mapping):
        return [f"{path} must be an object"], []
    failures: list[str] = []
    advisories: list[str] = []
    components = coverage.get("components")
    if "components" in coverage and not _is_int(components):
        failures.append(
            f"{path}.components must be an integer, not {type(components).__name__}"
        )
    elif _is_int(components) and components < 0:
        failures.append(f"{path}.components must be >= 0")
    for key in ("pct_with_version", "pct_with_license", "pct_with_identifier") + tuple(
        extra_keys
    ):
        if key not in coverage:
            continue
        pct = coverage.get(key)
        if not _is_number(pct):
            failures.append(f"{path}.{key} must be a number, not {type(pct).__name__}")
        elif pct < 0 or pct > 100:
            failures.append(f"{path}.{key} must be between 0 and 100, got {pct}")
    unmatched = coverage.get("unmatched_vs_build")
    if unmatched is not None and not isinstance(unmatched, list):
        failures.append(f"{path}.unmatched_vs_build must be a list or null")
    if _is_int(components) and components > 0:
        for key in ("pct_with_version", "pct_with_license", "pct_with_identifier"):
            if key not in coverage:
                advisories.append(f"{path}.{key} is absent (coverage gap is advisory)")
    return failures, advisories


def _check_license_flags(flags: Any, *, path: str) -> list[str]:
    if not isinstance(flags, list):
        return [f"{path} must be a list"]
    failures: list[str] = []
    for idx, item in enumerate(flags):
        if not isinstance(item, Mapping):
            failures.append(f"{path}[{idx}] must be an object")
            continue
        flag = item.get("flag")
        if not json_in(flag, LICENSE_FLAGS):
            failures.append(
                f"{path}[{idx}].flag must be deny|flag|missing, got {flag!r}"
            )
        if not isinstance(item.get("component"), str) or not item["component"]:
            failures.append(f"{path}[{idx}].component must be a non-empty string")
    return failures


def _reconcile_sbom_verdict(
    obj: Mapping[str, Any],
    *,
    path: str,
    verdict: str,
) -> list[str]:
    """Fail when the verdict contradicts authoritative SBOM fields."""
    failures: list[str] = []
    valid = obj.get("valid")
    minimum = obj.get("minimum_elements")
    min_passed = minimum.get("passed") if isinstance(minimum, Mapping) else None
    flags = (
        obj.get("license_flags") if isinstance(obj.get("license_flags"), list) else []
    )
    coverage = obj.get("coverage") if isinstance(obj.get("coverage"), Mapping) else {}
    unmatched = (
        coverage.get("unmatched_vs_build") if isinstance(coverage, Mapping) else None
    )
    has_findings = bool(flags) or (isinstance(unmatched, list) and len(unmatched) > 0)
    if valid is False or min_passed is False:
        if verdict != "fail":
            failures.append(
                f"{path}.verdict must be fail when the SBOM is invalid or "
                f"minimum elements failed (valid={valid!r}, "
                f"minimum_elements.passed={min_passed!r})"
            )
    elif verdict == "pass" and has_findings:
        failures.append(
            f"{path}.verdict is pass but license flags or unmatched build "
            "components are present; use pass_with_findings"
        )
    elif verdict == "pass" and isinstance(coverage, Mapping):
        for pct_key in (
            "pct_with_version",
            "pct_with_license",
            "pct_with_identifier",
        ):
            pct = coverage.get(pct_key)
            if _is_number(pct) and pct < 100:
                failures.append(
                    f"{path}.verdict is pass but {pct_key} is {pct}; "
                    "coverage gaps require pass_with_findings"
                )
                break
    elif verdict == "fail" and valid is True and min_passed is True:
        # A fail with a valid SBOM and passed minimum elements contradicts
        # the 004/nested-sbom rule unless this is a nested object whose
        # overall fail is owned elsewhere. Nested sbom_audit.fail must still
        # be justified by valid/minimum_elements.
        failures.append(
            f"{path}.verdict is fail but valid is true and minimum elements "
            "passed; do not fabricate a fail"
        )
    return failures


def _validate_sbom_body(
    obj: Mapping[str, Any],
    *,
    path: str,
    require_delta_null: bool,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    advisories: list[str] = []
    failures.extend(_check_source(obj.get("source"), path=f"{path}.source"))
    if not _is_bool(obj.get("valid")):
        failures.append(
            f"{path}.valid must be a boolean, not {type(obj.get('valid')).__name__}"
        )
    failures.extend(
        _check_minimum_elements(
            obj.get("minimum_elements"), path=f"{path}.minimum_elements"
        )
    )
    cov_fail, cov_adv = _check_coverage(
        obj.get("coverage"),
        path=f"{path}.coverage",
        extra_keys=("pct_with_license_concluded", "pct_with_license_declared"),
    )
    failures.extend(cov_fail)
    advisories.extend(cov_adv)
    failures.extend(
        _check_license_flags(obj.get("license_flags"), path=f"{path}.license_flags")
    )
    if require_delta_null and "delta" in obj and obj.get("delta") is not None:
        failures.append(f"{path}.delta must be null in this schema")
    verdict = obj.get("verdict")
    if not json_in(verdict, AUDIT_VERDICTS) and verdict != AUDIT_INCOMPLETE_VERDICT:
        failures.append(
            f"{path}.verdict must be pass|pass_with_findings|fail, got {verdict!r}"
        )
    elif json_in(verdict, AUDIT_VERDICTS):
        failures.extend(_reconcile_sbom_verdict(obj, path=path, verdict=verdict))
    return failures, advisories


def _check_negative_control(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    if not isinstance(value.get("query"), str):
        failures.append(f"{path}.query must be a string")
    if not isinstance(value.get("result"), str):
        failures.append(f"{path}.result must be a string")
    if not _is_bool(value.get("method_blind")):
        failures.append(
            f"{path}.method_blind must be a boolean, not "
            f"{type(value.get('method_blind')).__name__}"
        )
    return failures


def _check_source_matrix(matrix: Any, *, path: str) -> tuple[list[str], list[str]]:
    if not isinstance(matrix, Mapping):
        return [f"{path} must be an object"], []
    failures: list[str] = []
    advisories: list[str] = []
    for key in SOURCE_MATRIX_KEYS:
        entry = matrix.get(key)
        if not isinstance(entry, Mapping):
            failures.append(f"{path}.{key} is required and must be an object")
            continue
        kind = entry.get("kind")
        expected_kind = "database" if key in DATABASE_SOURCES else "heuristic"
        if not json_in(kind, SOURCE_KINDS):
            failures.append(f"{path}.{key}.kind must be database|heuristic")
        elif key in DATABASE_SOURCES and kind != "database":
            failures.append(f"{path}.{key}.kind must be database")
        elif key in HEURISTIC_SOURCES and kind != expected_kind:
            failures.append(f"{path}.{key}.kind must be heuristic")
        for count_key in ("screenable", "blind"):
            count = entry.get(count_key)
            if not _is_int(count):
                failures.append(
                    f"{path}.{key}.{count_key} must be an integer, not "
                    f"{type(count).__name__}"
                )
            elif count < 0:
                failures.append(f"{path}.{key}.{count_key} must be >= 0")
        failures.extend(
            _check_negative_control(
                entry.get("negative_control"),
                path=f"{path}.{key}.negative_control",
            )
        )
    screened = matrix.get("screened_by_no_source")
    if "screened_by_no_source" in matrix and not _is_int(screened):
        failures.append(
            f"{path}.screened_by_no_source must be an integer, not "
            f"{type(screened).__name__}"
        )
    elif "screened_by_no_source" not in matrix:
        advisories.append(f"{path}.screened_by_no_source is absent (advisory)")
    return failures, advisories


def _check_inventory(
    inventory: Any,
    *,
    path: str,
    extra_release_fields: bool = False,
) -> tuple[list[str], list[str]]:
    if not isinstance(inventory, Mapping):
        return [f"{path} must be an object"], []
    failures: list[str] = []
    advisories: list[str] = []
    for key in ("components", "matchable", "unmatchable"):
        value = inventory.get(key)
        if not _is_int(value):
            failures.append(
                f"{path}.{key} must be an integer, not {type(value).__name__}"
            )
        elif value < 0:
            failures.append(f"{path}.{key} must be >= 0")
    components = inventory.get("components")
    matchable = inventory.get("matchable")
    unmatchable = inventory.get("unmatchable")
    if _is_int(components) and _is_int(matchable) and _is_int(unmatchable):
        if matchable + unmatchable != components:
            failures.append(
                f"{path} coverage contradiction: matchable ({matchable}) + "
                f"unmatchable ({unmatchable}) != components ({components})"
            )
    if extra_release_fields:
        for key in ("db_resolvable", "not_db_resolvable"):
            if key in inventory:
                value = inventory.get(key)
                if not _is_int(value):
                    failures.append(
                        f"{path}.{key} must be an integer, not {type(value).__name__}"
                    )
        db_r = inventory.get("db_resolvable")
        not_db = inventory.get("not_db_resolvable")
        if (
            _is_int(db_r)
            and _is_int(not_db)
            and _is_int(components)
            and db_r + not_db != components
        ):
            failures.append(
                f"{path} coverage contradiction: db_resolvable ({db_r}) + "
                f"not_db_resolvable ({not_db}) != components ({components})"
            )
        if "negative_control" in inventory:
            failures.extend(
                _check_negative_control(
                    inventory.get("negative_control"),
                    path=f"{path}.negative_control",
                )
            )
        if "by_ecosystem" in inventory and not isinstance(
            inventory.get("by_ecosystem"), Mapping
        ):
            failures.append(f"{path}.by_ecosystem must be an object")
    matrix_fail, matrix_adv = _check_source_matrix(
        inventory.get("source_matrix"), path=f"{path}.source_matrix"
    )
    failures.extend(matrix_fail)
    advisories.extend(matrix_adv)
    return failures, advisories


def _finding_enters_gate(finding: Mapping[str, Any]) -> bool:
    if finding.get("vex_status"):
        return False
    match_kind = finding.get("match_kind")
    sources = finding.get("sources") or []
    if match_kind == "heuristic":
        return False
    if (
        isinstance(sources, list)
        and sources
        and all(json_in(src, HEURISTIC_SOURCES) for src in sources)
    ):
        return False
    return True


def _check_finding(item: Any, *, path: str, allow_waived: bool) -> list[str]:
    if not isinstance(item, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    if not isinstance(item.get("id"), str) or not item["id"]:
        failures.append(f"{path}.id must be a non-empty string")
    if not isinstance(item.get("pkg"), str):
        failures.append(f"{path}.pkg must be a string")
    severity = item.get("severity")
    if not json_in(severity, FINDING_SEVERITIES):
        failures.append(f"{path}.severity must be a known severity, got {severity!r}")
    cvss = item.get("cvss")
    if cvss is not None and not _is_number(cvss):
        failures.append(f"{path}.cvss must be a number or null")
    epss = item.get("epss")
    if epss is not None and not _is_number(epss):
        failures.append(f"{path}.epss must be a number or null")
    if not _is_bool(item.get("kev")):
        failures.append(
            f"{path}.kev must be a boolean, not {type(item.get('kev')).__name__}"
        )
    sources = item.get("sources")
    if not isinstance(sources, list):
        failures.append(f"{path}.sources must be a list")
    match_kind = item.get("match_kind")
    if not json_in(match_kind, MATCH_KINDS):
        failures.append(f"{path}.match_kind must be database|heuristic")
    if allow_waived and "waived" in item and not _is_bool(item.get("waived")):
        failures.append(
            f"{path}.waived must be a boolean, not {type(item.get('waived')).__name__}"
        )
    return failures


def _check_counts_by_severity(
    counts: Any,
    findings: Sequence[Any],
    *,
    path: str,
) -> list[str]:
    if not isinstance(counts, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    for key in _SEVERITY_COUNT_KEYS:
        value = counts.get(key)
        if not _is_int(value):
            failures.append(
                f"{path}.{key} must be an integer, not {type(value).__name__}"
            )
    expected = {key: 0 for key in _SEVERITY_COUNT_KEYS}
    for item in findings:
        if isinstance(item, Mapping) and json_in(
            item.get("severity"), frozenset(_SEVERITY_COUNT_KEYS)
        ):
            expected[str(item["severity"])] += 1
    if all(_is_int(counts.get(key)) for key in _SEVERITY_COUNT_KEYS):
        for key in _SEVERITY_COUNT_KEYS:
            if counts.get(key) != expected[key]:
                failures.append(
                    f"{path}.{key} is {counts.get(key)} but findings count "
                    f"{expected[key]} (do not fabricate counts)"
                )
                break
    return failures


def _default_gate_failures(
    findings: Sequence[Any], *, policy: GatePolicy
) -> list[dict[str, Any]]:
    failing: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        if not _finding_enters_gate(item):
            continue
        cvss = item.get("cvss")
        kev = item.get("kev") is True and policy.fail_on_kev
        high_cvss = False
        if _is_number(cvss):
            score = float(cvss)
            high_cvss = math.isfinite(score) and score >= policy.fail_on_cvss_gte
        if kev or high_cvss:
            finding_id = str(item.get("id") or "")
            if finding_id:
                aliases = item.get("aliases")
                failing.append(
                    {
                        "id": finding_id,
                        "aliases": aliases if isinstance(aliases, list) else [],
                    }
                )
    return failing


def _gate_failure_ids(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(item.get("id") or "") for item in items if item.get("id")]


def _unwaived_id_set(values: Any) -> set[str]:
    keys: set[str] = set()
    if not isinstance(values, list):
        return keys
    for item in values:
        if isinstance(item, Mapping):
            key = normalize_waiver_id(item.get("id"))
        else:
            key = normalize_waiver_id(item)
        if key:
            keys.add(key)
    return keys


def _check_gate(
    gate: Any,
    findings: Sequence[Any],
    *,
    path: str,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]],
    platform_approvals: Optional[Sequence[PlatformApproval]],
    release_fields: bool,
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    gate_policy: GatePolicy = DEFAULT_GATE_POLICY,
) -> list[str]:
    if not isinstance(gate, Mapping):
        return [f"{path} must be an object"]
    failures: list[str] = []
    passed = gate.get("passed")
    if not _is_bool(passed):
        failures.append(
            f"{path}.passed must be a boolean true/false, not {type(passed).__name__}"
        )
        return failures
    policy = gate.get("policy")
    if not isinstance(policy, str) or not policy:
        failures.append(f"{path}.policy must be a non-empty string")
    computed = _default_gate_failures(findings, policy=gate_policy)
    computed_ids = _gate_failure_ids(computed)
    applied_raw = gate.get("waivers_applied")
    if "waivers_applied" in gate and not isinstance(applied_raw, list):
        failures.append(f"{path}.waivers_applied must be a list")
        applied_raw = []
    elif applied_raw is None:
        applied_raw = []
    if release_fields:
        before = gate.get("passed_before_waivers")
        if "passed_before_waivers" in gate and not _is_bool(before):
            failures.append(
                f"{path}.passed_before_waivers must be a boolean, not "
                f"{type(before).__name__}"
            )
        for list_key in (
            "unwaived_failures",
            "waivers_invalid",
            "waivers_unmatched",
        ):
            if list_key in gate and not isinstance(gate.get(list_key), list):
                failures.append(f"{path}.{list_key} must be a list")
        if _is_bool(before) and before is True and computed:
            failures.append(
                f"{path}.passed_before_waivers is true but unwaived gate "
                f"failures are present: {computed_ids}"
            )
        if _is_bool(before) and before is False and not computed and passed is True:
            failures.append(
                f"{path}.passed_before_waivers is false with no computed "
                "gate failures; do not fabricate a pre-waiver fail"
            )
        authentic: list[Mapping[str, Any]] = []
        if isinstance(applied_raw, list) and applied_raw:
            if authority == AUTHORITY_REQUIRED and platform_approvals is None:
                failures.append(
                    f"{path}.waivers_applied claimed but platform approval "
                    "authority is unavailable; fail closed"
                )
            else:
                auth_fail, authentic = _enforce_authentic_waivers(
                    applied_raw,
                    delivered_waivers=delivered_waivers,
                    platform_approvals=platform_approvals,
                    path=f"{path}.waivers_applied",
                    authority=authority,
                )
                failures.extend(auth_fail)
        outcome = apply_waivers(computed, authentic)
        expected_passed = bool(outcome["gate_passed_after_waivers"])
        if passed is True and not expected_passed:
            failures.append(
                f"{path}.passed is true but remaining unwaived KEV/CVSS "
                f"failures {outcome['unwaived_failures']} are not covered"
            )
        if passed is False and expected_passed:
            failures.append(
                f"{path}.passed is false but deterministic waiver application "
                "covers every computed gate failure"
            )
        submitted_applied = {
            normalize_waiver_id(entry.get("id"))
            for entry in applied_raw
            if isinstance(entry, Mapping)
        }
        submitted_applied.discard("")
        expected_applied = {
            normalize_waiver_id(entry["id"]) for entry in outcome["waivers_applied"]
        }
        if submitted_applied != expected_applied:
            failures.append(
                f"{path}.waivers_applied ids {sorted(submitted_applied)} != "
                f"deterministic set {sorted(expected_applied)}"
            )
        if "unwaived_failures" in gate:
            submitted_unwaived = _unwaived_id_set(gate.get("unwaived_failures"))
            expected_unwaived = {
                normalize_waiver_id(item) for item in outcome["unwaived_failures"]
            }
            expected_unwaived.discard("")
            if submitted_unwaived != expected_unwaived:
                failures.append(
                    f"{path}.unwaived_failures {sorted(submitted_unwaived)} != "
                    f"deterministic set {sorted(expected_unwaived)}"
                )
        elif passed is True and outcome["unwaived_failures"]:
            failures.append(
                f"{path}.passed is true but unwaived_failures is missing "
                f"for remaining {outcome['unwaived_failures']}"
            )
    else:
        if passed is True and computed:
            failures.append(
                f"{path}.passed is true but KEV/CVSS gate failures "
                f"{computed_ids} remain"
            )
        if passed is False and not computed:
            failures.append(
                f"{path}.passed is false but no database finding fails the "
                "configured KEV/CVSS policy"
            )
    return failures


def _waiver_field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict, bool)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()


def _waiver_content_view(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in entry if key not in _WAIVER_METADATA_KEYS}


def _delivered_waiver_matches(
    applied: Mapping[str, Any], delivered: Mapping[str, Any]
) -> bool:
    """Exact immutable waiver content, including optional scope fields.

    Extra keys such as scope/expiry/package/version/purl must match on both
    sides. Presence on only the applied entry is a silent broadening and is
    rejected.
    """
    left = _waiver_content_view(applied)
    right = _waiver_content_view(delivered)
    keys = set(left) | set(right)
    for name in keys:
        if name not in left or name not in right:
            if name in _WAIVER_CONTENT_FIELDS:
                if _waiver_field_text(left.get(name)) != _waiver_field_text(
                    right.get(name)
                ):
                    return False
                continue
            return False
        if _waiver_field_text(left.get(name)) != _waiver_field_text(right.get(name)):
            return False
    return True


def _strip_approval_trailer(text: str) -> str:
    stripped = text.strip()
    if stripped.endswith("]") and "[" in stripped:
        head, maybe = stripped.rsplit("[", 1)
        if "approval_id" in maybe.lower():
            return head.rstrip()
    return stripped


def _texts_from_tool_result(result: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(result, str) and result.strip():
        texts.append(_strip_approval_trailer(result))
        return texts
    if not isinstance(result, Mapping):
        return texts
    for key in ("answer", "answer_text", "text", "comment", "selected_option"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(_strip_approval_trailer(value))
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                texts.append(_strip_approval_trailer(block["text"]))
    elif isinstance(content, str) and content.strip():
        texts.append(_strip_approval_trailer(content))
    return texts


def _approval_human_texts(approval: PlatformApproval) -> list[str]:
    texts: list[str] = []
    if isinstance(approval.approver_comment, str) and approval.approver_comment.strip():
        texts.append(_strip_approval_trailer(approval.approver_comment))
    texts.extend(_texts_from_tool_result(approval.tool_result))
    if isinstance(approval.responses, Sequence):
        for vote in approval.responses:
            if isinstance(vote, Mapping):
                comment = vote.get("comment")
                if isinstance(comment, str) and comment.strip():
                    texts.append(_strip_approval_trailer(comment))
    return texts


def _parse_json_blob(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _collect_waiver_mappings(value: Any) -> list[Mapping[str, Any]]:
    collected: list[Mapping[str, Any]] = []
    if isinstance(value, list):
        collected.extend(item for item in value if isinstance(item, Mapping))
    elif isinstance(value, Mapping):
        nested = (
            value.get("waivers")
            or value.get("accepted")
            or value.get("entries")
            or value.get("finding_ids")
        )
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    collected.append(item)
                elif isinstance(item, str) and item.strip():
                    collected.append({"id": item.strip()})
        if value.get("id"):
            collected.append(value)
    return collected


def _ask_user_option_ids(approval: PlatformApproval) -> set[str]:
    args = approval.tool_args if isinstance(approval.tool_args, Mapping) else {}
    options = args.get("options")
    keys: set[str] = set()
    if not isinstance(options, list):
        return keys
    for item in options:
        if isinstance(item, str):
            key = normalize_waiver_id(item)
            if key:
                keys.add(key)
    return keys


def _ask_user_delivered_entries(
    approval: PlatformApproval,
) -> list[Mapping[str, Any]]:
    """Human-authored waiver objects from ask_user tool_result/responses."""
    entries: list[Mapping[str, Any]] = []
    result = approval.tool_result
    if isinstance(result, Mapping):
        for key in ("waivers", "accepted", "entries"):
            entries.extend(_collect_waiver_mappings(result.get(key)))
        selected = result.get("selected_option")
        if isinstance(selected, str) and selected.strip():
            entries.append({"id": selected.strip()})
        selected_ids = result.get("selected_options") or result.get("accepted_ids")
        if isinstance(selected_ids, list):
            for item in selected_ids:
                if isinstance(item, str) and item.strip():
                    entries.append({"id": item.strip()})
    for text in _approval_human_texts(approval):
        parsed = _parse_json_blob(text)
        if parsed is not None:
            entries.extend(_collect_waiver_mappings(parsed))
            continue
        for line in text.splitlines():
            match = _FINDING_ACCEPT_LINE.match(line)
            if match:
                entries.append(
                    {
                        "id": match.group("id"),
                        "reason": match.group("reason").strip(),
                    }
                )
    return entries


def _approval_identities(approval: PlatformApproval) -> set[str]:
    identities: set[str] = set()
    result = approval.tool_result
    if isinstance(result, Mapping):
        for key in ("answered_by", "author", "responded_by"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                identities.add(value.strip())
    if isinstance(approval.responses, Sequence):
        for vote in approval.responses:
            if not isinstance(vote, Mapping):
                continue
            decision = vote.get("decision")
            if decision not in (None, "approved"):
                continue
            for key in ("user_id", "author", "display_name", "email"):
                value = vote.get(key)
                if value is not None and str(value).strip():
                    identities.add(str(value).strip())
    return identities


def _approval_dates(approval: PlatformApproval) -> set[str]:
    dates: set[str] = set()

    def _add(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        dates.add(text)
        dates.add(text[:10])

    _add(approval.resolved_at)
    result = approval.tool_result
    if isinstance(result, Mapping):
        for key in ("answered_at", "date", "resolved_at"):
            _add(result.get(key))
    if isinstance(approval.responses, Sequence):
        for vote in approval.responses:
            if isinstance(vote, Mapping):
                _add(vote.get("timestamp"))
    return dates


def _identity_matches(author: str, identities: set[str]) -> bool:
    if not identities:
        return False
    if author in identities:
        return True
    lowered = author.lower()
    return any(item.lower() == lowered for item in identities)


def _date_matches(date: str, dates: set[str]) -> bool:
    if not dates:
        return True
    if date in dates:
        return True
    return any(item.startswith(date) or date.startswith(item[:10]) for item in dates)


def _human_platform_decision(approval: PlatformApproval) -> bool:
    """True when the stored row is a human decision, not AI or auto-approval."""
    return not approval.decided_by_ai and approval.auto_approved_reason is None


def _ask_user_waiver_matches(
    approval: PlatformApproval, entry: Mapping[str, Any]
) -> bool:
    """Authenticate an interactive 006 waiver from stored ask_user data."""
    if approval.tool_name != "ask_user":
        return False
    if approval.status != "approved":
        return False
    if not _human_platform_decision(approval):
        return False
    approval_id = str(entry.get("approval_id") or "").strip().lower()
    if not approval_id or approval.id.strip().lower() != approval_id:
        return False
    finding = normalize_waiver_id(entry.get("id"))
    if not finding:
        return False
    option_ids = _ask_user_option_ids(approval)
    if option_ids and finding not in option_ids:
        return False
    identities = _approval_identities(approval)
    author = str(entry.get("author") or "").strip()
    if not _identity_matches(author, identities):
        return False
    date = str(entry.get("date") or "").strip()
    if not _date_matches(date, _approval_dates(approval)):
        return False
    delivered = _ask_user_delivered_entries(approval)
    if not delivered:
        return False
    applied_content = _waiver_content_view(entry)
    for candidate in delivered:
        candidate_id = normalize_waiver_id(candidate.get("id"))
        if candidate_id != finding:
            continue
        merged = dict(candidate)
        merged.setdefault("author", author)
        merged.setdefault("date", date)
        if "reason" not in merged or not str(merged.get("reason") or "").strip():
            # selected_option-only records still need the human reason.
            continue
        if option_ids and candidate_id not in option_ids:
            continue
        if _delivered_waiver_matches(applied_content, merged):
            return True
    return False


def _waiver_platform_matches(
    approval: PlatformApproval, entry: Mapping[str, Any]
) -> bool:
    if approval.tool_name == "ask_user":
        return _ask_user_waiver_matches(approval, entry)
    return False


def _enforce_authentic_waivers(
    applied: Sequence[Any],
    *,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]],
    platform_approvals: Optional[Sequence[PlatformApproval]],
    path: str,
    authority: AuthorityMode = AUTHORITY_OFFLINE,
) -> tuple[list[str], list[Mapping[str, Any]]]:
    """Agent-authored waiver lists never confer authentic human approval.

    Non-object entries are reported rather than dropped. File waivers must
    match immutable contents exactly (including optional scope fields).
    Interactive waivers bind stored ``ask_user`` tool_result/responses.
    ``request_approval`` rows, including a ``waive_finding`` operation, do
    not confer waiver authority. Ambiguous approvals do not confer waiver
    authority.
    """
    failures: list[str] = []
    valid_entries, invalid = validate_waiver_entries(applied)
    for message in invalid:
        failures.append(f"{path}: {message}")
    authentic: list[Mapping[str, Any]] = []
    delivered_present = delivered_waivers is not None
    for entry in valid_entries:
        approval_id = str(entry.get("approval_id") or "").strip().lower()
        matched_input = False
        if delivered_present:
            matched_input = any(
                isinstance(candidate, Mapping)
                and _delivered_waiver_matches(entry, candidate)
                for candidate in delivered_waivers or []
            )
        matched_approval = False
        if approval_id:
            if platform_approvals is None:
                if authority == AUTHORITY_REQUIRED:
                    failures.append(
                        f"{path} waiver {entry.get('id')!r} claims approval_id "
                        "but platform approval authority is unavailable"
                    )
                    continue
            else:
                matched_approval = any(
                    _waiver_platform_matches(approval, entry)
                    for approval in platform_approvals
                )
                if not matched_approval:
                    failures.append(
                        f"{path} waiver {entry.get('id')!r} approval_id is not "
                        "a granted ask_user waiver for this finding"
                    )
                    continue
        if delivered_present:
            if not matched_input:
                failures.append(
                    f"{path} waiver {entry.get('id')!r} does not match the "
                    "delivered waiver contents (id, reason, author, date, "
                    "and any scope/expiry/package/version fields)"
                )
                continue
            authentic.append(entry)
            continue
        if matched_approval:
            authentic.append(entry)
            continue
        failures.append(
            f"{path} waiver {entry.get('id')!r} is agent-asserted and "
            "does not match delivered waiver input or an authentic "
            "ask_user approval; authentic human approval is required"
        )
    return failures, authentic


def _validate_vuln_body(
    obj: Mapping[str, Any],
    *,
    path: str,
    standalone: bool,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]],
    platform_approvals: Optional[Sequence[PlatformApproval]],
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    gate_policy: GatePolicy = DEFAULT_GATE_POLICY,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    advisories: list[str] = []
    if standalone:
        failures.extend(
            _check_source(obj.get("source_sbom"), path=f"{path}.source_sbom")
        )
        if obj.get("new_since_last_run") is not None:
            failures.append(f"{path}.new_since_last_run must be null in this schema")
        db_versions = obj.get("db_versions")
        if not isinstance(db_versions, Mapping):
            failures.append(f"{path}.db_versions must be an object")
    else:
        db_versions = obj.get("db_versions")
        if not isinstance(db_versions, Mapping):
            failures.append(f"{path}.db_versions must be an object")
    inv_fail, inv_adv = _check_inventory(
        obj.get("inventory"),
        path=f"{path}.inventory",
        extra_release_fields=not standalone,
    )
    failures.extend(inv_fail)
    advisories.extend(inv_adv)
    findings = obj.get("findings")
    if not isinstance(findings, list):
        failures.append(f"{path}.findings must be a list")
        findings = []
    else:
        for idx, item in enumerate(findings):
            failures.extend(
                _check_finding(
                    item, path=f"{path}.findings[{idx}]", allow_waived=not standalone
                )
            )
    failures.extend(
        _check_counts_by_severity(
            obj.get("counts_by_severity"),
            findings if isinstance(findings, list) else [],
            path=f"{path}.counts_by_severity",
        )
    )
    candidates = obj.get("art14_candidates")
    if not isinstance(candidates, list):
        failures.append(f"{path}.art14_candidates must be a list")
    failures.extend(
        _check_gate(
            obj.get("gate"),
            findings if isinstance(findings, list) else [],
            path=f"{path}.gate",
            delivered_waivers=delivered_waivers,
            platform_approvals=platform_approvals,
            release_fields=not standalone,
            authority=authority,
            gate_policy=gate_policy,
        )
    )
    return failures, advisories


def _validate_sbomaudit(
    obj: Mapping[str, Any],
) -> tuple[list[str], list[str], bool, bool]:
    failures = _require_keys(obj, SBOMAUDIT_REQUIRED, path="result")
    failures.extend(_check_envelope(obj, schema_id=SCHEMA_SBOMAUDIT_V1))
    body_fail, advisories = _validate_sbom_body(
        obj, path="result", require_delta_null=True
    )
    failures.extend(body_fail)
    verdict = obj.get("verdict")
    incomplete = verdict == AUDIT_INCOMPLETE_VERDICT
    completed = json_in(verdict, AUDIT_VERDICTS)
    return failures, advisories, completed, incomplete


def _validate_vulnscan(
    obj: Mapping[str, Any],
    *,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]],
    platform_approvals: Optional[Sequence[PlatformApproval]],
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    gate_policy: GatePolicy = DEFAULT_GATE_POLICY,
) -> tuple[list[str], list[str], bool, bool]:
    failures = _require_keys(obj, VULNSCAN_REQUIRED, path="result")
    failures.extend(_check_envelope(obj, schema_id=SCHEMA_VULNSCAN_V1))
    status = obj.get("status")
    if not json_in(status, VULNSCAN_STATUSES):
        failures.append(f"result.status must be success|error, got {status!r}")
    if "verdict" in obj:
        failures.append("result.verdict is not part of preloop.cra.vulnscan/v1")
    body_fail, advisories = _validate_vuln_body(
        obj,
        path="result",
        standalone=True,
        delivered_waivers=delivered_waivers,
        platform_approvals=platform_approvals,
        authority=authority,
        gate_policy=gate_policy,
    )
    failures.extend(body_fail)
    incomplete = status == "error"
    completed = status == "success"
    return failures, advisories, completed, incomplete


def _reconcile_release_verdict(
    obj: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    overall = obj.get("verdict")
    if not json_in(overall, AUDIT_VERDICTS) and overall != AUDIT_INCOMPLETE_VERDICT:
        return failures
    if overall == AUDIT_INCOMPLETE_VERDICT:
        return failures
    sbom = obj.get("sbom_audit") if isinstance(obj.get("sbom_audit"), Mapping) else {}
    vuln = obj.get("vuln_scan") if isinstance(obj.get("vuln_scan"), Mapping) else {}
    sbom_verdict = sbom.get("verdict") if isinstance(sbom, Mapping) else None
    gate = vuln.get("gate") if isinstance(vuln, Mapping) else {}
    gate_passed = gate.get("passed") if isinstance(gate, Mapping) else None
    applied = gate.get("waivers_applied") if isinstance(gate, Mapping) else []
    has_waivers = isinstance(applied, list) and len(applied) > 0
    if sbom_verdict == "fail" and overall != "fail":
        failures.append("result.verdict must be fail when sbom_audit.verdict is fail")
    if gate_passed is False and overall != "fail":
        failures.append(
            "result.verdict must be fail when vuln_scan.gate.passed is false"
        )
    if has_waivers and overall == "pass":
        failures.append(
            "result.verdict cannot be pass when waivers were applied; "
            "the ceiling is pass_with_findings"
        )
    findings = vuln.get("findings") if isinstance(vuln, Mapping) else []
    skipped_checks = [
        item
        for item in (obj.get("checks") or [])
        if isinstance(item, Mapping) and item.get("skipped") is True
    ]
    has_findings = (
        (isinstance(findings, list) and len(findings) > 0)
        or has_waivers
        or bool(skipped_checks)
    )
    if overall == "pass" and has_findings:
        failures.append(
            "result.verdict is pass but findings, waivers, or skipped checks "
            "are present; use pass_with_findings"
        )
    if (
        overall == "fail"
        and json_in(sbom_verdict, _PASS_OR_FINDINGS)
        and gate_passed is True
    ):
        failures.append(
            "result.verdict is fail but sbom_audit did not fail and the "
            "severity gate passed; do not fabricate an overall fail"
        )
    return failures


def _validate_releaseaudit(
    obj: Mapping[str, Any],
    *,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]],
    platform_approvals: Optional[Sequence[PlatformApproval]],
    previous_gap_register: Optional[Mapping[str, Any]],
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    gate_policy: GatePolicy = DEFAULT_GATE_POLICY,
) -> tuple[list[str], list[str], bool, bool]:
    failures = _require_keys(obj, RELEASEAUDIT_REQUIRED, path="result")
    failures.extend(_check_envelope(obj, schema_id=SCHEMA_RELEASEAUDIT_V1))
    sbom = obj.get("sbom_audit")
    if not isinstance(sbom, Mapping):
        failures.append("result.sbom_audit must be an object")
        advisories: list[str] = []
    else:
        body_fail, advisories = _validate_sbom_body(
            sbom, path="result.sbom_audit", require_delta_null=False
        )
        failures.extend(body_fail)
    vuln = obj.get("vuln_scan")
    if not isinstance(vuln, Mapping):
        failures.append("result.vuln_scan must be an object")
    else:
        vuln_fail, vuln_adv = _validate_vuln_body(
            vuln,
            path="result.vuln_scan",
            standalone=False,
            delivered_waivers=delivered_waivers,
            platform_approvals=platform_approvals,
            authority=authority,
            gate_policy=gate_policy,
        )
        failures.extend(vuln_fail)
        advisories.extend(vuln_adv)
    drift = obj.get("drift")
    if drift is not None and not isinstance(drift, Mapping):
        failures.append("result.drift must be an object or null")
    gap = obj.get("gap_register")
    if gap is not None:
        try:
            failures.extend(validate_gap_register(obj, previous=previous_gap_register))
        except (TypeError, AttributeError, ValueError) as exc:
            failures.append(f"gap_register is malformed: {type(exc).__name__}: {exc}")
        if isinstance(gap, Mapping) and gap.get("ran") is True:
            items = gap.get("items") or []
            gap_or_partial = [
                item
                for item in items
                if isinstance(item, Mapping)
                and json_in(item.get("status"), _GAP_STATUSES)
            ]
            ready = gap.get("ready")
            secrets_count = gap.get("secrets_findings_count")
            if ready is True and (gap_or_partial or secrets_count not in (0, None)):
                failures.append(
                    "gap_register.ready is true but gap/partial items or "
                    "secrets findings remain"
                )
            if not _is_bool(ready) and "ready" in gap:
                failures.append(
                    f"gap_register.ready must be a boolean, not {type(ready).__name__}"
                )
    storage = obj.get("evidence_storage")
    if storage is not None and not isinstance(storage, Mapping):
        failures.append("result.evidence_storage must be an object or null")
    verdict = obj.get("verdict")
    if not json_in(verdict, AUDIT_VERDICTS) and verdict != AUDIT_INCOMPLETE_VERDICT:
        failures.append(
            f"result.verdict must be pass|pass_with_findings|fail, got {verdict!r}"
        )
    failures.extend(_reconcile_release_verdict(obj))
    incomplete = verdict == AUDIT_INCOMPLETE_VERDICT
    completed = json_in(verdict, AUDIT_VERDICTS)
    return failures, advisories, completed, incomplete


def _due_diligence_approval_matches(
    approvals: Sequence[PlatformApproval],
    *,
    outcome: str,
    operation: str,
) -> bool:
    """Bind a recorded outcome to an exact request_approval row.

    ``ask_user`` answers are not component-risk approvals. Missing
    ``tool_args.operation`` cannot authorize an unrelated granted row.
    AI-judged and auto-approved rows cannot record a human decision.
    """
    expected_status = "approved" if outcome == "accepted" else "declined"
    for approval in approvals:
        if approval.tool_name != "request_approval":
            continue
        if approval.status != expected_status:
            continue
        if not _human_platform_decision(approval):
            continue
        if not approval.operation or approval.operation != operation:
            continue
        return True
    return False


def _validate_duediligence(
    obj: Mapping[str, Any],
    *,
    platform_approvals: Optional[Sequence[PlatformApproval]],
    authority: AuthorityMode = AUTHORITY_OFFLINE,
) -> tuple[list[str], list[str], bool, bool]:
    failures = _require_keys(obj, DUEDILIGENCE_REQUIRED, path="result")
    failures.extend(_check_envelope(obj, schema_id=SCHEMA_DUEDILIGENCE_V1))
    advisories: list[str] = []
    status = obj.get("status")
    verdict = obj.get("verdict")
    if not json_in(status, DUEDILIGENCE_STATUSES):
        failures.append(f"result.status must be success|error, got {status!r}")
    if not json_in(verdict, DUEDILIGENCE_VERDICTS):
        failures.append(f"result.verdict must be recorded|error, got {verdict!r}")
    component = obj.get("component")
    if not isinstance(component, Mapping):
        failures.append("result.component must be an object")
    else:
        if not isinstance(component.get("name"), str) or not component["name"]:
            failures.append("result.component.name must be a non-empty string")
        if not isinstance(component.get("version"), str) or not component["version"]:
            failures.append("result.component.version must be a non-empty string")
    evidence = obj.get("evidence")
    if not isinstance(evidence, Mapping):
        failures.append("result.evidence must be an object")
    else:
        ce_decl = evidence.get("ce_declaration")
        if isinstance(ce_decl, Mapping):
            verified = ce_decl.get("authenticity_verified")
            if verified is not False:
                failures.append(
                    "result.evidence.ce_declaration.authenticity_verified "
                    "must be boolean false (presence is reported, never authenticity)"
                )
        unknowns = evidence.get("open_unknowns")
        if unknowns is not None and not isinstance(unknowns, list):
            failures.append("result.evidence.open_unknowns must be a list")
    decision = obj.get("decision")
    if not isinstance(decision, Mapping):
        failures.append("result.decision must be an object")
        outcome = None
    else:
        outcome = decision.get("outcome")
        if not json_in(outcome, DUEDILIGENCE_OUTCOMES):
            failures.append(
                "result.decision.outcome must be accepted|rejected|pending, "
                f"got {outcome!r}"
            )
        if decision.get("reviewer") is not None:
            failures.append(
                "result.decision.reviewer must be null; reviewer identity "
                "lives in the Preloop approval audit trail"
            )
        decided_via = decision.get("decided_via")
        operation = decision.get("approval_operation")
        if json_in(outcome, _RECORDED_OUTCOMES):
            if decided_via != "preloop_approval":
                failures.append(
                    "result.decision.decided_via must be preloop_approval "
                    "when a human decision is recorded"
                )
            if not isinstance(operation, str) or not operation.strip():
                failures.append(
                    "result.decision.approval_operation must name the "
                    "request_approval operation"
                )
            elif authority == AUTHORITY_REQUIRED and platform_approvals is None:
                failures.append(
                    "result.decision claims a recorded human outcome but "
                    "platform approval authority is unavailable; fail closed"
                )
            elif platform_approvals is not None or authority == AUTHORITY_REQUIRED:
                records = platform_approvals or []
                if not _due_diligence_approval_matches(
                    records,
                    outcome=str(outcome),
                    operation=operation.strip(),
                ):
                    failures.append(
                        "result.decision claims a recorded human outcome but "
                        "no matching request_approval exists for this "
                        "execution, operation, and decision"
                    )
        if outcome == "pending" and verdict == "recorded":
            failures.append(
                "result.verdict cannot be recorded while decision.outcome is pending"
            )
    if verdict == "recorded":
        if status != "success":
            failures.append("result.verdict recorded requires result.status success")
        if not json_in(outcome, _RECORDED_OUTCOMES):
            failures.append(
                "result.verdict recorded requires decision.outcome accepted or rejected"
            )
    if (
        verdict == "error"
        and status == "success"
        and json_in(outcome, _RECORDED_OUTCOMES)
    ):
        failures.append(
            "result.verdict is error but a human accepted/rejected decision "
            "was recorded; verdict must be recorded"
        )
    record = obj.get("record")
    if not isinstance(record, Mapping):
        failures.append("result.record must be an object")
    elif "committed" in record and not _is_bool(record.get("committed")):
        failures.append(
            "result.record.committed must be a boolean, not "
            f"{type(record.get('committed')).__name__}"
        )
    incomplete = verdict == "error" or status == "error"
    completed = verdict == "recorded" and status == "success"
    return failures, advisories, completed, incomplete


def _release_denied_for(
    schema_id: str,
    obj: Mapping[str, Any],
    *,
    completed: bool,
    incomplete: bool,
) -> bool:
    if incomplete or not completed:
        return True
    if schema_id in {SCHEMA_SBOMAUDIT_V1, SCHEMA_RELEASEAUDIT_V1}:
        return obj.get("verdict") != "pass"
    if schema_id == SCHEMA_VULNSCAN_V1:
        gate = obj.get("gate")
        passed = gate.get("passed") if isinstance(gate, Mapping) else None
        return passed is not True
    if schema_id == SCHEMA_DUEDILIGENCE_V1:
        decision = obj.get("decision")
        outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
        return outcome != "accepted"
    return True


def result_claims_authority(
    payload: Any,
    *,
    expected_schema: Optional[str] = None,
    prompt: Optional[str] = None,
) -> bool:
    """Return True when a CRA result asserts a human approval, waiver, or decision.

    Non-CRA JSON never starts an approval query, even if it contains a
    ``decision`` object. Expected CRA context (prompt / schema) or an
    actual CRA schema id is required first.
    """
    if not isinstance(payload, Mapping):
        return False
    expected = expected_schema or expected_cra_schema_from_prompt(prompt)
    source: Mapping[str, Any] = payload
    if json_in(payload.get("error"), _WRAP_ERROR_CODES):
        raw = payload.get("raw")
        if isinstance(raw, Mapping):
            source = raw
        else:
            return False
    claimed = source.get("schema") if isinstance(source.get("schema"), str) else None
    looking_at_cra = bool(expected) or is_cra_schema_id(claimed)
    if not looking_at_cra:
        return False
    gate = source.get("gate")
    if isinstance(gate, Mapping):
        applied = gate.get("waivers_applied")
        if isinstance(applied, list) and applied:
            return True
    vuln = source.get("vuln_scan")
    if isinstance(vuln, Mapping):
        nested_gate = vuln.get("gate")
        if isinstance(nested_gate, Mapping):
            applied = nested_gate.get("waivers_applied")
            if isinstance(applied, list) and applied:
                return True
        findings = vuln.get("findings")
        if isinstance(findings, list):
            for item in findings:
                if isinstance(item, Mapping) and (
                    item.get("waived") is True or item.get("approval_id")
                ):
                    return True
    findings = source.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, Mapping) and (
                item.get("waived") is True or item.get("approval_id")
            ):
                return True
    decision = source.get("decision")
    if isinstance(decision, Mapping) and json_in(
        decision.get("outcome"), _RECORDED_OUTCOMES
    ):
        return True
    return False


def validate_cra_result(
    payload: Any,
    *,
    expected_schema: Optional[str] = None,
    prompt: Optional[str] = None,
    delivered_waivers: Optional[Sequence[Mapping[str, Any]]] = None,
    platform_approvals: Optional[Sequence[PlatformApproval]] = None,
    previous_gap_register: Optional[Mapping[str, Any]] = None,
    require_coverage: bool = False,
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    gate_policy: Optional[GatePolicy] = None,
) -> CraValidationResult:
    """Validate a candidate execution result against the CRA contracts.

    Args:
        payload: Parsed JSON object (or None if the agent wrote nothing).
        expected_schema: Schema id this flow is contracted to emit. When
            omitted, derived from ``prompt`` via ``Required shape``.
        prompt: Flow prompt used to detect expected CRA runs.
        delivered_waivers: Waiver entries from trigger/seed input. Agent
            lists that do not match this input (or a platform approval id)
            are not authentic human approval.
        platform_approvals: Control-plane approval rows for this execution.
            ``None`` with ``authority="offline"`` skips DB-backed matching
            (structural validation only). ``None`` with ``authority="required"``
            fails closed when the result claims a decision or waiver.
            An empty list means the lookup ran and found none.
        previous_gap_register: Prior run's gap register (freeze floor).
        require_coverage: When True, coverage advisories become failures
            (explicit release policy). Default treats gaps as advisory.
        authority: ``offline`` for schema-only checks; ``required`` for
            persist/release paths that must authenticate claimed decisions.
        gate_policy: Authoritative KEV/CVSS thresholds from trigger/flow/CI
            config. Default is KEV or CVSS >= 9.0. Never derived from
            model-authored ``gate.policy`` display text.

    Returns:
        :class:`CraValidationResult`. ``skipped`` means non-CRA JSON and
        must be persisted unchanged.
    """
    expected = expected_schema or expected_cra_schema_from_prompt(prompt)
    policy = gate_policy if gate_policy is not None else DEFAULT_GATE_POLICY
    if payload is None:
        if expected:
            return CraValidationResult(
                ok=False,
                failures=[
                    f"expected CRA result schema {expected} but no result.json "
                    "was persisted"
                ],
                expected_schema=expected,
                schema_id=expected if is_known_cra_result_schema(expected) else None,
            )
        return CraValidationResult(ok=True, skipped=True)

    if not isinstance(payload, Mapping):
        if expected:
            return CraValidationResult(
                ok=False,
                failures=["expected CRA result.json object, got non-object"],
                expected_schema=expected,
            )
        return CraValidationResult(ok=True, skipped=True)

    schema_id = payload.get("schema")
    if isinstance(schema_id, str) and schema_id in {
        INVALID_ERROR,
        MISSING_ERROR,
        UNSUPPORTED_ERROR,
    }:
        schema_id = None

    if json_in(payload.get("error"), _WRAP_ERROR_CODES):
        detail = str(payload.get("detail") or payload["error"])
        raw_failures = payload.get("failures")
        failures = failure_strings(raw_failures)
        if not failures:
            failures = [detail]
        return CraValidationResult(
            ok=False,
            failures=failures,
            expected_schema=expected,
            schema_id=(
                payload.get("raw", {}).get("schema")
                if isinstance(payload.get("raw"), Mapping)
                else None
            ),
        )

    claimed = schema_id if isinstance(schema_id, str) else None
    looking_at_cra = bool(expected) or is_cra_schema_id(claimed)

    if not looking_at_cra:
        return CraValidationResult(ok=True, skipped=True, schema_id=claimed)

    if expected and not claimed:
        return CraValidationResult(
            ok=False,
            failures=[
                f"expected CRA schema {expected} but result.json has no schema field"
            ],
            expected_schema=expected,
        )

    if claimed and not is_known_cra_result_schema(claimed):
        if is_cra_schema_id(claimed):
            return CraValidationResult(
                ok=False,
                failures=[
                    f"unsupported CRA result schema {claimed!r}; known ids: "
                    + ", ".join(sorted(FLOW_BY_SCHEMA))
                ],
                expected_schema=expected,
                schema_id=claimed,
            )
        if expected:
            return CraValidationResult(
                ok=False,
                failures=[
                    f"expected CRA schema {expected} but result.schema is {claimed!r}"
                ],
                expected_schema=expected,
                schema_id=claimed,
            )
        return CraValidationResult(ok=True, skipped=True, schema_id=claimed)

    if (
        expected
        and is_cra_schema_id(expected)
        and not is_known_cra_result_schema(expected)
    ):
        return CraValidationResult(
            ok=False,
            failures=[
                f"unsupported CRA result schema {expected!r} required by this flow"
            ],
            expected_schema=expected,
            schema_id=claimed,
        )

    if expected and claimed and claimed != expected:
        return CraValidationResult(
            ok=False,
            failures=[
                f"expected CRA schema {expected} but result.schema is {claimed!r}"
            ],
            expected_schema=expected,
            schema_id=claimed,
        )

    assert claimed is not None
    if is_incomplete_envelope(payload):
        # A run that stopped early reports why instead of inventing a body.
        # It is never a completion and never a release.
        incomplete_failures = _validate_incomplete_envelope(payload, schema_id=claimed)
        return CraValidationResult(
            ok=not incomplete_failures,
            failures=incomplete_failures,
            schema_id=claimed,
            expected_schema=expected,
            execution_completed=False,
            release_denied=True,
            incomplete=True,
        )

    if claimed == SCHEMA_SBOMAUDIT_V1:
        failures, advisories, completed, incomplete = _validate_sbomaudit(payload)
    elif claimed == SCHEMA_VULNSCAN_V1:
        failures, advisories, completed, incomplete = _validate_vulnscan(
            payload,
            delivered_waivers=delivered_waivers,
            platform_approvals=platform_approvals,
            authority=authority,
            gate_policy=policy,
        )
    elif claimed == SCHEMA_RELEASEAUDIT_V1:
        failures, advisories, completed, incomplete = _validate_releaseaudit(
            payload,
            delivered_waivers=delivered_waivers,
            platform_approvals=platform_approvals,
            previous_gap_register=previous_gap_register,
            authority=authority,
            gate_policy=policy,
        )
    elif claimed == SCHEMA_DUEDILIGENCE_V1:
        failures, advisories, completed, incomplete = _validate_duediligence(
            payload,
            platform_approvals=platform_approvals,
            authority=authority,
        )
    else:
        return CraValidationResult(
            ok=False,
            failures=[f"unsupported CRA result schema {claimed!r}"],
            expected_schema=expected,
            schema_id=claimed,
        )

    if require_coverage and advisories:
        failures.extend(advisories)
        advisories = []

    ok = not failures
    release_denied = True
    if ok:
        release_denied = _release_denied_for(
            claimed, payload, completed=completed, incomplete=incomplete
        )
    return CraValidationResult(
        ok=ok,
        failures=failures,
        advisories=advisories,
        schema_id=claimed,
        expected_schema=expected,
        execution_completed=ok and completed and not incomplete,
        release_denied=release_denied if ok else True,
        incomplete=incomplete,
    )


def assert_cra_result(payload: Any, **kwargs: Any) -> CraValidationResult:
    """Raise :class:`CraResultValidationError` when validation fails."""
    result = validate_cra_result(payload, **kwargs)
    if result.invalid:
        raise CraResultValidationError(result.failures)
    return result
