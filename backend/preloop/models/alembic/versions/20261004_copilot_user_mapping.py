"""Add Copilot login mappings and finding supersession (#1061).

Revision ID: 20261004_copilot_user_mapping
Revises: 20261004_audit_lookup_idx
Create Date: 2026-10-04

``copilot_user_mapping`` ties one canonical GitHub login of the connected
Copilot organization to one Preloop user of the same account, unique on
``(account_id, organization, github_login)`` so a login can never point at
two users while several logins may point at one. Mappings are written by an
operator; nothing is inferred.

``spend_outlier_finding`` gains ``superseded_at`` and ``superseded_reason``.
Imported premium-request spend arrives days late and can be corrected, so
the daily rules replay recent days; a finding whose day no longer qualifies
is kept as an audit row and stamped superseded instead of deleted.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20261004_copilot_user_mapping"
down_revision: Union[str, None] = "20261004_audit_lookup_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the mapping table and the supersession columns."""
    op.create_table(
        "copilot_user_mapping",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("copilot_import_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization", sa.String(255), nullable=False),
        sa.Column("github_login", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id",
            "organization",
            "github_login",
            name="uq_copilot_user_mapping_login",
        ),
    )
    op.create_index(
        "ix_copilot_user_mapping_account_user",
        "copilot_user_mapping",
        ["account_id", "user_id"],
    )
    op.add_column(
        "spend_outlier_finding",
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when a replay found the day no longer qualifies",
        ),
    )
    op.add_column(
        "spend_outlier_finding",
        sa.Column(
            "superseded_reason",
            sa.String(64),
            nullable=True,
            comment="Why the finding was superseded, e.g. no_longer_qualifies",
        ),
    )


def downgrade() -> None:
    """Drop the supersession columns and the mapping table."""
    op.drop_column("spend_outlier_finding", "superseded_reason")
    op.drop_column("spend_outlier_finding", "superseded_at")
    op.drop_index(
        "ix_copilot_user_mapping_account_user", table_name="copilot_user_mapping"
    )
    op.drop_table("copilot_user_mapping")
