"""Account kill-switch (halt) state, one row per account and scope.

The kill switch is the account-scoped emergency stop: while a scope is
active, the platform refuses the traffic class it covers (model-gateway
requests, MCP tool calls, new flow executions). Rows are keyed by
``(account_id, scope)`` so the three surfaces can be halted and re-enabled
independently — staged recovery is a first-class requirement, not an
afterthought.

Each row doubles as the audit record for the scope's current state: who
activated it and when, why, and who lifted it. Transitions additionally
land in ``AuditLog`` via the kill-switch endpoints (see
``preloop.api.endpoints.kill_switch``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

#: Halt scope covering model-gateway requests (all ingress protocols).
HALT_SCOPE_GATEWAY = "gateway"
#: Halt scope covering MCP tool calls.
HALT_SCOPE_TOOLS = "tools"
#: Halt scope covering new flow executions.
HALT_SCOPE_FLOWS = "flows"

#: Every scope a full halt covers, in the order surfaces are re-enabled
#: during staged recovery (verify cheap read-only traffic first, autonomous
#: flows last).
HALT_SCOPES = (HALT_SCOPE_GATEWAY, HALT_SCOPE_TOOLS, HALT_SCOPE_FLOWS)


class AccountHalt(Base):
    """Kill-switch state for one account and one traffic scope."""

    __tablename__ = "account_halt"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Which traffic class is halted: gateway | tools | flows.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)

    # Whether the halt is currently in force for this scope.
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Who activated the halt, when, and why (audit).
    activated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Who lifted the halt, when (kept so the row remains the audit record
    # after a staged or full re-enable).
    deactivated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    deactivation_reason: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )

    account = relationship("Account")
    activated_by_user = relationship("User", foreign_keys=[activated_by_user_id])
    deactivated_by_user = relationship("User", foreign_keys=[deactivated_by_user_id])

    __table_args__ = (
        UniqueConstraint("account_id", "scope", name="uq_account_halt_account_scope"),
        CheckConstraint(
            "scope IN ('gateway', 'tools', 'flows')",
            name="ck_account_halt_scope",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AccountHalt(account_id={self.account_id}, "
            f"scope={self.scope}, active={self.is_active})>"
        )
