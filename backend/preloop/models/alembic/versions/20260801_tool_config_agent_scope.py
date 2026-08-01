"""Add optional managed-agent scope to tool_configuration.

Revision ID: 20260801_tool_config_agent_scope
Revises: 20260730_webauthn_credentials
Create Date: 2026-08-01

A ``tool_configuration`` row can now carry an optional ``managed_agent_id``.
Null keeps the historical account-wide semantics; a non-null value scopes the
row to a single managed agent, so a default-disabled builtin (the
``permission_prompt`` tool is the first consumer) can be enabled for one
agent without exposing it, and its tools/list context tax, to every other
MCP client of the account.

The ``uq_account_tool_source`` unique constraint is recreated with the new
column so an account-wide row and per-agent rows can coexist for the same
tool.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260801_tool_config_agent_scope"
down_revision = "20260730_webauthn_credentials"
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the managed_agent_id scope column and widen the unique constraint."""
    op.add_column(
        "tool_configuration",
        sa.Column(
            "managed_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "managed_agent.id",
                ondelete="CASCADE",
                name="fk_tool_configuration_managed_agent_id",
            ),
            nullable=True,
            comment=(
                "Optional managed-agent scope; null = account-wide, set = the "
                "configuration applies only to that agent"
            ),
        ),
    )
    op.create_index(
        "ix_tool_configuration_managed_agent_id",
        "tool_configuration",
        ["managed_agent_id"],
    )
    op.drop_constraint("uq_account_tool_source", "tool_configuration", type_="unique")
    op.create_unique_constraint(
        "uq_account_tool_source",
        "tool_configuration",
        ["account_id", "tool_name", "tool_source", "mcp_server_id", "managed_agent_id"],
    )


def downgrade() -> None:
    """Drop agent-scoped rows and restore the account-wide constraint."""
    op.drop_constraint("uq_account_tool_source", "tool_configuration", type_="unique")
    # Agent-scoped rows cannot be represented without the column; remove them
    # before restoring the narrower constraint to avoid collisions with the
    # account-wide rows.
    op.execute("DELETE FROM tool_configuration WHERE managed_agent_id IS NOT NULL")
    op.drop_index(
        "ix_tool_configuration_managed_agent_id", table_name="tool_configuration"
    )
    op.drop_column("tool_configuration", "managed_agent_id")
    op.create_unique_constraint(
        "uq_account_tool_source",
        "tool_configuration",
        ["account_id", "tool_name", "tool_source", "mcp_server_id"],
    )
