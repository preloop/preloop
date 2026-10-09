"""Immutable values carried between gateway database phases and provider I/O.

JSON settings are stored as text and decoded on access. This preserves the
existing dictionary read API without sharing mutable nested state. Inference
credentials are resolved separately; a model snapshot never holds a secret row,
encrypted credential, refresh token, or legacy API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any
from uuid import UUID

from preloop.models import models


@dataclass(frozen=True)
class GatewayUserSnapshot:
    """Authenticated identity, without a user row or password hash."""

    id: UUID
    account_id: UUID


@dataclass(frozen=True)
class GatewayApiKeySnapshot:
    """Attribution and governance identity, without the bearer key/hash."""

    id: UUID
    account_id: UUID
    user_id: UUID
    name: str
    context_json: str
    scopes: tuple[Any, ...] = ()

    @property
    def context_data(self) -> dict[str, Any]:
        """Return an independent value copy of credential context."""
        return json.loads(self.context_json)


@dataclass(frozen=True)
class GatewayOAuthSnapshot:
    """OAuth authentication marker; inference needs no OAuth MCP secret."""

    id: UUID


@dataclass(frozen=True)
class GatewayCodexCredentials:
    """Only the access token and account header needed for Codex inference."""

    value: str = field(repr=False)
    account_id: str

    @property
    def payload(self) -> dict[str, str]:
        """Expose required headers without retaining a refresh credential."""
        return {"account_id": self.account_id}


@dataclass(frozen=True)
class GatewayModelSnapshot:
    """Authorized model identity and configuration, with no ORM relationships."""

    id: UUID
    account_id: UUID | None
    name: str
    provider_name: str
    model_identifier: str
    api_endpoint: str | None
    parameters_json: str
    metadata_json: str
    credential_type: str | None
    uses_ambient_credentials: bool
    has_api_key: bool
    is_principal_bound_oauth: bool
    supports_server_side_generation: bool
    is_default: bool
    created_at: datetime | None

    @property
    def model_parameters(self) -> dict[str, Any] | None:
        """Return independent provider settings."""
        return json.loads(self.parameters_json)

    @property
    def meta_data(self) -> dict[str, Any] | None:
        """Return independent model metadata."""
        return json.loads(self.metadata_json)

    # Runtime/pricing helpers also accept models without a gateway alias. They
    # may inspect credential fields, but must not resolve secrets from context.
    api_key = None
    credentials_secret = None

    @classmethod
    def from_model(cls, model: models.AIModel) -> GatewayModelSnapshot:
        """Copy only inference configuration while the preparing worker owns DB."""
        if isinstance(model, cls):
            return model
        return cls(
            id=model.id,
            account_id=model.account_id,
            name=model.name,
            provider_name=model.provider_name,
            model_identifier=model.model_identifier,
            api_endpoint=model.api_endpoint,
            parameters_json=json.dumps(model.model_parameters),
            metadata_json=json.dumps(model.meta_data),
            credential_type=model.credential_type,
            uses_ambient_credentials=model.uses_ambient_credentials,
            has_api_key=model.has_api_key,
            is_principal_bound_oauth=model.is_principal_bound_oauth,
            supports_server_side_generation=model.supports_server_side_generation,
            is_default=model.is_default,
            created_at=model.created_at,
        )


GatewayModel = models.AIModel | GatewayModelSnapshot
