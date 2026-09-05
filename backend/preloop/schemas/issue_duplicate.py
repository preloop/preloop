"""Issue Duplicate schemas for request and response validation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Dict
from uuid import UUID
from pydantic import BaseModel, ConfigDict, field_serializer


class IssueDuplicate(BaseModel):
    """Base schema for IssueDuplicate."""

    issue1_id: UUID  # Changed from str to UUID for validation
    issue2_id: UUID  # Changed from str to UUID for validation

    # AI model's decision
    decision: str
    decision_at: Optional[datetime] = None
    reason: Optional[str] = None
    suggestion: Optional[str] = None

    # User's resolution
    resolution: Optional[str] = None
    resolution_at: Optional[datetime] = None
    resolution_reason: Optional[str] = None
    resulting_issue1_id: Optional[UUID] = None  # Changed from str to UUID
    resulting_issue2_id: Optional[UUID] = None  # Changed from str to UUID
    ai_model_id: Optional[UUID] = None
    ai_model_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "issue1_id",
        "issue2_id",
        "resulting_issue1_id",
        "resulting_issue2_id",
        "ai_model_id",
    )
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class IssueDuplicateUpdate(BaseModel):
    """Schema for updating an IssueDuplicate."""

    resolution: Optional[str] = None
    resolution_at: Optional[datetime] = None
    resolution_reason: Optional[str] = None


class IssueDuplicateSuggestionRequest(BaseModel):
    """Schema for suggesting a resolution for an IssueDuplicate."""

    issue1_id: UUID
    issue2_id: UUID
    resolution: str
    resolution_reason: Optional[str] = None
    resulting_issue1_id: Optional[UUID] = None
    resulting_issue2_id: Optional[UUID] = None

    @field_serializer(
        "issue1_id", "issue2_id", "resulting_issue1_id", "resulting_issue2_id"
    )
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class IssueDuplicateSuggestionResponse(BaseModel):
    merged_title: Optional[str] = None
    merged_description: Optional[str] = None
    deconflicted_title1: Optional[str] = None
    deconflicted_description1: Optional[str] = None
    deconflicted_title2: Optional[str] = None
    deconflicted_description2: Optional[str] = None
    explanation: str


class IssueDuplicateResolutionRequest(BaseModel):
    issue1_id: UUID
    issue2_id: UUID
    resolution: str
    resolution_reason: Optional[str] = None
    resulting_issue_1_title: Optional[str] = None
    resulting_issue_1_description: Optional[str] = None
    resulting_issue_2_title: Optional[str] = None
    resulting_issue_2_description: Optional[str] = None

    @field_serializer("issue1_id", "issue2_id")
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class IssueDuplicateResolutionResponse(BaseModel):
    issue1_id: UUID
    issue2_id: UUID
    resolution: str

    @field_serializer("issue1_id", "issue2_id")
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class IssueDuplicateProjectStats(BaseModel):
    project_id: UUID
    project_name: str
    total: int
    duplicates: int

    @field_serializer("project_id")
    def serialize_uuid(self, value: UUID) -> str:
        """Serialize UUID field to string."""
        return str(value)


class IssueDuplicateStats(BaseModel):
    projects: Dict[str, IssueDuplicateProjectStats]


class IssueDuplicateAiStatus(BaseModel):
    """Whether the account has a default AI model for duplicate verdicts."""

    configured: bool
    model_name: Optional[str] = None


class IssueDuplicateAiErrorDetail(BaseModel):
    """Machine-readable error body for issue-duplicates AI endpoints.

    ``detail`` on ``GET /issue-duplicates/check`` and ``POST /ai-suggestion``
    is this object (not a string). ``no_default_ai_model`` is HTTP 422;
    ``ai_model_error`` is HTTP 500.
    """

    code: Literal["no_default_ai_model", "ai_model_error"]
    message: str


class IssueDuplicateAiError(BaseModel):
    """HTTP 422 or 500 envelope: FastAPI wraps the detail object."""

    detail: IssueDuplicateAiErrorDetail
