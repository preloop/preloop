"""CRUD operations for WebAuthn passkey credentials."""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models.webauthn_credential import WebAuthnCredential
from .base import CRUDBase


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

    def list_for_user(self, db: Session, *, user_id) -> List[WebAuthnCredential]:
        """List active credentials for a user."""
        return (
            db.query(self.model)
            .filter(
                self.model.user_id == user_id,
                self.model.is_active.is_(True),
            )
            .order_by(self.model.created_at)
            .all()
        )

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
