"""Shared metadata for builtin MCP/REST tools.

Keep tool names, descriptions, and JSON schemas here so the REST
``BUILTIN_TOOLS`` catalog and the FastMCP registrations cannot drift.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: The form vocabulary, described once and shared by ask_user and
#: request_approval. Both tools render the same console form, so their tool
#: definitions must document the same subset (see
#: services/question_schema.py for the authoritative grammar).
QUESTION_ITEMS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "Optional rows the question is about (findings, files, hosts). The "
        "console renders them as a table with a checkbox per row, so the "
        "human reads the finding instead of matching an opaque id against "
        "prose. Each row: id (required, the value an answer refers to), "
        "title, description, severity, badges, href."
    ),
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Stable id an answer refers to"},
            "title": {"type": "string", "description": "One-line label for the row"},
            "description": {"type": "string", "description": "Supporting detail"},
            "severity": {
                "type": "string",
                "description": "critical | high | medium | low | info",
            },
            "badges": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short labels shown on the row (e.g. KEV, pip)",
            },
            "href": {"type": "string", "description": "http(s) link to the source"},
        },
        "required": ["id"],
    },
}

QUESTION_INPUT_SCHEMA_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional JSON Schema subset describing the SHAPE of the answer. The "
        "console renders it as a form and the server validates the submitted "
        "answer against it, so the human never types JSON. Root must be "
        '{"type": "object", "properties": {...}, "required": [...]}. Field '
        "types: string (with optional enum, format date/date-time/textarea, "
        "minLength/maxLength), number, integer, boolean, array of "
        '{"enum": [...]} for a multi-select, array of {"type": "object", '
        '"properties": {...}} for per-row fields (give the row an "id" '
        "property whose enum lists the item ids to get the item table with a "
        "reason per row), and object for a named group of scalars. A string "
        'field may carry "x-autofill": "author" or "date"; the platform fills '
        "those from the deciding identity and the decision time and the human "
        "cannot type them. The answer comes back as validated JSON."
    ),
}


REQUEST_APPROVAL_TOOL: Dict[str, Any] = {
    "name": "request_approval",
    "description": (
        "Request approval for an operation before executing it. For isolated "
        "publication, pass publication_candidates with the exact repository "
        "URL, destination branch, base branch, and frozen head SHA for each "
        "write. Those tuples are the only publication authority; text in "
        "context cannot authorize a writer lease."
    ),
    "source": "builtin",
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Description of the operation requiring approval",
            },
            "context": {
                "type": "string",
                "description": "Additional context about the situation",
            },
            "reasoning": {
                "type": "string",
                "description": "Explanation of why this operation is needed",
            },
            "caller": {
                "type": "string",
                "description": (
                    "Optional: Name of the agent or flow requesting approval "
                    "(auto-populated if not specified)"
                ),
            },
            "approval_workflow": {
                "type": "string",
                "description": "Optional name of the approval workflow to use",
            },
            "items": QUESTION_ITEMS_SCHEMA,
            "input_schema": QUESTION_INPUT_SCHEMA_SCHEMA,
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Optional decision window in seconds. Bounded by the "
                    "flow's approval_window_seconds and the account cap. A "
                    "window longer than the short in-process wait parks the "
                    "execution: the run is suspended, holds no runtime, and "
                    "resumes when the human decides or the window closes."
                ),
            },
            "publication_candidates": {
                "type": "array",
                "description": (
                    "Optional isolated-publication destinations. Each item is "
                    "one frozen write: repository_url, branch, base, and "
                    "head_sha. Required when git_clone_config."
                    "publication_approval is set. Context JSON is not used."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repository_url": {
                            "type": "string",
                            "description": "Repository that would receive the writer lease",
                        },
                        "branch": {
                            "type": "string",
                            "description": "Destination branch that would be pushed",
                        },
                        "base": {
                            "type": "string",
                            "description": "Base branch for the publication",
                        },
                        "head_sha": {
                            "type": "string",
                            "description": "Frozen 40-character commit SHA that would be pushed",
                        },
                    },
                    "required": [
                        "repository_url",
                        "branch",
                        "base",
                        "head_sha",
                    ],
                },
            },
        },
        "required": ["operation", "context", "reasoning"],
    },
}


ASK_USER_TOOL: Dict[str, Any] = {
    "name": "ask_user",
    "description": (
        "Ask the human a question and wait for their answer. Offer "
        "multiple-choice options and/or let them type a free-text reply, or "
        "pass items (rows to pick from) and input_schema (the shape of the "
        "answer) to get a real form: a table with checkboxes and per-row "
        "fields instead of a request to type JSON. Returns the answer as "
        "text, or as validated JSON when input_schema was given."
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
            "items": QUESTION_ITEMS_SCHEMA,
            "input_schema": QUESTION_INPUT_SCHEMA_SCHEMA,
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
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Optional decision window in seconds. Bounded by the "
                    "flow's approval_window_seconds and the account cap. A "
                    "window longer than the short in-process wait parks the "
                    "execution: the run is suspended, holds no runtime, and "
                    "resumes when the human decides or the window closes."
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


RESOLVE_SBOM_UPSTREAMS_TOOL: Dict[str, Any] = {
    "name": "resolve_sbom_upstreams",
    "description": (
        "Resolve vendored Arduino/PlatformIO SBOM components (name + "
        "version) to their upstream git repository URL and version-shaped "
        "tag candidates via the public library registries (Arduino library "
        "index, PlatformIO registry). Read-only lookup: a component "
        "resolves only when a registry entry matches its name AND version "
        "and carries a repository URL; everything else comes back "
        "unresolved with a reason — a resolution is never fabricated. "
        "Returns JSON with resolved[] (repository_url, ref_candidates, "
        "enriched_purl), unresolved[] (reason), stats, and per-registry "
        "status."
    ),
    "source": "builtin",
    # Default-off: only SBOM security flows need this lookup, so regular
    # sessions should not pay its tools/list context tax (cf. issue #128).
    # Flow executions opt in via their allowed_mcp_tools allow-list, which
    # bypasses the default-enable filter.
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "description": (
                    "Components to resolve. Each entry carries the SBOM's "
                    "name and version, plus optionally its purl (echoed "
                    "back enriched with a vcs_url qualifier on success)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Component name from the SBOM",
                        },
                        "version": {
                            "type": "string",
                            "description": "Component version from the SBOM",
                        },
                        "purl": {
                            "type": "string",
                            "description": "Optional original purl",
                        },
                    },
                    "required": ["name", "version"],
                },
            },
        },
        "required": ["components"],
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
