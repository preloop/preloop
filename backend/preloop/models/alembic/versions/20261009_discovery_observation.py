"""Add immutable source discovery observations.
Revision ID: 20261009_discovery_observation
Revises: 20261009_access_rule_generation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261009_discovery_observation"
down_revision = "20261009_access_rule_generation"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS


def upgrade() -> None:
    """Create safe source evidence storage without changing candidates."""
    op.create_table(
        "discovery_observation",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workstation_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("observation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "source_ref",
            "observation_id",
            name="uq_discovery_observation_source",
        ),
    )
    for name in ("id", "account_id", "workstation_fingerprint", "received_at"):
        op.create_index(
            f"ix_discovery_observation_{name}", "discovery_observation", [name]
        )


def downgrade() -> None:
    """Drop observation history only."""
    op.drop_table("discovery_observation")
