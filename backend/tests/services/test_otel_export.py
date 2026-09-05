"""Unit tests for optional OTLP export."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from preloop.config import Settings
from preloop.services import otel_export
from preloop.services.otel_export import (
    ATTR_CONVERSATION_ID,
    ATTR_ESTIMATED_COST,
    ATTR_INPUT_TOKENS,
    ATTR_OPERATION_NAME,
    attributes_from_usage,
    configure_for_tests,
    emit_gateway_usage,
    emit_tool_call,
    is_enabled,
    parse_otlp_headers,
    shutdown_otel,
)


def _usage(**overrides) -> SimpleNamespace:
    session_id = uuid4()
    values = dict(
        id=uuid4(),
        account_id=uuid4(),
        runtime_session_id=session_id,
        status_code=200,
        duration=0.25,
        provider_name="openai",
        model_alias="openai/gpt-5",
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        estimated_cost=0.00025,
        cost_source="catalog",
        timestamp=None,
        meta_data={
            "endpoint_kind": "chat_completions",
            "requested_model": "openai/gpt-5",
            "finish_reason": "stop",
        },
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def otel_memory_exporter():
    exporter = InMemorySpanExporter()
    configure_for_tests(exporter)
    try:
        yield exporter
    finally:
        shutdown_otel()


def test_parse_otlp_headers_splits_pairs() -> None:
    parsed = parse_otlp_headers(
        "Authorization=Basic abc,x-langfuse-ingestion-version=4"
    )
    assert parsed["Authorization"] == "Basic abc"
    assert parsed["x-langfuse-ingestion-version"] == "4"


def test_attributes_include_conversation_id_and_usage() -> None:
    usage = _usage()
    attrs = attributes_from_usage(usage)
    assert attrs[ATTR_CONVERSATION_ID] == str(usage.runtime_session_id)
    assert attrs[ATTR_OPERATION_NAME] == "chat"
    assert attrs["gen_ai.request.model"] == "openai/gpt-5"
    assert attrs[ATTR_INPUT_TOKENS] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs[ATTR_ESTIMATED_COST] == pytest.approx(0.00025)
    assert attrs["preloop.account.id"] == str(usage.account_id)
    assert attrs["preloop.api_usage.id"] == str(usage.id)
    for key in attrs:
        assert key not in {
            "gen_ai.prompt",
            "gen_ai.completion",
            "gen_ai.input.messages",
            "gen_ai.output.messages",
        }
        assert not key.startswith("gen_ai.input.")
        assert not key.startswith("gen_ai.output.")


def test_attributes_omit_conversation_id_when_session_missing() -> None:
    attrs = attributes_from_usage(_usage(runtime_session_id=None))
    assert ATTR_CONVERSATION_ID not in attrs


def test_disabled_exporter_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(otel_export._state, "force_enabled", False)
    monkeypatch.setattr(otel_export._state, "init_failed", False)
    monkeypatch.setattr(otel_export._state, "provider", None)
    assert is_enabled() is False
    emit_gateway_usage(_usage())
    emit_tool_call(
        tool_name="search",
        runtime_session_id=str(uuid4()),
        account_id=str(uuid4()),
    )


def test_exporter_raise_does_not_propagate() -> None:
    class BoomExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("collector down")

        def shutdown(self):
            return True

        def force_flush(self, timeout_millis: int = 30000):
            return True

    configure_for_tests(BoomExporter())
    try:
        emit_gateway_usage(_usage())
        emit_tool_call(
            tool_name="search",
            runtime_session_id=str(uuid4()),
            account_id=str(uuid4()),
        )
    finally:
        shutdown_otel()


def test_emit_gateway_usage_writes_span(otel_memory_exporter) -> None:
    usage = _usage()
    emit_gateway_usage(usage)
    spans = otel_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes[ATTR_CONVERSATION_ID] == str(usage.runtime_session_id)
    assert span.attributes[ATTR_INPUT_TOKENS] == usage.prompt_tokens
    assert span.attributes[ATTR_ESTIMATED_COST] == pytest.approx(usage.estimated_cost)


def test_emit_tool_call_skips_without_session(otel_memory_exporter) -> None:
    emit_tool_call(tool_name="search", runtime_session_id=None)
    assert otel_memory_exporter.get_finished_spans() == ()


def test_emit_tool_call_writes_execute_tool_span(otel_memory_exporter) -> None:
    session_id = str(uuid4())
    emit_tool_call(
        tool_name="search",
        runtime_session_id=session_id,
        account_id=str(uuid4()),
        status="executed",
        duration_ms=12,
        server_name="preloop-mcp",
    )
    spans = otel_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes[ATTR_OPERATION_NAME] == "execute_tool"
    assert span.attributes[ATTR_CONVERSATION_ID] == session_id
    assert span.attributes["gen_ai.tool.name"] == "search"
    assert "gen_ai.tool.call.arguments" not in span.attributes


def test_otlp_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OTLP_ENABLED", "true")
    monkeypatch.setenv("OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTLP_HEADERS", "dd-api-key=secret,compute_stats=true")
    monkeypatch.setenv("OTLP_SERVICE_NAME", "preloop-test")
    monkeypatch.setenv("OTLP_SAMPLER_RATIO", "0.2")
    loaded = Settings.from_env()
    assert loaded.otlp.enabled is True
    assert loaded.otlp.endpoint == "http://collector:4318"
    assert loaded.otlp.protocol == "grpc"
    assert loaded.otlp.headers == "dd-api-key=secret,compute_stats=true"
    assert loaded.otlp.service_name == "preloop-test"
    assert loaded.otlp.sampler_ratio == pytest.approx(0.2)


def test_otlp_endpoint_falls_back_to_otel_env(monkeypatch) -> None:
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://alt:4318")
    loaded = Settings.from_env()
    assert loaded.otlp.endpoint == "http://alt:4318"


def test_default_otlp_disabled(monkeypatch) -> None:
    for key in (
        "OTLP_ENABLED",
        "OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = Settings.from_env()
    assert loaded.otlp.enabled is False
    assert loaded.otlp.endpoint == ""


def test_signal_endpoint_appends_traces_path() -> None:
    assert (
        otel_export._signal_endpoint("http://collector:4318", "http/protobuf", "traces")
        == "http://collector:4318/v1/traces"
    )
    assert (
        otel_export._signal_endpoint(
            "https://otlp.datadoghq.com/v1/traces", "http/protobuf", "traces"
        )
        == "https://otlp.datadoghq.com/v1/traces"
    )


def test_signal_endpoint_skips_metrics_on_traces_only_url() -> None:
    assert (
        otel_export._signal_endpoint(
            "https://otlp.datadoghq.com/v1/traces", "http/protobuf", "metrics"
        )
        is None
    )
    assert (
        otel_export._signal_endpoint(
            "http://collector:4318", "http/protobuf", "metrics"
        )
        == "http://collector:4318/v1/metrics"
    )


def test_provider_init_failure_is_sticky(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed exporter setup must not be retried for every subsequent span."""
    shutdown_otel()
    otel_export._state.provider = None
    otel_export._state.init_failed = False
    otel_export._state.force_enabled = True
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(otel_export, "_build_provider", boom)
    try:
        assert otel_export._ensure_provider() is None
        assert otel_export._ensure_provider() is None
        assert calls["n"] == 1
        assert otel_export._state.init_failed is True
        assert otel_export.is_enabled() is False
    finally:
        shutdown_otel()
