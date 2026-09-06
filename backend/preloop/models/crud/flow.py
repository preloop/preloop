from typing import List, Optional, Union
from uuid import UUID

from sqlalchemy import cast, String
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from .base import CRUDBase


class CRUDFlow(CRUDBase[models.Flow]):
    """CRUD operations for Flow model."""

    def __init__(self):
        """Initialize with the Flow model."""
        super().__init__(model=models.Flow)

    def get(
        self,
        db: Session,
        id: Union[str, UUID],
        account_id: Union[str, UUID, None] = None,
        *,
        refresh: bool = False,
    ) -> Optional[models.Flow]:
        """
        Retrieve a flow by its ID.

        Args:
            db: The database session.
            id: The ID of the flow to retrieve (string or UUID).
            account_id: The ID of the account associated with the flow (string or UUID). Optional.

        Returns:
            The flow object if found, otherwise None.
        """
        # Convert to string if UUID
        id_str = str(id) if isinstance(id, UUID) else id

        query = db.query(self.model).filter(cast(self.model.id, String) == id_str)
        if refresh:
            query = query.populate_existing().options(joinedload(self.model.ai_model))

        if account_id:
            account_id_str = (
                str(account_id) if isinstance(account_id, UUID) else account_id
            )
            query = query.filter(cast(self.model.account_id, String) == account_id_str)

        return query.first()

    def get_by_account(
        self,
        db: Session,
        account_id: Union[str, UUID],
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.Flow]:
        """
        Retrieve flows for a specific account with pagination.
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        query = db.query(self.model).filter(
            cast(self.model.account_id, String) == account_id_str
        )
        return query.offset(skip).limit(limit).all()

    def get_global_presets(
        self,
        db: Session,
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.Flow]:
        """
        Retrieve global flow presets (presets with no account_id).

        Global presets are system-wide templates that are available to all accounts.
        """
        query = db.query(self.model).filter(
            self.model.is_preset,
            self.model.account_id.is_(None),
        )
        return query.offset(skip).limit(limit).all()

    def get_presets_for_account(
        self,
        db: Session,
        account_id: Union[str, UUID],
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.Flow]:
        """
        Retrieve flow presets available to an account.

        Returns global presets (account_id=None) plus account-specific presets.
        """
        from sqlalchemy import or_

        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        query = db.query(self.model).filter(
            self.model.is_preset,
            or_(
                self.model.account_id.is_(None),
                cast(self.model.account_id, String) == account_id_str,
            ),
        )
        return query.offset(skip).limit(limit).all()

    def get_by_name_and_account(
        self,
        db: Session,
        name: str,
        account_id: Optional[Union[str, UUID]] = None,
    ) -> Optional[models.Flow]:
        """
        Retrieve a flow by name within an account scope.

        Args:
            db: Database session
            name: Flow name to search for
            account_id: Account ID (None for global presets)

        Returns:
            Flow if found, None otherwise
        """
        query = db.query(self.model).filter(self.model.name == name)
        if account_id is None:
            query = query.filter(self.model.account_id.is_(None))
        else:
            account_id_str = (
                str(account_id) if isinstance(account_id, UUID) else account_id
            )
            query = query.filter(cast(self.model.account_id, String) == account_id_str)
        return query.first()

    def get_global_preset_by_name(
        self,
        db: Session,
        name: str,
    ) -> Optional[models.Flow]:
        """
        Retrieve a global preset by name.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.name == name,
                self.model.is_preset,
                self.model.account_id.is_(None),
            )
            .first()
        )

    def get_by_source_preset(
        self,
        db: Session,
        account_id: Union[str, UUID],
        source_preset_id: Union[str, UUID],
    ) -> Optional[models.Flow]:
        """Return the account flow cloned from ``source_preset_id``.

        Enabled flows win, then the oldest ``created_at``. A disabled clone
        is still returned so callers can refuse a second create.
        """
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id
        preset_id_str = (
            str(source_preset_id)
            if isinstance(source_preset_id, UUID)
            else source_preset_id
        )
        return (
            db.query(self.model)
            .filter(
                cast(self.model.account_id, String) == account_id_str,
                cast(self.model.source_preset_id, String) == preset_id_str,
                self.model.is_preset.is_(False),
            )
            .order_by(self.model.is_enabled.desc(), self.model.created_at.asc())
            .first()
        )

    def get_by_trigger(
        self,
        db: Session,
        *,
        event_source: str,
        event_type: str,
        project_id: Optional[str] = None,
        account_id: Optional[Union[str, UUID]] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.Flow]:
        """
        Retrieve flows that match a specific trigger event.

        Matching rules:
        - trigger_event_source: Must match exactly, OR be null (meaning "any source")
        - trigger_event_types: Event type must be in the array
        - trigger_project_ids: Project must match (if specified):
          - trigger_project_ids is null/empty (any project in org), OR
          - trigger_project_ids array contains project_id
        """
        from sqlalchemy import func, or_

        # Event type matching: event_type must be in trigger_event_types array
        event_type_match = self.model.trigger_event_types.any(event_type)

        # Project matching: NULL or empty array both mean "any project"
        wildcard_project = or_(
            self.model.trigger_project_ids.is_(None),
            func.coalesce(func.array_length(self.model.trigger_project_ids, 1), 0) == 0,
        )
        if project_id:
            project_match = or_(
                # Flow accepts any project (no project filter or empty array)
                wildcard_project,
                # project_id is in trigger_project_ids array
                self.model.trigger_project_ids.any(project_id),
            )
        else:
            # No project_id provided - match flows with no project filter
            project_match = wildcard_project

        query = db.query(self.model).filter(
            # Source matches exactly OR flow accepts any source (null)
            or_(
                self.model.trigger_event_source == event_source,
                self.model.trigger_event_source.is_(None),
            ),
            event_type_match,
            project_match,
        )
        if account_id:
            account_id_str = (
                str(account_id) if isinstance(account_id, UUID) else account_id
            )
            query = query.filter(cast(self.model.account_id, String) == account_id_str)
        return query.offset(skip).limit(limit).all()

    def get_scheduled(self, db: Session) -> List[models.Flow]:
        """
        Retrieve all flows with an active schedule (cron) trigger.

        Returns enabled, non-preset flows with
        ``trigger_event_source == 'schedule'`` and a schedule_config set.
        Used by the scheduler to reconcile cron jobs.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.trigger_event_source == "schedule",
                self.model.is_enabled.is_(True),
                self.model.is_preset.is_(False),
                self.model.schedule_config.isnot(None),
            )
            .all()
        )

    def create(
        self,
        db: Session,
        *,
        flow_in: schemas.FlowCreate,
        account_id: Optional[Union[str, UUID]] = None,
    ) -> models.Flow:
        """
        Create a new flow.

        Args:
            db: The database session.
            flow_in: The data for the new flow.
            account_id: Account ID (string or UUID).

        Returns:
            The created flow object.
        """
        db_flow = self.model(**flow_in.model_dump())
        if account_id:
            db_flow.account_id = (
                str(account_id) if isinstance(account_id, UUID) else account_id
            )
        db.add(db_flow)
        db.commit()
        db.refresh(db_flow)
        return db_flow

    def update(
        self,
        db: Session,
        *,
        db_obj: models.Flow,
        flow_in: schemas.FlowUpdate,
        account_id: Union[str, UUID],
    ) -> models.Flow:
        """
        Update an existing flow.

        Args:
            db: The database session.
            db_obj: The existing flow object to update.
            flow_in: The new data for the flow.
            account_id: Account ID (string or UUID).

        Returns:
            The updated flow object.
        """
        update_data = flow_in.model_dump(exclude_unset=True)
        update_data["account_id"] = (
            str(account_id) if isinstance(account_id, UUID) else account_id
        )
        for field, value in update_data.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def remove(
        self, db: Session, *, id: Union[str, UUID], account_id: Union[str, UUID]
    ) -> Optional[models.Flow]:
        """
        Remove a flow by its ID.

        Args:
            db: The database session.
            id: The ID of the flow to remove (string or UUID).
            account_id: The ID of the account (string or UUID).

        Returns:
            The removed flow object if found and deleted, otherwise None.
        """
        # Convert to string if UUID
        id_str = str(id) if isinstance(id, UUID) else id
        account_id_str = str(account_id) if isinstance(account_id, UUID) else account_id

        db_flow = (
            db.query(self.model)
            .filter(
                cast(self.model.id, String) == id_str,
                cast(self.model.account_id, String) == account_id_str,
            )
            .first()
        )
        if db_flow:
            db.delete(db_flow)
            db.commit()
        return db_flow
