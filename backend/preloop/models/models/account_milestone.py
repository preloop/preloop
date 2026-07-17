"""Account milestone model: idempotent activation records for funnels."""

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base


class AccountMilestone(Base):
    """First-time activation milestones per account.

    One row per (account, milestone), written idempotently. Milestones:
    ``first_agent``, ``first_ai_model``, ``first_tracker``,
    ``first_mcp_server``, ``first_flow``, and the derived ``activated``
    (first agent or first flow — the point an account gets real value).

    Rows are materialized by the EE growth plugin scanning existing product
    tables — OSS deployments have the table but nothing populates it.
    """

    __tablename__ = "account_milestone"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "milestone", name="uq_account_milestone_account_milestone"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    milestone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment=(
            "first_agent | first_ai_model | first_tracker | first_mcp_server "
            "| first_flow | activated"
        ),
    )
    achieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<AccountMilestone {self.milestone} account={str(self.account_id)[:8]}>"
