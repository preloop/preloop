"""Tests for runtime-session optimization (moved from the billing plugin)."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.session_optimization import SessionOptimizationService
from preloop.models.crud import crud_account, crud_api_usage, crud_runtime_session
from preloop.models.models.ai_model import AIModel
from preloop.schemas.gateway_usage import RuntimeSessionOptimizationRequest


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


@pytest.fixture
def mock_fast_model() -> AIModel:
    return AIModel(
        id=uuid4(),
        name="Fast model",
        provider_name="openai",
        model_identifier="gpt-test",
        meta_data={"gateway": {"model_alias": "openai/gpt-test"}},
    )


def test_session_optimization_service_returns_trim_context(
    db_session, test_user
) -> None:
    """Prompt-heavy sessions should suggest context trimming."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-opt"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert result.suggestions[0].id == "trim-context"
    # No captured payloads -> no measured compressible tokens are claimed.
    assert result.suggestions[0].expected_savings_tokens == 0


def test_session_optimization_suggestions_are_grounded_in_payloads(
    db_session, test_user
) -> None:
    """Suggestions should carry measured savings and evidence event ids."""
    from datetime import UTC, datetime, timedelta

    from preloop.models.crud import crud_runtime_session_activity

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-grounded"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )
    file_content = "def main():\n    pass\n" * 50
    now = datetime.now(UTC)
    payload = {
        "request": {
            "messages": [
                {"role": "system", "content": "You are an agent"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "c2",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": file_content},
                {"role": "tool", "tool_call_id": "c2", "content": file_content},
                {"role": "user", "content": "continue"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "read_file"}},
                {
                    "type": "function",
                    "function": {
                        "name": "never_used",
                        "description": "long description " * 20,
                    },
                },
            ],
        },
        "prompt_tokens": 900,
        "completion_tokens": 100,
        "total_tokens": 1000,
        "outcome": "success",
        "status_code": 200,
        "estimated_cost": 0.5,
    }
    activity = crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="success",
        metadata=payload,
        timestamp=now + timedelta(seconds=1),
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    by_id = {suggestion.id: suggestion for suggestion in result.suggestions}
    assert "dedupe-tool-outputs" in by_id
    duplicate_tokens = len(file_content) // 4
    assert by_id["dedupe-tool-outputs"].expected_savings_tokens == duplicate_tokens
    assert by_id["dedupe-tool-outputs"].evidence_event_ids == [str(activity.id)]
    assert by_id["dedupe-tool-outputs"].expected_savings_usd == round(
        duplicate_tokens * 0.5 / 1000, 6
    )
    assert "scope-tools" in by_id
    assert by_id["scope-tools"].expected_savings_tokens > 0
    assert "never_used" in by_id["scope-tools"].evidence[1]


