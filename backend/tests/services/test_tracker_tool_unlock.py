"""Unit tests for tracker-gated builtin unlock diff (#145)."""

from preloop.services.tracker_tool_unlock import (
    enabled_map_from_configs,
    supported_tracker_builtin_names,
    unlocked_tool_names_after_tracker,
)

ANY_TRACKER_DEFAULT_ENABLED = {
    "get_issue",
    "create_issue",
    "update_issue",
    "search",
    "add_comment",
}
GITHUB_GITLAB_ONLY = {
    "update_comment",
    "get_pull_request",
    "update_pull_request",
    "create_pull_request",
}
COMPLIANCE_DEFAULT_DISABLED = {"estimate_compliance", "improve_compliance"}


def test_first_jira_unlocks_any_tracker_default_enabled_only():
    unlocked = unlocked_tool_names_after_tracker(
        had_tracker=False,
        types_before=[],
        types_after=["jira"],
    )
    assert set(unlocked) == ANY_TRACKER_DEFAULT_ENABLED
    assert COMPLIANCE_DEFAULT_DISABLED.isdisjoint(unlocked)
    assert GITHUB_GITLAB_ONLY.isdisjoint(unlocked)


def test_first_github_unlocks_any_plus_github_gitlab_tools():
    unlocked = unlocked_tool_names_after_tracker(
        had_tracker=False,
        types_before=[],
        types_after=["github"],
    )
    assert set(unlocked) == ANY_TRACKER_DEFAULT_ENABLED | GITHUB_GITLAB_ONLY
    assert COMPLIANCE_DEFAULT_DISABLED.isdisjoint(unlocked)


def test_second_tracker_same_capability_returns_empty():
    unlocked = unlocked_tool_names_after_tracker(
        had_tracker=True,
        types_before=["github"],
        types_after=["github", "jira"],
    )
    assert unlocked == []


def test_github_after_jira_unlocks_only_github_gitlab_set():
    unlocked = unlocked_tool_names_after_tracker(
        had_tracker=True,
        types_before=["jira"],
        types_after=["jira", "github"],
    )
    assert set(unlocked) == GITHUB_GITLAB_ONLY


def test_explicit_disabled_config_excludes_tool():
    unlocked = unlocked_tool_names_after_tracker(
        had_tracker=False,
        types_before=[],
        types_after=["jira"],
        enabled_by_name={"add_comment": False},
    )
    assert "add_comment" not in unlocked
    assert set(unlocked) == ANY_TRACKER_DEFAULT_ENABLED - {"add_comment"}


def test_supported_names_respect_default_enabled_false():
    names = supported_tracker_builtin_names(
        has_tracker=True,
        tracker_types=["github"],
    )
    assert COMPLIANCE_DEFAULT_DISABLED.isdisjoint(names)
    assert ANY_TRACKER_DEFAULT_ENABLED.issubset(names)


class _FakeConfig:
    def __init__(self, tool_name: str, tool_source: str, is_enabled: bool):
        self.tool_name = tool_name
        self.tool_source = tool_source
        self.is_enabled = is_enabled


def test_enabled_map_from_configs_filters_builtins():
    configs = [
        _FakeConfig("add_comment", "builtin", False),
        _FakeConfig("external_tool", "mcp", True),
    ]
    assert enabled_map_from_configs(configs) == {"add_comment": False}
