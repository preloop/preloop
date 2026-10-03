"""CRUD operations for IssueDuplicate model."""

from typing import List, Optional, Dict, Any
from datetime import datetime, UTC

from sqlalchemy.orm import Query, Session
from sqlalchemy import or_

from preloop.models.crud.base import CRUDBase
from ..models.issue_duplicate import IssueDuplicate
from ..models.issue import Issue
from ..models.tracker import Tracker


class CRUDIssueDuplicate(CRUDBase[IssueDuplicate]):
    """CRUD for IssueDuplicate model."""

    def _scope_to_account(self, query: Query, account_id: Any) -> Query:
        """Scope through the first issue's tracker, like the other lookups."""
        return (
            query.join(Issue, self.model.issue1_id == Issue.id)
            .join(Tracker, Issue.tracker_id == Tracker.id)
            .filter(Tracker.account_id == account_id)
        )

    def create(self, db: Session, *, obj_in: Dict[str, Any]) -> IssueDuplicate:
        """Create a new issue duplicate."""
        db_obj_data = obj_in.copy()

        id1 = db_obj_data.pop("issue1_id")
        id2 = db_obj_data.pop("issue2_id")

        db_obj_data["issue1_id"] = min(id1, id2)
        db_obj_data["issue2_id"] = max(id1, id2)

        db_obj = self.model(**db_obj_data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def update_resolution(
        self,
        db: Session,
        *,
        db_obj: IssueDuplicate,
        resolution: str,
        resolution_reason: Optional[str] = None,
        resulting_issue1_id: Optional[str] = None,
        resulting_issue2_id: Optional[str] = None,
    ) -> IssueDuplicate:
        """Update the resolution of an issue duplicate."""
        db_obj.resolution = resolution
        db_obj.resolution_at = datetime.now(UTC)
        db_obj.resolution_reason = resolution_reason
        db_obj.resulting_issue1_id = resulting_issue1_id
        db_obj.resulting_issue2_id = resulting_issue2_id
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_by_issue_ids(
        self,
        db: Session,
        *,
        issue1_id: int,
        issue2_id: int,
        account_id: Optional[str] = None,
    ) -> Optional[IssueDuplicate]:
        """Get an IssueDuplicate entry by the two issue IDs, order-agnostic."""
        id_a = min(issue1_id, issue2_id)
        id_b = max(issue1_id, issue2_id)
        query = db.query(self.model).filter(
            self.model.issue1_id == id_a, self.model.issue2_id == id_b
        )
        if account_id:
            query = (
                query.join(Issue, self.model.issue1_id == Issue.id)
                .join(Tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.first()

    def get_all_for_issue(
        self, db: Session, *, issue_id: int, account_id: Optional[str] = None
    ) -> List[IssueDuplicate]:
        """Get all duplicates for a given issue."""
        query = db.query(self.model).filter(
            (self.model.issue1_id == issue_id) | (self.model.issue2_id == issue_id)
        )
        if account_id:
            query = (
                query.join(Issue, self.model.issue1_id == Issue.id)
                .join(Tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.all()

    def remove_by_issue_id(self, db: Session, *, issue_id: str) -> None:
        """Remove all duplicate entries associated with a given issue ID."""
        db.query(self.model).filter(
            or_(self.model.issue1_id == issue_id, self.model.issue2_id == issue_id)
        ).delete(synchronize_session=False)
        db.commit()


crud_issue_duplicate = CRUDIssueDuplicate(IssueDuplicate)
