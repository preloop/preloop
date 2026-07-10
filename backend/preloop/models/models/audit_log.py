"""Audit log model for security-sensitive operations."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime, String

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .user import User


class AuditLog(Base):
    """Audit log model for tracking security-sensitive operations.

    This model records all important security events including:
    - Permission checks (allowed/denied)
    - User authentication events
    - Role assignments and changes
    - Data access and modifications
    - Administrative actions

    Attributes:
        id: The unique identifier for the audit log entry.
        account_id: The account this audit log belongs to.
        user_id: The user who performed the action (nullable for system actions).
        action: The action performed (e.g., 'permission_check', 'role_assigned').
        resource_type: The type of resource affected (e.g., 'issue', 'user', 'team').
        resource_id: The ID of the specific resource affected (nullable).
        status: The result of the action ('success', 'denied', 'failure').
        ip_address: The IP address of the request (nullable).
        user_agent: The user agent string (nullable).
        details: JSON field with additional context (e.g., permission name, old/new values).
        timestamp: When the action occurred.
    """

    __tablename__ = "audit_log"

    # Foreign keys
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Action details
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )
    resource_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # success, denied, failure

    # Request context. Composite index
    # ``ix_audit_log_account_action_status_ts_ip`` covers CLI activity
    # aggregations (``get_cli_activity_stats`` COUNT DISTINCT ip_address).
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Additional details (JSON)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Timestamp
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="audit_logs")
    user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")

    def __repr__(self) -> str:
        """Return a string representation of the audit log entry.

        Returns:
            String representation of the audit log.
        """
        user_info = f"user {self.user_id}" if self.user_id else "system"
        return f"<AuditLog {self.action} by {user_info} at {self.timestamp} - {self.status}>"
