"""CRUD operations for WebAuthn passkey credentials."""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase

WebAuthnCredential = models.WebAuthnCredential


class CRUDWebAuthnCredential(CRUDBase[WebAuthnCredential]):
    """CRUD operations for WebAuthn credentials."""

    def get_by_credential_id(
        self, db: Session, *, credential_id: str
    ) -> Optional[WebAuthnCredential]:
        """Look up an active credential by its base64url credential ID."""
        return (
            db.query(self.model)
            .filter(
                self.model.credential_id == credential_id,
                self.model.is_active.is_(True),
            )
            .first()
        )

    def lock_active(
        self, db: Session, *, credential_id: str
    ) -> Optional[WebAuthnCredential]:
        """Recheck an active passkey after locking its person and membership."""
        return (
            db.query(self.model)
            .filter(
                self.model.credential_id == credential_id,
                self.model.is_active.is_(True),
            )
            .with_for_update()
            .populate_existing()
            .first()
        )

    def list_for_person(
        self, db: Session, *, person_id: UUID
    ) -> List[WebAuthnCredential]:
        """List identity-owned passkeys independent of registration membership."""
        return (
            db.query(self.model)
            .filter(self.model.person_id == person_id, self.model.is_active.is_(True))
            .order_by(self.model.created_at)
            .all()
        )

    def list_for_user(self, db: Session, *, user_id: UUID) -> List[WebAuthnCredential]:
        """Compatibility wrapper resolving membership to the credential owner."""
        from .person import crud_person

        person = crud_person.resolve_for_membership(db, membership_id=user_id)
        return self.list_for_person(db, person_id=person.id)

    def touch(
        self, db: Session, *, obj: WebAuthnCredential, sign_count: int
    ) -> WebAuthnCredential:
        """Record a successful authentication: update counter and timestamp."""
        obj.sign_count = sign_count
        obj.last_used_at = datetime.now(timezone.utc)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    def deactivate(self, db: Session, *, obj: WebAuthnCredential) -> None:
        """Soft-delete a credential."""
        obj.is_active = False
        db.add(obj)
        db.commit()
