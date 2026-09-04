"""Add account.default_runner_pool.

Revision ID: 20260904_acct_runner_pool
Revises: 20260903_flow_timeout
Create Date: 2026-09-04

Private runners are the account default. When a flow has no runner_pool
and a trigger does not override it, executions lease to
account.default_runner_pool when set, otherwise to any online private
runner. The literal 'server' opts the account into the hosted executor.
NULL keeps the 'any online private runner' default.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260904_acct_runner_pool"
down_revision = "20260903_flow_timeout"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable account default runner pool column."""
    op.add_column(
        "account",
        sa.Column(
            "default_runner_pool",
            sa.String(length=200),
            nullable=True,
            comment=(
                "Account default runner pool: a runner id, name, or label; "
                "the literal 'server' for Preloop hosted; NULL means any "
                "online private runner."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the account default runner pool column."""
    op.drop_column("account", "default_runner_pool")
