"""Tests for deterministic gateway-side context optimization."""

from preloop.services.context_optimization import (
    CONTEXT_OPTIMIZATION_KEY,
    MIN_TOOL_RESULT_CAP,
    ContextOptimizationSettings,
    estimate_tokens,
    optimize_messages,
    resolve_context_optimization_settings,
    sanitize_tool_choice,
    strip_disabled_tools,
    strip_noise_text,
    subject_governance_affects_gateway_context,
    tool_choice_named_tool,
)
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    SUBJECT_TYPE_MANAGED_AGENTS,
    set_subject_governance,
)

LONG_TEXT = "\n".join(f"unique log line {i} with details" for i in range(100))


def _tool_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class TestOptimizeMessages:
    def test_disabled_settings_are_noop(self) -> None:
        messages = [_tool_msg("a", LONG_TEXT), _tool_msg("b", LONG_TEXT)]
        out, stats = optimize_messages(messages, ContextOptimizationSettings())
        assert out is messages
        assert not stats.changed

    def test_dedupe_keeps_first_occurrence_and_stubs_repeat(self) -> None:
        settings = ContextOptimizationSettings(dedupe_tool_results=True)
        messages = [
            {"role": "user", "content": "hi"},
            _tool_msg("a", LONG_TEXT),
            _tool_msg("b", LONG_TEXT),
        ]
        out, stats = optimize_messages(messages, settings)
        assert out[1]["content"] == LONG_TEXT
        assert "identical to tool result in message 2" in out[2]["content"]
        assert stats.deduped_results == 1
        assert stats.deduped_chars > 0
        assert stats.estimated_tokens_saved > 0
        # Input untouched.
        assert messages[2]["content"] == LONG_TEXT

    def test_dedupe_skips_short_results(self) -> None:
        settings = ContextOptimizationSettings(dedupe_tool_results=True)
        messages = [_tool_msg("a", "ok"), _tool_msg("b", "ok")]
        out, stats = optimize_messages(messages, settings)
        assert out[1]["content"] == "ok"
        assert stats.deduped_results == 0

    def test_dedupe_ignores_whitespace_differences(self) -> None:
        settings = ContextOptimizationSettings(dedupe_tool_results=True)
        messages = [
            _tool_msg("a", LONG_TEXT),
            _tool_msg("b", LONG_TEXT.replace("\n", "  \n")),
        ]
        _, stats = optimize_messages(messages, settings)
        assert stats.deduped_results == 1

    def test_cap_truncates_head_and_tail(self) -> None:
        settings = ContextOptimizationSettings(max_tool_result_chars=1_000)
        messages = [_tool_msg("a", LONG_TEXT)]
        out, stats = optimize_messages(messages, settings)
        content = out[0]["content"]
        assert "removed by Preloop gateway tool-result cap" in content
        assert content.startswith("unique log line 0")
        assert content.endswith("unique log line 99 with details")
        assert stats.truncated_results == 1
        assert stats.truncated_chars > 0

    def test_noise_stripping_counts_savings(self) -> None:
        settings = ContextOptimizationSettings(strip_noise=True)
        noisy = "\x1b[31mred\x1b[0m\n" + ("progress 10%\rprogress 100%\n" * 5)
        messages = [_tool_msg("a", noisy)]
        out, stats = optimize_messages(messages, settings)
        assert "\x1b" not in out[0]["content"]
        assert "progress 100%" in out[0]["content"]
        assert "progress 10%" not in out[0]["content"]
        assert stats.noise_chars > 0

    def test_non_tool_messages_untouched(self) -> None:
        settings = ContextOptimizationSettings(
            dedupe_tool_results=True, strip_noise=True, max_tool_result_chars=1_000
        )
        messages = [
            {"role": "system", "content": LONG_TEXT},
            {"role": "user", "content": LONG_TEXT},
            {"role": "assistant", "content": LONG_TEXT},
        ]
        out, stats = optimize_messages(messages, settings)
        assert out == messages
        assert not stats.changed

    def test_content_part_lists_are_flattened(self) -> None:
        settings = ContextOptimizationSettings(dedupe_tool_results=True)
        parts = [{"type": "text", "text": LONG_TEXT}]
        messages = [_tool_msg("a", LONG_TEXT), {**_tool_msg("b", ""), "content": parts}]
        _, stats = optimize_messages(messages, settings)
        assert stats.deduped_results == 1


class TestStripNoiseText:
    def test_collapses_repeated_lines(self) -> None:
        text = "same line\n" * 10 + "tail"
        out = strip_noise_text(text)
        assert out.count("same line") == 1
        assert "[previous line repeated 9 more times]" in out
        assert out.endswith("tail")

    def test_keeps_unique_lines(self) -> None:
        text = "one\ntwo\nthree"
        assert strip_noise_text(text) == text


