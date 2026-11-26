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
    "encrypted_api_key",
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
    # Encrypted original arguments on a pending reference-only approval
    # (#1124): shown in the console only, never in emails or webhooks.
    "_preloop_sealed_args",
    "sealed_args",
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


# --- Agent config snapshots ---------------------------------------------------
#
# Managed-agent enrollments store JSON the CLI sends: the agent config it found,
# the one it wrote, backup notes and validation results. The CLI sanitises that
# before sending; ``redact_agent_config`` is the server-side second line, applied
# when such JSON is stored and again when it is served.
#
# ``redact_dict`` is not reused for it. Its key list is tuned for logs and audit
# payloads (``key``, ``auth*``, ``credential*``) and would blank config fields
# the console reads; it has no value rule, so a secret under a neutral key or in
# an argument list passes; and it would blank the ``<AGENT_KEY>`` placeholders
# that manual setup instructions must keep.

#: Marker written in place of a secret. The CLI writes the same one.
CONFIG_REDACTED = "<redacted>"

#: Placeholders that stand for a secret and are safe to store and show.
CONFIG_PLACEHOLDERS = ("<AGENT_KEY>", "<AGENT_KEY_LAST_20>", CONFIG_REDACTED)

_CONFIG_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth_token",
    "oauth_token",
    "access_token",
    "refresh_token",
    "bearer_token",
    "session_token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "x-api-key",
)
# Keys that name where a secret lives, or report on one, hold no secret:
# ``bearer_token_env_var``, ``credentials_secret_id``, ``api_key_helper``.
_CONFIG_REFERENCE_KEY = re.compile(
    r"(_env|_env_var|_ids?|_path|_file|helper|_ok|_configured|_status|_source"
    r"|_type|_kind)$"
)
_PLACEHOLDER_ALTERNATIVES = "|".join(re.escape(item) for item in CONFIG_PLACEHOLDERS)
_PLACEHOLDER_ONLY = re.compile(rf"\s*(?:Bearer\s+)?(?:{_PLACEHOLDER_ALTERNATIVES})\s*")
_KEY_CHARS = r"A-Za-z0-9_\-"
_CONFIG_SECRET_VALUE = re.compile(
    rf"\bagt_[{_KEY_CHARS}]{{8,}}"
    rf"|\bsk-ant-[{_KEY_CHARS}]+"
    rf"|\bsk-[{_KEY_CHARS}]{{20,}}"
    # Well-known provider and forge token prefixes, whatever key holds them.
    rf"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{{20,}}"
    rf"|\bgithub_pat_[{_KEY_CHARS}]{{20,}}"
    rf"|\bglpat-[{_KEY_CHARS}]{{16,}}"
    rf"|\bxox[abposr]-[{_KEY_CHARS}]{{10,}}"
    rf"|\bhf_[A-Za-z0-9]{{20,}}"
    rf"|\bnpm_[A-Za-z0-9]{{20,}}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_\-]{30,}"
    # The password of a URL with credentials (``postgres://user:pass@host``).
    # Only the password part is replaced; scheme, user and host stay.
    r"|(?<=://)(?P<url_user>[^/\s:@]*):[^/\s@]+(?=@)"
    # A bearer token: at least 8 token characters with a digit among them, or
    # 20 and more. Prose such as "Bearer authentication" is left alone.
    r"|\bBearer\s+(?=[A-Za-z0-9._~+/=\-]*[0-9]|[A-Za-z0-9._~+/=\-]{20})"
    r"[A-Za-z0-9._~+/=\-]{8,}"
)


def _is_config_secret_key(key: Any) -> bool:
    """Tell whether a config key names a secret value."""
    if not isinstance(key, str):
        return False
    lowered = key.lower().strip()
    if lowered == "token":
        return True
    normalised = lowered.replace("-", "_")
    # ``GITHUB_TOKEN``, ``NPM_TOKEN``, ``HF_TOKEN``: any ``*_token`` name,
    # and the ``*_key`` names of provider credentials (``OPENAI_KEY``,
    # ``private_key``). The CLI sanitiser treats ``_token`` the same way.
    named_secret = (
        normalised.endswith("_token")
        or normalised.endswith("_key")
        or any(part in lowered for part in _CONFIG_SENSITIVE_KEY_PARTS)
    )
    if not named_secret:
        return False
    return _CONFIG_REFERENCE_KEY.search(normalised) is None


def _replace_config_secret(match: re.Match[str]) -> str:
    """Replace one matched credential, keeping the user of a credential URL."""
    user = match.groupdict().get("url_user")
    if user is not None:
        return f"{user}:{CONFIG_REDACTED}"
    return CONFIG_REDACTED


def _redact_config_string(value: str, *, under_secret_key: bool) -> str:
    """Redact one string of an agent config snapshot."""
    if not value.strip() or _PLACEHOLDER_ONLY.fullmatch(value):
        return value
    if under_secret_key:
        return CONFIG_REDACTED
    return _CONFIG_SECRET_VALUE.sub(_replace_config_secret, value)


def redact_agent_config(data: Any, *, _under_secret_key: bool = False) -> Any:
    """Recursively redact secrets from an agent config snapshot.

    Two rules, applied to every level of nested dicts and lists:

    * A string under a key that names a secret is replaced by ``<redacted>``.
      A key names a secret when, lowercased, it is ``token`` or contains one
      of ``api_key``, ``apikey``, ``auth_token``, ``oauth_token``,
      ``access_token``, ``refresh_token``, ``bearer_token``,
      ``session_token``, ``secret``, ``password``, ``passwd``,
      ``authorization`` or ``x-api-key`` (so ``ANTHROPIC_API_KEY`` and
      ``AWS_SECRET_ACCESS_KEY`` match). Every string nested below such a key
      is replaced too. Keys that only reference or report on a secret
      (``..._env_var``, ``..._id``, ``..._ok``) are exempt.
    * Under any other key, the part of a string that looks like a credential
      is replaced: an ``agt_`` agent key, an ``sk-ant-`` or ``sk-`` provider
      key, or ``Bearer`` followed by a token.

    Strings made only of a placeholder (``<AGENT_KEY>``,
    ``<AGENT_KEY_LAST_20>``, ``<redacted>``, optionally after ``Bearer``) are
    never changed, so manual setup instructions stay usable. Booleans, numbers
    and ``None`` are kept as they are: validation flags such as
    ``control_bearer_token_ok`` are read by the server. The function is
    idempotent.

    Args:
        data: A JSON-like value.

    Returns:
        A redacted copy; ``data`` is not modified.
    """
    if isinstance(data, str):
        return _redact_config_string(data, under_secret_key=_under_secret_key)
    if isinstance(data, dict):
        return {
            key: redact_agent_config(
                value,
                _under_secret_key=_under_secret_key or _is_config_secret_key(key),
            )
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [
            redact_agent_config(item, _under_secret_key=_under_secret_key)
            for item in data
        ]
    return data
