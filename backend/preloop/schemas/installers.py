"""Schemas for public installer download analytics."""

from datetime import datetime

from pydantic import BaseModel, Field


class InstallerVersionStat(BaseModel):
    """Aggregated download count for a requested installer version."""

    version: str
    count: int


class InstallerDownloadStats(BaseModel):
    """Summary statistics for installer script downloads and CLI activity."""

    audit_enabled: bool
    # Account UUID whose audit log stores installer_download events
    # (the INSTALLER_AUDIT_ACCOUNT_ID setting; superuser-only endpoint).
    audit_account_id: str | None = None
    # When auditing is disabled: the calling admin's own account id, offered
    # as a copy-paste candidate for INSTALLER_AUDIT_ACCOUNT_ID.
    suggested_account_id: str | None = None
    days: int
    total_downloads: int
    downloads_last_24h: int
    unique_ips: int
    cli_downloads: int
    oss_downloads: int
    pinned_downloads: int
    latest_version_downloads: int
    last_download_at: datetime | None = None
    top_versions: list[InstallerVersionStat] = Field(default_factory=list)
    # CLI activity (daily check-ins from installed CLIs, deduplicated
    # client-side to at most one per machine per day)
    cli_checkins_total: int = 0
    cli_active_unique_ips: int = 0
    cli_active_last_24h: int = 0
    cli_last_seen_at: datetime | None = None
    top_cli_versions: list[InstallerVersionStat] = Field(default_factory=list)
