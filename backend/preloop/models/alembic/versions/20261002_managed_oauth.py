"""Tenant-bound managed OAuth storage; existing credentials stay unmanaged.

Revision ID: 20261002_managed_oauth
Revises: 20261001_artifact_kinds_labels
"""

from alembic import op
import sqlalchemy as sa

revision = "20261002_managed_oauth"
down_revision = "20261001_artifact_kinds_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_provider_configuration",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("canonical_instance", sa.String(length=1000), nullable=False),
        sa.Column("context", sa.String(length=1000), nullable=False),
        sa.Column("client_id", sa.String(length=1000), nullable=False),
        sa.Column("client_secret_id", sa.UUID(), nullable=True),
        sa.Column("callback_uri", sa.String(length=2000), nullable=False),
        sa.Column("selected_permissions", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("version > 0", name="ck_oauth_configuration_version"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_secret_id"], ["secret_reference.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_secret_id"),
        sa.UniqueConstraint("id", "account_id", name="uq_oauth_configuration_tenant"),
    )
    op.create_unique_constraint(
        "uq_tracker_id_account", "tracker", ["id", "account_id"]
    )
    op.create_table(
        "oauth_connection_transaction",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("configuration_id", sa.UUID(), nullable=False),
        sa.Column("configuration_version", sa.Integer(), nullable=False),
        sa.Column("callback_uri", sa.String(length=2000), nullable=False),
        sa.Column("return_path", sa.String(length=2000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("pending_grant_id", sa.UUID(), nullable=True),
        sa.Column("tracker_id", sa.UUID(), nullable=True),
        sa.Column("pkce_verifier_encrypted", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'completed', 'invalidated')",
            name="ck_oauth_transaction_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id", "account_id"],
            [
                "oauth_provider_configuration.id",
                "oauth_provider_configuration.account_id",
            ],
            name="fk_oauth_transaction_configuration_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tracker_id", "account_id"],
            ["tracker.id", "tracker.account_id"],
            name="fk_oauth_transaction_tracker_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "account_id", name="uq_oauth_transaction_tenant"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index(
        op.f("ix_oauth_provider_configuration_id"),
        "oauth_provider_configuration",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_provider_configuration_account_id"),
        "oauth_provider_configuration",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_connection_transaction_expires_at"),
        "oauth_connection_transaction",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_connection_transaction_account_id"),
        "oauth_connection_transaction",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_connection_transaction_id"),
        "oauth_connection_transaction",
        ["id"],
        unique=False,
    )
    op.add_column(
        "oauth_token", sa.Column("auth_mode", sa.String(length=50), nullable=True)
    )
    op.add_column("oauth_token", sa.Column("tracker_id", sa.UUID(), nullable=True))
    op.add_column(
        "oauth_token", sa.Column("configuration_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "oauth_token", sa.Column("configuration_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "oauth_token", sa.Column("connection_transaction_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "oauth_token",
        sa.Column("provider_subject", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "oauth_token",
        sa.Column("canonical_instance", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "oauth_token", sa.Column("status", sa.String(length=30), nullable=True)
    )
    op.add_column(
        "oauth_token",
        sa.Column("rotation_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_oauth_rotation_version", "oauth_token", "rotation_version >= 0"
    )
    op.create_unique_constraint(
        "uq_oauth_token_tenant", "oauth_token", ["id", "account_id"]
    )
    op.create_foreign_key(
        "fk_oauth_grant_configuration_tenant",
        "oauth_token",
        "oauth_provider_configuration",
        ["configuration_id", "account_id"],
        ["id", "account_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_oauth_token_tracker", "oauth_token", ["tracker_id"])
    op.create_foreign_key(
        "fk_oauth_grant_tracker_tenant",
        "oauth_token",
        "tracker",
        ["tracker_id", "account_id"],
        ["id", "account_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_oauth_grant_transaction_tenant",
        "oauth_token",
        "oauth_connection_transaction",
        ["connection_transaction_id", "account_id"],
        ["id", "account_id"],
        ondelete="CASCADE",
        use_alter=True,
    )
    op.create_check_constraint(
        "ck_oauth_managed_grant",
        "oauth_token",
        "auth_mode IS NULL OR (auth_mode = 'managed_oauth' AND configuration_id IS NOT NULL AND configuration_version IS NOT NULL AND provider_subject IS NOT NULL AND canonical_instance IS NOT NULL AND installation_id IS NULL AND status IS NOT NULL AND configuration_version > 0 AND status IN ('pending', 'active', 'disconnected', 'invalidated') AND ((status = 'pending' AND connection_transaction_id IS NOT NULL AND tracker_id IS NULL) OR (status = 'active' AND tracker_id IS NOT NULL) OR status IN ('disconnected', 'invalidated')))",
    )
    op.create_foreign_key(
        "fk_oauth_transaction_grant_tenant",
        "oauth_connection_transaction",
        "oauth_token",
        ["pending_grant_id", "account_id"],
        ["id", "account_id"],
        use_alter=True,
    )
    op.alter_column(
        "oauth_token",
        "expires_at",
        type_=sa.DateTime(timezone=True),
        postgresql_using="expires_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "oauth_token",
        "refresh_token_expires_at",
        type_=sa.DateTime(timezone=True),
        postgresql_using="refresh_token_expires_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    # Managed credentials have no legacy equivalent; erase rather than expose them.
    op.execute("UPDATE tracker SET is_active = false WHERE auth_type = 'managed_oauth'")
    op.execute(
        "DELETE FROM secret_reference WHERE id IN "
        "(SELECT client_secret_id FROM oauth_provider_configuration)"
    )
    op.execute("DELETE FROM oauth_connection_transaction")
    op.execute("DELETE FROM oauth_token WHERE auth_mode = 'managed_oauth'")
    op.alter_column(
        "oauth_token",
        "expires_at",
        type_=sa.DateTime(),
        postgresql_using="expires_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "oauth_token",
        "refresh_token_expires_at",
        type_=sa.DateTime(),
        postgresql_using="refresh_token_expires_at AT TIME ZONE 'UTC'",
    )
    op.drop_constraint(
        "fk_oauth_transaction_grant_tenant",
        "oauth_connection_transaction",
        type_="foreignkey",
    )
    op.drop_constraint("ck_oauth_managed_grant", "oauth_token", type_="check")
    op.drop_constraint(
        "fk_oauth_grant_transaction_tenant", "oauth_token", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_oauth_grant_tracker_tenant", "oauth_token", type_="foreignkey"
    )
    op.drop_constraint("uq_oauth_token_tracker", "oauth_token", type_="unique")
    op.drop_constraint(
        "fk_oauth_grant_configuration_tenant", "oauth_token", type_="foreignkey"
    )
    op.drop_constraint("uq_oauth_token_tenant", "oauth_token", type_="unique")
    op.drop_constraint("ck_oauth_rotation_version", "oauth_token", type_="check")
    op.drop_column("oauth_token", "rotation_version")
    op.drop_column("oauth_token", "status")
    op.drop_column("oauth_token", "canonical_instance")
    op.drop_column("oauth_token", "provider_subject")
    op.drop_column("oauth_token", "connection_transaction_id")
    op.drop_column("oauth_token", "configuration_version")
    op.drop_column("oauth_token", "configuration_id")
    op.drop_column("oauth_token", "tracker_id")
    op.drop_column("oauth_token", "auth_mode")
    op.drop_index(
        op.f("ix_oauth_connection_transaction_id"),
        table_name="oauth_connection_transaction",
    )
    op.drop_index(
        op.f("ix_oauth_connection_transaction_account_id"),
        table_name="oauth_connection_transaction",
    )
    op.drop_index(
        op.f("ix_oauth_connection_transaction_expires_at"),
        table_name="oauth_connection_transaction",
    )
    op.drop_index(
        op.f("ix_oauth_provider_configuration_account_id"),
        table_name="oauth_provider_configuration",
    )
    op.drop_index(
        op.f("ix_oauth_provider_configuration_id"),
        table_name="oauth_provider_configuration",
    )
    op.drop_table("oauth_connection_transaction")
    op.drop_constraint("uq_tracker_id_account", "tracker", type_="unique")
    op.drop_table("oauth_provider_configuration")
