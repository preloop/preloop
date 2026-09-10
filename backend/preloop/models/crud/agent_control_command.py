"""CRUD operations for durable Agent Control command persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.agent_control_command import AgentControlCommand
from .base import CRUDBase


class CRUDAgentControlCommand(CRUDBase[AgentControlCommand]):
    """CRUD operations for persisted Agent Control command envelopes.

    Status state machine: ``pending`` -> ``delivered`` -> ``acked``, with
    ``failed`` (no delivery channel) and ``expired`` (pending past
    ``expires_at``) as terminal side states.
    """

    def create_command(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Union[uuid.UUID, str],
        runtime_session_id: Optional[Union[uuid.UUID, str]],
        command_id: str,
        envelope: Dict[str, Any],
        source: Optional[str] = None,
        created_by_user_id: Optional[Union[uuid.UUID, str]] = None,
        expires_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> AgentControlCommand:
        """Persist one command envelope as pending before any delivery."""
        # Set created_at explicitly: the DB server_default now() is the
        # transaction timestamp, which ties for same-transaction inserts and
        # would make redelivery order nondeterministic.
        record = AgentControlCommand(
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            account_id=account_id,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            command_id=command_id,
            envelope=envelope,
            status="pending",
            source=source,
            created_by_user_id=created_by_user_id,
            expires_at=expires_at,
        )
        db.add(record)
        if commit:
            db.commit()
            db.refresh(record)
        else:
            db.flush()
        return record

    def get_by_command_id(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        command_id: str,
        managed_agent_id: Optional[Union[uuid.UUID, str]] = None,
    ) -> Optional[AgentControlCommand]:
        """Resolve one command by its account-scoped envelope id.

        When ``managed_agent_id`` is provided, the lookup is additionally
        scoped to that agent so a peer agent in the same account cannot
        ack or mark delivery for another agent's commands.
        """
        query = db.query(AgentControlCommand).filter(
            AgentControlCommand.account_id == account_id,
            AgentControlCommand.command_id == command_id,
        )
        if managed_agent_id is not None:
            query = query.filter(
                AgentControlCommand.managed_agent_id == managed_agent_id
            )
        return query.first()

    def mark_delivered(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        command_id: str,
        delivered_at: datetime,
        managed_agent_id: Optional[Union[uuid.UUID, str]] = None,
        commit: bool = True,
    ) -> Optional[AgentControlCommand]:
        """Transition a pending command to delivered (idempotent)."""
        record = self.get_by_command_id(
            db,
            account_id=account_id,
            command_id=command_id,
            managed_agent_id=managed_agent_id,
        )
        if record is None:
            return None
        if record.status == "pending":
            record.status = "delivered"
            record.delivered_at = delivered_at
            if commit:
                db.commit()
        return record

    def mark_acked(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        command_id: str,
        acked_at: datetime,
        managed_agent_id: Optional[Union[uuid.UUID, str]] = None,
        commit: bool = True,
    ) -> Optional[AgentControlCommand]:
        """Record end-to-end acknowledgement from the runtime plugin.

        Returns ``None`` for unknown command ids (or ids belonging to a
        different agent when ``managed_agent_id`` is set) so callers can
        log and continue — acks are tolerant, never errors.
        """
        record = self.get_by_command_id(
            db,
            account_id=account_id,
            command_id=command_id,
            managed_agent_id=managed_agent_id,
        )
        if record is None:
            return None
        if record.status in {"pending", "delivered"}:
            record.status = "acked"
            record.acked_at = acked_at
            if record.delivered_at is None:
                # An ack implies delivery even if the delivery mark was lost.
                record.delivered_at = acked_at
            if commit:
                db.commit()
        return record

    def mark_failed(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        command_id: str,
        error: str,
        managed_agent_id: Optional[Union[uuid.UUID, str]] = None,
        commit: bool = True,
    ) -> Optional[AgentControlCommand]:
        """Mark a command as failed when no delivery channel was available."""
        record = self.get_by_command_id(
            db,
            account_id=account_id,
            command_id=command_id,
            managed_agent_id=managed_agent_id,
        )
        if record is None:
            return None
        if record.status == "pending":
            record.status = "failed"
            record.last_error = error
            if commit:
                db.commit()
        elif record.last_error != error:
            # Preserve terminal status; only refresh the diagnostic when it
            # changed so repeated failure marks stay cheap.
            record.last_error = error
            if commit:
                db.commit()
        return record

    def get_undelivered_for_agent(
        self,
        db: Session,
        *,
        managed_agent_id: Union[uuid.UUID, str],
        now: datetime,
        limit: int = 100,
    ) -> List[AgentControlCommand]:
        """List pending, unexpired commands for redelivery in send order.

        ``limit`` caps how many envelopes are loaded per reconnect so a
        long offline period cannot flood the WebSocket or RAM.
        """
        return (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.managed_agent_id == managed_agent_id,
                AgentControlCommand.kind == "command",
                AgentControlCommand.status == "pending",
                (AgentControlCommand.expires_at.is_(None))
                | (AgentControlCommand.expires_at > now),
            )
            .order_by(AgentControlCommand.created_at, AgentControlCommand.id)
            .limit(max(1, limit))
            .all()
        )

    def mark_delivered_many(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Union[uuid.UUID, str],
        command_ids: List[str],
        delivered_at: datetime,
        commit: bool = True,
    ) -> int:
        """Batch-transition pending commands to delivered (one commit)."""
        if not command_ids:
            return 0
        updated = (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.account_id == account_id,
                AgentControlCommand.managed_agent_id == managed_agent_id,
                AgentControlCommand.command_id.in_(command_ids),
                AgentControlCommand.kind == "command",
                AgentControlCommand.status == "pending",
            )
            .update(
                {
                    "status": "delivered",
                    "delivered_at": delivered_at,
                },
                synchronize_session="fetch",
            )
        )
        if commit:
            db.commit()
        return int(updated)

    def expire_stale(
        self,
        db: Session,
        *,
        now: datetime,
        commit: bool = True,
    ) -> int:
        """Mark pending commands past their expires_at as expired."""
        expired = (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.status == "pending",
                AgentControlCommand.expires_at.isnot(None),
                AgentControlCommand.expires_at <= now,
            )
            .update({"status": "expired"}, synchronize_session="fetch")
        )
        if commit:
            db.commit()
        return int(expired)

    def list_recent_for_agent(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Union[uuid.UUID, str],
        limit: int = 50,
    ) -> List[AgentControlCommand]:
        """List an agent's most recent commands (newest first, for UI)."""
        return (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.account_id == account_id,
                AgentControlCommand.managed_agent_id == managed_agent_id,
                AgentControlCommand.kind == "command",
            )
            .order_by(
                AgentControlCommand.created_at.desc(), AgentControlCommand.id.desc()
            )
            .limit(limit)
            .all()
        )

    # --- operator notes ----------------------------------------------------

    def create_note(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Union[uuid.UUID, str],
        runtime_session_id: Optional[Union[uuid.UUID, str]],
        note_id: str,
        body: str,
        envelope: Dict[str, Any],
        author_display: Optional[str],
        author_auth_method: Optional[str],
        created_by_user_id: Optional[Union[uuid.UUID, str]],
        expires_at: Optional[datetime],
        source: Optional[str] = None,
        commit: bool = True,
    ) -> AgentControlCommand:
        """Persist one operator note as pending, before any delivery.

        The A2A-shaped ``envelope`` is stored verbatim so a future A2A
        endpoint can hand back exactly what was recorded, and so the delivered
        text can be rebuilt from the row alone.
        """
        record = AgentControlCommand(
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            account_id=account_id,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            command_id=note_id,
            kind="note",
            envelope=envelope,
            body=body,
            author_display=author_display,
            author_auth_method=author_auth_method,
            status="pending",
            source=source,
            created_by_user_id=created_by_user_id,
            expires_at=expires_at,
        )
        db.add(record)
        if commit:
            db.commit()
            db.refresh(record)
        else:
            db.flush()
        return record

    def get_note(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        note_id: str,
    ) -> Optional[AgentControlCommand]:
        """Resolve one note by id inside the caller's account."""
        return (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.account_id == account_id,
                AgentControlCommand.command_id == note_id,
                AgentControlCommand.kind == "note",
            )
            .first()
        )

    def list_notes(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Optional[Union[uuid.UUID, str]] = None,
        runtime_session_id: Optional[Union[uuid.UUID, str]] = None,
        limit: int = 50,
    ) -> List[AgentControlCommand]:
        """List notes for an agent or a session, newest first."""
        query = db.query(AgentControlCommand).filter(
            AgentControlCommand.account_id == account_id,
            AgentControlCommand.kind == "note",
        )
        if managed_agent_id is not None:
            query = query.filter(
                AgentControlCommand.managed_agent_id == managed_agent_id
            )
        if runtime_session_id is not None:
            query = query.filter(
                AgentControlCommand.runtime_session_id == runtime_session_id
            )
        return (
            query.order_by(
                AgentControlCommand.created_at.desc(), AgentControlCommand.id.desc()
            )
            .limit(max(1, limit))
            .all()
        )

    def list_deliverable_notes(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Optional[Union[uuid.UUID, str]],
        runtime_session_id: Optional[Union[uuid.UUID, str]],
        now: datetime,
        limit: int = 5,
    ) -> List[AgentControlCommand]:
        """Candidate notes for one session, oldest first.

        A note either names this session or names the agent with no session
        yet (delivered to whichever session the agent opens next). Expired and
        cancelled notes are never candidates. This is the query every governed
        model call runs, so it is covered by the two partial note indexes and
        reads nothing for a session with no note.
        """
        if managed_agent_id is None and runtime_session_id is None:
            return []
        targets = []
        if runtime_session_id is not None:
            targets.append(AgentControlCommand.runtime_session_id == runtime_session_id)
        if managed_agent_id is not None:
            targets.append(
                (AgentControlCommand.managed_agent_id == managed_agent_id)
                & (AgentControlCommand.runtime_session_id.is_(None))
            )
        query = db.query(AgentControlCommand).filter(
            AgentControlCommand.account_id == account_id,
            AgentControlCommand.kind == "note",
            AgentControlCommand.status == "pending",
            (AgentControlCommand.expires_at.is_(None))
            | (AgentControlCommand.expires_at > now),
        )
        query = query.filter(targets[0] if len(targets) == 1 else or_(*targets))
        return (
            query.order_by(AgentControlCommand.created_at, AgentControlCommand.id)
            .limit(max(1, limit))
            .all()
        )

    def claim_note(
        self,
        db: Session,
        *,
        note_id: uuid.UUID,
        delivered_at: datetime,
        delivery_channel: str,
        runtime_session_id: Optional[Union[uuid.UUID, str]] = None,
        turn_index: Optional[int] = None,
        commit: bool = False,
    ) -> bool:
        """Take one pending note for delivery, exactly once.

        The guard is in the UPDATE (``status = 'pending'``), so two concurrent
        gateway requests for the same session cannot both win: the loser
        matches zero rows and delivers nothing. Callers must only render a
        note this returned ``True`` for.
        """
        values: Dict[str, Any] = {
            "status": "delivered",
            "delivered_at": delivered_at,
            "delivery_channel": delivery_channel,
        }
        if turn_index is not None:
            values["delivered_turn_index"] = turn_index
        if runtime_session_id is not None:
            values["runtime_session_id"] = runtime_session_id
        claimed = (
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.id == note_id,
                AgentControlCommand.kind == "note",
                AgentControlCommand.status == "pending",
            )
            .update(values, synchronize_session="fetch")
        )
        if commit:
            db.commit()
        return int(claimed) == 1

    def cancel_note(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        note_id: str,
        cancelled_at: datetime,
        cancelled_by_user_id: Optional[Union[uuid.UUID, str]] = None,
        commit: bool = True,
    ) -> Optional[AgentControlCommand]:
        """Withdraw a note that has not been delivered yet.

        Cancelling is a state, never a delete: a delivered note cannot be
        unsent, and the row stays either way.
        """
        record = self.get_note(db, account_id=account_id, note_id=note_id)
        if record is None:
            return None
        if record.status == "pending":
            record.status = "cancelled"
            record.cancelled_at = cancelled_at
            record.cancelled_by_user_id = cancelled_by_user_id
            if commit:
                db.commit()
        return record

    def count_recent_notes_by_author(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        managed_agent_id: Union[uuid.UUID, str],
        created_by_user_id: Union[uuid.UUID, str],
        since: datetime,
    ) -> int:
        """Count one author's recent notes to one agent (rate limiting)."""
        return int(
            db.query(AgentControlCommand)
            .filter(
                AgentControlCommand.account_id == account_id,
                AgentControlCommand.managed_agent_id == managed_agent_id,
                AgentControlCommand.created_by_user_id == created_by_user_id,
                AgentControlCommand.kind == "note",
                AgentControlCommand.created_at >= since,
            )
            .count()
        )
