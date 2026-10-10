"""CRUD for customer OIDC providers trusted on the Anthropic gateway."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.gateway_identity_provider import (
    GatewayIdentityProvider,
    GatewayIdentityProviderAudience,
)
from .base import CRUDBase

#: Fields an update may set directly on the provider row.
UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "issuer",
        "api_key_id",
        "allowed_email_domains",
        "email_claim",
        "require_email_verified",
        "groups_claim",
        "allowed_groups",
        "required_claims",
        "clock_skew_seconds",
        "max_token_lifetime_seconds",
        "allowed_algorithms",
        "allowed_jwks_hosts",
        "allow_private_network_issuer",
        "enabled",
    }
)


class CRUDGatewayIdentityProvider(CRUDBase[GatewayIdentityProvider]):
    """Account-scoped reads and writes; the hot path reads by issuer."""

    def list_for_account(
        self, db: Session, *, account_id: Any
    ) -> list[GatewayIdentityProvider]:
        """List an account's providers by name."""
        return list(
            db.execute(
                select(GatewayIdentityProvider)
                .where(GatewayIdentityProvider.account_id == account_id)
                .order_by(GatewayIdentityProvider.name)
            ).scalars()
        )

    def get_in_account(
        self, db: Session, *, provider_id: Any, account_id: Any
    ) -> Optional[GatewayIdentityProvider]:
        """Return one provider when it belongs to ``account_id``."""
        return db.execute(
            select(GatewayIdentityProvider).where(
                GatewayIdentityProvider.id == provider_id,
                GatewayIdentityProvider.account_id == account_id,
            )
        ).scalar_one_or_none()

    def list_enabled_for_issuer(
        self, db: Session, *, issuer: str
    ) -> list[GatewayIdentityProvider]:
        """Enabled providers whose issuer equals ``issuer`` exactly.

        Several accounts may share an issuer (a multi-tenant IdP); they are
        told apart by audience, which is globally unique per issuer.
        """
        return list(
            db.execute(
                select(GatewayIdentityProvider).where(
                    GatewayIdentityProvider.issuer == issuer,
                    GatewayIdentityProvider.enabled.is_(True),
                )
            ).scalars()
        )

    def audience_taken(
        self,
        db: Session,
        *,
        issuer: str,
        audiences: Iterable[str],
        exclude_provider_id: Any = None,
    ) -> list[str]:
        """Return the audiences already claimed for ``issuer`` by any account."""
        query = select(GatewayIdentityProviderAudience.audience).where(
            GatewayIdentityProviderAudience.issuer == issuer,
            GatewayIdentityProviderAudience.audience.in_(list(audiences)),
        )
        if exclude_provider_id is not None:
            query = query.where(
                GatewayIdentityProviderAudience.provider_id != exclude_provider_id
            )
        return sorted(set(db.execute(query).scalars()))

    def create_for_account(
        self,
        db: Session,
        *,
        account_id: Any,
        audiences: list[str],
        fields: dict[str, Any],
        commit: bool = True,
    ) -> GatewayIdentityProvider:
        """Create a provider and its audience rows.

        Raises:
            sqlalchemy.exc.IntegrityError: ``(issuer, audience)`` or the
                binding key is already taken.
        """
        provider = GatewayIdentityProvider(
            account_id=account_id,
            **{k: v for k, v in fields.items() if k in UPDATABLE_FIELDS},
        )
        provider.audience_rows = [
            GatewayIdentityProviderAudience(issuer=provider.issuer, audience=aud)
            for aud in sorted(set(audiences))
        ]
        db.add(provider)
        db.flush()
        if commit:
            db.commit()
            db.refresh(provider)
        return provider

    def update_provider(
        self,
        db: Session,
        *,
        provider: GatewayIdentityProvider,
        fields: dict[str, Any],
        audiences: Optional[list[str]] = None,
        commit: bool = True,
    ) -> GatewayIdentityProvider:
        """Apply ``fields`` and optionally replace the audiences.

        The denormalised ``issuer`` on audience rows follows the provider.
        """
        for key, value in fields.items():
            if key in UPDATABLE_FIELDS:
                setattr(provider, key, value)
        if audiences is not None:
            wanted = sorted(set(audiences))
            # Flush the deletes first so a kept audience can be re-inserted
            # without tripping the unique constraint.
            provider.audience_rows = []
            db.flush()
            provider.audience_rows = [
                GatewayIdentityProviderAudience(issuer=provider.issuer, audience=aud)
                for aud in wanted
            ]
        else:
            for row in provider.audience_rows:
                row.issuer = provider.issuer
        db.add(provider)
        db.flush()
        if commit:
            db.commit()
            db.refresh(provider)
        return provider

    def delete_provider(
        self, db: Session, *, provider: GatewayIdentityProvider, commit: bool = True
    ) -> None:
        """Delete a provider and its audiences. Subjects stay with the key."""
        db.delete(provider)
        db.flush()
        if commit:
            db.commit()
