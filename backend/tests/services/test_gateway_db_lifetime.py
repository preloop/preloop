"""Real local pool checks for gateway policy, retry and bookkeeping boundaries."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from preloop.models import models
from preloop.models.crud import crud_ai_model
from preloop.services.model_content_policy import (
    ModelIODecision,
    enforce_request_policy,
    hold_for_model_io_approval,
    wrap_stream_for_response_policy,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.policy.schema import ModelIORule


@pytest.fixture
def local_gateway() -> Iterator[tuple[OpenAIGatewayService, Engine]]:
    engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0
    )
    db = Session(engine)
    service = OpenAIGatewayService(
        db,
        ModelGatewayAuthContext(
            token="synthetic",
            user=SimpleNamespace(id="user", account_id="account"),
        ),
        upstream_backend=MagicMock(),
        owns_db_session=True,
    )
    try:
        yield service, engine
    finally:
        db.close()
        engine.dispose()


def _model() -> models.AIModel:
    return models.AIModel(
        name="Synthetic",
        provider_name="openai",
        model_identifier="synthetic",
        api_key="synthetic",
        meta_data={"gateway": {"enabled": True}},
    )


def _checkout(service: OpenAIGatewayService) -> None:
    service.db.execute(text("SELECT 1"))


def test_caller_owned_transaction_is_untouched() -> None:
    engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0
    )
    db = Session(engine)
    service = OpenAIGatewayService(
        db, ModelGatewayAuthContext(token="synthetic", user=SimpleNamespace())
    )
    try:
        _checkout(service)
        transaction = db.get_transaction()
        service.release_db_for_wait()
        assert db.get_transaction() is transaction
        assert transaction.is_active
        assert engine.pool.checkedout() == 1
    finally:
        db.close()
        engine.dispose()


def test_request_policy_releases_before_detector_and_denies(local_gateway: Any) -> None:
    service, engine = local_gateway
    rule = ModelIORule.model_validate(
        {
            "id": "deny",
            "target": "model.request",
            "conditions": [{"expression": "true", "action": "deny"}],
        }
    )

    def load(*_args: Any) -> list[ModelIORule]:
        _checkout(service)
        return [rule]

    def evaluate(**_kwargs: Any) -> ModelIODecision:
        assert engine.pool.checkedout() == 0
        return ModelIODecision(action="deny", rule_id="deny")

    with (
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            side_effect=load,
        ),
        patch(
            "preloop.services.model_content_policy.evaluate_model_io",
            side_effect=evaluate,
        ),
        pytest.raises(ModelGatewayAPIError, match="Blocked by content policy"),
    ):
        enforce_request_policy(
            service, payload={}, ai_model=_model(), messages=[], provider="openai"
        )
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("has_rules", [False, True])
def test_first_policy_stream_pull_does_not_hold_pool(
    local_gateway: Any, has_rules: bool
) -> None:
    service, engine = local_gateway
    rules = (
        [
            ModelIORule.model_validate(
                {
                    "id": "allow",
                    "target": "model.response",
                    "conditions": [{"expression": "true", "action": "allow"}],
                }
            )
        ]
        if has_rules
        else []
    )

    def load(*_args: Any) -> list[ModelIORule]:
        _checkout(service)
        return rules

    def upstream() -> Iterator[str]:
        assert engine.pool.checkedout() == 0
        with engine.connect():
            pass
        yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        assert engine.pool.checkedout() == 0
        yield "data: [DONE]\n\n"

    with patch(
        "preloop.services.model_content_policy.load_model_io_rules", side_effect=load
    ):
        events = list(
            wrap_stream_for_response_policy(
                upstream(),
                gateway=service,
                payload={},
                ai_model=_model(),
                provider="openai",
            )
        )
    assert len(events) == 2
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_exists", [False, True])
async def test_approval_releases_workflow_lookup_before_wait(
    local_gateway: Any, workflow_exists: bool
) -> None:
    service, engine = local_gateway

    def lookup(*_args: Any) -> str | None:
        _checkout(service)
        return "workflow" if workflow_exists else None

    async def approve(**_kwargs: Any) -> tuple[bool, str]:
        assert engine.pool.checkedout() == 0
        return True, "approved"

    with (
        patch(
            "preloop.services.model_content_policy._resolve_workflow_id",
            side_effect=lookup,
        ),
        patch(
            "preloop.services.approval_helper.require_approval", side_effect=approve
        ) as approval,
    ):
        result = await hold_for_model_io_approval(
            db=service.db,
            account_id="account",
            target="model.request",
            decision=ModelIODecision(action="require_approval"),
            release_after_lookup=service.release_db_for_wait,
        )
    assert result is workflow_exists
    assert approval.call_count == int(workflow_exists)
    assert engine.pool.checkedout() == 0


def test_retry_preparation_prefetch_and_backoff_release_every_time(
    local_gateway: Any,
) -> None:
    service, engine = local_gateway
    attempts = []
    sleeps = []

    def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        _checkout(service)
        return {}

    def completion(**_kwargs: Any) -> Iterator[dict[str, Any]]:
        assert engine.pool.checkedout() == 0
        attempts.append(True)
        if len(attempts) == 1:
            # Even a callback that reads the DB during error handling must
            # not keep it across the subsequent retry sleep.
            _checkout(service)
            raise httpx.ConnectError("synthetic disconnect")

        def chunks() -> Iterator[dict[str, Any]]:
            assert engine.pool.checkedout() == 0
            yield {"choices": [{"delta": {"content": "hello"}}]}
            assert engine.pool.checkedout() == 0

        return chunks()

    def sleep(_delay: float) -> None:
        assert engine.pool.checkedout() == 0
        sleeps.append(True)

    service.upstream_backend.completion.side_effect = completion
    with (
        patch.object(service, "_build_completion_kwargs", side_effect=prepare),
        patch(
            "preloop.services.openai_gateway._upstream_retry_max_attempts",
            return_value=2,
        ),
        patch(
            "preloop.services.openai_gateway._sleep_before_upstream_retry",
            side_effect=sleep,
        ),
    ):
        chunks = list(
            service._open_upstream_stream(
                _model(), messages=[], payload={}, provider="openai"
            )
        )
    assert len(chunks) == 1
    assert len(attempts) == 2
    assert len(sleeps) == 1
    assert service._last_upstream_retry_count == 1
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("failed", [False, True])
def test_accounting_closes_fresh_transaction_after_outcome(
    local_gateway: Any, failed: bool
) -> None:
    service, engine = local_gateway
    seen = []

    def record(**kwargs: Any) -> None:
        _checkout(service)
        seen.append(kwargs["status_code"])
        if failed:
            raise RuntimeError("synthetic bookkeeping failure")

    with patch.object(service, "_record_gateway_request_inner", side_effect=record):
        service._record_gateway_request(
            endpoint="/test",
            endpoint_kind="chat_completions",
            method="POST",
            status_code=200,
            duration=0.1,
            ai_model=_model(),
            requested_model="synthetic",
            response_payload={},
            upstream_response={},
        )
    assert seen == [200]
    assert engine.pool.checkedout() == 0
    assert service.db.get_transaction() is None


def test_owned_prepare_preserves_detached_orm_values_and_writes(
    db_session: Session, test_user: models.User
) -> None:
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="synthetic", user=test_user),
        owns_db_session=True,
    )
    model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "boundary",
            "provider_name": "openai",
            "model_identifier": "synthetic",
            "api_key": "synthetic",
        },
    )
    user_id, model_id = test_user.id, model.id
    test_user.full_name = "Saved at request preparation"
    service.release_db_for_wait(model)
    assert inspect(test_user).detached
    assert inspect(model).detached
    assert test_user.id == user_id
    assert test_user.full_name == "Saved at request preparation"
    assert model.id == model_id
    assert model.credential_type == "api_key"
    assert inspect(model.credentials_secret).detached
    assert model.credentials_secret.backend_type == "local_encrypted"
    assert db_session.get_transaction() is None
    refreshed = service._reattach_for_recording(test_user)
    assert refreshed.full_name == "Saved at request preparation"
    assert refreshed is not test_user
    service.release_db_for_wait(model)


def test_release_gateway_session_commits_preparation_unlike_embedding_guard(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.models.db.gateway_session import release_gateway_session

    user_id = test_user.id
    test_user.full_name = "preparation write must persist"
    pending = (*db_session.new, *db_session.dirty, *db_session.deleted)
    assert test_user in pending
    # HTTP-owned gateway sessions disable expire-on-commit so detached
    # snapshots keep materialized preparation values across this boundary.
    db_session.expire_on_commit = False
    release_gateway_session(db_session, preserve=(test_user,))
    assert inspect(test_user).detached
    assert test_user.full_name == "preparation write must persist"
    refreshed = db_session.get(models.User, user_id)
    assert refreshed is not None
    assert refreshed.full_name == "preparation write must persist"


@pytest.mark.parametrize(
    "transport",
    ["codex", "anthropic", "anthropic_stream", "responses", "responses_stream"],
)
def test_native_provider_bypasses_release_before_http(
    local_gateway: Any, transport: str
) -> None:
    service, engine = local_gateway
    model = _model()
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"id": "response", "output": []}

    def sent(*_args: Any, **_kwargs: Any) -> Any:
        assert engine.pool.checkedout() == 0
        return response

    _checkout(service)
    if transport == "codex":
        with (
            patch.object(
                service,
                "_resolve_openai_codex_credentials",
                return_value=SimpleNamespace(
                    value="token", payload={"account_id": "account"}
                ),
            ),
            patch.object(service, "_build_openai_codex_payload", return_value={}),
            patch.object(service, "_aggregate_codex_sse_stream", return_value={}),
            patch(
                "preloop.services.openai_gateway.urllib_request.urlopen",
                side_effect=sent,
            ),
        ):
            service._create_openai_codex_response(model, {})
    elif transport.startswith("anthropic"):
        client = MagicMock()
        client.post.side_effect = sent
        client.send.side_effect = sent
        with patch(
            "preloop.services.openai_gateway._anthropic_passthrough_http_client",
            return_value=client,
        ):
            if transport == "anthropic":
                service._anthropic_oauth_passthrough_complete(
                    url="https://synthetic.invalid", headers={}, body={}
                )
            else:
                service._open_anthropic_oauth_passthrough_stream(
                    url="https://synthetic.invalid", headers={}, body={}
                )
    else:
        client = MagicMock()
        client.post.side_effect = sent
        client.send.side_effect = sent
        with (
            patch.object(
                service,
                "_prepare_openai_responses_passthrough",
                return_value=("https://synthetic.invalid", {}, {}),
            ),
            patch(
                "preloop.services.openai_gateway._openai_passthrough_http_client",
                return_value=client,
            ),
        ):
            if transport == "responses":
                service._create_openai_responses_passthrough(model, {})
            else:
                service._open_openai_responses_passthrough_stream(model, {})
    assert engine.pool.checkedout() == 0


def test_oauth_rotation_survives_repeated_detached_credential_phases(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.secret_service import SecretService

    model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "rotating",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-5.4",
            "credential_type": "oauth_openai_codex",
            "credential_payload": {
                "access": "old",
                "refresh": "single-use",
                "expires": 1,
                "account_id": "account",
            },
        },
    )
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="synthetic", user=test_user),
        owns_db_session=True,
    )
    secret_service = SecretService()

    def rotate(_token: str) -> dict[str, Any]:
        # The OAuth row lock deliberately spans this bounded 30-second HTTP
        # call. The completion/stream boundary happens after persisted rotation.
        assert db_session.in_transaction()
        return {
            "access": "new",
            "refresh": "rotated",
            "expires": 1893456000000,
            "account_id": "account",
        }

    with (
        patch.object(
            secret_service, "_refresh_openai_codex_token", side_effect=rotate
        ) as refresh,
        patch(
            "preloop.services.openai_gateway.get_secret_service",
            return_value=secret_service,
        ),
    ):
        service.release_db_for_wait(model)
        first = service._resolve_openai_codex_credentials(model)
        service.release_db_for_wait(model)
        second = service._resolve_openai_codex_credentials(model)
        service.release_db_for_wait(model)
    assert first.value == second.value == "new"
    assert first.payload["refresh"] == second.payload["refresh"] == "rotated"
    refresh.assert_called_once_with("single-use")
    assert db_session.get_transaction() is None


def test_failed_materialization_still_returns_pool_connection(
    local_gateway: Any,
) -> None:
    service, engine = local_gateway
    _checkout(service)
    with (
        patch(
            "preloop.models.db.gateway_session.inspect",
            side_effect=RuntimeError("synthetic snapshot failure"),
        ),
        pytest.raises(RuntimeError, match="synthetic snapshot failure"),
    ):
        service.release_db_for_wait(_model())
    assert engine.pool.checkedout() == 0
