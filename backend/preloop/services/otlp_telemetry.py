"""OTLP/HTTP ingest of Claude client telemetry (issue #1412).

Claude Code, Claude Desktop and Cowork export OpenTelemetry metrics, log
events and (opt-in) traces. Behind a Claude apps gateway the exports are
relayed verbatim to each ``telemetry.forward_to`` destination; this module
is what a Preloop destination does with them.

Pipeline, per request:

1. :func:`decompress_body` and :func:`decode_request`: protobuf or OTLP JSON
   into the same ``opentelemetry-proto`` message types, with a 4 MiB cap on
   the decompressed body (stream-decompressed, so a gzip bomb stops at the
   cap).
2. :func:`extract_logs` and :func:`extract_metrics`: an allowlist. Only
   ``claude_code.api_request`` / ``claude_code.api_error`` events and the
   ``cost.usage``, ``token.usage`` and ``session.count`` metrics survive, and
   of those only the attributes in :data:`LOG_ATTRIBUTE_ALLOWLIST` /
   :data:`METRIC_ATTRIBUTE_ALLOWLIST`. Everything else (prompts, tool
   results, commands, file paths, raw API bodies) is counted and dropped
   before anything touches the database.
3. :class:`TelemetryIngestor`: reconciliation against ``api_usage`` with the
   never-double-count rules (see the class docstring).

Attribute and event names follow the Claude Code monitoring reference and
the apps gateway ``telemetry`` reference.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from google.protobuf import json_format
from google.protobuf.message import DecodeError, Message
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import (
    crud_api_usage,
    crud_gateway_subject,
    crud_telemetry_ingest,
)

logger = logging.getLogger(__name__)

# --- Limits (scope 3) ---------------------------------------------------------

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_RECORDS_PER_REQUEST = 10_000
MAX_ATTRIBUTE_VALUE_BYTES = 1024
MAX_ATTRIBUTES_PER_RECORD = 64

SIGNAL_LOGS = "logs"
SIGNAL_METRICS = "metrics"
SIGNAL_TRACES = "traces"

ENCODING_PROTOBUF = "protobuf"
ENCODING_JSON = "json"
MEDIA_TYPES = {
    ENCODING_PROTOBUF: "application/x-protobuf",
    ENCODING_JSON: "application/json",
}

OTLP_SOURCE = "otlp"
TELEMETRY_COST_SOURCE = "telemetry_estimate"

# --- Allowlists (scope 4) -----------------------------------------------------

#: Log events that are stored. Claude Code names them ``claude_code.<name>``
#: in the record body / event name and ``<name>`` in ``event.name``.
ALLOWED_LOG_EVENTS = frozenset({"api_request", "api_error"})

IDENTITY_ATTRIBUTES = (
    "user.email",
    "user.id",
    "enduser.id",
    "enduser.sub",
    "user.groups",
)

#: Attributes kept from an allowed log event (resource and record merged).
LOG_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "request_id",
        "client_request_id",
        "session.id",
        "prompt.id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "duration_ms",
        "status_code",
        "terminal.type",
        "service.name",
        "service.version",
        "organization.id",
        "identity.source",
        *IDENTITY_ATTRIBUTES,
    }
)

METRIC_COST = "claude_code.cost.usage"
METRIC_TOKENS = "claude_code.token.usage"
METRIC_SESSIONS = "claude_code.session.count"
ALLOWED_METRICS = frozenset({METRIC_COST, METRIC_TOKENS, METRIC_SESSIONS})

#: Attributes kept from an allowed metric data point.
METRIC_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "session.id",
        "model",
        "type",
        "terminal.type",
        "service.name",
        "service.version",
        "organization.id",
        "identity.source",
        *IDENTITY_ATTRIBUTES,
    }
)

#: ``claude_code.token.usage`` ``type`` values and the usage columns they feed.
TOKEN_TYPE_COLUMNS = {
    "input": "prompt_tokens",
    "output": "completion_tokens",
    "cacheRead": "cache_read_tokens",
    "cacheCreation": "cache_creation_tokens",
}

#: Terminal sessions signed in through the apps gateway mark their exports
#: with this value; only then is ``user.id`` the IdP subject. Elsewhere
#: (Desktop, signed-out CLI) ``user.id`` is a random anonymous id.
GATEWAY_IDENTITY_SOURCE = "gateway-oidc"

_TEMPORALITY_DELTA = 1
_TEMPORALITY_CUMULATIVE = 2


class OtlpError(Exception):
    """Base for request-level OTLP failures, carrying the HTTP status.

    ``public_message`` is the only text a client sees; the exception's own
    message (which may quote client input) stays in server logs.
    """

    status_code = 400
    public_message = "Bad request"


class OtlpDecodeError(OtlpError):
    """Undecodable body (400)."""

    status_code = 400
    public_message = "Undecodable OTLP request body"


class OtlpTooLargeError(OtlpError):
    """Body over the decompressed size limit (413)."""

    status_code = 413
    public_message = "Request body exceeds 4 MiB after decompression"


class OtlpUnsupportedMediaError(OtlpError):
    """Content type or content encoding not supported (415)."""

    status_code = 415
    public_message = (
        "Unsupported media: send application/x-protobuf or application/json, "
        "optionally gzip encoded"
    )


# --- Transport ----------------------------------------------------------------


def parse_content_type(header: Optional[str]) -> str:
    """Map a ``Content-Type`` header to an encoding name.

    Raises:
        OtlpUnsupportedMediaError: For anything but protobuf or JSON.
    """
    media = (header or "").split(";", 1)[0].strip().lower()
    if media == "application/x-protobuf":
        return ENCODING_PROTOBUF
    if media == "application/json":
        return ENCODING_JSON
    raise OtlpUnsupportedMediaError(f"Unsupported content type: {media or 'none'}")


def decompress_body(body: bytes, content_encoding: Optional[str]) -> bytes:
    """Undo ``Content-Encoding`` with a hard output cap.

    gzip is stream-decompressed with ``max_length`` so a small body that
    inflates past :data:`MAX_BODY_BYTES` is refused without inflating it.

    Raises:
        OtlpTooLargeError: Over the size cap.
        OtlpDecodeError: Corrupt gzip.
        OtlpUnsupportedMediaError: Any other encoding.
    """
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        if len(body) > MAX_BODY_BYTES:
            raise OtlpTooLargeError("Request body exceeds 4 MiB")
        return body
    if encoding != "gzip":
        raise OtlpUnsupportedMediaError(f"Unsupported content encoding: {encoding}")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(body, MAX_BODY_BYTES + 1)
    except zlib.error as exc:
        raise OtlpDecodeError("Invalid gzip body") from exc
    if len(out) > MAX_BODY_BYTES or inflater.unconsumed_tail:
        raise OtlpTooLargeError("Decompressed request body exceeds 4 MiB")
    if not inflater.eof:
        raise OtlpDecodeError("Truncated gzip body")
    return out


_REQUEST_TYPES: Dict[str, Any] = {
    SIGNAL_LOGS: logs_service_pb2.ExportLogsServiceRequest,
    SIGNAL_METRICS: metrics_service_pb2.ExportMetricsServiceRequest,
    SIGNAL_TRACES: trace_service_pb2.ExportTraceServiceRequest,
}

#: OTLP JSON encodes trace and span ids as hex, protobuf JSON mapping as
#: base64. Nothing here uses them, so they are removed before parsing.
_JSON_ID_FIELDS = frozenset({"traceId", "spanId", "parentSpanId"})


def _strip_json_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_json_ids(item)
            for key, item in value.items()
            if key not in _JSON_ID_FIELDS
        }
    if isinstance(value, list):
        return [_strip_json_ids(item) for item in value]
    return value


def decode_request(signal: str, body: bytes, encoding: str) -> Message:
    """Decode an export request into its ``opentelemetry-proto`` message.

    Raises:
        OtlpDecodeError: When the body does not parse.
    """
    message = _REQUEST_TYPES[signal]()
    try:
        if encoding == ENCODING_PROTOBUF:
            message.ParseFromString(body)
        else:
            payload = json.loads(body.decode("utf-8")) if body.strip() else {}
            if not isinstance(payload, dict):
                raise OtlpDecodeError("OTLP JSON body must be an object")
            json_format.ParseDict(
                _strip_json_ids(payload), message, ignore_unknown_fields=True
            )
    except OtlpDecodeError:
        raise
    except (DecodeError, json_format.ParseError, ValueError, TypeError) as exc:
        raise OtlpDecodeError(f"Undecodable OTLP {signal} body") from exc
    return message


def encode_response(
    signal: str, encoding: str, *, rejected: int = 0, error_message: str = ""
) -> bytes:
    """Build the ``Export*ServiceResponse`` in the request's encoding."""
    if signal == SIGNAL_LOGS:
        response: Message = logs_service_pb2.ExportLogsServiceResponse()
        if rejected:
            response.partial_success.rejected_log_records = rejected
    elif signal == SIGNAL_METRICS:
        response = metrics_service_pb2.ExportMetricsServiceResponse()
        if rejected:
            response.partial_success.rejected_data_points = rejected
    else:
        response = trace_service_pb2.ExportTraceServiceResponse()
        if rejected:
            response.partial_success.rejected_spans = rejected
    if rejected and error_message:
        response.partial_success.error_message = error_message
    if encoding == ENCODING_PROTOBUF:
        return bytes(response.SerializeToString())
    return str(json_format.MessageToJson(response, indent=None)).encode("utf-8")


