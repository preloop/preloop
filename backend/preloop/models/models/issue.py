"""Issue, EmbeddingModel, and IssueEmbedding models."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, DateTime

from .base import Base

from .project import Project
from .tracker import Tracker
from .comment import Comment

# Check if our vector type module is available
try:
    from ..db.vector_types import VectorType  # noqa: F401

    VECTOR_TYPE_AVAILABLE = True
except ImportError:
    VECTOR_TYPE_AVAILABLE = False


class Issue(Base):
    """Issue model - represents a task, bug, or feature in a project."""

    # Issue details
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    priority: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False, default="task")

    # External issue identifiers
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    key: Mapped[str] = mapped_column(String(512), nullable=True, index=True)

    # Foreign keys
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project", back_populates="issues")
    tracker: Mapped["Tracker"] = relationship("Tracker", back_populates="issues")
    parent: Mapped[Optional["Issue"]] = relationship(
        "Issue", remote_side="Issue.id", back_populates="children"
    )
    children: Mapped[List["Issue"]] = relationship(
        "Issue", back_populates="parent", cascade="all, delete-orphan"
    )
    embeddings: Mapped[List["IssueEmbedding"]] = relationship(
        "IssueEmbedding", back_populates="issue", cascade="all, delete-orphan"
    )
    comments: Mapped[List["Comment"]] = relationship(
        "Comment", back_populates="issue", cascade="all, delete-orphan"
    )

    # Metadata stored as JSON (for custom fields, labels, etc.)
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Timestamps for issue-specific events
    last_updated_external: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # One row per provider issue in a project. Concurrent webhook
        # deliveries upsert against this constraint (#1194).
        UniqueConstraint(
            "project_id", "external_id", name="uq_issue_project_external_id"
        ),
    )


class EmbeddingModel(Base):
    """Model to track different embedding models used in the system."""

    # Primary key is inherited from Base

    # Embedding model details
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # 'openai', 'google', etc.
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Additional embedding model properties
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Relationships
    embeddings: Mapped[List["IssueEmbedding"]] = relationship(
        "IssueEmbedding", back_populates="embedding_model"
    )

    __table_args__ = (
        # Enforce unique composite key for provider+version
        UniqueConstraint("provider", "version", name="uix_provider_version"),
    )


class IssueEmbedding(Base):
    """Model to store embeddings for issues.

    This flexible design supports embeddings of different dimensions.
    """

    # Primary key is inherited from Base

    # Foreign keys
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    comment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("comment.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    embedding_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("embeddingmodel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The actual embedding vector (PostgreSQL vector type)
    # The actual embedding vector, using VectorType which adapts to pgvector.
    embedding: Mapped[List[float]] = mapped_column(
        VectorType(1536), nullable=False, comment="Embedding vector"
    )

    # Metadata about how this embedding was created
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # When this embedding was created

    # Relationships
    issue: Mapped["Issue"] = relationship("Issue", back_populates="embeddings")
    comment: Mapped[Optional["Comment"]] = relationship(
        "Comment", back_populates="embeddings"
    )
    embedding_model: Mapped["EmbeddingModel"] = relationship(
        "EmbeddingModel", back_populates="embeddings"
    )

    __table_args__ = (
        # Enforce one embedding per issue per model
        UniqueConstraint(
            "issue_id",
            "comment_id",
            "embedding_model_id",
            name="uix_issue_embedding_model",
        ),
    )
