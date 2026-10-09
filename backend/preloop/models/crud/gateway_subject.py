"""CRUD for gateway subjects seen behind a trusted upstream gateway key."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.gateway_subject import GatewaySubject
from ..models.user import User
from .base import CRUDBase

#: ``last_seen_at`` is refreshed at most this often, so the hot path does not
#: write on every request for a subject that has not changed.
LAST_SEEN_REFRESH_INTERVAL = timedelta(minutes=15)

MAX_EXTERNAL_SUBJECT_LENGTH = 255
MAX_EMAIL_LENGTH = 255


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class CRUDGatewaySubject(CRUDBase[GatewaySubject]):
    """Resolve and read gateway subjects. Never creates users."""

    def get_for_key(
        self,
        db: Session,
        *,
        account_id: Any,
        api_key_id: Any,
        external_subject: str,
    ) -> Optional[GatewaySubject]:
        """Return the subject for ``(account, key, sub)`` or ``None``."""
        return db.execute(
            select(GatewaySubject).where(
                GatewaySubject.account_id == account_id,
                GatewaySubject.api_key_id == api_key_id,
                GatewaySubject.external_subject == external_subject,
            )
        ).scalar_one_or_none()

    def get_in_account(
        self, db: Session, *, subject_id: Any, account_id: Any
    ) -> Optional[GatewaySubject]:
        """Return one subject when it belongs to ``account_id``."""
        return db.execute(
            select(GatewaySubject).where(
                GatewaySubject.id == subject_id,
                GatewaySubject.account_id == account_id,
            )
        ).scalar_one_or_none()

    def list_for_account(
        self, db: Session, *, account_id: Any, limit: int = 500
    ) -> list[GatewaySubject]:
        """List an account's subjects, most recently seen first."""
        return list(
            db.execute(
                select(GatewaySubject)
                .where(GatewaySubject.account_id == account_id)
                .order_by(GatewaySubject.last_seen_at.desc())
                .limit(limit)
            ).scalars()
        )

    @staticmethod
    def find_member_by_email(
        db: Session, *, account_id: Any, email: Optional[str]
    ) -> Optional[uuid.UUID]:
        """Return the id of an existing account member with ``email``.

        Only users already in the account match. Nothing is created.
        """
        if not email:
            return None
        rows = db.execute(
            select(User.id, User.membership_kind).where(
                User.account_id == account_id,
                func.lower(User.email) == email.lower(),
            )
        ).all()
        if not rows:
            return None
        # A direct member wins over an inherited one with the same email.
        rows.sort(key=lambda row: 0 if row.membership_kind == "direct" else 1)
        return rows[0].id

    def resolve(
        self,
        db: Session,
        *,
        account_id: Any,
        api_key_id: Any,
        external_subject: str,
        email: Optional[str],
        now: Optional[datetime] = None,
    ) -> GatewaySubject:
        """Upsert the subject for one request and commit only when changed.

        A known subject whose email is unchanged and whose ``last_seen_at``
        is recent costs one indexed read and no write. A changed email
        re-evaluates ``linked_user_id``.

        Args:
            db: Database session.
            account_id: Account of the trusted upstream key.
            api_key_id: The trusted upstream key.
            external_subject: IdP ``sub`` from ``x-claude-gateway-user-id``.
            email: Forwarded email, when present.
            now: Request time (UTC); defaults to the current time.

        Returns:
            The persisted subject.
        """
        now = now or datetime.now(timezone.utc)
        external_subject = external_subject[:MAX_EXTERNAL_SUBJECT_LENGTH]
        email = email[:MAX_EMAIL_LENGTH] if email else None
        subject = self.get_for_key(
            db,
            account_id=account_id,
            api_key_id=api_key_id,
            external_subject=external_subject,
        )
        if subject is None:
            subject = GatewaySubject(
                account_id=account_id,
                api_key_id=api_key_id,
                external_subject=external_subject,
                email=email,
                linked_user_id=self.find_member_by_email(
                    db, account_id=account_id, email=email
                ),
                first_seen_at=now,
                last_seen_at=now,
            )
            try:
                with db.begin_nested():
                    db.add(subject)
                    db.flush()
                db.commit()
            except IntegrityError:
                # A concurrent request inserted the same subject first. The
                # flush inside the savepoint raised, so only the savepoint
                # rolled back; drop the loser and read the winner.
                if subject in db:
                    db.expunge(subject)
                existing = self.get_for_key(
                    db,
                    account_id=account_id,
                    api_key_id=api_key_id,
                    external_subject=external_subject,
                )
                if existing is None:
                    raise
                return existing
            return subject

        changed = False
        if email is not None and email != subject.email:
            subject.email = email
            subject.linked_user_id = self.find_member_by_email(
                db, account_id=account_id, email=email
            )
            changed = True
        last_seen = _aware(subject.last_seen_at)
        if last_seen is None or now - last_seen >= LAST_SEEN_REFRESH_INTERVAL:
            subject.last_seen_at = now
            changed = True
        if changed:
            db.commit()
        return subject