class TestStripDisabledTools:
    def _meta(self) -> dict:
        return set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id="agent-1",
            config={"tool_enabled_overrides": {"web_search": False}},
        )

    def test_removes_disabled_openai_tool(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]
        kept, removed = strip_disabled_tools(
            tools,
            meta_data=self._meta(),
            subject_context={"managed_agent_id": "agent-1", "api_key_id": None},
        )
        assert removed == ["web_search"]
        assert [t["function"]["name"] for t in kept] == ["read_file"]

    def test_removes_disabled_anthropic_tool(self) -> None:
        tools = [{"name": "web_search"}, {"name": "read_file"}]
        kept, removed = strip_disabled_tools(
            tools,
            meta_data=self._meta(),
            subject_context={"managed_agent_id": "agent-1", "api_key_id": None},
        )
        assert removed == ["web_search"]
        assert [t["name"] for t in kept] == ["read_file"]

    def test_unscoped_subject_keeps_all_tools(self) -> None:
        tools = [{"name": "web_search"}]
        kept, removed = strip_disabled_tools(
            tools,
            meta_data=self._meta(),
            subject_context={"managed_agent_id": "other", "api_key_id": None},
        )
        assert removed == []
        assert kept == tools


class TestResolveSettings:
    def test_unconfigured_returns_disabled(self) -> None:
        settings = resolve_context_optimization_settings(
            {}, subject_context={"managed_agent_id": "agent-1", "api_key_id": None}
        )
        assert not settings.enabled

    def test_agent_scope_settings_resolve(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id="agent-1",
            config={
                CONTEXT_OPTIMIZATION_KEY: {
                    "dedupe_tool_results": True,
                    "strip_noise": True,
                    "max_tool_result_chars": 9_000,
                }
            },
        )
        settings = resolve_context_optimization_settings(
            meta, subject_context={"managed_agent_id": "agent-1", "api_key_id": None}
        )
        assert settings.dedupe_tool_results is True
        assert settings.strip_noise is True
        assert settings.max_tool_result_chars == 9_000

    def test_api_key_scope_wins_over_agent_scope(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id="agent-1",
            config={CONTEXT_OPTIMIZATION_KEY: {"dedupe_tool_results": True}},
        )
        meta = set_subject_governance(
            meta,
            subject_type=SUBJECT_TYPE_API_KEYS,
            subject_id="key-1",
            config={CONTEXT_OPTIMIZATION_KEY: {"dedupe_tool_results": False}},
        )
        settings = resolve_context_optimization_settings(
            meta,
            subject_context={"managed_agent_id": "agent-1", "api_key_id": "key-1"},
        )
        assert settings.dedupe_tool_results is False

    def test_cap_is_floored(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id="agent-1",
            config={CONTEXT_OPTIMIZATION_KEY: {"max_tool_result_chars": 10}},
        )
        settings = resolve_context_optimization_settings(
            meta, subject_context={"managed_agent_id": "agent-1", "api_key_id": None}
        )
        assert settings.max_tool_result_chars == MIN_TOOL_RESULT_CAP


class TestEstimateTokens:
    def test_empty_text_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_four_chars_is_one_token(self) -> None:
        assert estimate_tokens("abcd") == 1

    def test_floors_to_at_least_one_for_short_text(self) -> None:
        # 1-3 chars are non-empty but floor-divide to 0; max(1, ...) applies.
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abc") == 1

    def test_long_text_is_chars_over_four(self) -> None:
        text = "x" * 4000
        assert estimate_tokens(text) == len(text) // 4 == 1000


class TestToolChoiceNamedTool:
    """Unit tests for ``tool_choice_named_tool`` across provider shapes."""

    def test_string_choices_name_no_tool(self) -> None:
        for value in ("auto", "none", "required", "any"):
            assert tool_choice_named_tool(value) is None

    def test_openai_function_shape(self) -> None:
        choice = {"type": "function", "function": {"name": "search"}}
        assert tool_choice_named_tool(choice) == "search"

    def test_anthropic_tool_shape(self) -> None:
        choice = {"type": "tool", "name": "search"}
        assert tool_choice_named_tool(choice) == "search"

    def test_malformed_object_names_no_tool(self) -> None:
        assert tool_choice_named_tool({"type": "function"}) is None
        assert tool_choice_named_tool({"type": "tool"}) is None
        assert tool_choice_named_tool({}) is None
        assert tool_choice_named_tool(None) is None


