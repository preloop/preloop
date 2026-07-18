"""Add CHECK constraints for enum-like string marker columns.

Revision ID: 20260712_enum_checks
Revises: 20260712_budget_nnd
Create Date: 2026-07-12

Enforces allowed values for agent_control_command.status and
api_usage.cost_source / usage_source at the database layer so invalid
markers cannot be written by buggy callers.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_enum_checks"
down_revision: Union[str, None] = "20260712_budget_nnd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add CHECK constraints for status / cost_source / usage_source."""
    # Normalize any unexpected legacy values before enforcing the constraint.
    op.execute(
        """
        UPDATE api_usage
        SET cost_source = NULL
        WHERE cost_source IS NOT NULL
          AND cost_source NOT IN (
              'override', 'model_config', 'catalog', 'subscription', 'unpriced'
          )
        """
    )
    op.execute(
        """
        UPDATE api_usage
        SET usage_source = NULL
        WHERE usage_source IS NOT NULL
          AND usage_source NOT IN ('provider', 'estimated', 'partial')
        """
    )
    op.execute(
        """
        UPDATE agent_control_command
        SET status = 'failed'
        WHERE status NOT IN (
            'pending', 'delivered', 'acked', 'failed', 'expired'
        )
        """
    )

    op.create_check_constraint(
        "ck_agent_control_command_status",
        "agent_control_command",
        "status IN ('pending', 'delivered', 'acked', 'failed', 'expired')",
    )
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'catalog', 'subscription', 'unpriced')",
    )
    op.create_check_constraint(
        "ck_api_usage_usage_source",
        "api_usage",
        "usage_source IS NULL OR usage_source IN ('provider', 'estimated', 'partial')",
    )


def downgrade() -> None:
    """Drop enum-like CHECK constraints."""
    op.drop_constraint("ck_api_usage_usage_source", "api_usage", type_="check")
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.drop_constraint(
        "ck_agent_control_command_status",
        "agent_control_command",
        type_="check",
    )
