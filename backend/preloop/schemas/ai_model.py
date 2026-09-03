"""Pydantic schemas for AIModel."""

import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from preloop.models.models.mixins import TimestampMixin
from preloop.schemas.gateway_usage import (
    GatewayTokenUsage,
    GatewayUsageByDay,
    GatewayUsageBySession,
)


class AIModelBase(BaseModel):
    """Base schema for AIModel, containing common attributes."""

    name: str = Field(..., description="User-defined name for this model configuration")
    description: Optional[str] = Field(None, description="Optional description")
    provider_name: str = Field(..., description="e.g., 'openai', 'anthropic'")
    model_identifier: str = Field(
        ..., description="Standardized identifier, e.g., 'gpt-5.4-turbo'"
    )
    model_kind: Literal["llm", "stt", "tts"] = Field(
        "llm",
        description="Service kind for this model configuration: inference, STT, or TTS",
    )
    api_endpoint: Optional[str] = Field(
        None, description="URL for the model's API, if not standard"
    )
    api_key: Optional[str] = Field(None, description="API key for the model provider")
    credential_type: Optional[str] = Field(
        None,
        description=(
            "Optional inline credential envelope type, e.g. 'oauth_openai_codex'"
        ),
    )
    credential_payload: Optional[Dict] = Field(
        None,
        description=(
            "Optional inline credential payload for non-API-key auth, stored "
            "encrypted when provided"
        ),
    )
    credentials_backend_type: Optional[str] = Field(
        None,
        description="Optional external credential backend type, e.g. 'vault_kv_v2'",
    )
    credentials_external_ref: Optional[str] = Field(
        None,
        description="Optional external secret reference for provider credentials",
    )
    credentials_meta_data: Optional[Dict] = Field(
        None,
        description="Optional metadata for external secret backends, e.g. field/version",
    )
    is_default: bool = Field(
        False, description="Indicates if this is the default model for the account"
    )
    model_parameters: Optional[Dict] = Field(
        None,
        description="Optional, for model-specific parameters like temperature, max_tokens",
    )
    meta_data: Optional[Dict] = Field(
        None, description="Optional, for custom fields, labels, etc."
    )


class AIModelCreate(AIModelBase):
    """Schema for creating a new AIModel entry."""

    credentials_secret_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Reuse an existing SecretReference instead of minting a new one. Used to "
            "create several models that share a single provider key. The secret must "
            "already belong to the caller's account."
        ),
    )

    @model_validator(mode="after")
    def validate_credentials(self):
        has_inline_payload = (
            self.credential_type is not None or self.credential_payload is not None
        )
        has_external = any(
            value is not None
            for value in (
                self.credentials_backend_type,
                self.credentials_external_ref,
                self.credentials_meta_data,
            )
        )
        if self.credentials_secret_id is not None and (
            self.api_key or has_inline_payload or has_external
        ):
            raise ValueError(
                "credentials_secret_id cannot be combined with new credential material"
            )
        if self.api_key and (has_external or has_inline_payload):
            raise ValueError("api_key cannot be combined with other credential fields")
        if has_inline_payload and has_external:
            raise ValueError(
                "credential_type/credential_payload cannot be combined with external credential fields"
            )
        if has_inline_payload and (
            not self.credential_type or self.credential_payload is None
        ):
            raise ValueError(
                "credential_type and credential_payload are required together"
            )
        if has_external and (
            not self.credentials_backend_type or not self.credentials_external_ref
        ):
            raise ValueError(
                "credentials_backend_type and credentials_external_ref are required together"
            )
        return self


class AIModelUpdate(BaseModel):
    """Schema for updating an existing AIModel entry. All fields are optional."""

    name: Optional[str] = None
    description: Optional[str] = None
    provider_name: Optional[str] = None
    model_identifier: Optional[str] = None
    model_kind: Optional[Literal["llm", "stt", "tts"]] = None
    api_endpoint: Optional[str] = None
    api_key: Optional[str] = None
    credential_type: Optional[str] = None
    credential_payload: Optional[Dict] = None
    credentials_backend_type: Optional[str] = None
    credentials_external_ref: Optional[str] = None
    credentials_meta_data: Optional[Dict] = None
    is_default: Optional[bool] = None
    model_parameters: Optional[Dict] = None
    meta_data: Optional[Dict] = None

    @model_validator(mode="after")
    def validate_credentials(self):
        has_inline_payload = (
            self.credential_type is not None or self.credential_payload is not None
        )
        has_external = any(
            value is not None
            for value in (
                self.credentials_backend_type,
                self.credentials_external_ref,
                self.credentials_meta_data,
            )
        )
        if self.api_key and (has_external or has_inline_payload):
            raise ValueError("api_key cannot be combined with other credential fields")
        if has_inline_payload and has_external:
            raise ValueError(
                "credential_type/credential_payload cannot be combined with external credential fields"
            )
        if has_inline_payload and (
            not self.credential_type or self.credential_payload is None
        ):
            raise ValueError(
                "credential_type and credential_payload are required together"
            )
        if has_external and (
            not self.credentials_backend_type or not self.credentials_external_ref
        ):
            raise ValueError(
                "credentials_backend_type and credentials_external_ref are required together"
            )
        return self


