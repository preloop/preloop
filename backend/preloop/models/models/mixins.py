from sqlalchemy import Column, DateTime
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declared_attr


class TimestampMixin:
    """Mixin for adding created_at and updated_at timestamps to a model."""

    @declared_attr
    def created_at(self) -> Column[DateTime]:
        return Column(DateTime, default=func.now(), nullable=False)

    @declared_attr
    def updated_at(self) -> Column[DateTime]:
        return Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
