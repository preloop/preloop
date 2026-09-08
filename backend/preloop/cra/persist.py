"""Persisted-execution boundary for CRA result.json validation.

Hosted capture sanitizes the agent JSON, then
:func:`apply_cra_persist_boundary` validates it. Private-runner completion
uses the same boundary so a malformed known schema cannot be stored as a
successful release. Raw evidence is preserved on the wrapped error object.
Non-CRA JSON is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from preloop.cra.schemas import (
    CAPTURE_ERROR_CODES,
    INVALID_ERROR,
    MISSING_ERROR,
    UNSUPPORTED_ERROR,
    expected_cra_schema_from_prompt,
    is_cra_schema_id,
)
from preloop.cra.validate import (
    AUTHORITY_OFFLINE,
    AUTHORITY_REQUIRED,
    AuthorityMode,
    CraValidationResult,
    PlatformApproval,
    failure_strings,
    gate_policy_from_trigger,
    json_in,
    result_claims_authority,
    validate_cra_result,
    wrap_invalid_cra_result,
)

logger = logging.getLogger(__name__)

# The execution row says "private" where the CRA envelope says "self_hosted".
CRA_RUNNER_KIND_BY_EXECUTION_KIND: Mapping[str, str] = {
    "hosted": "hosted",
    "private": "self_hosted",
}
# Marks the runner block as a control-plane fact, not an agent claim.
PLATFORM_ATTESTATION = "control_plane"


class CraAuthorityUnavailableError(RuntimeError):
    """Platform approval lookup failed; claimed decisions must fail closed."""


@dataclass(frozen=True)
class CraPersistDecision:
    """What to persist and whether the execution may succeed."""

    artifact: Optional[dict[str, Any]]
    validation: CraValidationResult

    @property
    def invalid(self) -> bool:
        return self.validation.invalid

    @property
    def detail(self) -> str:
        safe = failure_strings(self.validation.failures)
        if safe:
            return "; ".join(safe)
        return ""

    @property
    def fail_closed_status(self) -> Optional[str]:
        """Return FAILED when this result must not be a successful release."""
        if self.invalid:
            return "FAILED"
        if self.validation.incomplete:
            return "FAILED"
        return None


def delivered_waivers_from_trigger(payload: Any) -> Optional[list[Mapping[str, Any]]]:
    """Return waiver entries the caller actually delivered, if the key is present.

    A missing key is ``None`` (nothing to authenticate against). An empty list
    is an explicit empty delivery: agent-asserted waivers cannot match it.
    """
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("payload")
    source = payload
    if isinstance(nested, Mapping) and "waivers" in nested and "waivers" not in payload:
        source = nested
    if "waivers" not in source:
        return None
    waivers = source.get("waivers")
    if waivers is None:
        return []
    if not isinstance(waivers, list):
        return []
    return [item for item in waivers if isinstance(item, Mapping)]


def load_platform_approvals(db: Any, execution_id: Any) -> list[PlatformApproval]:
    """Load approval rows for this execution via CRUD.

    Raises:
        CraAuthorityUnavailableError: lookup cannot run or the CRUD query fails.
            Callers that persist a result claiming approvals/waivers must
            fail closed rather than skip authenticity.
    """
    if db is None or execution_id is None:
        raise CraAuthorityUnavailableError("approval lookup is unavailable")
    try:
        from preloop.models.crud import crud_approval_request
    except Exception as exc:
        raise CraAuthorityUnavailableError("approval CRUD is unavailable") from exc
    exec_id = str(execution_id)
    try:
        rows = crud_approval_request.get_multi_by_execution(db, execution_id=exec_id)
    except Exception as exc:
        logger.warning("CRA persist: could not load platform approvals: %s", exc)
        raise CraAuthorityUnavailableError("approval lookup failed") from exc
    if not isinstance(rows, (list, tuple)):
        raise CraAuthorityUnavailableError("approval lookup returned an invalid result")
    loaded: list[PlatformApproval] = []
    for row in rows:
        tool_args = getattr(row, "tool_args", None) or {}
        operation = None
        if isinstance(tool_args, Mapping):
            value = tool_args.get("operation")
            if isinstance(value, str) and value.strip():
                operation = value.strip()
        raw_reason = getattr(row, "auto_approved_reason", None)
        loaded.append(
            PlatformApproval(
                id=str(getattr(row, "id", "")),
                status=str(getattr(row, "status", "") or ""),
                tool_name=str(getattr(row, "tool_name", "") or ""),
                operation=operation,
                tool_args=dict(tool_args) if isinstance(tool_args, Mapping) else None,
                tool_result=getattr(row, "tool_result", None),
                responses=getattr(row, "responses", None),
                approver_comment=getattr(row, "approver_comment", None),
                resolved_at=_resolved_at_text(getattr(row, "resolved_at", None)),
                decided_by_ai=getattr(row, "decided_by_ai", False) is True,
                auto_approved_reason=(
                    raw_reason if isinstance(raw_reason, str) else None
                ),
            )
        )
    return loaded


def _resolved_at_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def resolve_persist_authority(
    artifact: Any,
    db: Any,
    execution_id: Any,
    *,
    prompt: Optional[str] = None,
    expected_schema: Optional[str] = None,
) -> tuple[Optional[list[PlatformApproval]], AuthorityMode]:
    """Load approvals only when a CRA result claims a human decision.

    Non-CRA JSON, including objects that happen to contain a ``decision``
    field, does not query the approval table.
    """
    if not result_claims_authority(
        artifact, prompt=prompt, expected_schema=expected_schema
    ):
        return None, AUTHORITY_OFFLINE
    try:
        return load_platform_approvals(db, execution_id), AUTHORITY_REQUIRED
    except CraAuthorityUnavailableError:
        return None, AUTHORITY_REQUIRED


def cra_runner_identity(execution_runner: Any) -> Optional[dict[str, Any]]:
    """Translate the control plane's runner record into CRA envelope terms.

    The console calls a self-hosted CLI runner "private"; the CRA envelope
    says "self_hosted" (:data:`preloop.cra.schemas.RUNNER_KINDS`). Only the
    facts the platform can prove travel: kind, runner id and pool. The
    display name is left out because it falls back to a placeholder when no
    runner row is loaded, and a placeholder is not evidence. Anything the
    platform cannot classify returns ``None``, which leaves the agent's own
    field untouched.
    """
    if not isinstance(execution_runner, Mapping):
        return None
    kind = CRA_RUNNER_KIND_BY_EXECUTION_KIND.get(
        str(execution_runner.get("kind") or "").strip()
    )
    if kind is None:
        return None
    identity: dict[str, Any] = {
        "kind": kind,
        "id": None,
        "attested_by": PLATFORM_ATTESTATION,
    }
    runner_id = execution_runner.get("id")
    if runner_id is not None:
        identity["id"] = str(runner_id)
    pool = execution_runner.get("pool")
    if isinstance(pool, str) and pool.strip():
        identity["pool"] = pool.strip()
    return identity


def stamp_runner_identity(payload: Any, execution_runner: Any) -> Any:
    """Overwrite ``result.runner`` with what the control plane knows.

    The agent cannot see which runner leased its job, so every preset tells
    it to write ``{"kind": null, "id": null}`` and every stored result used
    to say exactly that. Where the run executed is a compliance-relevant
    fact the platform owns, so it is stamped here, before validation, on any
    document that carries a CRA schema id. Other agent-written keys under
    ``runner`` are preserved; the platform-owned ones win.
    """
    identity = cra_runner_identity(execution_runner)
    if identity is None or not isinstance(payload, Mapping):
        return payload
    if not is_cra_schema_id(payload.get("schema")):
        return payload
    existing = payload.get("runner")
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    merged.update(identity)
    stamped = dict(payload)
    stamped["runner"] = merged
    return stamped


def apply_cra_persist_boundary(
    artifact: Optional[Mapping[str, Any]],
    *,
    prompt: Optional[str] = None,
    expected_schema: Optional[str] = None,
    trigger_payload: Any = None,
    platform_approvals: Optional[Sequence[PlatformApproval]] = None,
    previous_gap_register: Optional[Mapping[str, Any]] = None,
    require_coverage: bool = False,
    authority: AuthorityMode = AUTHORITY_OFFLINE,
    execution_runner: Any = None,
) -> CraPersistDecision:
    """Validate at the persist boundary and wrap failures without dropping raw JSON.

    Expected CRA flows with a missing schema cannot evade validation. Capture
    error objects produced by the runner stay visible. Unknown non-CRA JSON is
    returned unchanged (``validation.skipped``). ``execution_runner`` is the
    control plane's runner record for this execution; when it is given, it
    replaces the agent's ``runner`` block.
    """
    expected = expected_schema or expected_cra_schema_from_prompt(prompt)
    payload: Any = dict(artifact) if isinstance(artifact, Mapping) else artifact
    payload = stamp_runner_identity(payload, execution_runner)

    if isinstance(payload, Mapping) and json_in(
        payload.get("error"), CAPTURE_ERROR_CODES
    ):
        raw = payload.get("raw")
        if isinstance(raw, Mapping):
            raw_schema = raw.get("schema")
        else:
            raw_schema = payload.get("schema")
        if expected or is_cra_schema_id(raw_schema):
            failures = failure_strings(payload.get("failures"))
            if not failures:
                failures = [str(payload.get("detail") or payload["error"])]
            bound = dict(payload)
            bound["failures"] = failures
            validation = CraValidationResult(
                ok=False,
                failures=failures,
                expected_schema=expected,
                schema_id=expected,
            )
            return CraPersistDecision(artifact=bound, validation=validation)
        validation = CraValidationResult(ok=True, skipped=True)
        return CraPersistDecision(
            artifact=dict(payload),
            validation=validation,
        )

    waivers = delivered_waivers_from_trigger(trigger_payload)
    policy = gate_policy_from_trigger(trigger_payload)
    validation = validate_cra_result(
        payload,
        expected_schema=expected,
        prompt=prompt,
        delivered_waivers=waivers,
        platform_approvals=platform_approvals,
        previous_gap_register=previous_gap_register,
        require_coverage=require_coverage,
        authority=authority,
        gate_policy=policy,
    )
    if validation.skipped:
        persisted: Optional[dict[str, Any]]
        if payload is None:
            persisted = None
        elif isinstance(payload, dict):
            persisted = payload
        else:
            persisted = dict(payload)
        return CraPersistDecision(artifact=persisted, validation=validation)

    if validation.ok:
        persisted = dict(payload) if isinstance(payload, Mapping) else payload
        if validation.advisories:
            logger.info(
                "CRA result %s coverage advisories: %s",
                validation.schema_id,
                "; ".join(validation.advisories),
            )
        return CraPersistDecision(artifact=persisted, validation=validation)

    error = UNSUPPORTED_ERROR
    safe_failures = failure_strings(validation.failures)
    joined = " ".join(safe_failures).lower()
    if any("no result.json" in item or "no schema" in item for item in safe_failures):
        error = MISSING_ERROR
    elif "unsupported cra" in joined:
        error = UNSUPPORTED_ERROR
    else:
        error = INVALID_ERROR
    wrapped = wrap_invalid_cra_result(payload, validation.failures, error=error)
    logger.warning("CRA persist failed closed: %s", wrapped.get("detail"))
    return CraPersistDecision(artifact=wrapped, validation=validation)


def cra_fail_closed_error_message(decision: CraPersistDecision) -> str:
    """Operator-facing reason when persist-time CRA validation fails closed."""
    schema = decision.validation.expected_schema or decision.validation.schema_id
    prefix = "CRA result.json failed contract validation"
    if schema:
        prefix = f"CRA result.json ({schema}) failed contract validation"
    detail = decision.detail or "malformed or unsupported CRA result"
    return f"{prefix}: {detail}"


def cra_fail_closed_completion_error(
    decision: CraPersistDecision, original: Optional[str] = None
) -> str:
    """Keep a prior runner failure next to CRA contract diagnostics.

    Original text is scrubbed the same way execution logs are, so preserving
    it cannot store a credential the invalid-CRA path previously discarded.

    One exception: when the prior failure is the runner refusing to start the
    container, "no result.json was persisted" is a consequence of that, not an
    independent finding. Concatenating the two made operators debug the preset
    or the model when the cause was in the first clause (preloop/preloop#505,
    dogfood report 4.2), so a runner failure stands alone.
    """
    from preloop.services.flow_failure_category import (
        FAILURE_CATEGORY_RUNNER_CONFLICT,
        FAILURE_CATEGORY_RUNNER_ERROR,
        derive_failure_category,
    )
    from preloop.utils.secret_scrubbing import scrub_secrets

    contract = cra_fail_closed_error_message(decision)
    prior = (scrub_secrets(original) or "").strip() if original else ""
    if not prior or prior == contract:
        return contract
    category = derive_failure_category(status="FAILED", error_message=prior)
    if category in (FAILURE_CATEGORY_RUNNER_CONFLICT, FAILURE_CATEGORY_RUNNER_ERROR):
        return prior
    return f"{prior}; {contract}"


def apply_cra_fail_closed_completion(
    status: str,
    error: Optional[str],
    decision: CraPersistDecision,
) -> tuple[str, Optional[str]]:
    """Fail the execution when persist validation must deny a release."""
    if decision.invalid:
        return "FAILED", cra_fail_closed_completion_error(decision, error)
    if decision.fail_closed_status == "FAILED" and status == "SUCCEEDED":
        return "FAILED", error or cra_fail_closed_error_message(decision)
    return status, error


def normalize_execution_id(value: Any) -> Optional[str]:
    """Return a string execution id when ``value`` is a UUID or str."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None
