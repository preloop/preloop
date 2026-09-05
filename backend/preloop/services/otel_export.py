"""Optional OTLP export for gateway completions and MCP tool calls.

Default off. Failures are logged and never raised into the request path.
Span attributes follow OpenTelemetry GenAI conventions where they apply
(`gen_ai.conversation.id` matches Preloop runtime session identity) and
never include raw prompts, completions, or tool arguments.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from preloop.config import settings

logger = logging.getLogger(__name__)

TRACER_NAME = "preloop.gateway"
METER_NAME = "preloop.gateway"

ATTR_OPERATION_NAME = "gen_ai.operation.name"
ATTR_PROVIDER_NAME = "gen_ai.provider.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_CONVERSATION_ID = "gen_ai.conversation.id"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_FINISH_REASONS = "gen_ai.response.finish_reasons"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_TYPE = "gen_ai.tool.type"

ATTR_ACCOUNT_ID = "preloop.account.id"
ATTR_API_USAGE_ID = "preloop.api_usage.id"
ATTR_ESTIMATED_COST = "preloop.usage.estimated_cost_usd"
ATTR_COST_SOURCE = "preloop.usage.cost_source"
ATTR_STATUS_CODE = "http.response.status_code"
ATTR_SERVER_NAME = "preloop.mcp.server_name"

_FORBIDDEN_ATTR_KEYS = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.arguments",
    }
)
_FORBIDDEN_ATTR_PREFIXES = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.",
    "gen_ai.output.",
)


class _OtelState:
    """Process-wide OTLP runtime. Mutated in place; no `global` writes."""

    def __init__(self) -> None:
        self.provider: Optional[TracerProvider] = None
        self.force_enabled = False
        self.init_failed = False
        self.session_span_context: dict[str, trace.SpanContext] = {}


_lock = threading.Lock()
_state = _OtelState()
_SESSION_CONTEXT_MAX = 256

_HTTP_PROTOCOLS = frozenset({"http", "http/protobuf", "http/proto", "http/json"})


def parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse `key=value,key2=value2` header strings used by OTLP exporters."""
    headers: dict[str, str] = {}
    if not raw:
        return headers
    for part in raw.split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def is_enabled() -> bool:
    """Return whether OTLP export should run for this process."""
    if _state.init_failed:
        return False
    if _state.force_enabled:
        return True
    otlp = getattr(settings, "otlp", None)
    if otlp is None or not otlp.enabled:
        return False
    return bool((otlp.endpoint or "").strip())


def attributes_from_usage(usage: Any) -> dict[str, Any]:
    """Map an ApiUsage row to GenAI span attributes (no payloads)."""
    meta = (
        usage.meta_data if isinstance(getattr(usage, "meta_data", None), dict) else {}
    )
    endpoint_kind = str(meta.get("endpoint_kind") or "")
    operation = _operation_name(endpoint_kind)
    attrs: dict[str, Any] = {
        ATTR_OPERATION_NAME: operation,
        ATTR_STATUS_CODE: int(getattr(usage, "status_code", 0) or 0),
    }
    provider = getattr(usage, "provider_name", None) or meta.get("gateway_provider")
    if provider:
        attrs[ATTR_PROVIDER_NAME] = str(provider)
    model = getattr(usage, "model_alias", None) or meta.get("requested_model")
    if model:
        attrs[ATTR_REQUEST_MODEL] = str(model)
    session_id = getattr(usage, "runtime_session_id", None)
    if session_id:
        attrs[ATTR_CONVERSATION_ID] = str(session_id)
    account_id = getattr(usage, "account_id", None)
    if account_id:
        attrs[ATTR_ACCOUNT_ID] = str(account_id)
    usage_id = getattr(usage, "id", None)
    if usage_id:
        attrs[ATTR_API_USAGE_ID] = str(usage_id)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    if prompt_tokens is not None:
        attrs[ATTR_INPUT_TOKENS] = int(prompt_tokens)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if completion_tokens is not None:
        attrs[ATTR_OUTPUT_TOKENS] = int(completion_tokens)
    estimated_cost = getattr(usage, "estimated_cost", None)
    if estimated_cost is not None:
        attrs[ATTR_ESTIMATED_COST] = float(estimated_cost)
    cost_source = getattr(usage, "cost_source", None)
    if cost_source:
        attrs[ATTR_COST_SOURCE] = str(cost_source)
    finish_reason = meta.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        attrs[ATTR_FINISH_REASONS] = (finish_reason,)
    return _safe_attributes(attrs)


