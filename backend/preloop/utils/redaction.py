"""Centralized redaction of sensitive data for logs and audit surfaces.

This module provides helpers to prevent accidental leakage of secrets and
sensitive user data through application logs, error traces, approval logs,
and tool execution records.

Redaction policy:
- Field names matching SENSITIVE_FIELD_NAMES (case-insensitive) are replaced
  with REDACTED_STRING in dicts and nested structures.
- Use redact_dict() before logging or persisting dict-like data.
- Use redact_for_log() for safe string representation in log messages.
"""

import json
import re
from typing import Any, Optional, Set

REDACTED_STRING = "***REDACTED***"

# Field names that should never appear in logs or audit payloads.
# Matches case-insensitively.
SENSITIVE_FIELD_NAMES: Set[str] = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "private_key",
    "privatekey",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "bearer",
    "client_secret",
    "clientsecret",
    "webhook_secret",
    "webhooksecret",
    "jira_webhook_secret",
    "device_token",
    "devicetoken",
    "progress_token",
    "progresstoken",
    "approval_token",
    "approvaltoken",
    "key",
    "keys",
}


def _is_sensitive_key(key: str) -> bool:
    """Check if a dict key matches a sensitive field name."""
    if not isinstance(key, str):
        return False
    key_lower = key.lower().strip()
    # Exact match
    if key_lower in SENSITIVE_FIELD_NAMES:
        return True
    # Suffix match for keys like "github_api_key", "oauth_client_secret"
    for sensitive in SENSITIVE_FIELD_NAMES:
        if key_lower.endswith(sensitive) or key_lower.startswith(sensitive):
            return True
    # Common patterns: *_token, *_secret, *_password, *_key
    if re.search(r"(token|secret|password|api_key|credential)s?$", key_lower):
        return True
    return False


def omit_preloop_markers(data: Any) -> Any:
    """Drop trusted ``_preloop_`` markers from a tool-argument display copy.

    The markers travel on the stored approval so surfaces can read them.
    They are not arguments the model chose, so a rendered argument list
    leaves them out. Nested values are unchanged.

    Args:
        data: Tool arguments, or any other value.

    Returns:
        A shallow copy of a dict without ``_preloop_`` keys. Non-dicts are
        returned unchanged.
    """
    if not isinstance(data, dict):
        return data
    return {
        key: value
        for key, value in data.items()
        if not (isinstance(key, str) and key.startswith("_preloop_"))
    }


def redact_dict(
    data: Any,
    field_names: Optional[Set[str]] = None,
) -> Any:
    """Recursively redact sensitive fields from a dict or list.

    Replaces values for keys matching SENSITIVE_FIELD_NAMES (or field_names
    if provided) with REDACTED_STRING. Nested dicts and lists are processed
    recursively.

    Args:
        data: Dict, list, or scalar value to process.
        field_names: Optional override for sensitive field names. If None,
            uses SENSITIVE_FIELD_NAMES.

    Returns:
        A copy of data with sensitive values redacted. Scalars are returned
        unchanged.
    """
    sensitive = field_names if field_names is not None else SENSITIVE_FIELD_NAMES

    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            key_sensitive = isinstance(k, str) and (
                k.lower() in sensitive or _is_sensitive_key(k)
            )
            if key_sensitive:
                result[k] = REDACTED_STRING
            else:
                result[k] = redact_dict(v, field_names=sensitive)
        return result
    elif isinstance(data, list):
        return [redact_dict(item, field_names=sensitive) for item in data]
    else:
        return data


def redact_for_log(data: Any, max_length: int = 500) -> str:
    """Produce a safe string representation for logging.

    Redacts sensitive fields and truncates long values to avoid leaking
    secrets or flooding logs.

    Args:
        data: Value to serialize (typically dict or list).
        max_length: Maximum length of the output string.

    Returns:
        JSON string with sensitive fields redacted, truncated if needed.
    """
    redacted = redact_dict(data)
    try:
        s = json.dumps(redacted, default=str)
    except (TypeError, ValueError):
        s = repr(redacted)
    if len(s) > max_length:
        s = s[: max_length - 20] + "...[truncated]"
    return s
