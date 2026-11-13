"""What the control plane posted for one read-only review execution.

One row per execution. It is the idempotency record of the platform-posted
review: the row is claimed before any provider write, each provider write is
recorded as soon as the provider confirmed it, and a later attempt never
repeats a write the row already holds. See
``docs/architecture/platform-posted-review.md``.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

#: A worker holds the claim and may be writing to the provider.
STATE_PUBLISHING = "publishing"
#: Every write the flow asks for is on the provider and was read back.
STATE_POSTED = "posted"
#: The rules decided that nothing is posted for this execution.
STATE_SKIPPED = "skipped"
#: An attempt ended without completing; ``reason`` says why.
STATE_FAILED = "failed"


class PlatformReviewPublication(Base):
    """Claim, progress and outcome of one execution's posted review."""

    __tablename__ = "platform_review_publication"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", name="uq_platform_review_publication_execution"
        ),
    )

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    execution_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        index=True,
    )
    #: ``publishing``, ``posted``, ``skipped`` or ``failed``.
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Stable machine reason for ``skipped`` and ``failed``.
    reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Whether a later automatic attempt may succeed.
    retryable: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Random value placed in the posted review so a retry recognises its own.
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True
    )
    #: ``owner/repo`` of the execution's own binding, lower case.
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Pull request number of the execution's own binding.
    pull_request: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Reviewed commit of the execution's own binding.
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``posted`` or ``skipped`` once the review step was decided.
    review_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: ``comment``, ``approve`` or ``request_changes``.
    review_action: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    review_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: ``posted`` or ``skipped`` once the commit status step was decided.
    status_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: ``success``, ``failure`` or ``error`` as set on the commit.
    status_value: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    status_context: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
