"""Estimate per-tool schema token cost as served to agents.

Mirrors the justification-parameter injection in
``dynamic_fastmcp.list_tools`` and uses the same ``estimate_tokens``
heuristic as gateway ``tools_meta`` attribution.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping, Optional

from preloop.services.context_optimization import estimate_tokens

# Keep in sync with dynamic_fastmcp.list_tools justification injection.
_JUSTIFICATION_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Provide your reasoning and context for why "
        "this tool is being called. This will be "
        "reviewed by approvers and logged for audit "
        "purposes."
    ),
}


def apply_justification_to_schema(
    schema: Optional[Mapping[str, Any]],
    justification_mode: Optional[str],
) -> dict[str, Any]:
    """Return a schema copy with justification injected when configured.

    Args:
        schema: Tool input JSON schema (may be None).
        justification_mode: ``optional``, ``required``, or other/None.

    Returns:
        Schema dict reflecting what ``list_tools`` would advertise.
    """
    base: dict[str, Any]
    if isinstance(schema, Mapping):
        base = copy.deepcopy(dict(schema))
    else:
        base = {"type": "object", "properties": {}}

    if justification_mode not in ("optional", "required"):
        return base

    properties = base.setdefault("properties", {})
    if not isinstance(properties, dict):
        properties = {}
        base["properties"] = properties
    properties["justification"] = copy.deepcopy(_JUSTIFICATION_PROPERTY)

    if justification_mode == "required":
        required = list(base.get("required") or [])
        if "justification" not in required:
            required.append("justification")
        base["required"] = required

    return base


def estimate_tool_schema_tokens(
    *,
    name: str,
    description: str,
    schema: Optional[Mapping[str, Any]],
    justification_mode: Optional[str] = None,
) -> int:
    """Estimate tokens for a tool definition as served to agents.

    Serializes name, description, and parameters (with justification
    injection when configured) the same way gateway ``tools_meta``
    estimates schema cost: ``json.dumps(..., sort_keys=True)`` plus
    ``estimate_tokens``.

    Args:
        name: Tool name.
        description: Tool description.
        schema: Input JSON schema before injection.
        justification_mode: Optional/required justification setting.

    Returns:
        Estimated token count for one request that advertises this tool.
    """
    served_schema = apply_justification_to_schema(schema, justification_mode)
    definition = {
        "name": name,
        "description": description or "",
        "parameters": served_schema,
    }
    try:
        schema_json = json.dumps(definition, default=str, sort_keys=True)
    except (TypeError, ValueError):
        schema_json = ""
    return int(estimate_tokens(schema_json))
