"""Audit logging service for tracking security-sensitive operations."""

import hashlib
import logging
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from preloop.utils import get_client_ip_optional
from preloop.models.crud import crud_audit_log
from preloop.models import models

from .async_writer import (
    FLAG_ENV,
    outbox_enabled,
    log_best_effort_failure,
    origin_loop,
    schedule_audit_write,
    shutdown_audit_writer,
    start_audit_writer,
)

User = models.User

#: Mirrors ``outbox.OUTBOX_OPERATIONS`` without importing the outbox module
#: on the flag-off path; a test keeps the two in step.
_OUTBOX_OPERATION_NAMES = frozenset(
    {
        "log_tool_call",
        "log_policy_decision",
        "log_approval_lifecycle",
        "log_approval_notification",
        "log_approval_tool_executed",
        "log_runtime_session_event",
        "log_model_gateway_request",
        "log_session_search",
        "log_ai_approval",
    }
)
_warned_outbox_fallback = False

logger = logging.getLogger(__name__)


def _safe_error_detail(value: Optional[str]) -> Optional[str]:
    """Bound failure metadata and remove labelled credentials before storage."""
    if value is None:
        return None
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s;,]+", REDACTED, value)
    text = re.sub(
        r"(?i)\b(?:password|passwd|token|access_token|refresh_token|"
        r"(?:x[-_]?)?api[-_]?key|"
        r"client_secret|secret|authorization)\b[\"']?\s*[:=]\s*[\"']?"
        r"[^\s\"'&,;}]+",
        REDACTED,
        text,
    )
    return " ".join(text.split())[:200] or None


def _safe_grant_metadata(grant: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep only bounded grant identity, scopes and cache flags, never token fields."""
    if not isinstance(grant, dict):
        return None
    safe: Dict[str, Any] = {}
    for name in ("available", "active", "cached"):
        if type(grant.get(name)) is bool:
            safe[name] = grant[name]
    if type(grant.get("cached")) is bool:
        safe["cache_hit"] = grant["cached"]
    for name in ("sub", "client_id", "consent_ref"):
        value = grant.get(name)
        if value is None or isinstance(value, str):
            safe[name] = value if value is None or len(value) <= 512 else None
    if type(grant.get("exp")) is int:
        safe["exp"] = grant["exp"]
    scope = grant.get("scope")
    if isinstance(scope, list):
        safe["scope"] = [
            value
            for value in scope[:128]
            if isinstance(value, str) and len(value) <= 512
        ]
    return safe


def safe_gateway_subject(subject: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Keep only bounded display identity from a trusted gateway subject."""
    if not isinstance(subject, dict):
        return None
    return {
        key: value
        for key in ("id", "email", "external_subject", "api_key_id")
        if isinstance(value := subject.get(key), str) and len(value) <= 512
    } or None


# Action type constants for audit logging

#: Names whose values never reach an audit row. Matched against the whole
#: key and against its words (split on ``_``, ``-``, ``.``, spaces, digits
#: and camelCase). Short or common words (``key``, ``auth``, ``token``) are
#: never matched as a bare substring, so ``monkey``, ``author`` and
#: ``arg_keys`` stay readable.
SENSITIVE_ARG_NAMES = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "apikey",
        "api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "private_key",
        "client_secret",
        "credentials",
        "credential",
        "authorization",
        "auth",
        "bearer",
        "cookie",
        "key",
    }
)
_SENSITIVE_WORDS = SENSITIVE_ARG_NAMES - {"key"}
#: Long, unambiguous fragments that are secret wherever they appear in a
#: name, including glued forms (``userpassword``, ``clientsecret``,
#: ``secretkey``, ``authtoken`` via the suffix rule below).
_SENSITIVE_SUBSTRINGS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "credential",
    "apikey",
    "privatekey",
    "accesskey",
    "signingkey",
    "authorization",
    "bearer",
)
#: Digits are their own token: ``password1`` is ``password`` + ``1``.
_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")
#: Reference-only record marker and its operator-chosen name fields.
_REFERENCE_MARKER = "_preloop_reference"
_REFERENCE_NAME_LISTS = ("arg_keys",)
_REFERENCE_KEPT_MAPS = ("kept", "kept_result")
REDACTED = "[REDACTED]"


def is_sensitive_name(name: str) -> bool:
    """True when ``name`` is, or has a word that is, a sensitive name."""
    lowered = name.lower()
    if lowered in SENSITIVE_ARG_NAMES:
        return True
    compact = re.sub(r"[^a-z]", "", lowered)
    if any(fragment in compact for fragment in _SENSITIVE_SUBSTRINGS):
        return True
    # authtoken, csrftoken, token2: a name that ends in "token".
    if compact.endswith("token"):
        return True
    words = [word.lower() for word in _WORD_RE.findall(name)]
    words = [word for word in words if not word.isdigit()]
    if not words:
        return False
    # "key" names a secret as the last word (api_key, signing_key, key2),
    # not as a qualifier (key_id, key_name).
    if words[-1] == "key":
        return True
    if any(word in _SENSITIVE_WORDS for word in words):
        return True
    joined = ["_".join(pair) for pair in zip(words, words[1:], strict=False)]
    return any(pair in SENSITIVE_ARG_NAMES for pair in joined)


def is_reference_record(value: Any) -> bool:
    """True for a core reference-only record (``_preloop_reference``)."""
    return isinstance(value, dict) and value.get(_REFERENCE_MARKER) is True


def _sanitize_value(key: str, value: Any) -> Any:
    if is_sensitive_name(key):
        return REDACTED
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    return value


def _sanitize_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _sanitize_value(str(k), v) for k, v in mapping.items()}


def _sanitize_kept(kept: Any) -> Any:
    """Keep the paths; blank a value whose path names a sensitive field."""
    if not isinstance(kept, dict):
        return kept
    out: Dict[str, Any] = {}
    for path, value in kept.items():
        segments = _PATH_SEGMENT_RE.findall(str(path).lstrip("$"))
        if segments and segments[0] == "result" and str(path).startswith("$result"):
            segments = segments[1:]
        if any(is_sensitive_name(segment) for segment in segments):
            out[path] = REDACTED
        elif isinstance(value, dict):
            out[path] = _sanitize_mapping(value)
        elif isinstance(value, list):
            out[path] = [_sanitize_value("", item) for item in value]
        else:
            out[path] = value
    return out


