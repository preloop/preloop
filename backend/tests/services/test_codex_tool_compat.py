"""Tests for the Codex tool-shape compatibility translation.

Every shape asserted here was captured from the real `codex-cli` binary or from
a prod failure row, not invented:

* `custom` + lark `apply_patch` — `api_usage` `dcf29ac5` (flow exec `0792099a`),
  model `openai/gpt-5.2-codex`, `tools_meta[7].name == "apply_patch"`, upstream
  `400 Missing required parameter: 'tools[7].name'`.
* `namespace` `multi_agent_v1` AND `mcp__preloop` — `api_usage` `c851d95e`
  (flow exec `d610c5a6`), `400 tools[8].type: unknown variant 'namespace'`.
* `custom_tool_call` / `custom_tool_call_output` echoed in the input history —
  observed live: Codex applies a patch only when answered in that shape, and
  aborts with "invoked with incompatible payload" on a `function_call`.
"""

import json

from preloop.services.codex_tool_compat import (
    normalize_custom_tool_call_item,
    restore_custom_tool_calls,
    sanitize_codex_tools,
    unwrap_freeform_arguments,
)

APPLY_PATCH_TOOL = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Apply a patch to the workspace",
    "format": {
        "type": "grammar",
        "syntax": "lark",
        "definition": 'start: "*** Begin Patch"',
    },
}


def test_custom_tool_is_downgraded_to_function_with_top_level_name():
    """The whole point: the upstream must see a `function` with a `name`."""
    tools, freeform = sanitize_codex_tools([APPLY_PATCH_TOOL])

    assert freeform == {"apply_patch"}
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "apply_patch"
    # The exact defect from prod: a missing name is what OpenAI 400s on.
    assert tools[0]["function"]["parameters"]["required"] == ["input"]


def test_custom_tool_grammar_is_preserved_in_the_description():
    """Dropping the grammar would leave the model unable to form a payload."""
    tools, _ = sanitize_codex_tools([APPLY_PATCH_TOOL])

    description = tools[0]["function"]["description"]
    assert "*** Begin Patch" in description
    assert "lark grammar" in description
    assert "do NOT wrap it in JSON" in description


def test_namespace_tools_are_flattened_not_dropped():
    """Prod's `mcp__preloop` namespace held the flow's entire MCP toolset."""
    nested = {
        "type": "namespace",
        "name": "mcp__preloop",
        "tools": [
            {"type": "function", "function": {"name": "get_goal"}},
            {"type": "function", "function": {"name": "update_goal"}},
        ],
    }

    tools, _ = sanitize_codex_tools([nested])

    assert [tool["function"]["name"] for tool in tools] == ["get_goal", "update_goal"]
    assert all(tool["type"] == "function" for tool in tools)


def test_host_executed_tools_are_dropped():
    """`web_search`/`tool_search` have no name and no schema; nothing accepts them."""
    tools, _ = sanitize_codex_tools(
        [{"type": "web_search"}, {"type": "tool_search"}, APPLY_PATCH_TOOL]
    )

    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "apply_patch"


def test_plain_function_tools_are_left_untouched():
    """A request with no Codex-specific shape must not be perturbed at all."""
    plain = [{"type": "function", "function": {"name": "exec_command"}}]

    tools, freeform = sanitize_codex_tools(plain)

    assert tools is plain
    assert freeform == set()


def test_non_list_tools_pass_through():
    """Fail-soft: a malformed `tools` value must not raise."""
    assert sanitize_codex_tools(None) == (None, set())
    assert sanitize_codex_tools("nonsense") == ("nonsense", set())


def test_custom_tool_call_history_item_becomes_a_tool_call():
    """Codex echoes `custom_tool_call` on turn 2; it must not 400 on us."""
    item = {
        "type": "custom_tool_call",
        "call_id": "call_1",
        "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch",
    }

    normalized = normalize_custom_tool_call_item(item)

    assert normalized["id"] == "call_1"
    assert normalized["function"]["name"] == "apply_patch"
    assert json.loads(normalized["function"]["arguments"]) == {
        "input": "*** Begin Patch\n*** End Patch"
    }


def test_custom_tool_call_without_ids_is_skipped():
    """Without a call_id a result cannot be linked back to its call."""
    assert normalize_custom_tool_call_item({"type": "custom_tool_call"}) is None


def test_function_call_is_restored_to_custom_tool_call():
    """Codex ABORTS the run if a freeform tool is answered as a function_call."""
    output = [
        {
            "id": "fc_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "apply_patch",
            "arguments": json.dumps({"input": "*** Begin Patch"}),
        }
    ]

    restored = restore_custom_tool_calls(output, {"apply_patch"})

    assert restored[0]["type"] == "custom_tool_call"
    # Raw text, NOT a JSON envelope: this is what codex's router accepts.
    assert restored[0]["input"] == "*** Begin Patch"
    assert restored[0]["call_id"] == "call_1"


def test_calls_to_untranslated_tools_are_not_rewritten():
    """Only tools the client actually sent as `custom` may be rewritten."""
    output = [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "exec_command",
            "arguments": '{"cmd": "ls"}',
        }
    ]

    assert restore_custom_tool_calls(output, {"apply_patch"}) == output
    assert restore_custom_tool_calls(output, set()) is output


def test_unwrap_falls_back_to_raw_arguments_when_not_an_envelope():
    """A model emitting the raw payload directly is more correct, not less."""
    assert unwrap_freeform_arguments("*** Begin Patch") == "*** Begin Patch"
    assert unwrap_freeform_arguments('{"input": "x"}') == "x"
    # Single-key envelope under another name is still unambiguous.
    assert unwrap_freeform_arguments('{"patch": "y"}') == "y"
    # Multi-key JSON is ambiguous: keep it verbatim rather than guess.
    assert unwrap_freeform_arguments('{"a": "1", "b": "2"}') == '{"a": "1", "b": "2"}'
