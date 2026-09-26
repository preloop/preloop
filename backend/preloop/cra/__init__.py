"""CRA result.json runtime contracts and fail-closed CI helper.

Imports are lazy. ``python -m preloop.cra measure`` has to run in the
release SBOM job, which does not install backend dependencies. An eager
import here pulls pydantic through the CI helper before ``measure`` can run.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "CraCIError": "preloop.cra.ci",
    "ReleasePolicy": "preloop.cra.ci",
    "evaluate_release": "preloop.cra.ci",
    "run_cra_ci": "preloop.cra.ci",
    "CraAuthorityUnavailableError": "preloop.cra.persist",
    "CraPersistDecision": "preloop.cra.persist",
    "apply_cra_fail_closed_completion": "preloop.cra.persist",
    "apply_cra_persist_boundary": "preloop.cra.persist",
    "cra_fail_closed_completion_error": "preloop.cra.persist",
    "cra_fail_closed_error_message": "preloop.cra.persist",
    "delivered_waivers_from_trigger": "preloop.cra.persist",
    "load_platform_approvals": "preloop.cra.persist",
    "resolve_persist_authority": "preloop.cra.persist",
    "article14_deadlines": "preloop.cra.reporting",
    "earliest_discovery": "preloop.cra.reporting",
    "kev_finding_ids": "preloop.cra.reporting",
    "reportable_candidates": "preloop.cra.reporting",
    "CRA_RESULT_SCHEMAS": "preloop.cra.schemas",
    "SCHEMA_DUEDILIGENCE_V1": "preloop.cra.schemas",
    "SCHEMA_RELEASEAUDIT_V1": "preloop.cra.schemas",
    "SCHEMA_SBOMAUDIT_V1": "preloop.cra.schemas",
    "SCHEMA_VULNSCAN_V1": "preloop.cra.schemas",
    "expected_cra_schema_from_prompt": "preloop.cra.schemas",
    "CraResultValidationError": "preloop.cra.validate",
    "CraValidationResult": "preloop.cra.validate",
    "PlatformApproval": "preloop.cra.validate",
    "assert_cra_result": "preloop.cra.validate",
    "is_incomplete_envelope": "preloop.cra.validate",
    "result_claims_authority": "preloop.cra.validate",
    "validate_cra_result": "preloop.cra.validate",
    "wrap_invalid_cra_result": "preloop.cra.validate",
}


def __getattr__(name: str) -> Any:
    """Load one public name on first use. Missing names stay AttributeError."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


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
    "article14_deadlines",
    "apply_cra_persist_boundary",
    "assert_cra_result",
    "cra_fail_closed_completion_error",
    "cra_fail_closed_error_message",
    "delivered_waivers_from_trigger",
    "earliest_discovery",
    "evaluate_release",
    "expected_cra_schema_from_prompt",
    "is_incomplete_envelope",
    "kev_finding_ids",
    "load_platform_approvals",
    "resolve_persist_authority",
    "reportable_candidates",
    "result_claims_authority",
    "run_cra_ci",
    "validate_cra_result",
    "wrap_invalid_cra_result",
]
