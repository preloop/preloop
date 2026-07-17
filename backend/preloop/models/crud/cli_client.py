"""CRUD operations for tracked CLI clients."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.cli_client import CliClient
from .base import CRUDBase

# Columns accepted by get_all_paginated's sort_by parameter ("version" and
# "os" map to the cli_version/os columns).
CLI_CLIENT_SORT_FIELDS = (
    "last_seen",
    "first_seen",
    "version",
    "os",
    "country",
)


class CRUDCliClient(CRUDBase[CliClient]):
    """CRUD operations for CLI clients (version-check telemetry)."""

    def get_by_client_id(self, db: Session, *, client_id: UUID) -> Optional[CliClient]:
        """Get a client by its install identifier."""
        return db.query(CliClient).filter(CliClient.client_id == client_id).first()

    def get_all_paginated(
        self,
        db: Session,
        *,
        preloop_url: Optional[str] = None,
        active_within_days: Optional[int] = None,
        sort_by: str = "last_seen",
        sort_order: str = "desc",
        skip: int = 0,
        limit: int = 100,
    ) -> List[CliClient]:
        """List clients with server-side sorting (default: most recent first).

        Args:
            db: Database session
            preloop_url: Filter to clients configured against one server URL.
            active_within_days: Only clients whose last version check is
                within this many days.
            sort_by: One of CLI_CLIENT_SORT_FIELDS. Unknown values fall back
                to last_seen.
            sort_order: "asc" or "desc" (default). NULL sort keys always last.
            skip: Number of records to skip.
            limit: Maximum number of records to return.
        """
        query = db.query(CliClient)
        if preloop_url:
            query = query.filter(CliClient.preloop_url == preloop_url)
        if active_within_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=active_within_days)
            query = query.filter(CliClient.last_seen >= cutoff)

        if sort_by not in CLI_CLIENT_SORT_FIELDS:
            sort_by = "last_seen"
        sort_col = (
            CliClient.cli_version
            if sort_by == "version"
            else getattr(CliClient, sort_by)
        )
        ordering = sort_col.asc() if sort_order == "asc" else sort_col.desc()
        if sort_by in ("os", "country"):  # nullable sort columns
            ordering = ordering.nulls_last()
        # Stable tiebreaker so pagination never duplicates/skips rows.
        return query.order_by(ordering, CliClient.id).offset(skip).limit(limit).all()

    def get_with_coordinates(self, db: Session) -> List[CliClient]:
        """Clients that have lat/lon coordinates (for the admin map)."""
        return (
            db.query(CliClient)
            .filter(CliClient.lat.isnot(None), CliClient.lon.isnot(None))
            .all()
        )

    def count_total(self, db: Session) -> int:
        """Total tracked clients."""
        return db.query(CliClient).count()

    def count_active_within(self, db: Session, *, days: int) -> int:
        """Clients that checked in within the last N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return db.query(CliClient).filter(CliClient.last_seen >= cutoff).count()

    def get_version_counts(self, db: Session) -> dict:
        """Client counts grouped by CLI version."""
        rows = (
            db.query(CliClient.cli_version, func.count(CliClient.id))
            .group_by(CliClient.cli_version)
            .all()
        )
        return {version: count for version, count in rows}

    def get_country_counts(self, db: Session) -> dict:
        """Client counts grouped by ISO country code."""
        rows = (
            db.query(CliClient.country_code, func.count(CliClient.id))
            .filter(CliClient.country_code.isnot(None))
            .group_by(CliClient.country_code)
            .all()
        )
        return {code: count for code, count in rows}


crud_cli_client = CRUDCliClient(CliClient)