def _sanitize_reference_record(record: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in record.items():
        if key in _REFERENCE_NAME_LISTS or key == _REFERENCE_MARKER:
            out[key] = value
        elif key in _REFERENCE_KEPT_MAPS:
            out[key] = _sanitize_kept(value)
        else:
            out[key] = _sanitize_value(str(key), value)
    return out


class AuditActionType:
    """Constants for audit action types."""

    # Authentication and enterprise identity lifecycle events
    AUTH_LOGIN_SUCCEEDED = "auth.login.succeeded"
    AUTH_LOGIN_FAILED = "auth.login.failed"
    SSO_LOGIN_SUCCEEDED = "sso.login.succeeded"
    SSO_LOGIN_FAILED = "sso.login.failed"
    SSO_JIT_PROVISIONED = "sso.jit.provisioned"
    SSO_ROLE_MAPPED = "sso.role_mapped"
    SSO_BREAK_GLASS_USED = "sso.break_glass.used"
    SSO_ENFORCEMENT_BLOCKED = "sso.enforcement.blocked"
    ACCESS_REVOKED = "access.revoked"
    SSO_CONNECTION_CREATED = "sso.connection.created"
    SSO_CONNECTION_UPDATED = "sso.connection.updated"
    SSO_CONNECTION_DELETED = "sso.connection.deleted"
    SSO_DOMAIN_VERIFIED = "sso.domain.verified"
    SSO_DOMAIN_REMOVED = "sso.domain.removed"
    SSO_POLICY_CHANGED = "sso.policy.changed"
    SCIM_TOKEN_CREATED = "scim.token.created"
    SCIM_TOKEN_REVOKED = "scim.token.revoked"
    SCIM_USER_CREATED = "scim.user.created"
    SCIM_USER_UPDATED = "scim.user.updated"
    SCIM_USER_DEACTIVATED = "scim.user.deactivated"
    SCIM_USER_DELETED = "scim.user.deleted"
    SCIM_GROUP_UPDATED = "scim.group.updated"

    # Tool call actions
    TOOL_CALL = "tool_call"

    # Policy decision actions
    POLICY_ALLOW = "policy_allow"
    POLICY_DENY = "policy_deny"
    POLICY_REQUIRE_APPROVAL = "policy_require_approval"

    # Approval lifecycle actions
    APPROVAL_CREATED = "approval_created"
    APPROVAL_APPROVED = "approval_approved"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_ESCALATED = "approval_escalated"

    # AI approval actions
    AI_APPROVAL = "ai_approval"
    AI_APPROVAL_EVALUATED = "ai_approval_evaluated"

    # Approval notification fan-out (per channel)
    APPROVAL_NOTIFICATION_SENT = "approval_notification_sent"

    # Post-approval tool execution outcome (sub-event of approval flow)
    APPROVAL_TOOL_EXECUTED = "approval_tool_executed"

    # Runtime session lifecycle actions
    RUNTIME_SESSION_CREATED = "runtime_session_created"
    RUNTIME_SESSION_UPDATED = "runtime_session_updated"
    RUNTIME_SESSION_ENDED = "runtime_session_ended"

    # Model gateway request actions
    MODEL_GATEWAY_REQUEST = "model_gateway_request"

    # Ranked search over captured session content (prompts, responses, tool
    # calls, operator notes). A read, not a write, and the one read that lets
    # a caller find content they were never sent by guessing at words.
    SESSION_CONTENT_SEARCH = "session_content_search"


#: Match the core search schema's bound without retaining raw text by default.
MAX_QUERY_TEXT_CHARS = 512


def _record_query_text(db: Session, account_id: Union[UUID, str]) -> bool:
    """Use the canonical explicit opt-in; unreadable metadata never opts in."""
    from preloop.services.session_search_audit import (
        account_meta_data,
        query_text_audit_enabled,
    )

    return query_text_audit_enabled(account_meta_data(db, account_id=account_id))


def _safe_approval_execution_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Keep result evidence without copying tool output into the audit trail."""
    clean = dict(fields)
    preview = clean.pop("result_preview", None)
    if isinstance(preview, str):
        clean["result_preview_sha256"] = hashlib.sha256(preview.encode()).hexdigest()
        clean["result_preview_length"] = len(preview)
    if isinstance(clean.get("error"), str):
        clean["error"] = _safe_error_detail(clean["error"])
    return clean


def _broadcast_audit_event(
    *,
    account_id: Union[UUID, str],
    action: str,
    status: str,
    details: Optional[Dict[str, Any]] = None,
    audit_log_id: Optional[Union[UUID, str]] = None,
) -> None:
    """Best-effort fire-and-forget broadcast of a new audit log entry.

    Pushes a small ``audit_event`` envelope onto the account-scoped realtime
    bus so subscribers (e.g. the Audit page) can refresh without polling.

    The helper imports lazily and swallows all errors — the realtime fan-out
    must never break the audit write itself.
    """
    try:
        from preloop.services.account_realtime import (
            ACCOUNT_TOPIC_AUDIT,
            build_account_event,
            emit_account_event,
        )

        payload: Dict[str, Any] = {
            "action": action,
            "status": status,
        }
        if audit_log_id is not None:
            payload["audit_log_id"] = str(audit_log_id)

        if details:
            for key in (
                "correlation_id",
                "approval_id",
                "tool_name",
                "channel",
                "execution_id",
                "rule_description",
            ):
                value = details.get(key)
                if value is not None:
                    payload[key] = value

        event = build_account_event(
            account_id=str(account_id),
            topic=ACCOUNT_TOPIC_AUDIT,
            event_type="audit_event",
            payload=payload,
        )
        loop = origin_loop.get()
        if loop is None:
            emit_account_event(event)
        elif not loop.is_closed():
            loop.call_soon_threadsafe(emit_account_event, event)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"Failed to broadcast audit event: {exc}")


class AuditService:
    """Service for logging security-sensitive operations.

    This service provides a high-level interface for audit logging,
    automatically extracting request context and formatting log entries.
    """

    def __init__(self) -> None:
        """Initialize the audit service."""
        self.enabled = True

    def log_permission_check(
        self,
        db: Session,
        account_id: Union[UUID, str],
        user: User,
        permission: str,
        granted: bool,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log a permission check event.

        Args:
            db: Database session
            account_id: Account ID
            user: User performing the action
            permission: Permission being checked
            granted: Whether permission was granted
            resource_type: Type of resource being accessed
            resource_id: ID of resource being accessed
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            details = {
                "permission": permission,
                "granted": granted,
            }
            if resource_type:
                details["resource_type"] = resource_type
            if resource_id:
                details["resource_id"] = resource_id

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user.id if user else None,
                action="permission_check",
                resource_type=resource_type,
                resource_id=resource_id,
                status="success" if granted else "denied",
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log permission check: {e}", exc_info=True)

    def log_authentication(
        self,
        db: Session,
        account_id: Union[UUID, str],
        user_id: Optional[UUID],
        username: str,
        success: bool,
        method: str = "jwt",
        request: Optional[Request] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Log an authentication attempt.

        Args:
            db: Database session
            account_id: Account ID
            user_id: User ID (if successful)
            username: Username attempting to authenticate
            success: Whether authentication succeeded
            method: Authentication method used
            request: Optional FastAPI request for context
            failure_reason: Reason for failure (if applicable)
        """
        if not self.enabled:
            return

        try:
            details = {
                "username": username,
                "method": method,
            }
            if failure_reason:
                details["failure_reason"] = failure_reason

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action="authentication",
                resource_type="user",
                resource_id=str(user_id) if user_id else None,
                status="success" if success else "failure",
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log authentication: {e}", exc_info=True)

    def log_role_assignment(
        self,
        db: Session,
        account_id: Union[UUID, str],
        actor: User,
        target_user_id: UUID,
        role_name: str,
        action: str = "assigned",
        request: Optional[Request] = None,
    ) -> None:
        """Log a role assignment or removal.

        Args:
            db: Database session
            account_id: Account ID
            actor: User performing the action
            target_user_id: User whose role is being changed
            role_name: Name of the role
            action: 'assigned' or 'removed'
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            details = {
                "role": role_name,
                "action": action,
                "target_user": str(target_user_id),
            }

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=actor.id,
                action=f"role_{action}",
                resource_type="user",
                resource_id=str(target_user_id),
                status="success",
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log role assignment: {e}", exc_info=True)

    def log_resource_access(
        self,
        db: Session,
        account_id: Union[UUID, str],
        user: User,
        resource_type: str,
        resource_id: str,
        action: str,
        status: str = "success",
        request: Optional[Request] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log access to a resource.

        Args:
            db: Database session
            account_id: Account ID
            user: User accessing the resource
            resource_type: Type of resource (e.g., 'issue', 'flow', 'project')
            resource_id: ID of the resource
            action: Action performed (e.g., 'view', 'create', 'update', 'delete')
            status: Status of the action
            request: Optional FastAPI request for context
            details: Optional additional details
        """
        if not self.enabled:
            return

        try:
            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user.id,
                action=f"{resource_type}_{action}",
                resource_type=resource_type,
                resource_id=resource_id,
                status=status,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details or {},
            )
        except Exception as e:
            logger.error(f"Failed to log resource access: {e}", exc_info=True)

    def log_configuration_change(
        self,
        db: Session,
        account_id: Union[UUID, str],
        user: User,
        config_type: str,
        action: str,
        old_value: Optional[Any] = None,
        new_value: Optional[Any] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log a configuration change.

        Args:
            db: Database session
            account_id: Account ID
            user: User making the change
            config_type: Type of configuration
            action: Action performed
            old_value: Previous value
            new_value: New value
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            details = {
                "config_type": config_type,
                "action": action,
            }
            if old_value is not None:
                details["old_value"] = (
                    old_value if isinstance(old_value, (dict, list)) else str(old_value)
                )
            if new_value is not None:
                details["new_value"] = (
                    new_value if isinstance(new_value, (dict, list)) else str(new_value)
                )

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user.id,
                action="configuration_change",
                resource_type="configuration",
                resource_id=config_type,
                status="success",
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log configuration change: {e}", exc_info=True)

    def log_tool_call(
        self,
        db: Session,
        account_id: Union[UUID, str],
        tool_name: str,
        tool_args: Dict[str, Any],
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        policy_decision: Optional[str] = None,
        rule_matched: Optional[str] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        runtime_session_id: Optional[Union[UUID, str]] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        api_key_name: Optional[str] = None,
        request: Optional[Request] = None,
        error_code: Optional[str] = None,
        error_reason: Optional[str] = None,
        grant: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a tool call event.

        This logs every tool call (whether protected or not) for audit purposes.

        Args:
            db: Database session
            account_id: Account ID
            tool_name: Name of the tool being called
            tool_args: Arguments passed to the tool
            result: Result status ('success', 'failure', 'blocked', etc.)
            duration_ms: Execution duration in milliseconds
            policy_decision: Policy decision that was made ('allow', 'deny', 'require_approval')
            rule_matched: Description of the rule that matched (if any)
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
            request: Optional FastAPI request for context
            error_code: Upstream error classification, bounded and redacted
            error_reason: Upstream failure reason, bounded and redacted
            grant: Token-free delegated grant binding for audit attribution
        """
        if not self.enabled:
            return

        try:
            # Sanitize tool args to remove sensitive data
            sanitized_args = self._sanitize_tool_args(tool_args)

            details = {
                "tool_name": tool_name,
                "tool_args": sanitized_args,
                "duration_ms": duration_ms,
            }
            safe_grant = _safe_grant_metadata(grant)
            if safe_grant is not None:
                details["grant"] = safe_grant
            if policy_decision:
                details["policy_decision"] = policy_decision
            if rule_matched:
                details["rule_matched"] = rule_matched
            if execution_id:
                details["execution_id"] = str(execution_id)
            if correlation_id:
                details["correlation_id"] = correlation_id
            if runtime_session_id:
                details["runtime_session_id"] = str(runtime_session_id)
            if runtime_principal_type:
                details["runtime_principal_type"] = runtime_principal_type
            if runtime_principal_id:
                details["runtime_principal_id"] = runtime_principal_id
            if runtime_principal_name:
                details["runtime_principal_name"] = runtime_principal_name
            if api_key_id:
                details["api_key_id"] = str(api_key_id)
            if api_key_name:
                details["api_key_name"] = api_key_name

            for key, value in (
                ("error_code", error_code),
                ("error_reason", error_reason),
            ):
                safe_value = _safe_error_detail(value)
                if safe_value is not None:
                    details[key] = safe_value

            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action=AuditActionType.TOOL_CALL,
                resource_type="tool",
                resource_id=tool_name,
                status=result or "executed",
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=AuditActionType.TOOL_CALL,
                status=result or "executed",
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as e:
            logger.error(f"Failed to log tool call: {e}", exc_info=True)

    def log_policy_decision(
        self,
        db: Session,
        account_id: Union[UUID, str],
        tool_name: str,
        action: str,
        rule_description: Optional[str] = None,
        condition_matched: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log a policy decision event.

        This logs every policy evaluation result (allow, deny, require_approval).

        Args:
            db: Database session
            account_id: Account ID
            tool_name: Name of the tool being evaluated
            action: Policy decision ('allow', 'deny', 'require_approval')
            rule_description: Description of the rule that matched
            condition_matched: The condition expression that matched
            tool_args: Tool arguments that were evaluated
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            # Map action to audit action type
            action_type_map = {
                "allow": AuditActionType.POLICY_ALLOW,
                "deny": AuditActionType.POLICY_DENY,
                "require_approval": AuditActionType.POLICY_REQUIRE_APPROVAL,
            }
            audit_action = action_type_map.get(action, f"policy_{action}")

            details = {
                "tool_name": tool_name,
                "decision": action,
            }
            if rule_description:
                details["rule_description"] = rule_description
            if condition_matched:
                details["condition_matched"] = condition_matched
            if tool_args:
                details["tool_args"] = self._sanitize_tool_args(tool_args)
            if execution_id:
                details["execution_id"] = str(execution_id)
            if correlation_id:
                details["correlation_id"] = correlation_id
            if extra_details:
                details.update(extra_details)
                if "grant" in details:
                    details["grant"] = _safe_grant_metadata(details["grant"])

            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action=audit_action,
                resource_type="policy",
                resource_id=tool_name,
                status=action,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=audit_action,
                status=action,
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as e:
            logger.error(f"Failed to log policy decision: {e}", exc_info=True)

    def log_approval_lifecycle(
        self,
        db: Session,
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        event: str,
        tool_name: Optional[str] = None,
        approver_id: Optional[UUID] = None,
        reason: Optional[str] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log an approval lifecycle event.

        This logs approval request lifecycle events (created, approved, denied, expired, escalated).

        Args:
            db: Database session
            account_id: Account ID
            approval_id: Approval request ID
            event: Lifecycle event ('created', 'approved', 'denied', 'expired', 'escalated')
            tool_name: Name of the tool (if available)
            approver_id: ID of the user who approved/denied (if applicable)
            reason: Reason or comment for the decision
            execution_id: Flow execution ID (if applicable)
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            # Map event to audit action type
            event_type_map = {
                "created": AuditActionType.APPROVAL_CREATED,
                "approved": AuditActionType.APPROVAL_APPROVED,
                "denied": AuditActionType.APPROVAL_DENIED,
                "expired": AuditActionType.APPROVAL_EXPIRED,
                "escalated": AuditActionType.APPROVAL_ESCALATED,
            }
            audit_action = event_type_map.get(event, f"approval_{event}")

            details = {
                "approval_id": str(approval_id),
                "event": event,
            }
            if tool_name:
                details["tool_name"] = tool_name
            if approver_id:
                details["approver_id"] = str(approver_id)
            if reason:
                details["reason"] = reason
            if execution_id:
                details["execution_id"] = str(execution_id)
            if correlation_id:
                details["correlation_id"] = correlation_id
            if extra_details:
                details.update(extra_details)

            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=approver_id,
                action=audit_action,
                resource_type="approval",
                resource_id=str(approval_id),
                status=event,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=audit_action,
                status=event,
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as exc:
            log_best_effort_failure(
                logger,
                exc,
                summary="Failed to log approval lifecycle",
                diagnostic=f"Failed to log approval lifecycle: {exc}",
            )

    def log_approval_notification(
        self,
        db: Session,
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        channel: str,
        status: str,
        tool_name: Optional[str] = None,
        recipient_user_ids: Optional[List[Union[UUID, str]]] = None,
        sent_count: Optional[int] = None,
        failed_count: Optional[int] = None,
        skipped_count: Optional[int] = None,
        error: Optional[str] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a per-channel approval notification fan-out result.

        One row is written per notification channel (email, mobile_push,
        slack, mattermost, webhook, etc.) so the audit timeline can show
        *who* was notified through *which* channels and whether each fan-out
        actually succeeded.

        Args:
            db: Database session
            account_id: Account ID
            approval_id: Approval request ID this notification belongs to
            channel: Channel name ("email", "mobile_push", "slack", ...)
            status: "sent" | "partial" | "failed" | "skipped" | "no_devices"
            tool_name: Tool that triggered the approval (for grouping)
            recipient_user_ids: Users targeted by this fan-out
            sent_count: Number of successful deliveries
            failed_count: Number of failed deliveries
            skipped_count: Recipients skipped (e.g. preferences disabled)
            error: Optional error message when the channel itself failed
            correlation_id: Correlation ID linking the notification to the
                originating tool call
            extra_details: Per-channel metadata (e.g. {"ios": 1, "android": 0})
        """
        if not self.enabled:
            return

        try:
            details: Dict[str, Any] = {
                "approval_id": str(approval_id),
                "channel": channel,
            }
            if tool_name:
                details["tool_name"] = tool_name
            if recipient_user_ids is not None:
                details["recipient_user_ids"] = [str(uid) for uid in recipient_user_ids]
                details["recipient_count"] = len(recipient_user_ids)
            if sent_count is not None:
                details["sent_count"] = sent_count
            if failed_count is not None:
                details["failed_count"] = failed_count
            if skipped_count is not None:
                details["skipped_count"] = skipped_count
            if error:
                details["error"] = error
            if correlation_id:
                details["correlation_id"] = correlation_id
            if extra_details:
                details.update(extra_details)

            if isinstance(details.get("error"), str):
                details["error"] = _safe_error_detail(details["error"])
            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=None,
                action=AuditActionType.APPROVAL_NOTIFICATION_SENT,
                resource_type="approval",
                resource_id=str(approval_id),
                status=status,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=AuditActionType.APPROVAL_NOTIFICATION_SENT,
                status=status,
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as e:
            logger.error(f"Failed to log approval notification: {e}", exc_info=True)

    def log_approval_notification_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        channel: str,
        status: str,
        tool_name: Optional[str] = None,
        recipient_user_ids: Optional[List[Union[UUID, str]]] = None,
        sent_count: Optional[int] = None,
        failed_count: Optional[int] = None,
        skipped_count: Optional[int] = None,
        error: Optional[str] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Async fire-and-forget wrapper around :meth:`log_approval_notification`."""
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_approval_notification,
            account_id=account_id,
            approval_id=approval_id,
            channel=channel,
            status=status,
            tool_name=tool_name,
            recipient_user_ids=recipient_user_ids,
            sent_count=sent_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            error=error,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )

    def log_approval_tool_executed(
        self,
        db: Session,
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        tool_name: str,
        status: str,
        duration_ms: Optional[int] = None,
        result_preview: Optional[str] = None,
        error: Optional[str] = None,
        correlation_id: Optional[str] = None,
        execution_id: Optional[Union[UUID, str]] = None,
        extra_details: Optional[Dict[str, Any]] = None,
        result_preview_sha256: Optional[str] = None,
        result_preview_length: Optional[int] = None,
    ) -> None:
        """Log the post-approval tool execution outcome.

        Used by the async polling path (where the tool runs *after* the
        approval is granted, outside of the normal ``DynamicFastMCP`` audit
        path) so that the unified timeline can show what happened after the
        approver clicked "Approve".

        Args:
            db: Database session
            account_id: Account ID
            approval_id: Approval request ID the execution belongs to
            tool_name: Tool that was executed
            status: "executed" (success) or "failed"
            duration_ms: Execution time in milliseconds
            result_preview: Result evidence; only its hash and length are retained
            error: Error message if execution failed
            correlation_id: Correlation ID for grouping
            execution_id: Flow execution ID, if applicable
            extra_details: Additional metadata
        """
        if not self.enabled:
            return

        try:
            details: Dict[str, Any] = {
                "approval_id": str(approval_id),
                "tool_name": tool_name,
            }
            if duration_ms is not None:
                details["duration_ms"] = duration_ms
            if result_preview_sha256 is not None:
                details["result_preview_sha256"] = result_preview_sha256
            if result_preview_length is not None:
                details["result_preview_length"] = result_preview_length
            if result_preview is not None:
                details["result_preview"] = result_preview
            if error:
                details["error"] = error[:2000]
            if correlation_id:
                details["correlation_id"] = correlation_id
            if execution_id:
                details["execution_id"] = str(execution_id)
            if extra_details:
                details.update(extra_details)
                if "grant" in details:
                    details["grant"] = _safe_grant_metadata(details["grant"])

            details = _safe_approval_execution_fields(details)
            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=None,
                action=AuditActionType.APPROVAL_TOOL_EXECUTED,
                resource_type="approval",
                resource_id=str(approval_id),
                status=status,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=AuditActionType.APPROVAL_TOOL_EXECUTED,
                status=status,
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as e:
            logger.error(f"Failed to log approval tool execution: {e}", exc_info=True)

    def log_approval_tool_executed_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        tool_name: str,
        status: str,
        duration_ms: Optional[int] = None,
        result_preview: Optional[str] = None,
        error: Optional[str] = None,
        correlation_id: Optional[str] = None,
        execution_id: Optional[Union[UUID, str]] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Async fire-and-forget wrapper around :meth:`log_approval_tool_executed`."""
        if not self.enabled:
            return

        if extra_details and "grant" in extra_details:
            extra_details = {
                **extra_details,
                "grant": _safe_grant_metadata(extra_details["grant"]),
            }

        schedule_audit_write(
            db_factory,
            self.log_approval_tool_executed,
            account_id=account_id,
            approval_id=approval_id,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            result_preview=result_preview,
            error=error,
            correlation_id=correlation_id,
            execution_id=execution_id,
            extra_details=extra_details,
        )

    def log_runtime_session_event(
        self,
        db: Session,
        account_id: Union[UUID, str],
        runtime_session_id: Union[UUID, str],
        event: str,
        session_source_type: str,
        session_source_id: str,
        session_reference: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        flow_execution_id: Optional[Union[UUID, str]] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        api_key_name: Optional[str] = None,
        user_id: Optional[UUID] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log a runtime session lifecycle event."""
        if not self.enabled:
            return

        try:
            event_type_map = {
                "created": AuditActionType.RUNTIME_SESSION_CREATED,
                "updated": AuditActionType.RUNTIME_SESSION_UPDATED,
                "ended": AuditActionType.RUNTIME_SESSION_ENDED,
            }
            audit_action = event_type_map.get(event, f"runtime_session_{event}")
            details = {
                "runtime_session_id": str(runtime_session_id),
                "event": event,
                "session_source_type": session_source_type,
                "session_source_id": session_source_id,
            }
            if session_reference:
                details["session_reference"] = session_reference
            if runtime_principal_type:
                details["runtime_principal_type"] = runtime_principal_type
            if runtime_principal_id:
                details["runtime_principal_id"] = runtime_principal_id
            if runtime_principal_name:
                details["runtime_principal_name"] = runtime_principal_name
            if flow_execution_id:
                details["flow_execution_id"] = str(flow_execution_id)
            if api_key_id:
                details["api_key_id"] = str(api_key_id)
            if api_key_name:
                details["api_key_name"] = api_key_name

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action=audit_action,
                resource_type="runtime_session",
                resource_id=str(runtime_session_id),
                status=event,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log runtime session event: {e}", exc_info=True)

    def log_model_gateway_request(
        self,
        db: Session,
        account_id: Union[UUID, str],
        user_id: Optional[Union[UUID, str]],
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
        budget: Optional[Dict[str, Any]] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        estimated_cost: Optional[float] = None,
        gateway_subject: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a model gateway request event with optional token/cost attribution."""
        if not self.enabled:
            return

        try:
            details = {
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
                "upstream_request_id": upstream_request_id,
                "request_fingerprint": request_fingerprint,
                "gateway_attempt": gateway_attempt,
                "is_retry": is_retry,
                "retry_of_api_usage_id": retry_of_api_usage_id,
                "error_detail": error_detail,
                "error_type": error_type,
                "budget": budget,
                "gateway_subject": safe_gateway_subject(gateway_subject),
            }
            if prompt_tokens is not None:
                details["prompt_tokens"] = prompt_tokens
            if completion_tokens is not None:
                details["completion_tokens"] = completion_tokens
            if total_tokens is not None:
                details["total_tokens"] = total_tokens
            if estimated_cost is not None:
                details["estimated_cost"] = estimated_cost
            if runtime_session_id:
                details["runtime_session_id"] = runtime_session_id
            if runtime_principal_type:
                details["runtime_principal_type"] = runtime_principal_type
            if runtime_principal_id:
                details["runtime_principal_id"] = runtime_principal_id
            if runtime_principal_name:
                details["runtime_principal_name"] = runtime_principal_name
            if api_key_id:
                details["api_key_id"] = api_key_id
            if api_key_name:
                details["api_key_name"] = api_key_name
            if flow_id:
                details["flow_id"] = flow_id
            if flow_execution_id:
                details["flow_execution_id"] = flow_execution_id

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action=AuditActionType.MODEL_GATEWAY_REQUEST,
                resource_type="model_gateway",
                resource_id=requested_model or model_alias or endpoint_kind or endpoint,
                status=outcome,
                details={k: v for k, v in details.items() if v is not None},
            )
        except Exception as e:
            logger.error(f"Failed to log model gateway request: {e}", exc_info=True)

    def log_session_search(
        self,
        db: Session,
        account_id: Union[UUID, str],
        surface: str,
        status: str,
        query: Optional[str] = None,
        actor_type: str = "unknown",
        user_id: Optional[Union[UUID, str]] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        managed_agent_id: Optional[Union[UUID, str]] = None,
        mode: Optional[str] = None,
        effective_mode: Optional[str] = None,
        scope: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        result_count: Optional[int] = None,
        total: Optional[int] = None,
        snippets_returned: Optional[bool] = None,
        elapsed_ms: Optional[float] = None,
        refusal_reason: Optional[str] = None,
        permission: Optional[str] = None,
        error: Optional[str] = None,
        request: Optional[Request] = None,
        query_length: Optional[int] = None,
        query_hash: Optional[str] = None,
    ) -> None:
        """Log one search over captured session content.

        The row answers the question a compliance owner asks first: who
        searched the transcripts, for what, and when. It is written for every
        outcome, including a refused or failed search, because an attempted
        read of a corpus a caller may not read is the interesting event.

        Args:
            db: Database session, owned by the audit writer thread.
            account_id: Account whose corpus was searched.
            surface: Which way in the caller used ("search_endpoint",
                "agent_tool", ...). See ``session_search_audit``.
            status: "success", "denied" or "failure".
            query: Search text; retained only with the canonical account opt-in.
            query_hash: Fingerprint already computed before outbox serialization.
            actor_type: "user", "agent" or "unknown".
            user_id: Acting user, when the caller is a user.
            runtime_principal_id: Agent principal, when the caller is an agent.
            runtime_principal_name: Human readable name of that principal.
            api_key_id: API key the call authenticated with, when known.
            managed_agent_id: Managed agent row behind the call, when known.
            mode: Ranking mode the caller asked for.
            effective_mode: Ranking mode that actually ran.
            scope: Search scope, for surfaces that have one ("own", "account").
            filters: Filters the search was narrowed by, already flattened to
                strings by the caller.
            result_count: Sessions returned on this page.
            total: Sessions matching before paging.
            snippets_returned: Whether snippet text left the database.
            elapsed_ms: Server side time the search took.
            refusal_reason: Machine readable reason a refused search was
                refused.
            permission: Permission that gated a denied search.
            error: Error text for a failed search.
            request: Optional FastAPI request for IP and user agent.
            query_length: Original query length when ``query`` was already
                truncated, or emptied for an opted-out account, upstream (the
                durable outbox intent).
        """
        if not self.enabled:
            return

        try:
            details: Dict[str, Any] = {
                "surface": surface,
                "actor_type": actor_type,
            }
            if query is not None or query_hash is not None:
                from preloop.services.session_search_audit import (
                    query_hash as hash_query,
                )

                details["query_hash"] = query_hash or hash_query(query or "")
                if query is not None and _record_query_text(db, account_id):
                    details["query"] = query[:MAX_QUERY_TEXT_CHARS]
                    details["query_recorded"] = True
                else:
                    details["query_recorded"] = False
                details["query_length"] = (
                    query_length if query_length is not None else len(query or "")
                )
            if user_id:
                details["user_id"] = str(user_id)
            if runtime_principal_id:
                details["runtime_principal_id"] = runtime_principal_id
            if runtime_principal_name:
                details["runtime_principal_name"] = runtime_principal_name
            if api_key_id:
                details["api_key_id"] = str(api_key_id)
            if managed_agent_id:
                details["managed_agent_id"] = str(managed_agent_id)
            if mode:
                details["mode"] = mode
            if effective_mode:
                details["effective_mode"] = effective_mode
            if scope:
                details["scope"] = scope
            if filters is not None:
                details["filters"] = filters
            if result_count is not None:
                details["result_count"] = result_count
            if total is not None:
                details["total"] = total
            if snippets_returned is not None:
                details["snippets_returned"] = snippets_returned
            if elapsed_ms is not None:
                details["elapsed_ms"] = round(float(elapsed_ms), 3)
            if refusal_reason:
                details["refusal_reason"] = refusal_reason
            if permission:
                details["permission"] = permission
            if error:
                details["error"] = _safe_error_detail(error)

            user_uuid: Optional[UUID] = None
            if user_id is not None:
                try:
                    user_uuid = user_id if isinstance(user_id, UUID) else UUID(user_id)
                except (TypeError, ValueError):
                    # A principal id that is not a user id belongs in details,
                    # not in the foreign key column.
                    user_uuid = None

            audit_log = crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_uuid,
                action=AuditActionType.SESSION_CONTENT_SEARCH,
                resource_type="session_search",
                resource_id=surface,
                status=status,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
            _broadcast_audit_event(
                account_id=account_id,
                action=AuditActionType.SESSION_CONTENT_SEARCH,
                status=status,
                details=details,
                audit_log_id=audit_log.id if audit_log else None,
            )
        except Exception as e:
            logger.error(f"Failed to log session content search: {e}", exc_info=True)

    def log_ai_approval(
        self,
        db: Session,
        account_id: Union[UUID, str],
        approval_request_id: Union[UUID, str],
        tool_name: str,
        decision: str,  # "approve", "deny", "uncertain"
        confidence: float,
        reasoning: str,
        model_used: str,
        fallback_triggered: bool = False,
        fallback_behavior: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
        request: Optional[Request] = None,
    ) -> None:
        """Log an AI approval decision for audit purposes.

        This logs when an AI model evaluates a tool call for approval,
        capturing the model's decision, confidence, and reasoning.

        Args:
            db: Database session
            account_id: Account ID
            approval_request_id: ID of the approval request being evaluated
            tool_name: Name of the tool being evaluated
            decision: AI's decision ("approve", "deny", "uncertain")
            confidence: Confidence score (0.0 to 1.0)
            reasoning: AI's reasoning for the decision
            model_used: Name/identifier of the AI model used for evaluation
            fallback_triggered: Whether fallback behavior was triggered
            fallback_behavior: Fallback behavior used if AI was uncertain
            tool_args: Arguments passed to the tool (will be sanitized)
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
            request: Optional FastAPI request for context
        """
        if not self.enabled:
            return

        try:
            # Sanitize tool args to remove sensitive data
            sanitized_args = self._sanitize_tool_args(tool_args) if tool_args else {}

            details = {
                "decision": decision,
                "confidence": confidence,
                "reasoning": reasoning,
                "model_used": model_used,
                "fallback_triggered": fallback_triggered,
                "tool_name": tool_name,
                "tool_args": sanitized_args,
            }
            if fallback_behavior:
                details["fallback_behavior"] = fallback_behavior
            if execution_id:
                details["execution_id"] = str(execution_id)

            crud_audit_log.log_action(
                db=db,
                account_id=account_id,
                user_id=user_id,
                action=AuditActionType.AI_APPROVAL,
                resource_type="approval",
                resource_id=str(approval_request_id),
                status=decision,
                ip_address=self._get_client_ip(request) if request else None,
                user_agent=self._get_user_agent(request) if request else None,
                details=details,
            )
        except Exception as e:
            logger.error(f"Failed to log AI approval: {e}", exc_info=True)

    def _sanitize_tool_args(self, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize tool arguments to remove sensitive data.

        A value is blanked when its key is a sensitive name or contains one
        as a whole word (``api_key``, ``x-auth-token``, ``clientSecret``),
        not as a substring (``monkey``, ``author``, ``arg_keys``). A
        reference-only record (core #1124) keeps its operator-chosen names:
        ``arg_keys`` (argument names, not values) is left as is, and the
        paths under ``kept`` / ``kept_result`` stay readable while the value
        of a path naming a sensitive field is still blanked.

        Args:
            tool_args: Raw tool arguments

        Returns:
            Sanitized tool arguments with sensitive values redacted
        """
        if not tool_args:
            return {}
        if is_reference_record(tool_args):
            return _sanitize_reference_record(tool_args)
        return _sanitize_mapping(tool_args)

    def _get_client_ip(self, request: Request) -> Optional[str]:
        """Extract client IP address from request.

        Args:
            request: FastAPI request

        Returns:
            Client IP address or None
        """
        return get_client_ip_optional(request)

    def _get_user_agent(self, request: Request) -> Optional[str]:
        """Extract user agent from request.

        Args:
            request: FastAPI request

        Returns:
            User agent string or None
        """
        if not request:
            return None

        return request.headers.get("User-Agent")

    @staticmethod
    def durable_outbox_enabled() -> bool:
        """Whether ``enqueue_in_transaction`` is atomic with the caller.

        Callers migrating to the outbox can assert on this; with the flag off
        the method falls back to the best-effort writer.
        """
        return outbox_enabled()

    def enqueue_in_transaction(
        self,
        db: Session,
        operation: str,
        *,
        priority: bool = False,
        **fields: Any,
    ) -> None:
        """Record an audit event atomically with the caller's business write.

        With ``PRELOOP_AUDIT_OUTBOX_ENABLED`` on, the intent is added to
        ``db`` and flushed, never committed: it commits or rolls back with
        the caller's transaction, and the outbox relay writes the audit row.
        With the flag off this falls back to the bounded best-effort writer
        on a session of its own, as the ``*_async`` methods do today.

        Args:
            db: The caller's session holding the business write.
            operation: Name of an ``AuditService.log_*`` method, for example
                ``"log_approval_lifecycle"``.
            priority: Use reserved capacity on the fallback path and drain
                ahead of ordinary intents on the outbox path.
            **fields: Keyword arguments for that method, without ``db``.

        Raises:
            ValueError: ``operation`` is not an outbox audit method.
        """
        if not self.enabled:
            return
        if operation not in _OUTBOX_OPERATION_NAMES:
            raise ValueError(f"Unsupported audit outbox operation: {operation}")
        if self.durable_outbox_enabled():
            from .outbox import enqueue_intent

            enqueue_intent(db, operation, fields, priority=priority)
            return
        global _warned_outbox_fallback
        if not _warned_outbox_fallback:
            _warned_outbox_fallback = True
            logger.warning(
                "enqueue_in_transaction called with %s off: audit records use "
                "the best-effort writer and are not atomic with the caller",
                FLAG_ENV,
            )
        from preloop.models.db.session import get_session_factory

        schedule_audit_write(
            get_session_factory(),
            getattr(self, operation),
            priority=priority,
            allow_without_loop=True,
            **fields,
        )

    def enable(self) -> None:
        """Enable audit logging."""
        start_audit_writer()
        self.enabled = True
        logger.info("Audit logging enabled")

    def shutdown(self) -> None:
        """Stop audit submissions without blocking collector shutdown on the DB."""
        outbox_module = sys.modules.get(f"{__package__}.outbox")
        if outbox_module is not None:
            outbox_module.stop_relay()
        self.enabled = False
        shutdown_audit_writer()

    def disable(self) -> None:
        """Disable audit logging."""
        self.enabled = False
        logger.warning("Audit logging disabled")

    # =========================================================================
    # Async non-blocking methods for audit logging
    # These methods use fire-and-forget pattern to avoid slowing down operations
    # =========================================================================

    def log_tool_call_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        tool_name: str,
        tool_args: Dict[str, Any],
        result: Optional[str] = None,
        duration_ms: Optional[int] = None,
        policy_decision: Optional[str] = None,
        rule_matched: Optional[str] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        runtime_session_id: Optional[Union[UUID, str]] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        api_key_name: Optional[str] = None,
        error_code: Optional[str] = None,
        error_reason: Optional[str] = None,
        grant: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a tool call event asynchronously (fire-and-forget).

        This method admits a bounded background worker write with its own
        database session. Overload is counted and logged; delivery is best effort.

        Args:
            db_factory: Callable that returns a new database session
            account_id: Account ID
            tool_name: Name of the tool being called
            tool_args: Arguments passed to the tool
            result: Result status ('success', 'failure', 'blocked', etc.)
            duration_ms: Execution duration in milliseconds
            policy_decision: Policy decision that was made
            rule_matched: Description of the rule that matched
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
            error_code: Upstream error classification, bounded and redacted
            error_reason: Upstream failure reason, bounded and redacted
            grant: Token-free delegated grant binding for audit attribution
        """
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_tool_call,
            account_id=account_id,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
            duration_ms=duration_ms,
            policy_decision=policy_decision,
            rule_matched=rule_matched,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            error_code=_safe_error_detail(error_code),
            error_reason=_safe_error_detail(error_reason),
            grant=_safe_grant_metadata(grant),
        )

    def log_policy_decision_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        tool_name: str,
        action: str,
        rule_description: Optional[str] = None,
        condition_matched: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a policy decision event asynchronously (fire-and-forget).

        This method admits a bounded background worker write with its own
        database session. Overload is counted and logged; delivery is best effort.

        Args:
            db_factory: Callable that returns a new database session
            account_id: Account ID
            tool_name: Name of the tool being evaluated
            action: Policy decision ('allow', 'deny', 'require_approval')
            rule_description: Description of the rule that matched
            condition_matched: The condition expression that matched
            tool_args: Tool arguments that were evaluated
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
        """
        if not self.enabled:
            return

        if extra_details and "grant" in extra_details:
            extra_details = {
                **extra_details,
                "grant": _safe_grant_metadata(extra_details["grant"]),
            }
        schedule_audit_write(
            db_factory,
            self.log_policy_decision,
            allow_without_loop=True,
            priority=True,
            account_id=account_id,
            tool_name=tool_name,
            action=action,
            rule_description=rule_description,
            condition_matched=condition_matched,
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )

    def log_approval_lifecycle_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        approval_id: Union[UUID, str],
        event: str,
        tool_name: Optional[str] = None,
        approver_id: Optional[UUID] = None,
        reason: Optional[str] = None,
        execution_id: Optional[UUID] = None,
        correlation_id: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an approval lifecycle event asynchronously (fire-and-forget).

        This method admits a bounded background worker write with its own
        database session. Overload is counted and logged; delivery is best effort.

        Args:
            db_factory: Callable that returns a new database session
            account_id: Account ID
            approval_id: Approval request ID
            event: Lifecycle event ('created', 'approved', 'denied', 'expired', 'escalated')
            tool_name: Name of the tool (if available)
            approver_id: ID of the user who approved/denied
            reason: Reason or comment for the decision
            execution_id: Flow execution ID (if applicable)
        """
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_approval_lifecycle,
            priority=True,
            account_id=account_id,
            approval_id=approval_id,
            event=event,
            tool_name=tool_name,
            approver_id=approver_id,
            reason=reason,
            execution_id=execution_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )

    def log_runtime_session_event_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        runtime_session_id: Union[UUID, str],
        event: str,
        session_source_type: str,
        session_source_id: str,
        session_reference: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        flow_execution_id: Optional[Union[UUID, str]] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        api_key_name: Optional[str] = None,
        user_id: Optional[UUID] = None,
    ) -> None:
        """Log a runtime session lifecycle event asynchronously."""
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_runtime_session_event,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            event=event,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
            session_reference=session_reference,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            flow_execution_id=flow_execution_id,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            user_id=user_id,
        )

    def log_session_search_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        surface: str,
        status: str,
        query: Optional[str] = None,
        actor_type: str = "unknown",
        user_id: Optional[Union[UUID, str]] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        api_key_id: Optional[Union[UUID, str]] = None,
        managed_agent_id: Optional[Union[UUID, str]] = None,
        mode: Optional[str] = None,
        effective_mode: Optional[str] = None,
        scope: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        result_count: Optional[int] = None,
        total: Optional[int] = None,
        snippets_returned: Optional[bool] = None,
        elapsed_ms: Optional[float] = None,
        refusal_reason: Optional[str] = None,
        permission: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log a session content search asynchronously (fire-and-forget).

        A search is a read on a console path, so the audit write happens in
        the bounded background writer with its own database session: auditing
        the search never adds latency to it, and a writer that is down or full
        never fails it.

        The write is admitted even when the caller has no running event loop.
        The search endpoint runs its query on the FastAPI threadpool (see
        ``preloop.api.loop_safety.run_db_off_loop``), so the audit call is
        made from a worker thread, and the ordinary "no loop, skip it" rule
        would drop exactly the surface this exists to record.
        """
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_session_search,
            priority=True,
            allow_without_loop=True,
            account_id=account_id,
            surface=surface,
            status=status,
            query=query,
            actor_type=actor_type,
            user_id=user_id,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            api_key_id=api_key_id,
            managed_agent_id=managed_agent_id,
            mode=mode,
            effective_mode=effective_mode,
            scope=scope,
            filters=filters,
            result_count=result_count,
            total=total,
            snippets_returned=snippets_returned,
            elapsed_ms=elapsed_ms,
            refusal_reason=refusal_reason,
            permission=permission,
            error=error,
        )

    def log_ai_approval_async(
        self,
        db_factory: Callable[[], Session],
        account_id: Union[UUID, str],
        approval_request_id: Union[UUID, str],
        tool_name: str,
        decision: str,  # "approve", "deny", "uncertain"
        confidence: float,
        reasoning: str,
        model_used: str,
        fallback_triggered: bool = False,
        fallback_behavior: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        user_id: Optional[UUID] = None,
        execution_id: Optional[UUID] = None,
    ) -> None:
        """Log an AI approval decision asynchronously (fire-and-forget).

        This method admits a bounded background worker write with its own
        database session. Overload is counted and logged; delivery is best effort.

        Args:
            db_factory: Callable that returns a new database session
            account_id: Account ID
            approval_request_id: ID of the approval request being evaluated
            tool_name: Name of the tool being evaluated
            decision: AI's decision ("approve", "deny", "uncertain")
            confidence: Confidence score (0.0 to 1.0)
            reasoning: AI's reasoning for the decision
            model_used: Name/identifier of the AI model used for evaluation
            fallback_triggered: Whether fallback behavior was triggered
            fallback_behavior: Fallback behavior used if AI was uncertain
            tool_args: Arguments passed to the tool (will be sanitized)
            user_id: User ID (if available)
            execution_id: Flow execution ID (if applicable)
        """
        if not self.enabled:
            return

        schedule_audit_write(
            db_factory,
            self.log_ai_approval,
            priority=True,
            account_id=account_id,
            approval_request_id=approval_request_id,
            tool_name=tool_name,
            decision=decision,
            confidence=confidence,
            reasoning=reasoning,
            model_used=model_used,
            fallback_triggered=fallback_triggered,
            fallback_behavior=fallback_behavior,
            tool_args=tool_args,
            user_id=user_id,
            execution_id=execution_id,
        )


# Global instance
_audit_service: Optional[AuditService] = None


def get_audit_service() -> AuditService:
    """Get the global audit service instance.

    Returns:
        Global audit service instance
    """
    global _audit_service
    if _audit_service is None:
        _audit_service = AuditService()
    return _audit_service
