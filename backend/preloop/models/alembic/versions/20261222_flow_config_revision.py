"""Add flow configuration revisions and record them on executions.

``flow.config_revision`` backs conditional flow updates (ETag/If-Match,
#2375); ``flow_execution.flow_revision`` records which revision an execution
was created with.

Both columns are added without a table rewrite: PostgreSQL 11+ stores a
constant default in the catalog, and a nullable column with no default is a
catalog-only change. ``lock_timeout`` bounds the wait for the brief ACCESS
EXCLUSIVE lock so a busy table fails the migration instead of queueing every
reader behind it.

Revision ID: 20261222_flow_config_revision
Revises: 20261221_records_merge
"""

import sqlalchemy as sa
from alembic import op

revision = "20261222_flow_config_revision"
down_revision = "20261221_records_merge"
branch_labels = None
depends_on = None


def _bounded_lock_wait() -> None:
    """Fail fast instead of queueing behind long transactions on the table."""
    op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    """Add both columns; existing flows start at revision 1."""
    _bounded_lock_wait()
    op.add_column(
        "flow",
        sa.Column(
            "config_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column("flow_revision", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop both columns."""
    _bounded_lock_wait()
    op.drop_column("flow_execution", "flow_revision")
    op.drop_column("flow", "config_revision")
