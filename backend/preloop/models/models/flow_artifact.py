"""Encrypted hosted recovery artifacts, isolated by account, flow and thread."""

from sqlalchemy import Column, DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from .base import Base


class FlowArtifact(Base):
    """One immutable validated checkpoint with a renewable retention lease."""

    __tablename__ = "flow_artifact"
    account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    flow_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id = Column(String(200), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    manifest = Column(JSONB, nullable=False)
    manifest_sha256 = Column(String(64), nullable=False)
    ciphertext = Column(LargeBinary, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    availability = Column(String(20), nullable=False, default="available")
