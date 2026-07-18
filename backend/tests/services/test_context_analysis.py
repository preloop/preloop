"""Tests for deterministic runtime-session context analysis."""

import json

from preloop.services.context_analysis import (
    CacheBreakingEvent,
    CacheProfile,
    GatewayCallEvent,
    OversizedOutputField,
    RetryProfile,
    SessionContextProfile,
    ToolBloatProfile,
    ToolSchemaOverheadProfile,
    _content_kind,
    _duplicate_log_tokens,
    _extract_messages,
    analyze_cache_profile,
    analyze_retry_profile,
    analyze_segments,
    analyze_tool_bloat,
    analyze_tool_schema_overhead,
    build_profile_from_events,
    compute_profile_savings,
    estimate_tokens,
)

SYSTEM_PROMPT = "You are a helpful coding agent. " * 20
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {"type": "object", "properties": {"path": {}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unused_search",
            "description": "Search the web for documentation " * 10,
            "parameters": {"type": "object", "properties": {"query": {}}},
        },
    },
]


def _event(
    event_id: str,
    *,
    messages: list[dict],
    tools: list[dict] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    outcome: str = "success",
    status_code: int = 200,
    is_retry: bool = False,
    fingerprint: str | None = None,
    estimated_cost: float = 0.0,
) -> GatewayCallEvent:
    request: dict = {"messages": messages}
    if tools is not None:
        request["tools"] = tools
    return GatewayCallEvent(
        event_id=event_id,
        payload={
            "request": request,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "outcome": outcome,
            "status_code": status_code,
            "is_retry": is_retry,
            "request_fingerprint": fingerprint,
            "estimated_cost": estimated_cost,
        },
    )


def _conversation(user_text: str, *, tool_output: str | None = None) -> list[dict]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Earlier question about the repo"},
        {
            "role": "assistant",
            "content": "Earlier answer",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
    ]
    if tool_output is not None:
        messages.append(
            {"role": "tool", "tool_call_id": "call-1", "content": tool_output}
        )
    messages.append({"role": "user", "content": user_text})
    return messages


class TestSegmentAttribution:
    def test_segments_cover_all_kinds_and_sum_to_total(self) -> None:
        events = [
            _event(
                "e1",
                messages=_conversation("What does main.py do?", tool_output="x" * 800),
                tools=TOOL_DEFINITIONS,
                prompt_tokens=1000,
            )
        ]
        segments = analyze_segments(events)
        kinds = {segment.kind for segment in segments}
        assert {
            "system_prompt",
            "tool_schemas",
            "tool_outputs",
            "conversation_history",
            "user_messages",
        } <= kinds
        total = sum(segment.estimated_tokens for segment in segments)
        assert abs(total - 1000) <= len(segments)
        assert abs(sum(segment.share for segment in segments) - 1.0) < 0.01

    def test_segments_carry_evidence_and_excerpts(self) -> None:
        events = [
            _event(
                "e1",
                messages=_conversation("Question"),
                prompt_tokens=500,
            )
        ]
        segments = analyze_segments(events)
        system = next(s for s in segments if s.kind == "system_prompt")
        assert system.event_ids == ["e1"]
        assert system.sample_excerpt is not None
        assert system.sample_excerpt.startswith("You are a helpful")

    def test_empty_session_yields_no_segments(self) -> None:
        assert analyze_segments([]) == []

    def test_payload_without_messages_uses_preview_fallback(self) -> None:
        event = GatewayCallEvent(
            event_id="e1",
            payload={
                "conversation_preview": {
                    "messages": [
                        {"source": "request", "role": "user", "text": "hello there"},
                        {"source": "response", "role": "assistant", "text": "hi"},
                    ]
                },
                "prompt_tokens": 100,
            },
        )
        segments = analyze_segments([event])
        assert [segment.kind for segment in segments] == ["user_messages"]
        assert segments[0].estimated_tokens == 100