# --- Attribute handling -------------------------------------------------------


def _truncate(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= MAX_ATTRIBUTE_VALUE_BYTES:
        return value
    return raw[:MAX_ATTRIBUTE_VALUE_BYTES].decode("utf-8", errors="ignore")


def _scalar(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return _truncate(value.string_value)
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    # Arrays, maps and bytes are never on the allowlist.
    return None


def attributes(pairs: Iterable[KeyValue]) -> Dict[str, Any]:
    """Flatten OTLP attributes to scalars, truncated and capped.

    Values are cut to :data:`MAX_ATTRIBUTE_VALUE_BYTES`; at most
    :data:`MAX_ATTRIBUTES_PER_RECORD` attributes are read per record.
    """
    out: Dict[str, Any] = {}
    for pair in pairs:
        if len(out) >= MAX_ATTRIBUTES_PER_RECORD:
            break
        value = _scalar(pair.value)
        if value is not None:
            out[_truncate(pair.key)] = value
    return out


def _keep(attrs: Dict[str, Any], allowlist: frozenset[str]) -> Dict[str, Any]:
    return {key: value for key, value in attrs.items() if key in allowlist}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _nanos_to_datetime(nanos: int) -> Optional[datetime]:
    if not nanos:
        return None
    try:
        return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _digest(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_dedup_key(
    *,
    account_id: Any,
    signal: str,
    resource: Dict[str, Any],
    scope_name: str,
    scope_version: str,
    record_time: int,
    identity: str,
) -> str:
    """sha256 over the record identity (scope 8).

    ``identity`` is the ``request_id`` for log events, or the metric name
    plus series attributes for data points.
    """
    return _digest(
        str(account_id),
        signal,
        resource,
        scope_name,
        scope_version,
        record_time,
        identity,
    )


# --- Extraction ---------------------------------------------------------------


@dataclass
class LogEvent:
    """An allowlisted ``api_request`` / ``api_error`` event."""

    event: str
    attrs: Dict[str, Any]
    timestamp: datetime
    dedup_key: str

    @property
    def request_id(self) -> Optional[str]:
        return _as_str(self.attrs.get("request_id"))

    @property
    def client_request_id(self) -> Optional[str]:
        return _as_str(self.attrs.get("client_request_id"))

    @property
    def session_id(self) -> Optional[str]:
        return _as_str(self.attrs.get("session.id"))


@dataclass
class MetricPoint:
    """An allowlisted metric data point."""

    name: str
    value: float
    cumulative: bool
    attrs: Dict[str, Any]
    timestamp: datetime
    start_time: Optional[datetime]
    series_key: str
    dedup_key: str

    @property
    def session_id(self) -> Optional[str]:
        return _as_str(self.attrs.get("session.id"))


@dataclass
class Extraction:
    """What survived the allowlist, and how much did not."""

    logs: List[LogEvent] = field(default_factory=list)
    metrics: List[MetricPoint] = field(default_factory=list)
    total: int = 0
    dropped: int = 0
    over_limit: int = 0

    @property
    def rejected(self) -> int:
        return self.dropped + self.over_limit


def _event_name(record: Any, attrs: Dict[str, Any]) -> str:
    name = _as_str(attrs.get("event.name")) or _as_str(
        getattr(record, "event_name", "")
    )
    if not name and record.body.WhichOneof("value") == "string_value":
        name = record.body.string_value.strip()
    name = name or ""
    if name.startswith("claude_code."):
        name = name[len("claude_code.") :]
    return name


def extract_logs(request: Any, *, account_id: Any, now: datetime) -> Extraction:
    """Keep allowlisted events and attributes; count the rest as dropped."""
    result = Extraction()
    for resource_logs in request.resource_logs:
        resource = attributes(resource_logs.resource.attributes)
        for scope_logs in resource_logs.scope_logs:
            scope_name = scope_logs.scope.name
            scope_version = scope_logs.scope.version
            for record in scope_logs.log_records:
                result.total += 1
                if result.total > MAX_RECORDS_PER_REQUEST:
                    result.over_limit += 1
                    continue
                record_attrs = attributes(record.attributes)
                event = _event_name(record, record_attrs)
                if event not in ALLOWED_LOG_EVENTS:
                    result.dropped += 1
                    continue
                kept = _keep({**resource, **record_attrs}, LOG_ATTRIBUTE_ALLOWLIST)
                record_time = record.time_unix_nano or record.observed_time_unix_nano
                identity = (
                    _as_str(kept.get("request_id"))
                    or _as_str(kept.get("client_request_id"))
                    or _digest(event, kept)
                )
                result.logs.append(
                    LogEvent(
                        event=event,
                        attrs=kept,
                        timestamp=_nanos_to_datetime(record_time) or now,
                        dedup_key=record_dedup_key(
                            account_id=account_id,
                            signal=SIGNAL_LOGS,
                            resource=resource,
                            scope_name=scope_name,
                            scope_version=scope_version,
                            record_time=record_time,
                            identity=f"{event}:{identity}",
                        ),
                    )
                )
    return result


def _number_points(metric: Any) -> Tuple[Iterable[Any], bool, bool]:
    """Return (points, cumulative, supported) for a metric."""
    kind = metric.WhichOneof("data")
    if kind == "sum":
        return (
            metric.sum.data_points,
            metric.sum.aggregation_temporality == _TEMPORALITY_CUMULATIVE,
            True,
        )
    if kind == "gauge":
        return metric.gauge.data_points, False, True
    return (), False, False


def _count_points(metric: Any) -> int:
    kind = metric.WhichOneof("data")
    if kind is None:
        return 0
    return len(getattr(metric, kind).data_points)


def extract_metrics(request: Any, *, account_id: Any, now: datetime) -> Extraction:
    """Keep allowlisted metrics and attributes; count the rest as dropped."""
    result = Extraction()
    for resource_metrics in request.resource_metrics:
        resource = attributes(resource_metrics.resource.attributes)
        for scope_metrics in resource_metrics.scope_metrics:
            scope_name = scope_metrics.scope.name
            scope_version = scope_metrics.scope.version
            for metric in scope_metrics.metrics:
                points, cumulative, supported = _number_points(metric)
                if metric.name not in ALLOWED_METRICS or not supported:
                    count = _count_points(metric)
                    allowed = max(0, MAX_RECORDS_PER_REQUEST - result.total)
                    result.total += count
                    result.dropped += min(count, allowed)
                    result.over_limit += count - min(count, allowed)
                    continue
                for point in points:
                    result.total += 1
                    if result.total > MAX_RECORDS_PER_REQUEST:
                        result.over_limit += 1
                        continue
                    point_attrs = attributes(point.attributes)
                    kept = _keep(
                        {**resource, **point_attrs}, METRIC_ATTRIBUTE_ALLOWLIST
                    )
                    kind = point.WhichOneof("value")
                    value = float(point.as_int if kind == "as_int" else point.as_double)
                    series_attrs = {**resource, **point_attrs}
                    series_key = _digest(
                        str(account_id),
                        metric.name,
                        series_attrs,
                        scope_name,
                        point.start_time_unix_nano,
                    )
                    result.metrics.append(
                        MetricPoint(
                            name=metric.name,
                            value=value,
                            cumulative=cumulative,
                            attrs=kept,
                            timestamp=_nanos_to_datetime(point.time_unix_nano) or now,
                            start_time=_nanos_to_datetime(point.start_time_unix_nano),
                            series_key=series_key,
                            dedup_key=record_dedup_key(
                                account_id=account_id,
                                signal=SIGNAL_METRICS,
                                resource=resource,
                                scope_name=scope_name,
                                scope_version=scope_version,
                                record_time=point.time_unix_nano,
                                identity=_digest(metric.name, point_attrs, value),
                            ),
                        )
                    )
    return result


def count_spans(request: Any) -> int:
    """Number of spans in a trace export (traces are not stored in v1)."""
    return sum(
        len(scope_spans.spans)
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
    )


# --- Reconciliation -----------------------------------------------------------


@dataclass
class IngestContext:
    """Who is ingesting: the ingest key and its account."""

    account_id: UUID
    api_key_id: UUID
    now: datetime


@dataclass
class IngestStats:
    """Outcome counters, for logs and tests."""

    enriched: int = 0
    created: int = 0
    duplicates: int = 0
    aggregated: int = 0
    suppressed: int = 0


def _identity(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {key: attrs[key] for key in IDENTITY_ATTRIBUTES if key in attrs}


def _external_subject(attrs: Dict[str, Any]) -> Optional[str]:
    """IdP subject from telemetry, never an anonymous id (scope 6).

    Desktop and Cowork carry ``enduser.sub``. Terminal sessions signed in
    through the gateway carry the subject as ``user.id`` and mark it with
    ``identity.source: gateway-oidc``; without that mark ``user.id`` is the
    anonymous install id and is ignored.
    """
    sub = _as_str(attrs.get("enduser.sub"))
    if sub:
        return sub
    if attrs.get("identity.source") == GATEWAY_IDENTITY_SOURCE:
        return _as_str(attrs.get("user.id"))
    return None


def _client_label(attrs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: attrs[key]
        for key in ("service.name", "service.version", "terminal.type")
        if key in attrs
    }


class TelemetryIngestor:
    """Turn allowlisted telemetry into ``api_usage`` changes, never twice.

    Rules (scope 5):

    * An ``api_request`` / ``api_error`` event whose request matches a
      gateway row (``meta_data.client_request_id`` first, then
      ``upstream_request_id``) enriches that row's ``meta_data.telemetry``
      and creates nothing.
    * An unmatched ``api_request`` creates one ``imported`` row with
      ``cost_source='telemetry_estimate'``, unless an OTLP row for the same
      request already exists. Unmatched ``api_error`` events create nothing.
    * Metrics create hourly aggregates per (session, model) only when the
      session has neither log events nor gateway rows. A session whose log
      events arrive later loses its aggregates in the same transaction.
    * A gateway row recorded after an OTLP row for the same request removes
      the OTLP row (``CRUDTelemetryIngest.supersede_for_gateway_row``).
    * Every stored record claims a dedup key first; a re-sent batch is a
      no-op.
    """

    def __init__(self, db: Session, context: IngestContext) -> None:
        self.db = db
        self.ctx = context
        self.stats = IngestStats()
        self._subjects: Dict[Tuple[str, Optional[str]], Any] = {}
        self._sessions: Dict[str, Optional[Any]] = {}
        # Per-batch caches (one query per distinct key, not per record).
        self._session_covered: Dict[str, bool] = {}
        self._person_covered: Dict[Tuple[Any, ...], bool] = {}
        self._aggregates: Dict[str, Any] = {}
        self._series: Dict[str, Tuple[float, datetime, Optional[datetime]]] = {}
        self._dirty_series: set[str] = set()
        self._logged_sessions: set[str] = set()

    # -- shared helpers --

    def _subject(self, attrs: Dict[str, Any]) -> Any:
        sub = _external_subject(attrs)
        if not sub:
            return None
        email = _as_str(attrs.get("user.email"))
        key = (sub, email)
        if key not in self._subjects:
            self._subjects[key] = self._resolve_subject(sub, email)
        return self._subjects[key]

    def _resolve_subject(self, sub: str, email: Optional[str]) -> Any:
        """Resolve on a short-lived session, as the gateway path does.

        ``CRUDGatewaySubject.resolve`` commits. On the ingest session that
        would end the batch transaction and release the per-session advisory
        locks mid-batch, so it runs on its own session and only a detached
        reference comes back.
        """
        from preloop.services.gateway_upstream_identity import GatewaySubjectRef

        with Session(bind=self.db.get_bind(), expire_on_commit=False) as session:
            subject = crud_gateway_subject.resolve(
                session,
                account_id=self.ctx.account_id,
                api_key_id=self.ctx.api_key_id,
                external_subject=sub,
                email=email,
                now=self.ctx.now.replace(tzinfo=timezone.utc),
            )
            return GatewaySubjectRef(
                id=subject.id,
                external_subject=subject.external_subject,
                email=subject.email,
                linked_user_id=subject.linked_user_id,
                api_key_id=subject.api_key_id,
            )

    def _runtime_session(self, session_id: Optional[str]) -> Optional[Any]:
        if not session_id:
            return None
        if session_id not in self._sessions:
            self._sessions[session_id] = crud_telemetry_ingest.find_runtime_session(
                self.db, account_id=self.ctx.account_id, session_id=session_id
            )
        return self._sessions[session_id]

    def _claim(self, dedup_key: str, kind: str) -> bool:
        return crud_telemetry_ingest.claim(
            self.db,
            account_id=self.ctx.account_id,
            dedup_key=dedup_key,
            kind=kind,
            now=self.ctx.now,
        )

    def _claim_batch(self, keys: List[str], kind: str) -> set[str]:
        return crud_telemetry_ingest.claim_many(
            self.db,
            account_id=self.ctx.account_id,
            keys=[(key, kind) for key in keys],
            now=self.ctx.now,
        )

    def _session_logs_key(self, session_id: str) -> str:
        return _digest(str(self.ctx.account_id), "session_logs", session_id)

    def _telemetry_meta(self, attrs: Dict[str, Any], subject: Any) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "source": OTLP_SOURCE,
            "session_id": _as_str(attrs.get("session.id")),
            "prompt_id": _as_str(attrs.get("prompt.id")),
            "request_id": _as_str(attrs.get("request_id")),
            "client_request_id": _as_str(attrs.get("client_request_id")),
            "model": _as_str(attrs.get("model")),
            "cost_usd": _as_float(attrs.get("cost_usd")),
            "duration_ms": _as_float(attrs.get("duration_ms")),
            "status_code": _as_int(attrs.get("status_code")),
            "organization_id": _as_str(attrs.get("organization.id")),
            "client": _client_label(attrs) or None,
            "identity": _identity(attrs) or None,
            "gateway_subject_id": str(subject.id) if subject is not None else None,
        }
        return {key: value for key, value in meta.items() if value is not None}

    # -- logs --

    def _lock_sessions(self, session_ids: Iterable[Optional[str]]) -> None:
        for session_id in sorted({sid for sid in session_ids if sid}):
            crud_telemetry_ingest.lock_session(
                self.db, account_id=self.ctx.account_id, session_id=session_id
            )

    def ingest_logs(self, events: List[LogEvent]) -> IngestStats:
        """Apply allowlisted log events. Caller commits."""
        self._lock_sessions(event.session_id for event in events)
        fresh = self._claim_batch([event.dedup_key for event in events], "log")
        for event in events:
            if event.dedup_key not in fresh:
                self.stats.duplicates += 1
                continue
            fresh.discard(event.dedup_key)
            session_id = event.session_id
            if session_id and session_id not in self._logged_sessions:
                self._logged_sessions.add(session_id)
                self._claim(self._session_logs_key(session_id), "session_logs")
                crud_telemetry_ingest.delete_session_aggregates(
                    self.db, account_id=self.ctx.account_id, session_id=session_id
                )
            self._apply_log(event)
        return self.stats

    def _apply_log(self, event: LogEvent) -> None:
        attrs = event.attrs
        subject = self._subject(attrs)
        telemetry = {"event": event.event, **self._telemetry_meta(attrs, subject)}
        match = "request_id"
        gateway_row = crud_telemetry_ingest.find_gateway_usage(
            self.db,
            account_id=self.ctx.account_id,
            client_request_id=event.client_request_id,
            request_id=event.request_id,
        )
        if gateway_row is None:
            match = "signature"
            gateway_row = crud_telemetry_ingest.find_gateway_usage_by_signature(
                self.db,
                account_id=self.ctx.account_id,
                timestamp=event.timestamp,
                model=_as_str(attrs.get("model")),
                output_tokens=_as_int(attrs.get("output_tokens")),
                email=_as_str(attrs.get("user.email")),
                external_subject=_external_subject(attrs),
            )
        if gateway_row is not None:
            enriched = dict(gateway_row.meta_data or {})
            enriched["telemetry"] = {**telemetry, "match": match}
            gateway_row.meta_data = enriched
            flag_modified(gateway_row, "meta_data")
            self.db.flush()
            self.stats.enriched += 1
            return
        if event.event != "api_request":
            return
        if crud_telemetry_ingest.find_otlp_rows(
            self.db,
            account_id=self.ctx.account_id,
            client_request_id=event.client_request_id,
            request_id=event.request_id,
        ):
            # Same request already landed under another dedup key (e.g. a
            # relay re-stamped resource attributes). One row per request.
            self.stats.duplicates += 1
            return
        runtime_session = self._runtime_session(event.session_id)
        cost = _as_float(attrs.get("cost_usd"))
        meta: Dict[str, Any] = {
            "source": OTLP_SOURCE,
            "telemetry": telemetry,
            "otlp_request_id": event.request_id,
            "otlp_client_request_id": event.client_request_id,
            "gateway_subject_id": str(subject.id) if subject is not None else None,
            "gateway_subject_email": subject.email if subject is not None else None,
        }
        row = crud_api_usage.log_imported_usage_event(
            self.db,
            account_id=str(self.ctx.account_id),
            user_id=(
                str(subject.linked_user_id)
                if subject is not None and subject.linked_user_id
                else None
            ),
            timestamp=event.timestamp,
            model_alias=_as_str(attrs.get("model")),
            source=OTLP_SOURCE,
            prompt_tokens=_as_int(attrs.get("input_tokens")),
            completion_tokens=_as_int(attrs.get("output_tokens")),
            cache_read_tokens=_as_int(attrs.get("cache_read_tokens")),
            cache_creation_tokens=_as_int(attrs.get("cache_creation_tokens")),
            cost_usd=cost,
            cost_source=TELEMETRY_COST_SOURCE if cost is not None else None,
            cost_basis="estimated",
            conversation_id=event.session_id,
            runtime_session_id=runtime_session.id if runtime_session else None,
            import_fingerprint=event.dedup_key,
            meta_data={key: value for key, value in meta.items() if value is not None},
            endpoint="/telemetry/otlp/v1/logs",
            commit=False,
        )
        if row is not None:
            self.stats.created += 1

    # -- metrics --

    def ingest_metrics(self, points: List[MetricPoint]) -> IngestStats:
        """Apply allowlisted metric points. Caller commits."""
        self._lock_sessions(point.session_id for point in points)
        fresh = self._claim_batch([point.dedup_key for point in points], "metric")
        for point in points:
            if point.dedup_key not in fresh:
                self.stats.duplicates += 1
                continue
            fresh.discard(point.dedup_key)
            delta = self._delta(point)
            if point.name == METRIC_SESSIONS or delta <= 0:
                continue
            if self._session_is_covered(point.session_id) or self._person_is_covered(
                point
            ):
                self.stats.suppressed += 1
                continue
            self._aggregate(point, delta)
        self._write_series()
        self.db.flush()
        return self.stats

    def _delta(self, point: MetricPoint) -> float:
        if not point.cumulative:
            return point.value
        state = self._series.get(point.series_key)
        if state is None and point.series_key not in self._dirty_series:
            series = crud_telemetry_ingest.get_series(
                self.db,
                account_id=self.ctx.account_id,
                series_key=point.series_key,
                now=self.ctx.now,
            )
            if series is not None:
                state = (series.last_value, series.last_time, series.start_time)
        if state is not None and point.timestamp <= state[1]:
            # Not newer than the stored point: already counted. Checked before
            # the reset test so a late, lower retry is never counted again.
            return 0.0
        if state is None or point.value < state[0]:
            # First sight of the series, or the counter reset.
            delta = point.value
        else:
            delta = point.value - state[0]
        self._series[point.series_key] = (
            point.value,
            point.timestamp,
            point.start_time,
        )
        self._dirty_series.add(point.series_key)
        return delta

    def _write_series(self) -> None:
        for series_key in sorted(self._dirty_series):
            value, point_time, start_time = self._series[series_key]
            crud_telemetry_ingest.put_series(
                self.db,
                account_id=self.ctx.account_id,
                series_key=series_key,
                value=value,
                point_time=point_time,
                start_time=start_time,
                now=self.ctx.now,
            )
        self._dirty_series.clear()

    def _session_is_covered(self, session_id: Optional[str]) -> bool:
        """Logs or gateway rows exist for the session: metrics add nothing."""
        if not session_id:
            return False
        if session_id not in self._session_covered:
            self._session_covered[session_id] = self._session_covered_query(session_id)
        return self._session_covered[session_id]

    def _session_covered_query(self, session_id: str) -> bool:
        if crud_telemetry_ingest.is_claimed(
            self.db,
            account_id=self.ctx.account_id,
            dedup_key=self._session_logs_key(session_id),
        ):
            return True
        runtime_session = self._runtime_session(session_id)
        return runtime_session is not None and (
            crud_telemetry_ingest.session_has_gateway_rows(
                self.db,
                account_id=self.ctx.account_id,
                runtime_session_id=runtime_session.id,
            )
        )

    def _person_is_covered(self, point: MetricPoint) -> bool:
        """The person has gateway rows in this hour: metrics add nothing."""
        hour = point.timestamp.replace(minute=0, second=0, microsecond=0)
        email = _as_str(point.attrs.get("user.email"))
        sub = _external_subject(point.attrs)
        key = (email, sub, hour)
        if key not in self._person_covered:
            self._person_covered[key] = crud_telemetry_ingest.identity_has_gateway_rows(
                self.db,
                account_id=self.ctx.account_id,
                email=email,
                external_subject=sub,
                hour_start=hour,
            )
        return self._person_covered[key]

    def _aggregate(self, point: MetricPoint, delta: float) -> None:
        attrs = point.attrs
        session_id = point.session_id
        model = _as_str(attrs.get("model"))
        hour = point.timestamp.replace(minute=0, second=0, microsecond=0)
        fingerprint = _digest(
            str(self.ctx.account_id), "otlp_metric_aggregate", session_id, model, hour
        )
        row = self._aggregates.get(fingerprint)
        if row is None:
            row = crud_telemetry_ingest.get_imported_by_fingerprint(
                self.db, account_id=self.ctx.account_id, fingerprint=fingerprint
            )
        if row is None:
            subject = self._subject(attrs)
            runtime_session = self._runtime_session(session_id)
            aggregate_meta: Dict[str, Any] = {
                "source": OTLP_SOURCE,
                "aggregate": True,
                "aggregate_hour": hour.isoformat(),
                "telemetry": self._telemetry_meta(
                    {key: value for key, value in attrs.items() if key != "type"},
                    subject,
                ),
                "gateway_subject_id": str(subject.id) if subject is not None else None,
                "gateway_subject_email": subject.email if subject is not None else None,
            }
            row = crud_api_usage.log_imported_usage_event(
                self.db,
                account_id=str(self.ctx.account_id),
                user_id=(
                    str(subject.linked_user_id)
                    if subject is not None and subject.linked_user_id
                    else None
                ),
                timestamp=hour,
                model_alias=model,
                source=OTLP_SOURCE,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.0,
                cost_source=TELEMETRY_COST_SOURCE,
                cost_basis="estimated",
                conversation_id=session_id,
                runtime_session_id=runtime_session.id if runtime_session else None,
                import_fingerprint=fingerprint,
                meta_data={k: v for k, v in aggregate_meta.items() if v is not None},
                endpoint="/telemetry/otlp/v1/metrics",
                skip_fingerprint_lookup=True,
                commit=False,
            )
            if row is None:
                # A concurrent batch created it between our read and insert.
                row = crud_telemetry_ingest.get_imported_by_fingerprint(
                    self.db, account_id=self.ctx.account_id, fingerprint=fingerprint
                )
            if row is None:
                return
        self._aggregates[fingerprint] = row
        if point.name == METRIC_COST:
            row.estimated_cost = (row.estimated_cost or 0.0) + delta
        else:
            column = TOKEN_TYPE_COLUMNS.get(_as_str(attrs.get("type")) or "")
            if column is None:
                return
            setattr(row, column, (getattr(row, column) or 0) + int(round(delta)))
            row.total_tokens = (row.prompt_tokens or 0) + (row.completion_tokens or 0)
        self.stats.aggregated += 1


def utc_now() -> datetime:
    """Naive UTC now, the convention of ``api_usage.timestamp``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = [
    "MAX_BODY_BYTES",
    "IngestContext",
    "OtlpError",
    "TelemetryIngestor",
    "count_spans",
    "decode_request",
    "decompress_body",
    "encode_response",
    "extract_logs",
    "extract_metrics",
    "parse_content_type",
    "utc_now",
]
