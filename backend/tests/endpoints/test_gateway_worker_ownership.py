"""Gateway workers own short sessions independently of HTTP request teardown."""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Mapping
from dataclasses import fields, is_dataclass
from threading import Event, Lock, get_ident
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeout
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.db.session import _database_pool_kwargs, get_db_session
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from tests.endpoints.test_gateway_pool_lifetime import (
    GatewayPoolFixture,
    HeldProvider,
    _await_provider_event,
    _request,
    gateway_pool as _gateway_pool,
)


# Reuse the committed synthetic account and its existing cleanup.
gateway_pool = _gateway_pool


@pytest.fixture
def worker_pool(
    gateway_pool: GatewayPoolFixture,
) -> Generator[tuple[GatewayPoolFixture, list[Session]], None, None]:
    """Extend the existing one-slot rig with a separate two-slot request pool.

    The third concurrent stream must *queue* for a slot while the first two
    finish auth/prep and release. Production waits
    ``DATABASE_POOL_TIMEOUT`` (5s). A 0.5s timeout was shorter than
    Responses auth/prep on CI, so the third request died before any worker
    reached the held provider, and the mini-app rendered that as an opaque
    HTTP 500.
    """
    engine = create_engine(
        gateway_pool.engine.url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=_database_pool_kwargs()["pool_timeout"],
    )
    request_sessions: list[Session] = []

    def request_db() -> Generator[Session, None, None]:
        with Session(engine) as db:
            request_sessions.append(db)
            yield db

    gateway_pool.app.dependency_overrides[get_db_session] = request_db

    async def gateway_error(_request: Any, exc: ModelGatewayAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(),
            headers=exc.response_headers(),
        )

    async def pool_timeout(_request: Any, exc: SQLAlchemyPoolTimeout) -> JSONResponse:
        # Match create_app(): a saturated pool is 503, not a swallowed 500.
        return JSONResponse(
            status_code=503,
            content={
                "detail": ("Database connections are saturated. Please retry shortly.")
            },
            headers={"Retry-After": "1"},
        )

    # Match the application error renderer without starting external services.
    gateway_pool.app.add_exception_handler(ModelGatewayAPIError, gateway_error)
    gateway_pool.app.add_exception_handler(SQLAlchemyPoolTimeout, pool_timeout)
    try:
        yield (
            GatewayPoolFixture(
                gateway_pool.app,
                engine,
                gateway_pool.account_id,
                gateway_pool.token,
            ),
            request_sessions,
        )
    finally:
        engine.dispose()


def test_worker_pool_queues_for_the_production_checkout_timeout(
    worker_pool: tuple[GatewayPoolFixture, list[Session]],
) -> None:
    """The third stream must wait as long as production, not fail in 0.5s."""
    rig, _sessions = worker_pool
    assert rig.engine.pool.timeout() == _database_pool_kwargs()["pool_timeout"]


class ThreeHeldProviders(HeldProvider):
    """Count three provider calls while preserving independently held phases."""

    def __init__(self) -> None:
        super().__init__()
        self.all_handshakes = Event()
        self.all_streams = Event()

    def completion(self, **kwargs: Any) -> Any:
        assert kwargs["stream"] is True
        with self.lock:
            self.handshakes += 1
            self.first_handshake.set()
            if self.handshakes == 3:
                self.all_handshakes.set()
        assert self.release_handshakes.wait(20), "fake provider was not released"
        return self.chunks()

    def chunks(self) -> Generator[dict[str, Any], None, None]:
        yield {
            "id": "synthetic-completion",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {"content": "Hello"}}],
        }
        with self.lock:
            self.streams += 1
            if self.streams == 3:
                self.all_streams.set()
        assert self.release_streams.wait(20), "fake stream was not released"
        yield {
            "id": "synthetic-completion",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }


def _probe_both_slots(engine: Engine) -> None:
    """Both slots remain available, with clean transactions on checkout."""
    with engine.connect() as first, engine.connect() as second:
        assert not first.in_transaction()
        assert not second.in_transaction()
        assert first.execute(text("SELECT 1")).scalar_one() == 1
        assert second.execute(text("SELECT 1")).scalar_one() == 1


