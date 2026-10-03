"""Chat persistence, atomic proof consumption and recoverable claims."""

from __future__ import annotations
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any
from sqlalchemy import or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud.audit_chain import postgres_sqlstate

from .base import CRUDBase

_CHAT_WORK_EVENT_KEY = "chat_work_connection_id_event_id_key"
_UNIQUE_VIOLATION = "23505"


class ChatLeaseLostError(RuntimeError):
    """A worker no longer owns this durable job."""


def _is_chat_work_event_replay(exc: IntegrityError) -> bool:
    """True only for a duplicate ``(connection_id, event_id)`` receipt."""
    if postgres_sqlstate(exc) != _UNIQUE_VIOLATION:
        return False
    orig = getattr(exc, "orig", None)
    diagnostic = getattr(orig, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == _CHAT_WORK_EVENT_KEY:
        return True
    return _CHAT_WORK_EVENT_KEY in str(orig if orig is not None else exc)


class CRUDChat:
    connections = CRUDBase(models.ChatConnection)
    identities = CRUDBase(models.ChatIdentity)
    codes = CRUDBase(models.ChatLinkCode)
    work = CRUDBase(models.ChatWork)

    def recent_deliveries(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        connection_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[models.ChatWork]:
        return (
            db.query(models.ChatWork)
            .filter_by(
                account_id=account_id, connection_id=connection_id, user_id=user_id
            )
            .order_by(models.ChatWork.created_at.desc(), models.ChatWork.id.desc())
            .limit(50)
            .all()
        )

    def identity(
        self,
        db: Session,
        connection_id: uuid.UUID,
        *,
        external_user_id: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> models.ChatIdentity | None:
        query = db.query(models.ChatIdentity).filter_by(connection_id=connection_id)
        if external_user_id is not None:
            query = query.filter_by(external_user_id=external_user_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return query.first()

    def principal(
        self, db: Session, connection: models.ChatConnection, external_user_id: str
    ) -> models.User | None:
        # Reload on every call: a cached principal cannot survive revocation.
        db.expire_all()
        connection = self.connections.get(db, connection.id)
        if connection is None or not connection.enabled:
            return None
        account = (
            db.query(models.Account)
            .filter_by(id=connection.account_id, is_active=True)
            .first()
        )
        identity = self.identity(db, connection.id, external_user_id=external_user_id)
        if (
            account is None
            or identity is None
            or identity.account_id != connection.account_id
        ):
            return None
        return (
            db.query(models.User)
            .filter_by(
                id=identity.user_id, account_id=connection.account_id, is_active=True
            )
            .first()
        )

    def invalidate_codes(
        self, db: Session, connection_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        db.query(models.ChatLinkCode).filter_by(
            connection_id=connection_id, user_id=user_id, consumed_at=None
        ).update({"consumed_at": datetime.utcnow()})
        db.commit()

    def eligible_approver(
        self, db: Session, user: models.User, request_id: uuid.UUID
    ) -> bool:
        request = (
            db.query(models.ApprovalRequest)
            .filter_by(id=request_id, account_id=user.account_id, status="pending")
            .first()
        )
        if request is None:
            return False
        workflow = (
            db.query(models.ApprovalWorkflow)
            .filter_by(id=request.approval_workflow_id, account_id=user.account_id)
            .first()
        )
        if workflow is None:
            return False
        users, teams = (
            workflow.approver_user_ids or [],
            workflow.approver_team_ids or [],
        )
        if str(user.id) in {str(value) for value in users}:
            return True
        if teams:
            return (
                db.query(models.TeamMembership)
                .join(models.Team, models.Team.id == models.TeamMembership.team_id)
                .filter(
                    models.TeamMembership.user_id == user.id,
                    models.Team.id.in_(teams),
                    models.Team.account_id == user.account_id,
                )
                .first()
                is not None
            )
        return not users

    def consume_code(
        self,
        db: Session,
        connection: models.ChatConnection,
        external_user_id: str,
        code: str,
        *,
        digest: str | None = None,
    ) -> models.ChatIdentity:
        proof = (
            db.query(models.ChatLinkCode)
            .filter_by(
                connection_id=connection.id,
                account_id=connection.account_id,
                digest=digest or hashlib.sha256(code.encode()).hexdigest(),
            )
            .with_for_update()
            .first()
        )
        now = datetime.utcnow()
        if proof is None or proof.consumed_at or proof.expires_at <= now:
            raise ValueError("Link code is expired or already used")
        account = (
            db.query(models.Account)
            .filter_by(id=connection.account_id, is_active=True)
            .first()
        )
        user = (
            db.query(models.User)
            .filter_by(
                id=proof.user_id, account_id=connection.account_id, is_active=True
            )
            .first()
        )
        if not connection.enabled or account is None or user is None:
            raise ValueError("Identity is unavailable")
        existing = self.identity(db, connection.id, external_user_id=external_user_id)
        own = self.identity(db, connection.id, user_id=user.id)
        if (existing and existing.user_id != user.id) or (
            own and own.external_user_id != external_user_id
        ):
            raise ValueError(
                "Identity already linked; unlink it in the dashboard first"
            )
        proof.consumed_at = now
        identity = existing or models.ChatIdentity(
            account_id=connection.account_id,
            connection_id=connection.id,
            user_id=user.id,
            external_user_id=external_user_id,
        )
        db.add(identity)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("Identity already linked") from exc
        return identity

    def receive(
        self,
        db: Session,
        *,
        connection: models.ChatConnection,
        event_id: str,
        external_user_id: str,
        payload: dict[str, Any],
    ) -> models.ChatWork:
        row = models.ChatWork(
            account_id=connection.account_id,
            connection_id=connection.id,
            event_id=event_id,
            external_user_id=external_user_id,
            payload=payload,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            # Only a collision on the receipt key is an idempotent replay.
            # Preserve foreign-key, nullability and other integrity failures.
            # psycopg2 exposes pgcode; psycopg3 exposes sqlstate.
            if not _is_chat_work_event_replay(exc):
                raise
            row = (
                db.query(models.ChatWork)
                .filter_by(connection_id=connection.id, event_id=event_id)
                .one()
            )
        return row

    def claim(self, db: Session) -> models.ChatWork | None:
        now = datetime.utcnow()
        # A crashed effectful POST/delivery is ambiguous. Do not replay it.
        db.query(models.ChatWork).filter(
            models.ChatWork.status.in_(["acting", "sending"]),
            models.ChatWork.lease_until < now,
        ).update(
            {
                "status": "uncertain",
                "last_error": "Worker stopped during an effectful operation",
            },
            synchronize_session=False,
        )
        db.query(models.ChatWork).filter(
            models.ChatWork.status == "processing",
            models.ChatWork.lease_until < now,
            models.ChatWork.attempts >= 3,
        ).update(
            {"status": "failed", "last_error": "Processing retry limit reached"},
            synchronize_session=False,
        )
        row = (
            db.query(models.ChatWork)
            .filter(
                or_(
                    and_(
                        models.ChatWork.status.in_(["pending", "reply_ready"]),
                        models.ChatWork.next_attempt_at <= now,
                        or_(
                            models.ChatWork.lease_until.is_(None),
                            models.ChatWork.lease_until < now,
                        ),
                    ),
                    and_(
                        models.ChatWork.status == "processing",
                        models.ChatWork.lease_until < now,
                    ),
                )
            )
            .order_by(models.ChatWork.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if row is None:
            db.commit()
            return None
        row.status = "processing"
        row.attempts += 1
        row.lease_token = str(uuid.uuid4())
        row.lease_until = now + timedelta(seconds=180)
        db.commit()
        row._chat_lease_token = row.lease_token
        return row

    def transition(
        self, db: Session, row: models.ChatWork, status: str, **values: Any
    ) -> bool:
        updated = (
            db.query(models.ChatWork)
            .filter_by(
                id=row.id,
                lease_token=getattr(row, "_chat_lease_token", row.lease_token),
            )
            .filter(models.ChatWork.lease_until > datetime.utcnow())
            .update({"status": status, **values}, synchronize_session=False)
        )
        db.commit()
        if not updated:
            raise ChatLeaseLostError("Chat job lease expired or was reclaimed")
        db.refresh(row)
        return True


crud_chat = CRUDChat()
