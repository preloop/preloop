"""Tests for gateway activity touch behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import OperationalError

from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def test_runtime_session_touch_skips_recent_activity_without_flush():
    """Frequent activity touches should not rewrite the same hot row."""
    observed_at = datetime.now(timezone.utc)
    runtime_session = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        last_activity_at=observed_at - timedelta(seconds=5),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = runtime_session

    result = crud_runtime_session.touch_activity(
        db,
        account_id=runtime_session.account_id,
        runtime_session_id=runtime_session.id,
        observed_at=observed_at,
        min_update_interval=timedelta(seconds=30),
    )

    assert result is runtime_session
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_runtime_session_summary_refreshes_when_missing():
    """First gateway request for a session should persist a model-generated summary."""
    observed_at = datetime.now(timezone.utc)
    runtime_session = SimpleNamespace(
        id=uuid4(),
        summary=None,
        summary_updated_at=None,
    )
    usage = SimpleNamespace(
        model_alias="openai/gpt-test",
        provider_name="openai",
        status_code=200,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.01,
    )
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(
            token="token",
            user=SimpleNamespace(id=uuid4(), account_id=uuid4()),
        ),
    )

    with (
        patch(
            "preloop.services.openai_gateway.crud_ai_model.get_default_active_model",
            return_value=SimpleNamespace(name="Default model"),
        ),
        patch.object(
            service,
            "_runtime_session_summary_columns_available",
            return_value=True,
        ),
        patch.object(
            service,
            "_runtime_session_summary_state",
            return_value={"summary": None, "summary_updated_at": None},
        ),
        patch.object(
            service,
            "_generate_runtime_session_summary",
            return_value="Agent reviewed pricing changes",
        ),
    ):
        service._maybe_refresh_runtime_session_summary(
            runtime_session=runtime_session,
            usage=usage,
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
            response_payload={"choices": []},
            observed_at=observed_at,
        )

    service.db.execute.assert_called_once()
    service.db.commit.assert_called_once()


def test_runtime_session_summary_skips_recent_refresh():
    """Summary refresh should be occasional after the initial generated value."""
    observed_at = datetime.now(timezone.utc)
    runtime_session = SimpleNamespace(
        id=uuid4(),
        summary="Existing summary",
        summary_updated_at=observed_at - timedelta(minutes=10),
    )
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(
            token="token",
            user=SimpleNamespace(id=uuid4(), account_id=uuid4()),
        ),
    )

    with (
        patch.object(
            service,
            "_runtime_session_summary_columns_available",
            return_value=True,
        ),
        patch.object(
            service,
            "_runtime_session_summary_state",
            return_value={
                "summary": "Existing summary",
                "summary_updated_at": observed_at - timedelta(minutes=10),
            },
        ),
        patch(
            "preloop.services.openai_gateway.crud_ai_model.get_default_active_model"
        ) as get_default,
    ):
        service._maybe_refresh_runtime_session_summary(
            runtime_session=runtime_session,
            usage=SimpleNamespace(),
            request_payload=None,
            response_payload=None,
            observed_at=observed_at,
        )

    get_default.assert_not_called()
    service.db.execute.assert_not_called()


def test_gateway_request_recording_survives_activity_touch_timeout():
    """A best-effort runtime-session touch must not fail a logged gateway request."""
    account_id = uuid4()
    user_id = uuid4()
    api_key_id = uuid4()
    runtime_session_id = uuid4()
    usage_id = uuid4()
    observed_at = datetime.now(timezone.utc)
    user = SimpleNamespace(id=user_id, account_id=account_id)
    api_key = SimpleNamespace(
        id=api_key_id,
        name="Agent key",
        context_data={
            "runtime_session_id": str(runtime_session_id),
            "runtime_principal": {
                "type": "claude_code",
                "id": "workspace-123",
                "name": "Claude Workspace",
            },
        },
    )
    usage_row = SimpleNamespace(
        id=usage_id,
        timestamp=observed_at,
        runtime_session_id=runtime_session_id,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-123",
        runtime_principal_name="Claude Workspace",
        auth_subject_type="api_key",
        flow_id=None,
        flow_execution_id=None,
        upstream_request_id="upstream-123",
        estimated_cost=0.01,
    )
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(
            token="token",
            user=user,
            api_key=api_key,
        ),
    )
    ai_model = SimpleNamespace(
        id=uuid4(),
        provider_name="openai",
    )

    with (
        patch(
            "preloop.services.openai_gateway.resolve_ai_model_runtime",
            return_value=SimpleNamespace(
                model_gateway_model_alias="preloop/openai/gpt-test",
                model_gateway_provider="openai",
            ),
        ),
        patch(
            "preloop.services.openai_gateway.estimate_ai_model_usage_cost",
            return_value=0.01,
        ),
        patch(
            "preloop.services.openai_gateway.crud_api_usage.log_gateway_request",
            return_value=usage_row,
        ),
        patch("preloop.services.openai_gateway.log_model_gateway_request") as log_audit,
        patch("preloop.services.openai_gateway.ModelGatewayEventEmitter") as emitter,
        patch("preloop.services.openai_gateway.GatewayUsageSearchService") as search,
        patch(
            "preloop.services.openai_gateway.crud_runtime_session.touch_activity",
            side_effect=OperationalError(
                "UPDATE runtime_session",
                {},
                Exception("statement timeout"),
            ),
        ),
    ):
        service._record_gateway_request(
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=1.2,
            ai_model=ai_model,
            requested_model="gpt-test",
            response_payload={
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                }
            },
            upstream_response={
                "id": "upstream-123",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                },
            },
            endpoint_kind="chat_completions",
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
        )

    log_audit.assert_called_once()
    emitter.return_value.emit_for_usage.assert_called_once()
    search.return_value.auto_index_interaction.assert_called_once()
    service.db.rollback.assert_called_once()


def test_runtime_session_resolution_rolls_back_after_timeout():
    """Startup session resolution should not leave the DB session poisoned."""
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    api_key = SimpleNamespace(
        id=uuid4(),
        name="Agent key",
        context_data={
            "runtime_principal": {
                "type": "claude_code",
                "id": "workspace-timeout",
                "name": "Claude Workspace",
            },
        },
    )
    db = MagicMock()
    service = OpenAIGatewayService(
        db=db,
        auth_context=ModelGatewayAuthContext(
            token="token",
            user=user,
            api_key=api_key,
        ),
    )

    with patch(
        "preloop.services.openai_gateway.crud_runtime_session.get_by_source",
        side_effect=OperationalError(
            "SELECT runtime_session",
            {},
            Exception("statement timeout"),
        ),
    ):
        runtime_session_id = service._resolve_runtime_session()

    assert runtime_session_id is None
    db.rollback.assert_called_once()
