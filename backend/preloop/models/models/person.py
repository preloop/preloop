"""Person: the human behind one or more membership (``user``) rows.

One membership is one ``user`` row, scoped to one account as before. A person
links the rows that belong to the same human, so the same credentials can reach
several accounts with a different role in each. Nothing in this repository
reads the link yet: it is schema for the account hierarchy work (#986).

A person whose ``email_verified_at`` is NULL is provisional. It holds exactly
one row and is never merged into another person before its email is verified,
so pre-registering an unverified address cannot capture somebody else's
memberships. The partial unique index keeps one verified person per address.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .user import User


def normalize_email(email: Optional[str]) -> str:
    """Return the form of an address that ``person.email_normalized`` stores.

    Lowercase with surrounding whitespace removed, the same expression the
    backfill migration applies in SQL (``lower(btrim(email))``).
    """
    return (email or "").strip().lower()


class Person(Base):
    """A human who holds one or more ``user`` rows (memberships)."""

    __tablename__ = "person"

    email_normalized: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Lowercased, trimmed address of the membership rows this person holds",
    )
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the address was known verified; NULL for a provisional person",
    )
    primary_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "user.id",
            ondelete="SET NULL",
            name="fk_person_primary_user",
            use_alter=True,
        ),
        nullable=True,
        comment="The membership row holding password, passkeys and OAuth links",
    )
    last_active_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "user.id",
            ondelete="SET NULL",
            name="fk_person_last_active_user",
            use_alter=True,
        ),
        nullable=True,
        comment="The membership row this person used most recently",
    )

    # post_update: person and user reference each other. The person row is
    # inserted first with these NULL, then updated once the user row exists.
    primary_user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[primary_user_id], post_update=True
    )
    last_active_user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[last_active_user_id], post_update=True
    )
    memberships: Mapped[List["User"]] = relationship(
        "User", back_populates="person", foreign_keys="[User.person_id]"
    )

    __table_args__ = (
        Index(
            "uq_person_email_verified",
            "email_normalized",
            unique=True,
            postgresql_where=text("email_verified_at IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<Person(id={self.id}, email={self.email_normalized})>"
