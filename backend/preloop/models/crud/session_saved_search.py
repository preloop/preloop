"""Persistence for saved session searches.

Every read here binds ``account_id`` in SQL, the same rule the search endpoint
follows: an account bound applied by a serialiser is one refactor away from
not being applied at all. Visibility is part of the same predicate rather than
a filter over the rows that came back, so a private search belonging to
somebody else is never loaded in the first place.

Two facts a saved search carries are written here and nowhere else: the
ranking identity in force when it was saved or last edited, and the run
counters. The counters are the only evidence that saving searches earns its
place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.session_saved_search import (
    FILTER_SCHEMA_VERSION,
    VISIBILITY_ACCOUNT,
    VISIBILITY_PRIVATE,
    SessionSavedSearch,
)
from .base import CRUDBase

#: Columns a caller may change after the fact. Anything outside this set (the
#: author, the account, the counters) is not editable, so an update route
#: cannot be talked into moving a saved search between accounts.
EDITABLE_FIELDS = frozenset(
    {
        "name",
        "query",
        "mode",
        "filters",
        "filters_version",
        "max_snippets_per_session",
        "include_snippet_text",
        "visibility",
        "ranking_identity",
        "shared_at",
    }
)


class SessionSavedSearchNameConflictError(ValueError):
    """This author already has a saved search under that name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a saved search named {name!r} already exists")
        self.name = name


