"""Server-set approval origin markers (#2595)."""

from preloop.utils.approval_origin import (
    TOOL_ORIGIN_MARKER,
    is_native_tool_approval,
    strip_hook_markers,
)


def test_only_the_server_set_native_origin_counts() -> None:
    assert is_native_tool_approval({TOOL_ORIGIN_MARKER: "native"})
    assert not is_native_tool_approval({TOOL_ORIGIN_MARKER: "mcp"})
    # Hook markers are stamped on MCP-origin checks too.
    assert not is_native_tool_approval(
        {"_preloop_source": "claude_code", "_preloop_origin": {"session_id": "s"}}
    )
    assert not is_native_tool_approval(None)


def test_strip_hook_markers_removes_caller_supplied_markers() -> None:
    arguments = {
        "issue": "ABC-1",
        "_preloop_source": "claude_code",
        "_preloop_origin": {"session_id": "s"},
        TOOL_ORIGIN_MARKER: "native",
    }
    assert strip_hook_markers(arguments) == {"issue": "ABC-1"}
    assert TOOL_ORIGIN_MARKER in arguments
