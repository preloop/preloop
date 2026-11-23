"""Opt-in chat flows and durable run reservations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261227_chat_flows"
down_revision = "20261226_ext_approval_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable settings; existing flows remain unavailable to chat."""
    op.add_column("flow", sa.Column("chat_alias", sa.String(40), nullable=True))
    op.create_unique_constraint(
        "uq_flow_chat_alias", "flow", ["account_id", "chat_alias"]
    )
    for table, column, target in (
        ("chat_connection", "default_flow_id", "flow"),
        ("chat_work", "run_flow_id", "flow"),
        ("chat_work", "run_execution_id", "flow_execution"),
    ):
        op.add_column(
            table, sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{table}_{column}", table, target, [column], ["id"], ondelete="SET NULL"
        )
    op.add_column(
        "chat_work", sa.Column("run_reserved_at", sa.DateTime(), nullable=True)
    )
    op.create_index("ix_chat_work_run_flow_id", "chat_work", ["run_flow_id"])
    op.create_index("ix_chat_work_run_reserved_at", "chat_work", ["run_reserved_at"])


def downgrade() -> None:
    """Remove chat flow settings and reservations."""
    for column in ("run_reserved_at", "run_execution_id", "run_flow_id"):
        op.drop_column("chat_work", column)
    op.drop_column("chat_connection", "default_flow_id")
    op.drop_constraint("uq_flow_chat_alias", "flow", type_="unique")
    op.drop_column("flow", "chat_alias")
