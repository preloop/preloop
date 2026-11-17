"""Content-free refusal reasons shared by admission and native permission hooks."""

import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud.denial_audit import record_denial
from preloop.services.sensitive_data.redact import redact_text
from preloop.utils.secret_scrubbing import scrub_secrets

# Only known server reasons become codes. Access-rule explanations are bounded
# and redacted separately; provider bodies, approval comments and tool inputs
# never enter the record.
REASONS = {
    "ci_key_inactive": "The CI key is inactive or has been revoked.",
    "ci_key_expired": "The CI key has expired.",
    "ci_principal_inactive": "The CI principal is disabled or retired.",
    "ci_action_denied": "The CI principal or key does not grant this operation.",
    "ci_credential_invalid": "The CI credential is inactive, expired or invalid.",
    "ci_transport_denied": "CI credentials require one Authorization bearer header.",
    "ci_route_denied": "This operation does not accept restricted CI credentials.",
    "ci_authorization_denied": "The CI principal no longer has the required grant or binding.",
    "ci_flow_denied": "The requested flow is outside the CI principal's binding.",
    "ci_input_invalid": "Restricted CI requires a PR number and exact head only.",
    "ci_review_denied": "The review did not pass restricted CI admission.",
    "ci_request_invalid": "The CI request does not match the endpoint schema.",
    "ci_model_unavailable": "The flow's server model binding is unavailable.",
    "ci_admission_conflict": "A flow admission policy prevents this execution.",
    "ci_verification_unavailable": "Trusted review verification or dispatch is unavailable.",
    "permission_denied": "The tool call was refused by the permission policy.",
    "permission_auth_denied": "The credential cannot authorize this permission check.",
    "api_key_scope_denied": "The credential's scope does not allow this permission check.",
    "ci_credential_denied": "Machine authorization is required for this credential.",
    "key_inactive": "The API key is inactive.",
    "key_expired": "The API key has expired.",
    "session_ended": "The runtime session has ended.",
    "execution_ended": "The flow execution has ended.",
    "runtime_binding_invalid": "The credential is not bound to an active agent or execution.",
    "agent_inactive": "The managed agent is not active.",
    "client_policy_denied": "The agent's native policy denied this tool call.",
    "tool_blocked": "The tool is blocked in Preloop.",
    "access_rule_invalid": "The native access rule has an unsupported action.",
    "approval_unavailable": "The required approval workflow is unavailable.",
    "approval_denied": "The approval was declined, cancelled or could not be granted.",
    "approval_expired": "The approval expired or the permission check timed out.",
    "approval_pending": "The execution is paused pending approval.",
}

_DETAIL_CODES = {
    "API key is inactive": "key_inactive",
    "API key has expired": "key_expired",
    "Runtime session has ended": "session_ended",
    "Flow execution has ended": "execution_ended",
    "Invalid runtime session token": "runtime_binding_invalid",
    "Token is not bound to a managed agent or active flow execution": "runtime_binding_invalid",
    "Read-only native permission credential binding is invalid": "runtime_binding_invalid",
    "Managed agent credential is invalid": "runtime_binding_invalid",
    "Managed agent is not active": "agent_inactive",
    "Inactive user": "permission_auth_denied",
    "Denied by client policy": "client_policy_denied",
    "Tool blocked in Preloop": "tool_blocked",
    "Unsupported native access rule action": "access_rule_invalid",
    "Required approval workflow is unavailable": "approval_unavailable",
    "Registry approval is unavailable": "approval_unavailable",
    "Restricted CI authorization denied": "ci_authorization_denied",
    "Restricted CI flow denied": "ci_flow_denied",
    "Restricted CI requires PR number and exact head only": "ci_input_invalid",
    "Restricted CI server model binding unavailable": "ci_model_unavailable",
}


_CI_REVIEW_MESSAGES = {
    "ci_pr_closed": "Restricted CI pull request is not open",
    "ci_repository_mismatch": "Restricted CI repository binding does not match",
    "ci_head_mismatch": "Restricted CI pull request head changed",
    "ci_single_execution_required": "Restricted CI requires a single review execution",
    "ci_host_unsupported": "Restricted CI GitHub host unsupported",
    "ci_host_binding_denied": "Restricted CI provider host binding denied",
    "ci_provider_unsupported": "Restricted CI provider is unsupported",
    "ci_integration_unavailable": "Restricted CI integration unavailable",
    "ci_pr_verification_denied": "Restricted CI pull request verification denied",
    "ci_execution_binding_denied": "Restricted CI execution binding denied",
    "ci_execution_provenance_denied": "Restricted CI execution provenance denied",
    "ci_flow_unavailable": "Restricted CI flow unavailable",
}
REASONS.update(_CI_REVIEW_MESSAGES)
_DETAIL_CODES.update({message: code for code, message in _CI_REVIEW_MESSAGES.items()})


def reason_for_detail(detail: Any, fallback: str) -> str:
    """Classify allowlisted server reasons without retaining arbitrary text."""
    if isinstance(detail, dict):
        code = detail.get("code")
        return code if isinstance(code, str) and code in REASONS else fallback
    return _DETAIL_CODES.get(detail, fallback) if isinstance(detail, str) else fallback


def write_denial(
    db: Session,
    *,
    account_id: UUID,
    principal_id: UUID,
    principal_type: str,
    action: str,
    target: str,
    reason_code: str,
    user_id: UUID | None = None,
    rule_message: str | None = None,
) -> None:
    """Persist a bounded, redacted counter using only trusted attribution."""
    message = REASONS[reason_code]
    if reason_code == "permission_denied" and rule_message:
        rule_message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", rule_message)
        safe_message, _ = redact_text(scrub_secrets(rule_message))
        message = "".join(
            char for char in (safe_message or message) if char.isprintable()
        )[:500]
    # Redact before truncating, so a long credential cannot be cut into a
    # fragment that no longer matches a shared sensitive-data detector.
    target = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", target)
    redacted, _ = redact_text(scrub_secrets(target))
    target = "".join(char for char in (redacted or "unknown") if char.isprintable())
    record_denial(
        db,
        account_id=account_id,
        principal_id=principal_id,
        principal_type=principal_type,
        action=action,
        target=(target or "unknown")[:200],
        reason_code=reason_code,
        message=message,
        user_id=user_id,
    )
    db.commit()