class TestSanitizeToolChoice:
    """Unit tests for ``sanitize_tool_choice`` during partial tool strips."""

    def test_no_removed_tools_is_noop(self) -> None:
        choice = {"type": "function", "function": {"name": "search"}}
        result, changed = sanitize_tool_choice(choice, removed_tool_names=set())
        assert result is choice
        assert changed is False

    def test_string_choices_pass_through(self) -> None:
        for value in ("auto", "none", "required", "any"):
            result, changed = sanitize_tool_choice(value, removed_tool_names={"search"})
            assert result == value
            assert changed is False

    def test_kept_tool_passes_through_openai(self) -> None:
        choice = {"type": "function", "function": {"name": "kept"}}
        result, changed = sanitize_tool_choice(choice, removed_tool_names={"stripped"})
        assert result is choice
        assert changed is False

    def test_kept_tool_passes_through_anthropic(self) -> None:
        choice = {"type": "tool", "name": "kept"}
        result, changed = sanitize_tool_choice(choice, removed_tool_names={"stripped"})
        assert result is choice
        assert changed is False

    def test_stripped_tool_falls_back_to_auto_openai(self) -> None:
        choice = {"type": "function", "function": {"name": "stripped"}}
        result, changed = sanitize_tool_choice(choice, removed_tool_names={"stripped"})
        assert result == "auto"
        assert changed is True

    def test_stripped_tool_falls_back_to_auto_anthropic(self) -> None:
        choice = {"type": "tool", "name": "stripped"}
        result, changed = sanitize_tool_choice(choice, removed_tool_names={"stripped"})
        assert result == "auto"
        assert changed is True


class TestSubjectGovernanceAffectsGatewayContext:
    def test_empty_store_does_not_affect(self) -> None:
        subject_context = {"api_key_id": "key-1", "managed_agent_id": None}
        assert (
            subject_governance_affects_gateway_context(
                {},
                subject_context=subject_context,
                has_tools=True,
            )
            is False
        )

    def test_allowed_models_only_does_not_affect(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_API_KEYS,
            subject_id="key-1",
            config={"allowed_models": ["gpt-4"]},
        )
        subject_context = {"api_key_id": "key-1", "managed_agent_id": None}
        assert (
            subject_governance_affects_gateway_context(
                meta,
                subject_context=subject_context,
                has_tools=True,
            )
            is False
        )

    def test_tool_override_affects_when_tools_present(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_API_KEYS,
            subject_id="key-1",
            config={"tool_enabled_overrides": {"search": False}},
        )
        subject_context = {"api_key_id": "key-1", "managed_agent_id": None}
        assert (
            subject_governance_affects_gateway_context(
                meta,
                subject_context=subject_context,
                has_tools=True,
            )
            is True
        )
        assert (
            subject_governance_affects_gateway_context(
                meta,
                subject_context=subject_context,
                has_tools=False,
            )
            is False
        )

    def test_context_optimization_settings_affect(self) -> None:
        meta = set_subject_governance(
            {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id="agent-1",
            config={
                CONTEXT_OPTIMIZATION_KEY: {"dedupe_tool_results": True},
            },
        )
        subject_context = {
            "api_key_id": "key-1",
            "managed_agent_id": "agent-1",
        }
        assert (
            subject_governance_affects_gateway_context(
                meta,
                subject_context=subject_context,
                has_tools=False,
            )
            is True
        )


class TestAccountGovernanceCache:
    def test_empty_store_is_cached_negative(self, db_session, test_user) -> None:
        from unittest.mock import patch

        from preloop.models.crud import crud_account
        from preloop.services.account_governance_cache import (
            clear_account_governance_cache,
            get_cached_account_meta_data,
        )

        clear_account_governance_cache()
        account_id = str(test_user.account_id)
        with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
            assert get_cached_account_meta_data(db_session, account_id) is None
            assert get_cached_account_meta_data(db_session, account_id) is None
            assert get_mock.call_count == 1

    def test_invalidation_forces_refresh(self, db_session, test_user) -> None:
        from unittest.mock import patch

        from preloop.models.crud import crud_account
        from preloop.services.account_governance_cache import (
            clear_account_governance_cache,
            get_cached_account_meta_data,
            invalidate_account_governance_cache,
        )

        clear_account_governance_cache()
        account_id = str(test_user.account_id)
        account = crud_account.get(db_session, id=test_user.account_id)
        account.meta_data = set_subject_governance(
            account.meta_data or {},
            subject_type=SUBJECT_TYPE_API_KEYS,
            subject_id="key-cache",
            config={CONTEXT_OPTIMIZATION_KEY: {"strip_noise": True}},
        )
        db_session.add(account)
        db_session.commit()

        with patch.object(crud_account, "get", wraps=crud_account.get) as get_mock:
            assert get_cached_account_meta_data(db_session, account_id) is not None
            assert get_cached_account_meta_data(db_session, account_id) is not None
            assert get_mock.call_count == 1
            invalidate_account_governance_cache(account_id)
            assert get_cached_account_meta_data(db_session, account_id) is not None
            assert get_mock.call_count == 2
