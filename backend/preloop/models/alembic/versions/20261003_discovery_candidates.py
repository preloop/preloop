"""Add opt-in discovery reporting tables.

Revision ID: 20261003_discovery_candidates
Revises: 20261004_audit_lookup_idx
Create Date: 2026-10-03

Two new tables, nothing existing changes. ``discovered_agent_candidate``
holds one row per agent tool a workstation reported, keyed on salted hashes
only (unique on account, workstation fingerprint, kind and config path
hash). ``account_discovery_salt`` holds the per-account salt the CLI keys
those hashes with.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261003_discovery_candidates"
down_revision = "20261004_audit_lookup_idx"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


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
    """Create the candidate and salt tables."""
    op.create_table(
        "discovered_agent_candidate",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workstation_fingerprint", sa.String(64), nullable=False),
        sa.Column("agent_kind", sa.String(64), nullable=False),
        sa.Column("config_path_hash", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=True),
        sa.Column("mcp_server_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "reported_enrolled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("os_family", sa.String(16), nullable=True),
        sa.Column("cli_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column(
            "managed_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_agent.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "agent_kind",
            "config_path_hash",
            name="uq_discovered_agent_candidate_key",
        ),
    )
    op.create_index(
        "ix_discovered_agent_candidate_id", "discovered_agent_candidate", ["id"]
    )
    op.create_index(
        "ix_discovered_agent_candidate_account_id",
        "discovered_agent_candidate",
        ["account_id"],
    )
    op.create_index(
        "ix_discovered_agent_candidate_last_seen_at",
        "discovered_agent_candidate",
        ["last_seen_at"],
    )

    op.create_table(
        "account_discovery_salt",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("salt", sa.String(64), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_account_discovery_salt_id", "account_discovery_salt", ["id"])


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("ix_account_discovery_salt_id", table_name="account_discovery_salt")
    op.drop_table("account_discovery_salt")
    op.drop_index(
        "ix_discovered_agent_candidate_last_seen_at",
        table_name="discovered_agent_candidate",
    )
    op.drop_index(
        "ix_discovered_agent_candidate_account_id",
        table_name="discovered_agent_candidate",
    )
    op.drop_index(
        "ix_discovered_agent_candidate_id", table_name="discovered_agent_candidate"
    )
    op.drop_table("discovered_agent_candidate")