def emit_gateway_usage(usage: Any) -> None:
    """Export one span for a recorded gateway request. Never raises."""
    try:
        if not is_enabled():
            return
        provider = _ensure_provider()
        if provider is None:
            return
        attrs = attributes_from_usage(usage)
        operation = str(attrs.get(ATTR_OPERATION_NAME) or "chat")
        model = attrs.get(ATTR_REQUEST_MODEL)
        span_name = f"{operation} {model}" if model else operation
        start_ns, end_ns = _span_timestamps(usage)
        tracer = provider.get_tracer(TRACER_NAME)
        span = tracer.start_span(
            span_name,
            kind=SpanKind.CLIENT,
            start_time=start_ns,
            attributes=attrs,
        )
        status_code = int(getattr(usage, "status_code", 0) or 0)
        if status_code >= 400:
            span.set_status(Status(StatusCode.ERROR))
        session_id = attrs.get(ATTR_CONVERSATION_ID)
        if isinstance(session_id, str) and session_id:
            _remember_session_span(session_id, span)
        span.end(end_time=end_ns)
        _record_duration_metric(attrs, float(getattr(usage, "duration", 0.0) or 0.0))
    except Exception:
        logger.warning(
            "OTLP gateway export failed; request is unchanged", exc_info=True
        )


def emit_list_models(*, account_id: Optional[str] = None) -> None:
    """Export a span for GET /openai/v1/models. Never raises."""
    try:
        if not is_enabled():
            return
        if _ensure_provider() is None:
            return
        attrs = _safe_attributes(
            {
                ATTR_OPERATION_NAME: "list_models",
                ATTR_ACCOUNT_ID: account_id,
            }
        )
        tracer = (
            _state.provider.get_tracer(TRACER_NAME)
            if _state.provider is not None
            else None
        )
        if tracer is None:
            return
        span = tracer.start_span("list_models", kind=SpanKind.SERVER, attributes=attrs)
        span.end()
    except Exception:
        logger.warning(
            "OTLP list_models export failed; request is unchanged", exc_info=True
        )


def emit_tool_call(
    *,
    tool_name: str,
    runtime_session_id: Optional[str],
    account_id: Optional[str] = None,
    status: str = "executed",
    duration_ms: int = 0,
    server_name: Optional[str] = None,
) -> None:
    """Export an execute_tool span when a session id is present. Never raises."""
    try:
        if not is_enabled():
            return
        if not runtime_session_id:
            return
        if _ensure_provider() is None:
            return
        attrs = _safe_attributes(
            {
                ATTR_OPERATION_NAME: "execute_tool",
                ATTR_TOOL_NAME: tool_name,
                ATTR_TOOL_TYPE: "function",
                ATTR_CONVERSATION_ID: str(runtime_session_id),
                ATTR_ACCOUNT_ID: account_id,
                ATTR_SERVER_NAME: server_name,
            }
        )
        links: Sequence[Link] = ()
        with _lock:
            parent = _state.session_span_context.get(str(runtime_session_id))
        if parent is not None and parent.is_valid:
            links = (Link(parent),)
        duration_ns = max(int(duration_ms) * 1_000_000, 0)
        end_ns = _now_ns()
        start_ns = end_ns - duration_ns
        tracer = (
            _state.provider.get_tracer(TRACER_NAME)
            if _state.provider is not None
            else None
        )
        if tracer is None:
            return
        span = tracer.start_span(
            f"execute_tool {tool_name}",
            kind=SpanKind.CLIENT,
            start_time=start_ns,
            attributes=attrs,
            links=links,
        )
        if status == "failed":
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=end_ns)
        _record_duration_metric(attrs, duration_ms / 1000.0)
    except Exception:
        logger.warning("OTLP tool export failed; request is unchanged", exc_info=True)


