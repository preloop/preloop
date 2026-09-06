"""Unit tests for the runtime session explorer service."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.services import runtime_session_explorer as rse_mod
from preloop.services.runtime_session_explorer import (
    INTERACTION_SUMMARY_ATTEMPT_TIMEOUT_SECONDS,
    MAX_TITLES_PER_LIST_REQUEST,
    TITLE_REFRESH_REQUEST_DELTA,
    RuntimeSessionExplorerService,
)


def _make_summary_row(**overrides):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    row = {
        "id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "session_source_type": "managed_agent",
        "session_source_id": "src-1",
        "session_reference": "ref-1",
        "runtime_principal_type": "user",
        "runtime_principal_id": "p-1",
        "runtime_principal_name": "Alice",
        "title": "My session",
        "summary": None,
        "summary_updated_at": None,
        "started_at": now,
        "last_activity_at": now,
        "ended_at": None,
        "flow_id": None,
        "flow_name": None,
        "flow_execution_id": None,
        "latest_model_alias": "gpt-5",
        "latest_provider_name": "openai",
        "is_active_now": False,
        "activity_status": "idle",
        "total_requests": 3,
        "successful_requests": 3,
        "failed_requests": 0,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "estimated_cost": 0.0123,
        "last_request_at": now,
    }
    row.update(overrides)
    return row


def _make_account():
    account = MagicMock()
    account.id = uuid.uuid4()
    return account


@pytest.fixture
def service():
    return RuntimeSessionExplorerService(db=MagicMock())


# --- _normalize_period -----------------------------------------------------


def test_normalize_period_defaults_to_last_30_days():
    start, end = RuntimeSessionExplorerService._normalize_period(None, None)
    assert end.tzinfo == timezone.utc
    assert (end - start).days == 30


def test_normalize_period_adds_utc_to_naive_datetimes():
    naive_start = datetime(2026, 1, 1, 0, 0)
    naive_end = datetime(2026, 1, 10, 0, 0)
    start, end = RuntimeSessionExplorerService._normalize_period(naive_start, naive_end)
    assert start.tzinfo == timezone.utc
    assert end.tzinfo == timezone.utc


# --- _summary_row_to_schema ------------------------------------------------


def test_summary_row_to_schema_maps_token_usage():
    summary = RuntimeSessionExplorerService._summary_row_to_schema(_make_summary_row())
    assert summary.token_usage.prompt_tokens == 100
    assert summary.token_usage.completion_tokens == 50
    assert summary.token_usage.total_tokens == 150
    assert summary.estimated_cost == pytest.approx(0.0123)
    assert summary.latest_model_alias == "gpt-5"


# --- _message_content_to_text ----------------------------------------------


def test_message_content_to_text_none():
    assert RuntimeSessionExplorerService._message_content_to_text(None) == ""


def test_message_content_to_text_plain_string():
    assert RuntimeSessionExplorerService._message_content_to_text("  hi  ") == "hi"


def test_message_content_to_text_list_of_strings_and_dicts():
    value = ["one", {"text": "two"}, {"content": "three"}, 42]
    result = RuntimeSessionExplorerService._message_content_to_text(value)
    assert "one" in result and "two" in result and "three" in result


def test_message_content_to_text_other_type():
    assert RuntimeSessionExplorerService._message_content_to_text(42) == "42"


# --- _extract_request_messages ---------------------------------------------


def test_extract_request_messages_from_request_messages():
    payload = {
        "request": {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
            ]
        }
    }
    messages = RuntimeSessionExplorerService._extract_request_messages(payload)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["text"] == "hello"


def test_extract_request_messages_falls_back_to_conversation_preview():
    payload = {
        "request": {},
        "conversation_preview": {
            "messages": [
                {"source": "user", "text": "preview text"},
                {"role": "assistant", "text": ""},  # empty, skipped
            ]
        },
    }
    messages = RuntimeSessionExplorerService._extract_request_messages(payload)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["text"] == "preview text"


def test_extract_request_messages_empty_payload():
    assert RuntimeSessionExplorerService._extract_request_messages({}) == []


# --- _local_interaction_summary --------------------------------------------


def test_local_interaction_summary_low_risk():
    payload = {
        "model_alias": "gpt-5",
        "endpoint_kind": "responses",
        "outcome": "success",
        "status_code": 200,
        "total_tokens": 10,
        "request": {"messages": [{"role": "user", "content": "do the thing"}]},
    }
    summary = RuntimeSessionExplorerService._local_interaction_summary(
        event_id="e1", payload=payload
    )
    assert summary.risk_level == "low"
    assert summary.generated_by == "local"
    assert any("do the thing" in p for p in summary.key_points)
    assert summary.next_action is None


def test_local_interaction_summary_high_risk_on_error():
    payload = {
        "model_alias": "gpt-5",
        "outcome": "error",
        "status_code": 500,
    }
    summary = RuntimeSessionExplorerService._local_interaction_summary(
        event_id="e2", payload=payload
    )
    assert summary.risk_level == "high"
    assert summary.next_action == "Inspect raw payload."


# --- _to_litellm_model -----------------------------------------------------


def test_to_litellm_model_maps_provider_prefix():
    model = MagicMock()
    model.provider_name = "google"
    model.model_identifier = "gemini-2.0"
    assert RuntimeSessionExplorerService._to_litellm_model(model) == "gemini/gemini-2.0"


def test_to_litellm_model_keeps_slashed_identifier():
    model = MagicMock()
    model.provider_name = "openai"
    model.model_identifier = "openai/gpt-5"
    assert RuntimeSessionExplorerService._to_litellm_model(model) == "openai/gpt-5"


def test_to_litellm_model_unknown_provider_passthrough():
    model = MagicMock()
    model.provider_name = "customcorp"
    model.model_identifier = "model-x"
    model.api_endpoint = ""
    assert (
        RuntimeSessionExplorerService._to_litellm_model(model) == "customcorp/model-x"
    )


def test_to_litellm_model_unknown_provider_with_endpoint_is_openai_compatible():
    # Unknown providers that carry their own endpoint (imported custom
    # OpenAI-compatible providers) route via litellm's generic openai adapter.
    model = MagicMock()
    model.provider_name = "customcorp"
    model.model_identifier = "model-x"
    model.api_endpoint = "https://api.customcorp.example/v1"
    assert RuntimeSessionExplorerService._to_litellm_model(model) == "openai/model-x"


# --- timestamp / scalar parsers --------------------------------------------


def test_parse_timestamp_handles_z_suffix():
    result = RuntimeSessionExplorerService._parse_timestamp("2026-01-01T00:00:00Z")
    assert result.tzinfo is not None


def test_parse_timestamp_invalid_returns_none():
    assert RuntimeSessionExplorerService._parse_timestamp("not-a-date") is None
    assert RuntimeSessionExplorerService._parse_timestamp(None) is None


def test_normalize_timestamp_adds_utc():
    naive = datetime(2026, 1, 1)
    assert RuntimeSessionExplorerService._normalize_timestamp(naive).tzinfo == (
        timezone.utc
    )


def test_parse_optional_int_and_float():
    assert RuntimeSessionExplorerService._parse_optional_int("5") == 5
    assert RuntimeSessionExplorerService._parse_optional_int("") is None
    assert RuntimeSessionExplorerService._parse_optional_int("x") is None
    assert RuntimeSessionExplorerService._parse_optional_float("1.5") == 1.5
    assert RuntimeSessionExplorerService._parse_optional_float(None) is None


def test_parse_bool_variants():
    assert RuntimeSessionExplorerService._parse_bool(True) is True
    assert RuntimeSessionExplorerService._parse_bool("true") is True
    assert RuntimeSessionExplorerService._parse_bool("FALSE") is False
    assert RuntimeSessionExplorerService._parse_bool(1) is True


# --- _session_needs_title --------------------------------------------------


def test_session_needs_title_when_missing():
    assert RuntimeSessionExplorerService._session_needs_title({"title": ""}) is True


def test_session_needs_title_when_request_delta_exceeded():
    row = {
        "title": "t",
        "total_requests": TITLE_REFRESH_REQUEST_DELTA + 1,
        "title_request_count": 0,
    }
    assert RuntimeSessionExplorerService._session_needs_title(row) is True


def test_session_does_not_need_title_when_fresh():
    row = {"title": "t", "total_requests": 2, "title_request_count": 1}
    assert RuntimeSessionExplorerService._session_needs_title(row) is False


# --- schedule_missing_session_titles ---------------------------------------


def test_schedule_titles_noop_without_background_tasks(service):
    # No background_tasks means the hot path makes no scheduling call.
    service.schedule_missing_session_titles(
        account_id="a", rows=[{"id": "1", "title": ""}], background_tasks=None
    )


def test_schedule_titles_adds_bounded_task(service):
    rows = [
        {"id": str(i), "title": "", "total_requests": 0, "title_request_count": 0}
        for i in range(MAX_TITLES_PER_LIST_REQUEST + 5)
    ]
    bg = MagicMock()
    service.schedule_missing_session_titles(
        account_id="acct", rows=rows, background_tasks=bg
    )
    bg.add_task.assert_called_once()
    kwargs = bg.add_task.call_args.kwargs
    assert len(kwargs["session_ids"]) == MAX_TITLES_PER_LIST_REQUEST


def test_schedule_titles_skips_when_all_titled(service):
    rows = [{"id": "1", "title": "t", "total_requests": 1, "title_request_count": 1}]
    bg = MagicMock()
    service.schedule_missing_session_titles(
        account_id="acct", rows=rows, background_tasks=bg
    )
    bg.add_task.assert_not_called()


# --- _run_title_generation -------------------------------------------------


def test_run_title_generation_noop_when_service_unavailable():
    # Plugin manager raising must be swallowed.
    with patch("preloop.plugins.base.get_plugin_manager", side_effect=Exception):
        RuntimeSessionExplorerService._run_title_generation(
            account_id="a", session_ids=["s1"]
        )


def test_run_title_generation_calls_service_and_closes_db():
    generate = MagicMock()
    pm = MagicMock()
    pm.get_service.return_value = generate
    fake_db = MagicMock()
    factory = MagicMock(return_value=fake_db)
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=pm),
        patch("preloop.models.db.session.get_session_factory", return_value=factory),
    ):
        RuntimeSessionExplorerService._run_title_generation(
            account_id="acct", session_ids=["s1", "s2"]
        )
    assert generate.call_count == 2
    fake_db.close.assert_called_once()


# --- _attach_optimization_badges -------------------------------------------


def test_attach_optimization_badges_sets_scores(service):
    account = _make_account()
    item = RuntimeSessionExplorerService._summary_row_to_schema(_make_summary_row())
    cached_row = MagicMock()
    cached_row.runtime_session_id = item.id
    cached_row.response = {
        "waste_score": 42,
        "potential_savings_tokens": 1000,
        "potential_savings_usd": 0.5,
    }
    with patch.object(
        rse_mod.crud_runtime_session_optimization_result,
        "list_for_sessions",
        return_value=[cached_row],
    ):
        service._attach_optimization_badges(account=account, items=[item])
    assert item.optimization_waste_score == 42
    assert item.optimization_potential_savings_tokens == 1000
    assert item.optimization_potential_savings_usd == 0.5


def test_attach_optimization_badges_empty_items_noop(service):
    # Should not touch the DB when there is nothing to badge.
    with patch.object(
        rse_mod.crud_runtime_session_optimization_result, "list_for_sessions"
    ) as lookup:
        service._attach_optimization_badges(account=_make_account(), items=[])
    lookup.assert_not_called()


def test_attach_optimization_badges_swallows_db_error(service):
    from sqlalchemy.exc import SQLAlchemyError

    item = RuntimeSessionExplorerService._summary_row_to_schema(_make_summary_row())
    with patch.object(
        rse_mod.crud_runtime_session_optimization_result,
        "list_for_sessions",
        side_effect=SQLAlchemyError("boom"),
    ):
        service._attach_optimization_badges(account=_make_account(), items=[item])
    # No badge applied, no exception raised.
    assert item.optimization_waste_score is None


# --- get_account_session_detail / 404 paths --------------------------------


def test_get_account_session_detail_raises_404(service):
    with patch.object(
        rse_mod.crud_runtime_session,
        "get_account_session_summary",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            service.get_account_session_detail(
                account=_make_account(), runtime_session_id="missing"
            )
    assert exc.value.status_code == 404


def test_get_account_session_detail_returns_response(service):
    row = _make_summary_row()
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session_summary",
            return_value=row,
        ),
        patch.object(
            rse_mod.crud_api_usage, "get_gateway_usage_by_model", return_value=[]
        ),
    ):
        response = service.get_account_session_detail(
            account=_make_account(), runtime_session_id=row["id"]
        )
    assert response.session.id == row["id"]
    assert response.usage_by_model == []


def test_get_account_session_interactions_raises_404(service):
    with patch.object(
        rse_mod.crud_runtime_session,
        "get_account_session_summary",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            service.get_account_session_interactions(
                account=_make_account(), runtime_session_id="missing"
            )
    assert exc.value.status_code == 404


# --- get_account_session_summary_insight -----------------------------------


def test_summary_insight_low_risk(service):
    row = _make_summary_row(failed_requests=0, estimated_cost=0.0)
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session_summary",
            return_value=row,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=None
        ),
    ):
        insight = service.get_account_session_summary_insight(
            account=_make_account(), runtime_session_id=row["id"]
        )
    assert insight.risk_level == "low"
    assert insight.generated_by == "local"
    assert insight.fast_model_name is None


def test_summary_insight_high_risk_on_failures(service):
    row = _make_summary_row(failed_requests=2)
    model = MagicMock()
    model.name = "fast-model"
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session_summary",
            return_value=row,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=model
        ),
    ):
        insight = service.get_account_session_summary_insight(
            account=_make_account(), runtime_session_id=row["id"]
        )
    assert insight.risk_level == "high"
    assert insight.fast_model_name == "fast-model"
    assert insight.next_action == "Review failed request details."


def test_summary_insight_medium_risk_on_high_cost(service):
    row = _make_summary_row(failed_requests=0, estimated_cost=5.0)
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session_summary",
            return_value=row,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=None
        ),
    ):
        insight = service.get_account_session_summary_insight(
            account=_make_account(), runtime_session_id=row["id"]
        )
    assert insight.risk_level == "medium"


def test_summary_insight_raises_404(service):
    with patch.object(
        rse_mod.crud_runtime_session,
        "get_account_session_summary",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            service.get_account_session_summary_insight(
                account=_make_account(), runtime_session_id="missing"
            )
    assert exc.value.status_code == 404


# --- _call_interaction_summary_model ---------------------------------------


def _make_ai_model():
    model = MagicMock()
    model.provider_name = "openai"
    model.model_identifier = "gpt-5"
    model.api_key = "sk-test"
    model.api_endpoint = None
    model.name = "GPT-5"
    return model


def test_call_interaction_summary_model_parses_json(service):
    model = _make_ai_model()
    payload = {"request": {"messages": [{"role": "user", "content": "hi"}]}}
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[
        0
    ].message.content = '{"title": "T", "summary": "S", "risk_level": "low"}'
    with patch.object(rse_mod.litellm, "completion", return_value=completion):
        result = service._call_interaction_summary_model(
            model, {"api_key": "sk-test"}, payload=payload
        )
    assert result["title"] == "T"


def test_call_interaction_summary_model_strips_code_fence(service):
    model = _make_ai_model()
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[
        0
    ].message.content = '```json\n{"title": "T", "summary": "S"}\n```'
    with patch.object(rse_mod.litellm, "completion", return_value=completion):
        result = service._call_interaction_summary_model(
            model, {"api_key": "sk-test"}, payload={}
        )
    assert result["title"] == "T"


def test_call_interaction_summary_model_rejects_non_object(service):
    model = _make_ai_model()
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = "[1, 2, 3]"
    with patch.object(rse_mod.litellm, "completion", return_value=completion):
        with pytest.raises(ValueError):
            service._call_interaction_summary_model(
                model, {"api_key": "sk-test"}, payload={}
            )


def test_call_interaction_summary_model_bounds_provider_timeout(service):
    model = _make_ai_model()
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = '{"title": "T", "summary": "S"}'
    with patch.object(
        rse_mod.litellm, "completion", return_value=completion
    ) as mock_completion:
        service._call_interaction_summary_model(
            model, {"api_key": "sk-test"}, payload={}
        )
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["timeout"] == INTERACTION_SUMMARY_ATTEMPT_TIMEOUT_SECONDS
    assert kwargs["num_retries"] == 0


# --- summarize_account_runtime_session_interaction -------------------------


@pytest.mark.asyncio
async def test_summarize_interaction_raises_404_for_missing_session(service):
    with patch.object(
        rse_mod.crud_runtime_session, "get_account_session", return_value=None
    ):
        with pytest.raises(HTTPException) as exc:
            await service.summarize_account_runtime_session_interaction(
                account=_make_account(),
                runtime_session_id="s",
                activity_id="a",
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_summarize_interaction_raises_404_for_missing_activity(service):
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session",
            return_value=MagicMock(),
        ),
        patch.object(
            rse_mod.crud_runtime_session_activity,
            "get_model_gateway_call_for_session",
            return_value=None,
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await service.summarize_account_runtime_session_interaction(
                account=_make_account(),
                runtime_session_id="s",
                activity_id="a",
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_summarize_interaction_returns_local_when_no_model(service):
    activity = MagicMock()
    activity.id = uuid.uuid4()
    activity.metadata_ = {"model_alias": "gpt-5", "outcome": "success"}
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session",
            return_value=MagicMock(),
        ),
        patch.object(
            rse_mod.crud_runtime_session_activity,
            "get_model_gateway_call_for_session",
            return_value=activity,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=None
        ),
    ):
        result = await service.summarize_account_runtime_session_interaction(
            account=_make_account(),
            runtime_session_id="s",
            activity_id=str(activity.id),
        )
    assert result.generated_by == "local"


@pytest.mark.asyncio
async def test_summarize_interaction_returns_model_summary(service):
    activity = MagicMock()
    activity.id = uuid.uuid4()
    activity.metadata_ = {"model_alias": "gpt-5", "outcome": "success"}
    model = _make_ai_model()
    generated = {
        "title": "Generated title",
        "summary": "Generated summary",
        "key_points": ["a", "b"],
        "risk_level": "low",
        "next_action": "Do nothing",
    }
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session",
            return_value=MagicMock(),
        ),
        patch.object(
            rse_mod.crud_runtime_session_activity,
            "get_model_gateway_call_for_session",
            return_value=activity,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=model
        ),
        patch.object(
            service, "_call_interaction_summary_model", return_value=generated
        ),
    ):
        result = await service.summarize_account_runtime_session_interaction(
            account=_make_account(),
            runtime_session_id="s",
            activity_id=str(activity.id),
        )
    assert result.generated_by == "model"
    assert result.title == "Generated title"
    assert result.model_name == "GPT-5"


@pytest.mark.asyncio
async def test_summarize_interaction_falls_back_on_model_error(service):
    activity = MagicMock()
    activity.id = uuid.uuid4()
    activity.metadata_ = {"model_alias": "gpt-5", "outcome": "success"}
    model = _make_ai_model()
    with (
        patch.object(
            rse_mod.crud_runtime_session,
            "get_account_session",
            return_value=MagicMock(),
        ),
        patch.object(
            rse_mod.crud_runtime_session_activity,
            "get_model_gateway_call_for_session",
            return_value=activity,
        ),
        patch.object(
            rse_mod.crud_ai_model, "get_default_active_model", return_value=model
        ),
        patch.object(
            service,
            "_call_interaction_summary_model",
            side_effect=RuntimeError("llm down"),
        ),
    ):
        result = await service.summarize_account_runtime_session_interaction(
            account=_make_account(),
            runtime_session_id="s",
            activity_id=str(activity.id),
        )
    # Falls back to the local summary but records the model name.
    assert result.generated_by == "local"
    assert result.model_name == "GPT-5"


def test_default_activity_title_for_transcript_messages() -> None:
    """Ingested transcript messages are titled by role in the timeline."""
    from types import SimpleNamespace

    from preloop.services.runtime_session_explorer import _default_activity_title

    def activity(activity_type, role=None):
        return SimpleNamespace(
            activity_type=activity_type,
            metadata_={"role": role} if role else None,
        )

    assert (
        _default_activity_title(activity("transcript_message", "user"))
        == "User message"
    )
    assert (
        _default_activity_title(activity("transcript_message", "assistant"))
        == "Assistant message"
    )
    assert (
        _default_activity_title(activity("transcript_message", "tool_use"))
        == "Tool call"
    )
    assert (
        _default_activity_title(activity("transcript_message")) == "Transcript message"
    )
    assert (
        _default_activity_title(activity("agent_control_message")) == "Operator message"
    )
    assert _default_activity_title(activity("tool_call")) == "Tool call"
