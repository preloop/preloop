"""Encrypted recovery artifacts and immutable manifests.

Revision ID: 20260906_flow_artifacts
Revises: 20260906_flow_feedback
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_flow_artifacts"
down_revision = "20260906_flow_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create encrypted artifact storage with tenant and thread indexes."""
    op.create_table(
        "flow_artifact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("availability", sa.String(20), nullable=False),
    )
    for name in ("account_id", "flow_id", "execution_id", "thread_id", "expires_at"):
        op.create_index(f"ix_flow_artifact_{name}", "flow_artifact", [name])


def downgrade() -> None:
    """Drop recovery artifacts; existing legacy snapshots are unaffected."""
    op.drop_table("flow_artifact")
