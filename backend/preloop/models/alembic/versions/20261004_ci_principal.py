"""Stable restricted CI identities and nullable historical attribution.

Revision ID: 20261004_ci_principal
Revises: 20261004_audit_lookup_idx
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261004_ci_principal"
down_revision = "20261004_audit_lookup_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add explicit machine credentials without reclassifying existing rows."""
    op.create_table(
        "ci_principal",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "administered_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "flow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tracker_id", UUID(as_uuid=True), nullable=False),
        sa.Column("repository_identifier", sa.String(100), nullable=False),
        sa.Column("repository_binding", sa.JSON(), nullable=False),
        sa.Column("grant", sa.JSON(), nullable=False),
    )
    for column in ("id", "account_id", "project_id", "flow_id"):
        op.create_index(f"ix_ci_principal_{column}", "ci_principal", [column])
    op.create_index(
        "ix_ci_principal_account_binding",
        "ci_principal",
        ["account_id", "project_id", "flow_id"],
    )
    op.add_column(
        "api_key",
        sa.Column(
            "credential_type", sa.String(32), nullable=False, server_default="legacy"
        ),
    )
    op.add_column(
        "api_key", sa.Column("credential_version", sa.Integer(), nullable=True)
    )
    op.add_column("api_key", sa.Column("ci_actions", sa.JSON(), nullable=True))
    for table in ("api_key", "flow_execution", "webhook_endpoint"):
        op.add_column(
            table, sa.Column("ci_principal_id", UUID(as_uuid=True), nullable=True)
        )
        op.create_index(f"ix_{table}_ci_principal_id", table, ["ci_principal_id"])
        op.create_foreign_key(
            f"fk_{table}_ci_principal",
            table,
            "ci_principal",
            ["ci_principal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        if table != "api_key":
            op.add_column(
                table,
                sa.Column("initiating_ci_key_id", UUID(as_uuid=True), nullable=True),
            )
            op.create_index(
                f"ix_{table}_initiating_ci_key_id", table, ["initiating_ci_key_id"]
            )
            op.create_foreign_key(
                f"fk_{table}_initiating_ci_key",
                table,
                "api_key",
                ["initiating_ci_key_id"],
                ["id"],
                ondelete="SET NULL",
            )
    # No ownership backfill: null history is not owned by any machine.


def downgrade() -> None:
    """Refuse to erase machine mode markers while restricted keys exist."""
    # Otherwise a downgraded legacy server could authenticate a CI key as its
    # issuing owner. Explicitly revoke/remove such credentials before rollback.
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM api_key WHERE credential_type <> 'legacy' "
            "OR credential_version IS NOT NULL OR ci_principal_id IS NOT NULL OR ci_actions IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM ci_principal) "
            "OR EXISTS (SELECT 1 FROM flow_execution WHERE ci_principal_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM webhook_endpoint WHERE ci_principal_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError(
            "Remove restricted CI keys and retained identities before downgrading the identity schema"
        )
    for table in ("webhook_endpoint", "flow_execution", "api_key"):
        if table != "api_key":
            op.drop_constraint(
                f"fk_{table}_initiating_ci_key", table, type_="foreignkey"
            )
            op.drop_index(f"ix_{table}_initiating_ci_key_id", table_name=table)
            op.drop_column(table, "initiating_ci_key_id")
        op.drop_constraint(f"fk_{table}_ci_principal", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_ci_principal_id", table_name=table)
        op.drop_column(table, "ci_principal_id")
    for column in ("ci_actions", "credential_version", "credential_type"):
        op.drop_column("api_key", column)
    op.drop_table("ci_principal")
