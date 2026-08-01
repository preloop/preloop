"""Tests for unused-builtin detection and account-wide disable (#146)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_tool_configuration,
)
from preloop.models.schemas.tool_configuration import ToolConfigurationCreate
from preloop.schemas.gateway_usage import (
    RuntimeSessionOptimizationActionSpec,
    RuntimeSessionOptimizationApplyRequest,
    RuntimeSessionOptimizationSuggestion,
)
from preloop.services.builtin_tool_optimizer import (
    expand_tool_name_match_keys,
    partition_unused_tools,
    resolve_builtin_tool_name,
    tool_name_in_removed,
)
from preloop.services.context_analysis import (
    SessionContextProfile,
    ToolSchemaOverheadProfile,
    compute_profile_savings,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.savings_measurement import compute_input_token_savings
from preloop.services.session_optimization import (
    ACTION_DISABLE_BUILTIN_TOOLS,
    SessionOptimizationService,
)


BUILTIN_NAMES = {
    "get_issue",
    "create_issue",
    "search",
    "request_approval",
    "permission_prompt",
}


def test_resolve_builtin_maps_preloop_prefix() -> None:
    """mcp__preloop__get_issue maps to builtin get_issue."""
    assert (
        resolve_builtin_tool_name(
            "mcp__preloop__get_issue", builtin_names=BUILTIN_NAMES
        )
        == "get_issue"
    )
    assert (
        resolve_builtin_tool_name(
            "mcp__preloop-mcp__get_issue", builtin_names=BUILTIN_NAMES
        )
        == "get_issue"
    )


def test_resolve_builtin_rejects_external_mcp_prefix() -> None:
    """mcp__github__search must not map to builtin search."""
    assert (
        resolve_builtin_tool_name("mcp__github__search", builtin_names=BUILTIN_NAMES)
        is None
    )


def test_resolve_builtin_accepts_bare_name() -> None:
    """Bare get_issue maps when present in the catalog."""
    assert (
        resolve_builtin_tool_name("get_issue", builtin_names=BUILTIN_NAMES)
        == "get_issue"
    )


def test_expand_keys_do_not_alias_external_mcp_to_bare() -> None:
    """External MCP prefixes stay namespaced for matching."""
    keys = expand_tool_name_match_keys("mcp__github__search")
    assert "search" not in keys
    assert "mcp__github__search" in keys


def test_tool_name_in_removed_accepts_bare_and_preloop_prefix() -> None:
    """Replay matching accepts bare builtin against Preloop-prefixed defs."""
    assert tool_name_in_removed("mcp__preloop__get_issue", {"get_issue"})
    assert tool_name_in_removed("get_issue", {"mcp__preloop__get_issue"})
    assert not tool_name_in_removed("mcp__github__search", {"search"})


def test_partition_tier1_zero_invocations() -> None:
    """Zero account-wide invocations place builtins on the disable path."""
    part = partition_unused_tools(
        ["mcp__preloop__get_issue", "never_used_ext"],
        builtin_names=BUILTIN_NAMES,
        unused_tool_tokens={
            "mcp__preloop__get_issue": 1200,
            "never_used_ext": 800,
        },
        invocation_counts={},
    )
    assert part.tier1_builtin_names == ["get_issue"]
    assert part.tier1_session_tokens == 1200
    assert part.scope_raw_names == ["never_used_ext"]
    assert part.scope_session_tokens == 800


def test_partition_tier2_invoked_elsewhere_stays_on_scope() -> None:
    """Builtins invoked by another agent stay on agent-scoped scope_tools."""
    part = partition_unused_tools(
        ["mcp__preloop__get_issue", "search"],
        builtin_names=BUILTIN_NAMES,
        unused_tool_tokens={
            "mcp__preloop__get_issue": 1200,
            "search": 500,
        },
        invocation_counts={"get_issue": 3},
    )
    assert part.tier1_builtin_names == ["search"]
    assert "mcp__preloop__get_issue" in part.scope_raw_names
    assert part.tier1_session_tokens == 500
    assert part.scope_session_tokens == 1200


def test_partition_dedupe_savings_sum_unchanged() -> None:
    """Carved suggestion tokens sum to the original unused total."""
    unused_tokens = {
        "mcp__preloop__get_issue": 1200,
        "mcp__github__search": 900,
        "local_helper": 100,
    }
    total = sum(unused_tokens.values())
    part = partition_unused_tools(
        list(unused_tokens),
        builtin_names=BUILTIN_NAMES,
        unused_tool_tokens=unused_tokens,
        invocation_counts={},
    )
    assert part.tier1_session_tokens + part.scope_session_tokens == total
    # External search stays on scope; get_issue is tier 1.
    assert "get_issue" in part.tier1_builtin_names
    assert "mcp__github__search" in part.scope_raw_names


def test_compute_profile_savings_unchanged_by_suggestion_split() -> None:
    """Headline profile savings still use the single unused-schema figure."""
    profile = SessionContextProfile(
        session_id="s1",
        analyzed_event_count=2,
        total_prompt_tokens=10_000,
        total_completion_tokens=500,
        tool_schema_overhead=ToolSchemaOverheadProfile(
            unused_tool_names=["mcp__preloop__get_issue", "ext"],
            unused_tool_tokens={
                "mcp__preloop__get_issue": 1200,
                "ext": 800,
            },
            unused_schema_tokens_estimate=2000,
            resend_count=2,
        ),
    )
    before = compute_profile_savings(profile).total_tokens
    # Suggestion-level split does not mutate the profile.
    part = partition_unused_tools(
        profile.tool_schema_overhead.unused_tool_names,
        builtin_names=BUILTIN_NAMES,
        unused_tool_tokens=profile.tool_schema_overhead.unused_tool_tokens,
        invocation_counts={},
    )
    after = compute_profile_savings(profile).total_tokens
    assert before == after == 2000
    assert part.tier1_session_tokens + part.scope_session_tokens == 2000


def _summary(*, total_tokens: int = 10_000, cost: float = 1.0) -> MagicMock:
    summary = MagicMock()
    summary.token_usage.total_tokens = total_tokens
    summary.token_usage.prompt_tokens = total_tokens
    summary.estimated_cost = cost
    summary.failed_requests = 0
    return summary


def test_tier1_suggestion_emits_disable_builtin_tools() -> None:
    """Unused builtins with zero account usage produce disable-builtin-tools."""
    profile = SessionContextProfile(
        session_id="s1",
        analyzed_event_count=2,
        total_prompt_tokens=8_000,
        total_completion_tokens=500,
        tool_schema_overhead=ToolSchemaOverheadProfile(
            advertised_tools=3,
            invoked_tools=1,
            unused_tool_names=["mcp__preloop__get_issue", "ext_tool"],
            unused_tool_tokens={
                "mcp__preloop__get_issue": 1500,
                "ext_tool": 500,
            },
            unused_schema_tokens_estimate=2000,
            resend_count=2,
        ),
    )
    account = MagicMock()
    account.id = uuid4()
    service = SessionOptimizationService(MagicMock())
    with patch.object(
        service,
        "_partition_unused_for_account",
        return_value=partition_unused_tools(
            profile.tool_schema_overhead.unused_tool_names,
            builtin_names={"get_issue"},
            unused_tool_tokens=profile.tool_schema_overhead.unused_tool_tokens,
            invocation_counts={},
        ),
    ):
        suggestions = service._local_optimization_suggestions(
            _summary(), profile, account=account
        )
    by_id = {s.id: s for s in suggestions}
    assert "disable-builtin-tools" in by_id
    assert by_id["disable-builtin-tools"].expected_savings_tokens == 1500
    assert any(
        "invocation_count = 0" in line
        for line in by_id["disable-builtin-tools"].evidence
    )
    assert "scope-tools" in by_id
    assert by_id["scope-tools"].expected_savings_tokens == 500
    assert "ext_tool" in by_id["scope-tools"].evidence[1]
    assert "get_issue" not in by_id["scope-tools"].evidence[1]


def test_tier2_only_scope_tools_for_invoked_elsewhere() -> None:
    """Builtins used elsewhere only get agent-scoped scope_tools."""
    profile = SessionContextProfile(
        session_id="s1",
        analyzed_event_count=1,
        total_prompt_tokens=4_000,
        total_completion_tokens=100,
        tool_schema_overhead=ToolSchemaOverheadProfile(
            advertised_tools=2,
            invoked_tools=0,
            unused_tool_names=["get_issue"],
            unused_tool_tokens={"get_issue": 900},
            unused_schema_tokens_estimate=900,
            resend_count=1,
        ),
    )
    account = MagicMock()
    account.id = uuid4()
    service = SessionOptimizationService(MagicMock())
    with patch.object(
        service,
        "_partition_unused_for_account",
        return_value=partition_unused_tools(
            ["get_issue"],
            builtin_names={"get_issue"},
            unused_tool_tokens={"get_issue": 900},
            invocation_counts={"get_issue": 2},
        ),
    ):
        suggestions = service._local_optimization_suggestions(
            _summary(), profile, account=account
        )
    by_id = {s.id: s for s in suggestions}
    assert "disable-builtin-tools" not in by_id
    assert "scope-tools" in by_id
    assert by_id["scope-tools"].expected_savings_tokens == 900


def test_infer_model_suggestion_attaches_disable_builtin_action() -> None:
    """Model text about unused Preloop builtins gets the disable action."""
    service = SessionOptimizationService(MagicMock())
    suggestion = RuntimeSessionOptimizationSuggestion(
        id="model-1",
        title="Disable the unused Preloop builtins",
        description="These builtin tools were never called; disable them.",
        expected_savings_tokens=100,
        expected_savings_usd=0.0,
        confidence="high",
        action_label="Apply",
    )
    summary = MagicMock()
    summary.runtime_principal_name = "agent"
    summary.session_source_type = "claude_code"
    action = service._infer_model_suggestion_action(
        suggestion,
        agent_id=str(uuid4()),
        unused_tools=["ext"],
        tier1_builtins=["get_issue", "permission_prompt"],
        top_oversized_field=None,
        summary=summary,
        suggested_budget=3.0,
    )
    assert action is not None
    assert action.type == ACTION_DISABLE_BUILTIN_TOOLS
    assert action.params["tool_names"] == ["get_issue", "permission_prompt"]


def test_replay_accepts_prefixed_builtin_removed_names() -> None:
    """Removing bare get_issue strips mcp__preloop__get_issue from tools."""
    payload = {
        "tools": [
            {"name": "mcp__preloop__get_issue", "description": "x" * 40},
            {"name": "keep_me"},
        ]
    }
    result = compute_input_token_savings(payload, removed_tool_names={"get_issue"})
    assert result.delta_tokens > 0
    # Inverse: prefixed candidate removes bare definition.
    payload_bare = {
        "tools": [
            {"name": "get_issue", "description": "x" * 40},
            {"name": "keep_me"},
        ]
    }
    result2 = compute_input_token_savings(
        payload_bare, removed_tool_names={"mcp__preloop__get_issue"}
    )
    assert result2.delta_tokens > 0


def test_replay_does_not_strip_external_mcp_search_for_bare_search() -> None:
    """Bare search must not remove mcp__github__search."""
    payload = {
        "tools": [
            {"name": "mcp__github__search", "description": "x" * 40},
            {"name": "keep_me"},
        ]
    }
    result = compute_input_token_savings(payload, removed_tool_names={"search"})
    assert result.delta_tokens == 0


def _create_runtime_session(db_session, test_user, *, source_id: str) -> object:
    now = datetime.now(UTC)
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=source_id,
        session_reference=f"claude-session-{source_id}",
        runtime_principal_type="claude_code",
        runtime_principal_id=source_id,
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )


def test_apply_disable_builtin_tools_upserts_and_is_idempotent(
    db_session, test_user
) -> None:
    """Apply creates disabled builtin configs and is idempotent per tool."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-disable-builtins"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    # Pre-create an agent-source row that must never be touched (#132).
    agent_row = crud_tool_configuration.create(
        db_session,
        config_in=ToolConfigurationCreate(
            tool_name="get_issue",
            tool_source="agent",
            account_id=str(account.id),
            is_enabled=True,
        ),
    )
    service = SessionOptimizationService(db_session)
    request = RuntimeSessionOptimizationApplyRequest(
        suggestion_id="disable-builtin-tools",
        suggestion_title="Disable unused builtins",
        action=RuntimeSessionOptimizationActionSpec(
            type=ACTION_DISABLE_BUILTIN_TOOLS,
            params={"tool_names": ["get_issue", "search"]},
        ),
    )
    applied = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=request,
        current_user=test_user,
    )
    assert applied.action_type == ACTION_DISABLE_BUILTIN_TOOLS
    assert isinstance(applied.baseline, dict)
    assert set(applied.result["disabled_tools"]) == {"get_issue", "search"}
    assert applied.result["tool_source"] == "builtin"

    get_issue_cfg = crud_tool_configuration.get_by_tool_name_and_source(
        db_session,
        account_id=str(account.id),
        tool_name="get_issue",
        tool_source="builtin",
    )
    assert get_issue_cfg is not None
    assert get_issue_cfg.is_enabled is False
    db_session.refresh(agent_row)
    assert agent_row.is_enabled is True
    assert agent_row.tool_source == "agent"

    # Handler-level idempotency: already-disabled row is a no-op.
    result = service._apply_disable_builtin_tools(
        account=account, params={"tool_names": ["get_issue"]}
    )
    assert result["disabled_tools"] == ["get_issue"]
    cfg_again = crud_tool_configuration.get_by_tool_name_and_source(
        db_session,
        account_id=str(account.id),
        tool_name="get_issue",
        tool_source="builtin",
    )
    assert cfg_again is not None
    assert cfg_again.is_enabled is False


