"""Time-boxed approval bypasses ("panic switch" for approval/notification fatigue).

An :class:`ApprovalBypass` is a deliberate, *expiring* relaxation of approval
gating for one user, optionally narrowed to a single managed agent.

Two independent modes exist because notification fatigue and approval fatigue
are different problems:

``mute_notifications``
    Approval requests are still created and still **block** the agent. Only the
    outbound notification fan-out (push/email/webhook) is suppressed. The user
    stops being buzzed but keeps full control; they decide from the console or
    the app when they choose to look.

``auto_approve``
    Approval requests are created, recorded, and **immediately auto-approved**
    without notifying anyone. This is the governance bypass — the equivalent of
    ``--dangerously-skip-permissions``. It is the escape hatch for a runaway
    agent, not a normal operating mode.

Safety properties, all deliberate:

* **Always time-boxed.** ``expires_at`` is NOT NULL and is capped by
  :data:`MAX_BYPASS_DURATION`. A bypass cannot be created that never ends, so
  it cannot be silently forgotten — the single largest risk of an indefinite
  "skip permissions" toggle.
* **Always attributable.** ``created_by_user_id`` and ``reason`` record who
  opened the hole and why.
* **Always recorded.** Requests auto-approved under a bypass are still
  persisted as :class:`ApprovalRequest` rows carrying
  ``auto_approved_reason='bypass'`` and ``auto_approval_bypass_id``, so the
  audit trail shows exactly what ran unsupervised and under whose authority.
  Suppressing the *notification* solves the actual pain; suppressing the
  *record* would only destroy evidence.
* **Revocable.** ``revoked_at`` ends a bypass early without deleting the
  history of it having existed.
"""

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .user import User


class ApprovalBypassMode(str):
    """Mode of an approval bypass."""

    #: Suppress notifications only; approvals still block the agent.
    MUTE_NOTIFICATIONS = "mute_notifications"
    #: Auto-approve and suppress notifications. Governance bypass.
    AUTO_APPROVE = "auto_approve"


#: Longest bypass that may be created in a single call. Deliberately short:
#: a bypass is an escape hatch from a runaway agent, not a configuration mode.
#: Re-arming is cheap and forces a conscious re-decision.
MAX_BYPASS_DURATION = timedelta(hours=8)

#: Default when the caller does not specify. One hour is long enough to finish
#: whatever the agent is doing, short enough that forgetting it is not a
#: security incident.
DEFAULT_BYPASS_DURATION = timedelta(hours=1)


class ApprovalBypass(Base):
    """A time-boxed suppression of approval gating and/or notifications."""

    __tablename__ = "approval_bypass"
    __table_args__ = (
        Index(
            "ix_approval_bypass_lookup",
            "account_id",
            "user_id",
            "expires_at",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Account this bypass belongs to",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User whose approval requests are bypassed",
    )

    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Managed agent this bypass is scoped to. NULL means account-wide "
            "for this user (the 'global switch')."
        ),
    )

    mode: Mapped[str] = mapped_column(
        SQLEnum(
            "mute_notifications",
            "auto_approve",
            name="approval_bypass_mode",
        ),
        nullable=False,
        index=True,
        comment="'mute_notifications' (still blocks) or 'auto_approve' (bypass)",
    )

    reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Operator-supplied justification for opening the bypass",
    )

    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        comment="User who created the bypass (for attribution)",
    )

    created_via: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment="Originating surface: console|mobile|watch|api|cli",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
        comment="When the bypass automatically ends. NOT NULL by design.",
    )

    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the bypass was ended early, if it was",
    )

    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who revoked the bypass early",
    )

    #: Number of approval requests auto-approved under this bypass. Maintained
    #: incrementally so the console can show "47 calls ran unsupervised"
    #: without an aggregate query on every render.
    auto_approved_count: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="How many approval requests this bypass auto-approved",
    )

    account: Mapped["Account"] = relationship("Account")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_user_id])

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """Whether this bypass is currently in force.

        Args:
            now: Reference time; defaults to :func:`datetime.utcnow`.

        Returns:
            True when the bypass is neither revoked nor expired.
        """
        reference = now or datetime.utcnow()
        if self.revoked_at is not None:
            return False
        return self.expires_at > reference

    def __repr__(self) -> str:
        """String representation."""
        scope = (
            f"agent={self.managed_agent_id}"
            if self.managed_agent_id
            else "account-wide"
        )
        return (
            f"<ApprovalBypass(id={self.id}, mode={self.mode}, {scope}, "
            f"user={self.user_id}, expires_at={self.expires_at})>"
        )
