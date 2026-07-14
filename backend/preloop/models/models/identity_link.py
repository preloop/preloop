"""Identity link model: correlates analytics principals with users/accounts."""

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base


class IdentityLink(Base):
    """One edge in the cross-device identity graph.

    A *principal* is any analytics identity: a web visitor id, a browser
    fingerprint, a CLI install (client_id), a mobile device id, or an OSS
    instance uuid. A link records that the principal was observed acting as
    a given user/account (e.g. logged in over the same session).

    Rows are materialized by the EE growth plugin from co-occurrence in the
    ``event`` table and check-in records — OSS deployments have the table
    but nothing populates it.
    """

    __tablename__ = "identity_link"
    __table_args__ = (
        UniqueConstraint(
            "principal_type",
            "principal_id",
            "user_id",
            name="uq_identity_link_principal_user",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    principal_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="visitor | fingerprint | cli_client | mobile_device | oss_instance",
    )
    principal_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="The principal's identifier (uuid/fingerprint/device id as string)",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment="ws_auth | login | register | qr_register | device_register | cli_check_in",
    )
    first_linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<IdentityLink {self.principal_type}:{self.principal_id[:12]} "
            f"user={str(self.user_id)[:8] if self.user_id else 'none'}>"
        )