def test_apply_disable_builtin_tools_rejects_unknown_names(
    db_session, test_user
) -> None:
    """Unknown tool names return 400."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-disable-unknown"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)
    with pytest.raises(HTTPException) as exc_info:
        service.apply_account_session_optimization_action(
            account=account,
            runtime_session_id=str(runtime_session.id),
            request=RuntimeSessionOptimizationApplyRequest(
                suggestion_id="disable-builtin-tools",
                suggestion_title="Disable",
                action=RuntimeSessionOptimizationActionSpec(
                    type=ACTION_DISABLE_BUILTIN_TOOLS,
                    params={"tool_names": ["not_a_real_builtin"]},
                ),
            ),
            current_user=test_user,
        )
    assert exc_info.value.status_code == 400


def test_apply_disable_builtin_tools_duplicate_suggestion_409(
    db_session, test_user
) -> None:
    """Duplicate apply for the same suggestion returns 409."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-disable-dupe"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)
    request = RuntimeSessionOptimizationApplyRequest(
        suggestion_id="disable-builtin-tools",
        suggestion_title="Disable",
        action=RuntimeSessionOptimizationActionSpec(
            type=ACTION_DISABLE_BUILTIN_TOOLS,
            params={"tool_names": ["get_issue"]},
        ),
    )
    service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=request,
        current_user=test_user,
    )
    with pytest.raises(HTTPException) as exc_info:
        service.apply_account_session_optimization_action(
            account=account,
            runtime_session_id=str(runtime_session.id),
            request=request,
            current_user=test_user,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_gateway_hides_disabled_builtin_after_apply(
    db_session, test_user
) -> None:
    """After apply, list_tools hides the builtin and call_tool denies it."""
    from unittest.mock import AsyncMock

    from fastmcp import FastMCP
    from fastmcp.tools.tool import Tool, ToolResult

    from preloop.services.dynamic_fastmcp import DynamicFastMCP

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-disable-gateway"
    )
    db_session.commit()
    service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationApplyRequest(
            suggestion_id="disable-builtin-tools-gw",
            suggestion_title="Disable",
            action=RuntimeSessionOptimizationActionSpec(
                type=ACTION_DISABLE_BUILTIN_TOOLS,
                params={"tool_names": ["get_issue"]},
            ),
        ),
        current_user=test_user,
    )
    disabled_config = crud_tool_configuration.get_by_tool_name_and_source(
        db_session,
        account_id=str(account.id),
        tool_name="get_issue",
        tool_source="builtin",
    )
    assert disabled_config is not None
    assert disabled_config.is_enabled is False

    # Reuse PR #127 dynamic_fastmcp patterns with the row apply just wrote.
    mcp = DynamicFastMCP("preloop-mcp")
    user_context = UserContext(
        user_id=str(test_user.id),
        account_id=str(account.id),
        username=test_user.username,
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=["github"],
    )
    mcp._user_context_provider = lambda: user_context
    default_tools = [
        Tool(name="get_issue", description="Get issue", parameters={}),
        Tool(name="create_issue", description="Create issue", parameters={}),
    ]
    with (
        patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=[],
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
            return_value=[disabled_config],
        ),
        patch(
            "preloop.models.crud.crud_account.get",
            return_value=MagicMock(meta_data={}),
        ),
        patch.object(FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)),
    ):
        mock_get_db.side_effect = lambda: iter([MagicMock()])
        listed = await mcp.list_tools()
    names = {t.name for t in listed}
    assert "get_issue" not in names
    assert "create_issue" in names

    with (
        patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
            return_value=[disabled_config],
        ),
        patch.object(
            mcp.__class__.__bases__[0],
            "call_tool",
            new=AsyncMock(),
            create=True,
        ) as mock_super,
    ):
        mock_get_db.side_effect = lambda: iter([MagicMock()])
        result = await mcp.call_tool("get_issue", {"issue": "ABC-1"})
    mock_super.assert_not_called()
    assert isinstance(result, ToolResult)
    assert "disabled" in result.content[0].text.lower()
