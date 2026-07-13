"""Rename approval_policy to approval_workflow

Revision ID: 20260219_rename_approval_policy
Revises:
Create Date: 2026-02-19

Renames:
- Table: approval_policy -> approval_workflow
- FK columns: approval_policy_id -> approval_workflow_id (in tool_configuration, approval_request, tool_access_rules)
- Self-referencing FK: escalation_policy_id -> escalation_workflow_id (in approval_workflow)
- Unique constraint: uq_account_policy_name -> uq_account_workflow_name
"""

from alembic import op


# revision identifiers
revision = "20260219_rename_approval_policy"
down_revision = "9a5b2c3d4e9q"  # 20260217_add_async_approvals_and_justification
branch_labels = None
depends_on = None


def upgrade():
    # 1. Rename the table
    op.rename_table("approval_policy", "approval_workflow")

    # 2. Rename FK columns in tool_configuration
    op.alter_column(
        "tool_configuration",
        "approval_policy_id",
        new_column_name="approval_workflow_id",
    )

    # 3. Rename FK columns in approval_request
    op.alter_column(
        "approval_request",
        "approval_policy_id",
        new_column_name="approval_workflow_id",
    )

    # 4. Rename FK columns in tool_access_rules
    op.alter_column(
        "tool_access_rules",
        "approval_policy_id",
        new_column_name="approval_workflow_id",
    )

    # 5. Rename self-referencing FK column in approval_workflow
    op.alter_column(
        "approval_workflow",
        "escalation_policy_id",
        new_column_name="escalation_workflow_id",
    )

    # 6. Rename unique constraint
    op.drop_constraint("uq_account_policy_name", "approval_workflow", type_="unique")
    op.create_unique_constraint(
        "uq_account_workflow_name", "approval_workflow", ["account_id", "name"]
    )


def downgrade():
    # Reverse all renames
    op.drop_constraint("uq_account_workflow_name", "approval_workflow", type_="unique")
    op.create_unique_constraint(
        "uq_account_policy_name", "approval_workflow", ["account_id", "name"]
    )

    op.alter_column(
        "approval_workflow",
        "escalation_workflow_id",
        new_column_name="escalation_policy_id",
    )
    op.alter_column(
        "tool_access_rules",
        "approval_workflow_id",
        new_column_name="approval_policy_id",
    )
    op.alter_column(
        "approval_request",
        "approval_workflow_id",
        new_column_name="approval_policy_id",
    )
    op.alter_column(
        "tool_configuration",
        "approval_workflow_id",
        new_column_name="approval_policy_id",
    )

    op.rename_table("approval_workflow", "approval_policy")
