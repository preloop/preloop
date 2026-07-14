"""CRUD operations for identity links (cross-device correlation graph)."""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.identity_link import IdentityLink
from .base import CRUDBase


class CRUDIdentityLink(CRUDBase[IdentityLink]):
    """CRUD operations for identity links."""

    def upsert_link(
        self,
        db: Session,
        *,
        principal_type: str,
        principal_id: str,
        user_id: Optional[UUID],
        account_id: Optional[UUID] = None,
        source: Optional[str] = None,
        meta: Optional[dict] = None,
        seen_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> None:
        """Idempotently record an identity observation.

        Inserts a new (principal, user) edge or bumps ``last_seen_at`` on the
        existing one; ``account_id`` is filled in when previously missing.
        """
        now = seen_at or datetime.now(timezone.utc)
        statement = pg_insert(IdentityLink).values(
            principal_type=principal_type,
            principal_id=principal_id,
            user_id=user_id,
            account_id=account_id,
            source=source,
            meta=meta,
            first_linked_at=now,
            last_seen_at=now,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_identity_link_principal_user",
            set_={
                "last_seen_at": now,
                # Keep an already-known account; fill it in when missing.
                "account_id": func.coalesce(
                    IdentityLink.account_id, statement.excluded.account_id
                ),
            },
        )
        db.execute(statement)
        if commit:
            db.commit()

    def get_for_user(self, db: Session, *, user_id: UUID) -> List[IdentityLink]:
        """All principals observed acting as this user."""
        return (
            db.query(IdentityLink)
            .filter(IdentityLink.user_id == user_id)
            .order_by(IdentityLink.last_seen_at.desc())
            .all()
        )

    def get_for_principal(
        self, db: Session, *, principal_type: str, principal_id: str
    ) -> List[IdentityLink]:
        """All users this principal has been observed acting as."""
        return (
            db.query(IdentityLink)
            .filter(
                IdentityLink.principal_type == principal_type,
                IdentityLink.principal_id == principal_id,
            )
            .order_by(IdentityLink.last_seen_at.desc())
            .all()
        )


crud_identity_link = CRUDIdentityLink(IdentityLink)
