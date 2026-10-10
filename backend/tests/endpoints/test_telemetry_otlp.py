"""OTLP/HTTP telemetry ingest (issue #1412).

Bodies are built with the ``opentelemetry-proto`` classes and posted as
protobuf or OTLP JSON, exactly as a Claude client or the apps gateway relay
sends them.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException
from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from starlette.requests import Request

from preloop.api.auth.key_scopes import (
    TELEMETRY_INGEST_SCOPE,
    api_key_allowed_on_channel,
    enforce_api_key_route_scope,
    is_single_purpose_api_key,
)
from preloop.api.endpoints import telemetry_otlp
from preloop.models import models
from preloop.models.crud import crud_api_key, crud_api_usage
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.gateway_subject import GatewaySubject
from preloop.models.models.telemetry_ingest import TelemetryMetricSeries
from preloop.services import otlp_telemetry as otlp

LOGS = "/api/v1/telemetry/otlp/v1/logs"
METRICS = "/api/v1/telemetry/otlp/v1/metrics"
TRACES = "/api/v1/telemetry/otlp/v1/traces"
PB = "application/x-protobuf"
JSON = "application/json"

SECRET_PROMPT = "please refactor /Users/alice/secret/project.py and rm -rf build"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    telemetry_otlp.reset_rate_limits()
    yield
    telemetry_otlp.reset_rate_limits()


# --- builders -----------------------------------------------------------------


def _kv(key: str, value: Any) -> KeyValue:
    if isinstance(value, bool):
        any_value = AnyValue(bool_value=value)
    elif isinstance(value, int):
        any_value = AnyValue(int_value=value)
    elif isinstance(value, float):
        any_value = AnyValue(double_value=value)
    else:
        any_value = AnyValue(string_value=str(value))
    return KeyValue(key=key, value=any_value)


def _nanos(when: Optional[datetime] = None) -> int:
    when = when or datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc)
    return int(when.timestamp() * 1e9)


def _log_record(event: str, attrs: dict, when: Optional[datetime] = None) -> Any:
    return logs_pb2.LogRecord(
        time_unix_nano=_nanos(when),
        body=AnyValue(string_value=f"claude_code.{event}"),
        attributes=[_kv("event.name", event), *[_kv(k, v) for k, v in attrs.items()]],
    )


def _logs_request(*records: Any, resource: Optional[dict] = None) -> Any:
    resource = resource or {"service.name": "claude-code", "service.version": "2.1.280"}
    return logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                resource={"attributes": [_kv(k, v) for k, v in resource.items()]},
                scope_logs=[
                    logs_pb2.ScopeLogs(
                        scope={
                            "name": "com.anthropic.claude_code.events",
                            "version": "2.1.280",
                        },
                        log_records=list(records),
                    )
                ],
            )
        ]
    )


def _api_request(
    request_id: str = "req_011abc",
    client_request_id: Optional[str] = None,
    session_id: str = "11111111-1111-4111-8111-111111111111",
    **extra: Any,
) -> Any:
    attrs = {
        "request_id": request_id,
        "session.id": session_id,
        "prompt.id": "22222222-2222-4222-8222-222222222222",
        "model": "claude-sonnet-5",
        "input_tokens": 120,
        "output_tokens": 45,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 10,
        "cost_usd": 0.0123,
        "duration_ms": 900,
        **extra,
    }
    if client_request_id:
        attrs["client_request_id"] = client_request_id
    return _log_record("api_request", attrs)


def _sum_metric(
    name: str,
    points: list[tuple[float, dict]],
    *,
    cumulative: bool = False,
    when: Optional[datetime] = None,
    start: Optional[datetime] = None,
) -> Any:
    return metrics_pb2.Metric(
        name=name,
        sum=metrics_pb2.Sum(
            aggregation_temporality=2 if cumulative else 1,
            is_monotonic=True,
            data_points=[
                metrics_pb2.NumberDataPoint(
                    as_double=value,
                    time_unix_nano=_nanos(when),
                    start_time_unix_nano=_nanos(start) if start else 0,
                    attributes=[_kv(k, v) for k, v in attrs.items()],
                )
                for value, attrs in points
            ],
        ),
    )


def _metrics_request(*metrics: Any, resource: Optional[dict] = None) -> Any:
    resource = resource or {"service.name": "claude-code"}
    return metrics_service_pb2.ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource={"attributes": [_kv(k, v) for k, v in resource.items()]},
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope={
                            "name": "com.anthropic.claude_code",
                            "version": "2.1.280",
                        },
                        metrics=list(metrics),
                    )
                ],
            )
        ]
    )


def _encode(message: Any, content_type: str) -> bytes:
    if content_type == PB:
        return message.SerializeToString()
    return json_format.MessageToJson(message).encode("utf-8")


# --- fixtures -----------------------------------------------------------------


def _key(db_session, user, scopes, name="otlp ingest") -> tuple[Any, str]:
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    key = models.ApiKey(
        name=name,
        key=token,
        key_hash=crud_api_key.build_key_hash(token),
        key_prefix=crud_api_key.build_key_prefix(token),
        scopes=scopes,
        account_id=user.account_id,
        user_id=user.id,
    )
    db_session.add(key)
    db_session.commit()
    return key, token


@pytest.fixture
def ingest_key(db_session, test_user):
    return _key(db_session, test_user, [TELEMETRY_INGEST_SCOPE])


def _post(
    client, path, token, message, content_type=PB, *, gzip_body=False, header="bearer"
):
    body = _encode(message, content_type)
    headers = {"Content-Type": content_type}
    if gzip_body:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    if token is not None:
        if header == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["x-api-key"] = token
    return client.post(path, content=body, headers=headers)


def _otlp_rows(db_session, account_id):
    return (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "imported_usage",
        )
        .all()
    )


def _gateway_row(db_session, user, **kwargs):
    defaults = dict(
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.5,
        user_id=str(user.id),
        account_id=str(user.account_id),
        model_alias="claude-sonnet-5",
        upstream_request_id="msg_01gateway",
        prompt_tokens=120,
        completion_tokens=45,
        estimated_cost=0.0123,
        cost_source="catalog",
        usage_source="provider",
    )
    defaults.update(kwargs)
    row = crud_api_usage.log_gateway_request(db_session, **defaults)
    db_session.commit()
    return row


# --- endpoint behaviour -------------------------------------------------------


@pytest.mark.parametrize("content_type", [PB, JSON])
def test_logs_export_both_encodings(
    client, db_session, test_user, ingest_key, content_type
):
    _, token = ingest_key
    response = _post(client, LOGS, token, _logs_request(_api_request()), content_type)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    if content_type == PB:
        parsed = logs_service_pb2.ExportLogsServiceResponse.FromString(response.content)
    else:
        parsed = json_format.Parse(
            response.content, logs_service_pb2.ExportLogsServiceResponse()
        )
    assert not parsed.HasField("partial_success")
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1


@pytest.mark.parametrize("content_type", [PB, JSON])
def test_metrics_and_traces_export_both_encodings(client, ingest_key, content_type):
    _, token = ingest_key
    metrics = _metrics_request(
        _sum_metric(otlp.METRIC_SESSIONS, [(1.0, {"session.id": "s-1"})])
    )
    assert _post(client, METRICS, token, metrics, content_type).status_code == 200

    traces = trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x01" * 16,
                                span_id=b"\x02" * 8,
                                name="claude_code.interaction",
                            )
                        ]
                    )
                ]
            )
        ]
    )
    response = _post(client, TRACES, token, traces, content_type)
    assert response.status_code == 200


def test_otlp_json_with_hex_trace_ids_decodes(client, ingest_key):
    """OTLP JSON carries hex ids, which protobuf JSON mapping rejects."""
    _, token = ingest_key
    body = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1760000000000000000",
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "body": {"stringValue": "claude_code.api_request"},
                                "attributes": [
                                    {
                                        "key": "event.name",
                                        "value": {"stringValue": "api_request"},
                                    },
                                    {
                                        "key": "request_id",
                                        "value": {"stringValue": "req_hex"},
                                    },
                                    {"key": "input_tokens", "value": {"intValue": "3"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    response = client.post(
        LOGS,
        content=json.dumps(body),
        headers={"Content-Type": JSON, "x-api-key": token},
    )
    assert response.status_code == 200


def test_gzip_and_x_api_key(client, db_session, test_user, ingest_key):
    _, token = ingest_key
    response = _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request()),
        gzip_body=True,
        header="x-api-key",
    )
    assert response.status_code == 200
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_undecodable_body_is_400(client, ingest_key):
    _, token = ingest_key
    response = client.post(
        LOGS,
        content=b"\xff\xff\xff not protobuf",
        headers={"Content-Type": PB, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    bad_json = client.post(
        LOGS, content=b"{not json", headers={"Content-Type": JSON, "x-api-key": token}
    )
    assert bad_json.status_code == 400
    bad_gzip = client.post(
        LOGS,
        content=b"not gzip",
        headers={"Content-Type": PB, "Content-Encoding": "gzip", "x-api-key": token},
    )
    assert bad_gzip.status_code == 400


def test_oversize_and_gzip_bomb_are_413(client, ingest_key):
    _, token = ingest_key
    huge = b"\x00" * (otlp.MAX_BODY_BYTES + 1)
    response = client.post(
        LOGS, content=huge, headers={"Content-Type": PB, "x-api-key": token}
    )
    assert response.status_code == 413
    bomb = gzip.compress(b"\x00" * (otlp.MAX_BODY_BYTES * 4))
    assert len(bomb) < otlp.MAX_BODY_BYTES
    response = client.post(
        LOGS,
        content=bomb,
        headers={"Content-Type": PB, "Content-Encoding": "gzip", "x-api-key": token},
    )
    assert response.status_code == 413


def test_other_content_type_is_415(client, ingest_key):
    _, token = ingest_key
    response = client.post(
        LOGS,
        content=b"hello",
        headers={"Content-Type": "text/plain", "x-api-key": token},
    )
    assert response.status_code == 415


def test_no_key_is_401_and_wrong_scope_is_403(client, db_session, test_user):
    response = _post(client, LOGS, None, _logs_request(_api_request()))
    assert response.status_code == 401
    assert _post(client, LOGS, "nope", _logs_request()).status_code == 401
    _, token = _key(db_session, test_user, ["mcp:read"], name="mcp only")
    response = _post(client, LOGS, token, _logs_request(_api_request()))
    assert response.status_code == 403


def test_rate_limit_is_429_with_retry_after(client, ingest_key, monkeypatch):
    _, token = ingest_key
    monkeypatch.setattr(telemetry_otlp, "RATE_LIMIT_PER_MINUTE", 2)
    for _ in range(2):
        assert _post(client, LOGS, token, _logs_request()).status_code == 200
    response = _post(client, LOGS, token, _logs_request())
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_mixed_batch_reports_partial_success(client, db_session, test_user, ingest_key):
    _, token = ingest_key
    request = _logs_request(
        _api_request(),
        _log_record("user_prompt", {"prompt": SECRET_PROMPT, "prompt_length": 60}),
        _log_record(
            "tool_result", {"tool_name": "Bash", "tool_parameters": "rm -rf build"}
        ),
    )
    response = _post(client, LOGS, token, request)
    assert response.status_code == 200
    parsed = logs_service_pb2.ExportLogsServiceResponse.FromString(response.content)
    assert parsed.partial_success.rejected_log_records == 2
    assert "allowlist" in parsed.partial_success.error_message

    metrics = _metrics_request(
        _sum_metric(otlp.METRIC_SESSIONS, [(1.0, {"session.id": "s-1"})]),
        _sum_metric("claude_code.lines_of_code.count", [(5.0, {"type": "added"})]),
    )
    response = _post(client, METRICS, token, metrics, JSON)
    parsed = json_format.Parse(
        response.content, metrics_service_pb2.ExportMetricsServiceResponse()
    )
    assert parsed.partial_success.rejected_data_points == 1


def test_records_over_the_per_request_limit_are_rejected(monkeypatch):
    monkeypatch.setattr(otlp, "MAX_RECORDS_PER_REQUEST", 2)
    request = _logs_request(
        _api_request("req_1"), _api_request("req_2"), _api_request("req_3")
    )
    extraction = otlp.extract_logs(request, account_id="a", now=otlp.utc_now())
    assert len(extraction.logs) == 2
    assert extraction.over_limit == 1


def test_attribute_values_truncated_and_capped():
    pairs = [_kv("model", "x" * 5000)] + [_kv(f"k{i}", i) for i in range(100)]
    flat = otlp.attributes(pairs)
    assert len(flat["model"].encode()) == otlp.MAX_ATTRIBUTE_VALUE_BYTES
    assert len(flat) == otlp.MAX_ATTRIBUTES_PER_RECORD


# --- scope ---------------------------------------------------------------------


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def test_ingest_only_key_is_refused_elsewhere():
    key = SimpleNamespace(id="k", name="otlp", scopes=[TELEMETRY_INGEST_SCOPE])
    enforce_api_key_route_scope(key, _request(LOGS, "POST"))
    for path in ("/api/v1/issues", "/api/v1/account", "/api/v1/agents/discovery-salt"):
        with pytest.raises(HTTPException) as denied:
            enforce_api_key_route_scope(key, _request(path))
        assert denied.value.status_code == 403
    assert is_single_purpose_api_key(key)
    assert not api_key_allowed_on_channel(key, "console")
    mixed = SimpleNamespace(id="k", scopes=[TELEMETRY_INGEST_SCOPE, "mcp:read"])
    assert not is_single_purpose_api_key(mixed)


def test_ingest_only_key_is_refused_on_a_real_rest_route(
    app, client, db_session, test_user
):
    """The real REST dependency refuses the key (no user override)."""
    from preloop.api.auth import get_current_active_user

    _, token = _key(db_session, test_user, [TELEMETRY_INGEST_SCOPE], name="rest probe")
    app.dependency_overrides.pop(get_current_active_user, None)
    response = client.get(
        "/api/v1/auth/api-keys", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "api_key_scope_denied"


def test_creating_an_ingest_key_requires_admin(
    client, db_session, test_user, monkeypatch
):
    from preloop.api.auth import router as auth_router

    monkeypatch.setattr(auth_router, "_is_account_admin", lambda db, user: True)
    response = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "otlp-admin", "scopes": [TELEMETRY_INGEST_SCOPE]},
    )
    assert response.status_code == 201
    assert response.json()["scopes"] == [TELEMETRY_INGEST_SCOPE]

    monkeypatch.setattr(auth_router, "_is_account_admin", lambda db, user: False)
    response = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "otlp-member", "scopes": [TELEMETRY_INGEST_SCOPE]},
    )
    assert response.status_code == 403


# --- reconciliation -------------------------------------------------------------


def test_matching_client_request_id_enriches_and_creates_no_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    gateway = _gateway_row(
        db_session,
        test_user,
        meta_data={"client_request_id": "cr-1", "client": "claude_code"},
    )
    response = _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_x", client_request_id="cr-1")),
    )
    assert response.status_code == 200
    assert _otlp_rows(db_session, test_user.account_id) == []
    db_session.refresh(gateway)
    telemetry = gateway.meta_data["telemetry"]
    assert telemetry["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert telemetry["prompt_id"] == "22222222-2222-4222-8222-222222222222"
    assert telemetry["cost_usd"] == pytest.approx(0.0123)
    assert telemetry["client"]["service.name"] == "claude-code"
    assert gateway.estimated_cost == pytest.approx(0.0123)


def test_matching_upstream_request_id_enriches(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    gateway = _gateway_row(db_session, test_user, upstream_request_id="req_same")
    _post(client, LOGS, token, _logs_request(_api_request("req_same")))
    assert _otlp_rows(db_session, test_user.account_id) == []
    db_session.refresh(gateway)
    assert gateway.meta_data["telemetry"]["request_id"] == "req_same"


def test_unmatched_event_creates_one_telemetry_estimate_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_new", client_request_id="cr-9")),
    )
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.usage_source == "imported"
    assert row.cost_source == "telemetry_estimate"
    assert row.estimated_cost == pytest.approx(0.0123)
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 45
    assert row.cache_read_tokens == 1000
    assert row.meta_data["source"] == "otlp"
    assert row.meta_data["otlp_request_id"] == "req_new"
    assert row.meta_data["otlp_client_request_id"] == "cr-9"


def test_replaying_the_same_batch_creates_nothing(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    request = _logs_request(_api_request("req_replay"))
    for _ in range(3):
        assert _post(client, LOGS, token, request).status_code == 200
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1
    assert _post(client, LOGS, token, request, JSON).status_code == 200
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_api_error_without_match_creates_no_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    error = _log_record("api_error", {"request_id": "req_err", "status_code": 529})
    _post(client, LOGS, token, _logs_request(error))
    assert _otlp_rows(db_session, test_user.account_id) == []


def test_late_gateway_row_leaves_exactly_one_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_late", client_request_id="cr-late")),
    )
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1

    gateway = _gateway_row(
        db_session, test_user, meta_data={"client_request_id": "cr-late"}
    )

    assert _otlp_rows(db_session, test_user.account_id) == []
    rows = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .all()
    )
    assert [row.id for row in rows] == [gateway.id]
    db_session.refresh(gateway)
    assert gateway.meta_data["telemetry"]["request_id"] == "req_late"

    # A re-sent export after that still leaves one row, enriched.
    _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_late", client_request_id="cr-late")),
    )
    assert (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .count()
        == 1
    )


def test_late_gateway_row_matched_by_upstream_request_id(
    db_session, test_user, ingest_key
):
    key, _ = ingest_key
    ingestor = otlp.TelemetryIngestor(
        db_session,
        otlp.IngestContext(
            account_id=test_user.account_id, api_key_id=key.id, now=otlp.utc_now()
        ),
    )
    extraction = otlp.extract_logs(
        _logs_request(_api_request("req_up")),
        account_id=test_user.account_id,
        now=otlp.utc_now(),
    )
    ingestor.ingest_logs(extraction.logs)
    db_session.commit()
    _gateway_row(db_session, test_user, upstream_request_id="req_up")
    assert _otlp_rows(db_session, test_user.account_id) == []


def _cost_and_tokens(session_id: str, cost: float, tokens: float, **kw) -> Any:
    return _metrics_request(
        _sum_metric(
            otlp.METRIC_COST,
            [(cost, {"session.id": session_id, "model": "claude-sonnet-5"})],
            **kw,
        ),
        _sum_metric(
            otlp.METRIC_TOKENS,
            [
                (
                    tokens,
                    {
                        "session.id": session_id,
                        "model": "claude-sonnet-5",
                        "type": "input",
                    },
                ),
                (
                    tokens / 2,
                    {
                        "session.id": session_id,
                        "model": "claude-sonnet-5",
                        "type": "output",
                    },
                ),
            ],
            **kw,
        ),
    )


def test_metrics_only_stream_creates_hourly_aggregates(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    noon = datetime(2026, 10, 9, 12, 5, tzinfo=timezone.utc)
    _post(client, METRICS, token, _cost_and_tokens("s-agg", 0.5, 100, when=noon))
    _post(
        client,
        METRICS,
        token,
        _cost_and_tokens("s-agg", 0.25, 50, when=noon + timedelta(minutes=10)),
    )
    _post(
        client,
        METRICS,
        token,
        _cost_and_tokens("s-agg", 1.0, 10, when=noon + timedelta(hours=1)),
    )

    rows = sorted(
        _otlp_rows(db_session, test_user.account_id), key=lambda r: r.timestamp
    )
    assert len(rows) == 2
    first, second = rows
    assert first.meta_data["aggregate"] is True
    assert first.cost_source == "telemetry_estimate"
    assert first.usage_source == "imported"
    assert first.estimated_cost == pytest.approx(0.75)
    assert first.prompt_tokens == 150
    assert first.completion_tokens == 75
    assert first.timestamp == datetime(2026, 10, 9, 12, 0)
    assert second.estimated_cost == pytest.approx(1.0)


def test_cumulative_metrics_become_deltas(client, db_session, test_user, ingest_key):
    _, token = ingest_key
    start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    for minutes, total in ((1, 0.25), (2, 0.75), (3, 1.0)):
        request = _metrics_request(
            _sum_metric(
                otlp.METRIC_COST,
                [(total, {"session.id": "s-cum", "model": "claude-sonnet-5"})],
                cumulative=True,
                when=start + timedelta(minutes=minutes),
                start=start,
            )
        )
        assert _post(client, METRICS, token, request).status_code == 200
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].estimated_cost == pytest.approx(1.0)
    assert db_session.query(TelemetryMetricSeries).count() == 1


def test_series_state_expires_after_24h(db_session, test_user, ingest_key):
    from preloop.models.crud import crud_telemetry_ingest

    now = otlp.utc_now()
    crud_telemetry_ingest.put_series(
        db_session,
        account_id=test_user.account_id,
        series_key="s" * 64,
        value=5.0,
        point_time=now,
        start_time=None,
        now=now - timedelta(hours=25),
    )
    assert (
        crud_telemetry_ingest.get_series(
            db_session, account_id=test_user.account_id, series_key="s" * 64, now=now
        )
        is None
    )


def test_metrics_plus_logs_for_one_session_creates_no_aggregate(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    session_id = "33333333-3333-4333-8333-333333333333"
    # Metrics first: an aggregate exists until the session's logs show up.
    _post(client, METRICS, token, _cost_and_tokens(session_id, 0.5, 100))
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1
    _post(
        client, LOGS, token, _logs_request(_api_request("req_s", session_id=session_id))
    )
    _post(
        client,
        METRICS,
        token,
        _cost_and_tokens(
            session_id, 0.7, 10, when=datetime(2026, 10, 9, 12, 40, tzinfo=timezone.utc)
        ),
    )

    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].meta_data.get("aggregate") is None
    assert rows[0].meta_data["otlp_request_id"] == "req_s"


def test_metrics_for_a_gateway_session_create_no_aggregate(
    client, db_session, test_user, ingest_key
):
    from preloop.models.crud import crud_runtime_session

    key, token = ingest_key
    session_id = "44444444-4444-4444-8444-444444444444"
    now = otlp.utc_now()
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="api_key",
        session_source_id=f"{uuid.uuid4()}:{session_id}",
        runtime_principal_type="api_key",
        runtime_principal_id="p",
        runtime_principal_name="gateway",
        started_at=now,
        last_activity_at=now,
    )
    _gateway_row(db_session, test_user, runtime_session_id=str(runtime_session.id))

    _post(client, METRICS, token, _cost_and_tokens(session_id, 0.5, 100))
    assert _otlp_rows(db_session, test_user.account_id) == []

    # A log row of that session joins the gateway session grouping (scope 7).
    _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_sess", session_id=session_id)),
    )
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].runtime_session_id == runtime_session.id
    assert rows[0].conversation_id == session_id


# --- privacy ---------------------------------------------------------------------


def test_no_prompt_command_or_path_is_stored(client, db_session, test_user, ingest_key):
    _, token = ingest_key
    request = _logs_request(
        _log_record("user_prompt", {"prompt": SECRET_PROMPT, "session.id": "s-p"}),
        _log_record(
            "tool_result",
            {
                "tool_parameters": '{"command": "rm -rf build"}',
                "file_path": "/Users/alice/secret",
            },
        ),
        _log_record("api_response_body", {"body": SECRET_PROMPT}),
        _api_request(
            "req_priv",
            prompt=SECRET_PROMPT,
            prompt_text=SECRET_PROMPT,
            tool_input="rm -rf build",
            file_path="/Users/alice/secret/project.py",
        ),
        resource={
            "service.name": "claude-code",
            "user.email": "dev@example.com",
            "cwd": "/Users/alice/secret",
        },
    )
    assert _post(client, LOGS, token, request).status_code == 200

    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    dumped = json.dumps(
        {
            column.name: getattr(rows[0], column.name)
            for column in ApiUsage.__table__.columns
        },
        default=str,
    )
    for needle in (
        "refactor",
        "rm -rf",
        "/Users/alice",
        "secret",
        "prompt_text",
        "tool_input",
    ):
        assert needle not in dumped
    assert '"prompt"' not in json.dumps(rows[0].meta_data)
    assert rows[0].meta_data["telemetry"]["identity"]["user.email"] == "dev@example.com"


# --- identity ----------------------------------------------------------------------


def test_enduser_sub_resolves_a_subject_and_links_a_member(
    client, db_session, test_user, ingest_key
):
    key, token = ingest_key
    request = _logs_request(
        _api_request("req_id"),
        resource={
            "service.name": "claude-desktop",
            "enduser.sub": "idp-sub-123",
            "enduser.id": "idp-sub-123",
            "user.email": test_user.email,
            "user.id": "anon-desktop-install",
        },
    )
    _post(client, LOGS, token, request)
    subject = db_session.query(GatewaySubject).one()
    assert subject.external_subject == "idp-sub-123"
    assert subject.api_key_id == key.id
    assert subject.linked_user_id == test_user.id
    row = _otlp_rows(db_session, test_user.account_id)[0]
    assert row.user_id == test_user.id
    assert row.meta_data["gateway_subject_id"] == str(subject.id)


def test_desktop_anonymous_user_id_is_ignored(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    users_before = db_session.query(models.User).count()
    request = _logs_request(
        _api_request("req_anon"),
        resource={"service.name": "claude-desktop", "user.id": "anon-desktop-install"},
    )
    _post(client, LOGS, token, request)
    assert db_session.query(GatewaySubject).count() == 0
    assert db_session.query(models.User).count() == users_before
    assert _otlp_rows(db_session, test_user.account_id)[0].user_id is None


def test_terminal_gateway_user_id_is_the_subject(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    request = _logs_request(
        _api_request("req_term"),
        resource={
            "service.name": "claude-code",
            "user.id": "idp-sub-terminal",
            "identity.source": "gateway-oidc",
            "user.email": "someone-else@example.com",
        },
    )
    _post(client, LOGS, token, request)
    subject = db_session.query(GatewaySubject).one()
    assert subject.external_subject == "idp-sub-terminal"
    assert subject.linked_user_id is None


# --- through the real Anthropic gateway route ------------------------------------


def test_gateway_request_records_client_request_id_and_supersedes_otlp_row(
    client, db_session, test_user, ingest_key
):
    """Telemetry first, gateway second: one row, carrying the telemetry."""
    from tests.endpoints.test_anthropic_gateway_trusted_upstream import (
        _identity,
        _model,
        _post as gateway_post,
        _usage,
    )
    from tests.endpoints.test_anthropic_gateway_trusted_upstream import _key as gw_key

    _, token = ingest_key
    _post(
        client,
        LOGS,
        token,
        _logs_request(_api_request("req_e2e", client_request_id="cr-e2e")),
    )
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1

    _model(db_session, test_user.account_id)
    api_key, gateway_token = gw_key(db_session, test_user)
    response = gateway_post(
        client, gateway_token, {**_identity(), "x-client-request-id": "cr-e2e"}
    )
    assert response.status_code == 200

    usage = _usage(db_session, api_key)
    assert usage.meta_data["client_request_id"] == "cr-e2e"
    assert usage.meta_data["telemetry"]["request_id"] == "req_e2e"
    assert _otlp_rows(db_session, test_user.account_id) == []
    assert (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .count()
        == 1
    )


# --- behind the apps gateway: no shared request id --------------------------------
#
# Observed on the harness (gateway 2.1.288): relayed api_request events carry
# neither request_id nor client_request_id, and the gateway forwards neither
# id upstream. Event and gateway row agree on time, model, output tokens and
# the person's email.

DATED = "claude-sonnet-4-5-20250929"


def _apps_gateway_row(db_session, user, *, email="alice@example.com", completion=100):
    return _gateway_row(
        db_session,
        user,
        model_alias=DATED,
        upstream_request_id=f"chatcmpl-{uuid.uuid4()}",
        prompt_tokens=1000,
        completion_tokens=completion,
        meta_data={
            "gateway_source": "claude_apps_gateway",
            "client": "unknown",
            "gateway_subject_id": str(uuid.uuid4()),
            "gateway_subject_email": email,
        },
    )


def _relayed_event(*, email="alice@example.com", output=100, when=None):
    when = when or datetime.now(timezone.utc)
    record = _log_record(
        "api_request",
        {
            "session.id": str(uuid.uuid4()),
            "model": DATED,
            "input_tokens": 0,
            "output_tokens": output,
            "cost_usd": 0.0015,
        },
        when=when,
    )
    return _logs_request(
        record,
        resource={
            "service.name": "claude-code",
            "user.email": email,
            "user.id": "dex-sub",
        },
    )


def test_relayed_event_without_ids_enriches_by_signature(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    gateway = _apps_gateway_row(db_session, test_user)
    _post(client, LOGS, token, _relayed_event())

    assert _otlp_rows(db_session, test_user.account_id) == []
    db_session.refresh(gateway)
    assert gateway.meta_data["telemetry"]["match"] == "signature"
    assert (
        gateway.meta_data["telemetry"]["identity"]["user.email"] == "alice@example.com"
    )


def test_signature_needs_same_person_tokens_and_an_unenriched_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _apps_gateway_row(db_session, test_user)
    _post(client, LOGS, token, _relayed_event(email="bob@example.com"))
    _post(client, LOGS, token, _relayed_event(output=99))
    assert len(_otlp_rows(db_session, test_user.account_id)) == 2

    _post(client, LOGS, token, _relayed_event())
    assert len(_otlp_rows(db_session, test_user.account_id)) == 2
    # The gateway row is taken now: a second identical event is its own request.
    _post(
        client,
        LOGS,
        token,
        _relayed_event(when=datetime.now(timezone.utc) + timedelta(seconds=1)),
    )
    assert len(_otlp_rows(db_session, test_user.account_id)) == 3


def test_signature_ignores_rows_outside_the_window(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _apps_gateway_row(db_session, test_user)
    _post(
        client,
        LOGS,
        token,
        _relayed_event(when=datetime.now(timezone.utc) - timedelta(minutes=5)),
    )
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_late_gateway_row_without_ids_supersedes_by_signature(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(client, LOGS, token, _relayed_event())
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1

    gateway = _apps_gateway_row(db_session, test_user)

    assert _otlp_rows(db_session, test_user.account_id) == []
    db_session.refresh(gateway)
    assert (
        gateway.meta_data["telemetry"]["identity"]["user.email"] == "alice@example.com"
    )


def test_late_gateway_row_of_another_person_keeps_the_otlp_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(client, LOGS, token, _relayed_event(email="bob@example.com"))
    _apps_gateway_row(db_session, test_user, email="alice@example.com")
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_metrics_of_a_person_with_gateway_rows_create_no_aggregate(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _apps_gateway_row(db_session, test_user)
    now = datetime.now(timezone.utc)
    request = _metrics_request(
        _sum_metric(
            otlp.METRIC_COST,
            [(0.5, {"session.id": "s-person", "model": DATED})],
            when=now,
        ),
        resource={"service.name": "claude-code", "user.email": "alice@example.com"},
    )
    _post(client, METRICS, token, request)
    assert _otlp_rows(db_session, test_user.account_id) == []

    other = _metrics_request(
        _sum_metric(
            otlp.METRIC_COST,
            [(0.5, {"session.id": "s-other", "model": DATED})],
            when=now,
        ),
        resource={"service.name": "claude-code", "user.email": "carol@example.com"},
    )
    _post(client, METRICS, token, other)
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_session_lock_is_taken_for_every_session_in_a_batch(
    db_session, test_user, ingest_key, monkeypatch
):
    from preloop.models.crud import crud_telemetry_ingest

    key, _ = ingest_key
    locked: list[str] = []
    original = crud_telemetry_ingest.lock_session

    def spy(db, *, account_id, session_id):
        locked.append(session_id)
        original(db, account_id=account_id, session_id=session_id)

    monkeypatch.setattr(crud_telemetry_ingest, "lock_session", spy)
    ingestor = otlp.TelemetryIngestor(
        db_session,
        otlp.IngestContext(
            account_id=test_user.account_id, api_key_id=key.id, now=otlp.utc_now()
        ),
    )
    events = otlp.extract_logs(
        _logs_request(
            _api_request("r1", session_id="b"), _api_request("r2", session_id="a")
        ),
        account_id=test_user.account_id,
        now=otlp.utc_now(),
    ).logs
    ingestor.ingest_logs(events)
    assert locked == ["a", "b"]


def _keyed_row(db_session, user):
    """A gateway row on a personal key: no gateway subject."""
    return _gateway_row(
        db_session,
        user,
        model_alias=DATED,
        upstream_request_id=f"chatcmpl-{uuid.uuid4()}",
        completion_tokens=100,
        meta_data={"gateway_source": "direct", "client": "claude_code"},
    )


def test_signature_skips_a_keyed_row_of_another_member(
    client, db_session, test_user, ingest_key
):
    """Harness regression: alice's event matched an admin's curl request."""
    _, token = ingest_key
    gateway = _keyed_row(db_session, test_user)  # owner is test@example.com
    _post(client, LOGS, token, _relayed_event(email="alice@example.com"))
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1
    db_session.refresh(gateway)
    assert "telemetry" not in gateway.meta_data


def test_signature_matches_a_keyed_row_of_the_same_member(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    gateway = _keyed_row(db_session, test_user)
    _post(client, LOGS, token, _relayed_event(email=test_user.email))
    assert _otlp_rows(db_session, test_user.account_id) == []
    db_session.refresh(gateway)
    assert gateway.meta_data["telemetry"]["match"] == "signature"


def test_late_keyed_row_of_another_member_keeps_the_otlp_row(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(client, LOGS, token, _relayed_event(email="alice@example.com"))
    _keyed_row(db_session, test_user)
    assert len(_otlp_rows(db_session, test_user.account_id)) == 1


def test_late_keyed_row_of_the_same_member_supersedes(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    _post(client, LOGS, token, _relayed_event(email=test_user.email))
    _keyed_row(db_session, test_user)
    assert _otlp_rows(db_session, test_user.account_id) == []


def test_error_bodies_are_fixed_messages(client, ingest_key):
    """No exception text or client input is echoed (CodeQL py/stack-trace-exposure)."""
    _, token = ingest_key
    response = client.post(
        LOGS,
        content=b"x",
        headers={"Content-Type": "text/<script>", "x-api-key": token},
    )
    assert response.status_code == 415
    assert "<script>" not in response.text
    assert response.json()["message"] == otlp.OtlpUnsupportedMediaError.public_message
    response = client.post(
        LOGS,
        content=b"x",
        headers={"Content-Type": PB, "Content-Encoding": "br-evil", "x-api-key": token},
    )
    assert "br-evil" not in response.text
    response = client.post(
        LOGS, content=b"\xff\xff\xff", headers={"Content-Type": PB, "x-api-key": token}
    )
    assert response.json()["message"] == otlp.OtlpDecodeError.public_message


# --- review findings on PR #1465 ---------------------------------------------------


def _ingestor(db_session, test_user, key):
    return otlp.TelemetryIngestor(
        db_session,
        otlp.IngestContext(
            account_id=test_user.account_id, api_key_id=key.id, now=otlp.utc_now()
        ),
    )


def test_subject_resolves_outside_the_ingest_transaction(
    db_session, test_user, ingest_key, monkeypatch
):
    """resolve() commits; on the ingest session it would drop the session locks."""
    from preloop.models.crud import crud_gateway_subject

    key, _ = ingest_key
    sessions: list[Any] = []
    original = crud_gateway_subject.resolve

    def spy(db, **kwargs):
        sessions.append(db)
        return original(db, **kwargs)

    monkeypatch.setattr(crud_gateway_subject, "resolve", spy)
    ingestor = _ingestor(db_session, test_user, key)
    events = otlp.extract_logs(
        _logs_request(
            _api_request("req_lock"),
            resource={"enduser.sub": "sub-lock", "user.email": test_user.email},
        ),
        account_id=test_user.account_id,
        now=otlp.utc_now(),
    ).logs
    ingestor.ingest_logs(events)
    assert sessions and all(session is not db_session for session in sessions)
    row = _otlp_rows(db_session, test_user.account_id)[0]
    assert row.user_id == test_user.id
    assert row.meta_data["gateway_subject_email"] == test_user.email


def test_out_of_order_older_cumulative_point_is_not_counted(
    client, db_session, test_user, ingest_key
):
    _, token = ingest_key
    start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    def point(minutes: int, total: float) -> Any:
        return _metrics_request(
            _sum_metric(
                otlp.METRIC_COST,
                [(total, {"session.id": "s-ooo", "model": "claude-sonnet-5"})],
                cumulative=True,
                when=start + timedelta(minutes=minutes),
                start=start,
            )
        )

    _post(client, METRICS, token, point(5, 1.0))
    _post(client, METRICS, token, point(3, 0.8))  # older and lower: a late retry
    _post(client, METRICS, token, point(6, 1.5))
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].estimated_cost == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929", True),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5-20250929", True),
        ("claude-sonnet-4", "claude-sonnet-4-5", False),
        ("claude-opus-4", "claude-opus-4-1", False),
        ("gpt-4o", "gpt-4o-mini", False),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5-20251001", False),
    ],
)
def test_same_model_only_allows_a_date_suffix(left, right, same):
    from preloop.models.crud.telemetry_ingest import _same_model

    assert _same_model(left, right) is same
    assert _same_model(right, left) is same


def test_metric_batch_queries_do_not_scale_with_points(
    db_session, test_user, ingest_key
):
    """A large batch of one session, model and hour costs a bounded query count."""
    from sqlalchemy import event

    key, _ = ingest_key
    base = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    request = _metrics_request(
        *[
            _sum_metric(
                otlp.METRIC_COST,
                [(0.01, {"session.id": "s-bulk", "model": "claude-sonnet-5"})],
                when=base + timedelta(seconds=i),
            )
            for i in range(300)
        ],
        resource={"service.name": "claude-code", "user.email": "bulk@example.com"},
    )
    points = otlp.extract_metrics(
        request, account_id=test_user.account_id, now=otlp.utc_now()
    ).metrics
    assert len(points) == 300
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        _ingestor(db_session, test_user, key).ingest_metrics(points)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) < 40, len(statements)
    rows = _otlp_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].estimated_cost == pytest.approx(3.0)
