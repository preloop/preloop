"""Explicit grants for completed account-owned result export, without adoption."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261122_execution_result_grant"
down_revision = "20261120_execution_realtime_seq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create consent records without granting access to any historical run."""
    op.create_table(
        "execution_result_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ci_principal.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    for name in ("account_id", "execution_id", "principal_id"):
        op.create_index(
            f"ix_execution_result_grant_{name}", "execution_result_grant", [name]
        )

    op.execute("""
        CREATE FUNCTION guard_execution_result_grant() RETURNS trigger AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.account_id IS DISTINCT FROM OLD.account_id
             OR NEW.execution_id IS DISTINCT FROM OLD.execution_id
             OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
             OR NEW.result_sha256 IS DISTINCT FROM OLD.result_sha256
             OR NEW.binding IS DISTINCT FROM OLD.binding
             OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
             OR (NEW.granted_by_user_id IS DISTINCT FROM OLD.granted_by_user_id
                 AND NEW.granted_by_user_id IS NOT NULL)
             OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
          THEN RAISE EXCEPTION 'Result export consent is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER execution_result_grant_immutable
        BEFORE UPDATE ON execution_result_grant
        FOR EACH ROW EXECUTE FUNCTION guard_execution_result_grant();
    """)


def downgrade() -> None:
    """Remove export authority together with its records."""
    op.drop_table("execution_result_grant")
    op.execute("DROP FUNCTION guard_execution_result_grant()")
