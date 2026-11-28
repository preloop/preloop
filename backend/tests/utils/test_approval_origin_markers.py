"""Server-set approval origin markers (#2595)."""

from preloop.utils.approval_origin import (
    TOOL_ORIGIN_MARKER,
    hook_recorded_tool_origin,
    strip_hook_markers,
)


def test_server_marker_names_the_origin() -> None:
    assert hook_recorded_tool_origin({TOOL_ORIGIN_MARKER: "native"}) == "native"
    assert hook_recorded_tool_origin({TOOL_ORIGIN_MARKER: "mcp"}) == "mcp"


def test_rows_from_before_the_marker_are_native() -> None:
    assert hook_recorded_tool_origin({"_preloop_source": "claude_code"}) == "native"
    assert (
        hook_recorded_tool_origin({"_preloop_origin": {"session_id": "s"}}) == "native"
    )


def test_mcp_dispatcher_rows_are_not_hook_recorded() -> None:
    assert hook_recorded_tool_origin({"issue": "ABC-1"}) is None
    assert hook_recorded_tool_origin(None) is None


def test_strip_hook_markers_removes_caller_supplied_markers() -> None:
    arguments = {
        "issue": "ABC-1",
        "_preloop_source": "claude_code",
        "_preloop_origin": {"session_id": "s"},
        TOOL_ORIGIN_MARKER: "native",
    }
    assert strip_hook_markers(arguments) == {"issue": "ABC-1"}
    assert hook_recorded_tool_origin(strip_hook_markers(arguments)) is None
