"""Schemas for account-level outbound webhook endpoints and deliveries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.services.event_webhooks.events import EVENT_TYPES_V1

# The secret is returned exactly once, in the create response. It is stored
# encrypted and there is no read endpoint for it: an operator who loses it
# rotates rather than recovers.
SECRET_ONCE_NOTE = "Shown once. Store it now; it cannot be read back."


class WebhookEndpointBase(BaseModel):
    """Fields an operator sets."""

    url: str = Field(..., max_length=2048, description="HTTPS URL to POST events to")
    description: Optional[str] = Field(None, max_length=255)
    event_types: List[str] = Field(
        default_factory=list,
        description="Event types to receive. Empty means every v1 event.",
    )
    active: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Reject anything that is not an absolute http(s) URL."""
        candidate = (value or "").strip()
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return candidate

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: List[str]) -> List[str]:
        """Reject unknown event types rather than silently never firing."""
        unknown = sorted(set(value) - set(EVENT_TYPES_V1))
        if unknown:
            raise ValueError(
                f"unknown event types: {', '.join(unknown)}. "
                f"Known: {', '.join(EVENT_TYPES_V1)}"
            )
        # Deduplicated and ordered so the stored filter reads the same as the
        # catalogue regardless of the order the console sent it in.
        return [name for name in EVENT_TYPES_V1 if name in set(value)]


class WebhookEndpointCreate(WebhookEndpointBase):
    """Create request. The secret is generated server side."""


class WebhookEndpointUpdate(BaseModel):
    """Partial update. Omitted fields are left alone."""

    url: Optional[str] = Field(None, max_length=2048)
    description: Optional[str] = Field(None, max_length=255)
    event_types: Optional[List[str]] = None
    active: Optional[bool] = None

    _validate_url = field_validator("url")(WebhookEndpointBase._validate_url.__func__)
    _validate_event_types = field_validator("event_types")(
        WebhookEndpointBase._validate_event_types.__func__
    )


class WebhookEndpointRead(BaseModel):
    """Endpoint as returned by the API. Never carries the secret."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: str
    description: Optional[str] = None
    event_types: List[str] = Field(default_factory=list)
    active: bool
    source: str
    secret_hint: Optional[str] = None
    created_by_user_id: Optional[UUID] = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_delivery_status: Optional[str] = None
    last_delivery_at: Optional[datetime] = None
    last_response_code: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None


class WebhookEndpointCreated(WebhookEndpointRead):
    """Create response, the one and only time the secret is returned."""

    secret: str
    secret_note: str = SECRET_ONCE_NOTE


class WebhookDeliveryRead(BaseModel):
    """One delivery attempt row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    endpoint_id: UUID
    event_id: UUID
    event_type: str
    status: str
    attempt_count: int
    generation: int
    occurred_at: datetime
    next_attempt_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    response_status: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None


class WebhookTestSendResult(BaseModel):
    """Result of queueing a test event."""

    event_id: Optional[UUID] = None
    delivery_ids: List[UUID] = Field(default_factory=list)
    queued: int = 0


class WebhookReplayResult(BaseModel):
    """Result of replaying one event id."""

    event_id: UUID
    delivery_ids: List[UUID] = Field(default_factory=list)
    queued: int = 0


class WebhookEventTypeInfo(BaseModel):
    """One entry in the subscribable event catalogue."""

    name: str
    description: str


class WebhookCatalogue(BaseModel):
    """What a console needs to render the create form."""

    version: str
    event_types: List[WebhookEventTypeInfo]
    signature_header: str
    tolerance_seconds: int
    max_attempts: int
    retry_delays_seconds: List[int]


class WebhookPayloadEcho(BaseModel):
    """Envelope preview, used by docs and the console help text."""

    envelope: dict[str, Any]
