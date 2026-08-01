"""API usage tracking model for analytics."""

import uuid
from datetime import datetime

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Boolean, DateTime, Float, Integer, String

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .ai_model import AIModel
    from .api_key import ApiKey
    from .flow import Flow
    from .flow_execution import FlowExecution
    from .gateway_usage_search_document import GatewayUsageSearchDocument
    from .runtime_session import RuntimeSession
    from .user import User


class ApiUsage(Base):
    """API usage model for tracking API requests and resource consumption.

    Attributes:
        id: The unique identifier for the usage record.
        user_id: The ID of the user making the request (nullable for anonymous requests).
        endpoint: The API endpoint being accessed.
        method: The HTTP method used (GET, POST, etc.).
        status_code: The HTTP status code of the response.
        duration: The time taken to process the request in seconds.
        action_type: The type of action (create_issue, update_issue, etc.).
        timestamp: When the request was made.
    """

    __tablename__ = "api_usage"

    # Request details
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_key.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    action_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    auth_subject_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ai_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_model.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_alias: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    upstream_request_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Unified cache semantics: cache_read_tokens covers OpenAI
    # prompt_tokens_details.cached_tokens and Anthropic cache_read_input_tokens;
    # the raw provider breakdown stays in meta_data["usage_details"].
    cache_read_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_creation_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # ISO-4217 code of estimated_cost; NULL on legacy rows means USD.
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    # override | model_config | catalog | subscription | unpriced
    cost_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # provider | estimated | partial
    usage_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_retry: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Provider-advised Retry-After (milliseconds) observed on a rate-limited
    # upstream response; the full observed header snapshot is in
    # meta_data["rate_limit"] (#136). NULL when the provider sent no hint.
    rate_limit_retry_after_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    runtime_principal_type: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    runtime_principal_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    runtime_principal_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    meta_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    account: Mapped[Optional["Account"]] = relationship("Account")
    api_key: Mapped[Optional["ApiKey"]] = relationship("ApiKey")
    ai_model: Mapped[Optional["AIModel"]] = relationship("AIModel")
    flow: Mapped[Optional["Flow"]] = relationship("Flow")
    flow_execution: Mapped[Optional["FlowExecution"]] = relationship("FlowExecution")
    runtime_session: Mapped[Optional["RuntimeSession"]] = relationship(
        "RuntimeSession", back_populates="api_usages"
    )
    gateway_search_document: Mapped[Optional["GatewayUsageSearchDocument"]] = (
        relationship(
            "GatewayUsageSearchDocument",
            back_populates="api_usage",
            uselist=False,
            cascade="all, delete-orphan",
        )
    )
    user: Mapped[Optional["User"]] = relationship("User", back_populates="api_usages")

    __table_args__ = (
        CheckConstraint(
            "cost_source IS NULL OR cost_source IN "
            "('override', 'model_config', 'catalog', 'subscription', 'unpriced')",
            name="ck_api_usage_cost_source",
        ),
        CheckConstraint(
            "usage_source IS NULL OR usage_source IN "
            "('provider', 'estimated', 'partial')",
            name="ck_api_usage_usage_source",
        ),
        Index(
            "ix_api_usage_account_action_ts",
            "account_id",
            "action_type",
            "timestamp",
            postgresql_ops={"timestamp": "DESC"},
        ),
        Index(
            "ix_api_usage_account_principal_ts",
            "account_id",
            "runtime_principal_type",
            "runtime_principal_id",
            "timestamp",
            postgresql_ops={"timestamp": "DESC"},
        ),
        Index(
            "ix_api_usage_rate_limited",
            "account_id",
            "timestamp",
            postgresql_where=text("status_code = 429"),
        ),
    )

    def __repr__(self) -> str:
        """Return a string representation of the usage record.

        Returns:
            String representation of the usage record.
        """
        return f"<ApiUsage {self.method} {self.endpoint} by user {self.user_id} at {self.timestamp}>"
