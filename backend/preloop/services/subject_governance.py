"""Helpers for subject-scoped agent and API-key governance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional


SUBJECT_GOVERNANCE_KEY = "subject_governance"
SUBJECT_TYPE_MANAGED_AGENTS = "managed_agents"
SUBJECT_TYPE_API_KEYS = "api_keys"
SUBJECT_TYPES = (SUBJECT_TYPE_MANAGED_AGENTS, SUBJECT_TYPE_API_KEYS)


def empty_subject_governance_store() -> dict[str, Any]:
    return {
        SUBJECT_TYPE_MANAGED_AGENTS: {},
        SUBJECT_TYPE_API_KEYS: {},
    }


def normalize_subject_governance_store(
    meta_data: Optional[dict[str, Any]],
) -> dict[str, Any]:
    meta_data = meta_data or {}
    store = meta_data.get(SUBJECT_GOVERNANCE_KEY)
    if not isinstance(store, dict):
        return empty_subject_governance_store()
    normalized = empty_subject_governance_store()
    for subject_type in SUBJECT_TYPES:
        value = store.get(subject_type)
        normalized[subject_type] = value if isinstance(value, dict) else {}
    return normalized


def get_subject_governance(
    meta_data: Optional[dict[str, Any]], *, subject_type: str, subject_id: str
) -> dict[str, Any]:
    store = normalize_subject_governance_store(meta_data)
    subject_bucket = store.get(subject_type)
    if not isinstance(subject_bucket, dict):
        return {}
    config = subject_bucket.get(str(subject_id))
    return deepcopy(config) if isinstance(config, dict) else {}


def set_subject_governance(
    meta_data: Optional[dict[str, Any]],
    *,
    subject_type: str,
    subject_id: str,
    config: Optional[dict[str, Any]],
) -> dict[str, Any]:
    normalized_meta = deepcopy(meta_data or {})
    store = normalize_subject_governance_store(normalized_meta)
    subject_bucket = store.setdefault(subject_type, {})
    if config:
        subject_bucket[str(subject_id)] = sanitize_subject_governance_config(config)
    else:
        subject_bucket.pop(str(subject_id), None)
    normalized_meta[SUBJECT_GOVERNANCE_KEY] = store
    return normalized_meta


def sanitize_subject_governance_config(config: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {
        "allowed_models": [],
        "model_budgets": {},
        "tool_rules": {},
        "tool_enabled_overrides": {},
        "context_optimization": {},
    }
    allowed_models = config.get("allowed_models")
    if isinstance(allowed_models, list):
        sanitized["allowed_models"] = [
            str(item).strip() for item in allowed_models if str(item).strip()
        ]
    model_budgets = config.get("model_budgets")
    if isinstance(model_budgets, dict):
        sanitized["model_budgets"] = deepcopy(model_budgets)
    tool_rules = config.get("tool_rules")
    if isinstance(tool_rules, dict):
        sanitized["tool_rules"] = deepcopy(tool_rules)
    tool_enabled_overrides = config.get("tool_enabled_overrides")
    if isinstance(tool_enabled_overrides, dict):
        sanitized["tool_enabled_overrides"] = deepcopy(tool_enabled_overrides)
    context_optimization = config.get("context_optimization")
    if isinstance(context_optimization, dict):
        sanitized["context_optimization"] = deepcopy(context_optimization)
    return sanitized


def build_subject_context_from_api_key(api_key: Any) -> dict[str, Optional[str]]:
    context_data = (
        api_key.context_data if isinstance(api_key.context_data, dict) else {}
    )
    runtime_principal = (
        context_data.get("runtime_principal")
        if isinstance(context_data.get("runtime_principal"), dict)
        else {}
    )
    return {
        "api_key_id": str(api_key.id) if getattr(api_key, "id", None) else None,
        "managed_agent_id": (
            str(context_data.get("managed_agent_id"))
            if context_data.get("managed_agent_id")
            else None
        ),
        "runtime_session_id": (
            str(context_data.get("runtime_session_id"))
            if context_data.get("runtime_session_id")
            else None
        ),
        "runtime_principal_type": runtime_principal.get("type"),
        "runtime_principal_id": runtime_principal.get("id"),
        "runtime_principal_name": runtime_principal.get("name"),
    }


def subject_scope_chain(
    subject_context: dict[str, Optional[str]],
) -> list[tuple[str, str]]:
    scopes: list[tuple[str, str]] = []
    api_key_id = subject_context.get("api_key_id")
    managed_agent_id = subject_context.get("managed_agent_id")
    if api_key_id:
        scopes.append((SUBJECT_TYPE_API_KEYS, api_key_id))
    if managed_agent_id:
        scopes.append((SUBJECT_TYPE_MANAGED_AGENTS, managed_agent_id))
    return scopes


def get_scoped_tool_rules(
    meta_data: Optional[dict[str, Any]],
    *,
    tool_name: str,
    subject_context: dict[str, Optional[str]],
) -> list[dict[str, Any]]:
    matched_rules: list[dict[str, Any]] = []
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        tool_rules = config.get("tool_rules")
        if not isinstance(tool_rules, dict):
            continue
        rules = tool_rules.get(tool_name)
        if isinstance(rules, list):
            return [rule for rule in deepcopy(rules) if isinstance(rule, dict)]
    return matched_rules


def get_scoped_model_governance(
    meta_data: Optional[dict[str, Any]],
    *,
    subject_context: dict[str, Optional[str]],
) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        if config:
            configs.append(config)
    return configs


def is_tool_enabled_for_subject(
    meta_data: Optional[dict[str, Any]],
    *,
    tool_name: str,
    subject_context: dict[str, Optional[str]],
) -> bool:
    """Check if a tool is explicitly enabled or disabled for a subject.

    Walks the scope chain (most specific to least specific).
    Returns False if an explicit override disabled the tool.
    Returns True if an explicit override enabled the tool, or if no override exists.
    """
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        overrides = config.get("tool_enabled_overrides")
        if not isinstance(overrides, dict):
            continue

        # Check if the tool has an explicit override boolean value
        is_enabled = overrides.get(tool_name)
        if isinstance(is_enabled, bool):
            return is_enabled

    return True
