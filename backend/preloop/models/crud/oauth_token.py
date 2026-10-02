"""CRUD operations for OAuthToken model."""

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..models.github_oauth_token import OAuthToken
from .base import CRUDBase


class CRUDOAuthToken(CRUDBase[OAuthToken]):
    """CRUD operations for OAuthToken model."""

    def get_by_user_and_installation(
        self,
        db: Session,
        *,
        provider: str,
        user_id: UUID,
        installation_id: UUID,
    ) -> Optional[OAuthToken]:
        """Get token by provider, user, and installation.

        Args:
            db: Database session
            provider: OAuth provider (e.g., 'github')
            user_id: User UUID
            installation_id: Installation UUID

        Returns:
            Token if found, None otherwise
        """
        return (
            db.query(self.model)
            .filter(
                self.model.provider == provider,
                self.model.auth_mode.is_(None),
                self.model.user_id == user_id,
                self.model.installation_id == installation_id,
            )
            .first()
        )

    def get_by_user_and_provider(
        self,
        db: Session,
        *,
        provider: str,
        user_id: UUID,
    ) -> Optional[OAuthToken]:
        """Get token by provider and user.

        Args:
            db: Database session
            provider: OAuth provider (e.g., 'github')
            user_id: User UUID

        Returns:
            Token if found, None otherwise
        """
        return (
            db.query(self.model)
            .filter(
                self.model.provider == provider,
                self.model.auth_mode.is_(None),
                self.model.user_id == user_id,
            )
            .first()
        )


crud_oauth_token = CRUDOAuthToken(OAuthToken)
