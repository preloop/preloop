"""Schemas for Anthropic gateway identity providers (#1414)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.models.models.gateway_identity_provider import (
    DEFAULT_ALLOWED_ALGORITHMS,
    DEFAULT_CLOCK_SKEW_SECONDS,
    DEFAULT_MAX_TOKEN_LIFETIME_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
)

#: Kept in step with ``gateway_idp_auth.SUPPORTED_ALGORITHMS``.
_ALGORITHMS = {
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
}


def _issuer(value: str) -> str:
    value = value.strip()
    if not value.startswith("https://") or len(value) <= len("https://"):
        raise ValueError("issuer must be an https:// URL")
    if any(c.isspace() for c in value) or "#" in value or "?" in value:
        raise ValueError("issuer must not contain spaces, a query or a fragment")
    return value


def _audiences(value: list[str]) -> list[str]:
    cleaned = sorted({a.strip() for a in value if a and a.strip()})
    if not cleaned:
        raise ValueError("at least one audience is required")
    if any(len(a) > 512 for a in cleaned):
        raise ValueError("audience too long")
    return cleaned


def _algorithms(value: list[str]) -> list[str]:
    cleaned = sorted(set(value))
    if not cleaned:
        raise ValueError("at least one algorithm is required")
    bad = [a for a in cleaned if a not in _ALGORITHMS]
    if bad:
        raise ValueError(
            f"unsupported algorithms {bad}; none and HMAC are never allowed"
        )
    return cleaned


def _domains(value: list[str]) -> list[str]:
    return sorted({d.strip().lower().lstrip("@") for d in value if d and d.strip()})


class _ProviderFields(BaseModel):
    allowed_email_domains: list[str] = Field(default_factory=list)
    email_claim: str = Field("email", min_length=1, max_length=128)
    require_email_verified: bool = True
    groups_claim: Optional[str] = Field(None, max_length=128)
    allowed_groups: Optional[list[str]] = None
    required_claims: dict[str, Any] = Field(default_factory=dict)
    clock_skew_seconds: int = Field(
        DEFAULT_CLOCK_SKEW_SECONDS, ge=0, le=MAX_CLOCK_SKEW_SECONDS
    )
    max_token_lifetime_seconds: int = Field(
        DEFAULT_MAX_TOKEN_LIFETIME_SECONDS, ge=60, le=30 * 86400
    )
    allowed_algorithms: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_ALGORITHMS)
    )
    allowed_jwks_hosts: list[str] = Field(default_factory=list)
    allow_private_network_issuer: bool = False
    enabled: bool = True


class GatewayIdentityProviderCreate(_ProviderFields):
    """Create a provider. ``api_key_id`` is the binding key."""

    name: str = Field(..., min_length=1, max_length=255)
    issuer: str = Field(..., max_length=512)
    audiences: list[str]
    api_key_id: UUID

    _v_issuer = field_validator("issuer")(_issuer)
    _v_aud = field_validator("audiences")(_audiences)
    _v_alg = field_validator("allowed_algorithms")(_algorithms)
    _v_dom = field_validator("allowed_email_domains")(_domains)


class GatewayIdentityProviderUpdate(BaseModel):
    """Partial update; omitted fields keep their value."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    issuer: Optional[str] = Field(None, max_length=512)
    audiences: Optional[list[str]] = None
    api_key_id: Optional[UUID] = None
    allowed_email_domains: Optional[list[str]] = None
    email_claim: Optional[str] = Field(None, min_length=1, max_length=128)
    require_email_verified: Optional[bool] = None
    groups_claim: Optional[str] = Field(None, max_length=128)
    allowed_groups: Optional[list[str]] = None
    required_claims: Optional[dict[str, Any]] = None
    clock_skew_seconds: Optional[int] = Field(None, ge=0, le=MAX_CLOCK_SKEW_SECONDS)
    max_token_lifetime_seconds: Optional[int] = Field(None, ge=60, le=30 * 86400)
    allowed_algorithms: Optional[list[str]] = None
    allowed_jwks_hosts: Optional[list[str]] = None
    allow_private_network_issuer: Optional[bool] = None
    enabled: Optional[bool] = None

    @field_validator("issuer")
    @classmethod
    def _v_issuer(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _issuer(value)

    @field_validator("audiences")
    @classmethod
    def _v_aud(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return None if value is None else _audiences(value)

    @field_validator("allowed_algorithms")
    @classmethod
    def _v_alg(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return None if value is None else _algorithms(value)

    @field_validator("allowed_email_domains")
    @classmethod
    def _v_dom(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return None if value is None else _domains(value)


class GatewayIdentityProviderRead(_ProviderFields):
    """A configured provider."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    issuer: str
    audiences: list[str]
    api_key_id: UUID
    allow_private_network_issuer_effective: bool = Field(
        False,
        description=(
            "Whether private addresses are actually allowed: the provider flag "
            "and the instance setting gateway_idp_allow_private_issuers."
        ),
    )
    created_at: datetime
    updated_at: datetime


class GatewayIdentityProviderKey(BaseModel):
    """One signing key the issuer publishes."""

    kid: Optional[str] = None
    kty: Optional[str] = None
    alg: Optional[str] = None


class GatewayIdentityProviderTestResult(BaseModel):
    """Outcome of fetching discovery and JWKS; no token is involved."""

    ok: bool
    error: Optional[str] = None
    jwks_uri: Optional[str] = None
    keys: list[GatewayIdentityProviderKey] = Field(default_factory=list)
