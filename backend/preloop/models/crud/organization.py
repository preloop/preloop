"""CRUD operations for Organization model."""

from datetime import datetime
from uuid import UUID
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func  # Import func for lower()

from ..models.organization import Organization
from ..models.tracker import Tracker
from .base import CRUDBase


class CRUDOrganization(CRUDBase[Organization]):
    """CRUD operations for Organization model."""

    def touch_webhook(
        self, db: Session, *, organization_id: UUID, observed_at: datetime
    ) -> None:
        """Record acknowledged webhook delivery in a short standalone phase."""
        db.query(Organization).filter(Organization.id == organization_id).update(
            {Organization.last_webhook_update: observed_at}, synchronize_session=False
        )
        db.commit()

    def get_by_identifier(
        self, db: Session, *, identifier: str, account_id: Optional[str] = None
    ) -> Optional[Organization]:
        """Get organization by unique identifier."""
        query = db.query(Organization).filter(Organization.identifier == identifier)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_by_identifier_and_tracker(
        self, db: Session, *, identifier: str, tracker_id: str
    ) -> Optional[Organization]:
        """Get organization by identifier and tracker ID.

        Args:
            db: Database session
            identifier: Organization's external identifier (e.g., GitHub org ID)
            tracker_id: Tracker UUID

        Returns:
            Organization if found, None otherwise
        """
        return (
            db.query(Organization)
            .filter(
                Organization.identifier == identifier,
                Organization.tracker_id == tracker_id,
            )
            .first()
        )

    def get_by_name(
        self,
        db: Session,
        *,
        name: str,
        tracker_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Organization]:
        """
        Get organization by name with optional tracker filter.
        """
        query = db.query(Organization).filter(
            func.lower(Organization.name) == func.lower(name)
        )

        if tracker_id:
            query = query.filter(Organization.tracker_id == tracker_id)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.first()

    def count(self, db: Session, **filters) -> int:
        """Count total number of organizations, with optional filtering."""
        query = db.query(Organization)
        if "account_id" in filters:
            account_id = filters.pop("account_id")
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        for key, value in filters.items():
            if hasattr(Organization, key):
                query = query.filter(getattr(Organization, key) == value)
        return query.count()

    def get_for_tracker(
        self,
        db: Session,
        *,
        tracker_id: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Organization]:
        """Get organizations for a tracker."""
        query = db.query(Organization).filter(Organization.tracker_id == tracker_id)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_active(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Organization]:
        """Get active organizations."""
        query = db.query(Organization).filter(Organization.is_active.is_(True))
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_for_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[Organization]:
        """Get organizations for an account."""
        return (
            db.query(Organization)
            .join(Tracker)
            .filter(Tracker.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def deactivate(self, db: Session, *, id: str) -> Optional[Organization]:
        """Deactivate an organization."""
        organization = self.get(db, id=id)
        if organization:
            organization.is_active = False
            db.add(organization)
            db.commit()
            db.refresh(organization)
        return organization

    def get_with_tracker(
        self, db: Session, *, id: str, account_id: Optional[str] = None
    ) -> Optional[Organization]:
        """Get organization by ID with tracker eagerly loaded."""
        query = (
            db.query(Organization)
            .options(joinedload(Organization.tracker))
            .filter(Organization.id == id)
        )
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_for_trackers(
        self,
        db: Session,
        *,
        tracker_ids: List[str],
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> tuple[List[Organization], int]:
        """
        Get organizations for multiple trackers with pagination.

        Returns:
            Tuple of (organizations list, total count)
        """
        query = db.query(Organization).filter(Organization.tracker_id.in_(tracker_ids))
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        total = query.count()
        organizations = query.offset(skip).limit(limit).all()

        return organizations, total
