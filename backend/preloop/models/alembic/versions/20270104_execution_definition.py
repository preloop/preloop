"""Retain redacted definitions selected at launch, without backfilling history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270104_execution_definition"
down_revision = "20270102_runner_flow_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add empty history tables."""
    op.create_table(
        "flow_definition_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("flow_id", "revision"),
    )
    op.create_table(
        "execution_configuration_selection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_definition_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("execution_id", "stage", "snapshot_id"),
    )
    op.create_index(
        "ix_execution_configuration_selection_snapshot_id",
        "execution_configuration_selection",
        ["snapshot_id"],
    )


def downgrade() -> None:
    """Remove launch attribution."""
    op.drop_table("execution_configuration_selection")
    op.drop_table("flow_definition_snapshot")
