"""Base CRUD class for all models."""

from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from sqlalchemy.orm import Session

from ..models.base import Base

# Define a generic type variable for models that inherit from Base
ModelType = TypeVar("ModelType", bound=Base)


class CRUDBase(Generic[ModelType]):
    """Base class for CRUD operations on models."""

    def __init__(self, model: Type[ModelType]):
        """Initialize with a model class."""
        self.model = model

    def get(
        self, db: Session, id: Any, *, account_id: Optional[str] = None
    ) -> Optional[ModelType]:
        """Get entity by ID."""
        query = db.query(self.model).filter(self.model.id == id)
        if account_id and hasattr(self.model, "account_id"):
            query = query.filter(self.model.account_id == account_id)
        return query.first()

    def get_multi(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
        **filters,
    ) -> List[ModelType]:
        """Get multiple entities with optional filtering."""
        query = db.query(self.model)
        if account_id and hasattr(self.model, "account_id"):
            query = query.filter(self.model.account_id == account_id)

        for key, value in filters.items():
            if hasattr(self.model, key):
                query = query.filter(getattr(self.model, key) == value)
        return query.offset(skip).limit(limit).all()

    def create(
        self, db: Session, *, obj_in: Dict[str, Any], commit: bool = True
    ) -> ModelType:
        """Create new entity.

        Args:
            db: Database session.
            obj_in: Column values for the new row.
            commit: When False, flush only so callers can batch several
                writes into one atomic transaction and commit themselves.

        Returns:
            The created (and refreshed) model instance.
        """
        # Don't add an ID unless it's missing - let the model handle it with default=uuid.uuid4
        obj_data = dict(obj_in)

        db_obj = self.model(**obj_data)
        db.add(db_obj)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(db_obj)
        return db_obj

    def update(
        self, db: Session, *, db_obj: ModelType, obj_in: Dict[str, Any]
    ) -> ModelType:
        """Update an entity."""
        # Get the set of actual table column names to avoid updating relationships
        table_columns = {column.name for column in db_obj.__table__.columns}

        # Update model attributes from obj_in, but only for actual table columns
        for field, value in obj_in.items():
            if field in table_columns:
                setattr(db_obj, field, value)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def delete(self, db: Session, *, id: Any) -> Optional[ModelType]:
        """Delete an entity by ID."""
        obj = db.query(self.model).filter(self.model.id == id).first()
        if obj:
            db.delete(obj)
            db.commit()
        return obj