def configure_for_tests(exporter: SpanExporter) -> None:
    """Install an in-memory (or fake) exporter for unit tests."""
    with _lock:
        _shutdown_locked()
        _state.force_enabled = True
        _state.init_failed = False
        _state.session_span_context = {}
        resource = Resource.create({"service.name": "preloop-test"})
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(1.0)),
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        _state.provider = provider


def shutdown_otel() -> None:
    """Flush and shut down the tracer (and meter) providers."""
    with _lock:
        _shutdown_locked()
        _state.force_enabled = False


def _shutdown_locked() -> None:
    if _state.provider is not None:
        try:
            _state.provider.shutdown()
        except Exception:
            logger.debug("OTLP tracer shutdown failed", exc_info=True)
        _state.provider = None
    try:
        meter_provider = metrics.get_meter_provider()
        shutdown = getattr(meter_provider, "shutdown", None)
        if callable(shutdown) and type(meter_provider).__name__ == "MeterProvider":
            shutdown()
    except Exception:
        logger.debug("OTLP meter shutdown failed", exc_info=True)
    _state.init_failed = False
    _state.session_span_context = {}


def _ensure_provider() -> Optional[TracerProvider]:
    if _state.provider is not None:
        return _state.provider
    if _state.init_failed:
        return None
    with _lock:
        if _state.provider is not None:
            return _state.provider
        if _state.init_failed:
            return None
        try:
            _state.provider = _build_provider()
            return _state.provider
        except Exception:
            _state.init_failed = True
            logger.warning(
                "OTLP exporter setup failed; export disabled for this process",
                exc_info=True,
            )
            return None


def _build_provider() -> TracerProvider:
    otlp = settings.otlp
    protocol = (otlp.protocol or "http/protobuf").strip().lower()
    headers = parse_otlp_headers(otlp.headers or "")
    resource_attrs: dict[str, str] = {
        "service.name": (otlp.service_name or "preloop").strip() or "preloop",
    }
    if otlp.service_namespace:
        resource_attrs["service.namespace"] = otlp.service_namespace
    environment = otlp.deployment_environment or getattr(settings, "environment", "")
    if environment:
        resource_attrs["deployment.environment"] = environment
    resource = Resource.create(resource_attrs)
    ratio = _clamp_ratio(otlp.sampler_ratio)
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    exporter = _build_otlp_span_exporter(otlp.endpoint, protocol, headers)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        logger.debug("Global tracer provider already set; using local provider")
    _init_metrics(resource, otlp.endpoint, protocol, headers)
    logger.info(
        "OTLP export enabled protocol=%s endpoint=%s sampler_ratio=%s",
        protocol,
        otlp.endpoint,
        ratio,
    )
    return provider


def _build_otlp_span_exporter(
    endpoint: str, protocol: str, headers: Mapping[str, str]
) -> SpanExporter:
    if protocol in _HTTP_PROTOCOLS:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        traces_endpoint = _signal_endpoint(endpoint, protocol, "traces")
        if traces_endpoint is None:
            traces_endpoint = endpoint.strip().rstrip("/")
        return OTLPSpanExporter(
            endpoint=traces_endpoint,
            headers=dict(headers),
        )
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        kwargs: dict[str, Any] = {"headers": tuple(headers.items())}
        grpc_endpoint, insecure = _grpc_endpoint(endpoint)
        kwargs["endpoint"] = grpc_endpoint
        kwargs["insecure"] = insecure
        return OTLPSpanExporter(**kwargs)
    raise ValueError(f"Unsupported OTLP protocol: {protocol}")


