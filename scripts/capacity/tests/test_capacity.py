"""Local protocol and measurement checks; never contact a paid provider."""

import io
import json
import os

from collections.abc import Callable
from typing import Any

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from scripts.capacity import fake, run


def test_nearest_rank_percentile_and_empty_samples() -> None:
    assert run.percentile([], 0.95) is None
    assert run.percentile([40, 10, 30, 20], 0.50) == 20
    assert run.percentile([40, 10, 30, 20], 0.95) == 40


def test_cancellations_do_not_inflate_completion_throughput_or_latency() -> None:
    samples = [
        {"operation": "gateway_stream", "ms": 100, "ok": True},
        {"operation": "gateway_stream", "ms": 1, "ok": True, "cancelled": True},
        {"operation": "gateway_stream", "ms": 200, "ok": False, "error": "http_503"},
    ]
    summary = run.summarize(samples, 2)
    stats = summary["operations"]["gateway_stream"]
    assert stats["successes"] == 1
    assert stats["successes_per_second"] == 0.5
    assert stats["cancelled_streams"] == 1
    assert stats["p95_ms"] == 100
    assert stats["errors"] == 1
    assert stats["error_rate"] == pytest.approx(1 / 3)
    assert run.breaches(summary, 0.01, 50) == [
        "gateway_stream:error_rate",
        "gateway_stream:p95",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://staging.example.com",
        "http://example.com",
        "http://user:pass@localhost",
        "file:///tmp/a",
        "https://api.openai.com",
    ],
)
def test_driver_rejects_external_endpoints(url: str) -> None:
    with pytest.raises(ValueError):
        run.local_url(url)


def test_recorder_bounds_reservations_and_writes_payload_free_samples() -> None:
    output = io.StringIO()
    recorder = run.Recorder(output, 1, 2)
    assert recorder.reserve()
    assert recorder.reserve()
    assert not recorder.reserve()
    recorder.record("mcp_tool", 0, "mcp_tool_error")
    row = json.loads(output.getvalue())
    assert row["operation"] == "mcp_tool"
    assert row["ok"] is False
    assert "headers" not in row