class TestCacheProfile:
    def test_stable_prefix_session_has_no_breaking_events(self) -> None:
        base = _conversation("Question one")
        extended = [*base, {"role": "assistant", "content": "Answer one"}]
        extended.append({"role": "user", "content": "Question two"})
        events = [
            _event("e1", messages=base, tools=TOOL_DEFINITIONS, prompt_tokens=400),
            _event("e2", messages=extended, tools=TOOL_DEFINITIONS, prompt_tokens=500),
        ]
        profile = analyze_cache_profile(events)
        assert profile.prefix_stability == "stable"
        assert profile.cache_breaking_events == []
        assert profile.repeated_prefix_share > 0.5
        assert profile.avg_repeated_prefix_tokens > 0

    def test_system_prompt_mutation_breaks_prefix(self) -> None:
        first = _conversation("Question one")
        second = [
            {"role": "system", "content": "Completely different system prompt"},
            *first[1:],
        ]
        events = [
            _event("e1", messages=first),
            _event("e2", messages=second),
        ]
        profile = analyze_cache_profile(events)
        assert profile.prefix_stability == "unstable"
        assert len(profile.cache_breaking_events) == 1
        event = profile.cache_breaking_events[0]
        assert event.event_id == "e2"
        assert event.diverged_at_message_index == 0
        assert event.reason_hint == "system_prompt_changed"

    def test_tool_schema_change_is_reported(self) -> None:
        messages = _conversation("Question")
        events = [
            _event("e1", messages=messages, tools=TOOL_DEFINITIONS),
            _event("e2", messages=messages, tools=TOOL_DEFINITIONS[:1]),
        ]
        profile = analyze_cache_profile(events)
        assert profile.prefix_stability == "unstable"
        assert profile.cache_breaking_events[0].reason_hint == "tool_schema_changed"

    def test_single_event_session_returns_defaults(self) -> None:
        profile = analyze_cache_profile([_event("e1", messages=_conversation("q"))])
        assert profile.avg_repeated_prefix_tokens == 0
        assert profile.cache_breaking_events == []


class TestRetryProfile:
    def test_clean_session_has_zero_waste(self) -> None:
        events = [
            _event("e1", messages=_conversation("q"), prompt_tokens=100),
        ]
        profile = analyze_retry_profile(events)
        assert profile.failed_requests == 0
        assert profile.wasted_tokens == 0
        assert profile.failure_event_ids == []

    def test_failures_and_retries_attributed(self) -> None:
        events = [
            _event(
                "e1",
                messages=_conversation("q"),
                prompt_tokens=200,
                outcome="error",
                status_code=429,
                fingerprint="fp-1",
                estimated_cost=0.02,
            ),
            _event(
                "e2",
                messages=_conversation("q"),
                prompt_tokens=200,
                completion_tokens=50,
                fingerprint="fp-1",
                estimated_cost=0.03,
            ),
            _event(
                "e3",
                messages=_conversation("other"),
                prompt_tokens=300,
                completion_tokens=80,
                estimated_cost=0.05,
            ),
        ]
        profile = analyze_retry_profile(events)
        assert profile.failed_requests == 1
        assert profile.retry_requests == 1
        assert profile.failure_event_ids == ["e1"]
        # failed call total (200) + retry prompt tokens (200)
        assert profile.wasted_tokens == 400
        assert profile.wasted_cost_estimate == 0.05

    def test_explicit_retry_flag_counts(self) -> None:
        events = [
            _event("e1", messages=_conversation("q"), prompt_tokens=100),
            _event(
                "e2",
                messages=_conversation("q"),
                prompt_tokens=100,
                is_retry=True,
            ),
        ]
        profile = analyze_retry_profile(events)
        assert profile.retry_requests == 1
        assert profile.wasted_tokens == 100


