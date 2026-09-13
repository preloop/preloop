"""Track historical usage repricing submissions and worker outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260913_repricing_job"
down_revision = "20260912_control_connection"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS


def upgrade() -> None:
    op.create_table(
        "repricing_job",
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column(
            "account_id",
            sa.UUID(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_repricing_job_id", "repricing_job", ["id"])
    op.create_index("ix_repricing_job_account_id", "repricing_job", ["account_id"])


def downgrade() -> None:
    op.drop_table("repricing_job")
