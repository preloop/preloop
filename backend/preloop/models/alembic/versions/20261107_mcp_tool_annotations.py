"""Store upstream MCP tool annotations on cached proxied tools.

Revision ID: 20261107_mcp_tool_annotations
Revises: 20261106_fleet_work_ref_idx
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261107_mcp_tool_annotations"
down_revision = "20261106_fleet_work_ref_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable ``mcp_tool.annotations`` column (metadata-only DDL)."""
    op.add_column("mcp_tool", sa.Column("annotations", JSONB(), nullable=True))


def downgrade() -> None:
    """Drop ``mcp_tool.annotations``."""
    op.drop_column("mcp_tool", "annotations")
