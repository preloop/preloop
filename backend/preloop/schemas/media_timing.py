"""Typed recording timing and server-resolved timeline mappings."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class MediaStepOffset(BaseModel):
    """An exact producer-reported position in the media timebase."""

    model_config = ConfigDict(extra="forbid")
    source_step_id: str = Field(min_length=1, max_length=200)
    offset_ms: int = Field(ge=0, strict=True)


class MediaTiming(BaseModel):
    """Wall clock at media t=0, optionally supplemented by exact offsets."""

    model_config = ConfigDict(extra="forbid")
    started_at: AwareDatetime
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    clock: Literal["agent", "server"]
    step_offsets: list[MediaStepOffset] | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def unique_steps(self) -> "MediaTiming":
        """Ambiguous duplicate step identities cannot define an exact seek."""
        ids = [step.source_step_id for step in self.step_offsets or []]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source_step_id")
        return self


class MediaSyncMapping(BaseModel):
    """One timeline row resolved to a recording position."""

    activity_id: str
    activity_type: str
    api_usage_id: str | None = None
    offset_ms: int
    rule: Literal["exact", "occurred_at", "server_skew"]
    approximate: bool


class RecordingSync(BaseModel):
    """Independent mapping for one recording, including unavailable artifacts."""

    artifact_id: str
    skew_ms: int
    mappings: list[MediaSyncMapping]
    unmapped_activity_ids: list[str]


class MediaSyncOut(BaseModel):
    """Session mappings computed once for the console and evidence export."""

    recordings: list[RecordingSync]
