"""Persisted-execution boundary for CRA result.json validation.

Hosted capture (``FlowExecutionOrchestrator._capture_result_artifact``) and
private-runner completion both call :func:`apply_cra_persist_boundary` so a
malformed known schema cannot be stored as a successful release. Raw evidence
is preserved on the wrapped error object. Non-CRA JSON is unchanged.
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
    result_claims_authority,
    validate_cra_result,
    wrap_invalid_cra_result,
)

logger = logging.getLogger(__name__)


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
        if self.validation.failures:
            return "; ".join(self.validation.failures)
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
        loaded.append(
            PlatformApproval(
                id=str(getattr(row, "id", "")),
                status=str(getattr(row, "status", "") or ""),
                tool_name=str(getattr(row, "tool_name", "") or ""),
                operation=operation,
            )
        )
    return loaded


def resolve_persist_authority(
    artifact: Any,
    db: Any,
    execution_id: Any,
) -> tuple[Optional[list[PlatformApproval]], AuthorityMode]:
    """Load approvals only when the result claims a human decision.

    Non-CRA JSON and CRA results with no claimed waiver/decision do not
    query the approval table (avoids a persist-path performance regression).
    """
    if not result_claims_authority(artifact):
        return None, AUTHORITY_OFFLINE
    try:
        return load_platform_approvals(db, execution_id), AUTHORITY_REQUIRED
    except CraAuthorityUnavailableError:
        return None, AUTHORITY_REQUIRED


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
) -> CraPersistDecision:
    """Validate at the persist boundary and wrap failures without dropping raw JSON.

    Expected CRA flows with a missing schema cannot evade validation. Capture
    error objects produced by the runner stay visible. Unknown non-CRA JSON is
    returned unchanged (``validation.skipped``).
    """
    expected = expected_schema or expected_cra_schema_from_prompt(prompt)
    payload: Any = dict(artifact) if isinstance(artifact, Mapping) else artifact

    if isinstance(payload, Mapping) and payload.get("error") in CAPTURE_ERROR_CODES:
        if expected or is_cra_schema_id(
            (payload.get("raw") or {}).get("schema")
            if isinstance(payload.get("raw"), Mapping)
            else payload.get("schema")
        ):
            failures = list(payload.get("failures") or [])
            if not failures:
                failures = [str(payload.get("detail") or payload["error"])]
            validation = CraValidationResult(
                ok=False,
                failures=failures,
                expected_schema=expected,
                schema_id=expected,
            )
            return CraPersistDecision(artifact=dict(payload), validation=validation)
        validation = CraValidationResult(ok=True, skipped=True)
        return CraPersistDecision(
            artifact=dict(payload),
            validation=validation,
        )

    waivers = delivered_waivers_from_trigger(trigger_payload)
    validation = validate_cra_result(
        payload,
        expected_schema=expected,
        prompt=prompt,
        delivered_waivers=waivers,
        platform_approvals=platform_approvals,
        previous_gap_register=previous_gap_register,
        require_coverage=require_coverage,
        authority=authority,
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
    joined = " ".join(validation.failures).lower()
    if any(
        "no result.json" in item or "no schema" in item for item in validation.failures
    ):
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


def normalize_execution_id(value: Any) -> Optional[str]:
    """Return a string execution id when ``value`` is a UUID or str."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None
