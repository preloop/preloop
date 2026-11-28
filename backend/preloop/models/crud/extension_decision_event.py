"""Append-only extension decision history, scoped to one account."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models

MAX_DECISION_EVENTS = 500


class CRUDExtensionDecisionEvent:
    """Write and read the history behind ``extension_approval``."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        agent_kind: str,
        extension_kind: str,
        extension_id: str,
        action: str,
        decided_by_user_id: UUID | None,
        previous_state: str | None = None,
        new_state: str | None = None,
        content_hash: str | None = None,
        note: str | None = None,
    ) -> models.ExtensionDecisionEvent:
        """Append one history row; the caller owns the transaction.

        Args:
            db: Active session.
            account_id: Account that owns the decision.
            agent_kind: Agent the extension belongs to.
            extension_kind: Extension kind, for example ``plugin``.
            extension_id: Extension identifier.
            action: ``state_changed``, ``pin_added`` or ``pin_removed``.
            decided_by_user_id: User who made the change.
            previous_state: Approval state before the change, None when new.
            new_state: Approval state after the change.
            content_hash: Pinned hash for pin actions.
            note: Decision or pin note.

        Returns:
            The flushed history row.
        """
        row = models.ExtensionDecisionEvent(
            account_id=account_id,
            agent_kind=agent_kind,
            extension_kind=extension_kind,
            extension_id=extension_id,
            action=action,
            previous_state=previous_state,
            new_state=new_state,
            content_hash=content_hash,
            note=note,
            decided_by_user_id=decided_by_user_id,
            # Clock time, not transaction time: one request may append several.
            created_at=datetime.now(UTC),
        )
        db.add(row)
        db.flush()
        return row

    def list_recent(
        self,
        db: Session,
        *,
        account_id: UUID,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest decisions first, with the decider's display name.

        Args:
            db: Active session.
            account_id: Account to read.
            limit: Maximum rows, capped at ``MAX_DECISION_EVENTS``.
            since: Only rows created at or after this time.

        Returns:
            Plain dicts ready for the response schema.
        """
        event, user = models.ExtensionDecisionEvent, models.User
        statement = (
            select(event, user.username, user.full_name)
            .outerjoin(user, user.id == event.decided_by_user_id)
            .where(event.account_id == account_id)
            .order_by(event.created_at.desc(), event.id.desc())
            .limit(max(1, min(limit, MAX_DECISION_EVENTS)))
        )
        if since is not None:
            statement = statement.where(event.created_at >= since)
        return [
            {
                "id": row.id,
                "created_at": row.created_at,
                "agent_kind": row.agent_kind,
                "extension_kind": row.extension_kind,
                "extension_id": row.extension_id,
                "action": row.action,
                "previous_state": row.previous_state,
                "new_state": row.new_state,
                "content_hash": row.content_hash,
                "note": row.note,
                "decided_by_user_id": row.decided_by_user_id,
                "decided_by": full_name or username,
            }
            for row, username, full_name in db.execute(statement).all()
        ]


crud_extension_decision_event = CRUDExtensionDecisionEvent()
