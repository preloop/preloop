"""CRUD operations for Issue model."""

import uuid as uuid_module
from datetime import datetime, timezone  # Import timezone
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.issue import Issue
from ..models.project import Project
from ..models.tracker import Tracker
from .base import CRUDBase
from .issue_compliance_result import issue_compliance_result


class CRUDIssue(CRUDBase[Issue]):
    """CRUD operations for Issue model."""

    def create_with_external(
        self, db: Session, *, obj_in: Dict, sync_to_tracker: bool = True
    ) -> Issue:
        """Create issue, optionally syncing with external tracker."""
        issue = self.create(db, obj_in=obj_in)

        if sync_to_tracker and issue.tracker_id:
            # Placeholder for logic to sync issue to external tracker
            # Update external_id and external_url after sync
            pass

        return issue

    def get_by_title(
        self,
        db: Session,
        *,
        title: str,
        project_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        tracker_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """
        Get issue by title with optional project, organization, and tracker filters.
        """
        query = db.query(Issue).filter(Issue.title == title)

        if project_id:
            query = query.filter(Issue.project_id == project_id)

        if organization_id:
            query = query.join(Project, Issue.project_id == Project.id).filter(
                Project.organization_id == organization_id
            )

        if tracker_id:
            query = query.filter(Issue.tracker_id == tracker_id)

        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.first()

    def get_by_key(
        self,
        db: Session,
        *,
        key: str,
        project_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """Get issue by its unique key."""
        query = db.query(Issue).filter(Issue.key == key)
        if project_id:
            query = query.filter(Issue.project_id == project_id)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_by_key_postfix(
        self,
        db: Session,
        *,
        key_postfix: str,
        project_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """Get issue by its unique key postfix."""
        query = db.query(Issue).filter(Issue.key.endswith(key_postfix))
        if project_id:
            query = query.filter(Issue.project_id == project_id)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_by_external_id(
        self,
        db: Session,
        *,
        project_id: str,
        external_id: str,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """Get issue by its external ID and project ID."""
        query = db.query(Issue).filter(
            Issue.project_id == project_id, Issue.external_id == str(external_id)
        )
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_by_external_url(
        self,
        db: Session,
        *,
        external_url: str,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """Get issue by its external URL."""
        query = db.query(Issue).filter(Issue.external_url == str(external_url))
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_for_project(
        self,
        db: Session,
        *,
        project_id: str,
        status: Optional[str] = None,
        issue_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Issue]:
        """Get issues for a project with optional filters."""
        query = db.query(Issue).filter(Issue.project_id == project_id)

        if status:
            query = query.filter(Issue.status == status)
        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.order_by(Issue.created_at.desc()).offset(skip).limit(limit).all()

    def get_issue_counts_per_project(
        self,
        db: Session,
        *,
        project_ids: Optional[List[str]] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, int]]:
        """
        Get the number of issues for each project.
        """
        query = db.query(Issue.project_id, func.count(Issue.id))

        if project_ids is not None:
            if not project_ids:
                return {}
            query = query.filter(Issue.project_id.in_(project_ids))

        if account_id:
            query = query.join(Tracker).filter(
                Tracker.account_id == account_id,
                Tracker.is_active,
                ~Tracker.is_deleted,
            )

        result = query.group_by(Issue.project_id).all()
        return {project_id: {"total": count} for project_id, count in result}

    def get_issue_count(self, db: Session, *, account_id: str) -> int:
        """Get the total number of issues for an account from non-deleted trackers."""
        count = (
            db.query(func.count(Issue.id))
            .join(Tracker, Issue.tracker_id == Tracker.id)
            .filter(
                Tracker.account_id == account_id,
                Tracker.is_active,
                ~Tracker.is_deleted,
            )
            .scalar()
        )
        return count or 0

    def get_for_tracker(
        self,
        db: Session,
        *,
        tracker_id: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Issue]:
        """Get issues for a tracker."""
        query = db.query(Issue).filter(Issue.tracker_id == tracker_id)
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.order_by(Issue.created_at.desc()).offset(skip).limit(limit).all()

    def get_for_trackers(
        self,
        db: Session,
        *,
        tracker_ids: List[str],
        account_id: Optional[str] = None,
    ):
        """Get issues query for multiple trackers. Returns a query object for further filtering."""
        query = db.query(Issue).filter(Issue.tracker_id.in_(tracker_ids))
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query

    def get_by_external_id_or_key_in_trackers(
        self,
        db: Session,
        *,
        external_id: str,
        key: str,
        tracker_ids: List[str],
        project_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """Get issue by external ID or key across multiple trackers."""
        from sqlalchemy import or_

        query = db.query(Issue).filter(
            Issue.tracker_id.in_(tracker_ids),
            or_(
                Issue.external_id == external_id,
                Issue.key == key,
            ),
        )

        if project_id:
            query = query.filter(Issue.project_id == project_id)

        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.first()

    def find_by_flexible_identifier(
        self,
        db: Session,
        *,
        identifier: str,
        tracker_ids: List[str],
        project_id: Optional[str] = None,
        alternative_keys: Optional[List[str]] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Issue]:
        """
        Find issue by flexible identifier matching external_id, key, or id.

        Args:
            identifier: Main identifier to search for
            tracker_ids: List of tracker IDs to search within
            project_id: Optional project ID to filter by
            alternative_keys: Optional list of alternative key formats to try
            account_id: Optional account ID for authorization

        Returns:
            First matching issue or None
        """
        from sqlalchemy import or_

        # Build list of conditions to check
        conditions = [
            Issue.external_id == identifier,
            Issue.key == identifier,
        ]
        # Only add id condition for valid UUIDs to avoid PostgreSQL cast errors
        try:
            uuid_module.UUID(identifier)
            conditions.append(Issue.id == identifier)
        except (ValueError, TypeError):
            # Identifier is not a UUID; skip id equality to avoid PostgreSQL cast errors.
            pass
        if alternative_keys:
            for alt_key in alternative_keys:
                conditions.append(Issue.key == alt_key)

        query = db.query(Issue).filter(
            Issue.tracker_id.in_(tracker_ids), or_(*conditions)
        )

        if project_id:
            query = query.filter(Issue.project_id == project_id)

        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.order_by(Issue.last_updated_external.desc()).first()

    def sync_from_external(
        self, db: Session, *, tracker_id: str, external_id: str
    ) -> Optional[Issue]:
        """Sync issue from external tracker by ID."""
        # Placeholder for logic to fetch issue details from external tracker
        # and update or create local issue
        return None

    def update(self, db: Session, *, db_obj: Issue, obj_in: Dict) -> Optional[Issue]:
        """Update issue and optionally sync to tracker."""
        retval = super().update(db, db_obj=db_obj, obj_in=obj_in)
        issue_compliance_result.delete_by_issue_id(db, issue_id=db_obj.id)
        return retval

    def update_status(
        self, db: Session, *, id: str, status: str, sync_to_tracker: bool = True
    ) -> Optional[Issue]:
        """Update issue status and optionally sync to tracker."""
        issue = self.get(db, id=id)
        if issue:
            issue.status = status

            if sync_to_tracker and issue.external_id:
                # Placeholder for logic to sync status to external tracker
                pass

            db.add(issue)
            db.commit()
            db.refresh(issue)
        return issue

    def assign_parent(
        self, db: Session, *, issue_id: str, parent_id: str
    ) -> Optional[Issue]:
        """Assign a parent to an issue."""
        if issue_id == parent_id:
            return None  # An issue cannot be its own parent

        issue = self.get(db, id=issue_id)
        parent_issue = self.get(db, id=parent_id)

        if issue and parent_issue:
            issue.parent_id = parent_id
            db.add(issue)
            db.commit()
            db.refresh(issue)
            return issue
        return None

    def get_children(
        self, db: Session, *, issue_id: str, skip: int = 0, limit: int = 100
    ) -> List[Issue]:
        """Get child issues for a given issue."""
        return (
            db.query(self.model)
            .filter(self.model.parent_id == issue_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def update_last_synced(self, db: Session, *, id: str) -> Optional[Issue]:
        """Update last_synced timestamp."""
        issue = self.get(db, id=id)
        if issue:
            issue.last_synced = datetime.now(timezone.utc)
            db.add(issue)
            db.commit()
            db.refresh(issue)
        return issue

    def get_with_full_hierarchy(
        self, db: Session, *, id: str, account_id: Optional[str] = None
    ) -> Optional[Issue]:
        """Get issue by ID with project, organization, and tracker eagerly loaded."""
        from sqlalchemy.orm import joinedload
        from ..models.organization import Organization

        query = (
            db.query(Issue)
            .options(
                joinedload(Issue.project)
                .joinedload(Project.organization)
                .joinedload(Organization.tracker)
            )
            .filter(Issue.id == id)
        )
        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)
        return query.first()

    def get_for_project_with_embeddings(
        self,
        db: Session,
        *,
        project_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Issue]:
        """Get issues for a project with embeddings eagerly loaded."""
        from sqlalchemy.orm import selectinload

        query = (
            db.query(Issue)
            .options(selectinload(Issue.embeddings))
            .filter(Issue.project_id == project_id)
        )

        if status and status != "all":
            query = query.filter(Issue.status == status)

        if account_id:
            query = query.join(Tracker).filter(Tracker.account_id == account_id)

        return query.limit(limit).all()
