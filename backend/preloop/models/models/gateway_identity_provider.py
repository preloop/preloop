"""Customer OpenID Connect providers trusted on the Anthropic gateway.

Claude Desktop can sign users in with the organization's own identity
provider and send the resulting IdP token to the gateway as the bearer
credential. A ``GatewayIdentityProvider`` row tells Preloop which issuer and
audiences to accept for one account, and which Preloop API key (the binding
key) supplies the account, allowed models and per-subject budget for those
requests.

Two invariants are enforced by the database, not only by the API:

* ``(issuer, audience)`` is unique across all accounts
  (``gateway_identity_provider_audience``), so one token can never resolve to
  two accounts, even on a shared multi-tenant issuer.
* ``api_key_id`` is unique, so a binding key serves one provider and the
  ``gateway_subject`` rows it owns are keyed on one issuer's ``sub`` space.
"""

import uuid
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

DEFAULT_ALLOWED_ALGORITHMS = ["RS256", "ES256"]
DEFAULT_CLOCK_SKEW_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_MAX_TOKEN_LIFETIME_SECONDS = 86400


class GatewayIdentityProvider(Base):
    """One trusted OIDC issuer for an account's Anthropic gateway traffic.

    Attributes:
        account_id: Owning account.
        name: Admin label.
        issuer: Exact ``iss`` string; ``https://`` only.
        api_key_id: Binding key (same account) whose models, budgets and
            subjects the provider's requests use.
        allowed_email_domains: Lowercase domains; empty means any.
        email_claim: Claim holding the user's email.
        require_email_verified: Reject when ``email_verified`` is present
            and not true.
        groups_claim: Claim holding the user's groups, when configured.
        allowed_groups: Group allowlist; empty or null means any.
        required_claims: Claim name to exact required value.
        clock_skew_seconds: Leeway for ``exp``, ``nbf`` and ``iat``.
        max_token_lifetime_seconds: Upper bound on ``exp - iat``.
        allowed_algorithms: Asymmetric JWS algorithms accepted.
        allowed_jwks_hosts: Exact extra hosts ``jwks_uri`` may use.
        allow_private_network_issuer: Allow private addresses; honoured only
            when the instance setting ``gateway_idp_allow_private_issuers``
            is also on.
        enabled: Disabled providers are ignored (tokens fall through).
    """

    __tablename__ = "gateway_identity_provider"
    __table_args__ = (
        UniqueConstraint("api_key_id", name="uq_gateway_identity_provider_api_key"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_key.id", ondelete="CASCADE"),
        nullable=False,
    )
    allowed_email_domains: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    email_claim: Mapped[str] = mapped_column(
        String(128), nullable=False, default="email"
    )
    require_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    groups_claim: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    allowed_groups: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    required_claims: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    clock_skew_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CLOCK_SKEW_SECONDS
    )
    max_token_lifetime_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_TOKEN_LIFETIME_SECONDS
    )
    allowed_algorithms: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_ALLOWED_ALGORITHMS)
    )
    allowed_jwks_hosts: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    allow_private_network_issuer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    audience_rows: Mapped[list["GatewayIdentityProviderAudience"]] = relationship(
        "GatewayIdentityProviderAudience",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="GatewayIdentityProviderAudience.audience",
    )

    @property
    def audiences(self) -> list[str]:
        """Configured audiences, sorted."""
        return [row.audience for row in self.audience_rows]

    def __repr__(self) -> str:
        """Return a short representation."""
        return f"<GatewayIdentityProvider {self.id} issuer={self.issuer!r}>"


class GatewayIdentityProviderAudience(Base):
    """One accepted audience of a provider; ``(issuer, audience)`` is global."""

    __tablename__ = "gateway_identity_provider_audience"
    __table_args__ = (
        UniqueConstraint(
            "issuer", "audience", name="uq_gateway_idp_audience_issuer_audience"
        ),
    )

    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gateway_identity_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from the provider so the global unique constraint holds
    # in the database across accounts.
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    audience: Mapped[str] = mapped_column(String(512), nullable=False)
