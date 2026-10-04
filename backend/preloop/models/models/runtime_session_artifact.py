"""Encrypted binary artifacts attached to a runtime session.

Screenshots, recordings, audio, transcripts, documents, generated files and
traces are observation records stored per account. Provenance columns
(``producer``, ``agent_id``, ``tool_name``) say where the bytes came from;
``labels`` carry account-defined metadata (reserved keys ``site``,
``tenant_ref``, ``consent_basis``, ``retention_class`` and ``tags``). A row
is never an approval, a dispatch, or proof that a browser reached a state.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RuntimeSessionArtifact(Base):
    """One encrypted artifact for a runtime session."""

    __tablename__ = "runtime_session_artifact"
    __table_args__ = (
        Index(
            "uq_runtime_session_artifact_source",
            "runtime_session_id",
            "kind",
            "source",
            "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
        Index(
            "ix_runtime_session_artifact_labels",
            "labels",
            postgresql_using="gin",
        ),
        Index(
            "ix_runtime_session_artifact_account_kind_created",
            "account_id",
            "kind",
            "created_at",
        ),
        # Account-wide search order (#1086); created by migration
        # 20261004_artifact_created_idx.
        Index(
            "ix_runtime_session_artifact_account_created",
            "account_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session_activity.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    ciphertext: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    availability: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="available",
        server_default=text("'available'"),
    )
    legal_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        index=True,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    labels: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    producer: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    tool_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    text_status: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        default="none",
        server_default=text("'none'"),
    )
    parent_artifact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "runtime_session_artifact.id",
            ondelete="SET NULL",
            name="fk_runtime_session_artifact_parent",
        ),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
