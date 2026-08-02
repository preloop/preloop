"""Shared metadata for builtin MCP/REST tools.

Keep tool names, descriptions, and JSON schemas here so the REST
``BUILTIN_TOOLS`` catalog and the FastMCP registrations cannot drift.
"""

from __future__ import annotations

from typing import Any, Dict, List

ASK_USER_TOOL: Dict[str, Any] = {
    "name": "ask_user",
    "description": (
        "Ask the human a question and wait for their answer. Offer "
        "multiple-choice options and/or let them type a free-text reply. "
        "Returns the user's answer as text."
    ),
    "source": "builtin",
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the human",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of answer options to offer",
            },
            "allow_free_text": {
                "type": "boolean",
                "description": (
                    "Whether the user may type a free-text answer (default true)"
                ),
            },
            "context": {
                "type": "string",
                "description": "Optional additional context shown to the human",
            },
            "approval_workflow": {
                "type": "string",
                "description": (
                    "Optional name of the approval workflow to route the question to"
                ),
            },
        },
        "required": ["question"],
    },
}


PERMISSION_PROMPT_TOOL: Dict[str, Any] = {
    "name": "permission_prompt",
    "description": (
        "Claude Code --permission-prompt-tool adapter. Decides whether a "
        "native tool call may proceed by routing it through Preloop's "
        "approval workflows, and returns Claude's required behavior schema "
        'as a JSON string: {"behavior": "allow", "updatedInput": {...}} '
        'or {"behavior": "deny", "message": "..."}. A deny message '
        "starting with PRELOOP_APPROVAL_PENDING means the human has not "
        "decided yet: retry the same tool call to keep waiting. Intended for "
        "headless runs (claude -p --permission-prompt-tool "
        "mcp__preloop__permission_prompt); not for direct agent use."
    ),
    "source": "builtin",
    # Default-off: only headless Claude Code runs that pass
    # --permission-prompt-tool need this tool, so accounts should not pay
    # its tools/list context tax (issue #128) unless they opt in.
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Name of the native tool Claude wants to run",
            },
            "input": {
                "type": "object",
                "description": "Arguments of the native tool call",
            },
            "tool_use_id": {
                "type": "string",
                "description": "Claude's tool use id (recorded for audit)",
            },
        },
        "required": ["tool_name", "input"],
    },
}


def builtin_tools_with_ask_user(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ``tools`` with ``ASK_USER_TOOL`` inserted after request_approval."""
    result: List[Dict[str, Any]] = []
    inserted = False
    for tool in tools:
        result.append(tool)
        if tool.get("name") == "request_approval":
            result.append(dict(ASK_USER_TOOL))
            inserted = True
    if not inserted:
        result.append(dict(ASK_USER_TOOL))
    return result
