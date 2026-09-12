"""Public gateway authentication must not hold DB capacity during provider I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import create_access_token
from preloop.api.endpoints import anthropic_gateway, gemini_gateway, openai_gateway
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_runtime_session,
    crud_user,
)
from preloop.models.db.session import get_db_session


@dataclass
class GatewayPoolFixture:
    app: FastAPI
    engine: Engine
    account_id: UUID
    token: str = field(repr=False)


@pytest.fixture
def gateway_pool(db_engine: Engine) -> Generator[GatewayPoolFixture, None, None]:
    """Seed committed synthetic rows visible to separate HTTP request sessions."""
    engine = create_engine(db_engine.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    suffix = uuid4().hex
    with Session(engine) as db:
        account = crud_account.create(
            db,
            obj_in={"organization_name": f"Gateway pool {suffix}", "is_active": True},
        )
        account_id = account.id
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account_id,
                "email": f"pool-{suffix}@example.com",
                "username": f"pool-{suffix}",
                "hashed_password": "synthetic-unused-password",
                "is_active": True,
                "email_verified": True,
                "user_source": "local",
            },
        )
        token = create_access_token({"sub": str(user.id)})
        crud_ai_model.create_with_account(
            db,
            account_id=account_id,
            obj_in={
                "name": "Synthetic gateway model",
                "provider_name": "openai",
                "model_identifier": "gpt-5",
                "api_key": "synthetic-unused-provider-key",
                "is_default": True,
                "meta_data": {
                    "gateway": {
                        "enabled": True,
                        "model_alias": "example-model",
                        "provider_adapter": "preloop",
                        "responses_api": "transcode",
                    },
                    "pricing": {
                        "input_price_per_1k": 0.01,
                        "output_price_per_1k": 0.02,
                    },
                },
            },
        )
        db.commit()

    def request_db() -> Generator[Session, None, None]:
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(openai_gateway.router, prefix="/openai/v1")
    app.include_router(anthropic_gateway.router, prefix="/anthropic/v1")
    app.include_router(gemini_gateway.router, prefix="/gemini/v1beta")
    # Keep all real auth dependencies and route/service constructors. Only the
    # database dependency is redirected to the isolated one-connection pool.
    app.dependency_overrides[get_db_session] = request_db
    try:
        yield GatewayPoolFixture(app, engine, account_id, token)
    finally:
        # These committed records belong exclusively to this parametrized test.
        with Session(db_engine) as db:
            db.execute(
                delete(models.ApiUsage).where(models.ApiUsage.account_id == account_id)
            )
            db.execute(
                delete(models.AIModel).where(models.AIModel.account_id == account_id)
            )
            db.execute(delete(models.Account).where(models.Account.id == account_id))
            db.commit()
        engine.dispose()


def _request(protocol: str) -> tuple[str, dict[str, Any]]:
    if protocol == "responses":
        return "/openai/v1/responses", {
            "model": "example-model",
            "input": "Hello",
            "stream": True,
        }
    if protocol == "gemini":
        return "/gemini/v1beta/models/example-model:streamGenerateContent", {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}]
        }
    payload = {
        "model": "example-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
        "max_tokens": 16,
    }
    return (
        "/anthropic/v1/messages"
        if protocol == "anthropic"
        else "/openai/v1/chat/completions"
    ), payload


class HeldProvider:
    """Hold both the handshake and later chunk reads independently."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.handshakes = 0
        self.streams = 0
        self.first_handshake = Event()
        self.both_handshakes = Event()
        self.both_streams = Event()
        self.release_handshakes = Event()
        self.release_streams = Event()

    def completion(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        assert kwargs["stream"] is True
        with self.lock:
            self.handshakes += 1
            self.first_handshake.set()
            if self.handshakes == 2:
                self.both_handshakes.set()
        assert self.release_handshakes.wait(10), "test did not release fake provider"
        return self.chunks()

    def chunks(self) -> Iterator[dict[str, Any]]:
        yield {
            "id": "synthetic-completion",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {"content": "Hello"}}],
        }
        with self.lock:
            self.streams += 1
            if self.streams == 2:
                self.both_streams.set()
        assert self.release_streams.wait(10), "test did not release fake stream"
        yield {
            "id": "synthetic-completion",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }


def _probe_pool(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


async def _await_provider_event(
    event: Event,
    tasks: list[asyncio.Task[httpx.Response]],
    *,
    message: str,
    timeout: float = 10,
) -> None:
    """Wait until a provider event fires, or fail with the HTTP outcome."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        for task in tasks:
            if not task.done():
                continue
            if task.cancelled():
                raise AssertionError(f"{message}; request was cancelled")
            exc = task.exception()
            if exc is not None:
                raise AssertionError(f"{message}; request raised {exc!r}") from exc
            response = task.result()
            raise AssertionError(
                f"{message}; request finished HTTP {response.status_code}: "
                f"{response.text[:400]!r}"
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"{message}; request still in flight")
        await asyncio.to_thread(event.wait, min(0.1, remaining))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_concurrent_authenticated_streams_release_request_pool(
    gateway_pool: GatewayPoolFixture, protocol: str, asgi_spec: str
) -> None:
    """Two real HTTP auth/preflight paths fit while both provider calls wait."""
    provider = HeldProvider()
    path, payload = _request(protocol)
    headers = {
        "Authorization": f"Bearer {gateway_pool.token}",
        "anthropic-version": "2023-06-01",
    }

    async def asgi_app(scope, receive, send):
        scope["asgi"]["spec_version"] = asgi_spec
        await gateway_pool.app(scope, receive, send)

    transport = httpx.ASGITransport(app=asgi_app, raise_app_exceptions=False)
    with (
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            side_effect=provider.completion,
        ),
        # External observers are irrelevant here; usage CRUD remains real.
        patch("preloop.services.openai_gateway.emit_account_event"),
        patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
        patch(
            "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
        ),
        patch(
            "preloop.services.openai_gateway.GatewayUsageSearchService.auto_index_interaction"
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            tasks = [
                asyncio.create_task(client.post(path, json=payload, headers=headers))
            ]
            try:
                await _await_provider_event(
                    provider.first_handshake,
                    tasks,
                    message="provider handshake never started",
                )
                tasks.append(
                    asyncio.create_task(
                        client.post(path, json=payload, headers=headers)
                    )
                )
                await _await_provider_event(
                    provider.both_handshakes,
                    tasks,
                    message=(
                        "request/auth DB connection remained checked out during "
                        f"provider handshake; provider calls={provider.handshakes}"
                    ),
                )
                await asyncio.to_thread(_probe_pool, gateway_pool.engine)
                provider.release_handshakes.set()
                await _await_provider_event(
                    provider.both_streams,
                    tasks,
                    message=(
                        "stream iteration or policy lookup reacquired and "
                        "retained request DB"
                    ),
                )
                assert all(not task.done() for task in tasks)
                await asyncio.to_thread(_probe_pool, gateway_pool.engine)
            finally:
                provider.release_handshakes.set()
                provider.release_streams.set()
                responses = await asyncio.gather(*tasks)
    assert [response.status_code for response in responses] == [200, 200]
    assert all("Hello" in response.text for response in responses)
    assert gateway_pool.engine.pool.checkedout() == 0
    with Session(gateway_pool.engine) as db:
        rows = db.scalars(
            select(models.ApiUsage).where(
                models.ApiUsage.account_id == gateway_pool.account_id
            )
        ).all()
        assert len(rows) == 2
        assert all(row.status_code == 200 and row.total_tokens == 4 for row in rows)
        assert all(row.auth_subject_type == "user_token" for row in rows)


@pytest.mark.asyncio
async def test_nested_session_summary_releases_pool_and_preserves_accounting(
    gateway_pool: GatewayPoolFixture,
) -> None:
    """A summary provider wait inside real recording preserves its outer rows."""
    from datetime import datetime, timezone

    with Session(gateway_pool.engine) as db:
        user = db.scalars(
            select(models.User).where(models.User.account_id == gateway_pool.account_id)
        ).one()
        runtime_session = crud_runtime_session.upsert_by_source(
            db,
            account_id=gateway_pool.account_id,
            session_source_type="custom",
            session_source_id=uuid4().hex,
            started_at=datetime.now(timezone.utc),
        )
        runtime_session_id = runtime_session.id
        _, token = crud_api_key.create_runtime_key(
            db,
            name="Synthetic nested summary",
            account_id=gateway_pool.account_id,
            user_id=user.id,
            context_data={"runtime_session_id": str(runtime_session_id)},
        )
    entered = Event()
    release = Event()
    calls = []

    def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        is_summary = kwargs["messages"][0].get("role") == "system"
        if is_summary:
            entered.set()
            assert release.wait(10)
        if is_summary:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Example session summary")
                    )
                ]
            )
        return {
            "id": "synthetic-main",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Example session summary" if is_summary else "Hello",
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    with (
        patch(
            "preloop.services.openai_gateway.litellm.completion", side_effect=completion
        ),
        patch("preloop.services.openai_gateway.emit_account_event") as account_events,
        patch("preloop.services.openai_gateway._emit_account_event_nonblocking"),
        patch(
            "preloop.services.openai_gateway.ModelGatewayEventEmitter.emit_for_usage"
        ),
        patch(
            "preloop.services.openai_gateway.GatewayUsageSearchService.auto_index_interaction"
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=gateway_pool.app, raise_app_exceptions=False
            ),
            base_url="http://test",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": "example-model",
                        "messages": [{"role": "user", "content": "Hello"}],
                    },
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3), (
                    "nested summary provider was never called"
                )
                assert not task.done()
                await asyncio.to_thread(_probe_pool, gateway_pool.engine)
            finally:
                release.set()
                response = await task
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello"
        assert len(calls) == 2
        # Emission after nested generation reads both the outer RuntimeSession
        # and ApiUsage objects, catching expiration/reattachment regressions.
        assert account_events.called
    assert gateway_pool.engine.pool.checkedout() == 0
    with Session(gateway_pool.engine) as db:
        usage = db.scalars(
            select(models.ApiUsage).where(
                models.ApiUsage.account_id == gateway_pool.account_id
            )
        ).one()
        assert usage.status_code == 200 and usage.total_tokens == 4
        assert usage.runtime_session_id == runtime_session_id
        summary = db.execute(
            text("SELECT summary FROM runtime_session WHERE id = :id"),
            {"id": runtime_session_id},
        ).scalar_one()
        assert summary == "Example session summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_completed_http_stream_persists_usage_after_body_flush(
    gateway_pool: GatewayPoolFixture, asgi_spec: str
) -> None:
    """A normal disconnect notification after body completion cannot lose usage."""
    provider = HeldProvider()
    provider.release_handshakes.set()
    provider.release_streams.set()

    async def asgi_app(scope, receive, send):
        scope["asgi"]["spec_version"] = asgi_spec
        await gateway_pool.app(scope, receive, send)

    with (
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
            "preloop.services.openai_gateway.GatewayUsageSearchService.auto_index_interaction"
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app), base_url="http://test"
        ) as client:
            path, payload = _request("chat")
            response = await client.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {gateway_pool.token}"},
            )
    assert response.status_code == 200
    assert "[DONE]" in response.text
    assert gateway_pool.engine.pool.checkedout() == 0
    with Session(gateway_pool.engine) as db:
        rows = db.scalars(
            select(models.ApiUsage).where(
                models.ApiUsage.account_id == gateway_pool.account_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].status_code == 200 and rows[0].total_tokens == 4


@pytest.mark.asyncio
async def test_http_stream_disconnect_records_partial_usage_and_releases_pool(
    gateway_pool: GatewayPoolFixture,
) -> None:
    """A disconnected HTTP client leaves one partial record and no DB holder."""
    import json
    from starlette.requests import ClientDisconnect

    provider = HeldProvider()
    provider.release_handshakes.set()
    provider.release_streams.set()
    path, payload = _request("chat")
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {gateway_pool.token}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("synthetic disconnected client")

    with (
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
            "preloop.services.openai_gateway.GatewayUsageSearchService.auto_index_interaction"
        ),
    ):
        try:
            await gateway_pool.app(scope, receive, send)
        except ClientDisconnect:
            pass
        else:
            pytest.fail("the transport did not surface the simulated disconnect")
    assert sent[0]["status"] == 200
    assert gateway_pool.engine.pool.checkedout() == 0
    with Session(gateway_pool.engine) as db:
        rows = db.scalars(
            select(models.ApiUsage).where(
                models.ApiUsage.account_id == gateway_pool.account_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].status_code == 499


@pytest.mark.asyncio
async def test_gateway_auth_releases_pool_before_other_request_dependencies(
    gateway_pool: GatewayPoolFixture,
) -> None:
    """Authenticated state must not reserve capacity while later work queues."""
    from preloop.api.deps import NoopBudgetEnforcer, get_budget_enforcer

    checked = Event()

    def budget_dependency() -> NoopBudgetEnforcer:
        # A separate dependency can use the sole slot after authentication.
        # This models a request waiting for its next off-loop preparation step.
        _probe_pool(gateway_pool.engine)
        checked.set()
        return NoopBudgetEnforcer()

    gateway_pool.app.dependency_overrides[get_budget_enforcer] = budget_dependency
    provider = HeldProvider()
    provider.release_handshakes.set()
    provider.release_streams.set()
    path, payload = _request("chat")
    with (
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
            "preloop.services.openai_gateway.GatewayUsageSearchService.auto_index_interaction"
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=gateway_pool.app, raise_app_exceptions=False
            ),
            base_url="http://test",
        ) as client:
            response = await client.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {gateway_pool.token}"},
            )
    assert response.status_code == 200, response.text
    assert checked.is_set()
    assert "[DONE]" in response.text
    assert gateway_pool.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_gateway_owned_auth_detaches_context_and_still_checks_revocation(
    gateway_pool: GatewayPoolFixture,
) -> None:
    """Releasing auth reads never turns a prior success into an auth cache."""
    from sqlalchemy import inspect

    from preloop.services.model_gateway_auth import authenticate_bearer_token

    with Session(gateway_pool.engine) as db:
        user = crud_user.get_multi(db, account_id=str(gateway_pool.account_id))[0]
        key, token = crud_api_key.create_runtime_key(
            db,
            name="Synthetic auth phase key",
            account_id=gateway_pool.account_id,
            user_id=user.id,
            context_data={},
        )
        key_id = key.id

    with Session(gateway_pool.engine) as db:
        context = await authenticate_bearer_token(token, db, owns_db_session=True)
        assert context is not None and context.api_key is not None
        assert gateway_pool.engine.pool.checkedout() == 0
        assert inspect(context.user).detached
        assert inspect(context.api_key).detached
        # Scalar state needed by the next phase remains readable without SQL.
        assert context.user.account_id == gateway_pool.account_id
        assert context.api_key.id == key_id
        assert context.api_key.context_data == {}
        assert gateway_pool.engine.pool.checkedout() == 0

    with Session(gateway_pool.engine) as db:
        crud_api_key.deactivate(db, key_id=key_id)
    with Session(gateway_pool.engine) as db:
        assert await authenticate_bearer_token(token, db, owns_db_session=True) is None
        assert gateway_pool.engine.pool.checkedout() == 0
