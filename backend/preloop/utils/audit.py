"""Lightweight audit helper for configuration-change logging.

This module provides a single ``log_config_change`` helper that endpoints
can call after any configuration mutation (create, update, delete, toggle).

The helper gracefully degrades to a no-op when the EE audit plugin is not
installed, so OSS callers never need to guard the import.
"""

import logging
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.models.models.user import User
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AUDIT,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)


def _get_audit_service():
    """Lazy-import the EE audit service singleton (returns None in OSS)."""
    try:
        from plugins.audit.service import get_audit_service

        return get_audit_service()
    except ImportError:
        return None


def log_config_change(
    db: Session,
    *,
    user: User,
    config_type: str,
    action: str,
    old_value: Optional[Any] = None,
    new_value: Optional[Any] = None,
    request: Optional[Request] = None,
) -> None:
    """Log a configuration change to the audit trail.

    This is a thin wrapper around ``AuditService.log_configuration_change``
    that silently skips logging when the audit plugin is absent.

    Args:
        db: Active database session.
        user: The user performing the change.
        config_type: Entity type, e.g. ``"mcp_server"``, ``"tool_rule"``.
        action: Verb, e.g. ``"created"``, ``"updated"``, ``"deleted"``.
        old_value: Serialisable previous state (optional).
        new_value: Serialisable new state (optional).
        request: FastAPI request for IP / user-agent extraction (optional).
    """
    audit_service = _get_audit_service()
    if audit_service is None:
        return

    try:
        from preloop.utils.redaction import redact_dict

        audit_service.log_configuration_change(
            db,
            account_id=user.account_id,
            user=user,
            config_type=config_type,
            action=action,
            old_value=redact_dict(old_value) if old_value is not None else None,
            new_value=redact_dict(new_value) if new_value is not None else None,
            request=request,
        )
    except Exception:
        logger.debug("Audit log_configuration_change failed", exc_info=True)


def log_model_gateway_request(
    db: Session,
    *,
    account_id: Any,
    user_id: Optional[Any],
    api_usage_id: Optional[str],
    endpoint: str,
    endpoint_kind: Optional[str],
    status_code: int,
    outcome: str,
    requested_model: Optional[str],
    model_alias: Optional[str],
    provider_name: Optional[str],
    gateway_provider: Optional[str],
    auth_subject_type: Optional[str],
    runtime_session_id: Optional[str] = None,
    runtime_principal_type: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    runtime_principal_name: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    flow_execution_id: Optional[str] = None,
    upstream_request_id: Optional[str] = None,
    request_fingerprint: Optional[str] = None,
    gateway_attempt: Optional[int] = None,
    is_retry: bool = False,
    retry_of_api_usage_id: Optional[str] = None,
    error_detail: Optional[str] = None,
    error_type: Optional[str] = None,
    budget: Optional[dict[str, Any]] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    estimated_cost: Optional[float] = None,
) -> None:
    """Log a high-signal model gateway request event to the audit trail."""
    audit_service = _get_audit_service()
    if audit_service is None:
        _log_model_gateway_request_fallback(
            db=db,
            account_id=account_id,
            user_id=user_id,
            api_usage_id=api_usage_id,
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            status_code=status_code,
            outcome=outcome,
            requested_model=requested_model,
            model_alias=model_alias,
            provider_name=provider_name,
            gateway_provider=gateway_provider,
            auth_subject_type=auth_subject_type,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            flow_id=flow_id,
            flow_execution_id=flow_execution_id,
            upstream_request_id=upstream_request_id,
            request_fingerprint=request_fingerprint,
            gateway_attempt=gateway_attempt,
            is_retry=is_retry,
            retry_of_api_usage_id=retry_of_api_usage_id,
            error_detail=error_detail,
            error_type=error_type,
            budget=budget,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )
        _emit_model_gateway_audit_event(
            account_id=account_id,
            api_usage_id=api_usage_id,
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            status_code=status_code,
            outcome=outcome,
            requested_model=requested_model,
            model_alias=model_alias,
            provider_name=provider_name,
            gateway_provider=gateway_provider,
            auth_subject_type=auth_subject_type,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            flow_id=flow_id,
            flow_execution_id=flow_execution_id,
            upstream_request_id=upstream_request_id,
            request_fingerprint=request_fingerprint,
            gateway_attempt=gateway_attempt,
            is_retry=is_retry,
            retry_of_api_usage_id=retry_of_api_usage_id,
            error_detail=error_detail,
            error_type=error_type,
            budget=budget,
        )
        return

    try:
        audit_service.log_model_gateway_request(
            db=db,
            account_id=account_id,
            user_id=user_id,
            api_usage_id=api_usage_id,
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            status_code=status_code,
            outcome=outcome,
            requested_model=requested_model,
            model_alias=model_alias,
            provider_name=provider_name,
            gateway_provider=gateway_provider,
            auth_subject_type=auth_subject_type,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            flow_id=flow_id,
            flow_execution_id=flow_execution_id,
            upstream_request_id=upstream_request_id,
            request_fingerprint=request_fingerprint,
            gateway_attempt=gateway_attempt,
            is_retry=is_retry,
            retry_of_api_usage_id=retry_of_api_usage_id,
            error_detail=error_detail,
            error_type=error_type,
            budget=budget,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )
    except Exception:
        logger.debug("Audit log_model_gateway_request failed", exc_info=True)
        _log_model_gateway_request_fallback(
            db=db,
            account_id=account_id,
            user_id=user_id,
            api_usage_id=api_usage_id,
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            status_code=status_code,
            outcome=outcome,
            requested_model=requested_model,
            model_alias=model_alias,
            provider_name=provider_name,
            gateway_provider=gateway_provider,
            auth_subject_type=auth_subject_type,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            flow_id=flow_id,
            flow_execution_id=flow_execution_id,
            upstream_request_id=upstream_request_id,
            error_detail=error_detail,
            error_type=error_type,
            budget=budget,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )
    _emit_model_gateway_audit_event(
        account_id=account_id,
        api_usage_id=api_usage_id,
        endpoint=endpoint,
        endpoint_kind=endpoint_kind,
        status_code=status_code,
        outcome=outcome,
        requested_model=requested_model,
        model_alias=model_alias,
        provider_name=provider_name,
        gateway_provider=gateway_provider,
        auth_subject_type=auth_subject_type,
        runtime_session_id=runtime_session_id,
        runtime_principal_type=runtime_principal_type,
        runtime_principal_id=runtime_principal_id,
        runtime_principal_name=runtime_principal_name,
        api_key_id=api_key_id,
        api_key_name=api_key_name,
        flow_id=flow_id,
        flow_execution_id=flow_execution_id,
        upstream_request_id=upstream_request_id,
        error_detail=error_detail,
        error_type=error_type,
        budget=budget,
    )