def test_session_optimization_service_builds_budget_guardrail(
    db_session, test_user
) -> None:
    """Balanced sessions should still receive a budget guardrail suggestion."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-balanced"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=400,
        completion_tokens=600,
        total_tokens=1000,
        estimated_cost=0.2,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert result.suggestions[0].id == "budget-guardrail"


def test_session_optimization_service_suggests_fix_failures(
    db_session, test_user
) -> None:
    """Sessions with failed gateway calls should prioritize failure remediation."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-failed"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=403,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=200,
        completion_tokens=0,
        total_tokens=200,
        estimated_cost=0.05,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert any(suggestion.id == "fix-failures" for suggestion in result.suggestions)


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_session_optimization_service_uses_model_generated_suggestions(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Model-generated suggestions should be returned when the gateway call succeeds."""
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "id": "reduce-retries",
                                    "title": "Reduce retry spend",
                                    "description": "Several calls retried after 403s.",
                                    "expected_savings_tokens": 120,
                                    "expected_savings_usd": 0.12,
                                    "confidence": "high",
                                    "action_label": "Inspect failures",
                                    "evidence": ["2 denied requests"],
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 60,
            "total_tokens": 100,
        },
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-model"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=500,
        completion_tokens=500,
        total_tokens=1000,
        estimated_cost=0.2,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )

    assert result.generated_by == "model"
    assert result.suggestions[0].id == "reduce-retries"
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 100
    gateway.create_chat_completion.assert_called_once()
    payload = gateway.create_chat_completion.call_args.args[0]
    assert payload["metadata"]["purpose"] == "session_optimization"


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_session_optimization_service_falls_back_on_malformed_model_json(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Malformed model JSON should fall back to deterministic local suggestions."""
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [{"message": {"content": "not-json"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-bad-json"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert result.suggestions[0].id == "trim-context"


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_session_optimization_service_falls_back_when_suggestions_missing(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Missing suggestions key should trigger the local fallback path."""
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [
            {"message": {"content": json.dumps({"summary": "no suggestions"})}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-missing-key"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert result.suggestions[0].id == "trim-context"


def _grounded_payload(*, message_chars: int = 4000) -> dict:
    return {
        "request": {
            "messages": [
                {"role": "system", "content": "agent " * 50},
                {"role": "tool", "tool_call_id": "c1", "content": "x" * message_chars},
                {"role": "user", "content": "continue"},
            ],
        },
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "total_tokens": 2100,
        "outcome": "success",
        "status_code": 200,
        "estimated_cost": 0.1,
    }


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_optimization_prompt_stays_within_digest_budget(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Large sessions must not blow up the optimization prompt budget."""
    from datetime import UTC, datetime, timedelta

    from preloop.services.session_optimization import DIGEST_CHAR_BUDGET
    from preloop.models.crud import crud_runtime_session_activity

    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "id": "s1",
                                    "title": "t",
                                    "description": "d",
                                    "expected_savings_tokens": 10,
                                    "action_label": "a",
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-budget"
    )
    db_session.commit()
    now = datetime.now(UTC)
    for index in range(40):
        crud_runtime_session_activity.log_model_gateway_call(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime_session.id,
            status="success",
            metadata=_grounded_payload(message_chars=20_000),
            timestamp=now + timedelta(seconds=index),
        )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=80_000,
        completion_tokens=4_000,
        total_tokens=84_000,
        estimated_cost=4.2,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )

    assert result.generated_by == "model"
    payload = gateway.create_chat_completion.call_args.args[0]
    user_content = payload["messages"][1]["content"]
    assert len(user_content) <= DIGEST_CHAR_BUDGET + 2_000
    assert payload["response_format"] == {"type": "json_object"}
    digest = json.loads(user_content)
    assert digest["context_profile"] is not None
    assert all(len(snippet["text"]) <= 400 for snippet in digest["snippets"])


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_inflated_model_savings_are_clamped(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Savings claims above measured waste must be clamped and downgraded."""
    from datetime import UTC, datetime

    from preloop.models.crud import crud_runtime_session_activity

    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "id": "huge-claim",
                                    "title": "Save a million tokens",
                                    "description": "Inflated",
                                    "expected_savings_tokens": 1_000_000,
                                    "confidence": "high",
                                    "action_label": "Apply",
                                    "evidence_event_ids": ["not-a-real-event"],
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-clamp"
    )
    db_session.commit()
    crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="success",
        metadata=_grounded_payload(),
        timestamp=datetime.now(UTC),
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=2000,
        completion_tokens=100,
        total_tokens=2100,
        estimated_cost=0.1,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )

    assert result.generated_by == "model"
    suggestion = result.suggestions[0]
    assert suggestion.expected_savings_tokens == 2000
    assert suggestion.confidence == "low"
    assert suggestion.evidence_event_ids == []


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_response_format_rejection_falls_back_to_plain_call(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Providers rejecting response_format should get one retry without it."""
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    success_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "id": "retry-ok",
                                    "title": "t",
                                    "description": "d",
                                    "expected_savings_tokens": 1,
                                    "action_label": "a",
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    gateway.create_chat_completion.side_effect = [
        ValueError("response_format unsupported"),
        success_response,
    ]
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-respfmt"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=500,
        completion_tokens=500,
        total_tokens=1000,
        estimated_cost=0.2,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )

    assert result.generated_by == "model"
    assert result.suggestions[0].id == "retry-ok"
    assert gateway.create_chat_completion.call_count == 2
    second_payload = gateway.create_chat_completion.call_args_list[1].args[0]
    assert "response_format" not in second_payload


def _model_response(suggestion_id: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "id": suggestion_id,
                                    "title": "t",
                                    "description": "d",
                                    "expected_savings_tokens": 1,
                                    "action_label": "a",
                                }
                            ]
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_generated_results_are_cached_until_regenerate(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Re-opening the panel must not trigger a new LLM call; regenerate must."""
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = _model_response("cached-one")
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-cache"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=500,
        completion_tokens=500,
        total_tokens=1000,
        estimated_cost=0.2,
    )
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    first = service.get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )
    assert first.generated_by == "model"
    assert first.from_cache is False
    assert first.generated_at is not None
    assert gateway.create_chat_completion.call_count == 1

    second = service.get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )
    assert second.from_cache is True
    assert second.suggestions[0].id == "cached-one"
    assert gateway.create_chat_completion.call_count == 1

    # A different scope misses the cache.
    gateway.create_chat_completion.return_value = _model_response("scoped")
    scoped = service.get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(from_index=0, to_index=1),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )
    assert scoped.from_cache is False
    assert gateway.create_chat_completion.call_count == 2

    # regenerate=True bypasses and overwrites the cache.
    gateway.create_chat_completion.return_value = _model_response("cached-two")
    regenerated = service.get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(regenerate=True),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )
    assert regenerated.from_cache is False
    assert regenerated.suggestions[0].id == "cached-two"
    assert gateway.create_chat_completion.call_count == 3

    third = service.get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )
    assert third.from_cache is True
    assert third.suggestions[0].id == "cached-two"
    assert gateway.create_chat_completion.call_count == 3


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_daily_cap_skips_llm_and_degrades_to_local(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """Optimization spend above the daily cap must skip the model call."""
    from preloop.services.session_optimization import (
        LLM_SKIPPED_DAILY_CAP,
        OPTIMIZATION_USAGE_PURPOSE,
    )
    from preloop.config import settings

    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = _model_response("never-called")
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-cap"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )
    # Seed today's optimization spend at the cap (purpose-tagged usage row).
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=100,
        total_tokens=200,
        estimated_cost=float(settings.billing_session_optimization_daily_cap_usd),
        meta_data={"purpose": OPTIMIZATION_USAGE_PURPOSE},
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
        budget_enforcer=MagicMock(),
    )

    assert result.generated_by == "local"
    assert result.llm_skipped_reason == LLM_SKIPPED_DAILY_CAP
    gateway.create_chat_completion.assert_not_called()


def test_gateway_spend_purpose_filter_only_sums_tagged_rows(
    db_session, test_user
) -> None:
    """get_gateway_spend(purpose=...) must ignore untagged usage rows."""
    from datetime import UTC, datetime, timedelta

    kwargs = dict(
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=100,
        total_tokens=200,
    )
    crud_api_usage.log_gateway_request(db_session, estimated_cost=1.0, **kwargs)
    crud_api_usage.log_gateway_request(
        db_session,
        estimated_cost=0.25,
        meta_data={"purpose": "session_optimization"},
        **kwargs,
    )

    spend = crud_api_usage.get_gateway_spend(
        db_session,
        account_id=str(test_user.account_id),
        start=datetime.now(UTC) - timedelta(hours=1),
        purpose="session_optimization",
    )
    assert spend == 0.25


def test_scope_hash_handles_none_event_ids(mock_fast_model) -> None:
    """Scope hashing must tolerate absent event id lists."""
    request = RuntimeSessionOptimizationRequest.model_construct(event_ids=None)
    scope_hash = SessionOptimizationService._scope_hash(request, mock_fast_model)
    assert isinstance(scope_hash, str)
    assert len(scope_hash) == 64


@patch(
    "preloop.services.session_optimization.load_session_gateway_events",
    return_value=[],
)
def test_optimization_loads_gateway_events_once(
    mock_load_events,
    db_session,
    test_user,
) -> None:
    """Profile building and LLM digest prep should share one DB load."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-single-load"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    SessionOptimizationService(db_session).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )
    mock_load_events.assert_called_once()


def test_parse_optimization_model_response_strips_markdown_fence(
    mock_fast_model,
) -> None:
    """Markdown-wrapped JSON should still parse into suggestions."""
    service = SessionOptimizationService(MagicMock())
    suggestions, token_usage, estimated_cost = (
        service._parse_optimization_model_response(
            mock_fast_model,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                + json.dumps(
                                    {
                                        "suggestions": [
                                            {
                                                "id": "cache-tools",
                                                "title": "Cache tool schemas",
                                                "description": "Repeated schemas dominate prompt size.",
                                            }
                                        ]
                                    }
                                )
                                + "\n```"
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )
    )

    assert suggestions[0].id == "cache-tools"
    assert token_usage.total_tokens == 30
    assert estimated_cost >= 0.0


def test_parse_optimization_model_response_handles_fence_without_newline(
    mock_fast_model,
) -> None:
    """A fence glued to the JSON ('```json{...}') must parse, not IndexError."""
    service = SessionOptimizationService(MagicMock())
    payload = {
        "suggestions": [
            {
                "id": "cache-tools",
                "title": "Cache tool schemas",
                "description": "Repeated schemas dominate prompt size.",
            }
        ]
    }
    suggestions, token_usage, _ = service._parse_optimization_model_response(
        mock_fast_model,
        {
            "choices": [
                {"message": {"content": "```json" + json.dumps(payload) + "```"}}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        },
    )

    assert suggestions[0].id == "cache-tools"
    assert token_usage.total_tokens == 30


@patch("preloop.services.session_optimization.crud_ai_model.get_default_active_model")
@patch.object(SessionOptimizationService, "_create_gateway_service")
def test_no_newline_fenced_empty_suggestions_degrade_to_local_fallback(
    mock_create_gateway,
    mock_get_default_model,
    db_session,
    test_user,
    mock_fast_model,
) -> None:
    """The pathological '```json{"suggestions":[]}' payload must not raise.

    Regression: the fence stripper used ``raw.split("\\n", 1)[1]``, which
    raised IndexError when the fenced payload contained no newline at all.
    The service must degrade to deterministic local suggestions instead.
    """
    mock_get_default_model.return_value = mock_fast_model
    gateway = MagicMock()
    gateway.create_chat_completion.return_value = {
        "choices": [{"message": {"content": '```json{"suggestions":[]}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_create_gateway.return_value = gateway

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-fence-no-newline"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.generated_by == "local"
    assert result.suggestions[0].id == "trim-context"


def test_local_response_includes_waste_score_and_actions(db_session, test_user) -> None:
    """Grounded local responses must expose waste score and action contracts."""
    from datetime import timedelta

    from preloop.models.crud import crud_runtime_session_activity

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-finalize"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=2000,
        completion_tokens=100,
        total_tokens=2100,
        estimated_cost=0.1,
    )
    file_content = "def main():\n    pass\n" * 50
    now = datetime.now(UTC)
    payload = {
        "request": {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "c2",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": file_content},
                {"role": "tool", "tool_call_id": "c2", "content": file_content},
            ],
        },
        "prompt_tokens": 2000,
        "completion_tokens": 100,
        "total_tokens": 2100,
        "outcome": "success",
        "status_code": 200,
        "estimated_cost": 0.1,
    }
    crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        status="success",
        metadata=payload,
        timestamp=now + timedelta(seconds=1),
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    result = SessionOptimizationService(
        db_session
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    assert result.waste_score is not None
    assert result.waste_score > 0
    assert result.context_profile is not None
    assert result.potential_savings_tokens > 0
    by_id = {suggestion.id: suggestion for suggestion in result.suggestions}
    dedupe = by_id["dedupe-tool-outputs"]
    assert dedupe.action is not None
    assert dedupe.action.type == "open_events"
    assert dedupe.action.params["event_ids"] == dedupe.evidence_event_ids


def _apply_request(
    *,
    suggestion_id: str = "scope-tools",
    action_type: str = "scope_tools",
    params: dict | None = None,
):
    from preloop.schemas.gateway_usage import (
        RuntimeSessionOptimizationActionSpec,
        RuntimeSessionOptimizationApplyRequest,
    )

    return RuntimeSessionOptimizationApplyRequest(
        suggestion_id=suggestion_id,
        suggestion_title="Test suggestion",
        action=RuntimeSessionOptimizationActionSpec(
            type=action_type,
            params=params
            or {
                "subject_type": "api_keys",
                "subject_id": "key-1",
                "tool_names": ["never_used", "stale_tool"],
            },
        ),
    )


def test_apply_scope_tools_action_disables_tools(db_session, test_user) -> None:
    """Applying scope_tools must persist governance overrides and history."""
    from preloop.services.subject_governance import get_subject_governance

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-tools"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    applied = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=_apply_request(),
        current_user=test_user,
    )

    assert applied.action_type == "scope_tools"
    assert applied.status == "applied"
    assert applied.result["disabled_tools"] == ["never_used", "stale_tool"]
    db_session.refresh(account)
    config = get_subject_governance(
        account.meta_data, subject_type="api_keys", subject_id="key-1"
    )
    assert config["tool_enabled_overrides"] == {
        "never_used": False,
        "stale_tool": False,
    }
    history = service.list_account_session_optimization_actions(
        account=account, runtime_session_id=str(runtime_session.id)
    )
    assert len(history.items) == 1
    assert history.items[0].suggestion_id == "scope-tools"


def test_apply_enable_compression_sets_context_optimization(
    db_session, test_user
) -> None:
    """enable_compression must persist dedupe/noise settings in governance."""
    from preloop.services.subject_governance import get_subject_governance

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-compress"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    applied = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=_apply_request(
            suggestion_id="dedupe-tool-outputs",
            action_type="enable_compression",
            params={
                "subject_type": "api_keys",
                "subject_id": "key-compress",
                "dedupe_tool_results": True,
                "strip_noise": True,
            },
        ),
        current_user=test_user,
    )

    assert applied.action_type == "enable_compression"
    assert applied.result["context_optimization"] == {
        "dedupe_tool_results": True,
        "strip_noise": True,
    }
    db_session.refresh(account)
    config = get_subject_governance(
        account.meta_data, subject_type="api_keys", subject_id="key-compress"
    )
    assert config["context_optimization"]["dedupe_tool_results"] is True
    assert config["context_optimization"]["strip_noise"] is True


def test_apply_cap_tool_results_sets_cap_and_validates(db_session, test_user) -> None:
    """cap_tool_results must persist the cap and reject tiny caps."""
    from fastapi import HTTPException

    from preloop.services.subject_governance import get_subject_governance

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-cap"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    applied = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=_apply_request(
            suggestion_id="trim-context",
            action_type="cap_tool_results",
            params={
                "subject_type": "api_keys",
                "subject_id": "key-cap",
                "max_tool_result_chars": 8000,
            },
        ),
        current_user=test_user,
    )

    assert applied.action_type == "cap_tool_results"
    db_session.refresh(account)
    config = get_subject_governance(
        account.meta_data, subject_type="api_keys", subject_id="key-cap"
    )
    assert config["context_optimization"]["max_tool_result_chars"] == 8000

    with pytest.raises(HTTPException) as exc_info:
        service.apply_account_session_optimization_action(
            account=account,
            runtime_session_id=str(runtime_session.id),
            request=_apply_request(
                suggestion_id="trim-context-tiny",
                action_type="cap_tool_results",
                params={
                    "subject_type": "api_keys",
                    "subject_id": "key-cap",
                    "max_tool_result_chars": 10,
                },
            ),
            current_user=test_user,
        )
    assert exc_info.value.status_code == 400


def test_apply_same_suggestion_twice_conflicts(db_session, test_user) -> None:
    """Re-applying the same suggestion must raise a 409 conflict."""
    from fastapi import HTTPException

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-dupe"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)
    service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=_apply_request(),
        current_user=test_user,
    )

    with pytest.raises(HTTPException) as exc_info:
        service.apply_account_session_optimization_action(
            account=account,
            runtime_session_id=str(runtime_session.id),
            request=_apply_request(),
            current_user=test_user,
        )
    assert exc_info.value.status_code == 409


