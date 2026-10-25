"""Durable transactional audit outbox: enqueue in the caller's transaction,
drain on a relay thread that owns its sessions (EE #94).

Behind ``PRELOOP_AUDIT_OUTBOX_ENABLED`` (default off). With the flag off the
bounded best-effort writer in ``async_writer`` is unchanged.

With the flag on:

* ``enqueue_in_transaction`` adds an intent row to the caller's session and
  only flushes it, so the record commits or rolls back with the business
  write. Nothing remote is contacted on the caller's path.
* The existing ``*_async`` wrappers keep their bounded executor as the fast
  path, but the executor job now writes the intent instead of the audit row.
  A short local insert replaces the audit write, and the intent survives a
  restart.
* Intent payloads get the same redaction as the audit row before they are
  stored (``sanitize_intent_fields``).
* ``AuditOutboxRelay`` claims one due intent at a time with
  ``FOR UPDATE SKIP LOCKED``, deletes it and calls the same
  ``AuditService.log_*`` method the direct path uses. That method's commit
  commits the ``audit_log`` row and the delete together: a crash in between
  leaves both uncommitted, so replay never duplicates a row, and a delivered
  intent leaves no copy of its payload behind.
* A failed delivery backs off exponentially and parks as ``failed`` after
  ``max_attempts``; parked intents stay in the table for operator replay.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import audit_outbox as crud_audit_outbox

from .async_writer import FLAG_ENV, outbox_enabled

__all__ = ["FLAG_ENV", "outbox_enabled"]

logger = logging.getLogger(__name__)

#: ``AuditService`` methods the relay may call. A stored operation outside
#: this set is never dispatched, so a row cannot name an arbitrary attribute.
OUTBOX_OPERATIONS = frozenset(
    {
        "log_tool_call",
        "log_policy_decision",
        "log_approval_lifecycle",
        "log_approval_notification",
        "log_approval_tool_executed",
        "log_runtime_session_event",
        "log_model_gateway_request",
        "log_session_search",
        "log_ai_approval",
    }
)

_UUID_TAG = "__uuid__"
_DATETIME_TAG = "__datetime__"


def encode_payload(value: Any) -> Any:
    """Make audit kwargs JSON-safe while keeping UUIDs and datetimes typed."""
    if isinstance(value, UUID):
        return {_UUID_TAG: str(value)}
    if isinstance(value, datetime):
        return {_DATETIME_TAG: value.isoformat()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): encode_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode_payload(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def decode_payload(value: Any) -> Any:
    """Inverse of :func:`encode_payload`."""
    if isinstance(value, dict):
        if set(value) == {_UUID_TAG}:
            return UUID(value[_UUID_TAG])
        if set(value) == {_DATETIME_TAG}:
            return datetime.fromisoformat(value[_DATETIME_TAG])
        return {k: decode_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_payload(v) for v in value]
    return value


def sanitize_intent_fields(
    fields: dict[str, Any],
    *,
    db: Optional[Session] = None,
    operation: Optional[str] = None,
) -> dict[str, Any]:
    """Apply the audit redaction before an intent is persisted.

    The ``log_*`` methods redact when they write ``audit_log``; the intent is
    written earlier, so it gets the same treatment here. Every step is
    idempotent, so the relay's second pass changes nothing.
    """
    from .import_path import import_audit_submodule

    service_module = import_audit_submodule("service")
    service = service_module.get_audit_service()
    clean = dict(fields)
    if isinstance(clean.get("tool_args"), dict):
        clean["tool_args"] = service._sanitize_tool_args(clean["tool_args"])
    if "gateway_subject" in clean:
        clean["gateway_subject"] = service_module.safe_gateway_subject(
            clean["gateway_subject"]
        )
    if "grant" in clean:
        clean["grant"] = service_module._safe_grant_metadata(clean["grant"])
    extra = clean.get("extra_details")
    if isinstance(extra, dict) and "grant" in extra:
        clean["extra_details"] = {
            **extra,
            "grant": service_module._safe_grant_metadata(extra["grant"]),
        }
    for key in ("error", "error_code", "error_reason"):
        if isinstance(clean.get(key), str):
            clean[key] = service_module._safe_error_detail(clean[key])
    if isinstance(clean.get("extra_details"), dict) and isinstance(
        clean["extra_details"].get("error"), str
    ):
        clean["extra_details"] = {
            **clean["extra_details"],
            "error": service_module._safe_error_detail(clean["extra_details"]["error"]),
        }
    if operation == "log_approval_tool_executed":
        clean = service_module._safe_approval_execution_fields(clean)
        if isinstance(clean.get("extra_details"), dict):
            clean["extra_details"] = service_module._safe_approval_execution_fields(
                clean["extra_details"]
            )
    if operation == "log_session_search" and isinstance(clean.get("query"), str):
        from preloop.services.session_search_audit import query_hash

        query = clean["query"]
        clean["query_length"] = len(query)
        clean["query_hash"] = query_hash(query)
        if db is None or not service_module._record_query_text(db, clean["account_id"]):
            # Text requires explicit opt-in before the intent is persisted.
            clean["query"] = None
        else:
            clean["query"] = query[: service_module.MAX_QUERY_TEXT_CHARS]
    return clean


def enqueue_intent(
    db: Session,
    operation: str,
    fields: dict[str, Any],
    *,
    priority: bool = False,
) -> None:
    """Add one audit intent to ``db`` without committing.

    Raises:
        ValueError: ``operation`` is not an allowlisted audit method, or the
            fields carry no ``account_id``.
    """
    if operation not in OUTBOX_OPERATIONS:
        raise ValueError(f"Unsupported audit outbox operation: {operation}")
    account_id = fields.get("account_id")
    if account_id is None:
        raise ValueError("Audit outbox intent requires account_id")
    crud_audit_outbox.enqueue(
        db,
        account_id=account_id,
        operation=operation,
        payload=encode_payload(
            sanitize_intent_fields(fields, db=db, operation=operation)
        ),
        priority=priority,
    )


def intent_operation(
    operation: Callable[..., None], priority: bool
) -> Optional[Callable[..., None]]:
    """Wrap an audit method so the executor writes its intent, not its row.

    Returns ``None`` when ``operation`` is not an allowlisted audit method,
    so the caller keeps the direct write.
    """
    name = getattr(operation, "__name__", None)
    if name not in OUTBOX_OPERATIONS:
        return None

    def _write(db: Session, **fields: Any) -> None:
        enqueue_intent(db, name, fields, priority=priority)
        db.commit()

    _write.__name__ = f"outbox_{name}"
    return _write


_attempt_failure_counter: Any = None
_lag_histogram: Any = None
_lag_gauge: Any = None
_parked_gauge: Any = None
_metrics_failed = False


def _record_metric(kind: str, value: float, attributes: dict[str, str]) -> None:
    """Emit drain lag and failed attempts; never let telemetry break the relay."""
    global \
        _attempt_failure_counter, \
        _lag_histogram, \
        _lag_gauge, \
        _parked_gauge, \
        _metrics_failed
    if _metrics_failed:
        return
    try:
        from opentelemetry import metrics

        meter = metrics.get_meter("preloop.audit")
        if kind == "failed":
            if _attempt_failure_counter is None:
                _attempt_failure_counter = meter.create_counter(
                    "preloop.audit.outbox.attempts_failed",
                    unit="{attempt}",
                    description="Audit outbox admission, delivery and relay failures",
                )
            _attempt_failure_counter.add(int(value), attributes)
        elif kind == "parked":
            if _parked_gauge is None:
                _parked_gauge = meter.create_gauge(
                    "preloop.audit.outbox.parked",
                    description="Parked audit intents requiring operator replay",
                )
            _parked_gauge.set(int(value), attributes)
        else:
            if _lag_histogram is None:
                _lag_histogram = meter.create_histogram(
                    "preloop.audit.outbox.lag_seconds",
                    unit="s",
                    description="Age of the oldest pending audit outbox intent",
                )
            _lag_histogram.record(value, attributes)
            if _lag_gauge is None:
                _lag_gauge = meter.create_gauge(
                    "preloop.audit.outbox.oldest_pending_age",
                    unit="s",
                    description="Current age of the oldest due audit intent",
                )
            _lag_gauge.set(value, attributes)
    except Exception:
        _metrics_failed = True


def record_admission_failure(reason: str) -> None:
    """Alert on local admission lost after an irreversible external effect."""
    _record_metric("failed", 1, {"reason": reason})


class AuditOutboxRelay:
    """Drain due audit intents on a daemon thread with its own sessions."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        service_getter: Callable[[], Any],
        poll_interval: float = 1.0,
        batch_size: int = 100,
        max_attempts: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._service_getter = service_getter
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def deliver_one(self) -> Optional[bool]:
        """Deliver the next due intent.

        Returns:
            ``None`` when nothing is due, ``True`` when the audit row and the
            delivered mark committed, ``False`` when the attempt failed and
            was rescheduled or parked.
        """
        db = self._session_factory()
        try:
            row = crud_audit_outbox.claim_next(db)
            if row is None:
                db.rollback()
                return None
            outbox_id = row.id
            operation = row.operation
            payload = decode_payload(row.payload)
            if operation not in OUTBOX_OPERATIONS:
                db.rollback()
                self._fail(db, outbox_id, "unsupported operation", park=True)
                return False
            enqueued_at = row.created_at
            # Delete rather than mark: the intent's payload must not outlive
            # the audit row, and absence after the commit is the success
            # signal. The delete commits with the audit row or not at all.
            crud_audit_outbox.delete_for_delivery(db, row)
            error = "audit write did not commit"
            try:
                method = getattr(self._service_getter(), operation)
                payload.pop("db", None)
                if enqueued_at is not None:
                    payload = _with_enqueued_at(operation, payload, enqueued_at)
                # The log_* method commits the audit row; the delivered mark
                # pending in this session commits with it.
                method(db=db, **payload)
            except Exception as exc:
                error = type(exc).__name__
            db.rollback()
            if crud_audit_outbox.get_status(db, outbox_id) is None:
                db.rollback()
                return True
            db.rollback()
            self._fail(db, outbox_id, error)
            return False
        finally:
            db.close()

    def _fail(
        self, db: Session, outbox_id: UUID, error: str, *, park: bool = False
    ) -> None:
        _record_metric("failed", 1, {"reason": error[:40]})
        try:
            crud_audit_outbox.record_failure(
                db,
                outbox_id=outbox_id,
                error=error,
                max_attempts=1 if park else self._max_attempts,
            )
        except Exception:
            db.rollback()
            logger.warning("Could not record audit outbox failure for %s", outbox_id)

    def drain(self) -> int:
        """Deliver up to one batch. Returns the number delivered."""
        delivered = 0
        for _ in range(self._batch_size):
            if self._stop.is_set():
                break
            result = self.deliver_one()
            if result is None:
                break
            if result:
                delivered += 1
        return delivered

    def _report_lag(self) -> None:
        db = self._session_factory()
        try:
            _record_metric("lag", crud_audit_outbox.oldest_pending_age_seconds(db), {})
            _record_metric("parked", crud_audit_outbox.parked_count(db), {})
        except Exception:
            _record_metric("failed", 1, {"reason": "health_query"})
        finally:
            db.close()

    def _run(self) -> None:
        delay = self._poll_interval
        while not self._stop.is_set():
            try:
                self.drain()
                self._report_lag()
                delay = self._poll_interval
            except Exception:
                # Database down: keep intents pending and back off.
                _record_metric("failed", 1, {"reason": "relay_pass"})
                logger.warning("Audit outbox relay pass failed; retrying")
                delay = min(delay * 2, 60.0)
            self._stop.wait(delay)

    def start(self) -> None:
        """Start the relay thread if it is not already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="preloop-audit-outbox", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the relay to stop; pending intents remain for the next start."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def _with_enqueued_at(
    operation: str, payload: dict[str, Any], enqueued_at: datetime
) -> dict[str, Any]:
    """Carry the intent time into details where the method accepts extras."""
    if operation in {"log_policy_decision", "log_approval_lifecycle"}:
        extra = dict(payload.get("extra_details") or {})
        extra.setdefault("outbox_enqueued_at", enqueued_at.isoformat())
        return {**payload, "extra_details": extra}
    return payload


_relay: Optional[AuditOutboxRelay] = None


def start_relay() -> Optional[AuditOutboxRelay]:
    """Start the process relay when the flag is on; no-op otherwise."""
    global _relay
    if not outbox_enabled():
        return None
    if _relay is None:
        from preloop.models.db.session import get_session_factory

        from .import_path import import_audit_submodule

        _relay = AuditOutboxRelay(
            session_factory=get_session_factory(),
            service_getter=import_audit_submodule("service").get_audit_service,
        )
    _relay.start()
    return _relay


def stop_relay() -> None:
    """Stop the process relay if running."""
    if _relay is not None:
        _relay.stop()
