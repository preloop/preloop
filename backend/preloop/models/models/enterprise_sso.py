"""Per-account enterprise single sign-on configuration.

These tables hold what console sign-in through a customer's identity provider
needs: the IdP connections (OIDC or SAML), the email domains an account has
proven it owns, the account's enforcement policy, IdP group mappings, SCIM
bearer tokens (hashed) and a replay guard for single-use protocol ids.

The processing (sign-in, provisioning, the admin API) lives in the EE
``enterprise_sso`` plugin; the models are core so the schema is the same on
every edition.

Database-level invariants:

* A domain is verified by at most one account
  (``uq_account_verified_domain_verified``, partial on ``verified_at``).
* Neither a connection's default role nor a group mapping can be ``owner``.
* A replay guard id is accepted once per connection.

Secrets (OIDC client secret, SAML SP signing key) are never stored here; the
rows point at ``secret_reference`` rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

SSO_PROTOCOL_OIDC = "oidc"
SSO_PROTOCOL_SAML = "saml"
SSO_PROTOCOLS = (SSO_PROTOCOL_OIDC, SSO_PROTOCOL_SAML)
DEFAULT_SSO_ROLE = "member"
DEFAULT_OIDC_SCOPES = ["openid", "email", "profile"]
#: Role that SSO configuration may never grant.
SSO_FORBIDDEN_ROLE = "owner"


class AccountSsoConnection(Base):
    """One identity provider an account signs its members in with.

    Attributes:
        account_id: Owning account; the connection only ever provisions or
            signs in to this account.
        protocol: ``oidc`` or ``saml``.
        name: Admin label, unique per account.
        enabled: Disabled connections are ignored by sign-in.
        oidc_issuer: Exact tenant-specific issuer, ``https://`` only.
        oidc_client_id: OAuth client id registered at the IdP.
        oidc_client_secret_ref_id: ``secret_reference`` holding the secret.
        oidc_scopes: Scopes requested at the authorization endpoint.
        groups_claim: Claim (OIDC) or attribute (SAML) holding groups.
        saml_idp_entity_id: IdP entity id.
        saml_idp_sso_url: IdP single sign-on (redirect or POST) URL.
        saml_idp_metadata_url: Optional IdP metadata URL.
        saml_idp_certs: PEM signing certificates; several allow rotation.
        saml_sp_signing_key_ref_id: ``secret_reference`` holding the SP key.
        saml_want_assertions_signed: Require signed assertions.
        default_role: Role given to JIT-provisioned members; never ``owner``.
        jit_enabled: Create a membership on first sign-in.
        attribute_mapping: Claim or attribute names for email, names.
        created_by_user_id: Admin who created the connection.
        updated_by_user_id: Admin who last changed it.
    """

    __tablename__ = "account_sso_connection"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_account_sso_connection_name"),
        CheckConstraint(
            "protocol IN ('oidc', 'saml')", name="ck_account_sso_connection_protocol"
        ),
        CheckConstraint(
            "default_role <> 'owner'", name="ck_account_sso_connection_not_owner"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    oidc_issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    oidc_client_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    oidc_client_secret_ref_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
    )
    oidc_scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_OIDC_SCOPES)
    )
    groups_claim: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    saml_idp_entity_id: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    saml_idp_sso_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    saml_idp_metadata_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    saml_idp_certs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    saml_sp_signing_key_ref_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
    )
    saml_want_assertions_signed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    default_role: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_SSO_ROLE
    )
    jit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attribute_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        """Return a short representation without configuration values."""
        return f"<AccountSsoConnection {self.id} protocol={self.protocol}>"


class AccountVerifiedDomain(Base):
    """An email domain an account claims, proven by a DNS TXT record.

    Attributes:
        account_id: Claiming account.
        domain: Lowercase domain name.
        challenge_token: Value expected in ``preloop-verification=<token>``.
        verified_at: When the TXT record was last found; null while pending.
        last_checked_at: When verification was last attempted.
    """

    __tablename__ = "account_verified_domain"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "domain", name="uq_account_verified_domain_account"
        ),
        Index(
            "uq_account_verified_domain_verified",
            "domain",
            unique=True,
            postgresql_where=text("verified_at IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    challenge_token: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AccountSsoPolicy(Base):
    """Whether an account requires SSO, and who keeps break-glass access.

    Attributes:
        account_id: The account (one policy row per account).
        enforce: Members on a verified domain must sign in through SSO.
        break_glass_owner_ids: Owner user ids that keep password and passkey
            sign-in; null means every owner.
        session_max_age_seconds: Optional cap on SSO session age.
        updated_by_user_id: Admin who last changed the policy.
    """

    __tablename__ = "account_sso_policy"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    enforce: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    break_glass_owner_ids: Mapped[Optional[list[str]]] = mapped_column(
        JSONB, nullable=True
    )
    session_max_age_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )


class SsoGroupMapping(Base):
    """Maps one IdP group to a team, a role, or both.

    Attributes:
        connection_id: Connection whose groups claim carries ``idp_group``.
        idp_group: Group name or id as the IdP sends it.
        team_id: Team (same account as the connection) members join.
        role: Role members receive; never ``owner``.
    """

    __tablename__ = "sso_group_mapping"
    __table_args__ = (
        CheckConstraint(
            "team_id IS NOT NULL OR role IS NOT NULL",
            name="ck_sso_group_mapping_target",
        ),
        CheckConstraint(
            "role IS NULL OR role <> 'owner'", name="ck_sso_group_mapping_not_owner"
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idp_group: Mapped[str] = mapped_column(String(512), nullable=False)
    team_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("team.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ScimToken(Base):
    """A per-account SCIM bearer token; only its SHA-256 hash is stored.

    Attributes:
        account_id: The only account the token may read or provision.
        name: Admin label.
        token_hash: Hex SHA-256 of the token.
        prefix: First characters of the token, shown to admins.
        created_by_user_id: Admin who created it.
        last_used_at: Last successful SCIM request.
        revoked_at: Set when revoked; revoked tokens never authenticate.
    """

    __tablename__ = "scim_token"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SsoReplayGuard(Base):
    """A consumed single-use id (OIDC state or nonce, SAML assertion id).

    Attributes:
        connection_id: Connection the id was consumed on.
        guard_id: The consumed id.
        expires_at: After this the row may be purged; the protocol message
            it guards is no longer valid anyway.
    """

    __tablename__ = "sso_replay_guard"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "guard_id", name="uq_sso_replay_guard_connection_id"
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    guard_id: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
