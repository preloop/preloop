"""Store installed extensions and durable account approval decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261212_extensions"
down_revision = "20261211_external_grant_idx"
branch_labels = None
depends_on = None


def _common_columns() -> list[sa.Column]:
    """Return independent identity and timestamp columns for each table."""
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_kind", sa.String(64), nullable=False),
        sa.Column("extension_kind", sa.String(16), nullable=False),
        sa.Column("extension_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Create inventory with NULL-safe identity and approvals with separate lifetime."""
    op.create_table(
        "discovered_extension",
        *_common_columns(),
        sa.Column("workstation_fingerprint", sa.String(128), nullable=False),
        sa.Column(
            "profile_ref", sa.String(80), server_default="current", nullable=False
        ),
        sa.Column("version", sa.String(64)),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref_hash", sa.String(80)),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("project_ref_hash", sa.String(80)),
        sa.Column("enabled", sa.String(8), server_default="unknown", nullable=False),
        sa.Column("content_hash", sa.String(80)),
        sa.Column("event", sa.String(32)),
        sa.Column("hook_count", sa.Integer()),
        sa.Column("id_hashed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("present", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_observation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("discovery_observation.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint("extension_kind IN ('plugin','skill','hook','command')"),
        sa.CheckConstraint("scope IN ('user','project')"),
        sa.CheckConstraint("enabled IN ('true','false','unknown')"),
        sa.UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "profile_ref",
            "agent_kind",
            "extension_kind",
            "extension_id",
            "scope",
            "project_ref_hash",
            name="uq_discovered_extension",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_discovered_extension_account_ext",
        "discovered_extension",
        ["account_id", "agent_kind", "extension_kind", "extension_id"],
    )
    op.create_index(
        "ix_discovered_extension_last_observation_id",
        "discovered_extension",
        ["last_observation_id"],
    )
    op.create_index("ix_discovered_extension_id", "discovered_extension", ["id"])
    op.create_table(
        "extension_approval",
        *_common_columns(),
        sa.Column("approval_state", sa.String(16), nullable=False),
        sa.Column("version_constraint", sa.String(64)),
        sa.Column("note", sa.String(512)),
        sa.Column(
            "decided_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("approval_state IN ('approved','unapproved','unknown')"),
        sa.CheckConstraint("extension_kind IN ('plugin','skill','hook','command')"),
        sa.UniqueConstraint(
            "account_id",
            "agent_kind",
            "extension_kind",
            "extension_id",
            name="uq_extension_approval",
        ),
    )
    op.create_index("ix_extension_approval_id", "extension_approval", ["id"])


def downgrade() -> None:
    """Drop inventory and account decisions."""
    op.drop_table("extension_approval")
    op.drop_table("discovered_extension")
