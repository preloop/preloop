"""PolicySnapshot model for storing policy version snapshots.

This model enables version control for policy configurations:
- Create snapshots of the current policy state
- Tag versions for identification (e.g., "production", "v1.0")
- Rollback to previous versions
- Track version history
"""

import uuid
from typing import TYPE_CHECKING, Dict, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from preloop.models.db.encrypted_types import SecretFieldsJSON

from .base import Base

if TYPE_CHECKING:
    from .account import Account


class PolicySnapshot(Base):
    """
    Stores versioned snapshots of policy configurations.

    Each snapshot captures the complete policy state at a point in time,
    enabling version control, rollback, and auditing of policy changes.
    """

    __tablename__ = "policy_snapshot"

    # Ownership
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id"), nullable=False, index=True
    )

    # Version identification
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Snapshot data - complete policy document as JSON
    # MCP credentials and webhook secrets kept for rollback are sealed at rest.
    snapshot_data: Mapped[Dict] = mapped_column(
        SecretFieldsJSON("policy_snapshot", jsonb=True), nullable=False
    )

    # Metadata
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # Summary stats for quick reference
    mcp_servers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policies_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tools_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationships
    account: Mapped["Account"] = relationship(back_populates="policy_snapshots")

    def __repr__(self):
        return (
            f"<PolicySnapshot(id={self.id}, version={self.version_number}, "
            f"tag='{self.tag}', is_active={self.is_active})>"
        )
