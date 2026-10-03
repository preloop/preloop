"""Keep the OTLP exporter, the stability registry and the policy docs in sync."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from preloop.services import api_usage_recorder, otel_attributes, otel_export
from preloop.services.otel_attributes import (
    ALL_SPAN_ATTRIBUTES,
    EXPERIMENTAL_METRIC_DIMENSIONS,
    EXPERIMENTAL_METRICS,
    EXPERIMENTAL_SPAN_ATTRIBUTES,
    STABLE_METRIC_DIMENSIONS,
    STABLE_METRICS,
    STABLE_SPAN_ATTRIBUTES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_DOC = REPO_ROOT / "docs" / "guide" / "otlp-attribute-stability.md"


class _FakeInstrument:
    def __init__(self, sink: list, name: str) -> None:
        self._sink = sink
        self._name = name

    def record(self, _value, attributes=None) -> None:
        self._sink.append((self._name, dict(attributes or {})))

    add = record


class _FakeMeter:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def create_histogram(self, name, **_kwargs):
        return _FakeInstrument(self._sink, name)

    def create_counter(self, name, **_kwargs):
        return _FakeInstrument(self._sink, name)


def _full_usage() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runtime_session_id=uuid4(),
        status_code=200,
        duration=0.1,
        provider_name="openai",
        model_alias="openai/gpt-5",
        prompt_tokens=3,
        completion_tokens=4,
        estimated_cost=0.001,
        cost_source="catalog",
        timestamp=None,
        meta_data={"endpoint_kind": "chat_completions", "finish_reason": "stop"},
    )


@pytest.fixture
def emitted(monkeypatch):
    """Drive every exporter entry point and collect span and metric output."""
    metric_sink: list = []
    monkeypatch.setattr(
        otel_export.metrics, "get_meter", lambda *_a, **_k: _FakeMeter(metric_sink)
    )
    exporter = InMemorySpanExporter()
    otel_export.configure_for_tests(exporter)
    try:
        usage = _full_usage()
        otel_export.emit_gateway_usage(usage)
        otel_export.emit_list_models(account_id=str(uuid4()))
        otel_export.emit_tool_call(
            tool_name="search",
            runtime_session_id=str(usage.runtime_session_id),
            account_id=str(usage.account_id),
            duration_ms=5,
            server_name="example-server",
        )
        spans = exporter.get_finished_spans()
    finally:
        otel_export.shutdown_otel()
    keys = {key for span in spans for key in (span.attributes or {})}
    return keys, metric_sink


def _doc_table_names(section: str) -> set[str]:
    text = POLICY_DOC.read_text()
    match = re.search(rf"^## {re.escape(section)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"section {section!r} missing from {POLICY_DOC.name}"
    return set(re.findall(r"^\| `([^`]+)` \|", match.group(1), re.M))


def test_stable_and_experimental_do_not_overlap() -> None:
    assert not set(STABLE_SPAN_ATTRIBUTES) & set(EXPERIMENTAL_SPAN_ATTRIBUTES)
    assert not set(STABLE_METRICS) & set(EXPERIMENTAL_METRICS)


def test_preloop_namespace_is_stable_only() -> None:
    assert not [k for k in EXPERIMENTAL_SPAN_ATTRIBUTES if k.startswith("preloop.")]
    assert not [k for k in EXPERIMENTAL_METRICS if k.startswith("preloop.")]


def test_every_emitted_span_attribute_is_registered(emitted) -> None:
    keys, _ = emitted
    unregistered = keys - ALL_SPAN_ATTRIBUTES
    assert not unregistered, (
        f"exporter emits unregistered attributes {sorted(unregistered)}; add them "
        "to otel_attributes.py and docs/guide/otlp-attribute-stability.md"
    )


def test_emitted_preloop_attributes_match_stable_list(emitted) -> None:
    keys, _ = emitted
    emitted_preloop = {k for k in keys if k.startswith("preloop.")}
    stable_preloop = {k for k in STABLE_SPAN_ATTRIBUTES if k.startswith("preloop.")}
    assert emitted_preloop == stable_preloop


def test_every_attr_constant_is_registered() -> None:
    constants = {
        value
        for name, value in vars(otel_export).items()
        if name.startswith("ATTR_") and isinstance(value, str)
    }
    assert constants <= ALL_SPAN_ATTRIBUTES, sorted(constants - ALL_SPAN_ATTRIBUTES)


def test_emitted_metrics_and_dimensions_are_registered(emitted) -> None:
    _, metric_sink = emitted
    assert metric_sink
    for name, attrs in metric_sink:
        assert name in EXPERIMENTAL_METRICS or name in STABLE_METRICS, name
        allowed = {**STABLE_METRIC_DIMENSIONS, **EXPERIMENTAL_METRIC_DIMENSIONS}[name]
        assert set(attrs) <= set(allowed), (name, sorted(attrs))


def test_api_usage_recorder_metric_names_are_registered() -> None:
    source = Path(api_usage_recorder.__file__).read_text()
    names = set(re.findall(r'_emit_metric\(\s*"([^"]+)"', source))
    assert names, "expected _emit_metric calls in api_usage_recorder"
    assert names <= set(STABLE_METRICS) | set(EXPERIMENTAL_METRICS), sorted(names)


def test_policy_doc_tables_match_registry() -> None:
    assert _doc_table_names("Stable span attributes") == set(STABLE_SPAN_ATTRIBUTES)
    assert _doc_table_names("Experimental span attributes") == set(
        EXPERIMENTAL_SPAN_ATTRIBUTES
    )
    assert _doc_table_names("Metrics table") == set(STABLE_METRICS) | set(
        EXPERIMENTAL_METRICS
    )


def test_otlp_guide_links_policy() -> None:
    guide = (REPO_ROOT / "docs" / "guide" / "observability-otlp.md").read_text()
    assert "otlp-attribute-stability.md" in guide
    assert otel_attributes.__doc__ and "otlp-attribute-stability.md" in (
        otel_attributes.__doc__
    )
