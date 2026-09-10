import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.secret_scrubbing import scrub_secrets, scrub_structure
from .base import CRUDBase


class CRUDFlowExecutionLog(CRUDBase[models.FlowExecutionLog]):
    def __init__(self) -> None:
        super().__init__(model=models.FlowExecutionLog)

    def get_by_execution_id(
        self,
        db: Session,
        execution_id: uuid.UUID,
        tail: Optional[int] = None,
        desc: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> List[models.FlowExecutionLog]:
        query = select(models.FlowExecutionLog).filter(
            models.FlowExecutionLog.execution_id == execution_id,
        )

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
        persisted and must not create duplicate events.
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
                    "message": scrub_secrets(message),
                    "metadata": scrub_structure(metadata) if metadata else None,
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
            message=scrub_secrets(message),
            metadata_=scrub_structure(metadata) if metadata else None,
        )
        db.add(log_entry)
        if commit:
            db.commit()
            db.refresh(log_entry)
        return log_entry


crud_flow_execution_log = CRUDFlowExecutionLog()
