"""Base model class for all ORM models."""

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import field_validator, ValidationInfo

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models.

    Provides:
    - Automatic table name generation
    - Created/updated timestamps
    - Serialization methods
    - UUID primary key convention
    """

    # Generate table name automatically from class name
    @declared_attr.directive
    def __tablename__(self) -> str:
        return self.__name__.lower()

    # Common columns for all models
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, index=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def format_datetime_fields_to_str(
        cls, v: Any, info: ValidationInfo
    ) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, str):
            return v
        raise TypeError(
            f"Field '{info.field_name}' must be a datetime object or a string, got {type(v).__name__}"
        )

    @classmethod
    def generate_id(cls) -> uuid.UUID:
        """Generate a new UUID for the id field."""
        return uuid.uuid4()

    def to_dict(self) -> Dict[str, Any]:
        """Convert model instance to dictionary."""
        result = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value
        return result
