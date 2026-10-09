"""Pydantic schemas for MCP server configuration."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from preloop.models.schemas.grant_introspection import IntrospectionConfig
from preloop.utils.redaction import REDACTED_STRING, _is_sensitive_key

#: auth_config keys that look sensitive by name but hold no secret. They stay
#: readable so the console and CLI can show how a server authenticates.
NON_SECRET_AUTH_CONFIG_KEYS = frozenset(
    {
        "authorization_endpoint",
        "client_id",
        "expires_at",
        "key_name",
        "registration_endpoint",
        "scope",
        "scopes",
        "token_endpoint",
        "token_type",
    }
)


def _is_secret_auth_key(key: Any) -> bool:
    """Return True when an auth_config key holds a secret value."""
    if not isinstance(key, str):
        return False
    if key.lower().strip() in NON_SECRET_AUTH_CONFIG_KEYS:
        return False
    return _is_sensitive_key(key)


def redact_auth_config(auth_config: Any) -> Any:
    """Return a copy of an MCP server auth_config with secret values masked.

    Secret values are replaced with ``REDACTED_STRING`` recursively (so a
    nested ``headers`` dict is covered). Non-secret fields are kept. Every
    read path (API responses, policy version snapshots) must go through this.

    Args:
        auth_config: The stored auth_config, or None.

    Returns:
        A redacted copy, or the input unchanged if it is not a dict or list.
    """
    if isinstance(auth_config, dict):
        return {
            key: (
                REDACTED_STRING
                if _is_secret_auth_key(key) and value not in (None, "")
                else redact_auth_config(value)
            )
            for key, value in auth_config.items()
        }
    if isinstance(auth_config, list):
        return [redact_auth_config(item) for item in auth_config]
    return auth_config


def redact_snapshot_credentials(snapshot_data: Any) -> Any:
    """Mask ``mcp_servers[].auth_config`` secrets in a policy snapshot copy.

    Snapshots store credentials so rollback can restore them. Anything that
    leaves the API (version detail, rollback diff) must use this copy.

    Args:
        snapshot_data: A stored policy snapshot dict.

    Returns:
        A copy with MCP server secrets masked. The input is not modified.
    """
    if not isinstance(snapshot_data, dict):
        return snapshot_data
    servers = snapshot_data.get("mcp_servers")
    if not isinstance(servers, list):
        return snapshot_data
    redacted = dict(snapshot_data)
    redacted["mcp_servers"] = [
        (
            {**server, "auth_config": redact_auth_config(server["auth_config"])}
            if isinstance(server, dict) and server.get("auth_config")
            else server
        )
        for server in servers
    ]
    return redacted


def is_redaction_marker(auth_config: Any) -> bool:
    """Return True for the whole-object marker ``{"redacted": true}``."""
    return isinstance(auth_config, dict) and auth_config.get("redacted") is True


def merge_auth_config(incoming: Any, stored: Any) -> Any:
    """Apply write-only semantics to an incoming auth_config.

    - ``{"redacted": true}`` keeps the stored auth_config as is.
    - A value equal to ``REDACTED_STRING`` keeps the stored value for that key,
      or drops the key when nothing is stored for it.
    - Any other value replaces the stored one.

    Args:
        incoming: auth_config sent by the client.
        stored: auth_config currently stored (None on create).

    Returns:
        The auth_config to persist.
    """
    if is_redaction_marker(incoming):
        return stored
    if not isinstance(incoming, dict):
        return incoming
    stored_dict = stored if isinstance(stored, dict) else {}
    merged: Dict[str, Any] = {}
    for key, value in incoming.items():
        if value == REDACTED_STRING:
            if key in stored_dict:
                merged[key] = stored_dict[key]
            continue
        if isinstance(value, dict) and isinstance(stored_dict.get(key), dict):
            merged[key] = merge_auth_config(value, stored_dict[key])
        else:
            merged[key] = merge_auth_config(value, None)
    return merged


class MCPServerBase(BaseModel):
    """Base schema for MCP server configuration."""

    name: Optional[str] = Field(
        None, description="User-defined name for this MCP server"
    )
    url: Optional[str] = Field(None, description="URL of the external MCP server")
    transport: Optional[str] = Field(
        "http-streaming", description="Transport protocol (default: http-streaming)"
    )
    auth_type: Optional[str] = Field(
        "none",
        description="Authentication type: 'none', 'bearer', 'api_key', 'oauth'",
    )
    auth_config: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "JSON configuration for authentication. Secret values are "
            "write-only: responses return them as '***REDACTED***'. Sending "
            'that marker (or {"redacted": true}) keeps the stored value.'
        ),
    )
    status: Optional[str] = Field(
        "active", description="Server status: 'active', 'error', 'disabled'"
    )
    tool_prefix: Optional[str] = Field(
        None,
        description=(
            "Optional explicit prefix. When set, this server's tools are "
            "exposed as '<prefix>_<tool>' for listing, routing, policy and "
            "configuration. Lowercase [a-z0-9_], at most 32 characters. "
            "Never set automatically. Send null or an empty string to clear."
        ),
    )

    @field_validator("tool_prefix")
    @classmethod
    def _validate_tool_prefix(cls, value: Optional[str]) -> Optional[str]:
        from preloop.services.mcp_tool_collisions import validate_tool_prefix

        return validate_tool_prefix(value)

    @field_validator("auth_config")
    @classmethod
    def validate_introspection(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Validate nested grant configuration without changing write-only secrets."""
        if value and value.get("introspection") is not None:
            IntrospectionConfig.model_validate(value["introspection"])
        return value

    @model_validator(mode="after")
    def introspection_requires_delegated_auth(self) -> "MCPServerBase":
        """A grant check only applies to the bearer or OAuth token we forward."""
        config = self.auth_config or {}
        if config.get("introspection") is None:
            return self
        if self.auth_type not in ("bearer", "oauth"):
            raise ValueError("introspection requires bearer or oauth authentication")
        return self


class MCPServerCreate(MCPServerBase):
    """Schema for creating an MCP server.

    Note: account_id is not included here as it's extracted from
    the authenticated user in the endpoint handler.
    """

    name: str
    url: str


class MCPServerUpdate(MCPServerBase):
    """Schema for updating an MCP server."""

    pass


class MCPServerResponse(MCPServerBase):
    """Schema for MCP server response."""

    id: UUID
    account_id: UUID
    last_scan_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Tool name collisions and invalid tool names for this server: "
            "shadowed tools are hidden from agents because an older server "
            "owns the same name."
        ),
    )

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id", "account_id")
    def serialize_uuids(self, value: UUID) -> str:
        """Serialize UUID fields to strings."""
        return str(value)

    @field_serializer("auth_config")
    def serialize_auth_config(
        self, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Never return stored secrets; mask them on every read."""
        return redact_auth_config(value)