class TestToolBloat:
    def test_duplicate_tool_outputs_detected(self) -> None:
        file_content = "def main():\n    pass\n" * 50
        messages = [
            {"role": "system", "content": "agent"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "read_file", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": file_content},
            {"role": "tool", "tool_call_id": "c2", "content": file_content},
            {"role": "user", "content": "now what"},
        ]
        profile = analyze_tool_bloat([_event("e1", messages=messages)])
        assert profile.duplicate_output_tokens == estimate_tokens(file_content)
        assert profile.duplicate_event_ids == ["e1"]
        assert profile.compressible_tokens_estimate >= profile.duplicate_output_tokens
        assert profile.largest_outputs[0].tool_name == "read_file"

    def test_homogeneous_json_array_is_compressible(self) -> None:
        rows = [{"id": i, "name": f"row-{i}", "status": "active"} for i in range(100)]
        messages = [
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps(rows)},
            {"role": "user", "content": "summarize"},
        ]
        profile = analyze_tool_bloat([_event("e1", messages=messages)])
        assert profile.compressible_tokens_estimate > 0
        assert profile.largest_outputs[0].content_kind == "json"

    def test_repeated_log_lines_are_compressible(self) -> None:
        log = "\n".join(["INFO connection pool exhausted"] * 40)
        messages = [
            {"role": "tool", "tool_call_id": "c1", "content": log},
            {"role": "user", "content": "fix it"},
        ]
        profile = analyze_tool_bloat([_event("e1", messages=messages)])
        assert profile.compressible_tokens_estimate > 0
        assert profile.largest_outputs[0].content_kind == "log"

    def test_multiline_text_is_not_misclassified_as_log(self) -> None:
        paragraph = "\n".join(
            f"Paragraph line {index} with prose." for index in range(12)
        )
        assert _content_kind(paragraph) == "text"

    def test_lean_session_reports_no_bloat(self) -> None:
        profile = analyze_tool_bloat(
            [_event("e1", messages=_conversation("q", tool_output="short output"))]
        )
        assert profile.duplicate_output_tokens == 0
        assert profile.compressible_tokens_estimate == 0


class TestToolSchemaOverhead:
    def test_unused_tools_reported_with_resend_awareness(self) -> None:
        messages = _conversation("q")
        events = [
            _event("e1", messages=messages, tools=TOOL_DEFINITIONS),
            _event("e2", messages=messages, tools=TOOL_DEFINITIONS),
        ]
        profile = analyze_tool_schema_overhead(events)
        assert profile.advertised_tools == 2
        assert profile.invoked_tools == 1
        assert profile.unused_tool_names == ["unused_search"]
        assert profile.resend_count == 2
        assert profile.unused_schema_tokens_estimate > 0
        assert profile.schema_tokens_estimate > profile.unused_schema_tokens_estimate

    def test_lean_session_has_no_overhead(self) -> None:
        profile = analyze_tool_schema_overhead(
            [_event("e1", messages=_conversation("q"))]
        )
        assert profile.advertised_tools == 0
        assert profile.unused_tool_names == []
        assert profile.resend_count == 0


class TestProfileComposition:
    def test_build_profile_from_events_fills_all_sections(self) -> None:
        events = [
            _event(
                "e1",
                messages=_conversation("q", tool_output="data " * 200),
                tools=TOOL_DEFINITIONS,
                prompt_tokens=900,
                completion_tokens=100,
            )
        ]
        profile = build_profile_from_events("session-1", events)
        assert profile.session_id == "session-1"
        assert profile.analyzed_event_count == 1
        assert profile.total_prompt_tokens == 900
        assert profile.total_completion_tokens == 100
        assert profile.segments
        assert profile.cache_profile is not None
        assert profile.retry_profile is not None
        assert profile.tool_bloat is not None
        assert profile.tool_schema_overhead is not None


class TestMessageExtraction:
    def test_extract_messages_returns_empty_when_payload_missing(self) -> None:
        assert _extract_messages({}) == []
        assert _extract_messages({"request": {}}) == []
        assert _extract_messages({"request": {"messages": "bad"}}) == []

    def test_extract_messages_ignores_malformed_preview_entries(self) -> None:
        payload = {
            "conversation_preview": {
                "messages": [
                    "bad",
                    {"source": "response", "role": "assistant", "text": "skip"},
                    {"source": "request", "role": "user", "text": "keep me"},
                ]
            }
        }
        messages = _extract_messages(payload)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "keep me"