def _log_model_gateway_request_fallback(
    db: Session,
    *,
    account_id: Any,
    user_id: Optional[Any],
    api_usage_id: Optional[str],
    endpoint: str,
    endpoint_kind: Optional[str],
    status_code: int,
    outcome: str,
    requested_model: Optional[str],
    model_alias: Optional[str],
    provider_name: Optional[str],
    gateway_provider: Optional[str],
    auth_subject_type: Optional[str],
    runtime_session_id: Optional[str] = None,
    runtime_principal_type: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    runtime_principal_name: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    flow_execution_id: Optional[str] = None,
    upstream_request_id: Optional[str] = None,
    request_fingerprint: Optional[str] = None,
    gateway_attempt: Optional[int] = None,
    is_retry: bool = False,
    retry_of_api_usage_id: Optional[str] = None,
    error_detail: Optional[str] = None,
    error_type: Optional[str] = None,
    budget: Optional[dict[str, Any]] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    estimated_cost: Optional[float] = None,
) -> None:
    """Persist a compatible audit log when the EE audit service is unavailable."""
    details = {
        "api_usage_id": api_usage_id,
        "endpoint": endpoint,
        "endpoint_kind": endpoint_kind,
        "status_code": status_code,
        "requested_model": requested_model,
        "model_alias": model_alias,
        "provider_name": provider_name,
        "gateway_provider": gateway_provider,
        "auth_subject_type": auth_subject_type,
        "runtime_session_id": runtime_session_id,
        "runtime_principal_type": runtime_principal_type,
        "runtime_principal_id": runtime_principal_id,
        "runtime_principal_name": runtime_principal_name,
        "api_key_id": api_key_id,
        "api_key_name": api_key_name,
        "flow_id": flow_id,
        "flow_execution_id": flow_execution_id,
        "upstream_request_id": upstream_request_id,
        "request_fingerprint": request_fingerprint,
        "gateway_attempt": gateway_attempt,
        "is_retry": is_retry,
        "retry_of_api_usage_id": retry_of_api_usage_id,
        "error_detail": error_detail,
        "error_type": error_type,
        "budget": budget,
    }
    if prompt_tokens is not None:
        details["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        details["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        details["total_tokens"] = total_tokens
    if estimated_cost is not None:
        details["estimated_cost"] = estimated_cost
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="model_gateway_request",
        resource_type="model_gateway",
        resource_id=api_usage_id,
        status=outcome,
        details=details,
    )


def _emit_model_gateway_audit_event(
    *,
    account_id: Any,
    api_usage_id: Optional[str],
    endpoint: str,
    endpoint_kind: Optional[str],
    status_code: int,
    outcome: str,
    requested_model: Optional[str],
    model_alias: Optional[str],
    provider_name: Optional[str],
    gateway_provider: Optional[str],
    auth_subject_type: Optional[str],
    runtime_session_id: Optional[str] = None,
    runtime_principal_type: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    runtime_principal_name: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    flow_execution_id: Optional[str] = None,
    upstream_request_id: Optional[str] = None,
    request_fingerprint: Optional[str] = None,
    gateway_attempt: Optional[int] = None,
    is_retry: bool = False,
    retry_of_api_usage_id: Optional[str] = None,
    error_detail: Optional[str] = None,
    error_type: Optional[str] = None,
    budget: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a realtime audit event for gateway activity."""
    emit_account_event(
        build_account_event(
            account_id=str(account_id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload={
                "action": "model_gateway_request",
                "api_usage_id": api_usage_id,
                "endpoint": endpoint,
                "endpoint_kind": endpoint_kind,
                "status_code": status_code,
                "outcome": outcome,
                "requested_model": requested_model,
                "model_alias": model_alias,
                "provider_name": provider_name,
                "gateway_provider": gateway_provider,
                "auth_subject_type": auth_subject_type,
                "runtime_session_id": runtime_session_id,
                "runtime_principal_type": runtime_principal_type,
                "runtime_principal_id": runtime_principal_id,
                "runtime_principal_name": runtime_principal_name,
                "api_key_id": api_key_id,
                "api_key_name": api_key_name,
                "flow_id": flow_id,
                "flow_execution_id": flow_execution_id,
                "upstream_request_id": upstream_request_id,
                "request_fingerprint": request_fingerprint,
                "gateway_attempt": gateway_attempt,
                "is_retry": is_retry,
                "retry_of_api_usage_id": retry_of_api_usage_id,
                "error_detail": error_detail,
                "error_type": error_type,
                "budget": budget,
            },
            runtime_session_id=runtime_session_id,
            execution_id=flow_execution_id,
            flow_id=flow_id,
        )
    )
