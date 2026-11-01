"""Runner share grants and owner consent policy for runner sharing (EE #186)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261124_runner_share_grant"
down_revision = "20261121_exception_digest"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Create the grant and policy tables. No existing access changes."""
    op.create_table(
        "runner_share_grant",
        *_timestamps(),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_runner.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            comment="Runner owner when the grant was made",
        ),
        sa.Column("grantee_type", sa.String(8), nullable=False),
        sa.Column(
            "grantee_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="team.id or user.id, by grantee_type",
        ),
        sa.Column("scopes", postgresql.ARRAY(sa.String(16)), nullable=False),
        sa.Column(
            "harnesses",
            postgresql.ARRAY(sa.String(64)),
            comment="Harness ids the grant covers; NULL covers every harness",
        ),
        sa.Column(
            "directory_ids",
            postgresql.ARRAY(sa.String(128)),
            comment=(
                "Authorized directory ids the grant covers; NULL covers every "
                "authorized directory and tracker checkouts"
            ),
        ),
        sa.Column("consent_state", sa.String(32), nullable=False),
        sa.Column("consent_source", sa.String(32)),
        sa.Column("consented_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "grantee_type IN ('team', 'user')",
            name="ck_runner_share_grant_grantee_type",
        ),
        sa.CheckConstraint(
            "consent_state IN ('active', 'pending_owner_consent', 'declined')",
            name="ck_runner_share_grant_consent_state",
        ),
        sa.CheckConstraint(
            "consent_source IS NULL OR consent_source IN "
            "('owner', 'owner_accepted', 'admin_override')",
            name="ck_runner_share_grant_consent_source",
        ),
        sa.CheckConstraint(
            "cardinality(scopes) > 0 AND scopes <@ ARRAY['flows', 'sessions']"
            "::varchar[]",
            name="ck_runner_share_grant_scopes",
        ),
    )
    op.create_index("ix_runner_share_grant_id", "runner_share_grant", ["id"])
    op.create_index(
        "ix_runner_share_grant_runner_id", "runner_share_grant", ["runner_id"]
    )
    op.create_index(
        "ix_runner_share_grant_live",
        "runner_share_grant",
        ["account_id", "runner_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "runner_sharing_policy",
        *_timestamps(),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_runner.id", ondelete="CASCADE"),
            comment="NULL for the account-wide row",
        ),
        sa.Column("owner_consent", sa.String(16), nullable=False),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "owner_consent IN ('required', 'not_required')",
            name="ck_runner_sharing_policy_owner_consent",
        ),
    )
    op.create_index("ix_runner_sharing_policy_id", "runner_sharing_policy", ["id"])
    op.create_index(
        "uq_runner_sharing_policy_account",
        "runner_sharing_policy",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("runner_id IS NULL"),
    )
    op.create_index(
        "uq_runner_sharing_policy_runner",
        "runner_sharing_policy",
        ["account_id", "runner_id"],
        unique=True,
        postgresql_where=sa.text("runner_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_table("runner_sharing_policy")
    op.drop_table("runner_share_grant")
