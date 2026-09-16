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


SEND_NOTE_TOOL: Dict[str, Any] = {
    "name": "send_note",
    "description": (
        "Leave an operator note for exactly one other target: another managed "
        "agent, a runtime session, or a flow execution. The note is delivered "
        "into the target's next turn by the same rail that carries a human's "
        "note, and is recorded with you as the author, so a hand off between "
        "agents leaves a record instead of a file nobody sweeps. Name exactly "
        "one of agent_id, runtime_session_id or execution_id; naming none or "
        "two is refused and writes nothing. Targets outside your account do "
        "not exist. You can reach the runs you started, at any depth, and "
        "nothing else unless an access rule grants more: a sibling run, the "
        "run that started you, and an agent running nothing of yours are all "
        "refused, naming the scope required. Rate limited per author, per "
        "target, per hour."
    ),
    "source": "builtin",
    # Default-off: most agents never need to talk to a sibling, and every
    # agent would otherwise pay this schema's tools/list context tax
    # (cf. issue #128). A flow opts in through its tool allow-list, and an
    # account through the Tools page.
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "What the target should be told, in your own words. Say "
                    "what changed and what you want done about it; the "
                    "reader has none of your context."
                ),
                "minLength": 1,
                "maxLength": 4096,
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "Target managed agent. Delivered at its current session's "
                    "next turn, or at the start of the next session it opens."
                ),
            },
            "runtime_session_id": {
                "type": "string",
                "description": "Target runtime session, and only that session.",
            },
            "execution_id": {
                "type": "string",
                "description": (
                    "Target flow execution, resolved to the runtime session "
                    "it is running on. Deliverable once the run has made a "
                    "governed call."
                ),
            },
        },
        "required": ["text"],
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


RUN_FLOW_TOOL: Dict[str, Any] = {
    "name": "run_flow",
    "description": (
        "Run another flow of this account as a child of the current "
        "Run another flow of this account as a child of the current "
        "execution. Asynchronous by default: the call returns as soon as the "
        "child execution row exists. Pass wait=true on the last call of a fan "
        "out to wait for every child this execution started; if they are not "
        "all done within a short window this execution is parked, holds no "
        "container while they run, and resumes with one completion record per "
        "child. The target must be named on the calling flow's callable flows "
        "allowlist; depth, cycles, the number of direct children and what "
        "the delegation tree may spend are capped server side. Returns one "
        "A2A shaped task record as JSON. A refusal is a record too: state "
        "TASK_STATE_REJECTED with 'preloop.ai/refusalReason' naming the rule "
        "that declined the call (flow_not_found, flow_not_callable, "
        "tool_not_allowed, depth_exceeded, cycle_detected, fanout_exceeded, "
        "budget_exceeded), not an error to parse out of prose."
    ),
    "source": "builtin",
    # Default-off: delegation spends an account's budget from inside an
    # agent turn, so a flow opts in by selecting the tool (cf. issue #128
    # for the context tax argument, #630 for the authority one). A flow
    # execution's allowed_mcp_tools allow-list bypasses the default-enable
    # filter, which is the only way this tool is meant to be reached.
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "flow": {
                "type": "string",
                "description": (
                    "Slug or name of the flow to run, resolved inside this "
                    "account. A reference naming nothing in the account is "
                    "refused with flow_not_found."
                ),
            },
            "payload": {
                "type": "object",
                "description": (
                    "Trigger payload for the child. Readable in the child's "
                    "prompt as {{trigger_event.payload.<key>}}. Model and "
                    "harness overrides in here are stripped: a child runs on "
                    "its own flow's routing."
                ),
            },
            "label": {
                "type": "string",
                "description": (
                    "Short label for this child, recorded on the child so a "
                    "human reading the execution tree can tell siblings apart."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Optional window for the child, clamped to the calling "
                    "execution's own remaining time. Recorded on the child."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "Wait for every child this execution has started, not "
                    "only this one, then stop working: if they are all done "
                    "within a short in process window their completion "
                    "records are returned here, otherwise this execution is "
                    "parked and resumes when the last child finishes (or when "
                    "the child wait deadline passes, with the unfinished ones "
                    "marked expired). The results arrive as the next turn of "
                    "this run, not as the return value of this call. Defaults "
                    "to false."
                ),
            },
            "max_cost_usd": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": (
                    "Most this child, and anything it delegates in turn, may "
                    "cost in USD. Lowered to the calling flow's per child "
                    "ceiling when that is smaller. A child that does not fit "
                    "in what the delegation tree has left is refused with "
                    "budget_exceeded before it starts, and the refusal says "
                    "how much is left; children already running are never "
                    "killed to make room. Omit it to take the instance "
                    "default."
                ),
            },
        },
        "required": ["flow"],
    },
}