def test_apply_rejects_client_side_action_types(db_session, test_user) -> None:
    """open_events is client-side and must not be applied on the server."""
    from fastapi import HTTPException

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-open"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None

    with pytest.raises(HTTPException) as exc_info:
        SessionOptimizationService(
            db_session
        ).apply_account_session_optimization_action(
            account=account,
            runtime_session_id=str(runtime_session.id),
            request=_apply_request(
                action_type="open_events", params={"event_ids": ["e1"]}
            ),
            current_user=test_user,
        )
    assert exc_info.value.status_code == 400


def test_apply_set_budget_creates_policy_and_measures_outcome(
    db_session, test_user
) -> None:
    """set_budget must create a budget policy and measure post-apply outcomes."""
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-budget"
    )
    db_session.commit()
    # Baseline traffic for the principal before the action is applied.
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-apply-budget",
    )
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    applied = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=_apply_request(
            suggestion_id="budget-guardrail",
            action_type="set_budget",
            params={
                "subject_type": "account",
                "period": "daily",
                "hard_limit_usd": 2.5,
            },
        ),
        current_user=test_user,
    )

    assert applied.action_type == "set_budget"
    assert applied.result["hard_limit_usd"] == 2.5
    assert applied.baseline is not None
    assert applied.baseline["avg_total_tokens_per_request"] == 1000.0
    from preloop.models.crud import crud_budget_policy

    policy = crud_budget_policy.get(db_session, id=applied.result["budget_policy_id"])
    assert policy is not None
    assert float(policy.hard_limit_usd) == 2.5

    # Post-apply traffic from another session of the same principal.
    other_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-budget-next"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(other_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=450,
        completion_tokens=50,
        total_tokens=500,
        estimated_cost=0.25,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-apply-budget",
    )

    history = service.list_account_session_optimization_actions(
        account=account, runtime_session_id=str(runtime_session.id)
    )
    outcome = history.items[0].outcome
    assert outcome is not None
    assert outcome["requests"] == 1
    assert outcome["avg_total_tokens_per_request"] == 500.0
    assert outcome["tokens_per_request_change_pct"] == -50.0


