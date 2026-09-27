"""Add spend outlier settings and findings (#960).

Revision ID: 20260927_spend_outliers
Revises: 20260924_usage_principal_ts
Create Date: 2026-09-27

``spend_outlier_settings`` holds one account's thresholds for the three spend
outlier rules. ``spend_outlier_finding`` records each rule that fired, unique
by ``(account_id, fingerprint)`` so a rule fires once per user and day (or per
session). Dismissal reuses ``attention_dismissal``; no second dismissal table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "20260927_spend_outliers"
down_revision = "20260924_usage_principal_ts"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Create the spend outlier tables."""
    op.create_table(
        "spend_outlier_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "daily_multiple",
            sa.Float(),
            nullable=False,
            server_default=sa.text("3.0"),
            comment="Fire when yesterday >= this multiple of the trailing median",
        ),
        sa.Column(
            "min_history_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("7"),
            comment="Days with spend in the trailing 28 needed before the rule runs",
        ),
        sa.Column(
            "top_tier_model_prefixes",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Model name prefixes the operator marked top-tier",
        ),
        sa.Column(
            "top_tier_share",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.5"),
            comment="Fire when a top-tier model's daily share exceeds this, 2 days",
        ),
        sa.Column(
            "session_cost_threshold_usd",
            sa.Float(),
            nullable=True,
            comment="Fire when one session's cost exceeds this; NULL turns it off",
        ),
        sa.UniqueConstraint("account_id", name="uq_spend_outlier_settings_account"),
    )

    op.create_table(
        "spend_outlier_finding",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rule",
            sa.String(32),
            nullable=False,
            comment="daily_spend | model_mix | session_cost",
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "runtime_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "day",
            sa.Date(),
            nullable=False,
            comment="The UTC day the finding is about",
        ),
        sa.Column(
            "item_id",
            sa.String(255),
            nullable=False,
            comment="Attention item id: 'spend:<rule>:<user or session id>'",
        ),
        sa.Column(
            "fingerprint",
            sa.Text(),
            nullable=False,
            comment="Rule, user, and day or session id; unique per account",
        ),
        sa.Column(
            "details",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="The numbers the card shows",
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when the operator dismissed this exact fingerprint",
        ),
        sa.UniqueConstraint(
            "account_id",
            "fingerprint",
            name="uq_spend_outlier_finding_fingerprint",
        ),
    )
    op.create_index(
        "ix_spend_outlier_finding_account_detected",
        "spend_outlier_finding",
        ["account_id", "detected_at"],
    )


def downgrade() -> None:
    """Drop the spend outlier tables."""
    op.drop_index(
        "ix_spend_outlier_finding_account_detected",
        table_name="spend_outlier_finding",
    )
    op.drop_table("spend_outlier_finding")
    op.drop_table("spend_outlier_settings")
