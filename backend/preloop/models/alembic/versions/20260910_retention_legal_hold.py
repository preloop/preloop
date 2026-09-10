"""Legal hold records plus the derived enforcement flags they drive.

Revision ID: 20260910_retention_hold
Revises: 20260908_webhook_delivery_key
Create Date: 2026-09-10

``legal_hold`` is the account-visible record: who froze what, why, and the
release with its own actor and reason. The boolean columns added to
``flow_execution``, ``approval_request`` and ``flow_artifact`` are the derived
enforcement state the retention purge and the evidence janitor test, written
in the same transaction as the record so a batch DELETE stays a single table
query.

Retention settings themselves need no migration: they live in
``account.meta_data`` under ``retention``, the same place the approval window
cap and subject governance live.

Downgrade drops the flags and the table. It does not resurrect anything a
purge already removed, which is the honest limit of a reversible migration
over a destructive feature.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260910_retention_hold"
down_revision: Union[str, None] = "20260908_webhook_delivery_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_FLAGGED_TABLES = ("flow_execution", "approval_request", "flow_artifact")


def upgrade() -> None:
    """Create the hold record table and the three enforcement flags."""
    op.create_table(
        "legal_hold",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
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
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "placed_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "released_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_legal_hold_id", "legal_hold", ["id"])
    op.create_index("ix_legal_hold_account_id", "legal_hold", ["account_id"])
    op.create_index(
        "ix_legal_hold_placed_by_user_id", "legal_hold", ["placed_by_user_id"]
    )
    op.create_index("ix_legal_hold_placed_at", "legal_hold", ["placed_at"])
    op.create_index(
        "ix_legal_hold_account_released", "legal_hold", ["account_id", "released_at"]
    )
    # One active hold per resource. Two teams asking for the same freeze is
    # one freeze, and concurrent callers race past a service-level check.
    op.create_index(
        "uq_legal_hold_active_resource",
        "legal_hold",
        ["account_id", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )

    for table in _FLAGGED_TABLES:
        op.add_column(
            table,
            sa.Column(
                "legal_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        # Partial index: the purge and the janitor only ever ask "is this one
        # held?", and held rows are the rare case. A full index on a boolean
        # that is false for every historical row would be dead weight.
        op.create_index(
            f"ix_{table}_legal_hold",
            table,
            ["legal_hold"],
            postgresql_where=sa.text("legal_hold"),
        )


def downgrade() -> None:
    """Drop the flags and the hold records."""
    for table in _FLAGGED_TABLES:
        op.drop_index(f"ix_{table}_legal_hold", table_name=table)
        op.drop_column(table, "legal_hold")
    op.drop_index("uq_legal_hold_active_resource", table_name="legal_hold")
    op.drop_index("ix_legal_hold_account_released", table_name="legal_hold")
    op.drop_index("ix_legal_hold_placed_at", table_name="legal_hold")
    op.drop_index("ix_legal_hold_placed_by_user_id", table_name="legal_hold")
    op.drop_index("ix_legal_hold_account_id", table_name="legal_hold")
    op.drop_index("ix_legal_hold_id", table_name="legal_hold")
    op.drop_table("legal_hold")
