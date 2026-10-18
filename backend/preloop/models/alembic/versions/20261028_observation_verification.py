"""Add inert source-scoped verification storage for optional evidence processors."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261028_obs_verification"
down_revision = "20261028_chat_admin_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create verification storage without enabling any verifier or processor."""
    op.create_table(
        "observation_verification",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("subject_key", sa.String(64), nullable=False),
        sa.Column("policy_revision", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("invalidation_reason", sa.String(64)),
        sa.UniqueConstraint(
            "account_id",
            "source_ref",
            "subject_key",
            name="uq_observation_verification_scope",
        ),
        sa.CheckConstraint(
            "expires_at > verified_at", name="ck_observation_verification_window"
        ),
    )
    op.create_index(
        "ix_observation_verification_id", "observation_verification", ["id"]
    )
    op.create_index(
        "ix_observation_verification_account_id",
        "observation_verification",
        ["account_id"],
    )


def downgrade() -> None:
    """Remove optional verification records."""
    op.drop_table("observation_verification")
