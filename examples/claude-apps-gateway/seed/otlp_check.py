"""OTLP telemetry ingest check for verify.sh step 7 (#1412). Test-only.

Runs inside the ``preloop`` container. Posts a JSON and a protobuf OTLP logs
export to ``/api/v1/telemetry/otlp/v1/logs`` with the harness telemetry key,
drives two gateway requests carrying ``x-client-request-id`` with the
direct key, and reads ``api_usage`` through the models to assert:

* enrichment: an event whose ``client_request_id`` matches a gateway row
  lands in that row's ``meta_data.telemetry`` and creates no row;
* estimate: an unmatched event creates exactly one ``telemetry_estimate``
  row, and replaying the export creates nothing;
* no double count: a gateway row recorded after that event leaves exactly
  one row for the request.

Prints one JSON object of named booleans and the HTTP statuses.

With ``--relayed`` it instead audits what real Claude Code sessions sent
through the apps gateway's ``telemetry.forward_to``: no telemetry row may
duplicate a gateway row, and no metric aggregate may sit next to log rows of
its session or gateway rows of its person in the same hour.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import timedelta
import urllib.request
import uuid
from typing import Any, Callable

from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1 import logs_pb2

from preloop.models.db.session import get_db_session
from preloop.models.models.api_usage import ApiUsage

BASE = "http://localhost:8000"
TELEMETRY_KEY = os.environ["PRELOOP_TELEMETRY_KEY"]
DIRECT_KEY = os.environ["DIRECT_KEY"]


def _kv(key: str, value: Any) -> KeyValue:
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def _export(request_id: str, client_request_id: str) -> Any:
    record = logs_pb2.LogRecord(
        time_unix_nano=time.time_ns(),
        body=AnyValue(string_value="claude_code.api_request"),
        attributes=[
            _kv("event.name", "api_request"),
            _kv("request_id", request_id),
            _kv("client_request_id", client_request_id),
            _kv("session.id", str(uuid.uuid4())),
            _kv("model", "claude-sonnet-4-5"),
            _kv("input_tokens", 10),
            _kv("output_tokens", 5),
            _kv("cost_usd", 0.001),
            _kv("prompt", "harness prompt text that must not be stored"),
        ],
    )
    return logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                resource={"attributes": [_kv("service.name", "claude-code")]},
                scope_logs=[logs_pb2.ScopeLogs(log_records=[record])],
            )
        ]
    )


def _post_logs(message: Any, encoding: str) -> int:
    if encoding == "json":
        body = json_format.MessageToJson(message).encode()
        content_type = "application/json"
    else:
        body = message.SerializeToString()
        content_type = "application/x-protobuf"
    request = urllib.request.Request(
        f"{BASE}/api/v1/telemetry/otlp/v1/logs",
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {TELEMETRY_KEY}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def _gateway_request(client_request_id: str) -> int:
    request = urllib.request.Request(
        f"{BASE}/anthropic/v1/messages",
        data=json.dumps(
            {
                "model": "claude-sonnet-4-5",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode(),
        method="POST",
        headers={
            "x-api-key": DIRECT_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-client-request-id": client_request_id,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status


def _rows(where: Callable[[Any], Any]) -> list[ApiUsage]:
    db = next(get_db_session())
    try:
        return list(db.query(ApiUsage).filter(where(ApiUsage)).all())
    finally:
        db.close()


def _wait(fetch: Callable[[], list[ApiUsage]], ok: Callable[[list], bool]) -> list:
    rows: list = []
    for _ in range(30):
        rows = fetch()
        if ok(rows):
            return rows
        time.sleep(0.5)
    return rows


def _gateway_rows(client_request_id: str) -> list[ApiUsage]:
    return _rows(
        lambda m: (
            (m.action_type == "model_gateway")
            & (m.meta_data["client_request_id"].astext == client_request_id)
        )
    )


def _otlp_rows(client_request_id: str) -> list[ApiUsage]:
    return _rows(
        lambda m: (
            (m.action_type == "imported_usage")
            & (m.meta_data["otlp_client_request_id"].astext == client_request_id)
        )
    )


def _same_model(left: Optional[str], right: Optional[str]) -> bool:
    """Same rule as the ingest: equal, or equal once one date suffix is dropped."""
    if not left or not right:
        return False
    left, right = left.lower(), right.lower()
    if left == right:
        return True
    left_base = re.sub(r"-\d{8}$", "", left)
    right_base = re.sub(r"-\d{8}$", "", right)
    return left_base == right_base and (left == left_base or right == right_base)


def relayed() -> int:
    """Audit telemetry the gateway relayed during the Claude Code steps."""
    from preloop.models.models.telemetry_ingest import TelemetryIngestDedup

    db = next(get_db_session())
    try:
        records = db.query(TelemetryIngestDedup).count()
        gateway = (
            db.query(ApiUsage)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.meta_data["gateway_source"].astext == "claude_apps_gateway",
            )
            .all()
        )
        otlp = (
            db.query(ApiUsage)
            .filter(
                ApiUsage.action_type == "imported_usage",
                ApiUsage.meta_data["import_source"].astext == "otlp",
            )
            .all()
        )
    finally:
        db.close()
    window = timedelta(seconds=60)
    rows = [row for row in otlp if not (row.meta_data or {}).get("aggregate")]
    aggregates = [row for row in otlp if (row.meta_data or {}).get("aggregate")]

    def email_of(row: ApiUsage) -> Optional[str]:
        identity = ((row.meta_data or {}).get("telemetry") or {}).get("identity") or {}
        value = identity.get("user.email")
        return str(value).lower() if value else None

    # Pair 1:1, closest in time, as the ingest does. A gateway row that
    # already carries telemetry is paired with its own event, so a telemetry
    # row next to it is a different request (in step 6, the request the
    # spare upstream served while Preloop's key was revoked).
    duplicates = 0
    taken: set[Any] = {gw.id for gw in gateway if "telemetry" in (gw.meta_data or {})}
    for row in sorted(rows, key=lambda r: r.timestamp):
        candidates = [
            gw
            for gw in gateway
            if gw.id not in taken
            and abs(gw.timestamp - row.timestamp) <= window
            and gw.completion_tokens == row.completion_tokens
            and _same_model(gw.model_alias, row.model_alias)
            and (
                not email_of(row)
                or email_of(row)
                == str((gw.meta_data or {}).get("gateway_subject_email") or "").lower()
            )
        ]
        if candidates:
            best = min(candidates, key=lambda gw: abs(gw.timestamp - row.timestamp))
            taken.add(best.id)
            duplicates += 1
    sessions_with_logs = {row.conversation_id for row in rows if row.conversation_id}
    overlapping = 0
    for agg in aggregates:
        hour = agg.timestamp
        person = email_of(agg)
        if agg.conversation_id in sessions_with_logs or any(
            hour <= gw.timestamp < hour + timedelta(hours=1)
            and person
            and person
            == str((gw.meta_data or {}).get("gateway_subject_email") or "").lower()
            for gw in gateway
        ):
            overlapping += 1
    print(
        json.dumps(
            {
                "telemetry_records": records,
                "apps_gateway_rows": len(gateway),
                "enriched_gateway_rows": sum(
                    1 for gw in gateway if "telemetry" in (gw.meta_data or {})
                ),
                "telemetry_rows": len(rows),
                "aggregates": len(aggregates),
                "duplicate_rows": duplicates,
                "overlapping_aggregates": overlapping,
            }
        )
    )
    return 0


def main() -> int:
    if "--relayed" in sys.argv[1:]:
        return relayed()
    run = uuid.uuid4().hex[:8]
    result: dict[str, Any] = {}

    # Enrichment: gateway row first, JSON export second.
    enrich_id = f"harness-enrich-{run}"
    result["gateway_status_enrich"] = _gateway_request(enrich_id)
    _wait(lambda: _gateway_rows(enrich_id), lambda rows: len(rows) == 1)
    result["json_status"] = _post_logs(_export(f"req_{run}_a", enrich_id), "json")
    gateway = _wait(
        lambda: _gateway_rows(enrich_id),
        lambda rows: bool(rows) and "telemetry" in (rows[0].meta_data or {}),
    )
    result["enriched"] = bool(gateway) and "telemetry" in (gateway[0].meta_data or {})
    result["enrich_created_no_row"] = _otlp_rows(enrich_id) == []

    # Estimate row, replay, then a late gateway row: protobuf export.
    late_id = f"harness-late-{run}"
    message = _export(f"req_{run}_b", late_id)
    result["protobuf_status"] = _post_logs(message, "protobuf")
    estimate = _otlp_rows(late_id)
    result["estimate_row"] = (
        len(estimate) == 1 and estimate[0].cost_source == "telemetry_estimate"
    )
    result["prompt_not_stored"] = all(
        "harness prompt text" not in json.dumps(row.meta_data) for row in estimate
    )
    _post_logs(message, "protobuf")
    result["replay_noop"] = len(_otlp_rows(late_id)) == 1
    result["gateway_status_late"] = _gateway_request(late_id)
    late = _wait(lambda: _gateway_rows(late_id), lambda rows: len(rows) == 1)
    result["late_gateway_one_row"] = len(late) == 1 and _otlp_rows(late_id) == []
    result["late_gateway_has_telemetry"] = bool(late) and "telemetry" in (
        late[0].meta_data or {}
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
