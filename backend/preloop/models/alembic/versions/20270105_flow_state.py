"""Add per-flow persistent state documents."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270105_flow_state"
down_revision = "20270104_execution_definition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the flow_state table."""
    op.create_table(
        "flow_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_by_execution_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("flow_id", "key", name="uq_flow_state_key"),
    )
    op.create_index("ix_flow_state_id", "flow_state", ["id"])
    op.create_index("ix_flow_state_account_id", "flow_state", ["account_id"])
    op.create_index("ix_flow_state_flow_id", "flow_state", ["flow_id"])


def downgrade() -> None:
    """Drop the flow_state table."""
    op.drop_table("flow_state")
