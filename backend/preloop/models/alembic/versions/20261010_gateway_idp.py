"""Customer OIDC providers trusted on the Anthropic gateway (#1414).

Revision ID: 20261010_gateway_idp
Revises: 20261010_callback_receipt
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261010_gateway_idp"
down_revision = "20261010_callback_receipt"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Create the provider and audience tables."""
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "gateway_identity_provider",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_key.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "allowed_email_domains",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "email_claim", sa.String(128), nullable=False, server_default="email"
        ),
        sa.Column(
            "require_email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("groups_claim", sa.String(128), nullable=True),
        sa.Column("allowed_groups", jsonb, nullable=True),
        sa.Column(
            "required_claims",
            jsonb,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "clock_skew_seconds", sa.Integer(), nullable=False, server_default="60"
        ),
        sa.Column(
            "max_token_lifetime_seconds",
            sa.Integer(),
            nullable=False,
            server_default="86400",
        ),
        sa.Column(
            "allowed_algorithms",
            jsonb,
            nullable=False,
            server_default=sa.text('\'["RS256", "ES256"]\'::jsonb'),
        ),
        sa.Column(
            "allowed_jwks_hosts",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allow_private_network_issuer",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("api_key_id", name="uq_gateway_identity_provider_api_key"),
    )
    op.create_index(
        "ix_gateway_identity_provider_id", "gateway_identity_provider", ["id"]
    )
    op.create_index(
        "ix_gateway_identity_provider_account_id",
        "gateway_identity_provider",
        ["account_id"],
    )
    op.create_index(
        "ix_gateway_identity_provider_issuer", "gateway_identity_provider", ["issuer"]
    )
    op.create_table(
        "gateway_identity_provider_audience",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gateway_identity_provider.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("audience", sa.String(512), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "issuer", "audience", name="uq_gateway_idp_audience_issuer_audience"
        ),
    )
    op.create_index(
        "ix_gateway_identity_provider_audience_id",
        "gateway_identity_provider_audience",
        ["id"],
    )
    op.create_index(
        "ix_gateway_identity_provider_audience_provider_id",
        "gateway_identity_provider_audience",
        ["provider_id"],
    )


def downgrade() -> None:
    """Drop the audience and provider tables."""
    op.drop_index(
        "ix_gateway_identity_provider_audience_provider_id",
        table_name="gateway_identity_provider_audience",
    )
    op.drop_index(
        "ix_gateway_identity_provider_audience_id",
        table_name="gateway_identity_provider_audience",
    )
    op.drop_table("gateway_identity_provider_audience")
    op.drop_index(
        "ix_gateway_identity_provider_issuer", table_name="gateway_identity_provider"
    )
    op.drop_index(
        "ix_gateway_identity_provider_account_id",
        table_name="gateway_identity_provider",
    )
    op.drop_index(
        "ix_gateway_identity_provider_id", table_name="gateway_identity_provider"
    )
    op.drop_table("gateway_identity_provider")
