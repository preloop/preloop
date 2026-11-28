"""Store immutable, scoped flow change proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270104_flow_change_proposal"
down_revision = "20270102_runner_flow_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the review document store."""
    op.create_table(
        "flow_change_proposal",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        *(
            sa.Column(
                name + "_id",
                postgresql.UUID(),
                sa.ForeignKey(name + ".id", ondelete="CASCADE"),
                nullable=False,
            )
            for name in ("account", "project", "flow")
        ),
        sa.Column("digest", sa.String(64), unique=True, nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_flow_change_proposal_id", "flow_change_proposal", ["id"])


def downgrade() -> None:
    """Remove the review document store."""
    op.drop_table("flow_change_proposal")
