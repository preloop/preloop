import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models.models.flow_execution_log import FlowExecutionLog
from preloop.utils.secret_scrubbing import scrub_secrets, scrub_structure
from .base import CRUDBase


class CRUDFlowExecutionLog(CRUDBase[FlowExecutionLog]):
    def __init__(self) -> None:
        super().__init__(model=FlowExecutionLog)

    def get_by_execution_id(
        self,
        db: Session,
        execution_id: uuid.UUID,
        tail: Optional[int] = None,
        desc: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> List[FlowExecutionLog]:
        query = select(FlowExecutionLog).filter(
            FlowExecutionLog.execution_id == execution_id,
        )

        if desc:
            query = query.order_by(FlowExecutionLog.timestamp.desc())
        else:
            query = query.order_by(FlowExecutionLog.timestamp.asc())

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

    def get_event_by_id(
        self,
        db: Session,
        execution_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> Optional[FlowExecutionLog]:
        query = select(FlowExecutionLog).filter(
            FlowExecutionLog.execution_id == execution_id,
            FlowExecutionLog.id == event_id,
        )
        return db.execute(query).scalar_one_or_none()

    def append_log(
        self, db: Session, execution_id: str, log_data: dict, *, commit: bool = True
    ) -> FlowExecutionLog:
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

        log_entry = FlowExecutionLog(
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
