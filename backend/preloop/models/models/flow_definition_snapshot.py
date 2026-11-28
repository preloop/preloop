"""Redacted definitions selected by executions, shared across runs of a revision."""

from uuid import uuid4

from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class FlowDefinitionSnapshot(Base):
    """One immutable, inspectable definition per selected flow revision."""

    __tablename__ = "flow_definition_snapshot"
    __table_args__ = (UniqueConstraint("flow_id", "revision"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    flow_id = Column(
        UUID(as_uuid=True), ForeignKey("flow.id", ondelete="CASCADE"), nullable=False
    )
    account_id = Column(UUID(as_uuid=True), nullable=True)
    revision = Column(Integer, nullable=False)
    definition = Column(Text, nullable=False)


class ExecutionConfigurationSelection(Base):
    """A definition used at a particular stage of one execution."""

    __tablename__ = "execution_configuration_selection"
    __table_args__ = (UniqueConstraint("execution_id", "stage", "snapshot_id"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage = Column(String(24), nullable=False)
    snapshot_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_definition_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
