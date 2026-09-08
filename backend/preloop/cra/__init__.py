"""CRA result.json runtime contracts and fail-closed CI helper."""

from preloop.cra.ci import CraCIError, ReleasePolicy, evaluate_release, run_cra_ci
from preloop.cra.persist import (
    CraAuthorityUnavailableError,
    CraPersistDecision,
    apply_cra_fail_closed_completion,
    apply_cra_persist_boundary,
    cra_fail_closed_completion_error,
    cra_fail_closed_error_message,
    delivered_waivers_from_trigger,
    load_platform_approvals,
    resolve_persist_authority,
)
from preloop.cra.schemas import (
    CRA_RESULT_SCHEMAS,
    SCHEMA_DUEDILIGENCE_V1,
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_SBOMAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
    expected_cra_schema_from_prompt,
)
from preloop.cra.validate import (
    CraResultValidationError,
    CraValidationResult,
    PlatformApproval,
    assert_cra_result,
    is_incomplete_envelope,
    result_claims_authority,
    validate_cra_result,
    wrap_invalid_cra_result,
)

__all__ = [
    "CRA_RESULT_SCHEMAS",
    "CraAuthorityUnavailableError",
    "CraCIError",
    "CraPersistDecision",
    "CraResultValidationError",
    "CraValidationResult",
    "PlatformApproval",
    "ReleasePolicy",
    "SCHEMA_DUEDILIGENCE_V1",
    "SCHEMA_RELEASEAUDIT_V1",
    "SCHEMA_SBOMAUDIT_V1",
    "SCHEMA_VULNSCAN_V1",
    "apply_cra_fail_closed_completion",
    "apply_cra_persist_boundary",
    "assert_cra_result",
    "cra_fail_closed_completion_error",
    "cra_fail_closed_error_message",
    "delivered_waivers_from_trigger",
    "evaluate_release",
    "expected_cra_schema_from_prompt",
    "is_incomplete_envelope",
    "load_platform_approvals",
    "resolve_persist_authority",
    "result_claims_authority",
    "run_cra_ci",
    "validate_cra_result",
    "wrap_invalid_cra_result",
]
