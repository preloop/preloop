"""Redacted extension text an approver reads, stored once per account and hash."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExtensionContent(Base):
    """One distinct extension content of one account, addressed by its hash.

    The row is scoped to the account: two accounts that report the same hash
    hold two rows and never read each other's. ``ciphertext`` is the Fernet
    encryption of a JSON document (file list, redacted text, hook commands);
    the plain columns carry counts only, never text or file names.
    """

    __tablename__ = "extension_content"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "extension_kind",
            "content_hash",
            name="uq_extension_content_account_hash",
        ),
        CheckConstraint(
            "extension_kind IN ('plugin','skill','hook','command')",
            name="ck_extension_content_kind",
        ),
        CheckConstraint(
            "verification IN ('verified','partial','unverified','none')",
            name="ck_extension_content_verification",
        ),
        Index("ix_extension_content_last_reported", "last_reported_at"),
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    extension_kind: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(80))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    #: UTF-8 bytes of stored text, after redaction. Counts against the budget.
    stored_bytes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    entry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    stored_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    withheld_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    redaction_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    #: Set when the device sent no text for the whole extension, with the reason.
    withheld_reason: Mapped[str | None] = mapped_column(String(32))
    #: ``verified``: every stored text matched its hash on receipt.
    #: ``partial``: some did, the rest was redacted on the device.
    #: ``unverified``: all stored text was redacted. ``none``: no text stored.
    verification: Mapped[str] = mapped_column(String(16), server_default="none")
    redacted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    redaction_rules: Mapped[str | None] = mapped_column(String(64))
    collector_version: Mapped[str | None] = mapped_column(String(64))
    uploaded_fingerprint: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    #: Last time a collector reported an extension with this hash. Retention
    #: counts from here, so content nobody reports any more ages out.
    last_reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