class AIModelInDBBase(TimestampMixin, BaseModel):
    """Base schema for AIModel entries as stored in the database."""

    id: uuid.UUID = Field(..., description="Primary key")
    name: str
    description: Optional[str] = None
    provider_name: str
    model_identifier: str
    model_kind: Literal["llm", "stt", "tts"] = "llm"
    api_endpoint: Optional[str] = None
    is_default: bool = False
    model_parameters: Optional[Dict] = None
    meta_data: Optional[Dict] = None
    account_id: Optional[uuid.UUID] = Field(
        None, description="Account this model belongs to"
    )
    credentials_secret_id: Optional[uuid.UUID] = Field(
        None, description="Secret reference ID for model credentials"
    )
    credentials_backend_type: Optional[str] = Field(
        None, description="Backend type used for credential storage"
    )
    credentials_external_ref: Optional[str] = Field(
        None, description="External secret reference when using a non-local backend"
    )
    credential_type: Optional[str] = Field(
        None, description="Logical credential type stored for the model"
    )
    has_api_key: bool = Field(
        False, description="Whether this model has credentials configured"
    )
    supports_server_side_generation: bool = Field(
        False,
        description=(
            "Whether Preloop can run its own generation calls with this model. "
            "False for principal-bound OAuth (Claude Code / Codex subscription) "
            "credentials, which only authorize their owner's interactive traffic "
            "and must never be auto-selected as a default."
        ),
    )

    @field_serializer("account_id")
    def serialize_account_id(self, value: Optional[uuid.UUID]) -> Optional[str]:
        """Serialize UUID to string for JSON response."""
        return str(value) if value is not None else None

    model_config = ConfigDict(from_attributes=True)


class AIModelRead(AIModelInDBBase):
    """Schema for reading AIModel entries, including timestamps."""

    pass


class AIModelCredentialExportResponse(BaseModel):
    """Live subscription-OAuth bundle exported to the owning operator.

    Returned once over the authenticated API so the CLI can restore an
    agent's local login at offboard time (subscription refresh tokens are
    single-use, so the Preloop-held copy is the only live lineage after a
    server-side refresh). Token material must never be logged.
    """

    credential_type: str
    access: str
    refresh: Optional[str] = None
    expires: Optional[int] = None
    account_id: Optional[str] = None


