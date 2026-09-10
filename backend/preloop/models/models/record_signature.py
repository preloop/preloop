"""Detached signatures over records that are not files we can append to.

A period export is an archive, so its signature travels inside it as
``signature.json``. An evidence pack is different: the archive is immutable
and content addressed the moment it is stored, and appending a member would
change the digest the receipt already promised. So the signature lives beside
the record instead, keyed by what it covers.

The payload is stored, not just its digest. A verifier who has the archive
must be able to rebuild the exact bytes that were signed without asking us
what they were, and a digest alone would make them take our word for the
payload behind it.

One signature per (account, payload type, subject). Re-signing an immutable
record would either produce the same document again or quietly replace
evidence a customer already holds; the unique index makes the second one
impossible.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base

#: Subject kinds this table holds. Stored as plain strings: a new signed
#: record type should not need a migration.
SUBJECT_EVIDENCE_PACK = "evidence_pack"


class RecordSignature(Base):
    """One detached signature and the payload it was taken over."""

    __tablename__ = "record_signature"
    __table_args__ = (
        Index(
            "uq_record_signature_subject",
            "account_id",
            "payload_type",
            "subject_id",
            unique=True,
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payload_type: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="Signed payload type, inside the signed bytes as domain separation",
    )
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="Identifier of the record this signature covers",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="The exact payload the digest was taken over",
    )
    digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="sha256 of the canonical JSON of payload",
    )
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signature: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Base64 detached signature"
    )
    signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Covered by the signature, so it is dated rather than claimed",
    )

    def __repr__(self) -> str:
        """Describe the signature by what it covers, not by its bytes."""
        return (
            f"<RecordSignature {self.payload_type} {self.subject_type}="
            f"{self.subject_id} key={self.signing_key_id}>"
        )