def _assert_scalar_context(value: Any) -> None:
    """Recursively reject mapped instances, including detached credentials."""
    assert inspect(value, raiseerr=False) is None, (
        f"Stream retained mapped {type(value).__name__}"
    )
    if is_dataclass(value) and not isinstance(value, type):
        assert value.__dataclass_params__.frozen, type(value).__name__
        for item in fields(value):
            _assert_scalar_context(getattr(value, item.name))
    elif isinstance(value, Mapping):
        for item in value.values():
            _assert_scalar_context(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _assert_scalar_context(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "gemini"])
async def test_three_streams_keep_two_slot_pool_and_worker_sessions_independent(
    worker_pool: tuple[GatewayPoolFixture, list[Session]], protocol: str
) -> None:
    """Real authentication, preparation and accounting outlive request handles."""
    rig, request_sessions = worker_pool
    provider = ThreeHeldProviders()
    path, payload = _request(protocol)
    headers = {
        "Authorization": f"Bearer {rig.token}",
        "anthropic-version": "2023-06-01",
    }
    seen_sessions: dict[Session, set[int]] = {}
    accounting_sessions: list[Session] = []
    wait_contexts: list[tuple[Any, tuple[Any, ...]]] = []
    recording_contexts: list[tuple[Any, dict[str, Any]]] = []
    audit_lock = Lock()

    def began(db: Session, transaction: Any, connection: Any) -> None:
        if connection.engine is rig.engine:
            with audit_lock:
                seen_sessions.setdefault(db, set()).add(get_ident())

    def flushing(db: Session, flush_context: Any, instances: Any) -> None:
        if db.get_bind() is rig.engine and any(
            isinstance(row, models.ApiUsage) for row in db.new
        ):
            accounting_sessions.append(db)

    original_release = OpenAIGatewayService.release_db_for_wait
    original_record = OpenAIGatewayService._record_gateway_request

    def record(service: OpenAIGatewayService, **kwargs: Any) -> None:
        recording_contexts.append((service.auth_context, kwargs))
        original_record(service, **kwargs)

    def release(service: OpenAIGatewayService, *instances: Any) -> None:
        original_release(service, *instances)
        wait_contexts.append((service.auth_context, instances))

    async def assert_idle(client: httpx.AsyncClient) -> set[Session]:
        assert rig.engine.pool.checkedout() == 0
        assert all(not db.in_transaction() for db in seen_sessions)
        await asyncio.to_thread(_probe_both_slots, rig.engine)
        # A fully authenticated independent request must still perform DB reads.
        response = await asyncio.wait_for(
            client.get("/openai/v1/models", headers=headers), timeout=5
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"][0]["id"] == "example-model"
        assert rig.engine.pool.checkedout() == 0
        return set(seen_sessions)

    event.listen(Session, "after_begin", began)
    event.listen(Session, "before_flush", flushing)
    try:
        with (
            patch.object(OpenAIGatewayService, "release_db_for_wait", release),
            patch.object(OpenAIGatewayService, "_record_gateway_request", record),
            patch(
                "preloop.services.openai_gateway.litellm.completion",
                side_effect=provider.completion,
            ),
            patch("preloop.services.openai_gateway.emit_account_event"),
            patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
            patch(
                "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
            ),
            patch(
                "preloop.services.openai_gateway.GatewayUsageSearchService.build_index_document"
            ),
            patch("preloop.services.openai_gateway.get_gateway_usage_index_queue"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rig.app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                tasks = [
                    asyncio.create_task(
                        client.post(path, json=payload, headers=headers)
                    )
                    for _ in range(3)
                ]
                try:
                    await _await_provider_event(
                        provider.all_handshakes,
                        tasks,
                        message="three requests did not reach the held provider",
                        timeout=20,
                    )
                    await assert_idle(client)
                    provider.release_handshakes.set()
                    await _await_provider_event(
                        provider.all_streams,
                        tasks,
                        message="three responses did not reach held stream pulls",
                        timeout=20,
                    )
                    preparation_sessions = await assert_idle(client)
                    assert all(not task.done() for task in tasks)
                    # Inspect values while real provider callbacks are suspended.
                    for auth, instances in wait_contexts:
                        _assert_scalar_context(auth)
                        for instance in instances:
                            _assert_scalar_context(instance)
                finally:
                    provider.release_handshakes.set()
                    provider.release_streams.set()
                    responses = await asyncio.gather(*tasks)
        assert [response.status_code for response in responses] == [200, 200, 200]
        assert all("Hello" in response.text for response in responses)
        assert rig.engine.pool.checkedout() == 0
        assert all(not db.in_transaction() for db in seen_sessions)
        assert len(recording_contexts) == 3
        for auth, arguments in recording_contexts:
            _assert_scalar_context(auth)
            _assert_scalar_context(arguments)
        assert len(accounting_sessions) == 3
        assert len(set(accounting_sessions)) == 3
        assert set(accounting_sessions).isdisjoint(preparation_sessions)
        assert set(accounting_sessions).isdisjoint(request_sessions)
        assert all(len(threads) == 1 for threads in seen_sessions.values())
        with Session(rig.engine) as db:
            usage = db.scalars(
                select(models.ApiUsage).where(
                    models.ApiUsage.account_id == rig.account_id
                )
            ).all()
            assert len(usage) == 3
            assert all(
                row.status_code == 200 and row.total_tokens == 4 for row in usage
            )
            assert all(row.auth_subject_type == "user_token" for row in usage)
            assert all(row.estimated_cost > 0 for row in usage)
    finally:
        event.remove(Session, "after_begin", began)
        event.remove(Session, "before_flush", flushing)


@pytest.mark.asyncio
@pytest.mark.parametrize("accounting_failure", [False, True])
async def test_repeated_cancellation_drains_accounting_owned_session(
    worker_pool: tuple[GatewayPoolFixture, list[Session]], accounting_failure: bool
) -> None:
    """Request teardown cannot close or reuse a still-running accounting worker."""
    rig, request_sessions = worker_pool
    provider = HeldProvider()
    provider.release_handshakes.set()
    provider.release_streams.set()
    entered = Event()
    release = Event()
    accounting: list[Session] = []
    closed: list[Session] = []
    preparation: list[Session] = []
    original_record = OpenAIGatewayService._record_gateway_request_inner
    original_close = Session.close

    def began(db: Session, transaction: Any, connection: Any) -> None:
        if connection.engine is rig.engine:
            assert db not in closed, "a closed Session was reused by another phase"
            if not entered.is_set():
                preparation.append(db)

    def close(db: Session) -> None:
        closed.append(db)
        original_close(db)

    def record(service: OpenAIGatewayService, **kwargs: Any) -> None:
        db = service.db
        accounting.append(db)
        # Force a real transaction so cleanup must release actual pool capacity.
        assert db.execute(text("SELECT 1")).scalar_one() == 1
        entered.set()
        assert release.wait(20), "accounting worker was not released"
        if accounting_failure:
            raise SQLAlchemyError("synthetic accounting failure")
        original_record(service, **kwargs)

    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        scope["asgi"]["spec_version"] = "2.4"
        await rig.app(scope, receive, send)

    event.listen(Session, "after_begin", began)
    try:
        with (
            patch.object(Session, "close", close),
            patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert"),
            patch.object(OpenAIGatewayService, "_record_gateway_request_inner", record),
            patch(
                "preloop.services.openai_gateway.litellm.completion",
                side_effect=provider.completion,
            ),
            patch("preloop.services.openai_gateway.emit_account_event"),
            patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
            patch(
                "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
            ),
            patch(
                "preloop.services.openai_gateway.GatewayUsageSearchService.build_index_document"
            ),
            patch("preloop.services.openai_gateway.get_gateway_usage_index_queue"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi_app), base_url="http://test"
            ) as client:
                path, payload = _request("chat")
                task = asyncio.create_task(
                    client.post(
                        path,
                        json=payload,
                        headers={"Authorization": f"Bearer {rig.token}"},
                    )
                )
                try:
                    await _await_provider_event(
                        entered, [task], message="accounting worker did not start"
                    )
                    db = accounting[0]
                    assert db not in request_sessions
                    # Exclude accounting's own forced SELECT from this audit.
                    assert db not in preparation[:-1]
                    assert db not in closed
                    assert db.in_transaction()
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done(), "request did not drain accounting worker"
                    assert db not in closed, "request cleanup closed worker's session"
                    assert rig.engine.pool.checkedout() == 1
                    with rig.engine.connect() as probe:
                        assert probe.execute(text("SELECT 1")).scalar_one() == 1
                finally:
                    release.set()
                    # Also drain on an early assertion failure.
                    try:
                        await task
                    except asyncio.CancelledError:
                        # Expected: the request task was cancelled to prove
                        # accounting keeps its Session through drain.
                        pass
        assert len(accounting) == 1
        assert accounting[0] in closed
        assert not accounting[0].in_transaction()
        assert rig.engine.pool.checkedout() == 0
        await asyncio.to_thread(_probe_both_slots, rig.engine)
        with Session(rig.engine) as db:
            rows = db.scalars(
                select(models.ApiUsage).where(
                    models.ApiUsage.account_id == rig.account_id
                )
            ).all()
            assert len(rows) == (0 if accounting_failure else 1)
            if rows:
                assert rows[0].status_code == 200
                assert rows[0].total_tokens == 4
    finally:
        event.remove(Session, "after_begin", began)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_timeout", "initial_policy"])
async def test_http_failure_closes_every_owned_database_phase(
    worker_pool: tuple[GatewayPoolFixture, list[Session]], failure: str
) -> None:
    """Failures before streaming starts close fresh workers and preserve capacity."""
    from preloop.services.model_content_policy import load_model_io_rules

    rig, request_sessions = worker_pool
    observed: set[Session] = set()
    closed: set[Session] = set()
    policy_sessions: list[Session] = []
    accounting_sessions: list[Session] = []
    original_close = Session.close

    def began(db: Session, transaction: Any, connection: Any) -> None:
        if connection.engine is rig.engine:
            assert db not in closed, "a closed worker Session was reused"
            observed.add(db)

    def flushing(db: Session, flush_context: Any, instances: Any) -> None:
        if db.get_bind() is rig.engine and any(
            isinstance(row, models.ApiUsage) for row in db.new
        ):
            accounting_sessions.append(db)

    def close(db: Session) -> None:
        closed.add(db)
        original_close(db)

    def policy(db: Session, account_id: Any) -> Any:
        policy_sessions.append(db)
        rules = load_model_io_rules(db, account_id)
        if failure == "initial_policy":
            assert db.in_transaction()
            raise SQLAlchemyError("synthetic policy store unavailable")
        return rules

    def timeout(**kwargs: Any) -> Any:
        assert rig.engine.pool.checkedout() == 0
        assert all(not db.in_transaction() for db in observed)
        _probe_both_slots(rig.engine)
        raise httpx.ReadTimeout("synthetic provider timed out")

    event.listen(Session, "after_begin", began)
    event.listen(Session, "before_flush", flushing)
    try:
        with (
            patch.object(Session, "close", close),
            patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert"),
            patch(
                "preloop.services.model_content_policy.load_model_io_rules",
                side_effect=policy,
            ),
            patch(
                "preloop.services.openai_gateway.litellm.completion",
                side_effect=timeout,
            ) as provider,
            patch(
                "preloop.services.openai_gateway._upstream_retry_max_attempts",
                return_value=1,
            ),
            patch("preloop.services.openai_gateway.emit_account_event"),
            patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
            patch(
                "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
            ),
            patch(
                "preloop.services.openai_gateway.GatewayUsageSearchService.build_index_document"
            ),
            patch("preloop.services.openai_gateway.get_gateway_usage_index_queue"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rig.app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                path, payload = _request("chat")
                response = await client.post(
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {rig.token}"},
                )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == (
            "content_policy_unavailable" if failure == "initial_policy" else "network"
        )
        assert provider.call_count == (0 if failure == "initial_policy" else 1)
        assert policy_sessions
        assert observed.isdisjoint(request_sessions)
        assert observed.issubset(closed)
        assert all(not db.in_transaction() for db in observed)
        assert rig.engine.pool.checkedout() == 0
        await asyncio.to_thread(_probe_both_slots, rig.engine)
        assert set(accounting_sessions).isdisjoint(policy_sessions)
        with Session(rig.engine) as db:
            rows = db.scalars(
                select(models.ApiUsage).where(
                    models.ApiUsage.account_id == rig.account_id
                )
            ).all()
            # Preserve one local failure fact, including fail-closed policy
            # errors before any provider attempt.
            assert len(rows) == 1
            assert rows[0].status_code == 503
    finally:
        event.remove(Session, "after_begin", began)
        event.remove(Session, "before_flush", flushing)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["codex", "responses", "anthropic", "fallback"])
async def test_native_transports_keep_credential_orm_inside_worker(
    worker_pool: tuple[GatewayPoolFixture, list[Session]], native: str
) -> None:
    """Native routes and Responses fallback carry only snapshots into callbacks."""
    import io
    import json
    from unittest.mock import MagicMock

    from preloop.models.crud import crud_ai_model
    from preloop.services.model_content_policy import enforce_request_policy
    from preloop.services.secret_service import ResolvedModelCredentials

    rig, request_sessions = worker_pool
    with Session(rig.engine) as db:
        model = db.scalars(
            select(models.AIModel).where(models.AIModel.account_id == rig.account_id)
        ).one()
        metadata = dict(model.meta_data)
        metadata["gateway"] = {**metadata["gateway"], "responses_api": "native"}
        crud_ai_model.update(
            db,
            db_obj=model,
            obj_in={
                "provider_name": {
                    "codex": "openai-codex",
                    "anthropic": "anthropic",
                }.get(native, "openai"),
                "api_endpoint": f"https://{rig.account_id}.example.com/v1",
                "meta_data": metadata,
            },
        )

    services: list[OpenAIGatewayService] = []
    credential_sessions: list[Session] = []
    accounting_sessions: list[Session] = []
    closed: set[Session] = set()
    callback_contexts: list[tuple[Any, Any]] = []
    transport_calls: list[str] = []
    codex_contexts: list[Any] = []
    original_codex_credentials = OpenAIGatewayService._resolve_openai_codex_credentials
    original_init = OpenAIGatewayService.__init__
    original_close = Session.close
    original_record = OpenAIGatewayService._record_gateway_request

    def initialize(service: OpenAIGatewayService, *args: Any, **kwargs: Any) -> None:
        original_init(service, *args, **kwargs)
        services.append(service)

    def close(db: Session) -> None:
        closed.add(db)
        original_close(db)

    def credentials(model: models.AIModel, *, db: Session, **kwargs: Any) -> Any:
        # Only the vault is fake. The native credential adapter must reload a
        # real account-scoped model inside its fresh database worker.
        state = inspect(model)
        assert state.session is db
        assert db not in request_sessions
        assert db not in closed
        credential_sessions.append(db)
        return ResolvedModelCredentials(
            credential_type={
                "codex": "oauth_openai_codex",
                "anthropic": "oauth_anthropic_claude_code",
            }.get(native, "api_key"),
            backend_type="local",
            value="synthetic-provider-access-token",
            payload={
                "account_id": "synthetic-provider-account",
                "refresh_token": "synthetic-vault-refresh-token",
                "id_token": "synthetic-vault-id-token",
            },
        )

    def codex_credentials(service: OpenAIGatewayService, model: Any) -> Any:
        value = original_codex_credentials(service, model)
        codex_contexts.append(value)
        return value

    def policy(service: OpenAIGatewayService, **kwargs: Any) -> None:
        callback_contexts.append((service.auth_context, kwargs["ai_model"]))
        enforce_request_policy(service, **kwargs)

    def record(service: OpenAIGatewayService, **kwargs: Any) -> None:
        callback_contexts.append((service.auth_context, kwargs["ai_model"]))
        original_record(service, **kwargs)

    def flushing(db: Session, flush_context: Any, instances: Any) -> None:
        if db.get_bind() is rig.engine and any(
            isinstance(row, models.ApiUsage) for row in db.new
        ):
            accounting_sessions.append(db)

    def assert_provider_boundary() -> None:
        assert len(services) == 1
        service = services[0]
        assert service._db is None
        assert credential_sessions
        assert set(credential_sessions).issubset(closed)
        assert all(not db.in_transaction() for db in credential_sessions)
        _assert_scalar_context(service.auth_context)
        if native == "codex":
            assert len(codex_contexts) == 1
            value = codex_contexts[0]
            _assert_scalar_context(value)
            assert value.payload == {"account_id": "synthetic-provider-account"}
            assert not hasattr(value, "refresh_token")
            assert "synthetic-vault-refresh-token" not in repr(vars(value))
            assert "synthetic-vault-id-token" not in repr(vars(value))
        for auth, model in callback_contexts:
            _assert_scalar_context(auth)
            _assert_scalar_context(model)
        assert rig.engine.pool.checkedout() == 0
        _probe_both_slots(rig.engine)

    response_payload = {
        "id": "synthetic-native-response",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "id": "synthetic-message",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }

    def http_send(request: httpx.Request) -> httpx.Response:
        assert_provider_boundary()
        transport_calls.append("httpx")
        if native == "fallback":
            return httpx.Response(404, json={"error": "synthetic absent Responses API"})
        body = response_payload
        if native == "anthropic":
            body = {
                "id": "synthetic-message",
                "type": "message",
                "role": "assistant",
                "model": "example-model",
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        return httpx.Response(200, json=body)

    def urlopen(*args: Any, **kwargs: Any) -> Any:
        assert_provider_boundary()
        transport_calls.append("urllib")
        event_payload = {"type": "response.completed", "response": response_payload}
        body = io.BytesIO(
            f"event: response.completed\ndata: {json.dumps(event_payload)}\n\n".encode()
        )
        body.headers = {}  # type: ignore[attr-defined]
        return body

    def fallback(**kwargs: Any) -> Any:
        assert native == "fallback", "native route unexpectedly transcoded"
        assert_provider_boundary()
        transport_calls.append("litellm")
        return {
            "id": "synthetic-fallback-response",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    vault = MagicMock()
    vault.resolve_ai_model_credentials.side_effect = credentials
    event.listen(Session, "before_flush", flushing)
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(http_send)) as native_client,
            patch.object(OpenAIGatewayService, "__init__", initialize),
            patch.object(
                OpenAIGatewayService,
                "_resolve_openai_codex_credentials",
                codex_credentials,
            ),
            patch.object(OpenAIGatewayService, "_record_gateway_request", record),
            patch.object(Session, "close", close),
            patch(
                "preloop.services.openai_gateway.get_secret_service", return_value=vault
            ),
            patch(
                "preloop.services.openai_gateway.enforce_request_policy",
                side_effect=policy,
            ),
            patch(
                "preloop.services.openai_gateway._openai_passthrough_http_client",
                return_value=native_client,
            ),
            patch(
                "preloop.services.openai_gateway._anthropic_passthrough_http_client",
                return_value=native_client,
            ),
            patch(
                "preloop.services.openai_gateway.urllib_request.urlopen",
                side_effect=urlopen,
            ),
            patch(
                "preloop.services.openai_gateway.litellm.completion",
                side_effect=fallback,
            ),
            patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert"),
            patch("preloop.services.openai_gateway.emit_account_event"),
            patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
            patch(
                "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
            ),
            patch(
                "preloop.services.openai_gateway.GatewayUsageSearchService.build_index_document"
            ),
            patch("preloop.services.openai_gateway.get_gateway_usage_index_queue"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rig.app, raise_app_exceptions=True),
                base_url="http://test",
            ) as client:
                path, payload = _request(
                    "anthropic" if native == "anthropic" else "responses"
                )
                payload["stream"] = False
                response = await client.post(
                    path,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {rig.token}",
                        "anthropic-version": "2023-06-01",
                    },
                )
        assert response.status_code == 200, response.text
        assert "Hello" in response.text
        assert transport_calls == (
            ["urllib"]
            if native == "codex"
            else ["httpx", "litellm"]
            if native == "fallback"
            else ["httpx"]
        )
        assert len(callback_contexts) == 2
        for auth, model in callback_contexts:
            _assert_scalar_context(auth)
            _assert_scalar_context(model)
            assert model.credentials_secret is None
            assert model.api_key is None
        assert len(accounting_sessions) == 1
        assert set(accounting_sessions).isdisjoint(credential_sessions)
        assert set(accounting_sessions).isdisjoint(request_sessions)
        assert set(accounting_sessions).issubset(closed)
        assert rig.engine.pool.checkedout() == 0
        with Session(rig.engine) as db:
            usage = db.scalars(
                select(models.ApiUsage).where(
                    models.ApiUsage.account_id == rig.account_id
                )
            ).one()
            assert usage.status_code == 200
            assert usage.total_tokens == 4
    finally:
        event.remove(Session, "before_flush", flushing)
