"""Schemas for retention settings, legal holds and period exports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.models.models.legal_hold import HOLD_RESOURCE_TYPES
from preloop.services.legal_hold import MAX_REASON_CHARS, MIN_REASON_CHARS
from preloop.services.retention_policy import RECORD_CLASSES


class RetentionClassRead(BaseModel):
    """Resolved retention for one record class."""

    record_class: str
    label: str
    days: int
    source: str = Field(
        ..., description="'account' when this account set it, 'default' otherwise"
    )
    floored: bool = Field(
        False, description="True when the stored value was raised to meet the floor"
    )


class RetentionSettingsRead(BaseModel):
    """Everything the console needs to render the retention panel."""

    floor_days: int = Field(
        ..., description="No record class can be set below this many days"
    )
    default_days: int = Field(
        ..., description="Applied to any class this account has not set"
    )
    max_days: int
    classes: List[RetentionClassRead]
    purge_enabled: bool = Field(
        ...,
        description=(
            "False when this deployment never deletes. Retention is then a "
            "stated policy that nothing enforces."
        ),
    )
    purge_dry_run: bool
    purge_window_utc: Optional[str] = Field(
        None, description="Off-peak UTC hour window the purge runs in, e.g. '1-5'"
    )
    evidence_payload_hours: int = Field(
        ...,
        description=(
            "How long encrypted evidence payloads are kept. Separate from the "
            "'evidence' record class, which governs the record row."
        ),
    )


class RetentionSettingsUpdate(BaseModel):
    """Full desired state. An omitted class falls back to the default."""

    classes: Dict[str, Optional[int]] = Field(
        default_factory=dict,
        description=(
            "Record class to days. null clears the account's setting for that "
            f"class. Known classes: {', '.join(RECORD_CLASSES)}"
        ),
    )

    @field_validator("classes")
    @classmethod
    def _known_classes(
        cls, value: Dict[str, Optional[int]]
    ) -> Dict[str, Optional[int]]:
        """Refuse an unknown class instead of dropping it silently."""
        unknown = sorted(set(value) - set(RECORD_CLASSES))
        if unknown:
            raise ValueError(
                f"unknown record classes: {', '.join(unknown)}. "
                f"Known: {', '.join(RECORD_CLASSES)}"
            )
        return value


class LegalHoldCreate(BaseModel):
    """Place a hold on one execution, approval or evidence pack."""

    resource_type: str = Field(
        ..., description=f"One of {', '.join(HOLD_RESOURCE_TYPES)}"
    )
    resource_id: str = Field(..., max_length=255)
    reason: str = Field(
        ...,
        min_length=MIN_REASON_CHARS,
        max_length=MAX_REASON_CHARS,
        description="Why this record is frozen. Written to the audit log.",
    )

    @field_validator("resource_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in HOLD_RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {', '.join(HOLD_RESOURCE_TYPES)}"
            )
        return value


class LegalHoldRelease(BaseModel):
    """Lift a hold. The release reason is recorded too."""

    reason: str = Field(
        ...,
        min_length=MIN_REASON_CHARS,
        max_length=MAX_REASON_CHARS,
        description="Why the hold is being lifted. Written to the audit log.",
    )


class LegalHoldRead(BaseModel):
    """One hold, active or released."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    resource_type: str
    resource_id: str
    reason: str
    placed_by_user_id: Optional[UUID] = None
    placed_at: Optional[datetime] = None
    released_by_user_id: Optional[UUID] = None
    released_at: Optional[datetime] = None
    release_reason: Optional[str] = None
    active: bool
    #: Rows whose enforcement flag the place or release call moved, per table.
    flagged: Optional[Dict[str, int]] = None


class RetentionPurgePreview(BaseModel):
    """What a purge would remove right now, without removing it."""

    account_id: UUID
    purge_enabled: bool
    classes: List[Dict[str, Any]]
    total: int
