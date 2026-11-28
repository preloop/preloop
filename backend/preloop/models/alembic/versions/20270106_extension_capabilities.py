"""Store derived extension capabilities for the inventory filter."""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from preloop.schemas.extension_capabilities import capability_keys

revision = "20270106_extension_capabilities"
down_revision = "20270105_flow_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the capability column and derive it for rows already reported."""
    op.add_column(
        "discovered_extension",
        sa.Column("capabilities", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_discovered_extension_capabilities",
        "discovered_extension",
        ["capabilities"],
        postgresql_using="gin",
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, extension_kind, details FROM discovered_extension")
    )
    update = sa.text(
        "UPDATE discovered_extension SET capabilities = CAST(:caps AS JSONB) "
        "WHERE id = :id"
    )
    for row in rows.fetchall():
        bind.execute(
            update,
            {"id": row.id, "caps": json.dumps(capability_keys(row[1], row[2]))},
        )


def downgrade() -> None:
    """Remove derived capabilities; details remain the source of truth."""
    op.drop_index(
        "ix_discovered_extension_capabilities", table_name="discovered_extension"
    )
    op.drop_column("discovered_extension", "capabilities")
