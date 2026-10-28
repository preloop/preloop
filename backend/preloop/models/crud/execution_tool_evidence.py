"""Scoped durable tool evidence and safe terminal-delivery diagnostics."""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import String, cast
from sqlalchemy.dialects.postgresql import insert

from sqlalchemy.orm import Session

from preloop.utils.jsonb_sanitize import sanitize_for_jsonb

from preloop.models import models
from preloop.services.execution_tool_evidence import (
    EVIDENCE_TYPE,
    canonical_bytes,
    digest,
)


logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 8 * 1024 * 1024


def _fail_soft(
    db: Session,
    *,
    execution_id: Any,
    source: str,
    source_id: str | None,
    write: Callable[[], None],
) -> None:
    """Run an evidence write in a savepoint so a failure never aborts the caller.

    Evidence rides on the gateway usage transaction and the agent log batch.
    Losing either to an evidence failure would defeat the point of evidence,
    so failures are logged and replaced by a capture-failure marker.

    Args:
        db: Session of the surrounding transaction.
        execution_id: Execution the evidence belongs to.
        source: ``gateway`` or ``native``.
        source_id: Usage id or native item id, when known.
        write: Callable performing the evidence insert.
    """
    try:
        with db.begin_nested():
            write()
        return
    except Exception as exc:  # noqa: BLE001 - evidence must never break callers
        logger.warning(
            "Tool evidence capture failed for execution %s (%s): %s",
            execution_id,
            source,
            type(exc).__name__,
        )
    if not source_id:
        return
    try:
        with db.begin_nested():
            _persist_document(
                db,
                execution_id=UUID(str(execution_id)),
                source=source,
                source_id=source_id,
                document={
                    "version": 1,
                    "execution_id": str(execution_id),
                    "api_usage_id": source_id if source == "gateway" else None,
                    "capture_complete": False,
                    "capture_failed": True,
                    "observations": [],
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Tool evidence failure marker not stored for execution %s: %s",
            execution_id,
            type(exc).__name__,
        )


def _observation_key(observation: dict[str, Any]) -> str | None:
    """Return the request-side dedupe key, or None when not deduplicable."""
    call_id = observation.get("call_id")
    if observation.get("source") != "request" or not isinstance(call_id, str):
        return None
    return f"{observation.get('kind')}:{call_id}"


def _seen_request_keys(db: Session, execution_id: UUID) -> set[str]:
    """Load request-side observation keys already stored for an execution.

    Reads only the small ``request_keys`` metadata entry on the execution's
    indexed evidence rows, never the stored documents.
    """
    rows = db.query(models.FlowExecutionLog.metadata_["request_keys"]).filter(
        models.FlowExecutionLog.execution_id == execution_id,
        models.FlowExecutionLog.log_type == EVIDENCE_TYPE,
    )
    seen: set[str] = set()
    for (keys,) in rows:
        if isinstance(keys, list):
            seen.update(key for key in keys if isinstance(key, str))
    return seen


def persist_tool_evidence(
    db: Session, *, usage: models.ApiUsage, document: dict[str, Any]
) -> None:
    """Persist a bounded, sanitized record before the console payload is clipped.

    Never raises: a failure is logged and recorded as a capture-failure marker
    so the surrounding usage row survives.

    Args:
        db: Session of the gateway usage transaction.
        usage: Usage row the evidence belongs to.
        document: Document produced by ``capture_tools``.
    """
    if not usage.flow_execution_id:
        return
    _fail_soft(
        db,
        execution_id=usage.flow_execution_id,
        source="gateway",
        source_id=str(usage.id) if usage.id else None,
        write=lambda: _persist_tool_evidence(db, usage=usage, document=document),
    )


def _persist_tool_evidence(
    db: Session, *, usage: models.ApiUsage, document: dict[str, Any]
) -> None:
    """Deduplicate replayed request-side observations, then store the record."""
    from preloop.services.sensitive_data.storage import apply_storage_redaction

    document = {
        **document,
        "execution_id": str(usage.flow_execution_id),
        "api_usage_id": str(usage.id),
    }
    observations = document.get("observations")
    if isinstance(observations, list) and any(
        _observation_key(item) for item in observations if isinstance(item, dict)
    ):
        seen = _seen_request_keys(db, usage.flow_execution_id)
        kept = [
            item
            for item in observations
            if not isinstance(item, dict) or _observation_key(item) not in seen
        ]
        document["observations"] = kept
        document["request_duplicates_omitted"] = len(observations) - len(kept)
    sanitized = apply_storage_redaction(str(usage.account_id), document)
    sanitized["storage_redacted"] = (
        document.get("storage_redacted", False) or sanitized != document
    )
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

    source_id = sanitize_for_jsonb(source_id)
    safe = sanitize_for_jsonb(document)
    if safe != document:
        safe["storage_redacted"] = True
        safe["capture_complete"] = False
    cleaned = scrub_structure(safe)
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
                "request_keys": sorted(
                    {
                        key
                        for item in document.get("observations") or []
                        if isinstance(item, dict) and (key := _observation_key(item))
                    }
                ),
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
    """Capture Codex command events before the ordinary log string is capped.

    Never raises: a failure is logged and recorded as a capture-failure marker
    so the surrounding log batch is still written.

    Args:
        db: Session of the log batch transaction.
        execution_id: Execution that emitted the log line.
        line: Raw agent log line.
    """
    _fail_soft(
        db,
        execution_id=execution_id,
        source="native",
        source_id=_native_item_id(line),
        write=lambda: _persist_native_command_evidence(
            db, execution_id=execution_id, line=line
        ),
    )


def _native_item_id(line: Any) -> str | None:
    """Best-effort native item id for a failure marker."""
    import json

    if not isinstance(line, str) or not line.startswith("{"):
        return None
    try:
        item = json.loads(line).get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
    except (ValueError, AttributeError):
        return None
    if (
        not isinstance(item_id, str)
        or not 0 < len(item_id) <= 256
        or item.get("type") != "command_execution"
    ):
        return None
    return str(sanitize_for_jsonb(item_id))


def _persist_native_command_evidence(
    db: Session, *, execution_id: str, line: Any
) -> None:
    """Build and store one native command observation."""
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
    bounded = len(canonical_bytes(value)) <= MAX_EVIDENCE_BYTES
    storage_redacted = sanitize_for_jsonb(raw) != raw
    complete = bounded and not storage_redacted
    document = {
        "version": 1,
        "execution_id": str(execution_id),
        "capture_complete": complete,
        "storage_redacted": storage_redacted,
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "event": event["type"],
                "item_id": item_id,
                "value": value if bounded else None,
                "exit_code": item.get("exit_code")
                if type(item.get("exit_code")) is int
                else None,
                "redacted": raw != value,
                "truncated": not bounded,
            }
        ],
    }
    sanitized = apply_storage_redaction(account_id, document)
    sanitized["storage_redacted"] = (
        document.get("storage_redacted", False) or sanitized != document
    )
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
    ordered = query.order_by(
        models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id
    )
    records = []
    page_bytes = 2  # JSON array brackets; metadata envelope is bounded separately.
    # One-row queries bound driver buffering too (yield_per is not sufficient
    # with every DBAPI cursor). Fetch at most one candidate beyond the page.
    for index in range(limit):
        row = ordered.offset(skip + index).limit(1).first()
        if row is None:
            break
        stored = row.metadata_ or {}
        try:
            document = json.loads("".join(stored["chunks"]))
            valid = digest(document) == stored["sha256"]
        except (KeyError, TypeError, ValueError):
            document, valid = None, False
        record = {
            "id": str(row.id),
            "api_usage_id": stored.get("api_usage_id"),
            "sha256": stored.get("sha256"),
            "integrity_valid": valid,
            "document": document if valid else None,
        }
        record_bytes = len(canonical_bytes(record)) + bool(records)
        if records and page_bytes + record_bytes > MAX_PAGE_BYTES:
            break
        records.append(record)
        page_bytes += record_bytes
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
        "has_more": skip + len(records) < total,
        "next_skip": skip + len(records),
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