def _init_metrics(
    resource: Resource,
    endpoint: str,
    protocol: str,
    headers: Mapping[str, str],
) -> None:
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        if protocol in _HTTP_PROTOCOLS:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )

            metrics_endpoint = _signal_endpoint(endpoint, protocol, "metrics")
            if metrics_endpoint is None:
                logger.info("OTLP metrics export skipped; endpoint is traces-only")
                return
            exporter: Any = OTLPMetricExporter(
                endpoint=metrics_endpoint,
                headers=dict(headers),
            )
        elif protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )

            grpc_endpoint, insecure = _grpc_endpoint(endpoint)
            exporter = OTLPMetricExporter(
                endpoint=grpc_endpoint,
                insecure=insecure,
                headers=tuple(headers.items()),
            )
        else:
            return
        reader = PeriodicExportingMetricReader(exporter)
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        try:
            metrics.set_meter_provider(provider)
        except Exception:
            logger.debug("Global meter provider already set")
    except Exception:
        logger.warning(
            "OTLP metric exporter setup failed; traces still enabled",
            exc_info=True,
        )


def _record_duration_metric(attrs: Mapping[str, Any], duration_s: float) -> None:
    try:
        meter = metrics.get_meter(METER_NAME)
        histogram = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of a Preloop-governed model or tool call",
        )
        metric_attrs = {
            key: attrs[key]
            for key in (
                ATTR_OPERATION_NAME,
                ATTR_PROVIDER_NAME,
                ATTR_REQUEST_MODEL,
            )
            if key in attrs
        }
        histogram.record(max(duration_s, 0.0), metric_attrs)
    except Exception:
        logger.debug("OTLP duration metric record failed", exc_info=True)


def _signal_endpoint(endpoint: str, protocol: str, signal: str) -> Optional[str]:
    """Return the HTTP OTLP URL for ``signal``, or None if it would be invalid.

    Collectors that take a base URL get ``/v1/{signal}`` appended. A URL
    that already ends in that suffix is left alone. A URL that already
    names a *different* signal (Datadog traces-only intake) returns
    None so the caller can skip that exporter instead of posting to
    ``.../v1/traces/v1/metrics``.
    """
    cleaned = endpoint.strip().rstrip("/")
    if protocol not in _HTTP_PROTOCOLS:
        return cleaned
    suffix = f"/v1/{signal}"
    if cleaned.endswith(suffix):
        return cleaned
    for other in ("traces", "metrics", "logs"):
        if other != signal and cleaned.endswith(f"/v1/{other}"):
            return None
    return f"{cleaned}{suffix}"


def _grpc_endpoint(endpoint: str) -> tuple[str, bool]:
    parsed = urlparse(endpoint)
    if parsed.scheme:
        host = parsed.netloc or parsed.path
        insecure = parsed.scheme in {"http", "grpc"}
        return host, insecure
    return endpoint, True


def _operation_name(endpoint_kind: str) -> str:
    kind = (endpoint_kind or "").lower()
    if "gemini" in kind or "generate" in kind:
        return "generate_content"
    return "chat"


def _span_timestamps(usage: Any) -> tuple[int, int]:
    end_ns = _now_ns()
    timestamp = getattr(usage, "timestamp", None)
    if timestamp is not None:
        try:
            end_ns = int(timestamp.timestamp() * 1_000_000_000)
        except Exception:
            end_ns = _now_ns()
    duration_s = float(getattr(usage, "duration", 0.0) or 0.0)
    start_ns = end_ns - int(duration_s * 1_000_000_000)
    if start_ns <= 0:
        start_ns = end_ns
    return start_ns, end_ns


def _now_ns() -> int:
    import time

    return time.time_ns()


def _clamp_ratio(value: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return 1.0
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio


def _safe_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if key in _FORBIDDEN_ATTR_KEYS:
            continue
        if any(key.startswith(prefix) for prefix in _FORBIDDEN_ATTR_PREFIXES):
            continue
        if isinstance(value, (str, bool, int, float)):
            safe[key] = value
        elif isinstance(value, tuple):
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe


def _remember_session_span(session_id: str, span: trace.Span) -> None:
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return
    with _lock:
        if len(_state.session_span_context) >= _SESSION_CONTEXT_MAX:
            _state.session_span_context.pop(next(iter(_state.session_span_context)))
        _state.session_span_context[session_id] = ctx
