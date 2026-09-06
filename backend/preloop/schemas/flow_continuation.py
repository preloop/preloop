"""Explicit adoption contract for one previously published implementation."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

RecoveryMode = Literal["native_resume", "published_branch_handoff"]


class ContinuationPreview(BaseModel):
    execution_id: UUID
    flow_id: UUID
    pr_url: str
    branch: str
    head_sha: str
    feedback_enabled: bool
    artifact_upload_enabled: bool
    feedback_readable: bool = False
    feedback_blocked_reason: str | None = None
    native_resume_available: bool
    existing_thread_id: UUID | None = None
    existing_thread_state: str | None = None
    allowed_recovery_modes: list[RecoveryMode]
    warnings: list[str]


class ContinuationAdoptRequest(BaseModel):
    recovery_mode: RecoveryMode
    expected_head_sha: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    acknowledge_fresh_conversation: bool = False


class ContinuationAdoptResponse(BaseModel):
    thread_id: UUID
    state: str
    pr_url: str
    recovery_mode: RecoveryMode
