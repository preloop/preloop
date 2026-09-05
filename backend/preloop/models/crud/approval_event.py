"""CRUD operations for ApprovalEvent model (approval workflow timeline)."""

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session

from ..models.approval_event import ApprovalEvent
from .base import CRUDBase


class CRUDApprovalEvent(CRUDBase[ApprovalEvent]):
    """CRUD operations for approval workflow timeline events."""

    def get_by_request(
        self,
        db: Session,
        *,
        approval_request_id: Union[str, UUID],
        account_id: Optional[str] = None,
    ) -> list[ApprovalEvent]:
        """Return the timeline events for one approval request, in order.

        Args:
            db: Database session
            approval_request_id: The approval request the events belong to
            account_id: Optional account scoping; when given, only events of
                that account are returned

        Returns:
            Events ordered by timestamp (and insertion order for ties)
        """
        query = db.query(self.model).filter(
            self.model.approval_request_id == approval_request_id
        )
        if account_id:
            query = query.filter(self.model.account_id == account_id)
        return query.order_by(self.model.timestamp.asc(), self.model.id.asc()).all()

    def record(
        self,
        db: Session,
        *,
        approval_request_id: Union[str, UUID],
        account_id: Union[str, UUID],
        event_type: str,
        detail: str,
        comment: Optional[str] = None,
        actor_id: Optional[Union[str, UUID]] = None,
        commit: bool = True,
    ) -> ApprovalEvent:
        """Append one event to an approval request's timeline.

        Sync counterpart of ``ApprovalService._record_event`` for the sync
        request handlers (view tracking, token-page reads).

        Args:
            db: Database session
            approval_request_id: The approval request the event belongs to
            account_id: The account the event belongs to
            event_type: Timeline event type (approval_requested,
                notification_sent, viewed, vote_received,
                escalation_triggered, approval_complete, expired, ...)
            detail: Human-readable description rendered on the timeline
            comment: Optional approver comment attached to the event
            actor_id: Optional user who triggered the event
            commit: When False, flush only so callers can batch writes

        Returns:
            The created ApprovalEvent row
        """
        return self.create(
            db,
            obj_in={
                "approval_request_id": approval_request_id,
                "account_id": account_id,
                "event_type": event_type,
                "detail": detail,
                "comment": comment,
                "actor_id": actor_id,
            },
            commit=commit,
        )

    def has_event(
        self,
        db: Session,
        *,
        approval_request_id: Union[str, UUID],
        event_type: str,
        actor_id: Optional[Union[str, UUID]] = None,
        actor_is_null: bool = False,
    ) -> bool:
        """True when a matching event already exists on the request's timeline.

        Used to dedupe repeated timeline entries (e.g. one ``viewed`` event
        per viewer instead of one per page load).

        Args:
            db: Database session
            approval_request_id: The approval request to inspect
            event_type: Event type to look for
            actor_id: Exact actor to match (ignored when ``actor_is_null``)
            actor_is_null: When True, match only events without an actor
                (anonymous/token views)

        Returns:
            True if such an event exists
        """
        query = db.query(self.model).filter(
            self.model.approval_request_id == approval_request_id,
            self.model.event_type == event_type,
        )
        if actor_is_null:
            query = query.filter(self.model.actor_id.is_(None))
        elif actor_id is not None:
            query = query.filter(self.model.actor_id == actor_id)
        return bool(db.query(query.exists()).scalar())


# Create instance
crud_approval_event = CRUDApprovalEvent(ApprovalEvent)
