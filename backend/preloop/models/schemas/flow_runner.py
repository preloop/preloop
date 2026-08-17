"""Pydantic schemas for self-hosted flow runners."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RunnerRegisterRequest(BaseModel):
    """Register or resume a runner for the logged-in account."""

    name: Optional[str] = Field(None, max_length=200)
    hostname: Optional[str] = Field(None, max_length=255)
    os: Optional[str] = Field(None, max_length=30)
    arch: Optional[str] = Field(None, max_length=30)
    labels: List[str] = Field(default_factory=list)
    runner_id: Optional[UUID] = None
    instance_id: Optional[UUID] = None


class RunnerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    registered_by_user_id: Optional[UUID] = None
    instance_id: Optional[UUID] = None
    name: str
    hostname: Optional[str] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    status: str
    last_heartbeat: Optional[datetime] = None
    current_execution_id: Optional[UUID] = None
    registered_by_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RunnerRegisterResponse(RunnerResponse):
    token: str


class RunnerFleetSummary(BaseModel):
    runner_count: int
    online_runner_count: int
    last_runner_heartbeat: Optional[str] = None