GET_EXECUTION_TOOL: Dict[str, Any] = {
    "name": "get_execution",
    "description": (
        "Read the state, cost and result of an execution this execution "
        "started, or of this execution itself. Use it to poll a child "
        "created with run_flow: the id to pass is the one that call "
        "returned. Scope is enforced on the server and is exactly this "
        "execution and its descendants; anything else, including an "
        "execution that does not exist, comes back as a record with state "
        "TASK_STATE_REJECTED and 'preloop.ai/refusalReason' set to "
        "execution_not_found. Returns one A2A shaped task record as JSON: "
        "the state, the Preloop status it was mapped from, the depth, the "
        "cost and tokens spent so far, and, when the execution has finished "
        "and include_result is true, its result as an artifact. A failed "
        "execution carries its failure category on the status message. A "
        "result larger than the size cap comes back truncated, flagged with "
        "'preloop.ai/truncated', and with the path that serves the whole "
        "document."
    ),
    "source": "builtin",
    # Default-off, like run_flow: a tool that reads execution rows is only
    # useful to a flow that delegates, and every unused tool in a prompt is
    # a context tax on every other flow (cf. issue #128).
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "execution_id": {
                "type": "string",
                "description": (
                    "Execution to read, as returned by run_flow. Must be "
                    "this execution or one it started, directly or through "
                    "another child."
                ),
            },
            "include_result": {
                "type": "boolean",
                "description": (
                    "Whether to return the execution's result payload. "
                    "Defaults to false, because a result is only there once "
                    "the execution has finished and it is the expensive part "
                    "of the answer. A result is never returned for an "
                    "execution that is still running."
                ),
            },
        },
        "required": ["execution_id"],
    },
}


#: Sessions one ``search_sessions`` call returns when the caller says nothing.
#: Five is a handful an agent can actually read; a wider question is answered
#: by ``total``, which says how many sessions matched, not by a longer page.
SEARCH_SESSIONS_DEFAULT_LIMIT = 5

#: Most sessions one call may ask for. The response size cap is the real
#: bound (see services/agent_session_search.py); this one keeps a caller from
#: paying for ranking work whose results the cap would drop anyway.
SEARCH_SESSIONS_MAX_LIMIT = 20

#: Scope vocabulary. ``own`` is the calling agent's own sessions and is the
#: default; ``account`` is every session of the account and needs the grant
#: the tool refuses without.
SEARCH_SESSIONS_SCOPE_OWN = "own"
SEARCH_SESSIONS_SCOPE_ACCOUNT = "account"
SEARCH_SESSIONS_SCOPES = (
    SEARCH_SESSIONS_SCOPE_OWN,
    SEARCH_SESSIONS_SCOPE_ACCOUNT,
)


