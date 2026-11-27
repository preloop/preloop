"""One session selection contract shared by the console and Talk."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionFindRequest(BaseModel):
    """Search identity and captured content within the same list filters."""

    model_config = ConfigDict(extra="forbid")
    session_ids: list[UUID] | None = Field(default=None, max_length=500)
    query: str = Field(default="", max_length=512)
    agent: str | None = None
    agent_kind: str | None = None
    session_source_type: str | None = None
    status: Literal["all", "active", "ended"] = "all"
    start_date: datetime | None = None
    end_date: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    sort: Literal["last_activity", "started", "cost", "requests"] = "last_activity"
    has_artifacts: str | None = None
    workspace_id: str | None = Field(default=None, max_length=256)

    @field_validator("start_date", "end_date")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous naive bounds at the HTTP boundary."""
        if value is not None and value.utcoffset() is None:
            raise ValueError("date bounds must include a timezone offset")
        return value
