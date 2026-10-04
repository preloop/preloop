"""Closed machine subscriptions and immutable server-owned resource binding."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.schemas.webhook_endpoint import WebhookEndpointBase


def _subscription_url(value: str) -> str:
    """Retain the established webhook URL syntax validation."""
    return WebhookEndpointBase._validate_url(value)


COMPLETION_EVENT: Literal["flow.execution.finished"] = "flow.execution.finished"


class CiSubscriptionCreate(BaseModel):
    """One completion event for the authenticated principal's bound flow."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )
    url: str = Field(max_length=2048)
    description: str | None = Field(default=None, max_length=255)
    event_types: list[Literal["flow.execution.finished"]] = Field(
        default_factory=lambda: [COMPLETION_EVENT], min_length=1, max_length=1
    )
    _validate_url = field_validator("url")(_subscription_url)
    active: bool = Field(default=True, strict=True)


class CiSubscriptionUpdate(BaseModel):
    """Mutable receiver settings cannot broaden the immutable subscription."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )
    url: str | None = Field(default=None, max_length=2048)
    description: str | None = Field(default=None, max_length=255)
    _validate_url = field_validator("url")(_subscription_url)
    event_types: list[Literal["flow.execution.finished"]] | None = Field(
        default=None, min_length=1, max_length=1
    )
    active: bool | None = Field(default=None, strict=True)

    @field_validator("url", "event_types", "active", mode="before")
    @classmethod
    def no_explicit_null(cls, value: object) -> object:
        """Only description may be explicitly cleared."""
        if value is None:
            raise ValueError("Subscription settings cannot be null")
        return value


class CiSubscriptionBinding(BaseModel):
    """Immutable principal/resource binding; the initiating key is audit only."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )
    version: Literal[1]
    account_id: UUID
    principal_id: UUID
    key_id: UUID
    project_id: UUID
    flow_id: UUID
    tracker_id: UUID
    repository_identifier: str = Field(min_length=1)
    repository_slug: str = Field(min_length=1)
    tracker_type: Literal["github", "gitlab"]
    tracker_url: str | None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        """Stored version must be a protocol integer."""
        if type(value) is not int:
            raise ValueError("Subscription version must be an integer")
        return value
