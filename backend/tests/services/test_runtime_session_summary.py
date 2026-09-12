"""Optional session summaries must not affect primary gateway calls."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from openai import InternalServerError

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService


def _service() -> OpenAIGatewayService:
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test", user=SimpleNamespace(id=uuid4(), account_id=uuid4())
        ),
        upstream_backend=MagicMock(),
    )


def _usage(status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        model_alias="primary",
        provider_name="openai",
        status_code=status,
        prompt_tokens=2,
        completion_tokens=1,
        total_tokens=3,
        estimated_cost=0.01,
    )


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="summary-model",
        api_endpoint="https://summary.example.com/v1",
        meta_data={},
    )


def _generate(service: OpenAIGatewayService) -> str | None:
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="test-key")
        )
        return service._generate_runtime_session_summary(
            summary_model=_model(),
            existing_summary=None,
            usage=_usage(),
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
            response_payload={"choices": []},
        )


def _failure() -> InternalServerError:
    return InternalServerError(
        "Origin unavailable",
        response=httpx.Response(
            502,
            headers={"x-ratelimit-remaining-requests": "0"},
            request=httpx.Request("POST", "https://summary.example.com/v1"),
        ),
        body=None,
    )


@pytest.mark.parametrize("recover", [False, True])
def test_optional_failure_never_alerts_or_overwrites_primary_state(
    recover: bool,
) -> None:
    service = _service()
    initial_state = {
        "_last_upstream_retry_count": 2,
        "_last_rate_limit_snapshot": object(),
        "_last_context_optimization": object(),
        "_last_tools_meta": [{"name": "primary-tool"}],
        "_last_upstream_credential_type": "oauth",
        "_last_alibaba_cache_mode": "explicit",
    }
    for field, value in initial_state.items():
        setattr(service, field, value)
    success = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Reviewed changes"))]
    )
    service.upstream_backend.completion.side_effect = (
        [_failure(), success] if recover else _failure()
    )
    with (
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
        patch("preloop.services.openai_gateway.reserve_gateway_5xx_alert") as reserve,
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert") as alert,
        patch("preloop.services.openai_gateway.logger.info") as info,
        patch("preloop.services.openai_gateway.logger.warning") as warning,
    ):
        if recover:
            assert _generate(service) == "Reviewed changes"
        else:
            with pytest.raises(ModelGatewayAPIError):
                _generate(service)
    assert service.upstream_backend.completion.call_count == (2 if recover else 3)
    reserve.assert_not_called()
    alert.assert_not_called()
    assert info.call_count == (1 if recover else 2)
    assert all(
        "runtime_session_summary" in call.args[0] for call in info.call_args_list
    )
    warning.assert_not_called()
    for field, value in initial_state.items():
        assert getattr(service, field) is value


def test_primary_failure_still_alerts() -> None:
    service = _service()
    with (
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
        patch(
            "preloop.services.openai_gateway.reserve_gateway_5xx_alert",
            return_value=(True, 0),
        ),
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert") as alert,
    ):
        with pytest.raises(ModelGatewayAPIError):
            service._run_with_upstream_retries(
                "openai", MagicMock(side_effect=_failure()), ai_model=_model()
            )
    alert.assert_called_once()


@pytest.mark.parametrize(
    "result", [None, "Updated summary", RuntimeError("unavailable")]
)
def test_summary_cadence_survives_new_service_instances(result: Any) -> None:
    session = SimpleNamespace(id=uuid4())
    attempted = []
    existing = None
    for successful_count in range(1, 21):
        service = _service()
        generate = (
            MagicMock(side_effect=result)
            if isinstance(result, Exception)
            else MagicMock(return_value=result)
        )
        with (
            patch.object(
                service, "_runtime_session_summary_columns_available", return_value=True
            ),
            patch.object(
                service,
                "_runtime_session_summary_state",
                return_value={"summary": existing},
            ),
            patch(
                "preloop.services.openai_gateway.crud_api_usage.count_successful_gateway_calls_for_session",
                return_value=successful_count,
            ) as count,
            patch(
                "preloop.services.openai_gateway.crud_ai_model.get_default_active_model",
                return_value=_model(),
            ),
            patch.object(service, "_generate_runtime_session_summary", generate),
        ):
            service._maybe_refresh_runtime_session_summary(
                runtime_session=session,
                usage=_usage(),
                request_payload={},
                response_payload={},
                observed_at=datetime.now(timezone.utc),
            )
        count.assert_called_once_with(
            service.db,
            account_id=service.auth_context.user.account_id,
            runtime_session_id=session.id,
        )
        if generate.called:
            attempted.append(successful_count)
            if isinstance(result, str):
                existing = result
    assert attempted == [1, 10, 20]


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502])
def test_failed_primary_requests_never_generate_summaries(status: int) -> None:
    service = _service()
    with (
        patch.object(service, "_runtime_session_summary_columns_available") as columns,
        patch.object(service, "_generate_runtime_session_summary") as generate,
    ):
        service._maybe_refresh_runtime_session_summary(
            runtime_session=SimpleNamespace(id=uuid4()),
            usage=_usage(status),
            request_payload={},
            response_payload=None,
            observed_at=datetime.now(timezone.utc),
        )
    columns.assert_not_called()
    generate.assert_not_called()


def test_first_successful_flow_usage_after_failures_generates_initial_summary(
    db_session: Any, test_user: Any
) -> None:
    from preloop.models import models
    from preloop.models.crud import (
        crud_api_usage,
        crud_runtime_session,
        crud_runtime_session_activity,
    )

    account = SimpleNamespace(id=test_user.account_id)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account.id,
        session_source_type="flow",
        session_source_id=str(uuid4()),
        last_activity_at=datetime.now(timezone.utc),
    )
    # A real flow linkage ensures this path does not depend on activity mirrors.
    flow = models.Flow(
        account_id=account.id,
        name="Synthetic summary flow",
        trigger_event_source="manual",
        prompt_template="Review changes",
        agent_type="opencode",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(execution)
    db_session.commit()
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(
            token="test", user=SimpleNamespace(id=uuid4(), account_id=account.id)
        ),
    )
    with (
        patch.object(
            service, "_runtime_session_summary_columns_available", return_value=True
        ),
        patch.object(
            service, "_runtime_session_summary_state", return_value={"summary": None}
        ),
        patch(
            "preloop.services.openai_gateway.crud_ai_model.get_default_active_model",
            return_value=_model(),
        ),
        patch.object(
            service,
            "_generate_runtime_session_summary",
            return_value="Reviewed changes",
        ) as generate,
    ):
        for status in (400, 502, 200):
            usage = crud_api_usage.log_gateway_request(
                db_session,
                endpoint="/openai/v1/responses",
                method="POST",
                status_code=status,
                duration=0.1,
                account_id=account.id,
                runtime_session_id=session.id,
                flow_id=flow.id,
                flow_execution_id=execution.id,
                model_alias="primary",
            )
            service._maybe_refresh_runtime_session_summary(
                runtime_session=session,
                usage=usage,
                request_payload={},
                response_payload={},
                observed_at=datetime.now(timezone.utc),
            )
            assert generate.call_count == (1 if status == 200 else 0)
    db_session.add(
        models.ApiUsage(
            account_id=account.id,
            runtime_session_id=session.id,
            action_type="api",
            endpoint="/health",
            method="GET",
            status_code=200,
            duration=0.1,
        )
    )
    db_session.commit()
    assert (
        crud_runtime_session_activity.count_model_gateway_calls_for_session(
            db_session, account_id=account.id, runtime_session_id=session.id
        )
        == 0
    )
    assert (
        crud_api_usage.count_successful_gateway_calls_for_session(
            db_session, account_id=account.id, runtime_session_id=session.id
        )
        == 1
    )
    assert (
        crud_api_usage.count_successful_gateway_calls_for_session(
            db_session, account_id=uuid4(), runtime_session_id=session.id
        )
        == 0
    )
    assert (
        crud_api_usage.count_successful_gateway_calls_for_session(
            db_session, account_id=account.id, runtime_session_id=uuid4()
        )
        == 0
    )
    db_session.refresh(session)
    assert session.summary == "Reviewed changes"


def test_summary_wait_releases_db_and_preserves_primary_objects_and_identity() -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import QueuePool
    from preloop.models.db.gateway_session import release_gateway_session

    engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0
    )
    db = Session(engine)
    service = OpenAIGatewayService(
        db,
        ModelGatewayAuthContext(
            token="test", user=SimpleNamespace(id=uuid4(), account_id=uuid4())
        ),
        upstream_backend=MagicMock(),
        owns_db_session=True,
    )
    service._wait_model = _model()
    retained = (SimpleNamespace(id="runtime"), _usage())
    service._wait_preserve = retained
    service._client_identity_headers = {"user-agent": "opencode/test"}
    children = []

    def build(
        child: OpenAIGatewayService, *_args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        children.append(child)
        assert child is not service
        assert child._owns_db_session
        assert child._resolved_runtime_session_attempted
        assert not getattr(child, "_client_identity_headers", None)
        return {"model": "openai/test", "messages": kwargs["messages"]}

    def completion(**_kwargs: Any) -> Any:
        assert engine.pool.checkedout() == 0
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="Reviewed changes"))
            ]
        )

    service.upstream_backend.completion.side_effect = completion
    try:
        db.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1
        with (
            patch.object(
                OpenAIGatewayService,
                "_build_completion_kwargs",
                autospec=True,
                side_effect=build,
            ),
            patch.object(
                OpenAIGatewayService,
                "_optimize_request_context",
                autospec=True,
                side_effect=lambda _self, *, messages, payload: (messages, payload),
            ),
            patch(
                "preloop.services.openai_gateway.release_gateway_session",
                wraps=release_gateway_session,
            ) as release,
        ):
            assert _generate(service) == "Reviewed changes"
        assert engine.pool.checkedout() == 0
        assert children[0]._wait_preserve == (*retained, service._wait_model)
        preserved = release.call_args.kwargs["preserve"]
        assert all(
            any(item is original for item in preserved)
            for original in (*retained, service._wait_model)
        )
        assert service._wait_preserve is retained
        assert service._client_identity_headers == {"user-agent": "opencode/test"}
    finally:
        db.close()
        engine.dispose()
