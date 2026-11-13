"""Store registered managed bundles and independent control expectations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261113_bundle_registry"
down_revision = "20261207_user_passkey_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create account-scoped revision history and origin-owned drift tuples."""
    op.create_table(
        "registered_bundle_revision",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("policy_revision_hash", sa.String(64), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "account_id",
            "app_id",
            "platform",
            "policy_revision_hash",
            name="uq_bundle_revision",
        ),
        sa.UniqueConstraint("id", "account_id", name="uq_bundle_revision_account"),
    )
    op.create_index(
        "uq_bundle_active_scope",
        "registered_bundle_revision",
        ["account_id", "app_id", "platform"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_table(
        "control_expectation",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("bundle_revision_id", UUID(as_uuid=True)),
        sa.Column("source_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("workstation_fingerprint", sa.String(64), nullable=False),
        sa.Column("agent_kind", sa.String(64), nullable=False),
        sa.Column("config_path_hash", sa.String(64), nullable=False),
        sa.Column("axis", sa.String(32), nullable=False),
        sa.Column("policy_revision_hash", sa.String(64), nullable=False),
        sa.Column("required_scope", sa.String(32), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "account_id",
            "origin",
            "source_ref",
            "workstation_fingerprint",
            "agent_kind",
            "config_path_hash",
            "axis",
            "policy_revision_hash",
            name="uq_control_expectation_tuple",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_revision_id", "account_id"],
            ["registered_bundle_revision.id", "registered_bundle_revision.account_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(origin = 'bundle' AND bundle_revision_id IS NOT NULL) OR (origin = 'manual' AND bundle_revision_id IS NULL)",
            name="ck_control_expectation_origin",
        ),
    )
    for table in ("registered_bundle_revision", "control_expectation"):
        op.create_index(f"ix_{table}_id", table, ["id"])
        op.create_index(f"ix_{table}_account_id", table, ["account_id"])
    op.create_index(
        "ix_control_expectation_bundle_revision_id",
        "control_expectation",
        ["bundle_revision_id"],
    )


def downgrade() -> None:
    """Drop expectation tuples before their registered revision history."""
    op.drop_table("control_expectation")
    op.drop_table("registered_bundle_revision")
