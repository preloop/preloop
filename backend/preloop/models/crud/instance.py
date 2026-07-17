"""CRUD operations for Instance model."""

from typing import List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from .base import CRUDBase
from ..models.instance import Instance

# Columns accepted by get_all_paginated's sort_by parameter.
INSTANCE_SORT_FIELDS = (
    "last_seen",
    "first_seen",
    "version",
    "edition",
    "country",
)


class CRUDInstance(CRUDBase[Instance]):
    """CRUD operations for Instance model."""

    def get_by_uuid(self, db: Session, *, instance_uuid: UUID) -> Optional[Instance]:
        """Get instance by its unique UUID.

        Args:
            db: Database session
            instance_uuid: Unique instance identifier

        Returns:
            Instance if found, None otherwise
        """
        return (
            db.query(Instance).filter(Instance.instance_uuid == instance_uuid).first()
        )

    def get_active(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Instance]:
        """Get active instances.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of active instances
        """
        return (
            db.query(Instance)
            .filter(Instance.is_active == True)  # noqa: E712
            .order_by(Instance.last_seen.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_by_edition(
        self,
        db: Session,
        *,
        edition: str,
        active_only: bool = False,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Instance]:
        """Get instances by edition.

        Args:
            db: Database session
            edition: Edition to filter by ('oss' or 'enterprise')
            active_only: Whether to only return active instances
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of instances
        """
        query = db.query(Instance).filter(Instance.edition == edition)
        if active_only:
            query = query.filter(Instance.is_active == True)  # noqa: E712
        return query.order_by(Instance.last_seen.desc()).offset(skip).limit(limit).all()

    def get_with_coordinates(self, db: Session) -> List[Instance]:
        """Get instances that have lat/lon coordinates.

        Args:
            db: Database session

        Returns:
            List of instances with coordinates
        """
        return (
            db.query(Instance)
            .filter(Instance.lat.isnot(None), Instance.lon.isnot(None))
            .all()
        )

    def get_all_paginated(
        self,
        db: Session,
        *,
        active_only: bool = False,
        edition: Optional[str] = None,
        sort_by: str = "last_seen",
        sort_order: str = "desc",
        skip: int = 0,
        limit: int = 100,
    ) -> List[Instance]:
        """Get all instances with optional filters and server-side sorting.

        Args:
            db: Database session
            active_only: Whether to only return active instances
            edition: Edition to filter by
            sort_by: One of INSTANCE_SORT_FIELDS. Unknown values fall back
                to last_seen.
            sort_order: "asc" or "desc" (default). NULL sort keys always last.
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of instances
        """
        query = db.query(Instance)
        if active_only:
            query = query.filter(Instance.is_active == True)  # noqa: E712
        if edition:
            query = query.filter(Instance.edition == edition)

        if sort_by not in INSTANCE_SORT_FIELDS:
            sort_by = "last_seen"
        sort_col = getattr(Instance, sort_by)
        ordering = sort_col.asc() if sort_order == "asc" else sort_col.desc()
        if sort_by == "country":  # only nullable sort column
            ordering = ordering.nulls_last()
        # Stable tiebreaker so pagination never duplicates/skips rows.
        return query.order_by(ordering, Instance.id).offset(skip).limit(limit).all()

    def count_total(self, db: Session) -> int:
        """Count total instances.

        Args:
            db: Database session

        Returns:
            Total count
        """
        return db.query(Instance).count()

    def count_active(self, db: Session) -> int:
        """Count active instances.

        Args:
            db: Database session

        Returns:
            Active instance count
        """
        return db.query(Instance).filter(Instance.is_active == True).count()  # noqa: E712

    def count_by_edition(self, db: Session, *, edition: str) -> int:
        """Count instances by edition.

        Args:
            db: Database session
            edition: Edition to count

        Returns:
            Count of instances with given edition
        """
        return db.query(Instance).filter(Instance.edition == edition).count()

    def get_version_counts(self, db: Session) -> dict:
        """Get instance counts grouped by version.

        Args:
            db: Database session

        Returns:
            Dict mapping version to count
        """
        results = (
            db.query(Instance.version, func.count(Instance.id))
            .group_by(Instance.version)
            .all()
        )
        return {version: count for version, count in results}

    def get_country_counts(self, db: Session) -> dict:
        """Get instance counts grouped by country code.

        Args:
            db: Database session

        Returns:
            Dict mapping country code to count
        """
        results = (
            db.query(Instance.country_code, func.count(Instance.id))
            .filter(Instance.country_code.isnot(None))
            .group_by(Instance.country_code)
            .all()
        )
        return {code: count for code, count in results if code}


crud_instance = CRUDInstance(Instance)