def test_apply_set_budget_updates_existing_policy(db_session, test_user) -> None:
    """Re-applying a budget for the same scope must update, not duplicate."""
    from preloop.models.crud import crud_budget_policy

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-apply-budget-update"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    service = SessionOptimizationService(db_session)

    def request_with_limit(suggestion_id: str, limit: float):
        return _apply_request(
            suggestion_id=suggestion_id,
            action_type="set_budget",
            params={
                "subject_type": "account",
                "period": "daily",
                "hard_limit_usd": limit,
            },
        )

    first = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=request_with_limit("budget-one", 2.0),
        current_user=test_user,
    )
    second = service.apply_account_session_optimization_action(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=request_with_limit("budget-two", 5.0),
        current_user=test_user,
    )

    assert first.result["budget_policy_id"] == second.result["budget_policy_id"]
    policy = crud_budget_policy.get(db_session, id=second.result["budget_policy_id"])
    assert policy is not None
    assert float(policy.hard_limit_usd) == 5.0


def test_session_list_surfaces_cached_waste_score(db_session, test_user) -> None:
    """The explorer list must expose cached waste scores as badges."""
    from preloop.models.crud import crud_runtime_session_optimization_result
    from preloop.services.runtime_session_explorer import (
        RuntimeSessionExplorerService,
    )

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-badge"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-test",
        provider_name="openai",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )
    crud_runtime_session_optimization_result.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=str(runtime_session.id),
        scope_hash="scope",
        model_id=None,
        response={
            "waste_score": 42,
            "potential_savings_tokens": 1200,
            "potential_savings_usd": 0.6,
            "suggestions": [],
        },
    )

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    listing = RuntimeSessionExplorerService(db_session).list_account_sessions(
        account=account
    )
    item = next(item for item in listing.items if item.id == str(runtime_session.id))
    assert item.optimization_waste_score == 42
    assert item.optimization_potential_savings_tokens == 1200
    assert item.optimization_potential_savings_usd == 0.6


