"""Versioned restricted CI grants. Scope strings do not confer authority."""

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CiAction(str, Enum):
    """The complete v1 machine action ceiling."""

    TRIGGER = "flow:trigger"
    READ_EXECUTION = "execution:read"
    READ_RESULT = "execution:result:read"
    STOP_EXECUTION = "execution:stop"
    CREATE_SUBSCRIPTION = "subscription:create"
    READ_SUBSCRIPTION = "subscription:read"
    UPDATE_SUBSCRIPTION = "subscription:update"
    DELETE_SUBSCRIPTION = "subscription:delete"
    ROTATE_SUBSCRIPTION_SECRET = "subscription:secret:rotate"


class CiGrant(BaseModel):
    """Exactly one account-local project and its dedicated flow."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )

    version: Literal[1]
    project_id: UUID
    flow_id: UUID
    actions: tuple[CiAction, ...] = Field(min_length=1)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, version: object) -> object:
        """A boolean or coerced numeric value is not a protocol version."""
        if type(version) is not int:
            raise ValueError("CI grant version must be an integer")
        return version

    @field_validator("actions")
    @classmethod
    def unique_actions(cls, actions: tuple[CiAction, ...]) -> tuple[CiAction, ...]:
        """Reject duplicate or empty action lists rather than normalize them."""
        if len(set(actions)) != len(actions):
            raise ValueError("CI actions must be unique")
        return actions