class AvailableModelsRequest(BaseModel):
    """Discovery request for a provider's model catalog.

    The API key is carried in the body rather than the query string on
    purpose: as a query parameter it was written to access logs in plaintext
    and leaked live provider keys.
    """

    api_key: Optional[str] = Field(
        None,
        description=(
            "Provider API key used to list models. Never logged and never "
            "persisted by this endpoint. When omitted, pass ai_model_id so "
            "the server can decrypt the stored key; the stored key is never "
            "returned to the client. A typed key always wins over the stored "
            "one."
        ),
    )
    ai_model_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Existing AI model id. When no typed api_key is sent, the server "
            "decrypts the stored credentials via CRUD and lists live. The "
            "plaintext key is never returned in the response."
        ),
    )
    api_endpoint: Optional[str] = Field(
        None,
        description=(
            "Base URL of an OpenAI-compatible endpoint, e.g. "
            "https://openrouter.ai/api/v1. Required for the "
            "'openai-compatible' and 'custom' providers, which have no fixed "
            "model catalog."
        ),
    )
    model_kind: Literal["llm", "stt", "tts"] = Field(
        "llm", description="Model service kind to fetch"
    )
    aws_access_key_id: Optional[str] = Field(
        None,
        description=(
            "AWS access key id for the bedrock provider. Never logged and "
            "never persisted by this endpoint."
        ),
    )
    aws_secret_access_key: Optional[str] = Field(
        None,
        description=(
            "AWS secret access key for the bedrock provider. Never logged "
            "and never persisted by this endpoint."
        ),
    )
    aws_session_token: Optional[str] = Field(
        None,
        description=(
            "Optional temporary-session token for the bedrock provider. "
            "Never logged and never persisted by this endpoint."
        ),
    )
    aws_region_name: Optional[str] = Field(
        None,
        description=(
            "AWS region for the bedrock provider, e.g. us-east-1. Falls "
            "back to boto3's default region chain when omitted."
        ),
    )

    @field_serializer("api_key")
    def _hide_api_key(self, value: Optional[str]) -> Optional[str]:
        """Keep the key out of any serialized copy of this model (logs, traces)."""
        return "***" if value else value

    @field_serializer("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
    def _hide_aws_secrets(self, value: Optional[str]) -> Optional[str]:
        """Keep AWS credential material out of serialized copies (logs, traces)."""
        return "***" if value else value


class AvailableModelsResponse(BaseModel):
    """A provider's model listing plus the provenance of the result.

    ``source`` reports whether the ids came from the provider's live listing
    endpoint ("live") or an empty fallback ("fallback"). There is no bundled
    picker catalog. ``error`` is a short machine-readable reason drawn from a
    fixed vocabulary (e.g. "timeout", "network", "empty_response",
    "unsupported", "missing_endpoint", "sdk_missing", "missing_key", "auth",
    "unknown", "subscription_oauth") when a live attempt failed or was
    impossible; it never carries raw exception text, which can embed endpoint
    URLs or key material. "subscription_oauth" marks a stored principal-bound
    subscription-OAuth credential (e.g. Claude Code): the server never
    queries the provider with such a token, and the models list is the
    account's own catalog for the provider instead.
    """

    models: List[str] = Field(
        default_factory=list, description="Model identifiers to offer in the picker"
    )
    source: Literal["live", "fallback"] = Field(
        "fallback",
        description=(
            "'live' when the provider's listing endpoint answered; 'fallback' "
            "when an empty list was returned instead of a live catalog"
        ),
    )
    error: Optional[str] = Field(
        None,
        description=(
            "Short safe reason for a fallback after a failed or impossible "
            "live attempt (fixed vocabulary, never raw provider error text). "
            "None for a clean live result."
        ),
    )


class AIModelCatalogSyncRequest(BaseModel):
    """Request body for the account model-catalog sync."""

    provider: Optional[str] = Field(
        None,
        description=(
            "Optional provider filter, e.g. 'anthropic'. When omitted, every "
            "provider the account has credentialed models for is synced."
        ),
    )
    dry_run: bool = Field(
        False,
        description="Report what would be added without creating any models",
    )


class AIModelCatalogSyncProviderResult(BaseModel):
    """Sync outcome for one provider."""

    provider: str = Field(..., description="Provider name, e.g. 'anthropic'")
    source: Literal["live", "fallback"] = Field(
        "fallback",
        description=(
            "'live' when the provider's listing endpoint answered; "
            "'fallback' when discovery failed or was impossible"
        ),
    )
    error: Optional[str] = Field(
        None,
        description=(
            "Short safe reason when nothing could be discovered (fixed "
            "vocabulary; never raw provider error text)"
        ),
    )
    discovered: int = Field(
        0, description="Total model identifiers the provider listed"
    )
    added: List[str] = Field(
        default_factory=list,
        description="Gateway aliases of newly added catalog models",
    )
    skipped_existing: int = Field(
        0, description="Discovered identifiers already present in the catalog"
    )
    note: Optional[str] = Field(
        None, description="Human-readable context for skips and errors"
    )


class AIModelCatalogSyncResponse(BaseModel):
    """Per-provider results of one model-catalog sync run."""

    providers: List[AIModelCatalogSyncProviderResult] = Field(default_factory=list)
    dry_run: bool = False


class AIModelOverviewItem(BaseModel):
    """One row of the Models page: what this model did in the window."""

    ai_model_id: str
    model_name: str
    provider_name: str
    model_identifier: str
    model_alias: Optional[str] = Field(
        None, description="Gateway alias clients call this model by, if configured"
    )
    is_default: bool = False
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage = Field(default_factory=GatewayTokenUsage)
    estimated_cost: float = 0.0
    unpriced_request_count: int = Field(
        0, description="Requests with tokens but no price to apply"
    )
    active_session_count: int = Field(
        0, description="Runtime sessions still open that used this model"
    )
    last_request_at: Optional[datetime] = Field(
        None, description="Timestamp of the most recent gateway request"
    )
    pricing_source: Literal["override", "model_config", "catalog", "none"] = Field(
        "none", description="Where this model's effective price comes from"
    )


class AIModelsOverviewResponse(BaseModel):
    """Batch answer for the Models page and the dashboard inventory tab.

    Replaces one request per model per panel with a single response, so
    opening the page costs a fixed number of queries no matter how many
    models an account has configured.
    """

    period_start: datetime
    period_end: datetime
    models: List[AIModelOverviewItem] = Field(default_factory=list)


class AIModelGatewayUsageSummaryResponse(BaseModel):
    """Gateway usage summary for one durable AI model."""

    ai_model_id: str
    model_name: str
    provider_name: str
    model_identifier: str
    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)