# ---------------------------------------------------------------------------
# Cohort instrumentation (T16): launch-metric telemetry via audit_log.
#
# NOTE: audit_log is compliance-shaped (append-only, retention/PII semantics).
# These product-telemetry events are kept minimal; a dedicated
# user_activity_event table is the eventual clean home (deferred).
# ---------------------------------------------------------------------------


@pytest.fixture
def cost_client(app, db_session, test_user):
    """Test client for the session-optimization routes.

    The router is part of the core app; only ``get_account_for_user`` needs
    binding to the test account.
    """
    from fastapi.testclient import TestClient

    from preloop.api.common import get_account_for_user

    account = crud_account.get(db_session, id=test_user.account_id)
    app.dependency_overrides[get_account_for_user] = lambda: account
    with TestClient(app) as test_client:
        yield test_client


def test_optimization_result_view_writes_audit_event(
    cost_client, db_session, test_user
) -> None:
    """Hitting the optimizations endpoint writes an audit view event.

    The service is called internally in many places; the
    ``optimization_result_viewed`` event must only fire at the HTTP layer so
    that the "reached-their-number" launch metric counts real user views.
    """
    from preloop.models.crud import crud_audit_log

    # The value loop is open capability (T2 OSS move): no subscription or
    # entitlement is required for a free account to view its own findings.
    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-view-audit"
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(runtime_session.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=900,
        completion_tokens=100,
        total_tokens=1000,
        estimated_cost=0.5,
    )

    response = cost_client.post(
        f"/api/v1/billing/cost/runtime-sessions/{runtime_session.id}/optimizations",
        json={},
    )
    assert response.status_code == 200, response.text

    events = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action="optimization_result_viewed",
    )
    assert len(events) == 1
    event = events[0]
    assert event.user_id == test_user.id
    assert event.resource_type == "runtime_session"
    assert event.resource_id == str(runtime_session.id)
    assert event.details == {"runtime_session_id": str(runtime_session.id)}


