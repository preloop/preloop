"""Per-share session approval mode, preserving consent for existing grants."""

from alembic import op
import sqlalchemy as sa

revision = "20261130_runner_share_approval"
down_revision = "20261129_retention_exec_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runner_share_grant",
        sa.Column(
            "approval", sa.String(16), nullable=False, server_default="per_request"
        ),
    )
    op.create_check_constraint(
        "ck_runner_share_approval",
        "runner_share_grant",
        "approval IN ('none', 'per_request')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runner_share_approval", "runner_share_grant", type_="check")
    op.drop_column("runner_share_grant", "approval")
