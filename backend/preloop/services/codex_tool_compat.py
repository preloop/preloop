"""Compatibility translation for the tool shapes the Codex CLI emits.

Why this exists
---------------

Preloop's flow runtime runs the real Codex CLI against Preloop's OpenAI gateway
(``backend/preloop/agents/codex.py`` writes ``wire_api = "responses"``, so Codex
speaks the Responses API to ``/openai/v1/responses``). Codex does not send plain
OpenAI function tools. Depending on which model id the flow is configured with,
it puts one or more of these on the wire, verified against the real
``codex-cli 0.145.0`` binary:

* ``{"type": "custom", "name": "apply_patch", "format": {"type": "grammar",
  "syntax": "lark", ...}}`` — a *freeform* tool whose payload is raw patch text,
  not JSON. Sent for ``gpt-5.4``.
* ``{"type": "namespace", "name": "multi_agent_v1", "tools": [...]}`` — a
  container holding N nested function tools. Sent for ``gpt-5``,
  ``gpt-5-codex``, ``gpt-5.1-codex``.
* ``{"type": "tool_search", ...}`` and ``{"type": "web_search", ...}`` —
  host-executed discovery/search tools with no ``name`` at all.

Forwarded verbatim these are rejected by every upstream the gateway routes to:
OpenAI answers ``400 Missing required parameter: 'tools[N].name'`` (the
``custom``/``tool_search``/``web_search`` entries have no top-level ``name``),
and DeepSeek answers ``400 tools[N].type: unknown variant 'namespace', expected
'function'``. The result is that **every Codex flow fails at its first model
call**, which is what the release-flow build hit on executions ``0792099a``
(openai) and ``d610c5a6`` (deepseek).

Why the fix must live here and not in the runner
------------------------------------------------

Codex resolves these tool shapes from model metadata compiled into its own
binary, keyed on the model *name*. A Preloop gateway alias never matches that
table, and the config knobs that look like they should help
(``apply_patch_tool_type``, ``web_search_tool_type``, ``tool_mode``, whether set
under ``[models."<id>"]`` or via ``-c`` overrides) leave the wire payload
byte-identical — verified experimentally. The runner cannot configure its way
out, so the gateway has to translate.

Why the translation is symmetric
--------------------------------

Rewriting the request alone is not enough, and getting this wrong fails in a way
that only shows up on the first file edit. Verified against the real binary:

* Answering a ``custom`` ``apply_patch`` with an ordinary
  ``function_call`` (arguments as JSON) makes Codex abort the run with
  ``Fatal error: tool apply_patch invoked with incompatible payload``.
* Answering with ``{"type": "custom_tool_call", "name": "apply_patch",
  "call_id": ..., "input": "<raw patch text>"}`` makes Codex apply the patch,
  and it then echoes ``custom_tool_call`` / ``custom_tool_call_output`` items
  back in the *input history* of the next turn.

So three things are needed and all three are implemented here:

1. request tools: ``custom`` -> ``function`` (with a ``{"input": string}``
   schema), ``namespace`` -> its nested functions flattened, host-executed
   entries dropped;
2. request history: ``custom_tool_call`` / ``custom_tool_call_output`` items
   understood as assistant tool calls / tool results, so turn 2 of a session
   does not 400 on our own gateway;
3. response: a model ``function_call`` naming a tool the client sent as
   ``custom`` is rendered back as a ``custom_tool_call`` with the argument
   unwrapped to raw text.

Everything is fail-soft: an unrecognized shape is passed through unchanged, and
a request that never contained a Codex-specific tool is untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Codex tool ``type`` values that the model does not execute: the CLI (or the
# upstream host) runs them. They carry no top-level ``name`` and no function
# schema, so no upstream the gateway routes to can accept them. Dropping them is
# lossless for our purposes — the gateway never implemented them either.
_HOST_EXECUTED_TOOL_TYPES = frozenset(
    {
        "web_search",
        "web_search_preview",
        "tool_search",
        "file_search",
        "code_interpreter",
        "computer_use_preview",
        "image_generation",
        "local_shell",
    }
)

# The single argument a translated freeform tool takes. Codex's freeform tools
# consume raw text (an apply_patch envelope), so the function form wraps that
# text in one string property and the response translation unwraps it again.
FREEFORM_INPUT_KEY = "input"


def _freeform_parameters(description: Optional[str]) -> Dict[str, Any]:
    """Build the JSON schema standing in for a freeform grammar tool."""
    return {
        "type": "object",
        "properties": {
            FREEFORM_INPUT_KEY: {
                "type": "string",
                "description": description
                or "The complete, raw tool payload as plain text.",
            }
        },
        "required": [FREEFORM_INPUT_KEY],
        "additionalProperties": False,
    }


def _custom_tool_description(tool: Dict[str, Any]) -> str:
    """Describe a freeform tool so a model without grammar support can use it.

    The lark grammar is the only place the payload's syntax is written down. A
    model that cannot be handed the grammar must still be told the shape, so the
    grammar definition is inlined into the description rather than discarded.
    """
    description = str(tool.get("description") or "").strip()
    tool_format = tool.get("format")
    if isinstance(tool_format, dict):
        grammar = tool_format.get("grammar")
        definition = None
        if isinstance(grammar, dict):
            definition = grammar.get("definition")
        elif isinstance(grammar, str):
            definition = grammar
        if definition is None:
            definition = tool_format.get("definition")
        if isinstance(definition, str) and definition.strip():
            syntax = str(tool_format.get("syntax") or "").strip()
            label = f"{syntax} grammar" if syntax else "grammar"
            description = (
                f"{description}\n\nThe `{FREEFORM_INPUT_KEY}` argument must be "
                f"the raw tool payload as plain text (do NOT wrap it in JSON), "
                f"matching this {label}:\n{definition.strip()}"
            ).strip()
    return description


def sanitize_codex_tools(tools: Any) -> tuple[Any, Set[str]]:
    """Translate Codex-specific tool definitions into plain function tools.

    Args:
        tools: The request's ``tools`` value, any shape.

    Returns:
        A tuple of (translated tools, names of tools that were freeform
        ``custom`` tools). The second element is what the response translation
        needs in order to render the model's call back in the shape Codex
        accepts; it is empty for every request that contained no such tool.
    """
    if not isinstance(tools, list):
        return tools, set()

    freeform_names: Set[str] = set()
    translated: List[Any] = []
    changed = False

    for tool in tools:
        if not isinstance(tool, dict):
            translated.append(tool)
            continue

        tool_type = tool.get("type")

        if tool_type in _HOST_EXECUTED_TOOL_TYPES:
            # No name, no schema, executed by the client or the vendor host.
            # Every upstream rejects it; the gateway cannot serve it either.
            changed = True
            continue

        if tool_type == "namespace":
            nested = tool.get("tools")
            if not isinstance(nested, list):
                # A namespace with no usable payload is pure 400 fuel.
                changed = True
                continue
            changed = True
            nested_tools, nested_freeform = sanitize_codex_tools(nested)
            if isinstance(nested_tools, list):
                translated.extend(nested_tools)
            freeform_names |= nested_freeform
            continue

        if tool_type == "custom":
            name = tool.get("name")
            custom = tool.get("custom")
            if isinstance(custom, dict) and not name:
                name = custom.get("name")
            if not isinstance(name, str) or not name:
                changed = True
                continue
            changed = True
            freeform_names.add(name)
            translated.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": _custom_tool_description(
                            custom if isinstance(custom, dict) else tool
                        ),
                        "parameters": _freeform_parameters(None),
                    },
                }
            )
            continue

        translated.append(tool)

    if not changed:
        return tools, freeform_names
    return translated, freeform_names


def freeform_tool_names(tools: Any) -> Set[str]:
    """Return the names of freeform ``custom`` tools present in a request."""
    _, names = sanitize_codex_tools(tools)
    return names


def normalize_custom_tool_call_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a Codex ``custom_tool_call`` history item into a function call.

    Codex echoes its freeform calls back on every subsequent turn. Without this,
    turn 2 of any Codex session fails on Preloop's own gateway before it ever
    reaches an upstream.

    Args:
        item: One Responses-API input item.

    Returns:
        A chat-completions ``tool_calls`` entry, or ``None`` when the item lacks
        the identifiers needed to link a call to its result.
    """
    name = item.get("name")
    call_id = item.get("call_id") or item.get("id")
    if not name or not call_id:
        return None
    raw_input = item.get("input")
    if not isinstance(raw_input, str):
        raw_input = "" if raw_input is None else str(raw_input)
    return {
        "id": str(call_id),
        "type": "function",
        "function": {
            "name": str(name),
            "arguments": json.dumps({FREEFORM_INPUT_KEY: raw_input}),
        },
    }