def test_internal_service_call_does_not_write_view_audit_event(
    db_session, test_user
) -> None:
    """Calling the service directly must NOT emit a view event.

    The audit event lives at the endpoint layer, so cache-warm / internal
    service calls stay silent and do not inflate the view metric.
    """
    from preloop.models.crud import crud_audit_log

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-view-internal"
    )
    db_session.commit()
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None

    SessionOptimizationService(db_session).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=str(runtime_session.id),
        request=RuntimeSessionOptimizationRequest(),
        current_user=test_user,
    )

    events = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action="optimization_result_viewed",
    )
    assert events == []


def test_launch_metrics_are_derivable_from_audit_events(db_session, test_user) -> None:
    """Seed login + view + apply rows and derive all three launch metrics.

    Proves apply-click-rate, reached-their-number, and 7-day-return are
    computable from the audit_log rows this task adds (plus the pre-existing
    RuntimeSessionOptimizationAction apply rows).

    Assumptions (documented as this is proxy telemetry, not the final
    user_activity_event schema):
      - apply-click-rate  = distinct sessions with an apply / sessions viewed
      - reached-their-number = sessions both viewed and applied / sessions viewed
      - 7-day-return = users with a second login within 7 days of the first
    """
    from datetime import timedelta

    from preloop.models.crud import (
        crud_audit_log,
        crud_runtime_session_optimization_action,
    )

    account_id = test_user.account_id
    now = datetime.now(UTC)

    runtime_session = _create_runtime_session(
        db_session, test_user, source_id="workspace-metrics"
    )
    db_session.commit()

    # Two logins within 7 days => this user counts as a 7-day return.
    for offset in (timedelta(days=6), timedelta(days=1)):
        crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            user_id=test_user.id,
            action="user.login",
            resource_type="user",
            resource_id=str(test_user.id),
            status="success",
        )
        # Backdate the first login so the gap is measurable.
        latest = crud_audit_log.get_by_account(
            db_session, account_id=account_id, action="user.login", limit=1
        )[0]
        latest.timestamp = now - offset
        db_session.commit()

    # One viewed-results event for this session.
    crud_audit_log.log_action(
        db_session,
        account_id=account_id,
        user_id=test_user.id,
        action="optimization_result_viewed",
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        status="success",
        details={"runtime_session_id": str(runtime_session.id)},
    )

    # One applied action for the same session (pre-existing timestamped table).
    crud_runtime_session_optimization_action.create_applied(
        db_session,
        account_id=account_id,
        runtime_session_id=runtime_session.id,
        suggestion_id="trim-context",
        suggestion_title="Trim context",
        action_type="enable_compression",
        params={},
        applied_by=test_user.username,
        runtime_principal_id="workspace-metrics",
        baseline=None,
        result={"applied": True},
    )

    # --- Derive the three launch metrics purely from stored rows. ---
    view_events = crud_audit_log.get_by_account(
        db_session, account_id=account_id, action="optimization_result_viewed"
    )
    viewed_sessions = {e.resource_id for e in view_events}
    apply_rows = crud_runtime_session_optimization_action.list_for_session(
        db_session, account_id=account_id, runtime_session_id=runtime_session.id
    )
    applied_sessions = {str(a.runtime_session_id) for a in apply_rows}

    apply_click_rate = len(applied_sessions) / len(viewed_sessions)
    reached_number_rate = len(viewed_sessions & applied_sessions) / len(viewed_sessions)

    logins = crud_audit_log.get_by_account(
        db_session, account_id=account_id, action="user.login"
    )
    login_times = sorted(e.timestamp for e in logins)
    returned_within_7d = len(login_times) >= 2 and (
        login_times[-1] - login_times[0]
    ) <= timedelta(days=7)

    assert apply_click_rate == 1.0
    assert reached_number_rate == 1.0
    assert returned_within_7d is True
