"""Configuration for introspecting the exact upstream bearer grant."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator


def validate_scope_names(values: list[str]) -> list[str]:
    """Require bounded individual RFC scope names."""
    if any(
        not value or len(value) > 512 or any(c.isspace() for c in value)
        for value in values
    ):
        raise ValueError("required_scopes must contain nonempty individual scope names")
    return list(dict.fromkeys(values))


class IntrospectionConfig(BaseModel):
    """RFC 7662 client configuration stored in MCP server auth_config."""

    model_config = ConfigDict(extra="forbid", strict=True)

    endpoint: HttpUrl
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: SecretStr = Field(min_length=1, max_length=4096)
    client_auth: Literal["basic", "post"] = "post"
    timeout_seconds: float = Field(default=2, ge=0.1, le=30)
    max_cache_ttl_seconds: float = Field(default=60, ge=0, le=300)
    negative_cache_ttl_seconds: float = Field(default=5, ge=0, le=60)
    consent_ref_claim: str = Field(default="consent_ref", min_length=1, max_length=128)
    required_scopes: list[str] = Field(default_factory=list, max_length=128)
    fail_open: bool = False

    @field_validator("endpoint")
    @classmethod
    def secure_endpoint(cls, value: HttpUrl) -> HttpUrl:
        """Credentials require TLS and cannot appear in the endpoint URL."""
        if (
            value.scheme != "https"
            or value.username
            or value.password
            or value.fragment
        ):
            raise ValueError(
                "introspection endpoint must be HTTPS without URL credentials or fragment"
            )
        return value

    @field_validator("required_scopes")
    @classmethod
    def scope_names(cls, values: list[str]) -> list[str]:
        """Require individual RFC scope names rather than a joined scope string."""
        return validate_scope_names(values)


class GrantSample(BaseModel):
    """Synthetic dry-run identity, never an upstream token or client credential."""

    model_config = ConfigDict(extra="forbid", strict=True)

    available: bool = True
    active: bool
    scope: list[str] = Field(default_factory=list, max_length=128)
    sub: str | None = Field(None, max_length=512)
    client_id: str | None = Field(None, max_length=512)
    consent_ref: str | None = Field(None, max_length=512)
    exp: int | None = None
    cached: bool = False

    @field_validator("scope")
    @classmethod
    def bounded_scopes(cls, values: list[str]) -> list[str]:
        """Match the same individual scope bounds as introspected grants."""
        return validate_scope_names(values)