def custom_tool_call_output(item: Dict[str, Any]) -> Optional[str]:
    """Return the call id a ``custom_tool_call_output`` item responds to."""
    call_id = item.get("call_id") or item.get("id")
    if not call_id:
        return None
    return str(call_id)


def unwrap_freeform_arguments(arguments: Any) -> str:
    """Recover the raw freeform payload from a model's JSON arguments.

    Args:
        arguments: The model's ``arguments`` string for a translated tool.

    Returns:
        The raw text to hand back to Codex. Falls back to the original string
        when it is not the JSON envelope we asked for, since a model that
        emitted the raw patch directly is *more* correct, not less.
    """
    if not isinstance(arguments, str):
        return "" if arguments is None else str(arguments)
    try:
        decoded = json.loads(arguments)
    except (ValueError, TypeError):
        return arguments
    if isinstance(decoded, dict):
        value = decoded.get(FREEFORM_INPUT_KEY)
        if isinstance(value, str):
            return value
        if value is not None:
            return str(value)
        # A single-key envelope under a different name is still unambiguous.
        if len(decoded) == 1:
            only_value = next(iter(decoded.values()))
            if isinstance(only_value, str):
                return only_value
        return arguments
    if isinstance(decoded, str):
        return decoded
    return arguments


def restore_custom_tool_calls(
    output_items: List[Dict[str, Any]], freeform_names: Set[str]
) -> List[Dict[str, Any]]:
    """Render calls to translated tools back in the shape Codex accepts.

    A ``function_call`` naming a tool the client originally sent as ``custom``
    is rewritten to a ``custom_tool_call`` carrying the raw payload. Codex
    rejects the function form for these tools outright ("invoked with
    incompatible payload"), so this is required for the edit to land.

    Args:
        output_items: Responses-API output items built from the upstream reply.
        freeform_names: Names of tools the client sent as freeform ``custom``.

    Returns:
        The output items, with matching calls rewritten. Returns the input
        unchanged when the request had no freeform tools.
    """
    if not freeform_names or not isinstance(output_items, list):
        return output_items

    restored: List[Dict[str, Any]] = []
    for item in output_items:
        if (
            not isinstance(item, dict)
            or item.get("type") != "function_call"
            or item.get("name") not in freeform_names
        ):
            restored.append(item)
            continue
        restored.append(
            {
                "id": item.get("id"),
                "type": "custom_tool_call",
                "status": item.get("status", "completed"),
                "call_id": item.get("call_id"),
                "name": item.get("name"),
                "input": unwrap_freeform_arguments(item.get("arguments")),
            }
        )
    return restored