class CRUDSessionSavedSearch(CRUDBase[SessionSavedSearch]):
    """CRUD operations for :class:`SessionSavedSearch`."""

    def create_for_user(
        self,
        db: Session,
        *,
        account_id: Any,
        owner_user_id: Any,
        name: str,
        query: str,
        mode: str,
        filters: Dict[str, Any],
        ranking_identity: str,
        max_snippets_per_session: int,
        include_snippet_text: bool,
        visibility: str = VISIBILITY_PRIVATE,
        filters_version: int = FILTER_SCHEMA_VERSION,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionSavedSearch:
        """Save one named search for one user.

        Args:
            db: Request scoped session.
            account_id: Account the search belongs to.
            owner_user_id: The author.
            name: Label, unique per author inside the account.
            query: Query text, already normalised by the schema.
            mode: Requested search mode.
            filters: Validated filter payload, JSON serialisable.
            ranking_identity: Ranking constants in force right now.
            max_snippets_per_session: Saved snippet preference.
            include_snippet_text: Saved snippet preference.
            visibility: ``private`` or ``account``.
            filters_version: Filter schema the payload was validated against.
            now: Clock override for the share timestamp.
            commit: Commit rather than flush.

        Returns:
            The stored row.

        Raises:
            SessionSavedSearchNameConflictError: The author already used that name.
        """
        if self.name_taken(
            db, account_id=account_id, owner_user_id=owner_user_id, name=name
        ):
            raise SessionSavedSearchNameConflictError(name)
        moment = now or datetime.now(UTC)
        saved = SessionSavedSearch(
            account_id=account_id,
            owner_user_id=owner_user_id,
            name=name,
            query=query,
            mode=mode,
            filters=filters,
            filters_version=filters_version,
            max_snippets_per_session=max_snippets_per_session,
            include_snippet_text=include_snippet_text,
            visibility=visibility,
            shared_at=moment if visibility == VISIBILITY_ACCOUNT else None,
            ranking_identity=ranking_identity,
            run_count=0,
        )
        db.add(saved)
        db.flush()
        if commit:
            db.commit()
            db.refresh(saved)
        return saved

    def name_taken(
        self,
        db: Session,
        *,
        account_id: Any,
        owner_user_id: Any,
        name: str,
        exclude_id: Optional[Any] = None,
    ) -> bool:
        """Whether this author already saved a search under this name."""
        statement = select(func.count(SessionSavedSearch.id)).where(
            SessionSavedSearch.account_id == account_id,
            SessionSavedSearch.owner_user_id == owner_user_id,
            SessionSavedSearch.name == name,
        )
        if exclude_id is not None:
            statement = statement.where(SessionSavedSearch.id != exclude_id)
        return bool(db.execute(statement).scalar_one())

    def get_visible(
        self,
        db: Session,
        *,
        account_id: Any,
        user_id: Any,
        saved_search_id: Any,
    ) -> Optional[SessionSavedSearch]:
        """Return a saved search this caller may see, or ``None``.

        Visible means "mine, whatever its visibility" or "shared with my
        account by somebody else". Both halves of that are in the SQL.
        """
        statement = select(SessionSavedSearch).where(
            SessionSavedSearch.id == saved_search_id,
            SessionSavedSearch.account_id == account_id,
            or_(
                SessionSavedSearch.owner_user_id == user_id,
                SessionSavedSearch.visibility == VISIBILITY_ACCOUNT,
            ),
        )
        return db.execute(statement).scalars().one_or_none()

    def get_owned(
        self,
        db: Session,
        *,
        account_id: Any,
        user_id: Any,
        saved_search_id: Any,
    ) -> Optional[SessionSavedSearch]:
        """Return a saved search this caller wrote, or ``None``."""
        statement = select(SessionSavedSearch).where(
            SessionSavedSearch.id == saved_search_id,
            SessionSavedSearch.account_id == account_id,
            SessionSavedSearch.owner_user_id == user_id,
        )
        return db.execute(statement).scalars().one_or_none()

    def list_visible(
        self,
        db: Session,
        *,
        account_id: Any,
        user_id: Any,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[SessionSavedSearch], int]:
        """Return the page of saved searches this caller may see, and the count.

        Ordered most recently run first, then most recently saved, then by id.
        The last key is there so two searches saved in the same transaction
        still come back in one fixed order, which is what makes paging safe.
        """
        visible = (
            SessionSavedSearch.account_id == account_id,
            or_(
                SessionSavedSearch.owner_user_id == user_id,
                SessionSavedSearch.visibility == VISIBILITY_ACCOUNT,
            ),
        )
        total = int(
            db.execute(
                select(func.count(SessionSavedSearch.id)).where(*visible)
            ).scalar_one()
        )
        rows = (
            db.execute(
                select(SessionSavedSearch)
                .where(*visible)
                .order_by(
                    SessionSavedSearch.last_run_at.desc().nullslast(),
                    SessionSavedSearch.created_at.desc(),
                    SessionSavedSearch.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def update_owned(
        self,
        db: Session,
        *,
        saved: SessionSavedSearch,
        values: Dict[str, Any],
        commit: bool = False,
    ) -> SessionSavedSearch:
        """Apply an edit to a saved search the caller owns.

        Args:
            db: Request scoped session.
            saved: The row, already loaded through an ownership scoped read.
            values: Column values to change; keys outside
                :data:`EDITABLE_FIELDS` are refused rather than ignored.
            commit: Commit rather than flush.

        Returns:
            The updated row.

        Raises:
            ValueError: A key outside the editable set was passed.
            SessionSavedSearchNameConflictError: The new name is already used by
                this author.
        """
        unknown = set(values) - EDITABLE_FIELDS
        if unknown:
            raise ValueError(
                "not editable on a saved search: " + ", ".join(sorted(unknown))
            )
        new_name = values.get("name")
        if new_name is not None and new_name != saved.name:
            if self.name_taken(
                db,
                account_id=saved.account_id,
                owner_user_id=saved.owner_user_id,
                name=new_name,
                exclude_id=saved.id,
            ):
                raise SessionSavedSearchNameConflictError(new_name)
        for key, value in values.items():
            setattr(saved, key, value)
        db.add(saved)
        db.flush()
        if commit:
            db.commit()
            db.refresh(saved)
        return saved

    def delete_owned(
        self, db: Session, *, saved: SessionSavedSearch, commit: bool = False
    ) -> None:
        """Delete a saved search. The sessions it found are untouched."""
        db.delete(saved)
        db.flush()
        if commit:
            db.commit()

    def record_run(
        self,
        db: Session,
        *,
        saved: SessionSavedSearch,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionSavedSearch:
        """Record that a saved search ran, whatever the run returned.

        A run that came back empty, or degraded to keyword because the
        semantic half could not run, still counts: the question was asked.
        """
        saved.last_run_at = now or datetime.now(UTC)
        saved.run_count = (saved.run_count or 0) + 1
        db.add(saved)
        db.flush()
        if commit:
            db.commit()
            db.refresh(saved)
        return saved
