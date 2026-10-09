"""Gateway subjects: people seen behind a trusted upstream gateway key."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GatewaySubject(Base):
    """A developer identified by a trusted upstream gateway's identity headers.

    A Claude apps gateway forwarding to Preloop with
    ``forward_user_identity: true`` names each developer by the IdP ``sub``
    (``x-claude-gateway-user-id``) and, when the IdP supplies one, an email.
    The subject is keyed on ``sub`` and scoped to the upstream API key,
    because ``sub`` is only unique per IdP and one key serves one gateway.

    A subject is never a login and grants nothing. ``linked_user_id`` is set
    only when the email matches an existing member of the key's account, so
    that member's ``user`` budgets and attribution also apply.

    Attributes:
        account_id: Account of the upstream key.
        api_key_id: The trusted upstream key the subject was seen on.
        external_subject: IdP ``sub`` forwarded by the gateway.
        email: Display email forwarded by the gateway, when present.
        linked_user_id: Matching account member, when the email matches one.
        first_seen_at: First request time (UTC).
        last_seen_at: Most recent request time (UTC), refreshed coarsely.
    """

    __tablename__ = "gateway_subject"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "api_key_id",
            "external_subject",
            name="uq_gateway_subject_account_key_subject",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_key.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    linked_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        """Return a short representation without the email."""
        return f"<GatewaySubject {self.id} key={self.api_key_id}>"
