"""Require owner consent for runner flows and other users' agent sessions.

Revision ID: 20261022_owner_consent
Revises: 20261023_gov_retry_idx
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261022_owner_consent"
down_revision = "20261023_gov_retry_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply conservative defaults to existing and new personal resources."""
    op.add_column(
        "flow_runner",
        sa.Column(
            "flow_execution_consent",
            sa.String(16),
            nullable=False,
            server_default="ask_me",
        ),
    )
    op.create_check_constraint(
        "ck_flow_runner_execution_consent",
        "flow_runner",
        "flow_execution_consent IN ('always', 'ask_me', 'never')",
    )
    op.add_column(
        "managed_agent",
        sa.Column(
            "others_may_start_sessions",
            sa.String(16),
            nullable=False,
            server_default="ask_me",
        ),
    )
    op.create_check_constraint(
        "ck_managed_agent_session_consent",
        "managed_agent",
        "others_may_start_sessions IN ('ask_me', 'allow', 'deny')",
    )
    op.create_table(
        "owner_consent",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("request_key", sa.String(255), nullable=False),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notification_sent_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "account_id", "action", "request_key", name="uq_owner_consent_intent"
        ),
    )
    op.create_index("ix_owner_consent_id", "owner_consent", ["id"])
    op.create_index("ix_owner_consent_account_id", "owner_consent", ["account_id"])
    op.create_index(
        "ix_owner_consent_approval_request_id",
        "owner_consent",
        ["approval_request_id"],
        unique=True,
    )


def downgrade() -> None:
    """Remove consent bindings and settings."""
    op.drop_table("owner_consent")
    op.drop_constraint(
        "ck_managed_agent_session_consent", "managed_agent", type_="check"
    )
    op.drop_column("managed_agent", "others_may_start_sessions")
    op.drop_constraint("ck_flow_runner_execution_consent", "flow_runner", type_="check")
    op.drop_column("flow_runner", "flow_execution_consent")