class TestDuplicateLogTokens:
    def test_duplicate_log_tokens_uses_estimate_tokens(self) -> None:
        line = "ERROR timeout while connecting"
        log = "\n".join([line] * 6)
        assert _duplicate_log_tokens(log) == estimate_tokens(line) * 5


class TestBuildFromDatabase:
    def test_build_session_context_profile_reads_stored_payloads(
        self, db_session, test_user
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from preloop.services.context_analysis import (
            build_session_context_profile,
        )
        from preloop.models.crud import (
            crud_account,
            crud_runtime_session,
            crud_runtime_session_activity,
        )

        now = datetime.now(UTC)
        runtime_session = crud_runtime_session.upsert_by_source(
            db_session,
            account_id=test_user.account_id,
            session_source_type="claude_code",
            session_source_id="ctx-analysis",
            session_reference="ctx-analysis",
            runtime_principal_type="claude_code",
            runtime_principal_id="ctx-analysis",
            runtime_principal_name="Claude Workspace",
            started_at=now,
            last_activity_at=now,
        )
        db_session.commit()
        for index, event in enumerate(
            [
                _event(
                    "ignored",
                    messages=_conversation("first question"),
                    tools=TOOL_DEFINITIONS,
                    prompt_tokens=400,
                ),
                _event(
                    "ignored",
                    messages=_conversation("second question"),
                    prompt_tokens=500,
                    outcome="error",
                    status_code=500,
                ),
            ]
        ):
            crud_runtime_session_activity.log_model_gateway_call(
                db_session,
                account_id=test_user.account_id,
                runtime_session_id=runtime_session.id,
                status="success",
                metadata=event.payload,
                timestamp=now + timedelta(seconds=index),
            )
        account = crud_account.get(db_session, id=test_user.account_id)
        assert account is not None

        profile = build_session_context_profile(
            db_session,
            account=account,
            runtime_session_id=str(runtime_session.id),
        )

        assert profile.analyzed_event_count == 2
        assert profile.total_prompt_tokens == 900
        assert profile.retry_profile is not None
        assert profile.retry_profile.failed_requests == 1
        system = next(s for s in profile.segments if s.kind == "system_prompt")
        assert len(system.event_ids) == 2


class TestProfileSavingsDedupe:
    """Profile-level savings roll-up must not double-count overlapping waste."""

    @staticmethod
    def _profile(
        *,
        prompt_tokens: int = 100_000,
        completion_tokens: int = 0,
        schema: object = None,
        bloat: object = None,
        retry: object = None,
        cache: object = None,
    ) -> SessionContextProfile:
        return SessionContextProfile(
            session_id="s1",
            analyzed_event_count=3,
            total_prompt_tokens=prompt_tokens,
            total_completion_tokens=completion_tokens,
            tool_schema_overhead=schema,
            tool_bloat=bloat,
            retry_profile=retry,
            cache_profile=cache,
        )

    def test_schema_overhead_is_not_resend_multiplied_again(self) -> None:
        """``unused_schema_tokens_estimate`` already sums every resend."""
        profile = self._profile(
            schema=ToolSchemaOverheadProfile(
                advertised_tools=10,
                invoked_tools=2,
                unused_tool_names=["a", "b"],
                schema_tokens_estimate=30_000,
                unused_schema_tokens_estimate=24_000,
                resend_count=3,
            )
        )
        breakdown = compute_profile_savings(profile)
        assert breakdown.schema_overhead_tokens == 24_000
        assert breakdown.total_tokens == 24_000

    def test_oversized_fields_and_compressible_counted_once(self) -> None:
        """Both measure the same tool-output bytes, so take the larger."""
        profile = self._profile(
            bloat=ToolBloatProfile(
                duplicate_output_tokens=2_000,
                compressible_tokens_estimate=5_000,
                oversized_output_fields=[
                    OversizedOutputField(
                        tool_name="search", field_names=["raw"], estimated_tokens=4_000
                    )
                ],
            )
        )
        breakdown = compute_profile_savings(profile)
        assert breakdown.tool_output_tokens == 5_000
        assert breakdown.total_tokens == 5_000

    def test_cache_prefix_overlap_does_not_stack(self) -> None:
        """Repeated prefix re-measures schema + tool-output bytes."""
        profile = self._profile(
            schema=ToolSchemaOverheadProfile(
                unused_schema_tokens_estimate=10_000, resend_count=4
            ),
            bloat=ToolBloatProfile(compressible_tokens_estimate=5_000),
            cache=CacheProfile(
                avg_repeated_prefix_tokens=4_000,
                cache_breaking_events=[
                    CacheBreakingEvent(
                        event_id="e1",
                        diverged_at_message_index=1,
                        reason_hint="system drift",
                    ),
                    CacheBreakingEvent(
                        event_id="e2",
                        diverged_at_message_index=2,
                        reason_hint="system drift",
                    ),
                ],
            ),
        )
        breakdown = compute_profile_savings(profile)
        # max(10_000 + 5_000, 8_000) rather than 23_000.
        assert breakdown.total_tokens == 15_000

    def test_retry_waste_is_additive(self) -> None:
        """Retry spend is separate from the surviving prompt context."""
        profile = self._profile(
            schema=ToolSchemaOverheadProfile(
                unused_schema_tokens_estimate=1_000, resend_count=2
            ),
            retry=RetryProfile(failed_requests=1, wasted_tokens=500),
        )
        breakdown = compute_profile_savings(profile)
        assert breakdown.retry_waste_tokens == 500
        assert breakdown.total_tokens == 1_500

    def test_total_never_exceeds_analyzed_scope(self) -> None:
        """The invariant: savings <= the scope they were measured against."""
        profile = self._profile(
            prompt_tokens=1_000,
            completion_tokens=0,
            schema=ToolSchemaOverheadProfile(
                unused_schema_tokens_estimate=9_000, resend_count=3
            ),
        )
        breakdown = compute_profile_savings(profile)
        assert breakdown.total_tokens == 1_000
        assert breakdown.clamped is True
        assert breakdown.scope_tokens == 1_000

    def test_observed_131_percent_regression(self) -> None:
        """Regression for the observed 80,921 saved / 61,625 analyzed (131%).

        The reported figure was ``unused_schema_tokens_estimate`` (already the
        sum across 3 resends) multiplied by ``resend_count`` a second time:
        24,000 * 3 = 72,000, plus 4,921 retry waste and 4,000 compressible
        tool-output tokens => 80,921 against a 61,625-token scope.
        """
        profile = self._profile(
            prompt_tokens=58_000,
            completion_tokens=3_625,
            schema=ToolSchemaOverheadProfile(
                advertised_tools=18,
                invoked_tools=4,
                unused_tool_names=["a", "b", "c"],
                schema_tokens_estimate=31_000,
                unused_schema_tokens_estimate=24_000,
                resend_count=3,
            ),
            bloat=ToolBloatProfile(compressible_tokens_estimate=4_000),
            retry=RetryProfile(failed_requests=2, wasted_tokens=4_921),
        )
        buggy_total = (
            profile.tool_schema_overhead.unused_schema_tokens_estimate
            * profile.tool_schema_overhead.resend_count
            + profile.tool_bloat.compressible_tokens_estimate
            + profile.retry_profile.wasted_tokens
        )
        assert buggy_total == 80_921
        assert profile.total_prompt_tokens + profile.total_completion_tokens == 61_625

        breakdown = compute_profile_savings(profile)
        assert breakdown.total_tokens == 32_921
        assert breakdown.scope_tokens == 61_625
        assert breakdown.clamped is False
        assert breakdown.total_tokens < breakdown.scope_tokens
