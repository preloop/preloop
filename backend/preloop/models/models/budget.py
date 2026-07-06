"""Database models for Model Gateway Budgets."""

import enum
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.sql import func
import uuid

from .base import Base


class BudgetPeriod(str, enum.Enum):
    hourly = "hourly"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"
    all_time = "all_time"


class BudgetPolicy(Base):
    """Configuration for a budget limit applied to a model and subject."""

    __tablename__ = "budget_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )

    subject_type = Column(
        String, nullable=False, index=True
    )  # 'account', 'flow', 'api_key', 'managed_agent'
    subject_id = Column(
        UUID(as_uuid=True), nullable=True, index=True
    )  # UUID of the flow, api_key, etc. None if subject_type is account.

    model_alias = Column(
        String, nullable=True, index=True
    )  # e.g., 'gpt-5.4'. If null, applies to all models for that subject.
    period = Column(Enum(BudgetPeriod), nullable=False)

    hard_limit_usd = Column(Float, nullable=True)
    soft_limit_usd = Column(Float, nullable=True)

    notify_on_soft = Column(Boolean, default=False, nullable=False)
    notify_on_hard = Column(Boolean, default=False, nullable=False)
    notification_user_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    notification_team_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    notification_emails = Column(ARRAY(String), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "subject_type",
            "subject_id",
            "model_alias",
            "period",
            name="uq_budget_policies_subject_model_period",
        ),
    )


class BudgetSpendActivity(Base):
    """Aggregated rollup table for scalable tracking of budget consumption."""

    __tablename__ = "budget_spend_activities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )

    subject_type = Column(String, nullable=False)
    subject_id = Column(UUID(as_uuid=True), nullable=True)

    model_alias = Column(String, nullable=True)
    period = Column(Enum(BudgetPeriod), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=True)  # Null for 'all_time'

    spend_usd = Column(Float, default=0.0, nullable=False)

    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "subject_type",
            "subject_id",
            "model_alias",
            "period",
            "period_start",
            name="uq_budget_spend_activities_period_start",
        ),
        Index(
            "ix_budget_spend_activities_lookup",
            "account_id",
            "subject_type",
            "subject_id",
            "model_alias",
            "period",
            "period_start",
        ),
    )
