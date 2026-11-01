"""Report email controls, independent from approval notifications."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WeeklyReportPreferences(BaseModel):
    """Effective account and personal delivery switches."""

    account_enabled: bool
    tracking_enabled: bool
    user_enabled: bool
    members_enabled: bool
    member_policy_findings_enabled: bool
    synthesis_enabled: bool = False
    synthesis_model_id: UUID | None = None


class WeeklyReportPreferencesUpdate(BaseModel):
    """Omitted fields preserve the current setting."""

    model_config = ConfigDict(extra="forbid")
    tracking_enabled: bool | None = None
    account_enabled: bool | None = None
    user_enabled: bool | None = None
    members_enabled: bool | None = None
    member_policy_findings_enabled: bool | None = None

    synthesis_enabled: bool | None = None
    synthesis_model_id: UUID | None = None