SEARCH_SESSIONS_TOOL: Dict[str, Any] = {
    "name": "search_sessions",
    "description": (
        "Search what past sessions did, ranked by relevance, before "
        "repeating the work: whether that migration already ran, what the "
        "last run concluded, what this user was already asked. Scope is "
        "your own sessions unless you say otherwise. Asking for scope "
        "'account' without the operator grant is refused by name "
        "(account_scope_not_granted), never quietly narrowed, so a result "
        "set always means what you asked for. Results are compact on "
        "purpose: per session its reference, when the match happened, one "
        "snippet with the matching words marked, and why it matched. The "
        "whole answer is capped so a search cannot flood your context; when "
        "it is, truncated is true, results_omitted says how many were "
        "dropped and total says how many sessions matched, so narrow the "
        "query or the time range rather than paging. The degraded block "
        "says what the ranking could not do: with semantic ranking off, a "
        "miss is a keyword miss and not proof the work was never done. No "
        "match is an empty results list, not an error."
    ),
    "source": "builtin",
    # Default-off, like the other read tools added since #128: an agent that
    # never asks the corpus a question should not pay this schema's
    # tools/list context tax, and the corpus itself is opt-in per deployment.
    # A flow opts in through its tool allow-list, an account through the
    # Tools page.
    "default_enabled": False,
    "requires_tracker": False,
    "required_tracker_types": [],
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look for, parsed the way a search box is: a "
                    'quoted phrase ("rolling restart") stays a phrase, `or` '
                    "alternates and a leading `-` excludes. Terms, not a "
                    "sentence: the corpus is transcript text, so the words "
                    "an earlier run would have used beat a description of "
                    "what you want."
                ),
                "minLength": 1,
                "maxLength": 512,
            },
            "scope": {
                "type": "string",
                "enum": list(SEARCH_SESSIONS_SCOPES),
                "description": (
                    "Whose sessions to search. 'own' (the default) is the "
                    "sessions you ran. 'account' is every session of the "
                    "account and is refused unless an operator has granted "
                    "it to you."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["keyword", "semantic", "hybrid"],
                "description": (
                    "Requested ranking. Anything other than keyword is "
                    "answered with keyword results and a degraded marker "
                    "saying semantic ranking is not enabled, rather than an "
                    "error."
                ),
            },
            "start_date": {
                "type": "string",
                "format": "date-time",
                "description": (
                    "Only content at or after this instant. ISO 8601 with a "
                    "timezone offset; a value without one is refused."
                ),
            },
            "end_date": {
                "type": "string",
                "format": "date-time",
                "description": (
                    "Only content strictly before this instant. ISO 8601 "
                    "with a timezone offset; a value without one is refused."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": SEARCH_SESSIONS_MAX_LIMIT,
                "description": (
                    "Sessions to return, at most "
                    f"{SEARCH_SESSIONS_MAX_LIMIT} and "
                    f"{SEARCH_SESSIONS_DEFAULT_LIMIT} by default. The "
                    "response size cap can still return fewer."
                ),
            },
        },
        "required": ["query"],
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


# ``get_issue`` and ``update_issue`` carry the triage surface that
# ``get_issue_triage_context`` and ``apply_issue_triage`` used to own (issue
# #661). The description and schema live here so the REST catalogue
# (tools.py), the FastMCP registration (initialize_mcp.py) and the dynamic
# server (dynamic_mcp_server.py) cannot drift apart.

GET_ISSUE_DESCRIPTION = (
    "Get detailed information about an issue by its identifier (URL, key, or "
    "ID). Returns the synchronized snapshot. Pass include to add blocks read "
    "live from the tracker: label_catalog for the complete project label "
    "catalogue and the recognized complexity scheme, revision for the "
    "authoritative provider content and the expected_revision that a triage "
    "update_issue call must quote."
)

GET_ISSUE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue": {
            "type": "string",
            "description": "Issue identifier (URL, key like 'PROJECT#123', or UUID)",
        },
        "include": {
            "type": "array",
            "description": (
                "Optional extra blocks, each read live from the tracker at "
                "the cost of one round trip. GitHub and GitLab only."
            ),
            "items": {"type": "string", "enum": ["label_catalog", "revision"]},
        },
    },
    "required": ["issue"],
}

UPDATE_ISSUE_DESCRIPTION = (
    "Update an existing issue's metadata, write a managed triage assessment, "
    "and/or manage GitHub issue reactions. To add or remove a reaction only, "
    "pass add_reaction or remove_reaction without other fields. To record a "
    "triage assessment, pass expected_revision (from get_issue with "
    'include=["revision"]) and assessment, optionally complexity_label and '
    "title: that path preserves issue content outside the managed section, "
    "moves only labels in the recognized complexity family, creates standard "
    "complexity labels only when the project has no scheme, and returns a "
    "truthful conflict or partial-write receipt instead of the plain update "
    "response. It cannot be combined with the other metadata fields."
)

UPDATE_ISSUE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue": {"type": "string", "description": "Issue identifier"},
        "title": {"type": "string", "description": "New title"},
        "description": {"type": "string", "description": "New description"},
        "status": {"type": "string", "description": "New status"},
        "priority": {"type": "string", "description": "New priority"},
        "assignee": {"type": "string", "description": "New assignee"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "add_reaction": {
            "type": "string",
            "description": (
                "Reaction to add (GitHub: eyes, +1, heart, hooray, rocket, "
                "laugh, confused, -1). GitLab issues do not support reactions."
            ),
        },
        "remove_reaction": {
            "type": "string",
            "description": "Reaction to remove (same names as add_reaction)",
        },
        "expected_revision": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": (
                "Revision from get_issue include=revision. Required with "
                "assessment; a stale value returns a conflict receipt and "
                "writes nothing."
            ),
        },
        "assessment": {
            "type": "string",
            "minLength": 1,
            "maxLength": 16000,
            "description": (
                "Markdown for the managed triage section. Replaces only that "
                "section; human text around it is preserved."
            ),
        },
        "complexity_label": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 255},
                {"type": "null"},
            ],
            "default": None,
            "description": (
                "Exact name from the complexity scheme returned by get_issue "
                "include=label_catalog. Null leaves complexity unset."
            ),
        },
    },
    "required": ["issue"],
}
