"""Unit tests for subject-scoped governance helpers."""

from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    SUBJECT_TYPE_MANAGED_AGENTS,
    get_scoped_tool_rules,
    get_subject_governance,
    set_subject_governance,
)


def test_subject_governance_round_trip_per_subject():
    meta_data = {}
    meta_data = set_subject_governance(
        meta_data,
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id="agent-1",
        config={
            "allowed_models": ["openai/gpt-5"],
            "model_budgets": {"openai/gpt-5": {"monthly_usd_limit": 25}},
            "tool_rules": {"search_issues": [{"action": "allow"}]},
        },
    )
    meta_data = set_subject_governance(
        meta_data,
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-1",
        config={
            "allowed_models": ["openai/gpt-5-mini"],
            "tool_rules": {"search_issues": [{"action": "require_approval"}]},
        },
    )

    agent_config = get_subject_governance(
        meta_data, subject_type=SUBJECT_TYPE_MANAGED_AGENTS, subject_id="agent-1"
    )
    key_config = get_subject_governance(
        meta_data, subject_type=SUBJECT_TYPE_API_KEYS, subject_id="key-1"
    )

    assert agent_config["allowed_models"] == ["openai/gpt-5"]
    assert agent_config["model_budgets"]["openai/gpt-5"]["monthly_usd_limit"] == 25
    assert key_config["allowed_models"] == ["openai/gpt-5-mini"]
    assert key_config["tool_rules"]["search_issues"][0]["action"] == (
        "require_approval"
    )


def test_scoped_tool_rules_prioritize_api_key_before_agent():
    meta_data = {}
    meta_data = set_subject_governance(
        meta_data,
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id="agent-1",
        config={
            "tool_rules": {"search_issues": [{"action": "deny"}]},
        },
    )
    meta_data = set_subject_governance(
        meta_data,
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-1",
        config={
            "tool_rules": {"search_issues": [{"action": "require_approval"}]},
        },
    )

    rules = get_scoped_tool_rules(
        meta_data,
        tool_name="search_issues",
        subject_context={"api_key_id": "key-1", "managed_agent_id": "agent-1"},
    )

    assert [rule["action"] for rule in rules] == ["require_approval"]


def test_scoped_tool_rules_honour_agent_or_absent_source_when_requested():
    """Native evaluation asks for source=agent; MCP-tagged scoped rules drop."""
    meta_data = {}
    meta_data = set_subject_governance(
        meta_data,
        subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
        subject_id="agent-1",
        config={
            "tool_rules": {
                "Write": [
                    {"action": "deny", "source": "mcp"},
                    {"action": "allow", "source": "agent"},
                    {"action": "require_approval"},
                ]
            },
        },
    )

    filtered = get_scoped_tool_rules(
        meta_data,
        tool_name="Write",
        subject_context={
            "managed_agent_id": "agent-1",
            "tool_source": "agent",
        },
    )
    assert [rule["action"] for rule in filtered] == ["allow", "require_approval"]

    unfiltered = get_scoped_tool_rules(
        meta_data,
        tool_name="Write",
        subject_context={"managed_agent_id": "agent-1"},
    )
    assert [rule["action"] for rule in unfiltered] == [
        "deny",
        "allow",
        "require_approval",
    ]
