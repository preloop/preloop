import uuid
from datetime import datetime
from typing import Any, List, Optional, Sequence

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.secret_scrubbing import scrub_secrets, scrub_structure
from .base import CRUDBase

#: Longest message stored for one log line. Agent streams can emit one huge
#: line of binary garbage on a disconnect; the tail is not useful.
MAX_LOG_MESSAGE_CHARS = 65536
_TRUNCATION_MARKER = " [truncated]"


def _clean_text(value: str) -> str:
    """Replace NUL, which PostgreSQL text and JSONB reject (#1196)."""
    return value.replace("\x00", "\ufffd") if "\x00" in value else value


def _clean_structure(value: Any) -> Any:
    """Apply :func:`_clean_text` to every string key and value."""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return {
            _clean_text(k) if isinstance(k, str) else k: _clean_structure(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean_structure(item) for item in value]
    return value


def _storable_message(message: Any) -> Any:
    """Scrub secrets, drop NUL and cap the length of one log message."""
    message = scrub_secrets(message)
    if not isinstance(message, str):
        return message
    message = _clean_text(message)
    if len(message) > MAX_LOG_MESSAGE_CHARS:
        keep = MAX_LOG_MESSAGE_CHARS - len(_TRUNCATION_MARKER)
        message = message[:keep] + _TRUNCATION_MARKER
    return message


def _storable_metadata(metadata: Any) -> Any:
    """Scrub secrets and drop NUL from log metadata."""
    return _clean_structure(scrub_structure(metadata)) if metadata else None


class CRUDFlowExecutionLog(CRUDBase[models.FlowExecutionLog]):
    def __init__(self) -> None:
        super().__init__(model=models.FlowExecutionLog)

    def list_by_type(
        self,
        db: Session,
        *,
        execution_id: uuid.UUID,
        log_type: str,
        limit: int = 200,
    ) -> List[models.FlowExecutionLog]:
        """Log rows of one kind for one execution, oldest first.

        Small, bounded and typed, because some log rows are state a later
        turn reads back (a refused delegation is the first: it creates no
        execution row, so the parent's timeline is where it lives).
        """
        query = (
            select(models.FlowExecutionLog)
            .filter(
                models.FlowExecutionLog.execution_id == execution_id,
                models.FlowExecutionLog.log_type == log_type,
            )
            .order_by(
                models.FlowExecutionLog.timestamp.asc(),
                models.FlowExecutionLog.id.asc(),
            )
            .limit(max(1, limit))
        )
        return list(db.execute(query).scalars().all())

    def get_by_execution_id(
        self,
        db: Session,
        execution_id: uuid.UUID,
        tail: Optional[int] = None,
        desc: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
        log_types: Optional[Sequence[str]] = None,
    ) -> List[models.FlowExecutionLog]:
        query = select(models.FlowExecutionLog).filter(
            models.FlowExecutionLog.execution_id == execution_id,
        )
        if log_types is not None:
            # Filter in SQL so the tail counts only the requested rows: a long
            # run's agent log lines must not push its model calls out of it.
            query = query.filter(models.FlowExecutionLog.log_type.in_(log_types))

        if desc:
            query = query.order_by(models.FlowExecutionLog.timestamp.desc())
        else:
            query = query.order_by(models.FlowExecutionLog.timestamp.asc())

        if skip > 0:
            query = query.offset(skip)

        if tail:
            query = query.limit(tail)
        elif limit is not None:
            query = query.limit(limit)

        rows = db.execute(query).scalars().all()

        # If descending order was used (typically for tail), reverse to maintain chronological order
        if tail and desc:
            rows = list(reversed(rows))

        return list(rows)

    def get_agent_log_page(
        self,
        db: Session,
        execution_id: uuid.UUID,
        *,
        after: Optional[tuple[datetime, uuid.UUID]] = None,
        limit: int = 500,
    ) -> List[models.FlowExecutionLog]:
        """Read one bounded raw-log page, using an execution-scoped keyset cursor.

        Timestamp ties use row identity, so a page boundary cannot skip another
        line with the same timestamp. Derived metrics/events are never replayed.
        """
        query = select(models.FlowExecutionLog).where(
            models.FlowExecutionLog.execution_id == execution_id,
            models.FlowExecutionLog.log_type == "agent_log_line",
        )
        if after is not None:
            query = query.where(
                tuple_(models.FlowExecutionLog.timestamp, models.FlowExecutionLog.id)
                > after
            )
        query = query.order_by(
            models.FlowExecutionLog.timestamp.asc(), models.FlowExecutionLog.id.asc()
        ).limit(max(1, min(limit, 1000)))
        return list(db.execute(query).scalars().all())

    def get_event_by_id(
        self,
        db: Session,
        execution_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> Optional[models.FlowExecutionLog]:
        query = select(models.FlowExecutionLog).filter(
            models.FlowExecutionLog.execution_id == execution_id,
            models.FlowExecutionLog.id == event_id,
        )
        return db.execute(query).scalar_one_or_none()

    def append_logs(self, db: Session, batch: list[tuple[str, dict]]) -> None:
        """Insert a scrubbed batch atomically with stable IDs for safe retries.

        Callers retain each entry's ``_persistence_id`` across retries, including
        retries after an ambiguous commit failure. Conflicting IDs are already
        persisted and must not create duplicate events. NUL bytes are
        replaced and long messages capped so one bad line cannot fail the
        whole batch.
        """
        if not batch:
            return
        rows = []
        for execution_id, log_data in batch:
            payload = log_data.get("payload") or {}
            message = (
                log_data.get("message") or payload.get("line") or payload.get("message")
            )
            metadata = payload or log_data.get("metadata") or log_data.get("data")
            rows.append(
                {
                    "id": uuid.UUID(log_data["_persistence_id"]),
                    "execution_id": uuid.UUID(execution_id),
                    "log_type": log_data.get("type", "log"),
                    "message": _storable_message(message),
                    "metadata": _storable_metadata(metadata),
                }
            )
        statement = insert(models.FlowExecutionLog.__table__).values(rows)
        db.execute(statement.on_conflict_do_nothing(index_elements=["id"]))
        db.commit()

    def append_log(
        self, db: Session, execution_id: str, log_data: dict, *, commit: bool = True
    ) -> models.FlowExecutionLog:
        """Append a log entry.

        Moved from crud_flow_execution.append_log for better grouping.

        Both the message and the metadata payload are scrubbed of known
        credential formats here. This is the last gate before persistence, so
        it also covers producers that did not scrub at the source (issue #173).
        """
        payload = log_data.get("payload") or {}
        message = (
            log_data.get("message") or payload.get("line") or payload.get("message")
        )
        metadata = payload or log_data.get("metadata") or log_data.get("data")

        log_entry = models.FlowExecutionLog(
            execution_id=execution_id,
            log_type=log_data.get("type", "log"),
            message=_storable_message(message),
            metadata_=_storable_metadata(metadata),
        )
        db.add(log_entry)
        if commit:
            db.commit()
            db.refresh(log_entry)
        return log_entry


crud_flow_execution_log = CRUDFlowExecutionLog()
