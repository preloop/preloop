"""Enterprise SSO configuration tables.

Revision ID: 20261112_enterprise_sso
Revises: 20261110_media_timing

Per-account IdP connections, verified email domains, the SSO policy, IdP
group mappings, hashed SCIM tokens and the single-use id replay guard. New
tables only; nothing existing is altered.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261112_enterprise_sso"
down_revision = "20261110_media_timing"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def _account_fk() -> sa.Column:
    return sa.Column(
        "account_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )


def _secret_fk(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade() -> None:
    """Create the six SSO tables."""
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "account_sso_connection",
        *_base(),
        _account_fk(),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("oidc_issuer", sa.String(512), nullable=True),
        sa.Column("oidc_client_id", sa.String(255), nullable=True),
        _secret_fk("oidc_client_secret_ref_id"),
        sa.Column(
            "oidc_scopes",
            jsonb,
            nullable=False,
            server_default=sa.text('\'["openid", "email", "profile"]\'::jsonb'),
        ),
        sa.Column("groups_claim", sa.String(255), nullable=True),
        sa.Column("saml_idp_entity_id", sa.String(512), nullable=True),
        sa.Column("saml_idp_sso_url", sa.String(1024), nullable=True),
        sa.Column("saml_idp_metadata_url", sa.String(1024), nullable=True),
        sa.Column(
            "saml_idp_certs",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        _secret_fk("saml_sp_signing_key_ref_id"),
        sa.Column(
            "saml_want_assertions_signed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "default_role", sa.String(64), nullable=False, server_default="member"
        ),
        sa.Column(
            "jit_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "attribute_mapping",
            jsonb,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _user_fk("created_by_user_id"),
        _user_fk("updated_by_user_id"),
        sa.UniqueConstraint(
            "account_id", "name", name="uq_account_sso_connection_name"
        ),
        sa.CheckConstraint(
            "protocol IN ('oidc', 'saml')", name="ck_account_sso_connection_protocol"
        ),
        sa.CheckConstraint(
            "default_role <> 'owner'", name="ck_account_sso_connection_not_owner"
        ),
    )
    op.create_index("ix_account_sso_connection_id", "account_sso_connection", ["id"])
    op.create_index(
        "ix_account_sso_connection_account_id",
        "account_sso_connection",
        ["account_id"],
    )

    op.create_table(
        "account_verified_domain",
        *_base(),
        _account_fk(),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("challenge_token", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "account_id", "domain", name="uq_account_verified_domain_account"
        ),
    )
    op.create_index("ix_account_verified_domain_id", "account_verified_domain", ["id"])
    op.create_index(
        "ix_account_verified_domain_account_id",
        "account_verified_domain",
        ["account_id"],
    )
    op.create_index(
        "uq_account_verified_domain_verified",
        "account_verified_domain",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("verified_at IS NOT NULL"),
    )

    op.create_table(
        "account_sso_policy",
        *_base(),
        _account_fk(),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("break_glass_owner_ids", jsonb, nullable=True),
        sa.Column("session_max_age_seconds", sa.Integer(), nullable=True),
        _user_fk("updated_by_user_id"),
        sa.UniqueConstraint("account_id", name="account_sso_policy_account_id_key"),
    )
    op.create_index("ix_account_sso_policy_id", "account_sso_policy", ["id"])

    op.create_table(
        "sso_group_mapping",
        *_base(),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idp_group", sa.String(512), nullable=False),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("role", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "team_id IS NOT NULL OR role IS NOT NULL",
            name="ck_sso_group_mapping_target",
        ),
        sa.CheckConstraint(
            "role IS NULL OR role <> 'owner'", name="ck_sso_group_mapping_not_owner"
        ),
    )
    op.create_index("ix_sso_group_mapping_id", "sso_group_mapping", ["id"])
    op.create_index(
        "ix_sso_group_mapping_connection_id", "sso_group_mapping", ["connection_id"]
    )

    op.create_table(
        "scim_token",
        *_base(),
        _account_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        _user_fk("created_by_user_id"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_hash", name="scim_token_token_hash_key"),
    )
    op.create_index("ix_scim_token_id", "scim_token", ["id"])
    op.create_index("ix_scim_token_account_id", "scim_token", ["account_id"])

    op.create_table(
        "sso_replay_guard",
        *_base(),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("guard_id", sa.String(512), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "connection_id", "guard_id", name="uq_sso_replay_guard_connection_id"
        ),
    )
    op.create_index("ix_sso_replay_guard_id", "sso_replay_guard", ["id"])
    op.create_index(
        "ix_sso_replay_guard_expires_at", "sso_replay_guard", ["expires_at"]
    )


def downgrade() -> None:
    """Drop the SSO tables, dependents first."""
    for table in (
        "sso_replay_guard",
        "scim_token",
        "sso_group_mapping",
        "account_sso_policy",
        "account_verified_domain",
        "account_sso_connection",
    ):
        op.drop_table(table)
