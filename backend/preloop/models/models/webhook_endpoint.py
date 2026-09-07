"""Account-level outbound event webhook endpoints and their delivery outbox.

Distinct from :mod:`preloop.models.models.webhook`, which is the *inbound*
tracker/flow trigger side. These two tables are the outbound direction: an
operator registers a URL, Preloop signs and POSTs governance events to it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# ``source`` values. Account endpoints are operator-created and receive the v1
# envelope; approval-workflow endpoints are the compatibility shim for
# ``approval_config.webhook_url`` and receive the historical body.
SOURCE_ACCOUNT = "account"
SOURCE_APPROVAL_WORKFLOW = "approval_workflow"

# ``webhook_delivery.status`` values.
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_DEAD = "dead"


class WebhookEndpoint(Base):
    """One registered outbound target for one account."""

    __tablename__ = "webhook_endpoint"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Fernet ciphertext (preloop.utils.encryption). The plaintext is returned
    # exactly once, by create and rotate.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hint: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Empty list means "every v1 event type".
    event_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SOURCE_ACCOUNT
    )
    approval_workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_workflow.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    circuit_opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_delivery_status: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class WebhookDelivery(Base):
    """One outbox row: one event aimed at one endpoint.

    The row carries the literal body to POST, so the worker never has to
    rebuild a payload from state that may have moved on since the fact
    occurred.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id",
            "event_id",
            "generation",
            name="uq_webhook_delivery_event",
        ),
        Index("ix_webhook_delivery_due", "status", "next_attempt_at"),
        Index("ix_webhook_delivery_account_status", "account_id", "status"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoint.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_PENDING
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
