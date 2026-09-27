"""Spend outlier alerting: per-account rule settings and recorded findings.

The rules themselves live in :mod:`preloop.services.spend_outliers`. These two
tables are what they read and write:

* ``spend_outlier_settings`` is one row per account with the operator's
  thresholds. An account without a row uses the defaults.
* ``spend_outlier_finding`` is one row per fired rule. The unique
  ``(account_id, fingerprint)`` constraint is what makes an alert fire once:
  a second evaluation of the same day inserts nothing.

Dismissal is not stored here. Findings are shown in the console Attention
inbox and are dismissed through the existing ``attention_dismissal`` table,
keyed by the same ``item_id`` and ``fingerprint``. ``dismissed_at`` is only a
record of that decision for the weekly digest, which lists dismissed findings
too and needs to know which ones they were after the dismissal row has moved
on to a later day's fingerprint.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base

#: Rule ids. They are part of every item id and fingerprint, so never rename.
SPEND_OUTLIER_RULE_DAILY = "daily_spend"
SPEND_OUTLIER_RULE_MODEL_MIX = "model_mix"
SPEND_OUTLIER_RULE_SESSION = "session_cost"
SPEND_OUTLIER_RULES = (
    SPEND_OUTLIER_RULE_DAILY,
    SPEND_OUTLIER_RULE_MODEL_MIX,
    SPEND_OUTLIER_RULE_SESSION,
)

#: Defaults used when an account has no settings row (or a column is NULL).
DEFAULT_DAILY_MULTIPLE = 3.0
DEFAULT_MIN_HISTORY_DAYS = 7
DEFAULT_TOP_TIER_SHARE = 0.5


class SpendOutlierSettings(Base):
    """One account's spend outlier thresholds."""

    __tablename__ = "spend_outlier_settings"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_spend_outlier_settings_account"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    daily_multiple: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_DAILY_MULTIPLE,
        comment="Fire when yesterday >= this multiple of the trailing median",
    )
    min_history_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MIN_HISTORY_DAYS,
        comment="Days with spend in the trailing 28 needed before the rule runs",
    )
    top_tier_model_prefixes: Mapped[List[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Model name prefixes the operator marked top-tier",
    )
    top_tier_share: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_TOP_TIER_SHARE,
        comment="Fire when a top-tier model's daily share exceeds this, 2 days",
    )
    session_cost_threshold_usd: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Fire when one session's cost exceeds this; NULL turns it off",
    )

    def __repr__(self) -> str:
        return f"<SpendOutlierSettings account={str(self.account_id)[:8]}>"


class SpendOutlierFinding(Base):
    """One fired spend outlier rule."""

    __tablename__ = "spend_outlier_finding"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "fingerprint", name="uq_spend_outlier_finding_fingerprint"
        ),
        Index(
            "ix_spend_outlier_finding_account_detected",
            "account_id",
            "detected_at",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="daily_spend | model_mix | session_cost",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
    )
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
    )
    day: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="The UTC day the finding is about",
    )
    item_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Attention item id: 'spend:<rule>:<user or session id>'",
    )
    fingerprint: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Rule, user, and day or session id; unique per account",
    )
    details: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="The numbers the card shows",
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set when the operator dismissed this exact fingerprint",
    )

    def __repr__(self) -> str:
        return (
            f"<SpendOutlierFinding {self.rule} day={self.day} "
            f"account={str(self.account_id)[:8]}>"
        )
