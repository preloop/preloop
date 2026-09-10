"""Local protocol and measurement checks; never contact a paid provider."""

import io
import json
import os

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
