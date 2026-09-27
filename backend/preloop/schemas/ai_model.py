"""Pydantic schemas for AIModel."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from preloop.schemas.gateway_usage import (
    GatewayTokenUsage,
    GatewayUsageByDay,
    GatewayUsageBySession,
)

OPENAI_CODEX_OAUTH_TYPE = "oauth_openai_codex"
ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE = "oauth_anthropic_claude_code"

# Keys the credential resolver reads for each subscription-OAuth type. Codex
# needs the ChatGPT account id on every upstream call, and the server refreshes
# its single-use token, so refresh and expiry are mandatory. Claude Code also
# supports long-lived access-only tokens, which the resolver never refreshes.
_OAUTH_REQUIRED_PAYLOAD_KEYS: Dict[str, Tuple[str, ...]] = {
    OPENAI_CODEX_OAUTH_TYPE: ("access", "refresh", "account_id", "expires"),
    ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE: ("access",),
}
_OAUTH_OPTIONAL_PAYLOAD_KEYS: Dict[str, Tuple[str, ...]] = {
    OPENAI_CODEX_OAUTH_TYPE: (),
    ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE: ("refresh", "expires"),
}
# Key names used by the provider tools' own auth files, mapped to the name
# Preloop stores. Sending these is the most common mistake.
_OAUTH_PAYLOAD_KEY_ALIASES: Dict[str, str] = {
    "access_token": "access",
    "refresh_token": "refresh",
    "expires_at": "expires",
}
# 10**12 ms is 2001-09-09. Any smaller positive expiry is almost certainly
# epoch seconds, which the resolver would read as an already-expired token.
_MIN_EPOCH_MILLIS = 10**12
# 10**14 ms is the year 5138. A larger value is epoch micro- or nanoseconds,
# which the resolver would read as a far-future expiry and never refresh.
_MAX_EPOCH_MILLIS = 10**14

CREDENTIAL_PAYLOAD_DESCRIPTION = (
    "Inline credential payload for non-API-key auth, stored encrypted. "
    "Validated per credential_type at write time; an invalid payload returns "
    "422 and nothing is stored. For 'oauth_openai_codex' send "
    "{access, refresh, account_id, expires}: access, refresh and account_id "
    "are non-empty strings and expires is the access-token expiry as an "
    "integer in epoch milliseconds. For 'oauth_anthropic_claude_code' send "
    "{access} plus optional refresh (non-empty string) and expires (epoch "
    "milliseconds). The key names from the tools' own auth files "
    "(access_token, refresh_token, expires_at) are rejected. This is the "
    "same shape POST /ai-models/{model_id}/credentials/export returns."
)


def _oauth_payload_value_error(key: str, value: Any) -> Optional[str]:
    """Return a validation message for one OAuth payload value, or None.

    Args:
        key: Canonical payload key being checked.
        value: The value the caller sent for ``key``.

    Returns:
        A short message naming the key and the problem, or None when valid.
    """
    if key == "expires":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expires must be an integer (epoch milliseconds)"
        if value <= 0:
            return "expires must be a positive integer (epoch milliseconds)"
        if value < _MIN_EPOCH_MILLIS:
            return (
                "expires looks like epoch seconds; send epoch milliseconds "
                "(seconds * 1000)"
            )
        if value > _MAX_EPOCH_MILLIS:
            return (
                "expires is too large for epoch milliseconds (microseconds or "
                "nanoseconds?)"
            )
        return None
    if not isinstance(value, str) or not value.strip():
        return f"{key} must be a non-empty string"
    return None


def validate_credential_payload(
    credential_type: Optional[str], credential_payload: Optional[Dict]
) -> None:
    """Check an inline credential payload against its credential type.

    Only the subscription-OAuth types have a fixed shape. Other types are
    accepted unchanged. Extra keys that are not known aliases are ignored.

    Args:
        credential_type: The ``credential_type`` sent with the payload.
        credential_payload: The ``credential_payload`` dict sent by the caller.

    Raises:
        ValueError: If required keys are missing, a value has the wrong type,
            or a known alias is used instead of the canonical key. The message
            lists every problem found.
    """
    if credential_type not in _OAUTH_REQUIRED_PAYLOAD_KEYS:
        return
    payload = credential_payload or {}
    required = _OAUTH_REQUIRED_PAYLOAD_KEYS[credential_type]
    optional = _OAUTH_OPTIONAL_PAYLOAD_KEYS[credential_type]
    problems: List[str] = []

    missing = [key for key in required if key not in payload]
    if missing:
        problems.append("missing keys: " + ", ".join(missing))
    for alias, expected in _OAUTH_PAYLOAD_KEY_ALIASES.items():
        if alias in payload:
            problems.append(f"unexpected key '{alias}': use '{expected}'")
    for key in (*required, *optional):
        if key not in payload:
            continue
        message = _oauth_payload_value_error(key, payload[key])
        if message:
            problems.append(message)

    if problems:
        raise ValueError(
            f"invalid credential_payload for {credential_type}: " + "; ".join(problems)
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
        None, description=CREDENTIAL_PAYLOAD_DESCRIPTION
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
        if has_inline_payload:
            validate_credential_payload(self.credential_type, self.credential_payload)
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
    credential_payload: Optional[Dict] = Field(
        None, description=CREDENTIAL_PAYLOAD_DESCRIPTION
    )
    credentials_backend_type: Optional[str] = None
    credentials_external_ref: Optional[str] = None
    credentials_meta_data: Optional[Dict] = None
    credentials_secret_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Repoint this model at an existing SecretReference instead of minting a "
            "new one. Used to keep several models on one provider key or OAuth "
            "lineage. The secret must already belong to the caller's account."
        ),
    )
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
        if has_inline_payload:
            validate_credential_payload(self.credential_type, self.credential_payload)
        return self


class AIModelInDBBase(BaseModel):
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
    updated_at: Optional[datetime] = Field(
        None, description="When this AI model row was last updated"
    )
    credentials_status: Optional[str] = Field(
        None, description="Status of the model's credential secret"
    )
    credentials_last_error: Optional[str] = Field(
        None, description="Summary of the last credential refresh error"
    )
    credentials_last_error_code: Optional[str] = Field(
        None, description="Error code from the last credential refresh attempt"
    )
    credentials_last_failed_at: Optional[datetime] = Field(
        None, description="Timestamp of the last failed credential refresh"
    )
    credentials_last_verified_at: Optional[datetime] = Field(
        None, description="Timestamp when credentials were last verified or refreshed"
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


class AIModelAliasFailure(BaseModel):
    """One alias-group of failures, matching how the inbox keys a model."""

    alias: str = Field(
        ...,
        description=(
            "Alias the failing calls carried (the provider name when they "
            "carried none), which is how the console groups gateway failures."
        ),
    )
    last_failure_at: datetime = Field(
        ...,
        description=(
            "Newest failed request for this alias in the window. The "
            "console fingerprints a dismissed attention item with it."
        ),
    )
    failed_requests: int = Field(
        0, description="Failed requests for this alias in the window"
    )
    failed_requests_since: Optional[int] = Field(
        None,
        description=(
            "Failures for this alias newer than this model's failed_since "
            "query parameter. Null when the caller asked for no such moment."
        ),
    )


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
    last_failure_at: Optional[datetime] = Field(
        None,
        description=(
            "Timestamp of the most recent failed gateway request in the "
            "window. The console fingerprints a dismissed 'needs attention' "
            "item with it, so one more failure brings the item back."
        ),
    )
    last_failure_alias: Optional[str] = Field(
        None,
        description=(
            "Alias the most recent failed request carried (the provider name "
            "when it carried none), which is how the console groups gateway "
            "failures."
        ),
    )
    failed_requests_since: Optional[int] = Field(
        None,
        description=(
            "Failures newer than this model's failed_since query parameter. "
            "Null when the caller asked for no such moment."
        ),
    )
    alias_failures: List[AIModelAliasFailure] = Field(
        default_factory=list,
        description=(
            "Per-alias failure groups for this model, one item per inbox "
            "key. The row is Attention if any group is unacknowledged."
        ),
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
    last_failure_at: Optional[datetime] = Field(
        None,
        description=(
            "Timestamp of the most recent failed gateway request in the "
            "window, which the console fingerprints a dismissed 'needs "
            "attention' item with"
        ),
    )
    last_failure_alias: Optional[str] = Field(
        None,
        description=(
            "Alias the most recent failed request carried (the provider name "
            "when it carried none)"
        ),
    )
    failed_requests_since: Optional[int] = Field(
        None,
        description=(
            "Failures newer than the failed_since query parameter. Null when "
            "the caller asked for no such moment."
        ),
    )
    alias_failures: List[AIModelAliasFailure] = Field(
        default_factory=list,
        description=(
            "Per-alias failure groups for this model, one item per inbox "
            "key. The page is Attention if any group is unacknowledged."
        ),
    )
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)
