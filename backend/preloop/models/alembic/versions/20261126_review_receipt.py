"""Server-owned receipts for delivered pull request reviews (EE #202)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261126_review_receipt"
down_revision = "20261125_runner_share_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the receipt table; no existing run gets a receipt."""
    op.create_table(
        "review_publication_receipt",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("pull_request", sa.String(64), nullable=False),
        sa.Column("review_action", sa.String(32), nullable=False),
        sa.Column("head_sha", sa.String(64)),
        sa.Column("provider_review_id", sa.String(128)),
    )
    for name in ("account_id", "execution_id"):
        op.create_index(
            f"ix_review_publication_receipt_{name}",
            "review_publication_receipt",
            [name],
        )
    op.create_index(
        "ix_review_publication_receipt_target",
        "review_publication_receipt",
        ["account_id", "repository", "pull_request", "head_sha"],
    )


def downgrade() -> None:
    """Drop the receipts."""
    op.drop_table("review_publication_receipt")
