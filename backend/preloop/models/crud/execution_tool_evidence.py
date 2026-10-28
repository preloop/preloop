"""Scoped durable tool evidence and safe terminal-delivery diagnostics."""

from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import String, cast
from sqlalchemy.dialects.postgresql import insert

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.execution_tool_evidence import (
    EVIDENCE_TYPE,
    canonical_bytes,
    digest,
)


def persist_tool_evidence(
    db: Session, *, usage: models.ApiUsage, document: dict[str, Any]
) -> None:
    """Persist a bounded, sanitized record before the console payload is clipped."""
    if not usage.flow_execution_id:
        return
    from preloop.services.sensitive_data.storage import apply_storage_redaction

    document = {
        **document,
        "execution_id": str(usage.flow_execution_id),
        "api_usage_id": str(usage.id),
    }
    sanitized = apply_storage_redaction(str(usage.account_id), document)
    sanitized["storage_redacted"] = sanitized != document
    _persist_document(
        db,
        execution_id=usage.flow_execution_id,
        source="gateway",
        source_id=str(usage.id),
        document=sanitized,
    )


def _persist_document(
    db: Session,
    *,
    execution_id: UUID,
    source: str,
    source_id: str,
    document: dict[str, Any],
) -> None:
    """Idempotent observations keep duplicate log fetches out of the audit."""
    from preloop.utils.secret_scrubbing import scrub_structure

    cleaned = scrub_structure(document)
    if cleaned != document:
        cleaned["storage_redacted"] = True
    document = cleaned
    encoded = canonical_bytes(document).decode("utf-8")
    sha256 = digest(document)
    identity = uuid5(
        NAMESPACE_URL,
        f"preloop:tool-evidence:{execution_id}:{source}:{source_id}:{sha256}",
    )
    db.execute(
        insert(models.FlowExecutionLog)
        .values(
            id=identity,
            execution_id=execution_id,
            log_type=EVIDENCE_TYPE,
            metadata_={
                "source": source,
                "api_usage_id": source_id if source == "gateway" else None,
                "sha256": sha256,
                "capture_complete": document.get("capture_complete", False),
                "chunks": [
                    encoded[i : i + 16000] for i in range(0, len(encoded), 16000)
                ],
            },
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.flush()


def persist_native_command_evidence(
    db: Session, *, execution_id: str, line: Any
) -> None:
    """Capture Codex command events before the ordinary log string is capped."""
    import json
    from preloop.config import settings
    from preloop.models.crud.flow_execution_log import _account_for_execution
    from preloop.services.execution_tool_evidence import (
        MAX_EVIDENCE_BYTES,
        _redact_value,
    )
    from preloop.services.sensitive_data.storage import apply_storage_redaction

    if not isinstance(line, str) or not line.startswith("{"):
        return
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("type") not in {
        "item.started",
        "item.completed",
    }:
        return
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str) or not 0 < len(item_id) <= 256:
        return
    account_id = _account_for_execution(db, execution_id)
    if account_id is None:
        return
    raw = item.get("command")
    value = _redact_value(raw) if settings.model_gateway_capture_content else None
    complete = len(canonical_bytes(value)) <= MAX_EVIDENCE_BYTES
    document = {
        "version": 1,
        "execution_id": str(execution_id),
        "capture_complete": complete,
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "event": event["type"],
                "item_id": item_id,
                "value": value if complete else None,
                "exit_code": item.get("exit_code")
                if type(item.get("exit_code")) is int
                else None,
                "redacted": raw != value,
                "truncated": not complete,
            }
        ],
    }
    sanitized = apply_storage_redaction(account_id, document)
    sanitized["storage_redacted"] = sanitized != document
    _persist_document(
        db,
        execution_id=UUID(str(execution_id)),
        source="native",
        source_id=item_id,
        document=sanitized,
    )


def evidence_page(
    db: Session, *, execution: models.FlowExecution, skip: int, limit: int
) -> dict[str, Any]:
    """Read only an execution already checked against fresh principal authority."""
    import json

    query = db.query(models.FlowExecutionLog).filter(
        models.FlowExecutionLog.execution_id == execution.id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
    )
    total = query.count()
    rows = (
        query.order_by(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
        .offset(skip)
        .limit(limit)
        .all()
    )
    records = []
    for row in rows:
        stored = row.metadata_ or {}
        try:
            document = json.loads("".join(stored["chunks"]))
            valid = digest(document) == stored["sha256"]
        except (KeyError, TypeError, ValueError):
            document, valid = None, False
        records.append(
            {
                "id": str(row.id),
                "api_usage_id": stored.get("api_usage_id"),
                "sha256": stored.get("sha256"),
                "integrity_valid": valid,
                "document": document if valid else None,
            }
        )
    expected_query = db.query(models.ApiUsage).filter(
        models.ApiUsage.flow_execution_id == execution.id
    )
    expected = expected_query.count()
    captured_ids = db.query(
        models.FlowExecutionLog.metadata_["api_usage_id"].astext
    ).filter(
        models.FlowExecutionLog.execution_id == execution.id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
        models.FlowExecutionLog.metadata_["source"].astext == "gateway",
    )
    missing = expected_query.filter(
        ~cast(models.ApiUsage.id, String).in_(captured_ids)
    ).count()
    return {
        "records": records,
        "skip": skip,
        "total": total,
        "has_more": skip + len(rows) < total,
        "gateway_request_count": expected,
        "missing_gateway_requests": missing,
        "coverage": "missing" if missing else ("captured" if total else "unavailable"),
        "native_command_coverage": "observed_events_only",
        "representation": "preloop.tool-evidence/v1; sorted compact JSON; UTF-8; sanitized",
        "page_sha256": digest(records),
    }


def callback_diagnostics(
    db: Session, *, execution: models.FlowExecution, principal_id: UUID
) -> list[dict[str, Any]]:
    """No URL, signing material, response body or other principal's deliveries."""
    rows = (
        db.query(models.WebhookDelivery)
        .join(models.WebhookEndpoint)
        .filter(
            models.WebhookDelivery.subject_id == execution.id,
            models.WebhookEndpoint.ci_principal_id == principal_id,
            models.WebhookDelivery.event_type == "flow.execution.finished",
        )
        .order_by(models.WebhookDelivery.created_at)
        .limit(100)
        .all()
    )
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "attempt_count": row.attempt_count,
            "response_status": row.response_status,
            "next_attempt_at": row.next_attempt_at,
        }
        for row in rows
    ]
