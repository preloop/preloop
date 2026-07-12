"""CRUD operations for durable Agent Control command persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

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
            )
            .order_by(
                AgentControlCommand.created_at.desc(), AgentControlCommand.id.desc()
            )
            .limit(limit)
            .all()
        )