def test_fake_stream_has_content_usage_and_terminal_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MODEL_DELAY_MS", "0")
    monkeypatch.setenv("FAKE_TOKEN_DELAY_MS", "0")
    monkeypatch.setenv("FAKE_OUTPUT_TOKENS", "3")
    with TestClient(fake.app) as client:
        response = client.post("/v1/chat/completions", json={"stream": True})
    chunks = [
        line[6:] for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert chunks[-1] == "[DONE]"
    assert json.loads(chunks[-2])["usage"]["completion_tokens"] == 3
    assert (
        sum(
            bool(
                json.loads(chunk)["choices"]
                and json.loads(chunk)["choices"][0].get("delta", {}).get("content")
            )
            for chunk in chunks[:-1]
        )
        == 3
    )


def test_fake_error_injection_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ERROR_EVERY", "1")
    with TestClient(fake.app) as client:
        response = client.post("/v1/chat/completions", json={})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_mcp_fixture_executes_real_tool_protocol() -> None:
    async with Client(fake.mcp) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["capacity_echo"]
        result = await client.call_tool(
            "capacity_echo", {"sequence": 12, "delay_ms": 0}
        )
        assert not result.is_error
        assert result.data == {"sequence": 12}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ("data: [DONE]\n\n", "incomplete_stream"),
        ('data: {"error": "unavailable"}\n\n', "upstream_stream_error"),
    ],
)
async def test_http_200_does_not_hide_broken_model_stream(
    payload: str, expected: str
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(run.WorkloadError, match=expected):
            await run.model_call(client, "http://gateway", {}, False)


@pytest.mark.asyncio
async def test_intentional_stream_cancellation_requires_content() -> None:
    payload = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await run.model_call(client, "http://gateway", {}, True)
    assert result["cancelled"] is True
    assert result["ttft_ms"] is not None


def test_log_reconciliation_deduplicates_and_releases_previous_stage_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4
    from unittest.mock import MagicMock

    from preloop.models import crud
    from preloop.models.db import session
    from scripts.capacity.logs import LogFixture

    fixture = object.__new__(LogFixture)
    execution_id = uuid4()
    fixture.ids = [execution_id]
    fixture.cursors = {}
    fixture.seen = {0: {1, 2}}
    fixture.rows = {0: 2}
    rows = [
        SimpleNamespace(message=message, timestamp=datetime.now(UTC), id=uuid4())
        for message in ["capacity:0:3", "capacity:1:0", "capacity:1:0", "capacity:1:1"]
    ]

    def sessions():
        yield MagicMock()

    monkeypatch.setattr(session, "get_db_session", sessions)
    monkeypatch.setattr(
        crud.crud_flow_execution_log, "get_agent_log_page", lambda *a, **k: rows
    )
    result = fixture.reconcile(1)
    assert result == {"1": {"unique_persisted": 2, "rows": 3, "duplicates": 1}}
    assert fixture.cursors[execution_id] == (rows[-1].timestamp, rows[-1].id)
    assert 0 not in fixture.seen


@pytest.mark.parametrize(
    "message",
    ["not-a-log", "capacity:1", "capacity:1:0:extra", "capacity:x:0", ""],
)
def test_log_reconciliation_rejects_malformed_lines_as_unexpected_fixture_logs(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4
    from unittest.mock import MagicMock

    from preloop.models import crud
    from preloop.models.db import session
    from scripts.capacity.logs import LogFixture

    fixture = object.__new__(LogFixture)
    fixture.ids = [uuid4()]
    fixture.cursors = {}
    fixture.seen = {}
    fixture.rows = {}
    rows = [SimpleNamespace(message=message, timestamp=datetime.now(UTC), id=uuid4())]

    def sessions():
        yield MagicMock()

    monkeypatch.setattr(session, "get_db_session", sessions)
    monkeypatch.setattr(
        crud.crud_flow_execution_log, "get_agent_log_page", lambda *a, **k: rows
    )
    with pytest.raises(ValueError, match="Unexpected fixture log"):
        fixture.reconcile(1)


@pytest.mark.asyncio
async def test_recovery_signin_failure_fails_an_otherwise_passing_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import time
    from unittest.mock import AsyncMock

    args = argparse.Namespace(
        output=tmp_path / "run",
        api="http://api",
        fake="http://fake",
        levels=[1],
        logs_per_second=0,
        max_requests=100,
        seconds=0.01,
        cooldown_seconds=0.01,
        max_error_rate=0.01,
        max_p95_ms=5000,
        continue_after_failure=False,
    )
    monkeypatch.setattr(
        run, "bootstrap", AsyncMock(return_value=("local", {}, "account"))
    )
    monkeypatch.setattr(
        run, "checked", AsyncMock(return_value={"fixture": "preloop-capacity-v1"})
    )

    async def actor(index, args, token, recorder, stop):
        for operation in ("mcp_tool", "gateway_stream"):
            recorder.record(operation, time.perf_counter())

    async def observe(args, token, login, recorder, stop, output):
        for operation in ("api_ping", "gateway_ping", "signin"):
            recorder.record(
                operation,
                time.perf_counter(),
                "http_503"
                if recorder.phase == "recovery" and operation == "signin"
                else None,
            )

    monkeypatch.setattr(run, "actor", actor)
    monkeypatch.setattr(run, "observe", observe)
    assert await run.run(args) == 2
    summary = json.loads((args.output / "summary.json").read_text())
    assert "recovery:signin:error_rate" in summary["stages"][0]["threshold_breaches"]
    assert (
        "recovery:signin:no_successful_samples"
        in summary["stages"][0]["threshold_breaches"]
    )


@pytest.mark.asyncio
async def test_mcp_transport_disables_ambient_proxy_and_redirects() -> None:
    async with run.mcp_http_client(
        headers={"x-capacity": "test"}, follow_redirects=True
    ) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.headers["x-capacity"] == "test"


ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
STREAM_BODY = (
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _json_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content) if request.content else {}


def _async_client_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_bootstrap_follows_register_token_key_model_mcp_contract() -> None:
    from uuid import uuid4

    from preloop.models.schemas.mcp_server import MCPServerCreate
    from preloop.schemas.ai_model import AIModelCreate
    from preloop.schemas.auth import ApiKeyCreate, AuthUserCreate, LoginRequest

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((request.method, f"{request.url.host}{path}"))
        if request.method == "GET" and path == "/capacity/identity":
            assert request.url.host == "fake"
            return httpx.Response(200, json={"fixture": "preloop-capacity-v1"})
        body = _json_body(request)
        if path == "/api/v1/auth/register":
            AuthUserCreate.model_validate(body)
            assert body["email"].endswith("@example.com")
            return httpx.Response(201, json={"account_id": ACCOUNT_ID})
        if path == "/api/v1/auth/token/json":
            LoginRequest.model_validate(body)
            return httpx.Response(200, json={"access_token": "capacity-token"})
        assert request.headers.get("authorization") == "Bearer capacity-token"
        if path == "/api/v1/auth/api-keys":
            ApiKeyCreate.model_validate(body)
            assert body["name"] == "capacity-driver"
            return httpx.Response(201, json={"key": "capacity-key"})
        if path == "/api/v1/ai-models":
            AIModelCreate.model_validate(body)
            assert body["api_endpoint"] == "http://fake/v1"
            assert (
                body["meta_data"]["gateway"]["model_alias"] == "openai/capacity-model"
            )
            return httpx.Response(201, json={"id": str(uuid4())})
        if path == "/api/v1/mcp-servers":
            MCPServerCreate.model_validate(body)
            assert body["url"] == "http://fake/tools/mcp"
            assert body["transport"] == "http-streaming"
            assert body["auth_type"] == "none"
            return httpx.Response(201, json={"status": "active"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        key, login, account_id = await run.bootstrap(
            client, "http://api", "http://fake"
        )
    assert key == "capacity-key"
    assert account_id == ACCOUNT_ID
    assert login["username"].startswith("capacity")
    assert seen == [
        ("GET", "fake/capacity/identity"),
        ("POST", "api/api/v1/auth/register"),
        ("POST", "api/api/v1/auth/token/json"),
        ("POST", "api/api/v1/auth/api-keys"),
        ("POST", "api/api/v1/ai-models"),
        ("POST", "api/api/v1/mcp-servers"),
    ]


@pytest.mark.asyncio
async def test_bootstrap_rejects_a_non_fixture_identity() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"fixture": "other"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="identity check failed"):
            await run.bootstrap(client, "http://api", "http://fake")


@pytest.mark.asyncio
async def test_actor_alternates_tool_then_gateway_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse
    import time

    from fastmcp.client.transports import StreamableHttpTransport

    gateway_requests: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        assert request.url.path == "/openai/v1/chat/completions"
        body = _json_body(request)
        assert body["model"] == "openai/capacity-model"
        assert body["stream"] is True
        assert body["stream_options"]["include_usage"] is True
        assert request.headers["authorization"] == "Bearer capacity-token"
        assert request.headers["x-preloop-session-id"] == "capacity-0-3"
        return httpx.Response(200, text=STREAM_BODY)

    def fake_client(transport, timeout=None):
        assert isinstance(transport, StreamableHttpTransport)
        assert transport.url == "http://127.0.0.1/mcp/v1"
        assert transport.headers["Authorization"] == "Bearer capacity-token"
        assert transport.httpx_client_factory is run.mcp_http_client
        return Client(fake.mcp, timeout=timeout)

    monkeypatch.setattr(run, "Client", fake_client)
    monkeypatch.setattr(run.httpx, "AsyncClient", _async_client_with_handler(gateway))
    output = io.StringIO()
    recorder = run.Recorder(output, 0, 8)
    args = argparse.Namespace(
        api="http://127.0.0.1",
        gateway="http://127.0.0.1:8001",
        timeout=5,
        tool_delay_ms=0,
        think_ms=0,
        cancel_every=0,
    )
    await run.actor(3, args, "capacity-token", recorder, time.monotonic() + 0.4)
    operations = [sample["operation"] for sample in recorder.samples]
    assert operations[0] == "mcp_connect"
    pairs = [op for op in operations[1:] if op in {"mcp_tool", "gateway_stream"}]
    assert pairs[:2] == ["mcp_tool", "gateway_stream"]
    assert all(pairs[i] != pairs[i + 1] for i in range(len(pairs) - 1))
    assert "mcp_tool" in operations
    assert "gateway_stream" in operations
    assert all(sample["ok"] for sample in recorder.samples)
    assert gateway_requests


@pytest.mark.asyncio
async def test_observe_probes_ping_health_signin_control_and_nats_varz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse
    import asyncio

    from preloop.schemas.auth import LoginRequest

    seen: list[tuple[str, str]] = []
    login = {"username": "capacityuser", "password": "capacity-password-ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((request.method, f"{request.url.host}{path}"))
        if path == "/api/v1/ping":
            return httpx.Response(200, json={"status": "pong"})
        if path == "/api/v1/health":
            return httpx.Response(200, json={"database": "connected"})
        if path == "/api/v1/auth/token/json":
            LoginRequest.model_validate(_json_body(request))
            return httpx.Response(200, json={"access_token": "capacity-token"})
        if path == "/api/v1/auth/users/me":
            assert request.headers.get("authorization") == "Bearer capacity-token"
            return httpx.Response(200, json={"username": "capacityuser"})
        if path == "/varz":
            assert request.url.host == "nats"
            return httpx.Response(200, json={"connections": 1})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    monkeypatch.setattr(run.httpx, "AsyncClient", _async_client_with_handler(handler))
    output = io.StringIO()
    recorder = run.Recorder(output, 0, 6)
    args = argparse.Namespace(
        timeout=2,
        api="http://api",
        gateway="http://gateway",
        nats="http://nats",
        sample_seconds=0.05,
    )
    stop = asyncio.Event()
    await run.observe(args, "capacity-token", login, recorder, stop, output)
    assert seen[:6] == [
        ("GET", "api/api/v1/ping"),
        ("GET", "api/api/v1/health"),
        ("GET", "gateway/api/v1/ping"),
        ("GET", "gateway/api/v1/health"),
        ("POST", "api/api/v1/auth/token/json"),
        ("GET", "nats/varz"),
    ]
    assert ("GET", "api/api/v1/auth/users/me") in seen
    operations = [sample["operation"] for sample in recorder.samples]
    assert "api_ping" in operations
    assert "gateway_ping" in operations
    assert "signin" in operations
    assert "control" in operations
    assert all(sample["ok"] for sample in recorder.samples)


@pytest.mark.asyncio
async def test_log_publish_uses_flow_updates_subject_and_agent_log_line_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    import nats

    from scripts.capacity.logs import LogFixture

    execution_id = uuid4()
    published: list[tuple[str, dict]] = []

    class FakeNats:
        async def publish(self, subject: str, payload: bytes) -> None:
            published.append((subject, json.loads(payload)))

        async def flush(self, timeout: float = 5) -> None:
            return None

        async def close(self) -> None:
            return None

    async def connect(address, connect_timeout=5, max_reconnect_attempts=0):
        assert address == "nats://127.0.0.1:4222"
        assert connect_timeout == 5
        assert max_reconnect_attempts == 0
        return FakeNats()

    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setattr(nats, "connect", connect)
    fixture = object.__new__(LogFixture)
    fixture.ids = [execution_id]
    result = await fixture.publish("account-1", 2, rate=1000, seconds=1, maximum=2)
    assert result["submitted"] == 2
    assert result["maximum_reached"] is True
    assert [subject for subject, _ in published] == [
        "flow-updates." + str(execution_id)
    ] * 2
    for index, (_, payload) in enumerate(published):
        assert payload["type"] == "agent_log_line"
        assert payload["execution_id"] == str(execution_id)
        assert payload["account_id"] == "account-1"
        assert payload["payload"] == {"line": f"capacity:2:{index}"}
